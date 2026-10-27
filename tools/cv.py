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
    req = urllib.request.Request(url)
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


def cmd_probe(args):
    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA)
    baseline_p = vault / "probe_baseline.json"
    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
    key = resolve_key(args)
    profiles = load_profiles()
    if args.profile:
        profiles = [p for p in profiles if p["id"] == args.profile] or profiles
    while True:
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
                flag = "✗" if missing else "✓"
                print(f"{flag} [{pid}] ok http={code} {ms}ms  models={len(ids)}"
                      + (f"  消失: {missing}" if missing else ""))
                if missing:
                    notify("上游模型变动", f"[{pid}] 消失: {', '.join(missing)}")
            else:
                # 端点异常(网络/鉴权) ≠ 模型下架, 不进入模型消失判定, 防止狼来了效应
                detail = ids[0] if ids else ""
                print(f"✗ [{pid}] {status} http={code} {ms}ms  端点异常: {detail}")
                notify("上游端点异常", f"[{pid}] {status} http={code} — 无法判定模型状态")
            conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                         " VALUES(?,?,?,?,?,?,?,?)",
                         (now_iso(), pid, url, status, code, ms, json.dumps(ids), json.dumps(missing)))
        conn.commit()
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

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
