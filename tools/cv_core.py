"""Context Vault 极薄基础设施底座 (纯 Python 3.9+ 标准库, 零第三方依赖)。

集中存放被多个业务模块共用的只读/落盘/DB/Token 原语，彻底消除：
1. 重复定义的 vault_dir / fmt_usd / token 预算裁剪 / _cv() 延迟导入副本；
2. 每次 cv_core.db() 打开连接时重复执行 WAL 与 7 次 ALTER TABLE 异常开销。
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import os
import socketserver
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id TEXT PRIMARY KEY, created_at TEXT, agent TEXT, source_format TEXT,
  provider TEXT, model TEXT, session_ref TEXT, originator TEXT,
  completeness TEXT, tokens_in INTEGER, tokens_out INTEGER, reasoning_tokens INTEGER,
  cache_read INTEGER, cache_write INTEGER, total_tokens INTEGER, cost_usd REAL,
  usage_events INTEGER, blocks_total INTEGER, blocks_dropped INTEGER, path TEXT,
  parent_snapshot TEXT, fork_of TEXT, migrated_from TEXT, migration_baseline TEXT,
  cwd TEXT, parent_session_ref TEXT, summary_title TEXT);
CREATE TABLE IF NOT EXISTS ledger_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, occurred_at TEXT, kind TEXT,
  model TEXT, provider TEXT, client TEXT, tokens_saved INTEGER, cost_usd REAL,
  snapshot_id TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS probe_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, profile TEXT, endpoint TEXT,
  status TEXT, http_code INTEGER, latency_ms INTEGER, models TEXT, missing_models TEXT);
CREATE TABLE IF NOT EXISTS ops_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, action TEXT, status TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_parent ON snapshots(parent_snapshot);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_ref ON snapshots(agent, session_ref);
CREATE INDEX IF NOT EXISTS idx_probe_status_id ON probe_events(status, id);
CREATE INDEX IF NOT EXISTS idx_probe_ts ON probe_events(ts);
CREATE INDEX IF NOT EXISTS idx_ops_ts ON ops_events(ts);
CREATE INDEX IF NOT EXISTS idx_ops_kind ON ops_events(kind);
CREATE TABLE IF NOT EXISTS vault_meta(key TEXT PRIMARY KEY, value TEXT);
"""

_SNAP_EXTRA_COLS = (
    "parent_snapshot", "fork_of", "migrated_from", "migration_baseline",
    "cwd", "parent_session_ref", "summary_title",
)
_MIGRATED_DBS: set[tuple[str, int, int]] = set()
_DB_MIG_LOCK = threading.Lock()


class SnapshotNotFound(LookupError):
    """快照不存在 — 领域层可捕获错误。此前抛 SystemExit, 它不是 Exception 子类,
    HTTP 层的 except Exception 接不住, 控制台 /migrate 会直接断连而非返回 404 JSON。"""


class BadMigrateTarget(ValueError):
    """迁移目标不合法 (HTTP 层映射 400; CLI 由 argparse choices 前置拦截)。"""


def cv_mod():
    """延迟取得主模块 cv, 兼容以 __main__ / tools.cv 运行及测试对 cv.HOME 等的 monkeypatch。"""
    mod = sys.modules.get("cv")
    if mod is not None and hasattr(mod, "HOME"):
        return mod
    import cv as _cv_m
    return _cv_m


def reset_db_cache(vault: Path | None = None) -> None:
    """清理进程内已迁移 DB 缓存 (正常业务无需调用; 供测试或原地替换库后使用)。"""
    with _DB_MIG_LOCK:
        if vault is None:
            _MIGRATED_DBS.clear()
        else:
            key_p = os.path.normcase(os.path.abspath(str(Path(vault) / "vault.db")))
            for k in [item for item in _MIGRATED_DBS if item[0] == key_p]:
                _MIGRATED_DBS.discard(k)


def rotate_log_if_needed(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """单文件体积超阈值时滚动为 .1 (防常驻日志无限膨胀)。"""
    try:
        if path.is_file() and path.stat().st_size >= max_bytes:
            bak = path.with_name(path.name + ".1")
            if bak.exists():
                bak.unlink()
            path.replace(bak)
    except OSError:
        pass


def vault_dir(args) -> Path:
    raw = str(getattr(args, "vault", None) or "vault")
    p = Path(raw).expanduser()
    default_cwd = str(Path.cwd() / "vault")
    if raw in ("vault", default_cwd) and not (p / "vault.db").exists() and getattr(args, "cmd", None) != "init":
        ref_mod = sys.modules.get("cv")
        ref_file = getattr(ref_mod, "__file__", None) or __file__
        repo_vault = Path(ref_file).resolve().parent.parent / "vault"
        if (repo_vault / "vault.db").exists():
            return repo_vault
    return p


def db(vault: Path) -> sqlite3.Connection:
    """打开 vault.db 连接; 同进程对已校验过 WAL 与扩展列的非空库直接跳过重复 DDL。"""
    db_path = Path(vault) / "vault.db"
    norm_path = os.path.normcase(os.path.abspath(str(db_path)))
    try:
        st = db_path.stat()
        stamp = (norm_path, st.st_ino, st.st_ctime_ns) if st.st_size > 0 else None
    except OSError:
        stamp = None

    if stamp is not None:
        with _DB_MIG_LOCK:
            if stamp in _MIGRATED_DBS:
                conn = sqlite3.connect(db_path, timeout=10)
                conn.row_factory = sqlite3.Row
                return conn

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # 其他连接持锁时切不上就下次再切, 不阻塞本次
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    except sqlite3.OperationalError:
        cols = set()
    if cols:
        added = False
        for col in _SNAP_EXTRA_COLS:
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} TEXT")
                    added = True
                except sqlite3.OperationalError:
                    pass
        if added:
            conn.commit()
        try:
            cols2 = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
            if all(c in cols2 for c in _SNAP_EXTRA_COLS):
                st2 = db_path.stat()
                stamp2 = (norm_path, st2.st_ino, st2.st_ctime_ns)
                with _DB_MIG_LOCK:
                    _MIGRATED_DBS.add(stamp2)
        except (OSError, sqlite3.Error):
            pass
    return conn


def atomic_write_text(path: Path, text: str, *, fsync: bool = False) -> None:
    """同目录临时文件 + os.replace 原子落盘; 进程中途被杀或并发写时不留半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def fmt_usd(x) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "-"


def estimate_text_tokens(text: str) -> int:
    """文本 token 粗估 (预算装配与裁剪同口径): CJK ≈ 1 字/tok, ASCII ≈ 4 字/tok。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4 + 1


def parse_budget_tokens(val) -> int | None:
    """严格解析可选 budget_tokens 参数: 拒绝 bool 和非整数字符串。"""
    if val is None:
        return None
    if isinstance(val, bool):
        raise ValueError("budget_tokens 必须为整数, 不能是布尔值")
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return int(val.strip())
        except ValueError as e:
            raise ValueError(f"无效的 budget_tokens: {val!r}") from e
    raise ValueError(f"无效的 budget_tokens: {val!r}")


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """二分截断单条文本, 确保估算 token 数不超过 max_tokens。"""
    if max_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= max_tokens:
        return text
    if max_tokens <= 1:
        return "…"
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + "…"
        if estimate_text_tokens(cand) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return (text[:best] + "…") if best > 0 else "…"


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """本地回环 HTTP 服务器基类: 跳过 socket.getfqdn 反查 (macOS/离线环境避免 15s+ 阻塞)。"""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host or "127.0.0.1"
        self.server_port = port


def user_agent() -> str:
    """统一出站 HTTP User-Agent (动态对齐 cv.VERSION, 避免硬编码旧版本号)."""
    try:
        ver = str(getattr(cv_mod(), "VERSION", "")).strip()
    except Exception:
        ver = ""
    return f"ContextVault/{ver}" if ver else "ContextVault"
