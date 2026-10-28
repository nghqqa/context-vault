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
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # noconsole 进程 spawn 控制台程序不弹窗   # context-snapshot 格式版本
VERSION = "0.4.0"          # 工具版本 (发布节奏独立于格式)
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
        # 标准 Claude Code: user/assistant 行带 message 嵌套; ZCode 变体: 平铺 content
        return "claude-projects" if "message" in first else "claude-transcript"
    # 首行可能是 queue-operation/attachment 等辅助行, 扫前 20 行找判别行
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 20:
                break
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") in ("user", "assistant"):
                return "claude-projects" if "message" in obj else "claude-transcript"
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


def export_claude_code(path: Path, drop_developer: bool) -> dict:
    """标准 Claude Code 会话 (~/.claude/projects/<cwd码>/<uuid>.jsonl) → 快照。
    结构经真实 2.1.181 会话验证: user/assistant 行带 message 嵌套;
    tool_use 在 assistant.content 块数组 (id/name/input), tool_result 在 user 行块数组 (tool_use_id);
    usage 键: input_tokens / cache_read_input_tokens / cache_creation_input_tokens / output_tokens。"""
    blocks, stats, dropped = [], zero_stats(), 0
    capture = {"agent": "claude-code", "source_format": "claude-projects", "provider": None,
               "model": None, "session_ref": path.stem, "completeness": "full"}
    for obj in iter_jsonl(path):
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue  # queue-operation/attachment/last-prompt/system/mode 等辅助行
        msg = obj.get("message") or {}
        content = msg.get("content")
        ts = obj.get("timestamp", "")
        if t == "user":
            if isinstance(content, str):
                blocks.append({"type": "text", "role": "user", "content": content, "ts": ts})
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "role": "user",
                                       "content": b.get("text", ""), "ts": ts})
                    elif b.get("type") == "tool_result":
                        out = b.get("content")
                        if isinstance(out, list):
                            out = chr(10).join(x.get("text", "") for x in out if isinstance(x, dict))
                        text, _ = clip(str(out or ""))
                        blocks.append({"type": "tool_result", "call_id": b.get("tool_use_id"),
                                       "content": text, "is_error": False, "ts": ts})
        else:  # assistant
            usage = msg.get("usage") or {}
            if usage:
                stats["tokens_in"] += int(usage.get("input_tokens", 0) or 0)
                stats["tokens_out"] += int(usage.get("output_tokens", 0) or 0)
                stats["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
                stats["cache_write"] += int(usage.get("cache_creation_input_tokens", 0) or 0)
                stats["usage_events"] += 1
            if not capture.get("model") and msg.get("model"):
                capture["model"] = msg.get("model")
            for b in (content or []):
                bt = b.get("type")
                if bt == "text":
                    blocks.append({"type": "text", "role": "assistant",
                                   "content": b.get("text", ""), "ts": ts})
                elif bt == "thinking":
                    summary = b.get("thinking", "") or b.get("text", "") or ""
                    if summary:
                        blocks.append({"type": "thinking", "summary": summary, "ts": ts})
                elif bt == "tool_use":
                    blocks.append({"type": "tool_call", "call_id": b.get("id"),
                                   "name": b.get("name"), "namespace": None,
                                   "arguments": b.get("input"), "ts": ts})
        if obj.get("cwd") and not capture.get("cwd"):
            capture["cwd"] = obj.get("cwd")
        if obj.get("version") and not capture.get("agent_version"):
            capture["agent_version"] = obj.get("version")
    if not stats["usage_events"]:
        capture["completeness"] = "full"  # 内容完整, 只是源不带 usage
    stats["total_tokens"] = stats["tokens_in"] + stats["tokens_out"] + stats["cache_read"] + stats["cache_write"]
    stats["blocks_total"], stats["blocks_dropped"] = len(blocks), dropped
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
    exporters = {"codex-rollout": export_codex, "claude-transcript": export_claude,
                 "claude-projects": export_claude_code}
    exported = exporters[fmt](src, args.drop_developer)
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


def do_import(vault: Path, sid: str, target: str, apply: bool, strip_thinking=False) -> dict:
    """迁移执行核心 (CLI 与控制台 HTTP 共用)。返回 {lines, dropped, dest, new_sid, applied}。"""
    env = load_snapshot(vault, sid)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    if target == "codex":
        lines, dropped, new_sid, name = blocks_to_codex(env, strip_thinking)
        dest = vault / "outbox" / name
        if apply:
            day = dt.datetime.now()
            dest = CODEX_SESSIONS / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        lines, dropped = blocks_to_claude(env, strip_thinking)
        name = f"{uuid.uuid4()}.jsonl"
        cwd = env["capture"].get("cwd") or str(Path.cwd())
        dest = vault / "outbox" / f"claude-{sid}-{name}"
        if apply:
            dest = claude_project_dir(cwd) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    return {"lines": len(lines), "dropped": dropped, "dest": str(dest),
            "new_sid": new_sid if target == "codex" else None, "applied": apply}


def cmd_import(args):
    vault = vault_dir(args)
    r = do_import(vault, args.snapshot_id, args.target, args.apply, args.strip_thinking)
    if r["new_sid"]:
        print(f"(session_id: {r['new_sid']})")
    where = "已写入真实会话目录" if r["applied"] else "dry-run (vault/outbox/, 加 --apply 才写真实会话目录)"
    print(f"导入 {r['lines']} 行 → {r['dest']}"
          + (f"  (丢弃无法配对的 tool_result×{r['dropped']})" if r["dropped"] else ""))
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
                capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW)
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
    global _PROBE_ALERT_STATE
    try:
        _PROBE_ALERT_STATE
    except NameError:
        _PROBE_ALERT_STATE = set()  # (profile,kind,签名): 持续同样异常只首弹, 防通知轰炸
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
                if (pid, "missing", tuple(missing)) not in _PROBE_ALERT_STATE:
                    _PROBE_ALERT_STATE.add((pid, "missing", tuple(missing)))
                    notify("上游模型变动", f"[{pid}] 消失: {', '.join(missing)}")
        else:
            # 端点异常(网络/鉴权) ≠ 模型下架, 不进入模型消失判定, 防止狼来了效应
            detail = ids[0] if ids else ""
            if verbose:
                print(f"✗ [{pid}] {status} http={code} {ms}ms  端点异常: {detail}")
            alerts.append({"profile": pid, "kind": "endpoint", "detail": detail})
            if (pid, "endpoint", status, code) not in _PROBE_ALERT_STATE:
                _PROBE_ALERT_STATE.add((pid, "endpoint", status, code))
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


def owned_stats(vault: Path) -> dict:
    """Vault 自有账本: 全部快照的 token 消耗聚合 (零外部依赖, 人人开箱即有)。
    与 headroom lifetime (压缩/缓存节省金额) 分轨: 那是通路层增强, 这是资产层自有数据。"""
    conn = db(vault)
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o,"
                     " COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_write),0) cw,"
                     " COALESCE(SUM(total_tokens),0) t FROM snapshots").fetchone()
    by_agent = conn.execute("SELECT agent, COUNT(*) n, SUM(tokens_in) i FROM snapshots"
                            " GROUP BY agent ORDER BY i DESC").fetchall()
    hit = (r["cr"] / (r["i"] or 1))
    return {"snapshots": r["n"], "tokens_in": r["i"], "tokens_out": r["o"],
            "cache_read": r["cr"], "cache_write": r["cw"], "total_tokens": r["t"],
            "cache_hit_rate": round(hit, 3),
            "by_agent": [{"agent": a["agent"], "snapshots": a["n"], "tokens_in": a["i"]}
                         for a in by_agent],
            "source": "vault-owned (from agent session usage records)"}


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
    zx = conn.execute("SELECT COUNT(*) n, model FROM ledger_events WHERE kind='zcode_usage'"
                      " GROUP BY model ORDER BY n DESC").fetchall()
    if zx:
        tot = conn.execute("SELECT COUNT(*) n, SUM(CAST(json_extract(raw,'$.input') AS INT)) i,"
                           " SUM(CAST(json_extract(raw,'$.cache_read') AS INT)) cr"
                           " FROM ledger_events WHERE kind='zcode_usage'").fetchone()
        print(f"== zcode 用量 ({tot['n']} 次调用, 审计直读) ==")
        print(f"  入 {tot['i'] or 0:,} · cache读 {tot['cr'] or 0:,}"
              f" · 模型: {', '.join(r['model'] for r in zx[:3])}")
    own = owned_stats(vault_dir(args))
    if own["snapshots"]:
        print(f"== vault 自有账本 ({own['snapshots']} 快照, 零外部依赖) ==")
        print(f"  会话累计消耗: 入 {own['tokens_in']:,} · 出 {own['tokens_out']:,} · "
              f"cache 读 {own['cache_read']:,} (命中率 {own['cache_hit_rate']*100:.0f}%)")
        for a in own["by_agent"][:5]:
            print(f"    {a['agent']:<16} {a['snapshots']:>3} 会话 · {a['tokens_in'] or 0:>13,} tok")



# ------------------------------------------------- ledger import-relay: 直连账单端点

def fetch_relay_usage(base: str, key: str, start: str, end: str) -> dict:
    """OpenAI 兼容 dashboard/billing/usage (new-api/one-api 系普遍支持; 部分中转关闭)。"""
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = f"{base}/dashboard/billing/usage?start_date={start}&end_date={end}"
    req = urllib.request.Request(url, headers={"User-Agent": "ContextVault/0.1",
                                               "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(f"该端点未开放 billing API (404) — 实测 999555999 即此情况; "
                             "此 relay 的账本只能经 headroom 导入")
        raise SystemExit(f"billing API {e.code}: {e.reason}")


def fetch_relay_web_logs(base: str, token: str, user_id: str | None,
                           path: str, start_ts: int, page_size: int = 100) -> list:
    """new-api / one-api 系 web 消费日志 (/api/log/self, 系统访问令牌认证)。
    999555999 前端为 Next.js(neko 系), 实际路径若不同可用 --log-path 覆盖。"""
    base = base.rstrip("/")
    out, page = [], 0
    while True:
        url = f"{base}{path}?p={page}&page_size={page_size}&type=0&start_timestamp={start_ts}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "ContextVault/0.1", "Authorization": token,
            **({"New-Api-User": user_id} if user_id else {})})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = (data.get("data") or {}).get("items") or data.get("data") or []
        if not isinstance(items, list) or not items:
            break
        out += items
        if len(items) < page_size:
            break
        page += 1
    return out


QUOTA_PER_USD = 500000  # new-api 默认换算; 各站倍率不同, 入账标"估算"


def cmd_ledger_import_zcode(args):
    """Zcode 用量入账: ~/.zcode/cli/rollout/model-io-*.jsonl (每行一次模型调用, camelCase usage)。
    当前版 Zcode 主对话不落盘 (model-io 只存审计: system/tools 模板 + 截断 response + 完整 usage),
    故只做账本源; 订阅制无单价, cost 留空 (诚实)。requestId 幂等去重。"""
    vault = vault_dir(args)
    io_dir = HOME / ".zcode" / "cli" / "rollout"
    files = sorted(io_dir.glob("model-io-*.jsonl")) if io_dir.exists() else []
    if not files:
        raise SystemExit("未找到 ~/.zcode/cli/rollout/model-io-*.jsonl (zcode 未使用?)")
    conn = db(vault)
    conn.executescript(SCHEMA)
    seen = set()
    for (rawj,) in conn.execute("SELECT raw FROM ledger_events WHERE kind='zcode_usage'"):
        try:
            seen.add(json.loads(rawj).get("requestId"))
        except (ValueError, TypeError):
            pass
    n = 0
    agg = {}
    for f in files:
        for l in open(f, encoding="utf-8"):
            try:
                o = json.loads(l)
            except ValueError:
                continue
            rid = o.get("requestId")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            resp = o.get("response") or {}
            u = resp.get("usage") or {}
            model = (o.get("model") or {}).get("modelId") or resp.get("modelId") or "zcode"
            tin = int(u.get("inputTokens") or 0)
            tout = int(u.get("outputTokens") or 0)
            tcr = int(u.get("cacheReadTokens") or 0)
            tcw = int(u.get("cacheWriteTokens") or 0)
            conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                         "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("zcode-modelio", o.get("completedAt") or now_iso(), "zcode_usage",
                          model, (o.get("model") or {}).get("providerId"), "zcode", 0, None, None,
                          json.dumps({"requestId": rid, "input": tin, "output": tout,
                                      "cache_read": tcr, "cache_write": tcw,
                                      "duration_ms": o.get("durationMs")}, ensure_ascii=False)))
            a = agg.setdefault(model, [0, 0, 0, 0])
            a[0] += tin; a[1] += tout; a[2] += tcr; a[3] += tcw
            n += 1
    conn.commit()
    print(f"zcode 用量入账: {n} 条新记录 (requestId 幂等)")
    for m, (i, o_, cr, cw) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        print(f"  {m:<12} 入 {i:>12,} · 出 {o_:>9,} · cache读 {cr:>13,} · cache写 {cw:,}")


def cmd_ledger_import_relay(args):
    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA)
    key = resolve_key(args)
    if args.mode == "web":
        token = args.token or os.environ.get("CV_RELAY_WEB_TOKEN")
        if not token:
            raise SystemExit("web 模式需要系统访问令牌: --token 或 CV_RELAY_WEB_TOKEN"
                             "(中转站 web 个人设置里生成; 999555999 面板待提供令牌后对号入座)")
        wm_p = vault / "relay_watermark.json"
        wm = json.loads(wm_p.read_text(encoding="utf-8")) if wm_p.exists() else {}
        last_ts = int(wm.get(args.url, 0))
        logs = fetch_relay_web_logs(args.url, token, args.user_id, args.log_path, last_ts)
        n = 0
        for item in logs:
            ts = int(item.get("created_at", 0))
            quota = int(item.get("quota", 0) or 0)
            conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                         "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("relay-web", dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
                          if ts else now_iso(), "relay_log", item.get("model_name"), args.url,
                          item.get("channel") or None, 0, round(quota / QUOTA_PER_USD, 6), None,
                         json.dumps({"prompt_tokens": item.get("prompt_tokens"),
                                     "completion_tokens": item.get("completion_tokens"),
                                     "quota": quota, "use_time": item.get("use_time"),
                                     "cost_basis": "quota/500k 估算"}, ensure_ascii=False)))
            n += 1
            last_ts = max(last_ts, ts)
        wm[args.url] = last_ts
        wm_p.write_text(json.dumps(wm), encoding="utf-8")
        conn.commit()
        total = sum(int(i.get("quota", 0) or 0) for i in logs)
        print(f"[{args.url}] 拉取 {n} 条消费记录, ~${total / QUOTA_PER_USD:,.2f} (quota 估算) 已入账; "
              f"水位 {last_ts} (下次增量)")
        return
    # billing API 模式 (原逻辑)
    end = args.end or dt.date.today().isoformat()
    start = args.start or (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    if not key:
        raise SystemExit("无 API key (CV_RELAY_KEY / --key / headroom-auth-token)")
    data = fetch_relay_usage(args.url, key, start, end)
    total = float(data.get("total_usage", 0))
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                 "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("relay-billing", now_iso(), "relay_usage", None, args.url, None, 0,
                  total, None, json.dumps({"start": start, "end": end, **data}, ensure_ascii=False)))
    conn.commit()
    print(f"[{args.url}] {start} ~ {end}: ${total:,.2f} 已入账 (source=relay-billing)")



    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA)
    key = resolve_key(args)
    if not key:
        raise SystemExit("无 API key (CV_RELAY_KEY / --key / headroom-auth-token)")
    end = args.end or dt.date.today().isoformat()
    start = args.start or (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    data = fetch_relay_usage(args.url, key, start, end)
    total = float(data.get("total_usage", 0))  # OpenAI 口径: 美元(cent? openai 为 $, new-api 为 $)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                 "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("relay-billing", now_iso(), "relay_usage", None, args.url, None, 0,
                  total, None, json.dumps({"start": start, "end": end, **data}, ensure_ascii=False)))
    conn.commit()
    print(f"[{args.url}] {start} ~ {end}: ${total:,.2f} 已入账 (source=relay-billing)")


# ------------------------------------------------- sync: 自动入库 (增量 + lineage)

SYNC_TABLE = """
CREATE TABLE IF NOT EXISTS sync_sources(
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER, snapshot_id TEXT, synced_at TEXT);
"""


def scan_sources(extra_roots: list | None = None) -> list:
    """扫描本机全部会话源 (只读)。返回 [Path, ...]。"""
    roots = [("codex", CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl")),
             ("claude-projects", (HOME / ".claude" / "projects").glob("*/*.jsonl")),
             ("claude-transcripts", (HOME / ".claude" / "transcripts").glob("*.jsonl"))]
    for r in (extra_roots or []):
        p = Path(r).expanduser()
        roots.append((f"extra:{p.name}", p.rglob("*.jsonl")))
    files = []
    for _, it in roots:
        for f in it:
            try:
                if f.is_file() and f.stat().st_size > 0:
                    files.append(f)
            except OSError:
                continue
    return files


def sync_one(conn, vault: Path, path: Path) -> str:
    """单文件入库: 新增/更新(带 lineage)/跳过。返回状态。"""
    st = path.stat()
    row = conn.execute("SELECT mtime, size, snapshot_id FROM sync_sources WHERE path=?",
                       (str(path),)).fetchone()
    if row and abs(row["mtime"] - st.st_mtime) < 1 and row["size"] == st.st_size:
        return "skip"
    fmt = detect_format(first_jsonl(path), path)
    exporters = {"codex-rollout": export_codex, "claude-transcript": export_claude,
                 "claude-projects": export_claude_code}
    exported = exporters[fmt](path, False)
    envelope = build_envelope(exported)
    if exported["stats"]["blocks_total"] == 0:
        return "empty"  # 辅助文件/空会话不入库
    if row and row["snapshot_id"]:
        envelope["lineage"]["parent_snapshot"] = row["snapshot_id"]  # 同源演进 → 会话树
    sid = snapshot_id(envelope)
    out = vault / "snapshots" / f"{sid}.json"
    if not out.exists():
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
    register_snapshot(conn, envelope, sid, out)
    conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                 " VALUES(?,?,?,?,?)", (str(path), st.st_mtime, st.st_size, sid, now_iso()))
    return "update" if row else "new"


def cmd_sync(args):
    vault = vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    conn.executescript(SCHEMA + SYNC_TABLE)
    files = scan_sources(args.root)
    n_new = n_upd = n_skip = n_empty = 0
    t0 = time.time()
    for f in sorted(files):
        try:
            r = sync_one(conn, vault, f)
        except (SystemExit, Exception) as e:
            print(f"  ! 跳过 {f.name}: {str(e)[:80]}")
            n_skip += 1
            continue
        if r == "new":
            n_new += 1
        elif r == "update":
            n_upd += 1
        elif r == "empty":
            n_empty += 1
        else:
            n_skip += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"sync 完成: 新增 {n_new} · 更新 {n_upd} · 跳过 {n_skip}(未变/异常)"
          f" · 空会话 {n_empty} | vault 总快照 {total} | 耗时 {time.time()-t0:.1f}s")


# ------------------------------------------------- memory: M3 记忆层 (零依赖规则引擎 v0)

MEMORY_TABLE = """
CREATE TABLE IF NOT EXISTS memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
  kind TEXT, content TEXT, project TEXT,
  hits INTEGER DEFAULT 0, last_used TEXT);
CREATE INDEX IF NOT EXISTS idx_memory_snap ON memory_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind);
"""

# 提取规则 v0: 诚实规则式, 不假装语义理解; 外部引擎 (Supermemory/Graphiti) 走
# MemoryEngine 接口位 (高级语义记忆 = Pro 空间), 内置引擎为零依赖默认实现.
DECISION_MARKERS = ("决定", "决策", "采用", "方案是", "结论是", "结论：", "选型",
                    "我们选", "定了", "agreed", "we decide", "let's go with",
                    "chose", "final answer", "the plan is")
PREFERENCE_MARKERS = ("不要", "必须", "总是", "记住", "以后都", "每次都",
                      "never ", "always ", "remember to", "from now on")
MAX_MEMORY_PER_SNAPSHOT = 40


def extract_memories(env: dict) -> list:
    """从单个快照提取记忆条目: decision (assistant 决策句) / summary (压缩摘要)
    / preference (user 指令模式)。每条带块索引溯源 (raw 里 idx)。"""
    items, sid = [], env.get("_sid") or "?"
    project = None
    cwd = env["capture"].get("cwd")
    if cwd:
        project = str(cwd).rstrip("\\").rsplit("\\", 1)[-1] or str(cwd)
    seen = set()
    for i, b in enumerate(env["conversation"]):
        kind, text = None, ""
        if b["type"] == "compaction":
            kind, text = "summary", b.get("summary", "")
        elif b["type"] == "text" and b.get("role") == "assistant":
            text = b.get("content", "")
            kind = "decision" if any(m in text.lower() for m in DECISION_MARKERS) else None
        elif b["type"] == "text" and b.get("role") == "user":
            text = b.get("content", "")
            kind = "preference" if any(m in text.lower() for m in PREFERENCE_MARKERS) else None
        if not kind or not text:
            continue
        # 句级切分: 只取命中标记的句子 (±上下文一句), 控制颗粒度
        for sent in text.replace("\r", "").split("\n"):
            s = sent.strip()
            if not (12 <= len(s) <= 400):
                continue
            low = s.lower()
            if kind == "summary" or any(m in low for m in DECISION_MARKERS) \
               or (kind == "preference" and any(m in low[:24] for m in PREFERENCE_MARKERS)):
                key = s[:120]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"snapshot_id": sid, "kind": kind, "content": s,
                              "project": project, "block_idx": i})
        if len(items) >= MAX_MEMORY_PER_SNAPSHOT:
            break
    return items


# ---------------- MemoryEngine 协议 (外部引擎接口位) ----------------
# 内置 rule 引擎为零依赖默认; 外部引擎 (Supermemory/Graphiti/自定义 embedding 引擎)
# 通过 CV_MEMORY_ENGINE="path.to.module:ClassName" 或 --engine 加载, 实现同名协议即可
# 替换提取与检索。高级语义记忆 (embedding) 属 Pro 层规划。

ENGINE_PROTOCOL_DOC = ("MemoryEngine 协议: extract(env: dict) -> list[dict] "
                       "(kind/content/project/snapshot_id); "
                       "search(vault, query, limit) -> list[dict] "
                       "(kind/content/project/snapshot/score)")


class RuleMemoryEngine:
    """内置规则引擎 v0 — extract_memories/memory_search_api 的协议化封装。"""
    name = "rule"

    def extract(self, env: dict) -> list:
        return extract_memories(env)

    def search(self, vault: Path, query: str, limit: int) -> list:
        return memory_search_api(vault, query, limit)


def load_memory_engine(name: str):
    if name == "rule" or not name:
        return RuleMemoryEngine()
    mod_path, _, cls_name = name.partition(":")
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name or "Engine")()


def cmd_memory(args):
    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA + MEMORY_TABLE)
    engine_name = args.engine or os.environ.get("CV_MEMORY_ENGINE", "rule")
    engine = load_memory_engine(engine_name)
    if args.memory_cmd == "extract":
        snaps = load_all_snapshots_pub(vault)
        if args.snapshot:
            snaps = [e for e in snaps if e["_sid"] == args.snapshot]
        total = 0
        for env in snaps:
            conn.execute("DELETE FROM memory_items WHERE snapshot_id=?", (env["_sid"],))
            for it in engine.extract(env):
                conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)"
                             " VALUES(?,?,?,?,?)",
                             (now_iso(), it["snapshot_id"], it["kind"], it["content"], it["project"]))
                total += 1
        conn.commit()
        by = conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind").fetchall()
        print(f"记忆提取完成: {total} 条 (来自 {len(snaps)} 快照, engine={engine.name}) · "
              + " · ".join(f"{r[0]}×{r[1]}" for r in by))
    elif args.memory_cmd == "search":
        if engine_name != "rule":
            for m in engine.search(vault, args.query, args.limit):
                print(f"[{m['kind']:<10}] {m['content'][:100]}  ({m.get('project') or '?'})")
            return
        terms = tokenize(args.query)
        rows = conn.execute("SELECT * FROM memory_items").fetchall()
        scored = []
        for r in rows:
            low = r["content"].lower()
            n = sum(low.count(t) for t in terms)
            if n:
                scored.append((n + (3 if r["kind"] == "summary" else 0), r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            print(f"无相关记忆: {args.query}")
            return
        for n, r in scored[:args.limit]:
            print(f"[{r['kind']:<10}] {r['content'][:100]}  ({r['project'] or '?'})")
            conn.execute("UPDATE memory_items SET hits=hits+1, last_used=? WHERE id=?",
                         (now_iso(), r["id"]))
        conn.commit()
    elif args.memory_cmd == "list":
        q = "SELECT kind, COUNT(*) n FROM memory_items GROUP BY kind"
        for r in conn.execute(q):
            print(f"  {r[0]:<10} ×{r[1]}")
        for r in conn.execute("SELECT * FROM memory_items ORDER BY id DESC LIMIT ?", (args.limit,)):
            print(f"[{r['kind']:<10}] {r['content'][:90]}  ({r['project'] or '?'})")


def load_all_snapshots_pub(vault: Path) -> list:
    out = []
    for f in sorted((vault / "snapshots").glob("cv_*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            env["_sid"] = f.stem
            out.append(env)
        except (ValueError, OSError):
            continue
    return out


def memory_search_api(vault: Path, query: str, limit: int = 8) -> list:
    """serve /memory 与 MCP vault_remember 共用。"""
    conn = db(vault)
    conn.executescript(SCHEMA + MEMORY_TABLE)
    terms = tokenize(query)
    rows = conn.execute("SELECT * FROM memory_items").fetchall()
    scored = []
    for r in rows:
        low = r["content"].lower()
        n = sum(low.count(t) for t in terms)
        if n:
            scored.append((n + (3 if r["kind"] == "summary" else 0), r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for n, r in scored[:limit]:
        out.append({"kind": r["kind"], "content": r["content"], "project": r["project"],
                    "snapshot": r["snapshot_id"], "score": n})
        conn.execute("UPDATE memory_items SET hits=hits+1, last_used=? WHERE id=?",
                     (now_iso(), r["id"]))
    conn.commit()
    return out


# ------------------------------------------------- fork: 快照分叉 (M3)

def cmd_fork(args):
    """在历史节点开分支: 复制快照为新内容寻址实体, lineage.fork_of 指向原快照。
    原快照不可变; fork 的事实 (fork_of) 写在新实体自己的 lineage 里 (spec §7)。
    fork 后可 cv import <new-id> 把该节点写入 agent 从此续写。"""
    vault = vault_dir(args)
    env = load_snapshot(vault, args.snapshot_id)
    env["lineage"]["fork_of"] = args.snapshot_id
    if args.message:
        env["capture"]["fork_note"] = args.message
    sid = snapshot_id(env)
    out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        print(f"该分叉已存在 (幂等): {out.name}")
    else:
        out.write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已分叉: {args.snapshot_id} → {sid}"
              + (f" · 备注: {args.message}" if args.message else ""))
    conn = db(vault)
    conn.executescript(SCHEMA)
    register_snapshot(conn, env, sid, out)
    conn.commit()
    print(f"  续写: python tools/cv.py import {sid} --target codex|claude --apply")


def cmd_tree(args):
    """展示快照的 lineage 链: 向上追 parent (同源演进), 向下展 fork 子树。"""
    vault = vault_dir(args)
    env = load_snapshot(vault, args.snapshot_id)
    # 向上: parent 链 (sync 增量演进)
    chain = [args.snapshot_id]
    cur = env
    while cur["lineage"].get("parent_snapshot"):
        p = cur["lineage"]["parent_snapshot"]
        chain.append(p)
        ppath = vault / "snapshots" / f"{p}.json"
        if not ppath.exists():
            break
        cur = json.loads(ppath.read_text(encoding="utf-8"))
    chain.reverse()
    # 向下: fork 子树 (扫描 lineage.fork_of)
    children = {}
    for f in (vault / "snapshots").glob("cv_*.json"):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        fo = e.get("lineage", {}).get("fork_of")
        if fo:
            children.setdefault(fo, []).append(f.stem)
    print(f"快照 {args.snapshot_id} 的谱系:")
    for i, sid in enumerate(chain):
        mark = " → " if i < len(chain) - 1 else " ● "
        print(f"  {mark}{sid}" + ("  (本快照)" if sid == args.snapshot_id else ""))

    def show_forks(sid, depth=0):
        for c in children.get(sid, []):
            print("  " + "    " * depth + f"↳ fork {c}")
            show_forks(c, depth + 1)
    show_forks(args.snapshot_id)


# ------------------------------------------------- audit: 审计时间线回放 (M3)

def audit_summary_and_timeline(vault: Path, since: str, kind: str, limit: int = 200) -> dict:
    """统一事件流: probe/switch/warm + 汇总。数据源 probe_events (哨兵/守望/预热全留痕)。"""
    conn = db(vault)
    q = "SELECT * FROM probe_events WHERE 1=1"
    params = []
    if since:
        q += " AND ts >= ?"
        params.append(since)
    rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    if kind != "all":
        rows = [r for r in rows if kind in (r["status"],)]
    total = conn.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0]
    model_missing = sum(1 for r in conn.execute(
        "SELECT missing_models FROM probe_events WHERE missing_models != '[]'"
        " AND missing_models IS NOT NULL"))
    switches = conn.execute("SELECT COUNT(*) FROM probe_events WHERE status='switch'").fetchone()[0]
    ok_probes = conn.execute("SELECT COUNT(*) FROM probe_events WHERE status='ok'").fetchone()[0]
    err_probes = total - ok_probes - switches
    # 按天聚合可读时间线
    by_day = {}
    for r in rows:
        day = str(r["ts"])[:10]
        by_day.setdefault(day, []).append(r)
    timeline = []
    for day in sorted(by_day, reverse=True):
        events = [{"ts": str(r["ts"])[11:19], "profile": r["profile"], "status": r["status"],
                   "http": r["http_code"], "latency_ms": r["latency_ms"],
                   "missing": r["missing_models"] if r["missing_models"] not in ("[]", None) else None,
                   "detail": (r["models"] or "")[:80]}
                  for r in by_day[day]]
        timeline.append({"day": day, "events": events})
    return {"since": since or "(全部)", "total_events": total,
            "summary": {"ok_probes": ok_probes, "abnormal": err_probes, "switches": switches,
                        "model_missing_events": model_missing},
            "timeline": timeline}


def cmd_audit(args):
    vault = vault_dir(args)
    r = audit_summary_and_timeline(vault, args.since, args.kind)
    s = r["summary"]
    print(f"== 审计时间线 ({r['since']} 起, 库内共 {r['total_events']} 事件) ==")
    print(f"  探测正常 {s['ok_probes']} · 异常 {s['abnormal']} · 切换检测 {s['switches']}"
          f" · 模型消失事件 {s['model_missing_events']}")
    for day in r["timeline"][:args.days]:
        print(f"  {day['day']}  ({len(day['events'])} 事件)")
        for e in day["events"][:8]:
            mark = {"ok": "·", "switch": "⇄", "warm": "🔥"}.get(e["status"], "!")
            extra = f" 消失{e['missing']}" if e["missing"] else (f" {e['detail'][:40]}" if e["status"] == "switch" else "")
            print(f"    {mark} {e['ts']} [{e['profile']}] {e['status']}{extra}")
        if len(day["events"]) > 8:
            print(f"    … 其余 {len(day['events']) - 8} 条 (cv audit --days 更多)")


# ------------------------------------------------- mcp install: 一键接入客户端

def _mcp_cmd_parts(vault: Path) -> tuple:
    server = (Path(__file__).resolve().parent / "mcp_server.py")
    py = "python"
    return py, [str(server), "--vault", str(vault.resolve())]


def _claude_exe() -> str | None:
    import shutil
    return shutil.which("claude")  # npm .cmd shim 需 which 解析全路径


def _install_mcp_claude(vault: Path) -> str:
    exe = _claude_exe()
    if not exe:
        return "未找到 claude CLI (跳过)"
    r = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True, creationflags=NO_WINDOW)
    if "context-vault" in (r.stdout or ""):
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    r = subprocess.run([exe, "mcp", "add", "context-vault", "-s", "user", "--", py] + args,
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    return "已写入 ~/.claude.json (user scope)" if r.returncode == 0 else f"失败: {(r.stderr or r.stdout)[:120]}"


def _install_mcp_codex(vault: Path) -> str:
    cfg = HOME / ".codex" / "config.toml"
    if not cfg.parent.exists():
        return "未找到 ~/.codex (codex 未安装)"
    text = cfg.read_text(encoding="utf-8")
    if "context-vault" in text:
        return "已安装 (跳过)"
    cfg.with_suffix(".toml.bak-cv").write_text(text, encoding="utf-8")  # 备份
    py, args = _mcp_cmd_parts(vault)
    toml = ("\n[mcp_servers.context-vault]\n"
            f'command = "{py}"\n'
            "args = [" + ", ".join(f'"{a}"' for a in args) + "]\n")
    with cfg.open("a", encoding="utf-8") as fh:
        fh.write(toml)
    return "已追加到 ~/.codex/config.toml (备份 .bak-cv)"


def _install_mcp_cursor(vault: Path) -> str:
    cfg = Path.home() / ".cursor" / "mcp.json"
    if not cfg.parent.exists():
        return "未找到 ~/.cursor (cursor 未安装)"
    data = {}
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        cfg.with_suffix(".json.bak-cv").write_text(json.dumps(data, indent=2), encoding="utf-8")
    servers = data.setdefault("mcpServers", {})
    if "context-vault" in servers:
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    servers["context-vault"] = {"command": py, "args": args}
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "已写入 ~/.cursor/mcp.json"


def cmd_mcp(args):
    vault = vault_dir(args)
    clients = {"claude": _install_mcp_claude, "codex": _install_mcp_codex, "cursor": _install_mcp_cursor}
    if args.mcp_cmd == "install":
        targets = list(clients) if args.all else [args.client]
        for c in targets:
            print(f"[{c}] {clients[c](vault)}")
    elif args.mcp_cmd == "status":
        py, args_ = _mcp_cmd_parts(vault)
        print(f"server: {py} {' '.join(args_)}")
        exe = _claude_exe()
        out = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout if exe else ""
        print(f"[claude] {'✓ 已连接' if 'context-vault' in (out or '') else ('✗ 未安装' if exe else '✗ claude CLI 未找到')}")
        codex_cfg = HOME / ".codex" / "config.toml"
        print(f"[codex]  {'✓ 已配置' if codex_cfg.exists() and 'context-vault' in codex_cfg.read_text(encoding='utf-8') else '✗ 未安装'}")
        cur = Path.home() / ".cursor" / "mcp.json"
        print(f"[cursor] {'✓ 已配置' if cur.exists() and 'context-vault' in cur.read_text(encoding='utf-8') else '✗ 未安装'}")


# ------------------------------------------------- ingest: 经 session-migrate 接入任意 harness

SMIGRATE_CANDIDATES = ("smigrate", os.path.expanduser("~/.local/bin/smigrate"))


def find_smigrate() -> str | None:
    for cand in SMIGRATE_CANDIDATES:
        if subprocess.run(["cmd", "/c", "where", cand], capture_output=True, creationflags=NO_WINDOW).returncode == 0            or Path(cand).exists():
            return cand
    return None


def cmd_ingest(args):
    """任意 harness 会话 → 快照。链路: smigrate convert --to claude → claude-projects 适配器。
    覆盖 smigrate 支持的 17+ harness (opencode/cursor/qwen/kimi/grok/antigravity/pi/omp/...)。"""
    src_path = Path(args.source).expanduser()
    if not src_path.exists():
        raise SystemExit(f"文件不存在: {src_path}")
    exe = find_smigrate()
    if not exe:
        raise SystemExit("未找到 smigrate。安装: uv tool install session-migrate (MIT, Python>=3.11 独立环境)")
    vault = vault_dir(args)
    (vault / "ingest").mkdir(parents=True, exist_ok=True)
    tmp_out = vault / "ingest" / f"sm_{int(time.time())}_{uuid.uuid4().hex[:8]}.jsonl"
    cmd = [exe, "convert", str(src_path), "--to", "claude", "--output", str(tmp_out)]
    if args.format:
        cmd += ["--format", args.format]
    if args.cwd:
        cmd += ["--cwd", args.cwd]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    if r.returncode != 0 or not tmp_out.exists():
        raise SystemExit(f"smigrate convert 失败: {(r.stderr or r.stdout)[:400]}")
    exported = export_claude_code(tmp_out, args.drop_developer)
    origin = args.format or "harness"
    exported["capture"]["source_format"] = f"via-smigrate:{origin}"
    exported["capture"]["agent"] = origin  # 归属原始 harness, 而非 claude 中转层
    exported["capture"]["origin_path"] = str(src_path)
    envelope = build_envelope(exported)
    sid = snapshot_id(envelope)
    out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        print(f"已存在 (幂等跳过): {out.name}")
    else:
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        s = exported["stats"]
        print(f"已接入 {out.name}  源={exported['capture']['source_format']}  "
              f"块={s['blocks_total']}  入token={s['tokens_in']}")
    conn = db(vault)
    conn.executescript(SCHEMA)
    register_snapshot(conn, envelope, sid, out)
    conn.commit()
    if not args.keep_temp:
        tmp_out.unlink(missing_ok=True)


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
    # 自有账本卡 (零外部依赖, 人人开箱即有) — headroom 未装时仍是主视觉
    own = owned_stats(vault)
    if own["snapshots"]:
        cards.append(f'<div class="card"><div class="v">{own["tokens_in"]/(10**6):,.0f}M</div>'
                     f'<div class="k">会话累计消耗 tokens ({own["snapshots"]} 快照 · 自有)</div></div>')
        cards.append(f'<div class="card alt"><div class="v">{own["cache_hit_rate"]*100:.0f}%</div>'
                     f'<div class="k">缓存命中率 (cache读 {own["cache_read"]/(10**6):,.0f}M)</div></div>')
        top = own["by_agent"][0] if own["by_agent"] else None
        if top:
            cards.append(f'<div class="card alt"><div class="v">{top["snapshots"]}</div>'
                         f'<div class="k">{top["agent"]} 会话 · '
                         f'{(top["tokens_in"] or 0)/(10**6):,.0f}M tok</div></div>')
    if row:  # headroom 增强 (装了才有)
        total_saved = ((row["cost_usd"] or 0) + (raw.get("cache_savings_usd") or 0)) if row else 0
        cards.append(f'<div class="card"><div class="v">{usd(total_saved)}</div><div class="k">累计节省 (headroom 通路层)</div></div>')
        cards.append(f'<div class="card alt"><div class="v">{usd(raw.get("cache_savings_usd"))}</div><div class="k">缓存节省</div></div>')
    else:
        cards.append('<div class="card alt"><div class="v">—</div>'
                     '<div class="k">节省金额: 接入 headroom 解锁 (cv ledger import-headroom)</div></div>')
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
        subprocess.run(["cmd", "/c", "start", "", str(out.resolve())], shell=False, creationflags=NO_WINDOW)


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
                       capture_output=True, timeout=10, creationflags=NO_WINDOW)
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


CONSOLE_HTML = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Context Vault 控制台</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E%3Crect x='2.5' y='2.5' width='15' height='15' rx='3.5' fill='none' stroke='%232dd4a7' stroke-width='1.7'/%3E%3Crect x='6.5' y='8' width='7' height='6' rx='1' fill='%232dd4a7'/%3E%3Cpath d='M7.5 8V6.5a2.5 2.5 0 0 1 5 0V8' fill='none' stroke='%232dd4a7' stroke-width='1.7' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
:root{
  --bg0:#0b1013; --bg1:#10181c; --bg2:#162226; --bg3:#1b2a2f;
  --line:#1c2a2e; --line-soft:#162125;
  --ink:#dde9e5; --ink-2:#9fb3ad; --ink-3:#6e837f;
  --acc:#2dd4a7; --acc-ink:#0a1512; --acc-deep:#0f3b33;
  --bar:#17594a; --bar-hover:#1e6d5b;
  --warn:#e2b45c; --err:#e07862; --info:#5ca8dd;
  --mono:"Cascadia Code",Consolas,"SF Mono",monospace;
  --sans:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
  --rad:8px; --head-h:56px; --nav-h:46px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{background:var(--bg0);color:var(--ink);font:14px/1.65 var(--sans);
  font-feature-settings:"tnum";-webkit-font-smoothing:antialiased}
::selection{background:var(--acc-deep);color:var(--ink)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--bg3);border-radius:5px;border:2px solid var(--bg0)}
::-webkit-scrollbar-thumb:hover{background:#24363c}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:4px}
button{font:inherit;color:inherit}

/* ---------- 顶栏 ---------- */
header{display:flex;align-items:center;gap:14px;padding:0 28px;height:var(--head-h);
  border-bottom:1px solid var(--line);background:var(--bg0);position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15px;letter-spacing:.01em;white-space:nowrap}
.brand svg{display:block}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--ink-2);
  border:1px solid var(--line);border-radius:99px;padding:3px 11px;background:var(--bg1)}
.badge .pulse{width:7px;height:7px;border-radius:50%;background:var(--acc);
  box-shadow:0 0 0 0 rgba(45,212,167,.45);animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(45,212,167,.4)}70%{box-shadow:0 0 0 7px rgba(45,212,167,0)}100%{box-shadow:0 0 0 0 rgba(45,212,167,0)}}
.badge.err .pulse{background:var(--err);animation:none}
header .spacer{flex:1}
.privacy{font-size:12px;color:var(--ink-3);display:flex;align-items:center;gap:6px}
.privacy svg{opacity:.7}

/* ---------- 导航 (吸顶, 与表头 sticky 位对齐) ---------- */
nav{display:flex;gap:2px;padding:0 28px;border-bottom:1px solid var(--line);background:var(--bg0);
  position:sticky;top:var(--head-h);z-index:9;height:var(--nav-h)}
nav button{background:none;border:none;color:var(--ink-3);padding:0 14px;cursor:pointer;
  font-size:13.5px;position:relative;transition:color .15s ease;display:flex;align-items:center;gap:7px;
  height:var(--nav-h)}
nav button svg{opacity:.75}
nav button:hover{color:var(--ink-2)}
nav button.on{color:var(--ink);font-weight:600}
nav button.on::after{content:"";position:absolute;left:12px;right:12px;bottom:-1px;height:2px;
  background:var(--acc);border-radius:2px 2px 0 0}

main{padding:26px 28px 60px;max-width:1120px;margin:0 auto}
section{animation:fadein .18s ease-out}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
h2{font-size:12px;font-weight:600;color:var(--ink-3);letter-spacing:.08em;
  margin:26px 0 10px;text-transform:uppercase}
main > section > h2:first-child{margin-top:0}

/* ---------- 数据带 (主指标) ---------- */
.statband{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:44px;row-gap:18px;
  padding:6px 0 22px;border-bottom:1px solid var(--line-soft)}
.stat .v{font-family:var(--mono);font-size:27px;font-weight:600;letter-spacing:-.01em;
  font-variant-numeric:tabular-nums;color:var(--acc);line-height:1.2}
.stat .v.neutral{color:var(--ink)}
.stat .k{font-size:12px;color:var(--ink-3);margin-top:2px}

/* ---------- 表格 ---------- */
.tbl-wrap{width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:11.5px;font-weight:600;color:var(--ink-3);text-align:left;letter-spacing:.05em;
  text-transform:uppercase;padding:8px 12px;border-bottom:1px solid var(--line);
  background:var(--bg1);position:sticky;top:calc(var(--head-h) + var(--nav-h))}
td{padding:9px 12px;border-bottom:1px solid var(--line-soft);vertical-align:baseline}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{font-family:var(--mono);font-size:12.5px}
.linklike{background:none;border:none;padding:0;cursor:pointer;text-align:left}
.sid{color:var(--acc);font-family:var(--mono);font-size:12.5px}
.sid:hover{text-decoration:underline;text-underline-offset:3px}
.chip{display:inline-block;font-size:11px;padding:1px 8px;border-radius:5px;
  background:var(--bg2);color:var(--ink-2);border:1px solid var(--line)}
.chip.warn{color:var(--warn);border-color:#4a3b1e}
.chip.ok{color:var(--acc);border-color:#1d4a40}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px;vertical-align:1px}
.dot.ok{background:var(--acc)} .dot.err{background:var(--err)} .dot.warm{background:var(--info)} .dot.sw{background:var(--warn)}
td .sub{color:var(--ink-3);font-size:12px}

/* ---------- 月度走势 ---------- */
.months{display:flex;gap:6px;height:132px;padding:4px 0 0}
.mcol{flex:1;position:relative;min-width:0}
.mcol .stick{position:absolute;left:50%;bottom:22px;width:100%;max-width:46px;
  height:3%;max-height:calc(100% - 26px);background:var(--bar);border-radius:3px 3px 0 0;
  border-top:2px solid var(--acc);transform:translateX(-50%) scaleY(1);transform-origin:bottom;
  animation:grow .5s cubic-bezier(.2,.7,.3,1) backwards;animation-delay:calc(var(--i,0)*40ms);
  transition:background .15s ease}
@keyframes grow{from{transform:translateX(-50%) scaleY(0)}}
.mcol .stick:hover{background:var(--bar-hover)}
.mcol .lb{position:absolute;left:0;right:0;bottom:0;text-align:center;
  font-size:10.5px;color:var(--ink-3);font-variant-numeric:tabular-nums}

/* ---------- 表单与按钮 ---------- */
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
input,select{background:var(--bg1);border:1px solid var(--line);color:var(--ink);
  border-radius:var(--rad);padding:8px 12px;font-size:13.5px;outline:none;
  transition:border-color .15s ease,background .15s ease}
input::placeholder{color:var(--ink-3)}
input:focus,select:focus{border-color:var(--acc);background:var(--bg2)}
input{flex:1;min-width:260px}
.btn{display:inline-flex;align-items:center;gap:7px;background:var(--bg2);border:1px solid var(--line);
  border-radius:var(--rad);padding:8px 14px;font-size:13.5px;cursor:pointer;
  transition:border-color .15s ease,background .15s ease,transform .1s ease}
.btn:hover{border-color:#2a4046;background:var(--bg3)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-ink);font-weight:600}
.btn.primary:hover{background:#43e0b6;border-color:#43e0b6}
.btn.danger{background:none;border-color:#5a4020;color:var(--warn)}
.btn.danger:hover{background:#241b0e;border-color:var(--warn)}
.btn:disabled{opacity:.45;cursor:not-allowed}

/* ---------- 面板 ---------- */
.panel{background:var(--bg1);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin:12px 0}
.panel > b{font-size:14px}
.kv{display:grid;grid-template-columns:190px 1fr;gap:5px 14px;font-size:13px;margin-top:10px}
.kv b{color:var(--ink-3);font-weight:500}
.kv span{font-variant-numeric:tabular-nums}
.immlist{list-style:none;margin-top:12px;border-top:1px solid var(--line-soft);padding-top:10px}
.immlist li{display:flex;gap:9px;font-size:12.5px;color:var(--ink-2);padding:3.5px 0}
.immlist li svg{flex:none;margin-top:3px}
.immlist li.good{color:var(--acc)}
.note{color:var(--ink-3);font-size:12.5px;margin-top:10px;line-height:1.7}
.empty{padding:36px 20px;text-align:center;color:var(--ink-3);font-size:13px;
  border:1px dashed var(--line);border-radius:10px;line-height:2}
.empty code{font-family:var(--mono);font-size:12px;color:var(--ink-2);
  background:var(--bg1);padding:2px 7px;border-radius:5px}
.empty svg{display:block;margin:0 auto 6px}
.empty .btn{margin-top:12px}

/* ---------- 快照详情抽屉 ---------- */
.backdrop{position:fixed;inset:0;background:rgba(3,7,9,.55);opacity:0;
  transition:opacity .22s ease;z-index:40}
.backdrop.show{opacity:1}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(460px,94vw);background:var(--bg1);
  border-left:1px solid var(--line);z-index:41;overflow-y:auto;padding:20px 24px 28px;
  transform:translateX(28px);opacity:0;
  transition:transform .24s cubic-bezier(.2,.7,.3,1),opacity .2s ease;
  box-shadow:-18px 0 50px rgba(0,0,0,.45)}
.drawer.show{transform:none;opacity:1}
.dw-head{display:flex;align-items:flex-start;gap:12px}
.dw-head h3{font-size:15px;font-weight:600;margin-top:3px}
.dw-sub{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);word-break:break-all;margin:2px 0 0}
.dw-close{margin-left:auto;flex:none;background:none;border:1px solid transparent;border-radius:6px;
  color:var(--ink-3);cursor:pointer;padding:5px;display:inline-flex}
.dw-close:hover{color:var(--ink);border-color:var(--line);background:var(--bg2)}
.dw-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:14px 0 2px}
.drawer h3.sec{font-size:12px;font-weight:600;color:var(--ink-3);letter-spacing:.06em;
  text-transform:uppercase;margin:20px 0 8px}
.drawer .immlist{border-top:none;padding-top:2px}
.dw-actions{display:flex;gap:10px;margin-top:22px;padding-top:16px;border-top:1px solid var(--line-soft)}

/* ---------- 骨架 ---------- */
.skel{height:13px;border-radius:5px;background:linear-gradient(90deg,var(--bg2) 25%,var(--bg3) 50%,var(--bg2) 75%);
  background-size:200% 100%;animation:shimmer 1.3s linear infinite;margin:10px 0}
@keyframes shimmer{to{background-position:-200% 0}}

/* ---------- toast ---------- */
#toast{position:fixed;bottom:26px;right:26px;background:var(--bg2);border:1px solid var(--line);
  border-radius:10px;padding:13px 18px;max-width:440px;font-size:13px;line-height:1.6;
  box-shadow:0 10px 34px rgba(0,0,0,.55);transform:translateY(8px);opacity:0;
  transition:transform .18s ease,opacity .18s ease;pointer-events:none;z-index:50}
#toast.show{transform:none;opacity:1}
#toast.err{border-color:#5a3229;background:#1d1310}

/* ---------- 动效偏好 ---------- */
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}
}

/* ---------- 窄屏 ---------- */
@media(max-width:900px){
  header{padding:0 16px}
  .privacy{display:none}
  nav{padding:0 8px}
  main{padding:20px 16px 60px}
  .statband{column-gap:28px}
  .tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tbl-wrap table{min-width:640px}
  .tbl-wrap th{position:static}
  .kv{grid-template-columns:132px 1fr}
}
</style></head><body>
<header>
  <div class="brand">
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
      <rect x="2.5" y="2.5" width="15" height="15" rx="3.5" stroke="#2dd4a7" stroke-width="1.7"/>
      <rect x="6.5" y="8" width="7" height="6" rx="1" fill="#2dd4a7"/>
      <path d="M7.5 8V6.5a2.5 2.5 0 0 1 5 0V8" stroke="#2dd4a7" stroke-width="1.7" stroke-linecap="round"/>
    </svg>
    Context Vault
  </div>
  <span class="badge" id="status"><span class="pulse"></span>连接中</span>
  <span class="spacer"></span>
  <span class="privacy">
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
      <rect x="3" y="7" width="10" height="7" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
      <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" stroke="currentColor" stroke-width="1.4"/>
    </svg>
    本地回环 · 数据不出机器
  </span>
</header>
<nav aria-label="控制台分区">
  <button data-t="ov" class="on" aria-current="page">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 13.5h12M4 13.5V8m4 5.5V3.5m4 10V6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    概览</button>
  <button data-t="snap">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><ellipse cx="7" cy="7" rx="4.5" ry="4.5" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    快照资产</button>
  <button data-t="mig">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 8h11m0 0-3.5-3.5M13 8l-3.5 3.5M14 2.5v11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".4"/><path d="M2 8h9m0 0-3-3M11 8l-3 3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
    迁移</button>
  <button data-t="probe">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity=".45"/><circle cx="8" cy="8" r="2.6" stroke="currentColor" stroke-width="1.6"/><path d="M8 1.6V4M8 12v2.4M1.6 8H4M12 8h2.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
    哨兵</button>
</nav>
<main>
<section id="t-ov">
  <div class="statband" id="ov-band"><div class="skel" style="width:220px"></div><div class="skel" style="width:160px"></div><div class="skel" style="width:160px"></div></div>
  <h2>月度节省走势</h2><div class="months" id="ov-months"></div>
  <h2>按模型明细</h2><div id="ov-models"></div>
</section>

<section id="t-snap" style="display:none">
  <div class="controls">
    <input id="snap-q" placeholder="搜索全部历史会话 — 项目名、模型、任务关键词…" onkeydown="if(event.key==='Enter')snapSearch()">
    <button class="btn" onclick="snapSearch()">搜索</button>
    <button class="btn" onclick="loadSnaps()">最近</button>
  </div>
  <div id="snap-list"></div>
</section>

<section id="t-mig" style="display:none">
  <div class="panel">
    <b>把历史会话迁移到另一个 agent</b>
    <div class="controls" style="margin-top:14px;margin-bottom:4px">
      <select id="mig-sid" style="flex:1;min-width:260px"></select>
      <select id="mig-target"><option value="codex">→ Codex</option><option value="claude">→ Claude Code</option></select>
      <button class="btn primary" onclick="migPreview()">预览迁移</button>
    </div>
    <div id="mig-preview"></div>
    <div class="controls" id="mig-actions" style="display:none;margin-top:6px">
      <button class="btn" onclick="migGo(false)">生成到 outbox（安全）</button>
      <button class="btn danger" onclick="migGo(true)">写入目标会话目录</button>
    </div>
    <div class="note">stateless replay：压缩边界之后为活上下文，摘要将注入为新会话首条消息。<br>
    写入后用 <b>codex resume</b> / <b>claude --resume</b> 恢复会话。</div>
  </div>
</section>

<section id="t-probe" style="display:none">
  <div class="controls">
    <button class="btn primary" onclick="probeNow()">立即探测上游</button>
    <span class="note" id="probe-last" style="margin:0"></span>
  </div>
  <div id="probe-list"></div>
</section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<div class="backdrop" id="backdrop" hidden></div>
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="dw-title" hidden>
  <div class="dw-head">
    <div style="min-width:0">
      <h3 id="dw-title">快照详情</h3>
      <p class="dw-sub" id="dw-sub"></p>
    </div>
    <button class="dw-close" id="dw-close" aria-label="关闭详情" onclick="closeDrawer()">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3.5 3.5l9 9m0-9-9 9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    </button>
  </div>
  <div id="dw-body"></div>
</aside>
<script>
const $=s=>document.querySelector(s);
const usd=v=>v==null?"—":"$"+(+v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const num=v=>(+v||0).toLocaleString();
const bn=v=>{v=+v||0;return v>=1e9?(v/1e9).toFixed(2)+"B":v>=1e6?(v/1e6).toFixed(1)+"M":num(v)};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ftime=s=>{if(!s)return"—";const d=new Date(s);return isNaN(d)?String(s).slice(0,19)
  :d.toLocaleString("zh-CN",{year:"2-digit",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})};
const frel=s=>{if(!s)return"";const d=new Date(s);if(isNaN(d))return String(s).slice(0,19);
  const m=Math.round((Date.now()-d.getTime())/6e4);
  return m<1?"刚刚":m<60?m+" 分钟前":m<1440?Math.round(m/60)+" 小时前":Math.round(m/1440)+" 天前"};
const shortSid=s=>String(s||"").length>18?String(s).slice(0,12)+"…"+String(s).slice(-4):String(s||"");
let toastTimer;
function toast(m,err){const t=$("#toast");t.textContent=m;t.classList.toggle("err",!!err);t.classList.add("show");
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove("show"),5200)}
async function api(p,opt){const r=await fetch(p,opt);
  if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);return r.json()}

const WARN='<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M8 2L15 14H1L8 2Z" stroke="#e2b45c" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6.5v3.5M8 12v.5" stroke="#e2b45c" stroke-width="1.5" stroke-linecap="round"/></svg>';
const GOOD='<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.3" stroke="#2dd4a7" stroke-width="1.5"/><path d="M5.4 8.2l1.8 1.8 3.4-3.8" stroke="#2dd4a7" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const errEmpty=(msg,retry)=>'<div class="empty">'+WARN+esc(msg)+
  (retry?'<br><button class="btn" onclick="'+retry+'">重试</button>':"")+"</div>";

/* 单元格允许裸字符串或 {t,num} 对象 — 两种写法都渲染, 避免 undefined */
const cell=c=>(c&&typeof c==="object")?c:{t:c};
const table=(heads,rows)=>{if(!rows||!rows.length)return"";
  const hs=heads.map((h,i)=>{const c=cell(rows[0][i]);return "<th"+(c.num?' class="num"':"")+">"+h+"</th>"}).join("");
  const bs=rows.map(r=>"<tr>"+r.map(c0=>{const c=cell(c0);return "<td"+(c.num?' class="num"':"")+">"+c.t+"</td>"}).join("")+"</tr>").join("");
  return '<div class="tbl-wrap"><table><thead><tr>'+hs+"</tr></thead><tbody>"+bs+"</tbody></table></div>"};

function setTab(t){
  document.querySelectorAll("nav button").forEach(x=>{const on=x.dataset.t===t;
    x.classList.toggle("on",on);on?x.setAttribute("aria-current","page"):x.removeAttribute("aria-current")});
  ["ov","snap","mig","probe"].forEach(k=>$("#t-"+k).style.display=k===t?"":"none");
  if(t==="mig"&&!$("#mig-sid").options.length)fillMig();
  if(t==="probe"&&!$("#probe-list").dataset.loaded)loadProbe();
}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>setTab(b.dataset.t));

async function boot(){
  try{const h=await api("/healthz");
    $("#status").innerHTML='<span class="pulse"></span>服务正常 · '+h.snapshots+" 快照 · "+num(h.terms)+" 索引词项";
  }catch(e){const s=$("#status");s.classList.add("err");s.innerHTML="<span class='pulse'></span>服务异常"}
  loadOv();loadSnaps();
}
const stat=(v,k,cls="")=>'<div class="stat"><div class="v '+cls+'">'+v+'</div><div class="k">'+k+"</div></div>";
const kv=(k,v)=>"<b>"+k+"</b><span>"+(v==null?"—":v)+"</span>";

async function loadOv(){
  try{const d=await api("/ledger");const lt=d.lifetime||{};const ow=d.owned||{};
    const hasLt=(lt.saved_usd||0)+(lt.cache_savings_usd||0)>0;
    let band="";
    if(ow.snapshots){ /* vault 自有账本: 零外部依赖, 人人开箱即有 */
      band+=stat(bn(ow.tokens_in),"会话累计消耗 tokens（"+num(ow.snapshots)+" 快照 · 自有）")+
        stat((ow.cache_hit_rate*100).toFixed(0)+"%","缓存命中率（cache 读 "+bn(ow.cache_read)+"）","neutral");
      const top=(ow.by_agent||[])[0];
      if(top)band+=stat(num(top.snapshots),esc(top.agent)+" 会话 · "+bn(top.tokens_in||0)+" tok","neutral");
    }
    if(hasLt){ /* headroom 增强: 装了才解锁 */
      const total=(lt.saved_usd||0)+(lt.cache_savings_usd||0);
      band+=stat(usd(total),"累计节省（headroom 通路层）")+
        stat(usd(lt.cache_savings_usd),"缓存节省","neutral");
    }else{
      band+=stat("—","节省金额：接入 headroom 解锁（cv ledger import-headroom）","neutral");
    }
    $("#ov-band").innerHTML=band;
    const ms=d.monthly||[];
    if(!ms.length)$("#ov-months").innerHTML='<div class="empty">暂无月度数据<br>导入账本 <code>cv ledger import-headroom</code></div>';
    else{
      const mx=Math.max(...ms.map(m=>m.c||0),1);
      $("#ov-months").innerHTML=ms.map((m,i)=>'<div class="mcol"><div class="stick" style="height:'+
        Math.max(100*(m.c||0)/mx,3).toFixed(2)+'%;--i:'+i+'" title="'+esc(m.m+" · "+usd(m.c))+
        '"></div><div class="lb">'+esc(m.m.slice(2))+"</div></div>").join("");
    }
    $("#ov-models").innerHTML=table(["模型","事件","节省 tokens","节省金额"],
      (d.by_model||[]).map(m=>["<span class='mono'>"+esc(m.model||"未知模型")+"</span>",
        {t:num(m.n),num:1},{t:num(m.t),num:1},{t:usd(m.c),num:1}]))
      ||'<div class="empty">账本明细为空</div>';
  }catch(e){
    $("#ov-band").innerHTML=errEmpty("账本加载失败："+e.message,"loadOv()");
    $("#ov-months").innerHTML="";$("#ov-models").innerHTML="";
  }
}

const sidBtn=sid=>"<button class='linklike sid' title=\""+sid+"\" onclick=\"snapDetail('"+sid+"')\">"+shortSid(sid)+"</button>";
const agentCell=r=>"<span class='chip'>"+esc(r.agent||"?")+"</span>";
const whenCell=r=>"<span class='sub'>"+ftime(r.created_at)+"<br>"+esc(r.cwd||"—")+"</span>";
const compCell=r=>r.completeness&&r.completeness!=="full"
  ?"<span class='chip warn'>"+esc(r.completeness)+"</span>"
  :"<span style='color:var(--ink-3)'>完整</span>";

async function loadSnaps(){
  try{
    const rows=await api("/recent?limit=30");
    $("#snap-list").innerHTML=table(["快照","来源","块数","入 tokens","完整性","时间 / 目录"],
      rows.map(r=>[sidBtn(r.sid),agentCell(r),{t:num(r.blocks),num:1},{t:bn(r.tokens_in),num:1},
        compCell(r),whenCell(r)]))
      ||'<div class="empty">vault 还是空的<br>导出第一个会话 <code>python tools/cv.py export ~/.codex/sessions/…/rollout-*.jsonl</code></div>';
    fillMig(rows);
  }catch(e){$("#snap-list").innerHTML=errEmpty("快照列表加载失败："+e.message,"loadSnaps()")}
}
async function snapSearch(){
  const q=$("#snap-q").value.trim();if(!q)return loadSnaps();
  $("#snap-list").innerHTML='<div class="skel" style="width:40%"></div><div class="skel" style="width:70%"></div>';
  try{
    const d=await api("/search?q="+encodeURIComponent(q)+"&limit=20");
    $("#snap-list").innerHTML=table(["快照","来源","得分","时间 / 目录"],
      d.hits.map(h=>[sidBtn(h.sid),agentCell(h),{t:num(h.score),num:1},whenCell(h)]))
      ||'<div class="empty">没有命中「'+esc(q)+'」<br>换个关键词试试 — 项目名、文件路径、任务描述都行</div>';
  }catch(e){$("#snap-list").innerHTML=errEmpty("搜索失败："+e.message,"snapSearch()")}
}

/* ---------- 快照详情抽屉 ---------- */
let drawerOpen=false,lastFocus=null;
function openDrawer(){
  if(drawerOpen)return;drawerOpen=true;lastFocus=document.activeElement;
  const d=$("#drawer"),b=$("#backdrop");d.hidden=false;b.hidden=false;
  void d.offsetHeight; /* 同步 reflow 触发过渡, 不依赖 rAF (隐藏页 rAF 不触发) */
  d.classList.add("show");b.classList.add("show");
  $("#dw-close").focus();
}
function closeDrawer(){
  if(!drawerOpen)return;drawerOpen=false;
  const d=$("#drawer"),b=$("#backdrop");
  d.classList.remove("show");b.classList.remove("show");
  setTimeout(()=>{d.hidden=true;b.hidden=true},240);
  if(lastFocus&&lastFocus.focus)lastFocus.focus();
}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer()});
$("#backdrop").addEventListener("click",closeDrawer);

async function snapDetail(sid){
  openDrawer();
  $("#dw-sub").textContent=sid;
  $("#dw-body").innerHTML='<div class="skel" style="width:55%"></div><div class="skel" style="width:82%"></div><div class="skel" style="width:64%"></div><div class="skel" style="width:76%"></div>';
  try{
    const [d,p]=await Promise.all([api("/snapshot?id="+sid),api("/preview?id="+sid).catch(()=>null)]);
    const c=d.capture||{},s=d.stats||{};
    const imm=[];
    if(p){
      if(p.no_assistant)imm.push("assistant 文本缺失（源转录不含）— 目标会话只有 user / tool 视角");
      if(p.thinking)imm.push("thinking ×"+p.thinking+" — 仅明文摘要可迁移，加密推理不可移植");
      if(p.agent_messages)imm.push("agent_message ×"+p.agent_messages+" — 降级为带标注的文本");
      if(p.dropped_source)imm.push("源文件被丢弃行 ×"+p.dropped_source+"（developer 注入 / 无移植语义）");
    }
    const dist=Object.entries(d.block_dist||{}).map(([k,v])=>"<span class='chip'>"+esc(k)+" ×"+num(v)+"</span>").join(" ");
    $("#dw-body").innerHTML=
      "<div class='dw-chips'>"+
      (c.agent?"<span class='chip'>"+esc(c.agent)+"</span>":"")+
      (c.completeness&&c.completeness!=="full"?"<span class='chip warn'>"+esc(c.completeness)+"</span>":"<span class='chip ok'>完整</span>")+
      "<span class='sub'>"+ftime(c.created_at)+"</span></div>"+
      "<div class='kv'>"+
      kv("项目目录",esc(c.cwd||"—"))+
      (p?kv("活上下文",num(p.live_blocks)+" 块 · ~"+num(p.live_tokens_est)+" tokens"+(p.compactions?"（"+p.compactions+" 次压缩边界之后）":"")):"")+
      (p?kv("历史累计输入",num(p.history_tokens_in)+" tokens"):"")+
      kv("块总数",num(s.blocks_total)+" 块"+(s.blocks_dropped?" · 丢弃 "+num(s.blocks_dropped):""))+
      "</div>"+
      "<h3 class='sec'>块构成</h3><div class='dw-chips'>"+(dist||"<span class='sub'>—</span>")+"</div>"+
      (p?"<h3 class='sec'>可迁移性</h3><ul class='immlist'>"+
        (imm.length?imm.map(i=>"<li>"+WARN+i+"</li>").join(""):"<li class='good'>"+GOOD+"无可迁移性损失</li>")+"</ul>":"")+
      "<div class='dw-actions'>"+
      "<button class='btn primary' onclick=\"toMig('"+sid+"')\">迁移此快照 →</button>"+
      "<button class='btn' onclick=\"copySid('"+sid+"')\">复制 SID</button></div>";
  }catch(e){$("#dw-body").innerHTML=errEmpty("详情加载失败："+e.message,"snapDetail('"+sid+"')")}
}
function copySid(sid){navigator.clipboard&&navigator.clipboard.writeText(sid).then(
  ()=>toast("已复制完整 SID")).catch(()=>toast("复制失败",1))}
async function toMig(sid){
  closeDrawer();setTab("mig");
  if(!$("#mig-sid").options.length)await fillMig();
  const has=[...$("#mig-sid").options].some(o=>o.value===sid);
  if(has)$("#mig-sid").value=sid;
  migPreview();
}

async function fillMig(rows){
  if(!rows)rows=await api("/recent?limit=50").catch(()=>[]);
  $("#mig-sid").innerHTML=rows.map(r=>'<option value="'+r.sid+'">'+esc(shortSid(r.sid))+" · "+
    esc(r.agent||"?")+" · "+num(r.blocks)+" 块 · "+ftime(r.created_at)+"</option>").join("");
}
async function migPreview(){
  const id=$("#mig-sid").value;if(!id)return toast("vault 为空，无快照可迁移",1);
  try{
    const p=await api("/preview?id="+id);
    const imm=[];
    if(p.no_assistant)imm.push("assistant 文本缺失（源转录不含）— 目标会话只有 user / tool 视角");
    if(p.thinking)imm.push("thinking ×"+p.thinking+" — 仅明文摘要可迁移，加密推理不可移植");
    if(p.agent_messages)imm.push("agent_message ×"+p.agent_messages+" — 降级为带标注的文本");
    if(p.dropped_source)imm.push("源文件被丢弃行 ×"+p.dropped_source+"（developer 注入 / 无移植语义）");
    $("#mig-preview").innerHTML=
      '<div class="kv">'+
      kv("活上下文",p.live_blocks+" 块 · ~"+num(p.live_tokens_est)+" tokens"+(p.compactions?"（"+p.compactions+" 次压缩边界之后）":""))+
      kv("构成",Object.entries(p.live_dist).map(([k,v])=>esc(k)+" ×"+num(v)).join("　"))+
      kv("历史累计输入",num(p.history_tokens_in)+" tokens")+"</div>"+
      '<ul class="immlist">'+(imm.length?imm.map(i=>"<li>"+WARN+i+"</li>").join(""):"<li class='good'>"+GOOD+"无可迁移性损失</li>")+"</ul>";
    $("#mig-actions").style.display="flex";
  }catch(e){$("#mig-preview").innerHTML=errEmpty("预览失败："+e.message);$("#mig-actions").style.display="none"}
}
async function migGo(apply){
  const id=$("#mig-sid").value,t=$("#mig-target").value;
  if(apply&&!confirm("确认写入目标会话目录？\n\n"+id+"\n→ "+(t==="codex"?"Codex":"Claude Code")+
    "\n\n目标 agent 的 resume 列表将出现此会话。"))return;
  const btns=document.querySelectorAll("#mig-actions .btn");btns.forEach(b=>b.disabled=true);
  try{const r=await api("/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id,target:t,apply:apply})});
    const name=r.dest.split("\\").pop().split("/").pop();
    toast((r.applied?"已写入会话目录：":"已生成到 outbox：")+name+"（"+num(r.lines)+" 行"+
      (r.dropped?"，丢弃 "+r.dropped:"")+"）"+(r.new_sid?" · session "+r.new_sid.slice(0,8)+"…":""));
  }catch(e){toast("迁移失败："+e.message,1)}
  btns.forEach(b=>b.disabled=false);
}

async function loadProbe(){
  const el=$("#probe-list");el.innerHTML='<div class="skel" style="width:60%"></div><div class="skel" style="width:45%"></div>';
  try{
    const rows=await api("/probe_events?limit=100");
    $("#probe-last").textContent=rows.length?"最近事件 · "+frel(rows[0].ts):"";
    const cls=s=>s==="ok"?"ok":(s==="warm"||s==="probe"?"warm":(s==="switch"?"sw":"err"));
    el.innerHTML=table(["","时间","profile","状态","详情"],
      rows.map(r=>["<span class='dot "+cls(r.status)+"'></span>",
        "<span class='mono'>"+ftime(r.ts)+"</span>","<b style='font-weight:500'>"+esc(r.profile)+"</b>",
        esc(r.status)+(r.http_code?" · "+r.http_code:"")+(r.latency_ms?" · "+r.latency_ms+"ms":""),
        r.missing_models&&r.missing_models!=="[]"?"<span style='color:var(--err)'>消失 "+esc(r.missing_models)+"</span>":
          (r.status==="switch"?("<span class='sub'>切换检测 · "+shortModels(r.models)+"</span>"):"")]))
      ||'<div class="empty">暂无事件<br>点「立即探测上游」或等哨兵周期（托盘模式下每 5 分钟自动探测）</div>';
    el.dataset.loaded=1;
  }catch(e){el.innerHTML=errEmpty("哨兵事件加载失败："+e.message,"loadProbe()")}
}
const shortModels=s=>{try{const m=JSON.parse(s);return m.by_agent?Object.entries(m.by_agent).map(([k,v])=>k+"×"+v).join(" · ")+" 活跃会话":""}catch(e){return""}};
async function probeNow(){
  const b=document.querySelector("#t-probe .btn.primary");if(b)b.disabled=true;
  toast("探测中…");
  try{const r=await api("/probe",{method:"POST"});
    const bad=r.alerts&&r.alerts.length;
    toast(bad?"发现异常："+r.alerts.map(a=>"["+a.profile+"] "+(a.kind==="model_missing"?"模型消失 "+a.detail:a.kind)).join("；"):"探测完成：全部上游正常",bad);
    delete $("#probe-list").dataset.loaded;loadProbe();
  }catch(e){toast("探测失败："+e.message,1)}
  if(b)b.disabled=false;
}
boot();setInterval(()=>{if($("#t-probe").style.display!=="none"){delete $("#probe-list").dataset.loaded;loadProbe()}},30000);
</script></body></html>"""


class _KeyStub:
    key = None


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
                if u.path == "/":
                    body = CONSOLE_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path == "/healthz":
                    return self._json({"service": "context-vault", "status": "ok",
                                       "snapshots": len(idx.envs), "terms": len(idx.index)})
                if u.path == "/audit":
                    return self._json(audit_summary_and_timeline(
                        vault, q.get("since", ""), "all"))
                if u.path == "/memory":
                    return self._json({"query": q.get("q", ""),
                                       "memories": memory_search_api(vault, q.get("q", ""),
                                                                     int(q.get("limit", 8)))})
                if u.path == "/probe_events":
                    conn = db(vault)
                    rows = conn.execute("SELECT ts,profile,status,http_code,latency_ms,models,missing_models"
                                        " FROM probe_events ORDER BY id DESC LIMIT ?",
                                        (int(q.get("limit", 100)),)).fetchall()
                    return self._json([dict(r) for r in rows])
                if u.path == "/ledger":
                    conn = db(vault)
                    lt = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events"
                                      " WHERE source='headroom-lifetime'").fetchone()
                    by_model = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c"
                                            " FROM ledger_events WHERE kind='compression'"
                                            " GROUP BY model ORDER BY c DESC").fetchall()
                    monthly = conn.execute("SELECT substr(occurred_at,1,7) m, SUM(cost_usd) c"
                                           " FROM ledger_events WHERE kind='compression'"
                                           " GROUP BY m ORDER BY m").fetchall()
                    raw = json.loads(lt["raw"]) if lt else {}
                    return self._json({"lifetime": {"tokens_saved": lt["tokens_saved"] if lt else 0,
                                                    "saved_usd": lt["cost_usd"] if lt else 0, **raw},
                                       "by_model": [dict(r) for r in by_model],
                                       "monthly": [dict(r) for r in monthly],
                                       "owned": owned_stats(vault)})
                if u.path == "/preview":
                    env2 = idx.envs.get(q.get("id", ""))
                    if not env2:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    blocks = env2["conversation"]
                    dist = dict(Counter(b["type"] for b in blocks))
                    live, comp = live_context(blocks)
                    live_dist = dict(Counter(b["type"] for b in live))
                    s = env2["stats"]
                    return self._json({"id": q.get("id"), "agent": env2["capture"].get("agent"),
                                       "completeness": env2["capture"].get("completeness"),
                                       "cwd": env2["capture"].get("cwd"),
                                       "block_dist": dist, "live_blocks": len(live),
                                       "live_dist": live_dist, "compactions": dist.get("compaction", 0),
                                       "live_tokens_est": estimate_tokens(live),
                                       "history_tokens_in": s["tokens_in"],
                                       "thinking": dist.get("thinking", 0),
                                       "agent_messages": dist.get("agent_message", 0),
                                       "dropped_source": s["blocks_dropped"],
                                       "no_assistant": env2["capture"].get("completeness") == "no_assistant_text"})
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
                    return self._json({"stats": ledger_summary(vault), "owned": owned_stats(vault)})
                if u.path == "/snapshot":
                    env = idx.envs.get(q.get("id", ""))
                    if not env:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    dist = dict(Counter(b["type"] for b in env["conversation"]))
                    return self._json({"id": q.get("id"), "capture": env["capture"],
                                       "stats": env["stats"], "block_dist": dist})
                self._json({"error": "unknown route",
                            "routes": ["/", "/healthz", "/search?q=", "/context?q=", "/preview?id=",
                                       "/recent", "/stats", "/snapshot?id=", "/ledger", "/probe_events",
                                       "POST /migrate", "POST /probe"]}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            import urllib.parse as _up
            u = _up.urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                if u.path == "/migrate":
                    r = do_import(vault, body.get("id", ""), body.get("target", "codex"),
                                  bool(body.get("apply")))
                    return self._json(r)
                if u.path == "/probe":
                    baseline_p = vault / "probe_baseline.json"
                    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
                    alerts = probe_once(vault, load_profiles(), resolve_key(_KeyStub()), baseline,
                                        verbose=False)
                    baseline_p.write_text(json.dumps(baseline, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
                    return self._json({"alerts": alerts})
                self._json({"error": "unknown route"}, 404)
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
    ap.add_argument("--version", action="version", version=f"context-vault {VERSION}")
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

    sy = sub.add_parser("sync", help="自动入库: 扫描全部本地会话源, 增量导出 (mtime 水位 + lineage)")
    sy.add_argument("--root", action="append", help="额外扫描目录 (可多次)")
    sy.set_defaults(func=cmd_sync)

    fk = sub.add_parser("fork", help="在快照节点开分支 (lineage.fork_of)")
    fk.add_argument("snapshot_id")
    fk.add_argument("--message", help="分叉备注 (写入 capture.fork_note)")
    fk.set_defaults(func=cmd_fork)
    tr = sub.add_parser("tree", help="查看快照谱系 (parent 演进链 + fork 子树)")
    tr.add_argument("snapshot_id")
    tr.set_defaults(func=cmd_tree)
    au = sub.add_parser("audit", help="审计时间线回放 (探测/切换/模型消失 全留痕)")
    au.add_argument("--since", help="起始时间 (ISO 前缀)")
    au.add_argument("--kind", default="all", help="过滤: all|ok|switch|warm|model_missing")
    au.add_argument("--days", type=int, default=7)
    au.set_defaults(func=cmd_audit)

    mem = sub.add_parser("memory", help="M3 记忆层: 从快照提取跨会话记忆")
    msub = mem.add_subparsers(dest="memory_cmd", required=True)
    me = msub.add_parser("extract", help="提取决策/摘要/偏好记忆 (幂等, 重提先删旧)")
    me.add_argument("--snapshot", help="只处理单个快照")
    me.add_argument("--engine", help="记忆引擎 (默认 rule; 或 module:Class 动态加载)")
    me.set_defaults(func=cmd_memory)
    ms = msub.add_parser("search", help="检索记忆")
    ms.add_argument("query")
    ms.add_argument("--limit", type=int, default=8)
    ms.set_defaults(func=cmd_memory)
    ml = msub.add_parser("list", help="最近记忆")
    ml.add_argument("--limit", type=int, default=20)
    ml.set_defaults(func=cmd_memory)

    mc = sub.add_parser("mcp", help="MCP 记忆后端一键接入客户端")
    msub = mc.add_subparsers(dest="mcp_cmd", required=True)
    mi = msub.add_parser("install")
    mi.add_argument("--client", choices=["claude", "codex", "cursor"], default="claude")
    mi.add_argument("--all", action="store_true", help="接入全部已安装的客户端")
    mi.set_defaults(func=cmd_mcp)
    msub.add_parser("status").set_defaults(func=cmd_mcp)

    ig = sub.add_parser("ingest", help="接入任意 harness 会话 (经 smigrate, 17+ 种)")
    ig.add_argument("source", help="会话文件 / 导出 bundle (opencode/cursor/qwen/kimi/grok/...)")
    ig.add_argument("--format", help="源格式覆盖 (歧义时, 如 opencode vs kilo)")
    ig.add_argument("--cwd", help="覆盖会话 cwd")
    ig.add_argument("--drop-developer", action="store_true")
    ig.add_argument("--keep-temp", action="store_true", help="保留 claude 中转文件 (调试)")
    ig.set_defaults(func=cmd_ingest)

    lg = sub.add_parser("ledger", help="token 账本")
    lsub = lg.add_subparsers(dest="ledger_cmd", required=True)
    lsub.add_parser("import-headroom").set_defaults(func=cmd_ledger_import_headroom)
    lz = lsub.add_parser("import-zcode", help="Zcode 用量入账 (model-io 审计, requestId 幂等)")
    lz.set_defaults(func=cmd_ledger_import_zcode)
    lr = lsub.add_parser("import-relay", help="直连 relay 拉账 (billing API 或 new-api web 日志)")
    lr.add_argument("--url", required=True, help="relay base url (如 https://api.example.com)")
    lr.add_argument("--mode", choices=["billing", "web"], default="billing",
                    help="billing=OpenAI兼容端点; web=new-api 系消费日志 (系统访问令牌)")
    lr.add_argument("--token", help="web 模式: 系统访问令牌")
    lr.add_argument("--user-id", help="web 模式: new-api 用户 id (New-Api-User 头)")
    lr.add_argument("--log-path", default="/api/log/self", help="web 模式: 日志端点路径覆盖")
    lr.add_argument("--start")
    lr.add_argument("--end")
    lr.add_argument("--days", type=int, default=30)
    lr.add_argument("--key")
    lr.set_defaults(func=cmd_ledger_import_relay)
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
