#!/usr/bin/env python3
"""context-vault MCP server — 把 Vault 变成所有 MCP 客户端的记忆后端。

零第三方依赖 (Python 3.9+ stdlib only), stdio transport, 换行分隔 JSON-RPC 2.0 (MCP 规范)。

工具:
    vault_recent(limit)          最近快照列表 (id/agent/token/完整性)
    vault_search(query, limit)   全库文本搜索, 词频评分
    vault_context(query, limit)  记忆检索: 命中块 + 前后文摘录 (记忆后端核心)
    vault_snapshot(id)           单快照详情 (capture/stats/块分布/活上下文)
    vault_stats()                token 账本汇总 (含 headroom lifetime)

接入 (任一 MCP 客户端):
    claude mcp add context-vault -s user -- python /path/to/mcp_server.py --vault /path/to/vault
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

SERVER_NAME = "context-vault"
SERVER_VERSION = "0.1.0"
PROTO = "2025-06-18"
SEARCH_CONTEXT_CHARS = 400  # vault_context 每个命中块返回的摘录长度
MAX_TEXT_LEN = 2000


# ---------------- vault 读取 ----------------

def vault_dir(args) -> Path:
    return Path(args.vault).expanduser()


def load_all_snapshots(vault: Path) -> list:
    out = []
    snap_dir = vault / "snapshots"
    if not snap_dir.exists():
        return out
    for f in sorted(snap_dir.glob("cv_*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            env["_sid"] = f.stem  # 文件名即快照 id (内容寻址)
            out.append(env)
        except (ValueError, OSError):
            continue
    out.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return out


def ledger_summary(vault: Path) -> dict:
    db = vault / "vault.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = {}
    row = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events "
                       "WHERE source='headroom-lifetime'").fetchone()
    if row:
        out["lifetime_compression_saved_usd"] = row["cost_usd"]
        out["lifetime_tokens_saved"] = row["tokens_saved"]
        raw = json.loads(row["raw"])
        out["lifetime_cache_saved_usd"] = raw.get("cache_savings_usd")
        out["lifetime_total_input_tokens"] = raw.get("total_input_tokens")
    r2 = conn.execute("SELECT COUNT(*) n FROM snapshots").fetchone()
    out["snapshots"] = r2["n"]
    conn.close()
    return out


def brief(env: dict) -> str:
    c, s = env["capture"], env["stats"]
    return (f"{env.get('_sid')} | {c.get('agent')} | {env.get('created_at', '')[:19]} | "
            f"块={s['blocks_total']} 入={s['tokens_in']} cache读={s['cache_read']} | "
            f"{c.get('completeness')} | cwd={c.get('cwd', '?')}")


# ---------------- 工具实现 ----------------

def t_recent(args_vault, limit: int) -> str:
    snaps = load_all_snapshots(args_vault)
    if not snaps:
        return "vault 为空。先运行: python tools/cv.py export <会话文件>"
    lines = [f"vault 内 {len(snaps)} 个快照, 最近 {min(limit, len(snaps))} 个:"]
    for env in snaps[:limit]:
        lines.append("  " + brief(env))
    return "\n".join(lines)


def block_text(b: dict) -> str:
    if b["type"] in ("text", "agent_message"):
        return b.get("content", "") or ""
    if b["type"] == "thinking":
        return b.get("summary", "") or ""
    if b["type"] == "tool_call":
        return b.get("name", "") + " " + json.dumps(b.get("arguments", {}), ensure_ascii=False)[:200]
    if b["type"] == "tool_result":
        return (b.get("content", "") or "")[:500]
    return ""


def score_blocks(env: dict, terms: list) -> list:
    hits = []
    for i, b in enumerate(env["conversation"]):
        text = block_text(b)
        low = text.lower()
        n = sum(low.count(t) for t in terms)
        if n:
            hits.append((n, i, text))
    return hits


def t_search(args_vault, query: str, limit: int) -> str:
    terms = [t.lower() for t in query.split() if len(t) >= 2]
    if not terms:
        return "查询词太短"
    snaps = load_all_snapshots(args_vault)
    results = []
    for env in snaps:
        hits = score_blocks(env, terms)
        if hits:
            total = sum(h[0] for h in hits)
            results.append((total, env, len(hits)))
    results.sort(key=lambda r: -r[0])
    if not results:
        return f"未命中: {query}"
    lines = [f"搜索 '{query}' 命中 {len(results)} 个快照:"]
    for total, env, nhits in results[:limit]:
        c = env["capture"]
        lines.append(f"  [{env.get('_sid')}] {c.get('agent')} 得分={total} 命中块={nhits}"
                     f" — {c.get('cwd', '?')} ({env.get('created_at', '')[:10]})")
    return "\n".join(lines)


def t_context(args_vault, query: str, limit: int) -> str:
    """记忆检索: 跨快照找最相关的上下文摘录, 供当前会话引用。"""
    terms = [t.lower() for t in query.split() if len(t) >= 2]
    if not terms:
        return "查询词太短"
    snaps = load_all_snapshots(args_vault)
    candidates = []  # (score, sid, block_idx, excerpt)
    for env in snaps:
        hits = score_blocks(env, terms)
        for n, i, text in hits:
            role = env["conversation"][i].get("role") or env["conversation"][i]["type"]
            # 摘录取命中位置附近窗口
            pos = text.lower().find(terms[0])
            start = max(0, pos - SEARCH_CONTEXT_CHARS // 2)
            excerpt = text[start:start + SEARCH_CONTEXT_CHARS]
            candidates.append((n, env.get("_sid"), i, role, excerpt, env["capture"].get("cwd", "?")))
    candidates.sort(key=lambda c: -c[0])
    if not candidates:
        return f"vault 中无相关上下文: {query}"
    seen_sids, picked = set(), []
    for n, sid, i, role, excerpt, cwd in candidates:
        if len(picked) >= limit:
            break
        if sid in seen_sids:  # 每快照只取最佳命中, 避免单会话刷屏
            continue
        seen_sids.add(sid)
        picked.append((n, sid, i, role, excerpt, cwd))
    lines = [f"来自 context-vault 的相关历史上下文 (查询: '{query}'):"]
    for n, sid, i, role, excerpt, cwd in picked:
        lines.append(f"\n--- 快照 {sid} 块#{i} ({role}, cwd={cwd}) 相关度={n} ---\n{excerpt}")
    lines.append("\n(以上为历史会话摘录; 引用时注明来源快照)")
    return "\n".join(lines)[:12000]


def t_snapshot(args_vault, sid: str) -> str:
    vault = args_vault
    p = vault / "snapshots" / f"{sid}.json"
    if not p.exists():
        return f"快照不存在: {sid}"
    env = json.loads(p.read_text(encoding="utf-8"))
    from collections import Counter
    dist = Counter(b["type"] for b in env["conversation"])
    c, s = env["capture"], env["stats"]
    lines = [f"快照 {sid}",
             f"  来源: {c.get('agent')} ({c.get('source_format')}), session_ref={c.get('session_ref')}",
             f"  完整性: {c.get('completeness')} | cwd: {c.get('cwd')} | 创建: {env.get('created_at')}",
             f"  块分布: {dict(dist)}",
             f"  token: 入={s['tokens_in']:,} 出={s['tokens_out']:,} cache读={s['cache_read']:,} (events={s['usage_events']})"]
    # 活上下文开头
    last = -1
    for i, b in enumerate(env["conversation"]):
        if b["type"] == "compaction":
            last = i
    live = env["conversation"][last + 1:]
    head = []
    for b in live[:8]:
        t = block_text(b)[:200]
        if t:
            head.append(f"    [{b.get('role') or b['type']}] {t}")
    if head:
        lines.append(f"  活上下文前 {len(live)} 块 (压缩边界后), 开头:")
        lines += head
    return "\n".join(lines)[:8000]


def t_stats(args_vault) -> str:
    s = ledger_summary(args_vault)
    if not s:
        return "账本为空。先运行: python tools/cv.py ledger import-headroom"
    usd = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else "-"
    return ("token 账本 (context-vault):\n"
            f"  快照数: {s.get('snapshots', 0)}\n"
            f"  压缩节省: {s.get('lifetime_tokens_saved', 0):,} tokens = {usd(s.get('lifetime_compression_saved_usd'))}\n"
            f"  缓存节省: {usd(s.get('lifetime_cache_saved_usd'))}\n"
            f"  历史总输入: {s.get('lifetime_total_input_tokens', 0):,} tokens")


# ---------------- MCP 工具表 ----------------

# ---------------- 常驻服务联动 (cv serve) ----------------

SVC_BASE = os.environ.get("CV_VAULT_SVC", "http://127.0.0.1:8791")


def _svc_get(path: str, timeout: float = 2.0):
    import urllib.request
    headers = {"User-Agent": "ContextVault-mcp/0.1"}
    tok = os.environ.get("CV_SERVE_TOKEN")  # 与 cv serve --token 配对: 鉴权档热索引联动
    if tok:
        headers["X-Vault-Token"] = tok
    req = urllib.request.Request(SVC_BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _via_service(name: str, args_dict: dict):
    """常驻服务活着时走热索引; 挂了/超时返回 None 回退文件扫描。"""
    try:
        if name == "vault_search":
            r = _svc_get(f"/search?q={urllib.parse.quote(args_dict.get('query', ''))}"
                         f"&limit={int(args_dict.get('limit', 5))}")
            if not r["hits"]:
                return f"未命中: {r['query']} (via 热索引)"
            lines = [f"搜索 '{r['query']}' 命中 {len(r['hits'])} 个快照 (via 热索引):"]
            for h in r["hits"]:
                lines.append(f"  [{h['sid']}] {h['agent']} 得分={h['score']} — "
                             f"{h.get('cwd', '?')} ({str(h.get('created_at', ''))[:10]})")
            return "\n".join(lines)
        if name == "vault_context":
            r = _svc_get(f"/context?q={urllib.parse.quote(args_dict.get('query', ''))}"
                         f"&limit={int(args_dict.get('limit', 3))}")
            if not r["results"]:
                return f"vault 中无相关上下文: {r['query']}"
            lines = [f"来自 context-vault 的相关历史上下文 (查询: '{r['query']}', via 热索引):"]
            for h in r["results"]:
                lines.append(f"\n--- 快照 {h['sid']} 块#{h['block']} ({h['role']}, cwd={h.get('cwd')}) "
                             f"相关度={h['score']} ---\n{h['excerpt']}")
            return "\n".join(lines)[:12000]
        if name == "vault_recent":
            r = _svc_get(f"/recent?limit={int(args_dict.get('limit', 5))}")
            lines = [f"最近 {len(r)} 个快照 (via 热索引):"]
            for h in r:
                lines.append(f"  [{h['sid']}] {h['agent']} 块={h.get('blocks')} "
                             f"入={h.get('tokens_in')} | {str(h.get('created_at', ''))[:19]}")
            return "\n".join(lines)
        if name == "vault_stats":
            r = _svc_get("/stats")["stats"]
            usd = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else "-"
            return ("token 账本 (context-vault, via 热索引):\n"
                    f"  快照数: {r.get('snapshots', 0)}\n"
                    f"  压缩节省: {r.get('lifetime_tokens_saved', 0):,} tokens = "
                    f"{usd(r.get('lifetime_compression_saved_usd'))}\n"
                    f"  缓存节省: {usd(r.get('lifetime_cache_saved_usd'))}")
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
    {"name": "vault_remember", "description": "检索跨会话的长期记忆 (历史决策/项目结论/用户偏好, 由快照自动提取) — 新任务开始时先问一句'关于 X 我们之前定过什么'",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "主题关键词 (如 'RBAC 方案' / 'dataset1 调参')"},
         "limit": {"type": "integer", "default": 6}}, "required": ["query"]}},
    {"name": "vault_stats", "description": "token 账本汇总 (累计节省/缓存节省/快照数)",
     "inputSchema": {"type": "object", "properties": {}}},
]


def t_remember(vault: Path, query: str, limit: int) -> str:
    try:  # 常驻服务热路径
        r = _svc_get(f"/memory?q={urllib.parse.quote(query)}&limit={limit}")
        ms = r["memories"]
    except Exception:
        ms = _local_memory(vault, query, limit)
    if not ms:
        return f"vault 中没有关于「{query}」的记忆。可先运行: python tools/cv.py memory extract"
    lines = [f"跨会话记忆 ({query}) — 来自 context-vault 历史快照的自动提取:"]
    for m in ms:
        lines.append(f"  [{m['kind']}] {m['content'][:200]}\n"
                     f"      (项目 {m.get('project') or '?'} · 快照 {m.get('snapshot', '')[:18]})")
    lines.append("引用时注明来源快照; 记忆为规则提取, 关键决策请回读原快照核实。")
    return "\n".join(lines)[:9000]


def _local_memory(vault: Path, query: str, limit: int) -> list:
    sys.path.insert(0, str(Path(__file__).parent))
    import cv as _cv
    return _cv.memory_search_api(vault, query, limit)


def dispatch(name: str, args_dict: dict, vault: Path) -> str:
    # 优先走常驻服务热索引 (cv serve); 失活自动回退本地文件扫描 — 常驻只是加速, 不是依赖
    svc = _via_service(name, args_dict)
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
        return t_remember(vault, args_dict.get("query", ""), int(args_dict.get("limit", 6)))
    if name == "vault_stats":
        return t_stats(vault)
    raise ValueError(f"未知工具: {name}")


# ---------------- JSON-RPC / stdio 主循环 ----------------

def handle(msg: dict, vault: Path) -> dict | None:
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
        resp = handle(msg, vault)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
