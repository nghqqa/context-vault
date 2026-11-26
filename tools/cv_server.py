"""Context Vault 常驻 HTTP 热索引服务、Web 控制台、记忆 CRUD、语义增强与统一 /aml/* 接口。"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import gzip
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cv_core import LocalThreadingHTTPServer, cv_mod as _cv, user_agent
from cv_index import VaultIndex, block_text, format_snapshot_messages
from cv_ui import load_console_html as _load_console_html


# ------------------------------------------------- 缓存、数据库护栏与上下文

CONSOLE_HTML = _load_console_html()

_CLIENT_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
MAX_POST_BYTES = 32 * 1024 * 1024  # 单次 POST 上限 32 MiB, 防异常大包 OOM
GZIP_MIN_BYTES = 4096              # ≥4KB 响应在客户端声明 gzip 时自动压缩 (零依赖)
SERVE_FALLBACK_PORTS = [18791, 28791]
_SID_RE = re.compile(r"cv_[0-9a-f]{8,64}")


def _file_stamp(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _db_stamp(p: Path):
    """同时采集主库与 -wal 文件指纹: SQLite WAL 写入会先改 -wal 而不立刻动主库 mtime。"""
    return (_file_stamp(p), _file_stamp(Path(str(p) + "-wal")))


class _ServerCache:
    """进程内轻量结果缓存 (隔离在每个 build_vault_server 实例内, 避免单测串态)。
    key 纳入 DB + WAL 文件指纹, 外部进程写入自动失效; 服务内写接口亦会显式 clear()。"""

    def __init__(self, max_entries: int = 128):
        self._lock = threading.Lock()
        self._data: dict = {}
        self._max = max_entries

    def get_or_compute(self, key, fn):
        with self._lock:
            if key in self._data:
                return self._data[key]
        val = fn()
        with self._lock:
            if len(self._data) >= self._max:
                self._data.clear()
            self._data[key] = val
        return val

    def clear(self):
        with self._lock:
            self._data.clear()


def _ensure_vault_tables_if_missing(conn: sqlite3.Connection) -> None:
    """热读路径只在核心表或二级索引缺失时补齐, 避免每次 GET 跑 DDL 与后台 sync 争 SQLite 写锁。"""
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE"
        " (type='table' AND name IN ('snapshots','ledger_events','memory_items','probe_events'))"
        " OR (type='index' AND name IN ('idx_snapshots_parent','idx_snapshots_agent_ref','idx_probe_status_id'))"
    ).fetchone()[0]
    if n < 7:
        conn.executescript(_cv().SCHEMA)
        conn.commit()


class _KeyStub:
    key = None


def _make_http_logger(log_path: Path) -> logging.Logger:
    """使用标准库 RotatingFileHandler 记录脱敏后的访问日志 (delay=True, 无请求不开句柄)。"""
    logger = logging.getLogger(f"cv.http.{id(log_path)}.{time.monotonic_ns()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


class _ServerCtx:
    """单个服务实例的共享状态、缓存与资源清理上下文 (替代巨型闭包的扁平持参对象)。"""

    def __init__(self, vault: Path, idx: VaultIndex, token: str | None, cv_ledger_mod, ledger_summary_fn):
        self.vault = vault
        self.idx = idx
        self.token = token
        self.cache = _ServerCache()
        self.cv_ledger = cv_ledger_mod
        self.ledger_summary = ledger_summary_fn
        self._aml_router = None
        self._write_lock = threading.Lock()
        self.http_logger = _make_http_logger(vault / "serve_http.log")

    def compute_zcode_usage(self) -> dict:
        zcode_usage = {}
        try:  # 真相源: zcode 主库 model_usage (全量调用); model-io 审计文件仅零头
            zdb = sqlite3.connect(f"file:{_cv().ZCODE_DB}?mode=ro", uri=True)
            try:
                for r in zdb.execute("SELECT model_id, COUNT(*), COALESCE(SUM(input_tokens),0),"
                                     " COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_input_tokens),0)"
                                     " FROM model_usage GROUP BY model_id"):
                    zcode_usage.setdefault(r[0] or "未知",
                                           {"n": r[1], "tin": r[2], "tout": r[3], "cr": r[4]})
            finally:
                zdb.close()
        except sqlite3.Error:
            pass
        return zcode_usage

    def compute_ledger(self) -> dict:
        conn = _cv().db(self.vault)
        try:
            _ensure_vault_tables_if_missing(conn)
            try:
                mem_n = conn.execute("SELECT COUNT(*) n FROM memory_items").fetchone()["n"]
            except sqlite3.OperationalError:
                mem_n = 0
            prices = {}
            prices_p = self.vault / "prices.json"
            num_ok = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
            if prices_p.exists():
                try:
                    pj = json.loads(prices_p.read_text(encoding="utf-8"))
                    prices = {m: v for m, v in pj.items()
                              if isinstance(v, dict) and num_ok(v.get("input")) and num_ok(v.get("output"))}
                except (ValueError, AttributeError, OSError):
                    pass
            zcode_raw = self.cache.get_or_compute(
                ("zcode_usage", str(_cv().ZCODE_DB), _db_stamp(_cv().ZCODE_DB)),
                self.compute_zcode_usage,
            )
            zcode_usage = {m: dict(v) for m, v in zcode_raw.items()}
            zcode_snapshots = 0
            try:
                zn = conn.execute("SELECT COUNT(*) FROM snapshots WHERE agent='zcode'").fetchone()[0]
                if zn:
                    zcode_snapshots = zn
            except sqlite3.Error:
                pass
            for m, a in zcode_usage.items():
                pr = prices.get(m)
                if pr is None:
                    a["cost"] = None
                    continue
                try:
                    crp = pr.get("cache_read")
                    a["cost"] = round((a["tin"] * float(pr["input"]) + a["tout"] * float(pr["output"])
                                       + a["cr"] * (float(crp) if num_ok(crp) else 0)) / 1e6, 2)
                except (TypeError, ValueError):
                    a["cost"] = None
            lt = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events"
                              " WHERE source='headroom-lifetime'").fetchone()
            by_model = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c"
                                    " FROM ledger_events WHERE kind='compression'"
                                    " GROUP BY model ORDER BY c DESC").fetchall()
            monthly = conn.execute("SELECT substr(occurred_at,1,7) m, SUM(cost_usd) c"
                                   " FROM ledger_events WHERE kind='compression'"
                                   " GROUP BY m ORDER BY m").fetchall()
        finally:
            conn.close()
        raw = json.loads(lt["raw"]) if lt else {}
        return {"lifetime": {"tokens_saved": lt["tokens_saved"] if lt else 0,
                             "saved_usd": lt["cost_usd"] if lt else 0, **raw},
                "by_model": [dict(r) for r in by_model],
                "monthly": [dict(r) for r in monthly],
                "owned": _cv().owned_stats(self.vault), "memories": mem_n,
                "zcode_usage": zcode_usage, "zcode_snapshots": zcode_snapshots}

    def get_ledger_cached(self) -> dict:
        key = (
            "ledger",
            _db_stamp(self.vault / "vault.db"),
            _file_stamp(self.vault / "snapshots"),
            _file_stamp(self.vault / "prices.json"),
            str(_cv().ZCODE_DB),
            _db_stamp(_cv().ZCODE_DB),
        )
        return self.cache.get_or_compute(key, self.compute_ledger)

    def get_stats_cached(self) -> dict:
        key = (
            "stats",
            _db_stamp(self.vault / "vault.db"),
            _file_stamp(self.vault / "snapshots"),
        )
        return self.cache.get_or_compute(
            key,
            lambda: {
                "stats": self.ledger_summary(self.vault),
                "owned": _cv().owned_stats(self.vault),
                "usage": self.cv_ledger.usage_stats_summary(self.vault),
            },
        )

    def get_usage_base_cached(self, w: str, acct) -> dict:
        key = (
            "usage",
            _db_stamp(self.vault / "vault.db"),
            _file_stamp(self.vault / "accounts.json"),
            _file_stamp(self.vault / "prices.json"),
            _file_stamp(self.vault / "pricing.json"),
            w,
            acct,
            int(time.time() // 30),
        )
        return self.cache.get_or_compute(key, lambda: self.cv_ledger.usage_payload(self.vault, window=w, account=acct))

    def get_projects_cached(self, p_proj, p_sub: bool, p_arch: bool, p_ag):
        key = (
            "projects",
            _db_stamp(self.vault / "vault.db"),
            str(_cv().ZCODE_DB),
            int(time.time() // 30),
            p_proj,
            p_sub,
            p_arch,
            p_ag,
        )
        return self.cache.get_or_compute(
            key,
            lambda: _cv().group_conversations_by_project(
                self.vault, project=p_proj, include_subagents=p_sub, include_archived=p_arch, agent=p_ag
            ),
        )

    def get_aml_router(self):
        if self._aml_router is None:
            import aml_adapter as _aml
            self._aml_router = _aml.TenantRouter(self.vault, default_idx=self.idx)
        return self._aml_router

    def stats_overlay(self, sid: str, s: dict) -> dict:
        """快照文件不可变: zcode 历史版本的用量在索引层回填, 详情/预览优先展示回填值。"""
        if s.get("usage_events"):
            return s
        try:
            c = _cv().db(self.vault)
            r = c.execute("SELECT tokens_in, tokens_out, reasoning_tokens, cache_read, cache_write,"
                          " total_tokens, usage_events FROM snapshots WHERE id=?", (sid,)).fetchone()
            c.close()
        except sqlite3.Error:
            return s
        return {**s, **dict(r)} if r and r["usage_events"] else s

    def close(self) -> None:
        if self._aml_router is not None:
            self._aml_router.close_all()
        for h in list(self.http_logger.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            self.http_logger.removeHandler(h)


def _sid_ok(sid: str) -> bool:
    return bool(_SID_RE.fullmatch(str(sid or "")))


def _parse_int_arg(raw, name: str, *, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"invalid {name}: 需要整数")
    try:
        v = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: 需要整数") from exc
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _agy_sync_json(res: dict) -> dict:
    return {
        "applied": res["applied"],
        "targets_count": len(res["targets"]),
        "targets": [
            {
                "cid": t["cid"], "sid": t["sid"], "agent": t["agent"],
                "cwd": t["cwd"], "title": t["title"], "reason": t["reason"],
                "has_db": t["has_db"], "has_summary": t["has_summary"], "has_proto": t["has_proto"],
            }
            for t in res["targets"]
        ],
        "empty_projects": [
            {"id": e["id"], "name": e["name"], "cwd": e["cwd"]}
            for e in res.get("empty_projects") or []
        ],
        "db_files": res["db_files"],
        "summary_ids": res["summary_ids"],
        "proto_ids": res["proto_ids"],
        "dup_count": res["dup_count"],
        "agent_count": res["agent_count"],
        "removed_dbs": res["removed_dbs"],
        "removed_projects": res.get("removed_projects", 0),
        "cleaned_summaries": res["cleaned_summaries"],
        "cleaned_proto": res["cleaned_proto"],
        "summary_check_error": res.get("summary_check_error"),
    }


# ------------------------------------------------- GET 路由处理函数 (表驱动分派)

def _get_root(h, ctx: _ServerCtx, q: dict):
    h._send(_load_console_html().encode("utf-8"), "text/html; charset=utf-8")


def _get_healthz(h, ctx: _ServerCtx, q: dict):
    ctx.idx.request_rebuild(60)
    h._json({"service": "context-vault", "status": "ok",
             "snapshots": len(ctx.idx.meta), "terms": ctx.idx.term_count})


def _get_audit(h, ctx: _ServerCtx, q: dict):
    since = q.get("since", "")
    sum_only = (q.get("summary_only") == "1")
    akey = ("audit", _db_stamp(ctx.vault / "vault.db"), since, sum_only)
    h._json(ctx.cache.get_or_compute(
        akey, lambda: _cv().audit_summary_and_timeline(ctx.vault, since, "all", summary_only=sum_only)))


def _get_memory(h, ctx: _ServerCtx, q: dict):
    try:
        mlim = _parse_int_arg(q.get("limit", 8), "limit", lo=1, hi=200)
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    mkind = q.get("kind", "")
    proj = q.get("project") or None
    since = q.get("since") or None
    try:
        memories = _cv().memory_search_api(
            ctx.vault, q.get("q", ""), mlim, kind=mkind,
            include_history=(q.get("history") == "1"),
            project=proj, since=since,
        )
    except ValueError as e:
        return h._json({"error": str(e)}, 400)
    h._json({
        "query": q.get("q", ""),
        "kind": mkind,
        "project": proj or "",
        "since": since or "",
        "counts": _cv().memory_counts_api(ctx.vault),
        "projects": _cv().memory_projects_api(ctx.vault),
        "memories": memories,
    })


_RULE_OUT_NAMES = frozenset({"agents.md", "claude.md", ".cursorrules", "copilot-instructions.md"})
_RULE_OUT_EXTS = frozenset({".md", ".mdc", ".cursorrules"})


def _validate_rule_out_path(out_raw, apply: bool = False) -> str | None:
    s = str(out_raw or "").strip()
    if not s:
        return None
    p = Path(s).expanduser()
    if p.exists() and p.is_dir():
        raise ValueError("规则文件路径不能是目录")
    if p.name.lower() not in _RULE_OUT_NAMES and p.suffix.lower() not in _RULE_OUT_EXTS:
        raise ValueError("规则文件仅允许 AGENTS.md / CLAUDE.md / .cursorrules 或 .md/.mdc 文件")
    return str(p)


def _get_memory_compile_rules(h, ctx: _ServerCtx, q: dict):
    try:
        bt = _parse_int_arg(q.get("budget", 2000), "budget_tokens")
        out_p = _validate_rule_out_path(q.get("out"), apply=False)
        res = _cv().compile_project_rules(
            ctx.vault,
            q.get("project", ""),
            target=q.get("target") or "agents",
            out_path=out_p,
            budget_tokens=bt,
            include_summary=(q.get("include_summary") == "1"),
            apply=False,
        )
        h._json(res)
    except (ValueError, OSError) as e:
        h._json({"error": str(e)}, 400)


def _get_handoff(h, ctx: _ServerCtx, q: dict):
    c = _cv()
    try:
        bt = _parse_int_arg(q.get("budget_tokens", 4000), "budget_tokens")
        inc_raw = str(q.get("include_memories", "1")).strip().lower()
        inc_mem = inc_raw not in ("0", "false", "no", "off")
        res = c.build_handoff_api(
            ctx.vault,
            sid=q.get("id") or None,
            project=q.get("project") or None,
            budget_tokens=bt,
            include_memories=inc_mem,
        )
        h._json(res)
    except c.SnapshotNotFound as e:
        h._json({"error": str(e)}, 404)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _get_semantic_status(h, ctx: _ServerCtx, q: dict):
    import cv_semantic as _sem
    h._json(_sem.semantic_status(ctx.vault, probe=(q.get("probe") == "1")))


def _get_skills_list(h, ctx: _ServerCtx, q: dict):
    import cv_skills as _sk
    try:
        ws = Path(q["workspace"]) if q.get("workspace") else None
        h._json(_sk.list_skills_api(
            source=q.get("source") or "zcode",
            include_plugins=(q.get("include_plugins") == "1"),
            target=q.get("target") or "antigravity",
            agy_mode=q.get("agy_mode") or "skills",
            plugin_name=q.get("plugin_name") or "",
            workspace_dir=ws,
            refresh=(q.get("refresh") == "1"),
            vault=ctx.vault,
            connected_only=(q.get("connected_only") == "1"),
        ))
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _get_skills_inspect(h, ctx: _ServerCtx, q: dict):
    import cv_skills as _sk
    try:
        ws = Path(q["workspace"]) if q.get("workspace") else None
        h._json(_sk.inspect_skill_api(
            source=q.get("source") or "zcode",
            skill_id=q.get("id") or q.get("slug") or "",
            target=q.get("target") or "",
            agy_mode=q.get("agy_mode") or "skills",
            plugin_name=q.get("plugin_name") or "",
            include_plugins=(q.get("include_plugins") == "1"),
            normalize=(q.get("normalize", "1") != "0"),
            workspace_dir=ws,
            vault=ctx.vault,
            connected_only=(q.get("connected_only") == "1"),
        ))
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _get_skills_backups(h, ctx: _ServerCtx, q: dict):
    import cv_skills as _sk
    try:
        lim = _parse_int_arg(q.get("limit") or 20, "limit")
    except ValueError:
        return h._json({"error": "invalid limit"}, 400)
    h._json({"backups": _sk.list_skill_backups_api(ctx.vault, limit=lim)})


def _get_ops_mcp(h, ctx: _ServerCtx, q: dict):
    h._json(_cv().mcp_status_api(ctx.vault))


def _get_ops_status(h, ctx: _ServerCtx, q: dict):
    import cv_semantic as _sem
    vdb = ctx.vault / "vault.db"
    idb = ctx.vault / "search_index.db"
    bdir = ctx.vault / "backups"
    bfiles = sorted(bdir.glob("cv-backup-*.zip")) if bdir.is_dir() else []
    latest_b = bfiles[-1] if bfiles else None
    tok = ctx.cv_ledger.load_plan_token(ctx.vault) or ""
    tok_hint = (tok[:4] + "…" + tok[-4:]) if len(tok) >= 10 else ("***" if tok else "")
    accts = ctx.cv_ledger.load_accounts(ctx.vault)
    h._json({
        "version": _cv().VERSION,
        "vault": str(ctx.vault.resolve()),
        "frozen": bool(getattr(sys, "frozen", False)),
        "snapshots": len(ctx.idx.meta),
        "terms": ctx.idx.term_count,
        "db_mb": round(vdb.stat().st_size / 1048576, 2) if vdb.is_file() else 0.0,
        "index_mb": round(idb.stat().st_size / 1048576, 2) if idb.is_file() else 0.0,
        "backups_count": len(bfiles),
        "latest_backup": {
            "name": latest_b.name,
            "size_mb": round(latest_b.stat().st_size / 1048576, 2),
        } if latest_b and latest_b.is_file() else None,
        "plan_token_set": bool(tok),
        "plan_token_hint": tok_hint,
        "accounts_configured": bool(accts.get("profiles")),
        "semantic": _sem.semantic_status(ctx.vault, probe=(q.get("probe") == "1")),
    })


def _get_probe_events(h, ctx: _ServerCtx, q: dict):
    conn = _cv().db(ctx.vault)
    try:
        try:
            plim = _parse_int_arg(q.get("limit", 100), "limit")
        except ValueError:
            return h._json({"error": "invalid limit: 需要整数"}, 400)
        try:
            rows = conn.execute("SELECT ts,profile,status,http_code,latency_ms,models,missing_models"
                                " FROM probe_events ORDER BY id DESC LIMIT ?", (plim,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        conn.close()
    h._json([dict(r) for r in rows])


def _get_ledger(h, ctx: _ServerCtx, q: dict):
    h._json(ctx.get_ledger_cached())


def _get_ledger_usage(h, ctx: _ServerCtx, q: dict):
    w = q.get("window", "5h") or "5h"
    if w not in ctx.cv_ledger.WINDOW_CHOICES:
        return h._json({"error": "invalid window: 可选 5h|24h|7d|30d"}, 400)
    acct = q.get("account") or None
    payload = dict(ctx.get_usage_base_cached(w, acct))
    fast = (q.get("fast") == "1")
    pending = False
    rq = ctx.cv_ledger.remote_quota_cached(ctx.vault, swr=True, nonblocking_cold=fast)
    if isinstance(rq, dict) and rq.get("_pending"):
        pending = True
    elif rq is not None:
        payload["remote_quota"] = rq
    aq = ctx.cv_ledger.agv_quota_cached(swr=True, nonblocking_cold=fast)
    if isinstance(aq, dict) and aq.get("_pending"):
        pending = True
    elif aq is not None:
        payload["remote_quota_agv"] = aq
    if pending:
        payload["quota_pending"] = True
    h._json(payload)


def _get_preview(h, ctx: _ServerCtx, q: dict):
    qid = q.get("id", "")
    if not _sid_ok(qid):
        return h._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
    env2 = ctx.idx.load_env(qid)
    if not env2:
        return h._json({"error": "not found"}, 404)
    blocks = env2["conversation"]
    dist = dict(Counter(b["type"] for b in blocks))
    live, comp = _cv().live_context(blocks)
    live_dist = dict(Counter(b["type"] for b in live))
    s = ctx.stats_overlay(qid, env2["stats"])
    h._json({"id": qid, "agent": env2["capture"].get("agent"),
             "completeness": env2["capture"].get("completeness"),
             "cwd": env2["capture"].get("cwd"),
             "block_dist": dist, "live_blocks": len(live),
             "live_dist": live_dist, "compactions": dist.get("compaction", 0),
             "live_tokens_est": _cv().estimate_tokens(live),
             "history_tokens_in": s["tokens_in"],
             "thinking": dist.get("thinking", 0),
             "agent_messages": dist.get("agent_message", 0),
             "dropped_source": s["blocks_dropped"],
             "no_assistant": env2["capture"].get("completeness") == "no_assistant_text"})


def _get_search(h, ctx: _ServerCtx, q: dict):
    ctx.idx.rebuild()
    try:
        lim = _parse_int_arg(q.get("limit", 5), "limit")
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    proj = q.get("project") or None
    ag = q.get("agent") or None
    since = q.get("since") or None
    hyb = q.get("hybrid")
    use_dense = (hyb == "1")
    if hyb is None:
        try:
            from cv_semantic import DenseProvider
            use_dense = DenseProvider(vault=ctx.vault).available
        except Exception:
            use_dense = False
    if use_dense:
        try:
            from cv_semantic import DenseProvider, hybrid_search
            return h._json(hybrid_search(
                ctx.idx, ctx.vault, q.get("q", ""), lim,
                provider=DenseProvider(vault=ctx.vault),
                project=proj, agent=ag, since=since))
        except ValueError as e:
            return h._json({"error": str(e)}, 400)
        except Exception:
            pass
    try:
        h._json(ctx.idx.search(q.get("q", ""), lim, project=proj, agent=ag, since=since))
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _get_context(h, ctx: _ServerCtx, q: dict):
    ctx.idx.rebuild()
    try:
        lim = _parse_int_arg(q.get("limit", 3), "limit")
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    proj = q.get("project") or None
    ag = q.get("agent") or None
    since = q.get("since") or None
    try:
        h._json(ctx.idx.context(q.get("q", ""), lim, project=proj, agent=ag, since=since))
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _get_recent(h, ctx: _ServerCtx, q: dict):
    ctx.idx.request_rebuild(60)
    meta, sids = ctx.idx.recent_pair()
    total = len(meta)
    try:
        lim = _parse_int_arg(q.get("limit", 5), "limit/offset", lo=1, hi=200)
        off = _parse_int_arg(q.get("offset", 0), "limit/offset", lo=0)
    except ValueError:
        return h._json({"error": "invalid limit/offset: 需要整数"}, 400)
    desc = q.get("order", "desc") != "asc"
    agent_f = (q.get("agent") or "").strip().lower()
    pairs = [(sid, meta[sid]) for sid in sids]
    if agent_f:
        pairs = [(sid, m) for sid, m in pairs if str(m.get("agent") or "").lower() == agent_f]
        total = len(pairs)
    versions = {}
    if q.get("leaves") == "1":
        def _load_leaves():
            c = _cv().db(ctx.vault)
            try:
                parents_ = {r[0] for r in c.execute(
                    "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
                per_sess_ = {(r[0], r[1]): r[2] for r in c.execute(
                    "SELECT agent, session_ref, COUNT(*) FROM snapshots GROUP BY agent, session_ref")}
                sess_of_ = {r[0]: (r[1], r[2]) for r in c.execute(
                    "SELECT id, agent, session_ref FROM snapshots")}
                return parents_, per_sess_, sess_of_
            finally:
                c.close()
        try:
            parents, per_sess, sess_of = ctx.cache.get_or_compute(
                ("recent_leaves", _db_stamp(ctx.vault / "vault.db")), _load_leaves)
            pairs = [kv for kv in pairs if kv[0] not in parents]
            versions = {sid: per_sess.get(sess_of.get(sid), 1) for sid, _ in pairs}
        except sqlite3.Error:
            pass
        total = len(pairs)
    rows = sorted(pairs, key=lambda kv: kv[1]["created_at"] or "", reverse=desc)[off:off + lim]
    h._json({"total": total, "versions_total": len(meta),
             "rows": [{"sid": k, **v, **({"versions": versions[k]} if k in versions else {})}
                      for k, v in rows]})


def _get_timeline(h, ctx: _ServerCtx, q: dict):
    ref = q.get("session", "")
    agent = q.get("agent", "")
    if not ref:
        return h._json({"error": "invalid session: 需要 session_ref"}, 400)
    try:
        c = _cv().db(ctx.vault)
        try:
            if agent:
                rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                 " parent_snapshot FROM snapshots WHERE session_ref=? AND agent=?"
                                 " ORDER BY created_at", (ref, agent)).fetchall()
            else:
                rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                 " parent_snapshot FROM snapshots WHERE session_ref=?"
                                 " ORDER BY created_at", (ref,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        rows = []
    child = {r[5] for r in rows if r[5]}
    days = {}
    for r in rows:
        days.setdefault(str(r["created_at"] or "")[:10], []).append(
            {"sid": r["id"], "time": str(r["created_at"] or "")[11:19],
             "tokens_in": r["tokens_in"] or 0, "total_tokens": r["total_tokens"] or 0,
             "blocks": r["blocks_total"] or 0, "leaf": r["id"] not in child})
    h._json({"session": ref, "agent": agent,
             "days": [{"day": d, "versions": days[d]} for d in sorted(days)]})


def _get_stats(h, ctx: _ServerCtx, q: dict):
    h._json(ctx.get_stats_cached())


def _get_projects(h, ctx: _ServerCtx, q: dict):
    h._json(ctx.get_projects_cached(
        q.get("project") or None,
        q.get("subagents") == "1",
        q.get("archived") == "1",
        q.get("agent") or None,
    ))


def _get_agy_sync(h, ctx: _ServerCtx, q: dict):
    only_ag = q["only_agent"].strip() or None if "only_agent" in q else "zcode"
    res = _cv().sync_antigravity_archived(
        ctx.vault, project=q.get("project") or None, apply=False,
        dedup=(q.get("dedup", "1") == "1"), only_agent=only_ag,
    )
    h._json(_agy_sync_json(res))


def _get_agy_batch(h, ctx: _ServerCtx, q: dict):
    try:
        lim = _parse_int_arg(q.get("limit", 0) or 0, "limit")
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    h._json(_cv().sync_to_antigravity_batch(
        ctx.vault, agent=q.get("agent", "zcode"), project=q.get("project") or None,
        include_archived=(q.get("archived") == "1"), limit=lim, apply=False,
    ))


def _get_migrate_batch(h, ctx: _ServerCtx, q: dict):
    try:
        lim = _parse_int_arg(q.get("limit", 0) or 0, "limit", lo=0)
    except ValueError:
        return h._json({"error": "invalid limit: 需要非负整数"}, 400)
    skip_raw = str(q.get("skip_migrated", "1")).strip().lower()
    try:
        res = _cv().plan_batch_migration(
            ctx.vault,
            source=q.get("source", "all") or "all",
            target=q.get("target", "codex") or "codex",
            project=q.get("project") or None,
            query=q.get("q") or None,
            include_archived=q.get("archived") in ("1", "true"),
            include_subagents=q.get("subagents") in ("1", "true"),
            skip_migrated=skip_raw not in ("0", "false", "no"),
            limit=lim,
        )
    except (_cv().BadMigrateTarget, ValueError) as e:
        return h._json({"error": str(e)}, 400)
    h._json(res)


def _get_messages(h, ctx: _ServerCtx, q: dict):
    qid = q.get("id", "")
    if not _sid_ok(qid):
        return h._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
    try:
        off = _parse_int_arg(q.get("offset", 0) or 0, "offset/limit", lo=0)
        lim = _parse_int_arg(q.get("limit", 40) or 40, "offset/limit", lo=1, hi=100)
    except ValueError:
        return h._json({"error": "invalid offset/limit: 需要整数"}, 400)
    env_m = ctx.idx.load_env(qid)
    if not env_m:
        return h._json({"error": "not found"}, 404)
    res_m = format_snapshot_messages(
        env_m, scope=q.get("scope", "live"), offset=off, limit=lim, query=q.get("q", ""),
    )
    res_m["id"] = qid
    h._json(res_m)


def _get_snapshot(h, ctx: _ServerCtx, q: dict):
    qid = q.get("id", "")
    if not _sid_ok(qid):
        return h._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
    env = ctx.idx.load_env(qid)
    if not env:
        return h._json({"error": "not found"}, 404)
    dist = dict(Counter(b["type"] for b in env["conversation"]))
    h._json({"id": qid, "capture": env["capture"], "lineage": env.get("lineage") or {},
             "stats": ctx.stats_overlay(qid, env["stats"]), "block_dist": dist})


GET_ROUTES = {
    "/": _get_root,
    "/healthz": _get_healthz,
    "/audit": _get_audit,
    "/memory": _get_memory,
    "/memory/compile_rules": _get_memory_compile_rules,
    "/handoff": _get_handoff,
    "/semantic/status": _get_semantic_status,
    "/skills/list": _get_skills_list,
    "/skills/inspect": _get_skills_inspect,
    "/skills/backups": _get_skills_backups,
    "/ops/mcp": _get_ops_mcp,
    "/ops/status": _get_ops_status,
    "/probe_events": _get_probe_events,
    "/ledger": _get_ledger,
    "/ledger/usage": _get_ledger_usage,
    "/preview": _get_preview,
    "/search": _get_search,
    "/context": _get_context,
    "/recent": _get_recent,
    "/timeline": _get_timeline,
    "/stats": _get_stats,
    "/projects": _get_projects,
    "/antigravity/sync": _get_agy_sync,
    "/antigravity/batch": _get_agy_batch,
    "/migrate/batch": _get_migrate_batch,
    "/messages": _get_messages,
    "/snapshot": _get_snapshot,
}


# ------------------------------------------------- POST 路由处理函数 (表驱动分派)

def _post_migrate(h, ctx: _ServerCtx, body: dict):
    mid = str(body.get("id", ""))
    if not _sid_ok(mid):
        return h._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
    if not (ctx.vault / "snapshots" / f"{mid}.json").exists():
        return h._json({"error": f"snapshot not found: {mid}"}, 404)
    try:
        r = _cv().do_import(ctx.vault, mid, body.get("target", "codex"),
                            bool(body.get("apply")), budget_tokens=body.get("budget_tokens"))
    except _cv().SnapshotNotFound as e:
        return h._json({"error": str(e)}, 404)
    except _cv().BadMigrateTarget as e:
        return h._json({"error": str(e)}, 400)
    if str(body.get("target") or "").lower() == "markdown" and r.get("dest"):
        try:
            dp = Path(r["dest"])
            if dp.is_file() and dp.stat().st_size <= 65536:
                r = {**r, "content": dp.read_text(encoding="utf-8", errors="replace")}
        except OSError:
            pass
    h._json(r)


def _post_migrate_project(h, ctx: _ServerCtx, body: dict):
    proj = str(body.get("project") or "").strip()
    if not proj:
        return h._json({"error": "missing project: 需要 project 参数"}, 400)
    target = str(body.get("target") or "antigravity")
    try:
        lim = _parse_int_arg(body.get("limit") or 0, "limit")
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    items = _cv()._resolve_project_items(
        ctx.vault, proj, limit=lim, include_archived=bool(body.get("archived")),
        agent=str(body.get("agent") or "").strip() or None,
    )
    if not items:
        return h._json({"error": f"未找到项目的顶层主对话: {proj}"}, 404)
    apply_flag = bool(body.get("apply")) and target in ("codex", "claude", "antigravity", "zcode", "dsh")
    results = []
    try:
        for it in items:
            results.append(_cv().do_import(ctx.vault, it["id"], target, apply_flag))
    except _cv().SnapshotNotFound as e:
        return h._json({"error": str(e)}, 404)
    except _cv().BadMigrateTarget as e:
        return h._json({"error": str(e)}, 400)
    h._json({
        "project": proj, "target": target, "applied": apply_flag,
        "count": len(results), "dest_dir": str(Path(results[0]["dest"]).parent),
        "files": [r["dest"] for r in results], "total_lines": sum(r["lines"] for r in results),
    })


def _post_sync(h, ctx: _ServerCtx, body: dict):
    with ctx._write_lock:
        res = _cv().sync_vault(ctx.vault, debounce=bool(body.get("debounce")), quiet=True)
        ctx.idx.rebuild()
        ctx.cache.clear()
        if body.get("extract_memory", True):
            try:
                mres = _cv().extract_memories_incremental(ctx.vault)
                res["memories_added"] = mres["added"]
                res["memories_total"] = mres["total"]
            except Exception:
                res["memories_added"] = 0
    h._json(res)


def _post_memory_extract(h, ctx: _ServerCtx, body: dict):
    with ctx._write_lock:
        mres = _cv().extract_memories_incremental(ctx.vault, force=bool(body.get("force")))
        ctx.cache.clear()
    h._json(mres)


def _post_memory_dedup(h, ctx: _ServerCtx, body: dict):
    with ctx._write_lock:
        dres = _cv().memory_dedup(ctx.vault)
        ctx.cache.clear()
    h._json(dres)


def _post_memory_add(h, ctx: _ServerCtx, body: dict):
    try:
        with ctx._write_lock:
            mres = _cv().memory_add_manual(
                ctx.vault,
                body.get("content", ""),
                kind=body.get("kind") or "decision",
                project=body.get("project") or "manual",
                pinned=bool(body.get("pinned", False)),
                supersedes=body.get("supersedes"),
                source=body.get("source") or "manual",
            )
            ctx.cache.clear()
        h._json(mres)
    except (TypeError, ValueError) as e:
        h._json({"error": str(e)}, 400)


def _post_memory_compile_rules(h, ctx: _ServerCtx, body: dict):
    try:
        apply_flag = bool(body.get("apply", False))
        raw_bt = body.get("budget_tokens", body.get("budget", 2000))
        bt = _parse_int_arg(2000 if raw_bt is None else raw_bt, "budget_tokens")
        out_p = _validate_rule_out_path(body.get("out_path", body.get("out")), apply=apply_flag)
        if apply_flag:
            with ctx._write_lock:
                res = _cv().compile_project_rules(
                    ctx.vault,
                    body.get("project", ""),
                    target=body.get("target") or "agents",
                    out_path=out_p,
                    budget_tokens=bt,
                    include_summary=bool(body.get("include_summary", False)),
                    apply=True,
                )
        else:
            res = _cv().compile_project_rules(
                ctx.vault,
                body.get("project", ""),
                target=body.get("target") or "agents",
                out_path=out_p,
                budget_tokens=bt,
                include_summary=bool(body.get("include_summary", False)),
                apply=False,
            )
        h._json(res)
    except (ValueError, OSError) as e:
        h._json({"error": str(e)}, 400)


def _post_memory_delete(h, ctx: _ServerCtx, body: dict):
    try:
        dres = _cv().memory_delete(ctx.vault, body.get("id"))
        ctx.cache.clear()
        h._json(dres)
    except (TypeError, ValueError) as e:
        h._json({"error": str(e)}, 400)


def _post_memory_pin(h, ctx: _ServerCtx, body: dict):
    try:
        pres = _cv().memory_set_pin(ctx.vault, body.get("id"), pinned=bool(body.get("pinned", True)))
        ctx.cache.clear()
        h._json(pres)
    except (TypeError, ValueError) as e:
        h._json({"error": str(e)}, 400)


def _post_aml_add(h, ctx: _ServerCtx, body: dict):
    import aml_adapter as _aml
    try:
        uid = body.get("user_id")
        adapter = ctx.get_aml_router().for_user(uid)
        req_id = body.get("request_id")
        if req_id is None:
            req_id = h.headers.get("Idempotency-Key") or h.headers.get("X-Idempotency-Key")
        ares = adapter.add(
            body.get("messages") or [], session_id=body.get("session_id"), request_id=req_id,
            project=body.get("project") or "aml", cwd=body.get("cwd") or "",
            agent=body.get("agent") or "aml", parent_snapshot=body.get("parent_snapshot"),
            extract_memory=bool(body.get("extract_memory", True)), redact=body.get("redact"),
        )
        if not uid:
            ctx.idx.rebuild()
            ctx.cache.clear()
        h._json(ares)
    except _aml.RequestConflictError as e:
        h._json({"error": str(e)}, 409)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _post_aml_search(h, ctx: _ServerCtx, body: dict):
    try:
        uid = body.get("user_id")
        adapter = ctx.get_aml_router().for_user(uid)
        top_k = _parse_int_arg(body.get("top_k", 100), "top_k")
        ares = adapter.search(
            body.get("query") or "", top_k=top_k, budget_tokens=body.get("budget_tokens"),
            include_memories=bool(body.get("include_memories", True)),
            include_history=bool(body.get("include_history", False)),
        )
        h._json(ares)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _post_fork(h, ctx: _ServerCtx, body: dict):
    fid = str(body.get("id") or "")
    if not _sid_ok(fid):
        return h._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
    msg = str(body.get("message") or "").strip()[:200] or None
    try:
        fres = _cv().fork_snapshot(ctx.vault, fid, msg)
    except _cv().SnapshotNotFound as e:
        return h._json({"error": str(e)}, 404)
    ctx.idx.rebuild()
    ctx.cache.clear()
    h._json(fres)


def _post_agy_sync(h, ctx: _ServerCtx, body: dict):
    only_ag = str(body["only_agent"]).strip() or None if "only_agent" in body else "zcode"
    try:
        res = _cv().sync_antigravity_archived(
            ctx.vault, project=str(body.get("project") or "").strip() or None,
            apply=bool(body.get("apply")), dedup=bool(body.get("dedup", True)), only_agent=only_ag,
        )
    except _cv().BadMigrateTarget as e:
        return h._json({"error": str(e)}, 400)
    h._json(_agy_sync_json(res))


def _post_agy_batch(h, ctx: _ServerCtx, body: dict):
    try:
        lim = _parse_int_arg(body.get("limit") or 0, "limit")
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    ag = str(body.get("agent") if "agent" in body else "zcode").strip()
    try:
        res = _cv().sync_to_antigravity_batch(
            ctx.vault, agent=ag, project=str(body.get("project") or "").strip() or None,
            include_archived=bool(body.get("archived")), limit=lim, apply=bool(body.get("apply")),
        )
    except _cv().BadMigrateTarget as e:
        return h._json({"error": str(e)}, 400)
    h._json(res)


def _post_migrate_batch(h, ctx: _ServerCtx, body: dict):
    target = str(body.get("target") or "codex").strip()
    raw_sel = body.get("selected")
    selected = None
    if raw_sel is not None:
        if not isinstance(raw_sel, list):
            return h._json({"error": "invalid selected: 需要快照 ID 数组"}, 400)
        selected = [str(x or "").strip() for x in raw_sel if str(x or "").strip()]
        bad = [s for s in selected if not _sid_ok(s)]
        if bad:
            return h._json({"error": f"invalid selected id: {bad[0]}"}, 400)
    try:
        lim = _parse_int_arg(body.get("limit") or 0, "limit", lo=0)
        bt = _parse_int_arg(body.get("budget_tokens"), "budget_tokens", lo=1) if body.get("budget_tokens") not in (None, "") else None
    except ValueError as e:
        return h._json({"error": str(e)}, 400)
    apply_flag = bool(body.get("apply")) and target.lower() != "markdown"
    kwargs = dict(
        source=str(body.get("source") or "all").strip() or "all",
        target=target,
        selected=selected,
        project=str(body.get("project") or "").strip() or None,
        query=str(body.get("query") or "").strip() or None,
        include_archived=bool(body.get("archived")),
        include_subagents=bool(body.get("subagents")),
        skip_migrated=bool(body.get("skip_migrated", True)),
        strip_thinking=bool(body.get("strip_thinking")),
        budget_tokens=bt,
        limit=lim,
        apply=apply_flag,
    )
    try:
        if apply_flag:
            with ctx._write_lock:
                res = _cv().run_batch_migration(ctx.vault, **kwargs)
                ctx.cache.clear()
        else:
            res = _cv().run_batch_migration(ctx.vault, **kwargs)
    except _cv().SnapshotNotFound as e:
        return h._json({"error": str(e)}, 404)
    except (_cv().BadMigrateTarget, ValueError) as e:
        return h._json({"error": str(e)}, 400)
    h._json(res)


def _post_probe(h, ctx: _ServerCtx, body: dict):
    baseline_p = ctx.vault / "probe_baseline.json"
    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
    alerts = _cv().probe_once(ctx.vault, _cv().load_profiles(), _cv().resolve_key(_KeyStub()), baseline, verbose=False)
    _cv().atomic_write_text(baseline_p, json.dumps(baseline, ensure_ascii=False, indent=1))
    ctx.cache.clear()
    h._json({"alerts": alerts})


def _post_semantic_config(h, ctx: _ServerCtx, body: dict):
    import cv_semantic as _sem
    if body.get("ollama"):
        return h._json(_sem.configure_ollama(
            ctx.vault, host=str(body.get("host") or "http://127.0.0.1:11434"),
            embed_model=body.get("embeddings_model") or body.get("embed_model"),
            llm_model=body.get("llm_model"), enable_llm=bool(body.get("enable_llm", True)),
        ))
    _sem.save_ai_config(ctx.vault, body)
    h._json(_sem.semantic_status(ctx.vault, probe=False))


def _post_semantic_refine(h, ctx: _ServerCtx, body: dict):
    import cv_semantic as _sem
    try:
        lim = _parse_int_arg(body.get("limit") or 10, "limit", lo=1, hi=100)
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    sid = str(body.get("snapshot_id") or "").strip() or None
    if sid and not _sid_ok(sid):
        return h._json({"error": "invalid snapshot_id"}, 400)
    h._json(_sem.refine_memories_with_llm(ctx.vault, limit=lim, snapshot_id=sid, force=bool(body.get("force"))))


def _post_semantic_titles(h, ctx: _ServerCtx, body: dict):
    import cv_semantic as _sem
    try:
        lim = _parse_int_arg(body.get("limit") or 30, "limit", lo=1, hi=200)
    except ValueError:
        return h._json({"error": "invalid limit: 需要整数"}, 400)
    h._json(_sem.generate_session_titles(
        ctx.vault, limit=lim, force=bool(body.get("force")), use_llm=bool(body.get("use_llm", True)),
    ))


def _dispatch_ops_mcp_action(h, ctx: _ServerCtx, body: dict, api_fn, default_client: str | None = None):
    try:
        with ctx._write_lock:
            if "clients" in body:
                clients = body.get("clients")
                if not isinstance(clients, list):
                    raise ValueError("clients 必须为数组")
                res = api_fn(ctx.vault, client=None, clients=clients)
            else:
                cid = str(body.get("client") or default_client or "").strip()
                res = api_fn(ctx.vault, client=cid)
        h._json(res)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _post_ops_mcp_install(h, ctx: _ServerCtx, body: dict):
    _dispatch_ops_mcp_action(h, ctx, body, _cv().mcp_install_api, default_client="all")


def _post_ops_mcp_uninstall(h, ctx: _ServerCtx, body: dict):
    _dispatch_ops_mcp_action(h, ctx, body, _cv().mcp_uninstall_api, default_client=None)


def _post_ops_backup(h, ctx: _ServerCtx, body: dict):
    try:
        keep_n = _parse_int_arg(body.get("keep") or 10, "keep")
    except ValueError:
        return h._json({"error": "invalid keep"}, 400)
    try:
        with ctx._write_lock:
            res = _cv().backup_vault_api(ctx.vault, keep=keep_n)
        h._json(res)
    except (SystemExit, ValueError, FileNotFoundError) as e:
        h._json({"error": str(e)}, 400)


def _post_ops_prune(h, ctx: _ServerCtx, body: dict):
    apply_flag = bool(body.get("apply"))
    with ctx._write_lock:
        res = _cv().prune_snapshots_api(ctx.vault, apply=apply_flag)
        if apply_flag:
            ctx.idx.rebuild(force=True)
            ctx.cache.clear()
    h._json(res)


def _post_ops_plan_token(h, ctx: _ServerCtx, body: dict):
    p = ctx.vault / "plan_token.json"
    if body.get("clear"):
        if p.exists():
            p.unlink()
        ctx.cv_ledger._REMOTE_Q.update(at=0.0, data=None)
        ctx.cache.clear()
        return h._json({"ok": True, "plan_token_set": False, "message": "已清除智谱 Coding Plan Token"})
    tok = str(body.get("token") or "").strip()
    if not tok:
        return h._json({"error": "token 不能为空 (如需清除请传 clear=true)"}, 400)
    _cv().atomic_write_text(p, json.dumps({"token": tok, "set_at": _cv().now_iso()}, ensure_ascii=False))
    ctx.cv_ledger._REMOTE_Q.update(at=0.0, data=None)
    ctx.cache.clear()
    probe = ctx.cv_ledger.remote_quota_cached(ctx.vault, ttl=0.0) if body.get("verify", True) else None
    h._json({"ok": True, "plan_token_set": True,
             "plan_token_hint": (tok[:4] + "…" + tok[-4:]) if len(tok) >= 10 else "***",
             "probe": probe})


def _post_ops_ledger_sync(h, ctx: _ServerCtx, body: dict):
    try:
        with ctx._write_lock:
            res = ctx.cv_ledger.sync_usage_events(ctx.vault)
            ctx.cache.clear()
        h._json({"ok": True, "added": res["added"], "skipped": res["skipped"],
                 "files_scanned": res["files_scanned"], "message": res["message"]})
    except (FileNotFoundError, ValueError) as e:
        h._json({"error": str(e)}, 400)


def _post_skills_migrate(h, ctx: _ServerCtx, body: dict):
    import cv_skills as _sk
    sel = body.get("selected")
    if sel is not None and not isinstance(sel, list):
        return h._json({"error": "selected 必须为字符串数组"}, 400)
    ws_raw = str(body.get("workspace") or "").strip()
    try:
        with ctx._write_lock:
            res = _sk.migrate_skills(
                source=str(body.get("source") or "zcode"),
                target=str(body.get("target") or "antigravity"),
                selected=sel,
                include_plugins=bool(body.get("include_plugins")),
                agy_mode=str(body.get("agy_mode") or "skills"),
                plugin_name=str(body.get("plugin_name") or ""),
                conflict=str(body.get("conflict") or "skip"),
                normalize=bool(body.get("normalize", True)),
                workspace_dir=Path(ws_raw) if ws_raw else None,
                apply=bool(body.get("apply")),
                vault=ctx.vault,
                connected_only=bool(body.get("connected_only")),
            )
        h._json(res)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _post_skills_remove(h, ctx: _ServerCtx, body: dict):
    import cv_skills as _sk
    slugs = body.get("slugs")
    if not isinstance(slugs, list):
        return h._json({"error": "slugs 必须为字符串数组"}, 400)
    ws_raw = str(body.get("workspace") or "").strip()
    try:
        with ctx._write_lock:
            res = _sk.remove_skills_api(
                target=str(body.get("target") or ""),
                slugs=slugs,
                agy_mode=str(body.get("agy_mode") or "skills"),
                plugin_name=str(body.get("plugin_name") or ""),
                workspace_dir=Path(ws_raw) if ws_raw else None,
                vault=ctx.vault,
                apply=bool(body.get("apply")),
                connected_only=bool(body.get("connected_only")),
            )
        h._json(res)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


def _post_skills_rollback(h, ctx: _ServerCtx, body: dict):
    import cv_skills as _sk
    bid = str(body.get("backup_id") or "").strip()
    try:
        with ctx._write_lock:
            res = _sk.rollback_skill_backup_api(ctx.vault, bid, apply=bool(body.get("apply")))
        h._json(res)
    except ValueError as e:
        h._json({"error": str(e)}, 400)


POST_ROUTES = {
    "/migrate": _post_migrate,
    "/migrate_project": _post_migrate_project,
    "/migrate/batch": _post_migrate_batch,
    "/sync": _post_sync,
    "/memory/extract": _post_memory_extract,
    "/memory/dedup": _post_memory_dedup,
    "/memory/add": _post_memory_add,
    "/memory/compile_rules": _post_memory_compile_rules,
    "/memory/delete": _post_memory_delete,
    "/memory/pin": _post_memory_pin,
    "/aml/add": _post_aml_add,
    "/aml/search": _post_aml_search,
    "/fork": _post_fork,
    "/antigravity/sync": _post_agy_sync,
    "/antigravity/batch": _post_agy_batch,
    "/probe": _post_probe,
    "/semantic/config": _post_semantic_config,
    "/semantic/refine": _post_semantic_refine,
    "/semantic/titles": _post_semantic_titles,
    "/ops/mcp/install": _post_ops_mcp_install,
    "/ops/mcp/uninstall": _post_ops_mcp_uninstall,
    "/ops/backup": _post_ops_backup,
    "/ops/prune": _post_ops_prune,
    "/ops/plan_token": _post_ops_plan_token,
    "/ops/ledger_sync": _post_ops_ledger_sync,
    "/skills/migrate": _post_skills_migrate,
    "/skills/remove": _post_skills_remove,
    "/skills/rollback": _post_skills_rollback,
}


# ------------------------------------------------- HTTP 服务装配与启动

def _make_handler(ctx: _ServerCtx):
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        # HTTP/1.1 + 全端点 Content-Length = 正确 keep-alive
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            line = self.requestline
            try:
                line = urllib.parse.unquote(line)
            except Exception:
                pass
            line = re.sub(r"([?&])token=[^& ]*", r"\1token=***", line, flags=re.IGNORECASE)
            try:
                ctx.http_logger.info(
                    f"{dt.datetime.now().isoformat(timespec='seconds')} "
                    f"{self.client_address[0]} \"{line}\" {code} "
                    f"UA={str(self.headers.get('User-Agent', ''))[:48]}"
                )
            except OSError:
                pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            use_gzip = (len(body) >= GZIP_MIN_BYTES
                        and "gzip" in (self.headers.get("Accept-Encoding") or "").lower())
            out_body = gzip.compress(body, compresslevel=5) if use_gzip else body
            self.send_response(code)
            nc = getattr(self, "_new_cookie", None)
            if nc:
                self.send_header("Set-Cookie", nc)
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            try:
                self.wfile.write(out_body)
            except _CLIENT_DISCONNECT:
                pass

        def _json(self, obj, code: int = 200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", code)

        def _guard(self, post: bool) -> bool:
            """本地访问防护: Host/Origin 必须回环 + POST 强制 application/json + 可选 token 校验。"""
            def _reject(msg: str, code: int) -> bool:
                if post:
                    self.close_connection = True
                self._json({"error": msg}, code)
                return False

            host = (self.headers.get("Host") or "").strip().lower()
            try:
                hn = (urllib.parse.urlsplit("//" + host).hostname or "").lower() if host else ""
            except ValueError:
                hn = ""
            if hn not in ("127.0.0.1", "localhost", "::1"):
                return _reject(f"forbidden: host '{host}' 不在本地白名单", 403)
            org = self.headers.get("Origin")
            if org:
                try:
                    org_hn = (urlparse(org).hostname or "").lower()
                except ValueError:
                    org_hn = ""
                if org_hn not in ("127.0.0.1", "localhost", "::1"):
                    return _reject(f"forbidden: origin '{org}' 非本地", 403)
            if post:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return _reject("forbidden: POST 需要 Content-Type: application/json", 403)
            tok = ctx.token
            if tok:
                header = self.headers.get("X-Vault-Token")
                cookie = ""
                for part in (self.headers.get("Cookie") or "").split(";"):
                    if part.strip().startswith("vault_token="):
                        cookie = part.split("=", 1)[1].strip()
                        break
                qtok = "" if post else parse_qs(urlparse(self.path).query).get("token", [""])[0]
                if not any(cand and hmac.compare_digest(str(cand), str(tok)) for cand in (header, qtok, cookie)):
                    return _reject("unauthorized: 需要 token (?token= 首访, 或 X-Vault-Token 头)", 401)
                if "vault_token=" not in (self.headers.get("Cookie") or "") or (cookie and cookie != tok):
                    self._new_cookie = f"vault_token={tok}; Path=/; HttpOnly; SameSite=Strict"
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._guard(False):
                return
            try:
                fn = GET_ROUTES.get(u.path)
                if fn is not None:
                    return fn(self, ctx, q)
                self._json({
                    "error": "unknown route",
                    "routes": [*GET_ROUTES.keys(), *(f"POST {p}" for p in POST_ROUTES)],
                }, 404)
            except _CLIENT_DISCONNECT:
                pass
            except Exception as e:
                try:
                    ctx.http_logger.error("GET %s 500:\n%s", self.path, traceback.format_exc())
                except Exception:
                    pass
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._guard(True):
                return
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self.close_connection = True
                return self._json({"error": "invalid Content-Length"}, 400)
            if n < 0:
                self.close_connection = True
                return self._json({"error": "invalid Content-Length"}, 400)
            if n > MAX_POST_BYTES:
                self.close_connection = True
                return self._json({"error": f"payload too large: 上限 {MAX_POST_BYTES // 1048576} MB"}, 413)
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
                if not isinstance(body, dict):
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
                fn = POST_ROUTES.get(u.path)
                if fn is not None:
                    return fn(self, ctx, body)
                self._json({"error": "unknown route"}, 404)
            except _CLIENT_DISCONNECT:
                pass
            except Exception as e:
                try:
                    ctx.http_logger.error("POST %s 500:\n%s", self.path, traceback.format_exc())
                except Exception:
                    pass
                self._json({"error": str(e)}, 500)

    return H


def build_vault_server(vault: Path, port: int, token: str | None = None):
    """构建热索引 HTTP 服务 (不阻塞); cmd_serve 与 Pro 托盘共用, 避免实现漂移。"""
    sys.path.insert(0, str(Path(__file__).parent))
    from mcp_server import ledger_summary
    import cv_ledger

    idx = VaultIndex(vault, block_text)
    ctx = _ServerCtx(vault, idx, token, cv_ledger, ledger_summary)
    handler = _make_handler(ctx)

    fallback = getattr(_cv(), "SERVE_FALLBACK_PORTS", SERVE_FALLBACK_PORTS)
    srv, last_err = None, None
    for cand in ([port] if port == 0 else [port, *fallback, 0]):
        try:
            srv = LocalThreadingHTTPServer(("127.0.0.1", cand), handler)
            break
        except OSError as e:
            if cand == port and _cv_service_alive(port):
                ctx.close()
                raise
            last_err = e
    if srv is None:
        ctx.close()
        raise last_err
    actual = srv.server_address[1]
    if port and actual != port:
        print(f"  ⚠ 端口 {port} 不可用 ({last_err}) — 改用 {actual}, 实际地址见 vault/serve.json",
              file=sys.stderr)
    try:
        _cv().atomic_write_text(vault / "serve.json", json.dumps(
            {"url": f"http://127.0.0.1:{actual}", "port": actual, "pid": os.getpid(),
             "started": _cv().now_iso()}))
    except OSError:
        pass
    orig_close = srv.server_close

    def _close_with_resources():
        try:
            ctx.close()
        finally:
            orig_close()

    def _warm_async():
        """常驻服务/托盘启动后后台预热首屏高频缓存、实时额度与 Skill 指纹缓存。"""
        def _run():
            for job in (
                ctx.get_ledger_cached,
                ctx.get_stats_cached,
                lambda: ctx.get_usage_base_cached("5h", None),
                lambda: cv_ledger.remote_quota_cached(vault, swr=True),
                lambda: cv_ledger.agv_quota_cached(swr=True),
            ):
                try:
                    job()
                except Exception:
                    pass
            try:
                import cv_skills as _sk
                _sk.list_skills_api(source="zcode", target="antigravity")
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    srv.server_close = _close_with_resources
    srv.warm_async = _warm_async
    srv.clear_cache = ctx.cache.clear
    return srv, idx


def _cv_service_alive(port: int) -> bool:
    """该端口上是否已有 Context Vault 服务在应答 (区分"重复实例"与"端口被系统保留/他用")。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz",
                                     headers={"User-Agent": user_agent()})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("service") == "context-vault"
    except urllib.error.HTTPError as e:
        return e.code == 401
    except Exception:
        return False


def cmd_serve(args):
    token = args.token or os.environ.get("CV_SERVE_TOKEN")
    srv, idx = build_vault_server(_cv().vault_dir(args), args.port, token=token)
    port = srv.server_address[1]
    base_url = f"http://127.0.0.1:{port}/"
    print(f"vault 热索引就绪: {len(idx.meta)} 快照, {idx.term_count} 词项 (引擎 v2, SQLite 倒排)")
    print(f"Context Vault 服务已启动: {base_url} (Ctrl+C 退出)")
    if token:
        print("  鉴权已启用: 首访 http://127.0.0.1:{}/?token=*** 自动登录; "
              "MCP/脚本侧设 CV_SERVE_TOKEN 同值".format(port))
    print("  MCP server 自动发现本服务走热索引 (vault/serve.json; CV_VAULT_SVC 可显式覆盖)")
    print("  本地访问防护已启用: Host/Origin 回环校验 + POST 强制 application/json"
          " (浏览器跨站与 DNS rebinding 会被 403 拦截, 本地进程不受影响)")
    if hasattr(srv, "warm_async"):
        srv.warm_async()
    if getattr(args, "open", False):
        import webbrowser
        open_url = f"{base_url}?token={urllib.parse.quote(str(token))}" if token else base_url

        def _open_browser():
            time.sleep(0.35)
            try:
                webbrowser.open(open_url)
            except Exception:
                pass

        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()
        idx._close()
