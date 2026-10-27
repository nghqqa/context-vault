#!/usr/bin/env python3
"""cv — Context Vault MVP CLI.

本地优先的上下文资产库: 会话快照 (供应商中立格式) + token 账本 + 上游可用性探测。

用法 (全部零第三方依赖, Python 3.9+):
    python tools/cv.py init                          初始化 vault (SQLite + snapshots/)
    python tools/cv.py export <session.jsonl>        导出 Codex/Claude 会话 → 快照 (只读源)
    python tools/cv.py list                          列出 vault 内快照
    python tools/cv.py show <snapshot_id>            打印快照全文
    python tools/cv.py diff <snapshot_id>            迁移前预览: 可迁移/不可迁移清单
    python tools/cv.py import <snapshot_id> --target codex|claude [--apply]
                                                    快照 → 目标 agent 会话文件
                                                    (默认 dry-run 写 vault/outbox/, --apply 才写真实会话目录)
    python tools/cv.py migrate <src> --target ...    一键: 导出→预览→导入
    python tools/cv.py probe [--profile X] [--watch N]
                                                    上游 /v1/models 可用性探测, 模型消失即告警留痕
    python tools/cv.py ledger import-headroom       导入 headroom 账本 (savings_events + proxy_savings)
    python tools/cv.py ledger stats                 账本汇总
    python tools/cv.py doctor                       环境体检

快照格式规范: docs/snapshot-format.md (context-snapshot v0.1)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

FORMAT_VERSION = "0.1"
TOOL_RESULT_LIMIT = 256 * 1024  # spec §8.2 裁决: 单块截断阈值 256KB
HOME = Path.home()
HEADROOM_DIR = HOME / ".headroom"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

# ---------------------------------------------------------------- vault 基础

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id TEXT PRIMARY KEY, created_at TEXT, agent TEXT, source_format TEXT,
  provider TEXT, model TEXT, session_ref TEXT, originator TEXT,
  completeness TEXT, tokens_in INTEGER, tokens_out INTEGER, reasoning_tokens INTEGER,
  cache_read INTEGER, cache_write INTEGER, total_tokens INTEGER, cost_usd REAL,
  usage_events INTEGER, blocks_total INTEGER, blocks_dropped INTEGER, path TEXT);
CREATE TABLE IF NOT EXISTS ledger_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, occurred_at TEXT, kind TEXT,
  model TEXT, provider TEXT, client TEXT, tokens_saved INTEGER, cost_usd REAL,
  snapshot_id TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS probe_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, profile TEXT, endpoint TEXT,
  status TEXT, http_code INTEGER, latency_ms INTEGER, models TEXT, missing_models TEXT);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_events(occurred_at);
"""


def vault_dir(args) -> Path:
    return Path(args.vault).expanduser()


def db(vault: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(vault / "vault.db")
    conn.row_factory = sqlite3.Row
    return conn


def cmd_init(args):
    vault = vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    conn.executescript(SCHEMA)
    conn.commit()
    print(f"vault 就绪: {vault.resolve()}")


# ------------------------------------------------- 快照: 规范化与内容寻址

def canonical_bytes(envelope: dict) -> bytes:
    h = {k: v for k, v in envelope.items() if k not in ("created_at", "lineage")}
    return json.dumps(h, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def snapshot_id(envelope: dict) -> str:
    return "cv_" + hashlib.sha256(canonical_bytes(envelope)).hexdigest()[:16]


def iter_jsonl(path: Path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def first_jsonl(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                return json.loads(line)
    raise SystemExit(f"空文件: {path}")


def clip(text: str) -> tuple:
    if len(text) <= TOOL_RESULT_LIMIT:
        return text, False
    keep = TOOL_RESULT_LIMIT // 2
    return text[:keep] + "\n…[cv: truncated]…\n" + text[-keep:], True


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def zero_stats() -> dict:
    return {"tokens_in": 0, "tokens_out": 0, "reasoning_tokens": 0, "cache_read": 0,
            "cache_write": 0, "total_tokens": 0, "cost_usd": None, "usage_events": 0,
            "blocks_total": 0, "blocks_dropped": 0}


# ------------------------------------------------- 导出: 源格式适配器

def detect_format(first: dict, path: Path) -> str:
    t = first.get("type")
    if t in ("session_meta", "event_msg", "response_item", "turn_context"):
        return "codex-rollout"
    if t in ("user", "tool_use", "tool_result"):
        return "claude-transcript"
    raise SystemExit(f"无法识别源格式: {path} (首行 type={t!r})")


def export_codex(path: Path, drop_developer: bool) -> dict:
    blocks, stats, dropped = [], zero_stats(), 0
    capture = {"agent": "codex", "source_format": "codex-rollout", "provider": None,
               "model": None, "completeness": "full"}
    turn_usage, pending_conv_idx = {}, 0
    for obj in iter_jsonl(path):
        t, ts, payload = obj.get("type"), obj.get("timestamp", ""), obj.get("payload", {})
        if t == "session_meta":
            capture["session_ref"] = payload.get("session_id")
            capture["agent_version"] = payload.get("cli_version")
            capture["cwd"] = payload.get("cwd")
            capture["originator"] = payload.get("originator")
        elif t == "token_usage_record":
            turn_usage[payload.get("turn_id")] = payload.get("turn_token_usage") or payload.get("usage") or {}
        elif t == "compacted":
            blocks.append({"type": "compaction", "summary": (payload.get("message") or "")[:2000],
                           "replaced_blocks": [0, max(pending_conv_idx - 1, 0)], "ts": ts})
            pending_conv_idx = len(blocks)
        elif t == "response_item":
            pt = payload.get("type")
            if pt == "message":
                role = payload.get("role")
                # spec §3.1 裁决: developer 注入默认保留 (vault=完整审计资产);
                # 迁移时由 import 端过滤 (可移植性), 两个关注点分离
                if role == "developer" and drop_developer:
                    dropped += 1
                    continue
                text = "\n".join(c.get("text", "") for c in payload.get("content", []))
                blocks.append({"type": "text", "role": role, "content": text, "ts": ts})
            elif pt == "reasoning":
                summary = "\n\n".join(s.get("text", "") for s in payload.get("summary", []))
                if summary:
                    blocks.append({"type": "thinking", "summary": summary, "ts": ts})
            elif pt in ("function_call", "custom_tool_call"):
                args = payload.get("arguments", payload.get("input"))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        pass
                blocks.append({"type": "tool_call", "call_id": payload.get("call_id"),
                               "name": payload.get("name"), "namespace": payload.get("namespace"),
                               "arguments": args, "ts": ts})
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = payload.get("output")
                if isinstance(out, list):
                    out = "\n---\n".join(c.get("text", "") for c in out if isinstance(c, dict))
                content, _ = clip(str(out or ""))
                blocks.append({"type": "tool_result", "call_id": payload.get("call_id"),
                               "content": content, "is_error": False, "ts": ts})
            elif pt == "agent_message":
                text = "\n".join(c.get("text", "") for c in payload.get("content", []))
                blocks.append({"type": "agent_message", "author": payload.get("author"),
                               "recipient": payload.get("recipient"), "content": text, "ts": ts})
            else:
                dropped += 1
    for tu in turn_usage.values():
        for k, src in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
                       ("reasoning_tokens", "reasoning_output_tokens"),
                       ("cache_read", "cached_input_tokens"),
                       ("cache_write", "cache_write_input_tokens"), ("total_tokens", "total_tokens")):
            stats[k] += int(tu.get(src, 0) or 0)
        stats["usage_events"] += 1
    stats["blocks_total"], stats["blocks_dropped"] = len(blocks), dropped
    return {"capture": capture, "conversation": blocks, "stats": stats}


def export_claude(path: Path, drop_developer: bool) -> dict:
    blocks, stats = [], zero_stats()
    capture = {"agent": "claude-code", "source_format": "claude-transcript", "provider": None,
               "model": None, "session_ref": path.stem, "completeness": "no_assistant_text"}
    seq = 0
    pending: dict = {}  # tool_name -> [call_id, ...] 按序 FIFO 匹配 (同名调用顺序完成)
    unmatched_results = 0
    for obj in iter_jsonl(path):
        t, ts = obj.get("type"), obj.get("timestamp", "")
        if t == "user":
            blocks.append({"type": "text", "role": "user", "content": str(obj.get("content", "")), "ts": ts})
        elif t == "tool_use":
            seq += 1
            call_id = f"cv_synth_{seq}"
            pending.setdefault(obj.get("tool_name"), []).append(call_id)
            blocks.append({"type": "tool_call", "call_id": call_id, "name": obj.get("tool_name"),
                           "namespace": None, "arguments": obj.get("tool_input"), "ts": ts})
        elif t == "tool_result":
            queue = pending.get(obj.get("tool_name"))
            call_id = queue.pop(0) if queue else None
            if call_id is None:
                unmatched_results += 1
                continue
            out = obj.get("tool_output")
            content = out.get("output", "") if isinstance(out, dict) else str(out or "")
            content, _ = clip(str(content))
            blocks.append({"type": "tool_result", "call_id": call_id,
                           "content": content, "is_error": False, "ts": ts})
    if unmatched_results:
        capture["notes"] = f"{unmatched_results} 条无对应调用的 tool_result 被丢弃"
    stats["blocks_total"] = len(blocks)
    stats["blocks_dropped"] = unmatched_results
    return {"capture": capture, "conversation": blocks, "stats": stats}


def build_envelope(exported: dict) -> dict:
    return {"format_version": FORMAT_VERSION, "created_at": now_iso(),
            "capture": exported["capture"], "conversation": exported["conversation"],
            "memory_hints": None, "stats": exported["stats"],
            "lineage": {"parent_snapshot": None, "fork_of": None}}


SNAP_COLS = ("id,created_at,agent,source_format,provider,model,session_ref,originator,"
             "completeness,tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,"
             "total_tokens,cost_usd,usage_events,blocks_total,blocks_dropped,path")


def register_snapshot(conn, envelope: dict, sid: str, path: Path):
    c, s = envelope["capture"], envelope["stats"]
    conn.execute(
        f"INSERT OR REPLACE INTO snapshots({SNAP_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, envelope["created_at"], c.get("agent"), c.get("source_format"), c.get("provider"),
         c.get("model"), c.get("session_ref"), c.get("originator"), c.get("completeness"),
         s["tokens_in"], s["tokens_out"], s["reasoning_tokens"], s["cache_read"], s["cache_write"],
         s["total_tokens"], s["cost_usd"], s["usage_events"], s["blocks_total"], s["blocks_dropped"],
         str(path)))


def cmd_export(args):
    src = Path(args.source).expanduser()
    fmt = detect_format(first_jsonl(src), src)
    exported = (export_codex if fmt == "codex-rollout" else export_claude)(src, args.drop_developer)
    envelope = build_envelope(exported)
    sid = snapshot_id(envelope)
    vault = vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        print(f"已存在 (幂等跳过): {out.name}")
    else:
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        s = exported["stats"]
        print(f"已导出 {out.name}  格式={fmt}  块={s['blocks_total']}  入token={s['tokens_in']}  cache读={s['cache_read']}")
    conn = db(vault)
    conn.executescript(SCHEMA)
    register_snapshot(conn, envelope, sid, out)
    conn.commit()
    return sid


def load_snapshot(args_or_vault, sid: str) -> dict:
    vault = args_or_vault if isinstance(args_or_vault, Path) else vault_dir(args_or_vault)
    p = vault / "snapshots" / f"{sid}.json"
    if not p.exists():
        raise SystemExit(f"快照不存在: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------- list / show

def cmd_list(args):
    conn = db(vault_dir(args))
    rows = conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC").fetchall()
    if not rows:
        print("(vault 为空, 先运行: cv export <会话文件>)")
        return
    for r in rows:
        print(f"{r['id']}  {r['agent']:<12} 块={r['blocks_total']:<5} "
              f"入={r['tokens_in']:<9} 出={r['tokens_out']:<6} cache读={r['cache_read']:<9} "
              f"{r['completeness'] or ''}  {r['created_at'][:19]}")


def cmd_show(args):
    print(json.dumps(load_snapshot(args, args.snapshot_id), ensure_ascii=False, indent=1))


# ------------------------------------------------- diff: 迁移前预览

def live_context(blocks: list) -> tuple:
    """stateless replay 语义: 最后一个 compaction 之后为活上下文, 摘要可注入。"""
    last = -1
    for i, b in enumerate(blocks):
        if b["type"] == "compaction":
            last = i
    return (blocks[last + 1:], blocks[last]["summary"] if last >= 0 else None)


def estimate_tokens(blocks: list) -> int:
    n = 0
    for b in blocks:
        n += len(b.get("content", "") or "") + len(b.get("summary", "") or "")
        if b["type"] == "tool_call":
            n += len(json.dumps(b.get("arguments", ""), ensure_ascii=False))
    return n // 4  # 粗估: 4 字符 ≈ 1 token


def cmd_diff(args):
    env = load_snapshot(args, args.snapshot_id)
    c, blocks, s = env["capture"], env["conversation"], env["stats"]
    from collections import Counter
    dist = Counter(b["type"] for b in blocks)
    live, comp_summary = live_context(blocks)
    live_dist = Counter(b["type"] for b in live)
    print(f"快照 {args.snapshot_id}  ({c.get('agent')} ← {c.get('source_format')}, "
          f"completeness={c.get('completeness')})")
    print(f"  历史块分布: {dict(dist)}")
    if comp_summary is not None:
        print(f"  压缩边界: {dist.get('compaction', 0)} 次, 活上下文 = 最后一次压缩后的 "
              f"{len(live)} 块 {dict(live_dist)}")
        print(f"  迁移语义: stateless replay — 摘要注入为首条 user 消息, 压缩前历史仅供审计")
    print(f"  活上下文估算 token: ~{estimate_tokens(live):,}")
    print(f"  历史累计: 入={s['tokens_in']:,} 出={s['tokens_out']:,} cache读={s['cache_read']:,} "
          f"(usage_events={s['usage_events']})")
    imm = []
    if c.get("completeness") == "no_assistant_text":
        imm.append("assistant 文本缺失 (源转录不含) — 目标会话将只有 user/tool 视角")
    if dist.get("thinking"):
        imm.append(f"thinking×{dist['thinking']} — 仅明文摘要可迁移 (加密推理已丢弃)")
    if dist.get("agent_message"):
        imm.append(f"agent_message×{dist['agent_message']} — 降级为带标注的 assistant 文本")
    if dist.get("image"):
        imm.append(f"image×{dist['image']} — v0.1 跳过")
    if s["blocks_dropped"]:
        imm.append(f"源文件被丢弃行: {s['blocks_dropped']} (developer 注入/无移植语义行)")
    print("  不可迁移项 (诚实清单):")
    for x in imm or ["(无)"]:
        print(f"    - {x}")


# ------------------------------------------------- import: 快照 → 目标 agent

def blocks_to_codex(env: dict, strip_thinking: bool) -> tuple:
    """生成 Codex rollout 行 (结构对齐本机实测 codex-cli 0.153.4, 见 snapshot-format.md 附录 A)。
    返回 (lines, dropped, session_id, filename)。两个硬约束 (实测逆向):
    ① 文件名必须含 session UUID — resume 按文件名 rollout-*-<uuid>.jsonl 定位;
    ② 生命周期事件链 (task_started/…/task_complete) 缺失时 thread-store 判 rollout 为空。
    兼容性枚举字段 (source/originator/cli_version) 用合法值; 迁移来源在首条 user 消息声明。"""
    sid = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    cwd = env["capture"].get("cwd") or str(Path.cwd())
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    dropped = 0
    fname_ts = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    # ordinal 必须是数字: codex-rs RolloutLine.ordinal 是 Option<u64>, JSON 字符串会被
    # serde 拒绝 → 整行解析失败被静默跳过 → thread-store 报 "rollout is empty"
    # (实测 0.153.4 CLI 写数字 ordinal; 0.158 桌面端写字符串——两代互不兼容)
    lines = [{"timestamp": now, "ordinal": 0, "type": "session_meta",
              "payload": {
                  "session_id": sid, "id": sid, "timestamp": now, "cwd": cwd,
                  "originator": "codex_exec", "cli_version": "0.153.4",
                  "source": "exec", "thread_source": "user",
                  "model_provider": "custom",
                  # 以下三项为 0.153.4 thread-store 反序列化所需的结构占位
                  "base_instructions": {"text": "You are Codex, an agent based on GPT-6. [context-vault migrated session]",
                                        "provenance": {"type": "model", "model": env["capture"].get("model") or "gpt-6.1-sol"}},
                  "history_mode": "paginated",
                  "context_window": {"window_id": str(uuid.uuid4())}}}]
    ord_ = 1
    live, comp_summary = live_context(env["conversation"])
    last_assistant = ""

    def emit(obj):
        nonlocal ord_
        obj.update({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "ordinal": ord_})
        ord_ += 1
        lines.append(obj)

    started_at = int(time.time())

    def item_done(item):
        ms = int(time.time() * 1000)
        emit({"type": "event_msg", "payload": {"type": "item_completed", "thread_id": sid,
              "turn_id": turn_id, "item": item, "started_at_ms": ms, "completed_at_ms": ms}})

    emit({"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id,
          "started_at": started_at, "model_context_window": 258400,
          "collaboration_mode_kind": "default"}})
    # turn_context 是 thread 重建的 turn 边界锚点 (缺失 → list_turns 空 → reconcile 出空 thread)
    emit({"type": "turn_context", "payload": {"turn_id": turn_id, "root_turn_id": turn_id,
          "cwd": cwd, "workspace_roots": [cwd], "current_date": dt.date.today().isoformat(),
          "approval_policy": "never", "sandbox_policy": {"type": "danger-full-access"}}})
    if comp_summary:
        summary_text = f"[context-vault 迁移摘要] 此前会话经过压缩, 摘要如下:\n\n{comp_summary}"
        emit({"type": "response_item", "payload": {"type": "message", "id": f"msg_{uuid.uuid4()}",
              "role": "user", "content": [{"type": "input_text", "text": summary_text}]}})
        item_done({"type": "UserMessage", "id": str(uuid.uuid4()),
                   "content": [{"type": "text", "text": summary_text, "text_elements": []}]})
    for b in live:
        bt = b["type"]
        if bt == "text":
            if b["role"] == "developer":
                dropped += 1  # spec §3.1 裁决: developer 注入不迁移 (源 agent 环境提示, 对目标无意义)
                continue
            ct = "input_text" if b["role"] in ("user", "developer") else "output_text"
            if b["role"] == "assistant":
                last_assistant = b["content"]
            emit({"type": "response_item", "payload": {"type": "message", "id": f"msg_{uuid.uuid4()}",
                  "role": b["role"], "content": [{"type": ct, "text": b["content"]}]}})
            if b["role"] == "user":
                item_done({"type": "UserMessage", "id": str(uuid.uuid4()),
                           "content": [{"type": "text", "text": b["content"], "text_elements": []}]})
            elif b["role"] == "assistant":
                item_done({"type": "AgentMessage", "id": f"msg_{uuid.uuid4().hex[:24]}",
                           "content": [{"type": "Text", "text": b["content"]}], "phase": "final_answer"})
        elif bt == "thinking" and not strip_thinking:
            rid = f"rs_{uuid.uuid4().hex[:24]}"
            emit({"type": "response_item", "payload": {"type": "reasoning", "id": rid,
                  "summary": [{"type": "summary_text", "text": b["summary"]}]}})
            item_done({"type": "Reasoning", "id": rid,
                       "summary_text": [b["summary"]], "raw_content": []})
        elif bt == "tool_call":
            name = b["name"]
            if b.get("namespace"):
                name = f"{b['namespace']}.{name}"
            args = b.get("arguments", {})
            emit({"type": "response_item", "payload": {"type": "function_call", "id": f"fc_{uuid.uuid4().hex[:24]}",
                  "name": name, "arguments": json.dumps(args, ensure_ascii=False), "call_id": b["call_id"]}})
        elif bt == "tool_result":
            if not b.get("call_id"):
                dropped += 1
                continue
            emit({"type": "response_item", "payload": {"type": "function_call_output",
                  "id": f"fco_{uuid.uuid4()}", "call_id": b["call_id"], "output": b["content"]}})
        elif bt == "agent_message":
            emit({"type": "response_item", "payload": {"type": "message", "id": f"msg_{uuid.uuid4()}",
                  "role": "assistant", "content": [{"type": "output_text",
                  "text": f"[{b.get('author')} → {b.get('recipient')}] {b['content']}"}]}})
    s = env["stats"]
    if s.get("usage_events"):
        usage = {"input_tokens": s["tokens_in"], "cached_input_tokens": s["cache_read"],
                 "cache_write_input_tokens": s["cache_write"],
                 "output_tokens": s["tokens_out"],
                 "reasoning_output_tokens": s["reasoning_tokens"],
                 "total_tokens": s["total_tokens"]}
        emit({"type": "token_usage_record", "payload": {"thread_id": sid, "turn_id": turn_id,
              "session_id": sid, "usage": usage, "turn_token_usage": usage}})
        emit({"type": "event_msg", "payload": {"type": "token_count", "info": {
              "total_token_usage": usage, "last_token_usage": usage,
              "model_context_window": 258400}}})
    done_at = int(time.time())
    emit({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id,
          "last_agent_message": last_assistant[:2000], "started_at": started_at,
          "completed_at": done_at, "duration_ms": (done_at - started_at) * 1000}})
    return lines, dropped, sid, f"rollout-{fname_ts}-{sid}.jsonl"


def utc_z() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def blocks_to_claude(env: dict, strip_thinking: bool) -> tuple:
    """生成 Claude Code 2.x 会话文件 (~/.claude/projects/<encoded-cwd>/<uuid>.jsonl)。
    行结构按本机实测 claude 2.1.181 转录对齐 (2026-10-27 逆向验证)。
    返回 (lines, dropped)。"""
    session = str(uuid.uuid4())
    cwd = env["capture"].get("cwd") or str(Path.cwd())
    model = env["capture"].get("model") or "unknown"
    live, comp_summary = live_context(env["conversation"])
    lines, parent, dropped = [], None, 0

    def emit(typ, message):
        nonlocal parent
        u = str(uuid.uuid4())
        row = {"parentUuid": parent, "isSidechain": False, "type": typ, "message": message,
               "uuid": u, "timestamp": utc_z(), "userType": "external",
               "entrypoint": "cv-migrate", "cwd": cwd, "sessionId": session, "version": "2.1.181"}
        lines.append(row)
        parent = u

    if comp_summary:
        emit("user", {"role": "user", "content":
              f"[context-vault 迁移摘要] 此前会话经过压缩, 摘要如下:\n\n{comp_summary}"})
    for b in live:
        bt = b["type"]
        if bt == "text":
            if b["role"] == "developer":
                dropped += 1  # spec §3.1 裁决: developer 注入不迁移
                continue
            if b["role"] == "user":
                emit("user", {"role": "user", "content": b["content"]})
            else:
                mid = "msg_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:12]
                emit("assistant", {"id": mid, "type": "message", "role": "assistant", "model": model,
                     "content": [{"type": "text", "text": b["content"]}], "stop_reason": "end_turn"})
        elif bt == "thinking" and not strip_thinking:
            # thinking 摘要并入 assistant 文本 (避免空 signature 被 API 校验拒绝)
            mid = "msg_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:12]
            emit("assistant", {"id": mid, "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "text", "text": f"[reasoning summary] {b['summary']}"}],
                 "stop_reason": "end_turn"})
        elif bt == "tool_call":
            mid = "msg_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:12]
            emit("assistant", {"id": mid, "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "tool_use", "id": b["call_id"], "name": b["name"],
                              "input": b.get("arguments", {})}], "stop_reason": "tool_use"})
        elif bt == "tool_result":
            if not b.get("call_id"):
                dropped += 1
                continue
            emit("user", {"role": "user", "content": [{"type": "tool_result",
                 "tool_use_id": b["call_id"], "content": b["content"]}]})
        elif bt == "agent_message":
            mid = "msg_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:12]
            emit("assistant", {"id": mid, "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "text",
                              "text": f"[{b.get('author')} → {b.get('recipient')}] {b['content']}"}],
                 "stop_reason": "end_turn"})
    return lines, dropped


def claude_project_dir(cwd: str) -> Path:
    # 实测 claude 2.1.181: C:\Users\x → C--Users-x (':' 与 '\' 均替换为 '-')
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9-]", "-", cwd.replace("\\", "/"))


def cmd_import(args):
    env = load_snapshot(args, args.snapshot_id)
    vault = vault_dir(args)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    if args.target == "codex":
        lines, dropped, sid, name = blocks_to_codex(env, args.strip_thinking)
        dest = vault / "outbox" / name
        if args.apply:
            day = dt.datetime.now()
            dest = CODEX_SESSIONS / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"(session_id: {sid})")
    else:
        lines, dropped = blocks_to_claude(env, args.strip_thinking)
        name = f"{uuid.uuid4()}.jsonl"
        cwd = env["capture"].get("cwd") or str(Path.cwd())
        dest = vault / "outbox" / f"claude-{args.snapshot_id}-{name}"
        if args.apply:
            dest = claude_project_dir(cwd) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    where = "已写入真实会话目录" if args.apply else "dry-run (vault/outbox/, 加 --apply 才写真实会话目录)"
    print(f"导入 {len(lines)} 行 → {dest}" + (f"  (丢弃无法配对的 tool_result×{dropped})" if dropped else ""))
    print(f"  {where}")
    if args.target == "codex" and args.apply:
        print("  验证: codex resume --last 或 codex resume <session-id>")
    if args.target == "claude" and args.apply:
        print("  验证: claude --resume (会话列表应出现迁移会话)")


def cmd_migrate(args):
    sid = cmd_export(args)
    class A:
        pass
    a = A(); a.vault = args.vault; a.snapshot_id = sid
    cmd_diff(a)
    print()
    if not args.yes:
        ans = input("继续导入? [y/N] ").strip().lower()
        if ans != "y":
            print("已中止 (快照已留存于 vault)")
            return
    a.target, a.apply, a.strip_thinking = args.target, args.apply, args.strip_thinking
    cmd_import(a)


# ------------------------------------------------- probe: 上游可用性探测

def load_profiles() -> list:
    p = HEADROOM_DIR / "switcher-profiles.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    env = os.environ.get("CV_RELAY_PROFILES")
    if env:
        return json.loads(env)
    print("提示: 未找到 relay profiles (~/.headroom/switcher-profiles.json);"
          "可用 CV_RELAY_PROFILES='[{\"id\":\"myrelay\",\"openai_api_url\":\"https://host\",\"default_model\":\"m\"}]' 配置",
          file=sys.stderr)
    return []


def models_url(base: str) -> str:
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/models"


def fetch_models(url: str, key: str | None, timeout: int = 10) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": "ContextVault/0.1"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ids = sorted(m.get("id", "") for m in body.get("data", []))
            return ("ok", resp.status, ids, int((time.time() - t0) * 1000))
    except urllib.error.HTTPError as e:
        return ("http_error", e.code, [], int((time.time() - t0) * 1000))
    except Exception as e:
        return ("error", None, [type(e).__name__ + ": " + str(e)[:80]], int((time.time() - t0) * 1000))


def notify(title: str, msg: str):
    """零依赖告警: 控制台高亮 + BEL; Windows toast 交给 Switcher exe (M2 托盘)。"""
    print(f"\a⚠ {title}: {msg}", file=sys.stderr)


def resolve_key(args) -> str | None:
    """key 解析顺序: CV_RELAY_KEY 环境变量 → --key 参数 → headroom auth-token 脚本。"""
    if os.environ.get("CV_RELAY_KEY"):
        return os.environ["CV_RELAY_KEY"]
    if args.key:
        return args.key
    script = HOME / ".local" / "bin" / "headroom-auth-token.ps1"
    if script.exists():
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=8)
            token = (r.stdout or "").strip().splitlines()
            return token[-1].strip() if token else None
        except Exception:
            return None
    return None


def probe_once(vault: Path, profiles: list, key, baseline: dict, verbose=True) -> list:
    """单轮全 profile 探测; 更新 baseline(dict, 调用方负责持久化)并落库, 返回告警结果。"""
    conn = db(vault)
    conn.executescript(SCHEMA)
    alerts = []
    for p in profiles:
        pid, url = p["id"], models_url(p["openai_api_url"])
        status, code, ids, ms = fetch_models(url, key)
        watched, missing = p.get("default_model"), []
        if status == "ok":
            if watched and watched not in ids:
                missing.append(watched)
            # 仅告警基线中消失的模型; 新增模型不打扰
            if pid in baseline and baseline[pid]:
                missing += [m for m in baseline[pid] if m not in ids]
            missing = list(dict.fromkeys(missing))
            baseline[pid] = ids
            if verbose:
                flag = "✗" if missing else "✓"
                print(f"{flag} [{pid}] ok http={code} {ms}ms  models={len(ids)}"
                      + (f"  消失: {missing}" if missing else ""))
            if missing:
                alerts.append({"profile": pid, "kind": "model_missing", "detail": missing})
                notify("上游模型变动", f"[{pid}] 消失: {', '.join(missing)}")
        else:
            # 端点异常(网络/鉴权) ≠ 模型下架, 不进入模型消失判定, 防止狼来了效应
            detail = ids[0] if ids else ""
            if verbose:
                print(f"✗ [{pid}] {status} http={code} {ms}ms  端点异常: {detail}")
            alerts.append({"profile": pid, "kind": "endpoint", "detail": detail})
            notify("上游端点异常", f"[{pid}] {status} http={code} — 无法判定模型状态")
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (now_iso(), pid, url, status, code, ms, json.dumps(ids), json.dumps(missing)))
    conn.commit()
    return alerts


def cmd_probe(args):
    vault = vault_dir(args)
    baseline_p = vault / "probe_baseline.json"
    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
    key = resolve_key(args)
    profiles = load_profiles()
    if args.profile:
        profiles = [p for p in profiles if p["id"] == args.profile] or profiles
    while True:
        probe_once(vault, profiles, key, baseline)
        baseline_p.write_text(json.dumps(baseline, ensure_ascii=False, indent=1), encoding="utf-8")
        if not args.watch:
            break
        time.sleep(args.watch)


# ------------------------------------------------- ledger: 账本

def cmd_ledger_import_headroom(args):
    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA)
    n1 = n2 = 0
    # 1) 明细事件: savings_events.jsonl (逐条压缩节省)
    se = HEADROOM_DIR / "savings_events.jsonl"
    if se.exists():
        for obj in iter_jsonl(se):
            if obj.get("v") != 1:
                continue
            conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                         "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("headroom-savings", obj.get("ts"), "compression",
                          obj.get("model"), obj.get("source"), obj.get("client"),
                          int(obj.get("saved", 0)), float(obj.get("cost_usd", 0)), None,
                          json.dumps(obj, ensure_ascii=False)))
            n1 += 1
    # 2) 汇总: proxy_savings.json lifetime + by_model (kind=summary, 幂等: 先删旧 summary)
    ps = HEADROOM_DIR / "proxy_savings.json"
    if ps.exists():
        conn.execute("DELETE FROM ledger_events WHERE kind='summary' AND source='headroom-lifetime'")
        d = json.loads(ps.read_text(encoding="utf-8"))
        lt = d.get("lifetime", {})
        conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                     "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("headroom-lifetime", d.get("generated_at", now_iso()), "summary", None,
                      "aggregate", None, int(lt.get("tokens_saved", 0)),
                      float(lt.get("compression_savings_usd", 0)), None,
                      json.dumps({"cache_read_tokens": lt.get("cache_read_tokens"),
                                  "cache_savings_usd": lt.get("cache_savings_usd"),
                                  "requests": lt.get("requests"),
                                  "total_input_tokens": lt.get("total_input_tokens"),
                                  "total_input_cost_usd": lt.get("total_input_cost_usd")}, ensure_ascii=False)))
        n2 = 1
    conn.commit()
    print(f"账本导入完成: 明细 {n1} 条 (savings_events) + 汇总 1 条 (lifetime)")


def fmt_usd(x):
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "-"


def cmd_ledger_stats(args):
    conn = db(vault_dir(args))
    row = conn.execute("SELECT tokens_saved, cost_usd FROM ledger_events WHERE source='headroom-lifetime'").fetchone()
    if row:
        raw = json.loads(conn.execute("SELECT raw FROM ledger_events WHERE source='headroom-lifetime'").fetchone()[0])
        print("== lifetime (headroom 通路层累计) ==")
        print(f"  压缩节省: {row['tokens_saved']:,} tokens = {fmt_usd(row['cost_usd'])}")
        print(f"  缓存节省: {int(raw.get('cache_read_tokens', 0)):,} cache-read tokens = {fmt_usd(raw.get('cache_savings_usd'))}")
        print(f"  总输入:   {int(raw.get('total_input_tokens', 0)):,} tokens = {fmt_usd(raw.get('total_input_cost_usd'))}")
    rows = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c "
                        "FROM ledger_events WHERE kind='compression' GROUP BY model "
                        "ORDER BY c DESC LIMIT 10").fetchall()
    if rows:
        print("== 按模型 (明细) ==")
        for r in rows:
            print(f"  {r['model']:<24} 事件={r['n']:<6} 省={r['t'] or 0:>12,} tok  {fmt_usd(r['c'] or 0)}")
    snap = conn.execute("SELECT COUNT(*) n, SUM(tokens_in) i, SUM(total_tokens) t FROM snapshots").fetchone()
    if snap and snap["n"]:
        print(f"== vault 快照 ({snap['n']} 个) ==")
        print(f"  快照累计入token={snap['i'] or 0:,}  总token={snap['t'] or 0:,}")



# ------------------------------------------------- warm: 缓存预热 (M2, Pro 核心能力)

def chat_url(base: str) -> str:
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def warm_messages(env: dict, cap_chars: int) -> list:
    """活上下文 → chat messages (近似前缀; 精确前缀预热用 --prefix-file)。"""
    live, comp_summary = live_context(env["conversation"])
    msgs = []
    if comp_summary:
        msgs.append({"role": "user", "content": "[context-vault 迁移摘要] " + comp_summary[:4000]})
    total = 0
    for b in live:
        if b["type"] == "text" and b.get("role") in ("user", "assistant"):
            content = b["content"][:cap_chars]
            total += len(content)
            msgs.append({"role": b["role"], "content": content})
    return msgs


def post_chat(url: str, key: str, model: str, messages: list, timeout: int = 120) -> dict:
    body = json.dumps({"model": model, "messages": messages, "max_tokens": 1,
                       "temperature": 0}).encode("utf-8")
    # 自定义 UA 必需: Python-urllib 默认 UA 会被部分 relay 的 CDN 防护拦截 (实测 502)
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}",
                                          "User-Agent": "ContextVault/0.1"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), int((time.time() - t0) * 1000)


def cmd_warm(args):
    vault = vault_dir(args)
    env = load_snapshot(vault if isinstance(vault, Path) else args, args.snapshot)
    profiles = load_profiles()
    prof = next((p for p in profiles if p["id"] == args.profile), None)
    if not prof:
        raise SystemExit(f"profile 不存在: {args.profile} (可用: {[p['id'] for p in profiles]})")
    key = resolve_key(args)
    if not key:
        raise SystemExit("无 API key (设 CV_RELAY_KEY / --key, 或安装 headroom-auth-token.ps1)")
    model = args.model or prof.get("default_model") or "gpt-5.6-sol"
    if args.prefix_file:
        messages = [{"role": "user", "content": Path(args.prefix_file).read_text(encoding="utf-8")[:args.cap]}]
        mode = f"prefix-file {args.prefix_file}"
    else:
        messages = warm_messages(env, args.cap)
        mode = f"snapshot {args.snapshot} 活上下文"
    nchars = sum(len(m["content"]) for m in messages)
    print(f"预热目标: [{prof['id']}] {model}  模式: {mode}  消息={len(messages)} 字符={nchars:,}")
    url = chat_url(prof["openai_api_url"])
    # 第一次: 填充缓存
    r1, ms1 = post_chat(url, key, model, messages)
    u1 = r1.get("usage", {})
    print(f"  ① 预热请求: {ms1}ms  usage={json.dumps({k: u1.get(k) for k in ('prompt_tokens','completion_tokens')}, ensure_ascii=False)}")
    # 第二次: 验证缓存命中
    r2, ms2 = post_chat(url, key, model, messages)
    u2 = r2.get("usage", {})
    cached = (u2.get("prompt_tokens_details") or {}).get("cached_tokens") or u2.get("cached_tokens") or 0
    print(f"  ② 复验请求: {ms2}ms  prompt={u2.get('prompt_tokens')}  cached={cached}")
    conn = db(vault)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (now_iso(), prof["id"], url, "warm", 200, ms1, json.dumps(
                     {"model": model, "messages": len(messages), "chars": nchars,
                      "prompt_tokens": u2.get("prompt_tokens"), "cached_tokens": cached}), None))
    conn.commit()
    if cached:
        print(f"✓ 缓存已生效: 第二次请求 {cached}/{u2.get('prompt_tokens')} tokens 命中缓存 — 切换后真实会话将直接受益")
    else:
        print("⚠ 该上游未返回缓存命中间接信号 (cached_tokens); 前缀是否入缓存取决于供应商实现, 延迟对比可作旁证")


# ==== M2 IMPL (dashboard/watch/serve) ====
# ------------------------------------------------- dashboard: 账本可视化 (M2)

DASH_TMPL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Context Vault · token 经济面板</title>
<style>
:root{--bg:#0e1116;--card:#171c24;--ink:#e6e9ef;--dim:#8b93a3;--acc:#4f9cf9;--good:#3ecf8e;--warn:#f5a623}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.6 "Segoe UI",system-ui,sans-serif;padding:28px}
h1{font-size:20px;margin-bottom:2px} .sub{color:var(--dim);font-size:12px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:26px}
.card{background:var(--card);border-radius:12px;padding:16px 18px}
.card .v{font-size:24px;font-weight:700;color:var(--good)} .card .k{color:var(--dim);font-size:12px}
.card.alt .v{color:var(--acc)}
h2{font-size:15px;margin:24px 0 12px;color:var(--ink)}
.bar{display:flex;align-items:center;gap:10px;margin:7px 0}
.bar .name{width:220px;color:var(--dim);font-size:12px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{flex:1;background:#1d2430;border-radius:6px;height:18px;position:relative}
.bar .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#2d7d46,var(--good))}
.bar .val{width:130px;font-size:12px;color:var(--ink)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #202836}
th{color:var(--dim);font-weight:500}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.ok{background:var(--good)} .err{background:#e5534b} .warm{background:var(--warn)}
.months{display:flex;align-items:flex-end;gap:6px;height:120px}
.mcol{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:4px}
.mcol .stick{width:100%;max-width:46px;background:linear-gradient(180deg,var(--acc),#2a5f9e);border-radius:4px 4px 0 0}
.mcol .lb{font-size:10px;color:var(--dim)}
</style></head><body>
<h1>Context Vault · token 经济面板</h1>
<div class="sub">本地优先的信任货币账本 · __GENERATED__</div>
<div class="cards">__CARDS__</div>
<h2>按模型节省 (明细账)</h2>__BYMODEL__
<h2>月度节省走势 (__MONTHS__ 个月)</h2><div class="months">__TIMELINE__</div>
<h2>上游可用性 (最近探测/切换)</h2><table><tr><th></th><th>时间</th><th>profile</th><th>状态</th><th>详情</th></tr>__PROBE__</table>
<h2>vault 快照资产 (__NSNAP__)</h2><table><tr><th>id</th><th>agent</th><th>块</th><th>入 token</th><th>cache 读</th><th>完整性</th><th>时间</th></tr>__SNAPS__</table>
</body></html>"""


def cmd_dashboard(args):
    vault = vault_dir(args)
    conn = db(vault)
    cards, bymodel, months, probews, snaps = [], [], [], [], []
    row = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events "
                       "WHERE source='headroom-lifetime'").fetchone()
    raw = json.loads(row["raw"]) if row else {}
    usd = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else "-"
    total_saved = ((row["cost_usd"] or 0) + (raw.get("cache_savings_usd") or 0)) if row else 0
    cards.append(f'<div class="card"><div class="v">{usd(total_saved)}</div><div class="k">累计节省 (压缩+缓存)</div></div>')
    cards.append(f'<div class="card alt"><div class="v">{usd(raw.get("cache_savings_usd"))}</div><div class="k">缓存节省</div></div>')
    cards.append(f'<div class="card alt"><div class="v">{int(raw.get("total_input_tokens", 0)):,}</div><div class="k">历史总输入 tokens</div></div>')
    cards.append(f'<div class="card"><div class="v">{int(row["tokens_saved"] or 0):,}</div><div class="k">压缩节省 tokens</div></div>')
    rows = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c "
                        "FROM ledger_events WHERE kind='compression' GROUP BY model ORDER BY c DESC").fetchall()
    mx = max((r["c"] or 0) for r in rows) if rows else 1
    for r in rows:
        w = int(100 * (r["c"] or 0) / mx) if mx else 0
        bymodel.append(f'<div class="bar"><div class="name">{r["model"]}</div>'
                       f'<div class="track"><div class="fill" style="width:{w}%"></div></div>'
                       f'<div class="val">{usd(r["c"] or 0)} · {(r["t"] or 0):,} tok</div></div>')
    rows = conn.execute("SELECT substr(occurred_at,1,7) m, SUM(cost_usd) c FROM ledger_events "
                        "WHERE kind='compression' GROUP BY m ORDER BY m").fetchall()
    mx = max((r["c"] or 0) for r in rows) if rows else 1
    for r in rows:
        h = int(100 * (r["c"] or 0) / mx) if mx else 0
        months.append(f'<div class="mcol"><div class="stick" style="height:{max(h,3)}%" title="{usd(r["c"] or 0)}"></div>'
                      f'<div class="lb">{r["m"][2:]}</div></div>')
    for r in conn.execute("SELECT * FROM probe_events ORDER BY id DESC LIMIT 15"):
        cls = {"ok": "ok", "warm": "warm"}.get(r["status"], "err")
        detail = (r["missing_models"] or "") or (r["models"] or "")[:60]
        probews.append(f'<tr><td><span class="dot {cls}"></span></td><td>{r["ts"][:19]}</td>'
                       f'<td>{r["profile"]}</td><td>{r["status"]}</td><td>{detail}</td></tr>')
    for r in conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 20"):
        snaps.append(f'<tr><td>{r["id"]}</td><td>{r["agent"]}</td><td>{r["blocks_total"]}</td>'
                     f'<td>{r["tokens_in"]:,}</td><td>{r["cache_read"]:,}</td>'
                     f'<td>{r["completeness"]}</td><td>{str(r["created_at"])[:19]}</td></tr>')
    html = (DASH_TMPL.replace("__GENERATED__", now_iso())
            .replace("__CARDS__", "".join(cards))
            .replace("__BYMODEL__", "".join(bymodel) or '<div class="sub">明细为空</div>')
            .replace("__MONTHS__", str(len(months)))
            .replace("__TIMELINE__", "".join(months))
            .replace("__PROBE__", "".join(probews))
            .replace("__NSNAP__", str(len(snaps)))
            .replace("__SNAPS__", "".join(snaps)))
    out = Path(args.out) if args.out else vault / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"面板已生成: {out.resolve()}")
    if args.open:
        subprocess.run(["cmd", "/c", "start", "", str(out.resolve())], shell=False)


# ------------------------------------------------- watch: 切换携带会话 (M2)

def active_sessions(hours: float = 8) -> list:
    cutoff = time.time() - hours * 3600
    found = []
    for f in CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("codex", str(f)))
    for f in (HOME / ".claude" / "projects").glob("*/*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("claude-code", str(f)))
    for f in (HOME / ".claude" / "transcripts").glob("*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("claude-transcript", str(f)))
    return sorted(found, key=lambda x: -Path(x[1]).stat().st_mtime)


def toast(title: str, msg: str):
    esc = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
          "ContentType=WindowsRuntime] | Out-Null; "
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument; "
          "$x.LoadXml('<toast><visual><binding template=\"ToastText02\">"
          f"<text id=\"1\">{title}</text><text id=\"2\">{esc}</text>"
          "</binding></visual></toast>'); "
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Context Vault')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    print(f"\a⚠ {title}: {msg}", file=sys.stderr)


def cmd_watch(args):
    vault = vault_dir(args)
    prof_p = Path(args.file) if args.file else HEADROOM_DIR / "upstream-profile.json"
    if not prof_p.exists():
        raise SystemExit(f"未找到 {prof_p} (Switcher 未安装?)")
    conn = db(vault)
    conn.executescript(SCHEMA)
    last = prof_p.read_text(encoding="utf-8")
    print(f"守望中: 监听 {prof_p} (当前 "
          f"{json.loads(last).get('profile', '?')}), 每 {args.interval}s 检查; Ctrl+C 退出")
    while True:
        time.sleep(args.interval)
        try:
            cur = prof_p.read_text(encoding="utf-8")
        except OSError:
            continue
        if cur == last:
            continue
        last = cur
        try:
            prof = json.loads(cur)
        except ValueError:
            continue
        pid = prof.get("profile", "?")
        acts = active_sessions(args.hours)
        by_agent = {}
        for a, _ in acts:
            by_agent[a] = by_agent.get(a, 0) + 1
        acts_desc = ", ".join(f"{k}×{v}" for k, v in by_agent.items()) or "无"
        msg = (f"已切换到 [{pid}]。最近 {args.hours:.0f}h 活跃会话: {acts_desc}。"
               "携带迁移: cv migrate <会话文件> --target codex|claude")
        toast("Context Vault · 上游已切换", msg)
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (now_iso(), pid, "switch-detected", "switch", None, None,
                      json.dumps({"active_sessions": len(acts), "by_agent": by_agent},
                                 ensure_ascii=False), None))
        conn.commit()
        for agent, f in acts[:5]:
            print(f"  · [{agent}] {f}")


# ------------------------------------------------- serve: 常驻服务 + 热索引 (M2)

def tokenize(text: str) -> list:
    """英文词 + CJK 单字及 bigram (中英混合检索够用)."""
    out, cur = [], []
    for ch in text.lower():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
            if "\u4e00" <= ch <= "\u9fff":
                out.append(ch)
    if cur:
        out.append("".join(cur))
    words = [t for t in out if len(t) >= 2]
    cjk = [out[i] + out[i + 1] for i in range(len(out) - 1)
           if len(out[i]) == 1 and len(out[i + 1]) == 1]
    return words + cjk


class VaultIndex:
    """内存倒排索引: term -> [(sid, block_idx)]; 指纹失效自动重建。"""

    def __init__(self, vault: Path, block_text_fn):
        self.vault = vault
        self.block_text = block_text_fn
        self.fingerprint = None
        self.index, self.meta, self.envs = {}, {}, {}
        self.rebuild(force=True)

    def _fp(self) -> str:
        snap_dir = self.vault / "snapshots"
        return hashlib.sha256("|".join(f"{f.name}:{int(f.stat().st_mtime)}" for f in
                                       sorted(snap_dir.glob("cv_*.json"))).encode()).hexdigest()

    def rebuild(self, force=False):
        fp = self._fp()
        if not force and fp == self.fingerprint:
            return
        self.index, self.meta, self.envs = {}, {}, {}
        for f in sorted((self.vault / "snapshots").glob("cv_*.json")):
            try:
                env = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            sid = f.stem
            env["_sid"] = sid
            self.envs[sid] = env
            self.meta[sid] = {"agent": env["capture"].get("agent"),
                              "created_at": env.get("created_at"),
                              "cwd": env["capture"].get("cwd"),
                              "blocks": env["stats"]["blocks_total"],
                              "tokens_in": env["stats"]["tokens_in"],
                              "completeness": env["capture"].get("completeness")}
            for i, b in enumerate(env["conversation"]):
                for t in set(tokenize(self.block_text(b))):
                    self.index.setdefault(t, []).append((sid, i))
        self.fingerprint = fp

    def search(self, query: str, limit: int) -> dict:
        from collections import Counter
        terms = tokenize(query)
        sc = Counter()
        for t in terms:
            for sid, _ in self.index.get(t, []):
                sc[sid] += 1
        return {"query": query, "terms": terms,
                "hits": [{"sid": sid, "score": n, **self.meta[sid]}
                         for sid, n in sc.most_common(limit)]}

    def context(self, query: str, limit: int) -> dict:
        from collections import Counter
        terms = tokenize(query)
        pairs = Counter()
        for t in terms:
            for sid, i in self.index.get(t, []):
                pairs[(sid, i)] += 1
        out, seen = [], set()
        for (sid, i), n in pairs.most_common():
            if len(out) >= limit:
                break
            if sid in seen:
                continue
            seen.add(sid)
            b = self.envs[sid]["conversation"][i]
            text = self.block_text(b)
            pos = text.lower().find(terms[0]) if terms else 0
            start = max(0, pos - 200)
            out.append({"sid": sid, "block": i, "role": b.get("role") or b["type"],
                        "score": n, "excerpt": text[start:start + 400],
                        "cwd": self.meta[sid].get("cwd")})
        return {"query": query, "results": out}


def build_vault_server(vault: Path, port: int):
    """构建热索引 HTTP 服务 (不阻塞); cmd_serve 与 Pro 托盘共用, 避免实现漂移。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    sys.path.insert(0, str(Path(__file__).parent))
    from mcp_server import block_text
    idx = VaultIndex(vault, block_text)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/healthz":
                    return self._json({"service": "context-vault", "status": "ok",
                                       "snapshots": len(idx.envs), "terms": len(idx.index)})
                if u.path == "/search":
                    idx.rebuild()
                    return self._json(idx.search(q.get("q", ""), int(q.get("limit", 5))))
                if u.path == "/context":
                    idx.rebuild()
                    return self._json(idx.context(q.get("q", ""), int(q.get("limit", 3))))
                if u.path == "/recent":
                    rows = sorted(idx.meta.items(), key=lambda kv: kv[1]["created_at"] or "",
                                  reverse=True)[:int(q.get("limit", 5))]
                    return self._json([{"sid": k, **v} for k, v in rows])
                if u.path == "/stats":
                    return self._json({"stats": ledger_summary(vault)})
                if u.path == "/snapshot":
                    env = idx.envs.get(q.get("id", ""))
                    if not env:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    dist = dict(Counter(b["type"] for b in env["conversation"]))
                    return self._json({"id": q.get("id"), "capture": env["capture"],
                                       "stats": env["stats"], "block_dist": dist})
                self._json({"error": "unknown route",
                            "routes": ["/healthz", "/search?q=", "/context?q=",
                                       "/recent", "/stats", "/snapshot?id="]}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv, idx


def cmd_serve(args):
    srv, idx = build_vault_server(vault_dir(args), args.port)
    print(f"vault 热索引就绪: {len(idx.envs)} 快照, {len(idx.index)} 词项")
    print(f"Context Vault 服务已启动: http://127.0.0.1:{args.port} (Ctrl+C 退出)")
    print("  MCP server 检测到本服务时自动走热索引 (CV_VAULT_SVC 可覆盖地址)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")

# ------------------------------------------------- doctor: 环境体检

def cmd_doctor(args):
    checks = []
    checks.append(("Codex 会话目录", CODEX_SESSIONS.exists(), str(CODEX_SESSIONS)))
    checks.append(("Claude 转录目录", (HOME / ".claude" / "transcripts").exists(), str(HOME / ".claude/transcripts")))
    checks.append(("headroom 配置目录", HEADROOM_DIR.exists(), str(HEADROOM_DIR)))
    checks.append(("savings_events.jsonl", (HEADROOM_DIR / "savings_events.jsonl").exists(), ""))
    checks.append(("proxy_savings.json", (HEADROOM_DIR / "proxy_savings.json").exists(), ""))
    status, _, _, ms = fetch_models("http://127.0.0.1:8787/livez", None, timeout=3)
    checks.append(("headroom 代理 8787", status == "ok", f"{status} {ms}ms"))
    vault = vault_dir(args)
    checks.append(("vault 初始化", (vault / "vault.db").exists(), str(vault)))
    ok = True
    for name, good, detail in checks:
        ok &= good
        print(f"  {'✓' if good else '✗'} {name}  {detail}")
    print("结论: " + ("环境就绪" if ok else "存在缺失项 (见上), 核心导出功能不依赖缺失项"))


# ------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="cv", description="Context Vault — 本地优先上下文资产库")
    ap.add_argument("--vault", default="vault", help="vault 目录 (默认 ./vault)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    e = sub.add_parser("export", help="导出会话文件为快照 (只读源文件)")
    e.add_argument("source")
    e.add_argument("--drop-developer", action="store_true")
    e.set_defaults(func=cmd_export)

    sub.add_parser("list").set_defaults(func=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("snapshot_id")
    s.set_defaults(func=cmd_show)

    d = sub.add_parser("diff", help="迁移前预览")
    d.add_argument("snapshot_id")
    d.set_defaults(func=cmd_diff)

    i = sub.add_parser("import", help="快照 → 目标 agent 会话文件")
    i.add_argument("snapshot_id")
    i.add_argument("--target", choices=["codex", "claude"], required=True)
    i.add_argument("--apply", action="store_true", help="写入真实会话目录 (默认 dry-run)")
    i.add_argument("--strip-thinking", action="store_true", help="不迁移 reasoning 摘要")
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="一键: 导出→预览→导入")
    m.add_argument("source")
    m.add_argument("--target", choices=["codex", "claude"], required=True)
    m.add_argument("--apply", action="store_true")
    m.add_argument("--yes", action="store_true")
    m.add_argument("--strip-thinking", action="store_true")
    m.add_argument("--drop-developer", action="store_true")
    m.set_defaults(func=cmd_migrate)

    pr = sub.add_parser("probe", help="上游 /v1/models 可用性探测")
    pr.add_argument("--profile")
    pr.add_argument("--watch", type=int, help="循环间隔秒数 (缺省单次)")
    pr.add_argument("--key", help="API key (缺省读 CV_RELAY_KEY 环境变量)")
    pr.set_defaults(func=cmd_probe)

    lg = sub.add_parser("ledger", help="token 账本")
    lsub = lg.add_subparsers(dest="ledger_cmd", required=True)
    lsub.add_parser("import-headroom").set_defaults(func=cmd_ledger_import_headroom)
    lsub.add_parser("stats").set_defaults(func=cmd_ledger_stats)

    w = sub.add_parser("warm", help="缓存预热: 切换上游前预发历史前缀 (M2/Pro)")
    w.add_argument("--snapshot", required=True, help="快照 id (活上下文作前缀)")
    w.add_argument("--profile", required=True)
    w.add_argument("--model")
    w.add_argument("--prefix-file", help="精确前缀文件 (优先于快照, 供应商缓存按 token 前缀匹配)")
    w.add_argument("--cap", type=int, default=20000, help="单消息字符上限 (默认 20000)")
    w.add_argument("--key")
    w.set_defaults(func=cmd_warm)

# ==== M2 REG ====

    dsh = sub.add_parser("dashboard", help="token 经济可视化面板 (自包含 HTML)")
    dsh.add_argument("--out")
    dsh.add_argument("--open", action="store_true", help="生成后立即用浏览器打开")
    dsh.set_defaults(func=cmd_dashboard)

    wt = sub.add_parser("watch", help="守望上游切换: Switcher 换 profile 即提醒携带会话")
    wt.add_argument("--interval", type=int, default=2)
    wt.add_argument("--hours", type=float, default=8, help="活跃会话回看窗口 (小时)")
    wt.add_argument("--file", help="监听目标覆盖 (默认 ~/.headroom/upstream-profile.json; 测试用)")
    wt.set_defaults(func=cmd_watch)

    sv = sub.add_parser("serve", help="常驻服务: 热索引 + HTTP API (127.0.0.1)")
    sv.add_argument("--port", type=int, default=8791)
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)


    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
