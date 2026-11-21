#!/usr/bin/env python3
"""context-vault MCP server — 把 Vault 变成所有 MCP 客户端的记忆后端。

零第三方依赖 (Python 3.9+ stdlib only), stdio transport, 换行分隔 JSON-RPC 2.0 (MCP 规范)。

工具:
    vault_recent(limit)          最近快照列表 (id/agent/token/完整性)
    vault_search(query, limit)   全库文本搜索, 词频评分
    vault_context(query, limit)  记忆检索: 命中块 + 前后文摘录 (记忆后端核心)
    vault_snapshot(id)           单快照详情 (capture/stats/块分布/活上下文)
    vault_remember(query, limit) 跨会话长期记忆检索 (含演变链)
    vault_stats()                token 账本汇总 (含 headroom lifetime)

接入 (任一 MCP 客户端):
    claude mcp add context-vault -s user -- python /path/to/mcp_server.py --vault /path/to/vault
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

SERVER_NAME = "context-vault"
SERVER_VERSION = "0.7.0"
PROTO = "2025-06-18"
SEARCH_CONTEXT_CHARS = 400  # vault_context 每个命中块返回的摘录长度
MAX_TEXT_LEN = 2000


# ---------------- vault 读取 ----------------

def vault_dir(args) -> Path:
    raw = str(getattr(args, "vault", None) or "vault")
    p = Path(raw).expanduser()
    default_cwd = str(Path.cwd() / "vault")
    if raw in ("vault", default_cwd) and not (p / "vault.db").exists():
        repo_vault = Path(__file__).resolve().parent.parent / "vault"
        if (repo_vault / "vault.db").exists():
            return repo_vault
    return p


def load_all_snapshots(vault: Path) -> list:
    out = []
    snap_dir = vault / "snapshots"
    if not snap_dir.exists():
        return out
    for f in sorted(snap_dir.glob("cv_*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(env, dict) or not isinstance(env.get("capture"), dict) \
                or not isinstance(env.get("stats"), dict) or not isinstance(env.get("conversation"), list):
            continue  # 可解析但结构不完整: 与热索引一致地跳过, 不让单文件废掉冷路径 (全面测试 M6)
        env["_sid"] = f.stem  # 文件名即快照 id (内容寻址)
        out.append(env)
    out.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return out


def ledger_summary(vault: Path) -> dict:
    db = vault / "vault.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    out = {}
    try:
        row = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events "
                           "WHERE source='headroom-lifetime'").fetchone()
        if row:
            out["lifetime_compression_saved_usd"] = row["cost_usd"]
            out["lifetime_tokens_saved"] = row["tokens_saved"]
            try:
                raw = json.loads(row["raw"]) if row["raw"] else {}
            except (ValueError, TypeError):
                raw = {}
            if isinstance(raw, dict):
                out["lifetime_cache_saved_usd"] = raw.get("cache_savings_usd")
                out["lifetime_total_input_tokens"] = raw.get("total_input_tokens")
        r2 = conn.execute("SELECT COUNT(*) n FROM snapshots").fetchone()
        out["snapshots"] = r2["n"] if r2 else 0
    except sqlite3.OperationalError:
        return {}  # 零表空库 (曾由只 connect 不建表的 /stats 路径造出): 诚实按无账本处理, 不毒化冷路径
    finally:
        conn.close()
    return out


def brief(env: dict) -> str:
    c, s = env["capture"], env["stats"]
    return (f"{env.get('_sid')} | {c.get('agent')} | {str(env.get('created_at') or '')[:19]} | "
            f"块={s['blocks_total']} 入={s['tokens_in']} cache读={s['cache_read']} | "
            f"{c.get('completeness')} | cwd={c.get('cwd', '?')}")


# ---------------- 工具实现 ----------------

def t_recent(args_vault, limit: int) -> str:
    snaps = load_all_snapshots(args_vault)
    if not snaps:
        return "vault 为空。先运行: python tools/cv.py export <会话文件>"
    limit = max(1, min(int(limit if limit is not None else 5), 200))  # limit=0 同服务端 /recent 钳 1, hot==cold (修复5)
    lines = [f"vault 内 {len(snaps)} 个快照, 最近 {min(limit, len(snaps))} 个:"]
    for env in snaps[:limit]:
        lines.append("  " + brief(env))
    return "\n".join(lines)


def block_text(b: dict) -> str:
    """检索文本 — 委托 cv.block_text (规范实现, 与热索引同源; 2026-11-02 审查 #7)。"""
    import cv as _cv
    return _cv.block_text(b)


def _readonly_index(args_vault):
    """只读直查 search_index.db (常驻服务未启动时的毫秒级冷路径):
    装载失败/无索引文件返回 None (调用方回退全量扫描)。绝不重建/落盘/删文件。"""
    try:
        import cv as _cv
        idx = _cv.VaultIndex(args_vault, readonly=True)
        return None if getattr(idx, "_broken", False) else idx
    except Exception:
        return None


def t_search(args_vault, query: str, limit: int) -> str:
    """冷回退搜索 — 三级路径: 常驻服务热索引 > 只读 search_index.db 直查 (毫秒级)
    > 全量文件扫描 ColdRanker。三级共用同一评分/折叠/衰减数学, 命中集一致
    (铁律: 常驻只是加速, 不是依赖)。"""
    import cv as _cv
    if not _cv.tokenize(query):
        return "查询词太短"
    limit = max(1, min(int(limit), 200))  # limit<=0 → 1: 0 曾把真实命中报成"未命中"而热路径报"命中 N" (修复4)
    idx = _readonly_index(args_vault)
    if idx is not None:
        try:
            r = idx.search(query, limit)
        finally:
            idx._close()  # 立即释放 Windows SQLite 只读句柄
        via = " (via 本地索引)"
    else:
        snaps = load_all_snapshots(args_vault)
        r = _cv.ColdRanker(snaps, query).search(limit)
        via = ""
    if not r["hits"]:
        return f"未命中: {query}"
    folded = sum(int(h.get("folded", 0) or 0) for h in r["hits"])
    suffix = f" (折叠 {folded} 个历史版本)" if folded else ""
    lines = [f"搜索 '{query}' 命中 {r['total']} 个快照{suffix}{via}:"]
    for h in r["hits"]:
        lines.append(f"  [{h['sid']}] {h['agent']} 得分={h['score']}"
                     f" — {h.get('cwd', '?')} ({str(h.get('created_at', ''))[:10]})")
    return "\n".join(lines)


def t_context(args_vault, query: str, limit: int) -> str:
    """记忆检索: 跨快照找最相关的上下文摘录, 供当前会话引用。
    三级路径同 t_search (同分词/评分/折叠), 保证启停常驻服务不改变命中集合。"""
    import cv as _cv
    if not _cv.tokenize(query):
        return "查询词太短"
    limit = max(1, min(int(limit), 200))  # 同修复4: 0/负数钳 1, 冷热语义一致
    idx = _readonly_index(args_vault)
    if idx is not None:
        try:
            r = idx.context(query, limit)
        finally:
            idx._close()  # 立即释放 Windows SQLite 只读句柄
        via = " (via 本地索引)"
    else:
        snaps = load_all_snapshots(args_vault)
        r = _cv.ColdRanker(snaps, query).context(limit)
        via = ""
    if not r["results"]:
        return f"vault 中无相关上下文: {query}"
    lines = [f"来自 context-vault 的相关历史上下文 (查询: '{query}'):"]
    for res in r["results"]:
        ver = f", 版本链 {res['versions']}" if res.get("versions", 1) > 1 else ""
        lines.append(f"\n--- 快照 {res['sid']} 块#{res['block']} ({res['role']}, "
                     f"cwd={res['cwd']}{ver}) 相关度={res['score']} ---\n{res['excerpt']}")
    lines.append("\n(以上为历史会话摘录; 引用时注明来源快照)")
    return "\n".join(lines)[:12000]


def t_snapshot(args_vault, sid: str) -> str:
    vault = args_vault
    if not re.fullmatch(r"cv_[0-9a-f]{8,64}", str(sid or "")):
        # HTTP 侧同端点有 _sid_ok 同款守卫: MCP 面 id 由模型决定, 直拼路径曾可越 vault
        # 读本机任意 *.json (会话内注入可诱导外带 cwd/内容) — 二轮 mcp-4 high
        return "无效 id: 需要 cv_ 前缀的快照 id"
    p = vault / "snapshots" / f"{sid}.json"
    if not p.exists():
        return f"快照不存在: {sid}"
    try:
        env = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"快照文件损坏或不可读: {sid}"
    if not isinstance(env, dict) or not isinstance(env.get("capture"), dict) \
            or not isinstance(env.get("stats"), dict) or not isinstance(env.get("conversation"), list):
        return f"快照结构不完整: {sid}"
    from collections import Counter
    dist = Counter(b.get("type", "unknown") for b in env["conversation"] if isinstance(b, dict))
    c, s = env["capture"], env["stats"]
    lines = [f"快照 {sid}",
             f"  来源: {c.get('agent')} ({c.get('source_format')}), session_ref={c.get('session_ref')}",
             f"  完整性: {c.get('completeness')} | cwd: {c.get('cwd')} | 创建: {env.get('created_at')}",
             f"  块分布: {dict(dist)}",
             f"  token: 入={s.get('tokens_in', 0):,} 出={s.get('tokens_out', 0):,} "
             f"cache读={s.get('cache_read', 0):,} (events={s.get('usage_events', 0)})"]
    # 活上下文开头
    last = -1
    for i, b in enumerate(env["conversation"]):
        if isinstance(b, dict) and b.get("type") == "compaction":
            last = i
    live = [b for b in env["conversation"][last + 1:] if isinstance(b, dict)]
    head = []
    for b in live[:8]:
        t = block_text(b)[:200]
        if t:
            head.append(f"    [{b.get('role') or b.get('type', '?')}] {t}")
    if head:
        lines.append(f"  活上下文前 {len(live)} 块 (压缩边界后), 开头:")
        lines += head
    return "\n".join(lines)[:8000]


def _format_stats_text(s: dict, own: dict | None, via_hot: bool = False) -> str:
    usd = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else "-"
    hdr = "token 账本 (context-vault, via 热索引):" if via_hot else "token 账本 (context-vault):"
    own = own or {}
    snaps = own.get("snapshots") if own.get("snapshots") is not None else s.get("snapshots", 0)
    sessions = own.get("sessions", 0)
    in_eff = own.get("input_effective") or own.get("tokens_in") or 0
    hit_pct = (own.get("cache_hit_rate") or 0.0) * 100
    return "\n".join([
        hdr,
        f"  快照数: {snaps} (独立会话 {sessions})",
        f"  自有累计输入: {in_eff:,} tokens (缓存命中率 {hit_pct:.1f}%)",
        f"  压缩节省: {s.get('lifetime_tokens_saved', 0):,} tokens = {usd(s.get('lifetime_compression_saved_usd'))}",
        f"  缓存节省: {usd(s.get('lifetime_cache_saved_usd'))}",
        f"  历史总输入: {s.get('lifetime_total_input_tokens', 0):,} tokens",
    ])


def t_stats(args_vault) -> str:
    s = ledger_summary(args_vault)
    if not s:
        return "账本为空。先运行: python tools/cv.py ledger import-headroom"
    sys.path.insert(0, str(Path(__file__).parent))
    import cv as _cv
    return _format_stats_text(s, _cv.owned_stats(args_vault), via_hot=False)


# ---------------- MCP 工具表 ----------------

# ---------------- 常驻服务联动 (cv serve) ----------------

_DEFAULT_SVC = "http://127.0.0.1:8791"
SVC_BASE = os.environ.get("CV_VAULT_SVC", _DEFAULT_SVC)
_VAULT = None  # dispatch 时设置: 用于读 vault/serve.json 发现服务实际地址

try:  # 死服务负缓存时长 (秒, 可配 CV_SERVE_NEG_TTL; 垃圾值/负值按默认或关闭处理, 不炸启动)
    _NEG_TTL = max(0.0, float(os.environ.get("CV_SERVE_NEG_TTL", "300")))
except ValueError:
    _NEG_TTL = 300.0
_svc_neg_until = 0.0   # 负缓存到期时刻 (time.monotonic()); <=0 = 未启用
_svc_neg_base = None   # 负缓存记录的服务地址 (换了目标即失效)
_svc_neg_stamp = None  # 负缓存记录的 serve.json mtime (文件变了即失效 → 服务恢复能被重新发现)
_SERVE_WARN_MTIME = None  # 已告警过的 serve.json mtime: 同一坏文件只告警一行, 变更后重新告警


def _svc_url_ok(url) -> bool:
    """serve.json 条目严格校验 (修复2): scheme 必须 http、禁止 userinfo、host 必须回环。
    旧 startswith("http://127.0.0.1:") 前缀检查可被 "http://127.0.0.1:80@evil.example/"
    绕过 — urlparse 的真实 host 是 evil.example, 可写 serve.json 的进程借此把
    MCP 查询词与 X-Vault-Token 头外带。"""
    if not isinstance(url, str):
        return False
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return (p.scheme == "http"
            and p.username is None and p.password is None
            and p.hostname in ("127.0.0.1", "::1", "localhost"))


def _resolve_base() -> str:
    """服务地址: 显式 (CV_VAULT_SVC / 注入) > vault/serve.json 发现 > 默认 8791。
    首选端口可能落进 Windows 动态保留区段 (Hyper-V/WinNAT 重启后漂移, 绑定报 10013),
    服务会退到别的端口并把实际地址写进 serve.json; 只接受严格回环地址 (修复2),
    不合格条目视为无效 — 等效文件缺失/坏 JSON 的现有处理路径, 走默认发现并告警一行。"""
    global _SERVE_WARN_MTIME
    if SVC_BASE != _DEFAULT_SVC:
        return SVC_BASE
    if _VAULT is not None:
        p = Path(_VAULT) / "serve.json"
        try:
            url = json.loads(p.read_text(encoding="utf-8")).get("url")
        except (OSError, ValueError, AttributeError):
            return _DEFAULT_SVC  # 文件缺失/坏 JSON/非对象: 原有静默回退
        if _svc_url_ok(url):
            return url
        try:  # 条目存在但不合格 (恶意串/损坏): 绝不原样使用, 告警一行
            mtime = p.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != _SERVE_WARN_MTIME:
            _SERVE_WARN_MTIME = mtime
            print(f"mcp_server: serve.json 的 url 不合格 (需 http 回环且无 userinfo), 已忽略: {url!r}",
                  file=sys.stderr)
    return _DEFAULT_SVC


def _serve_stamp():
    if _VAULT is None:
        return None
    try:
        return (Path(_VAULT) / "serve.json").stat().st_mtime
    except OSError:
        return None


def _svc_skip() -> bool:
    """死服务负缓存 (修复3): TTL 内且目标 (地址 + serve.json mtime) 未变 → 本调用直接走冷
    路径, 不再每个工具调用先耗 2s 探活; mtime 变化 (服务恢复重写 serve.json) 即重新探测。"""
    if _svc_neg_until <= 0 or time.monotonic() >= _svc_neg_until:
        return False
    return _resolve_base() == _svc_neg_base and _serve_stamp() == _svc_neg_stamp


def _svc_mark_down():
    global _svc_neg_until, _svc_neg_base, _svc_neg_stamp
    _svc_neg_until = time.monotonic() + _NEG_TTL
    _svc_neg_base = _resolve_base()
    _svc_neg_stamp = _serve_stamp()


def _svc_clear_neg():
    global _svc_neg_until
    _svc_neg_until = 0.0


def _svc_get(path: str, timeout: float = 0.5):
    import urllib.request
    headers = {"User-Agent": "ContextVault-mcp/0.1"}
    tok = os.environ.get("CV_SERVE_TOKEN")  # 与 cv serve --token 配对: 鉴权档热索引联动
    if tok:
        headers["X-Vault-Token"] = tok
    req = urllib.request.Request(_resolve_base() + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    _svc_clear_neg()  # 一次成功请求即证明服务活着: 负缓存立即失效 (修复3)
    return data


def _via_service(name: str, args_dict: dict):
    """常驻服务活着时走热索引; 挂了/超时返回 None 回退文件扫描。"""
    try:
        if name == "vault_search":
            lim = max(1, min(int(args_dict.get('limit', 5)), 200))  # limit<=0 → 1, 与本地/冷扫同口径 (修复4)
            r = _svc_get(f"/search?q={urllib.parse.quote(args_dict.get('query', ''))}"
                         f"&limit={lim}")
            if not r.get("terms"):
                return "查询词太短"  # 与冷回退同口径: 参数无效不是一次真实搜索的无命中
            # 判空用服务端 total (截断前的真实命中数): limit=0 曾把真实命中报成"未命中" (二轮 mcp-6)
            if not r.get("total"):
                return f"未命中: {r['query']} (via 热索引)"
            folded = sum(int(h.get("folded", 0) or 0) for h in r.get("hits", []))
            suffix = f" (折叠 {folded} 个历史版本)" if folded else ""
            lines = [f"搜索 '{r['query']}' 命中 {r['total']} 个快照{suffix} (via 热索引):"]
            for h in r["hits"]:
                lines.append(f"  [{h['sid']}] {h['agent']} 得分={h['score']} — "
                             f"{h.get('cwd', '?')} ({str(h.get('created_at', ''))[:10]})")
            return "\n".join(lines)
        if name == "vault_context":
            lim = max(1, min(int(args_dict.get('limit', 3)), 200))  # 同修复4: 0/负数钳 1
            r = _svc_get(f"/context?q={urllib.parse.quote(args_dict.get('query', ''))}"
                         f"&limit={lim}")
            if not r.get("terms"):
                return "查询词太短"  # 与冷回退同口径
            if not r.get("total"):  # 截断前真实命中数, 同 mcp-6 口径
                return f"vault 中无相关上下文: {r['query']}"
            lines = [f"来自 context-vault 的相关历史上下文 (查询: '{r['query']}', via 热索引):"]
            for h in r["results"]:
                lines.append(f"\n--- 快照 {h['sid']} 块#{h['block']} ({h['role']}, cwd={h.get('cwd')}) "
                             f"相关度={h['score']} ---\n{h['excerpt']}")
            return "\n".join(lines)[:12000]
        if name == "vault_recent":
            lim = max(1, min(int(args_dict.get('limit', 5)), 200))  # 与 /recent 服务端钳制同口径 (修复5)
            r = _svc_get(f"/recent?limit={lim}")
            rows = r.get("rows", []) if isinstance(r, dict) else r  # /recent 契约 {total, rows[]} (2026-11-01); 兼容旧数组形态
            lines = [f"最近 {len(rows)} 个快照 (via 热索引):"]
            for h in rows:
                lines.append(f"  [{h['sid']}] {h['agent']} 块={h.get('blocks')} "
                             f"入={h.get('tokens_in')} | {str(h.get('created_at', ''))[:19]}")
            return "\n".join(lines)
        if name == "vault_stats":
            full = _svc_get("/stats")
            r = full.get("stats") or {}
            if not r:
                return None  # 空库交冷路径输出一致的"账本为空"提示
            return _format_stats_text(r, full.get("owned"), via_hot=True)
    except OSError:  # 连接拒绝/超时 = 服务失活: 记负缓存, TTL 内不再逐调用重试 (修复3)
        _svc_mark_down()
        return None
    except Exception:
        return None
    return None  # vault_snapshot 等复杂工具直接走本地, 避免双实现漂移


TOOLS = [
    {"name": "vault_search", "description": "在 context-vault 的全部历史会话快照中搜索关键词, 返回命中的快照列表",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "搜索词 (空格分多个词)"},
         "limit": {"type": "integer", "default": 5}},
         "required": ["query"]}},
    {"name": "vault_context", "description": "从历史会话中检索与当前任务相关的上下文摘录 (记忆检索, 建议在新任务/新会话开始时调用)",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "当前任务/问题关键词"},
         "limit": {"type": "integer", "default": 3}},
         "required": ["query"]}},
    {"name": "vault_recent", "description": "列出 vault 中最近的会话快照",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 5}}}},
    {"name": "vault_snapshot", "description": "查看单个快照的详情 (来源/token/块分布/活上下文开头)",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "快照 id (cv_ 开头)"}},
         "required": ["id"]}},
    {"name": "vault_remember", "description": "检索跨会话的长期记忆 (历史决策/项目结论/用户偏好/踩坑/架构红线, 由快照自动提取; 默认只返回当前有效记忆, 被推翻的旧记忆不污染上下文) — 新任务开始时先问一句'关于 X 我们之前定过什么'",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "主题关键词 (如 'RBAC 方案' / 'dataset1 调参')"},
         "limit": {"type": "integer", "default": 6},
         "include_history": {"type": "boolean", "default": False,
                             "description": "true = 追溯决策演变链 (含已 superseded 的旧记忆及其指向)"}},
         "required": ["query"]}},
    {"name": "vault_stats", "description": "token 账本汇总 (累计节省/缓存节省/快照数)",
     "inputSchema": {"type": "object", "properties": {}}},
]


def t_remember(vault: Path, query: str, limit: int, include_history: bool = False) -> str:
    hist_qs = "&history=1" if include_history else ""
    ms = None
    if not _svc_skip():  # 负缓存生效时不重复探活 (修复3)
        try:  # 常驻服务热路径
            r = _svc_get(f"/memory?q={urllib.parse.quote(query)}&limit={limit}{hist_qs}")
            ms = r["memories"]
        except OSError:  # 服务失活: 记负缓存再走本地 (修复3)
            _svc_mark_down()
        except Exception:
            pass
    if ms is None:
        ms = _local_memory(vault, query, limit, include_history)
    if not ms:
        return f"vault 中没有关于「{query}」的记忆。可先运行: python tools/cv.py memory extract"
    lines = [f"跨会话记忆 ({query}) — 来自 context-vault 历史快照的自动提取"
             + (" (含历史演变链):" if include_history else " (仅当前有效记忆):")]
    for m in ms:
        provenance = f"项目 {m.get('project') or '?'} · 快照 {m.get('snapshot', '')[:18]}"
        if m.get("block_idx") is not None:
            provenance += f" 块#{m['block_idx']}"
        if m.get("event_ts"):
            provenance += f" · {str(m['event_ts'])[:16]}"
        status = m.get("status") or "active"
        tag = "" if status == "active" else f" [已推翻 ← 见 #{m.get('superseded_by')}]"
        conf = f" · 置信 {m['confidence']:.1f}" if m.get("confidence") else ""
        lines.append(f"  [{m['kind']}]{tag} {m['content'][:200]}\n"
                     f"      ({provenance}{conf})")
    lines.append("引用时注明来源快照; 记忆为规则提取, 关键决策请回读原快照核实。")
    return "\n".join(lines)[:9000]


def _local_memory(vault: Path, query: str, limit: int, include_history: bool = False) -> list:
    sys.path.insert(0, str(Path(__file__).parent))
    import cv as _cv
    return _cv.memory_search_api(vault, query, limit, include_history=include_history)


def dispatch(name: str, args_dict: dict, vault: Path) -> str:
    # 优先走常驻服务热索引 (cv serve); 失活自动回退本地文件扫描 — 常驻只是加速, 不是依赖
    global _VAULT
    _VAULT = vault  # 供 _resolve_base 读 vault/serve.json 发现服务实际地址
    svc = None if _svc_skip() else _via_service(name, args_dict)  # 负缓存期内直接冷路径 (修复3)
    if svc is not None:
        return svc
    if name == "vault_search":
        return t_search(vault, args_dict.get("query", ""), int(args_dict.get("limit", 5)))
    if name == "vault_context":
        return t_context(vault, args_dict.get("query", ""), int(args_dict.get("limit", 3)))
    if name == "vault_recent":
        return t_recent(vault, int(args_dict.get("limit", 5)))
    if name == "vault_snapshot":
        return t_snapshot(vault, args_dict.get("id", ""))
    if name == "vault_remember":
        return t_remember(vault, args_dict.get("query", ""), int(args_dict.get("limit", 6)),
                          bool(args_dict.get("include_history", False)))
    if name == "vault_stats":
        return t_stats(vault)
    raise ValueError(f"未知工具: {name}")


# ---------------- JSON-RPC / stdio 主循环 ----------------

def handle(msg: dict, vault: Path) -> dict | None:
    if not isinstance(msg, dict):
        # 修复1: 合法 JSON 但非对象帧 ([1,2,3]/null/"str"/42) 无 id 可回显 — 忽略该帧。
        # 曾直接 msg.get(...) 抛 AttributeError 杀死整个 server, 单条坏帧毁掉整个 MCP 会话
        return None
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTO,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            text = dispatch(params.get("name", ""), params.get("arguments", {}) or {}, vault)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text[:12000]}], "isError": False}}
        except Exception as e:  # 工具错误按 MCP 规范返回 isError, 不炸 server
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"工具执行失败: {e}"}], "isError": True}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main():
    try:  # 中文 Windows 管道默认 GBK, MCP 规范要求 UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        # stdin 同样必须 UTF-8: 按 cp936 解码 UTF-8 中文 query 会产生孤立代理字符,
        # 响应写回 UnicodeEncodeError 直接杀死 server (全面测试 H3); 坏字节替换为
        # U+FFFD — 乱码 query 顶多搜不到, 不再致命
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="context-vault-mcp")
    ap.add_argument("--vault", default=str(Path.cwd() / "vault"))
    args = ap.parse_args()
    vault = vault_dir(args)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        # server 无状态: 每次调用即时重扫快照目录, cv CLI 与 MCP 永远看到同一份资产
        try:
            resp = handle(msg, vault)
        except Exception as e:  # 修复1: 帧级异常兜底 — 任何单帧异常都不终止进程
            mid = msg.get("id") if isinstance(msg, dict) else None
            if mid is None:  # 无 id 可回显 (通知/坏帧): 静默忽略
                print(f"mcp_server: 已忽略无法处理的帧: {e}", file=sys.stderr)
                continue
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"internal error: {e}"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
