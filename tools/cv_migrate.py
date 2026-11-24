"""迁移保真度预览、跨端导入 (Codex/Claude/Antigravity/ZCode/DSH/Markdown) 与五槽位交接压缩简报。"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


from cv_core import cv_mod as _cv, estimate_text_tokens  # noqa: E402


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
    env = _cv().load_snapshot(args, args.snapshot_id)
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

def _deterministic_session_id(env: dict) -> str:
    """同一源会话 (agent+session_ref) 恒定派生 uuid4 格式 id。
    重复导出/会话演进后重导出 = 目标端同名覆盖 (原位更新), 不再产生并排副本;
    uuid5 摘要改写版本位为 4 — smigrate/_require_uuid4 与各目标格式都强校验 uuid4。"""
    ref = (env.get("capture") or {}).get("session_ref") or env.get("_sid") or _cv().snapshot_id(env)
    agent = (env.get("capture") or {}).get("agent") or "unknown"
    b = bytearray(uuid.uuid5(uuid.NAMESPACE_URL, f"context-vault:export:{agent}:{ref}").bytes)
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def blocks_to_codex(env: dict, strip_thinking: bool, session_id: str | None = None) -> tuple:
    """生成 Codex rollout 行 (结构对齐本机实测 codex-cli 0.153.4, 见 snapshot-format.md 附录 A)。
    返回 (lines, dropped, session_id, filename)。两个硬约束 (实测逆向):
    ① 文件名必须含 session UUID — resume 按文件名 rollout-*-<uuid>.jsonl 定位;
    ② 生命周期事件链 (task_started/…/task_complete) 缺失时 thread-store 判 rollout 为空。
    兼容性枚举字段 (source/originator/cli_version) 用合法值; 迁移来源在首条 user 消息声明。
    session_id 缺省随机; 传入确定性 id 时文件名时间戳也从源会话首块取, 路径级幂等。"""
    sid = session_id or str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    cwd = env["capture"].get("cwd") or str(Path.cwd())
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    dropped = 0
    fname_ts = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if session_id:  # 确定性导出: 文件名也要确定, 否则同 id 不同名 = resume 歧义
        first_ts = next((b.get("ts") for b in env["conversation"] if b.get("ts")), None)
        try:
            fname_ts = dt.datetime.fromisoformat(
                str(first_ts).replace("Z", "+00:00")).strftime("%Y-%m-%dT%H-%M-%S")
        except (ValueError, AttributeError):
            pass
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
            last_assistant = b["content"]  # task_complete.last_agent_message 与 text 分支同权 (B05 同源)
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


def blocks_to_claude(env: dict, strip_thinking: bool, session_id: str | None = None) -> tuple:
    """生成 Claude Code 2.x 会话文件 (~/.claude/projects/<encoded-cwd>/<uuid>.jsonl)。
    行结构按本机实测 claude 2.1.181 转录对齐 (2026-10-27 逆向验证)。
    返回 (lines, dropped)。session_id 缺省随机, 传入确定性 id 供原位更新。"""
    session = session_id or str(uuid.uuid4())
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
                 "tool_use_id": b["call_id"], "content": b["content"],
                 "is_error": bool(b.get("is_error"))}]})
        elif bt == "agent_message":
            mid = "msg_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:12]
            emit("assistant", {"id": mid, "type": "message", "role": "assistant", "model": model,
                 "content": [{"type": "text",
                              "text": f"[{b.get('author')} → {b.get('recipient')}] {b['content']}"}],
                 "stop_reason": "end_turn"})
    return lines, dropped


def claude_project_dir(cwd: str) -> Path:
    # 实测 claude 2.1.181: C:\Users\x → C--Users-x (':' 与 '\' 均替换为 '-')
    return _cv().CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9-]", "-", cwd.replace("\\", "/"))


def _zcode_project_id(cwd: str) -> str:
    """实测规律: C:\\Users\\ngh\\.zcode\\workspace\\default → proj_c-users-ngh-.zcode-workspace-default
    (小写; ':' 与 '\\' → '-'; '.' 保留)。"""
    slug = (cwd or "").lower().replace("/", "\\").replace(":", "-").replace("\\", "-").strip("-")
    return "proj_" + re.sub(r"-+", "-", slug)


def blocks_to_zcode(env: dict, strip_thinking: bool, session_id: str | None = None) -> dict:
    """生成 ZCode 主库 (db.sqlite session/message/part) 行组 — export_zcode_session 的逆变换。
    结构按真库样本 (2026-11-16): message.data{role,time,modelId} + part.data{type} =
    text/reasoning/tool{callID,tool,state{status,input,output[,error]}}。
    返回 {session: {...}, messages: [...], parts: [...]}; id 由确定性 session_id 派生 → 重导幂等。"""
    sess_id = session_id or str(uuid.uuid4())
    cwd = env["capture"].get("cwd") or ""
    model = env.get("capture", {}).get("model") or None
    rng = _cv()._session_epoch_range(env)
    t0 = int(rng[0] * 1000) if rng else int(time.time() * 1000)
    t1 = int(rng[1] * 1000) if rng else t0
    messages, parts = [], []

    def msg(role, ms):
        data = {"role": role, "time": {"created": ms}}
        if role == "assistant":
            data["time"]["completed"] = ms
            if model:
                data["modelId"] = model
        mid = f"msg_cvimp_{uuid.uuid4().hex[:8]}_{len(messages):04d}"
        if messages:
            data["parentID"] = messages[-1]["id"]
        messages.append({"id": mid, "session_id": sess_id, "sequence": len(messages),
                         "time_created": ms, "time_updated": ms, "data": data})
        return messages[-1]

    def part(m, data):
        parts.append({"id": f"prt_cvimp_{uuid.uuid4().hex[:8]}_{len(parts):04d}",
                      "message_id": m["id"], "session_id": sess_id,
                      "time_created": m["time_created"], "time_updated": m["time_created"],
                      "sequence": len([p for p in parts if p["message_id"] == m["id"]]),
                      "data": data})

    live, comp_summary = live_context(env["conversation"])
    mspan = max(1, (t1 - t0) // (len(live) + 1))
    cur_ms = t0
    if comp_summary:  # 与 codex/claude 导出同语义: 压缩边界前内容以摘要垫底
        m = msg("user", cur_ms)
        part(m, {"type": "text", "text":
                 f"[context-vault 迁移摘要] 此前会话经过压缩, 摘要如下:\n\n{comp_summary}"})
    for b in live:
        cur_ms = min(cur_ms + mspan, t1)
        bt = b["type"]
        if bt == "text":
            role = b.get("role") or "assistant"
            if role == "developer":
                continue  # spec §3.1 裁决: developer 注入不迁移
            m = msg("user" if role == "user" else "assistant", cur_ms)
            part(m, {"type": "text", "text": b["content"]})
        elif bt == "thinking" and not strip_thinking:
            if messages and messages[-1]["data"]["role"] == "assistant":
                part(messages[-1], {"type": "reasoning", "text": b["summary"]})
            else:
                m = msg("assistant", cur_ms)
                part(m, {"type": "reasoning", "text": b["summary"]})
        elif bt == "tool_call":
            m = msg("assistant", cur_ms)
            part(m, {"type": "tool", "callID": b.get("call_id"), "tool": b.get("name"),
                     "state": {"status": "completed", "input": b.get("arguments", {})}})
        elif bt == "tool_result":
            if not parts or parts[-1]["data"].get("type") != "tool" \
                    or parts[-1]["data"].get("callID") != b.get("call_id"):
                continue  # 无调用可挂 (与 claude/codex 导出同丢弃口径)
            st = {"status": "error" if b.get("is_error") else "completed",
                  "input": {}, "output": b["content"]}
            if b.get("is_error"):
                st["error"] = str(b["content"])[:300]
            parts[-1]["data"]["state"].update(st)
        elif bt == "agent_message":
            m = msg("assistant", cur_ms)
            part(m, {"type": "text", "text":
                     f"[{b.get('author')} → {b.get('recipient')}] {b['content']}"})
    title = (env.get("capture") or {}).get("originator") \
        or _cv()._antigravity_first_user_text(env) or "(导入会话)"
    return {"session": {"id": sess_id, "project_id": _zcode_project_id(cwd) if cwd else None,
                        "slug": sess_id, "directory": cwd, "path": cwd, "title": title[:120],
                        "time_created": t0, "time_updated": t1},
            "messages": messages, "parts": parts}


def do_import_zcode(vault: Path, env: dict, sid: str, apply: bool,
                    session_id: str | None = None, db_path: Path | None = None,
                    skip_backup: bool = False) -> dict:
    """写回 ZCode 主库 (实验性): 默认 dry-run 出行数预览; --apply 先备份再事务写入。
    会话 id 确定性派生 → 重导 = 同会话原位更新 (messages/parts 重写)。
    ZCode 运行中内存有缓存, 写入后需重启 ZCode 才能在会话列表看到。"""
    sess = blocks_to_zcode(env, False, session_id=session_id)
    sess_id = sess["session"]["id"]
    dest = str(db_path or _cv().ZCODE_DB)
    if not apply:
        preview = vault / "outbox" / f"zcode-import-{sid[:14]}.json"
        _cv().atomic_write_text(preview, json.dumps(
            {"session_id": sess_id, "dest": dest, "title": sess["session"]["title"],
             "messages": len(sess["messages"]), "parts": len(sess["parts"]),
             "cwd": sess["session"]["directory"]}, ensure_ascii=False, indent=1))
        return {"lines": len(sess["messages"]), "dropped": 0, "dest": str(preview),
                "new_sid": sess_id, "applied": False}
    zdb = Path(dest)
    if not zdb.exists():
        raise _cv().BadMigrateTarget(f"ZCode 主库不存在: {zdb}")
    backup_note = ""
    if not skip_backup:
        backup_dir = vault / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"zcode-import-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.sqlite"
        shutil.copy2(zdb, backup)
        backup_note = f"备份 {backup.name}; "
    conn = sqlite3.connect(zdb, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        ver = conn.execute("SELECT version FROM session ORDER BY time_created LIMIT 1").fetchone()
        srow = dict(sess["session"])
        srow["version"] = ver[0] if ver and ver[0] else "0.17.1"
        srow["task_type"] = "interactive"
        srow["title_source"] = "first_input"  # NOT NULL (真库 schema), 默认值即此
        srow["permission"] = json.dumps({"mode": "yolo"})
        # 目标库可能是更老的 schema: 只写双方都有的列, NOT NULL 缺口填默认
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session)")]
        notnull = {r[1] for r in conn.execute("PRAGMA table_info(session)") if r[3]}
        for c in notnull - set(srow) - set(("project_id", "slug", "directory", "title",
                                            "version", "time_created", "time_updated")):
            dflt = next((r[4] for r in conn.execute("PRAGMA table_info(session)") if r[1] == c), None)
            srow[c] = dflt
        cur = conn.execute("SELECT 1 FROM session WHERE id=?", (sess_id,)).fetchone()
        if cur:  # 原位更新: 清旧内容再写新版
            conn.execute("DELETE FROM part WHERE session_id=?", (sess_id,))
            conn.execute("DELETE FROM message WHERE session_id=?", (sess_id,))
            set_clause = ",".join(f"{c}=?" for c in cols)
            conn.execute(f"UPDATE session SET {set_clause} WHERE id=?",
                         (*[srow.get(c) for c in cols], sess_id))
        else:
            ins = ",".join(c for c in cols)
            qs = ",".join("?" for _ in cols)
            conn.execute(f"INSERT INTO session ({ins}) VALUES ({qs})",
                         (*[srow.get(c) for c in cols],))
        mcols = [r[1] for r in conn.execute("PRAGMA table_info(message)")]
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(part)")]
        for m in sess["messages"]:
            mrow = dict(m)
            mrow["data"] = json.dumps(m["data"], ensure_ascii=False)
            conn.execute(f"INSERT INTO message ({','.join(mcols)}) VALUES ({','.join('?' * len(mcols))})",
                         (*[mrow.get(c) for c in mcols],))
        for p in sess["parts"]:
            prow = dict(p)
            prow["data"] = json.dumps(p["data"], ensure_ascii=False)
            conn.execute(f"INSERT INTO part ({','.join(pcols)}) VALUES ({','.join('?' * len(pcols))})",
                         (*[prow.get(c) for c in pcols],))
        ok = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        if ok != "ok":
            raise _cv().BadMigrateTarget(f"写入后完整性检查失败: {ok} (备份: {backup.name})")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_migration(vault, zdb, sid, baseline=None, target="zcode")
    return {"lines": len(sess["messages"]), "dropped": 0,
            "dest": f"{dest} ({backup_note}重启 ZCode 后生效)",
            "new_sid": sess_id, "applied": True}


# estimate_text_tokens 已收敛至 cv_core (顶部导入并向后兼容重导出)


_BRIEF_LINE_CAP = 220   # 槽位单行截断 (v0 同款视觉)


def _brief_stop_point(blocks: list) -> str:
    """停点 = 最后一条 assistant 文本 / agent_message (2026-11-02 审查 #5 契约, 两类都认)。"""
    for b in reversed(blocks):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "agent_message" or (b.get("type") == "text" and b.get("role") == "assistant"):
            txt = (b.get("content") or "").strip()
            if txt:
                return txt[:2000] + ("…[截断]" if len(txt) > 2000 else "")
    return "(会话中无 assistant 文本输出)"


def compress_brief(env: dict, budget_tokens: int | None = None,
                   max_chars: int = 48 * 1024) -> tuple:
    """预算受限交接简报装配器 (W3): 显式 token 预算下的效用最大化 —
    槽位优先级 ① 任务目标与最终停靠态 (必保) ② 关键架构裁决 ③ 已证伪死路与踩坑
    ④ 触碰文件与工具拓扑 ⑤ 活上下文尾部。同文件多次读取折叠为 ×N 末次态,
    重复报错去重计数, 长回显只留锚点行。返回 (markdown, stats 元数据)。"""
    from collections import Counter
    from cv_memory import CONSTRAINT_MARKERS, DECISION_MARKERS, PITFALL_MARKERS
    c, s = env.get("capture") or {}, env.get("stats") or {}
    blocks = env.get("conversation") or []
    dist = Counter(b.get("type") for b in blocks if isinstance(b, dict))
    budget = max(300, int(budget_tokens)) if budget_tokens else max_chars // 2
    orig_tokens = estimate_tokens(blocks)
    used = 0
    kept = {"decisions": 0, "pitfalls": 0, "files": 0, "tail": 0}

    # ---- ① 任务目标与最终停靠态 (强制保留 — 简报的存在意义) ----
    first_task = ""
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("role") == "user":
            t = (b.get("content") or "").strip()
            if t and not t.startswith(("<environment_context>", "<permissions")):
                first_task = " ".join(t.split())[:300]
                break
    goal_lines = ([f"- 任务: {first_task}"] if first_task else [])
    goal_lines.append(f"- 停点: {_brief_stop_point(blocks)}")

    # ---- ② 关键架构裁决 (assistant 决策/红线句, 行级去重) ----
    decisions, seen_dec = [], set()
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "text" or b.get("role") != "assistant":
            continue
        for ln in (b.get("content") or "").splitlines():
            t = ln.strip()
            if not (12 <= len(t) <= 400):
                continue
            low = t.lower()
            if any(m in low[:32] for m in DECISION_MARKERS) \
                    or any(m in low[:48] for m in CONSTRAINT_MARKERS):
                key = t[:80]
                if key not in seen_dec:
                    seen_dec.add(key)
                    decisions.append("- " + t[:_BRIEF_LINE_CAP])

    # ---- ③ 已证伪死路与踩坑 (assistant pitfall 句 + 报错去重 ×N) ----
    pitfalls, seen_pit = [], set()
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "text" or b.get("role") != "assistant":
            continue
        for ln in (b.get("content") or "").splitlines():
            t = ln.strip()
            low = t.lower()
            if 12 <= len(t) <= 400 and any(m in low[:48] for m in PITFALL_MARKERS):
                key = t[:80]
                if key not in seen_pit:
                    seen_pit.add(key)
                    pitfalls.append("- " + t[:_BRIEF_LINE_CAP])
    call_names = {}
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_call" and b.get("call_id"):
            call_names[str(b["call_id"])] = str(b.get("name") or "call")
    err_counts, err_order = {}, []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
            key = " ".join(str(b.get("content") or "").split())[:100]
            if key:
                if key not in err_counts:
                    err_counts[key] = [0, str(b.get("call_id") or "")]
                    err_order.append(key)
                err_counts[key][0] += 1
    err_lines = [f"[报错×{cnt} · {call_names.get(cid, cid[:20])}] {key[:160]}"
                 for key in err_order[:8]
                 for cnt, cid in (err_counts[key],)]

    # ---- ④ 触碰文件拓扑 + 工具统计 + 最近动作 (v0 契约段名保留) ----
    file_stats, tool_counts, calls = {}, Counter(), []
    path_arg_keys = ("path", "file", "file_path", "notebook_path", "abs_path")
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "tool_call":
            continue
        name = str(b.get("name") or "?")
        tool_counts[name] += 1
        calls.append(b)
        args = b.get("arguments") if isinstance(b.get("arguments"), dict) else {}
        for k in path_arg_keys:
            v = args.get(k)
            if isinstance(v, str) and v:
                st = file_stats.setdefault(v, {"n": 0, "last": name})
                st["n"] += 1
                st["last"] = name
                break
    file_lines = [f"- `{p}` ×{st['n']} (末次: {st['last']})"
                  for p, st in sorted(file_stats.items(), key=lambda kv: -kv[1]["n"])]
    recent_lines = [f"- `{b.get('name', '?')}` {json.dumps(b.get('arguments', {}), ensure_ascii=False)[:110]}"
                    for b in reversed(calls[-12:])]

    # ---- ⑤ 活上下文尾部 (v0 行格式) ----
    tail_lines = []
    for b in blocks[-30:]:
        if not isinstance(b, dict):
            continue
        role = b.get("role") or b.get("type")
        text = (b.get("content") or b.get("summary") or "").strip().replace("\n", " ")
        if b.get("type") == "compaction":
            text = "(压缩边界) " + (b.get("summary") or "")[:150]
        if b.get("type") == "tool_call":
            text = (b.get("name") or "") + " " + text[:110]
        elif b.get("type") == "tool_result":
            nm = call_names.get(str(b.get("call_id") or ""), "tool")
            note = "回显略 → 见死路/文件段" if b.get("is_error") else "回显略"
            text = "[" + nm + " " + note + "]"
        if text:
            tail_lines.append(f"- **{role}**: {text[:_BRIEF_LINE_CAP]}")

    # ---- 按预算装配 (优先级 ①→②→③→④→⑤; 段头成本忽略, ≪预算) ----
    out = [f"# 会话交接简报 — {c.get('agent', '?')} · {str(env.get('created_at', ''))[:10]}",
           "",
           f"- 工作目录: `{c.get('cwd', '?')}`",
           f"- 完整性: {c.get('completeness', '?')} · 块 {s.get('blocks_total', 0)} · "
           f"输入 {s.get('tokens_in', 0):,} tok · 压缩边界 {dist.get('compaction', 0)}",
           f"- 来源快照: `{env.get('_sid', _cv().snapshot_id(env))}` (可用 cv import --target codex|claude 精确迁移)"]

    def fit(title: str, lines: list, force: bool = False) -> list:
        nonlocal used
        body = []
        for ln in lines:
            cost = estimate_text_tokens(ln) + 1
            if used + cost > budget and body and not force:
                body.append("…[预算截断]")
                break
            if used + cost > budget and not force:
                continue
            body.append(ln)
            used += cost
        if not body:
            return []
        return ["", f"## {title}", ""] + body

    out += fit("它停在哪儿", goal_lines, force=True)
    if decisions:
        sec = fit("关键架构裁决", decisions)
        kept["decisions"] = sum(1 for ln in sec if ln.startswith("- "))
        out += sec
    pit_all = pitfalls + err_lines
    if pit_all:
        sec = fit("已证伪死路与踩坑", pit_all)
        kept["pitfalls"] = sum(1 for ln in sec if ln.startswith("- "))
        out += sec
    out += fit("最近动作 (工具调用, 末 12 条)", recent_lines or ["(无工具调用)"])
    if file_lines:
        top_tools = ", ".join(f"{k}×{v}" for k, v in tool_counts.most_common(5))
        sec = fit("触碰文件与工具", file_lines + [f"- 工具用量: {top_tools}"])
        kept["files"] = sum(1 for ln in sec if ln.startswith("- `"))
        out += sec
    if tail_lines:
        sec = fit("活上下文尾部 (最后 30 块)", tail_lines)
        kept["tail"] = sum(1 for ln in sec if ln.startswith("- **"))
        out += sec

    out_tokens = estimate_text_tokens("\n".join(out)) + used
    stats = {"budget_tokens": budget, "orig_tokens": orig_tokens, "out_tokens": out_tokens,
             "ratio": round(out_tokens / max(orig_tokens, 1), 4),
             "decisions_kept": kept["decisions"], "decisions_total": len(decisions),
             "pitfalls_kept": kept["pitfalls"], "pitfalls_total": len(pit_all),
             "files_kept": kept["files"], "files_total": len(file_lines),
             "tail_blocks_kept": kept["tail"]}
    out += ["", "## 压缩统计", "",
            f"- 预算 {budget} tok → 输出约 {stats['out_tokens']} tok"
            f" (原始估算 {orig_tokens} tok, 压缩比 {stats['ratio']:.2f})",
            f"- 保留: 裁决 {stats['decisions_kept']}/{stats['decisions_total']}"
            f" · 死路 {stats['pitfalls_kept']}/{stats['pitfalls_total']}"
            f" · 文件 {stats['files_kept']}/{stats['files_total']}"
            f" · 尾部块 {stats['tail_blocks_kept']}",
            "- token 数为估算值 (CJK≈1 字/tok, ASCII≈4 字符/tok)"]
    return "\n".join(out), stats


def blocks_to_brief(env: dict, max_chars: int = 48 * 1024, budget_tokens: int | None = None) -> str:
    """Markdown 交接简报: 精确迁移之外的可携降级档 (Beacon 模式) — 任何 agent 都吃 Markdown。
    v2: 预算受限五槽位效用装配 (compress_brief); budget_tokens=None 时按 max_chars 推导宽预算,
    小会话内容与 v0 一致。密钥类内容不特殊处理 (简报只含会话内已明文的内容)。"""
    brief, _stats = compress_brief(env, budget_tokens=budget_tokens, max_chars=max_chars)
    if len(brief) > max_chars:
        brief = brief[:max_chars] + "\n\n…[简报超限截断; 完整内容请用精确迁移 cv import]"
    return brief


def blocks_to_dsh(env: dict, strip_thinking: bool = False, session_id: str | None = None) -> tuple:
    """生成 DeepSeek Harness V4 会话 (header, events, dropped, dsh_sid, cwd), 实现位于 cv_dsh。"""
    import cv_dsh
    return cv_dsh.blocks_to_dsh(env, strip_thinking=strip_thinking, session_id=session_id)


def _record_migration(vault: Path, dest: Path, sid: str, baseline=None, target: str | None = None):
    rec = {"dest": _cv()._norm_path(dest), "source": sid, "baseline": baseline,
           "at": _cv().now_iso()}
    if target:
        rec["target"] = target
    with (vault / "migrations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def do_import(vault: Path, sid: str, target: str, apply: bool, strip_thinking=False,
              session_id: str | None = None, budget_tokens: int | None = None,
              skip_backup: bool = False) -> dict:
    """迁移执行核心 (CLI 与控制台 HTTP 共用)。返回 {lines, dropped, dest, new_sid, applied}。
    session_id 缺省按源会话 (agent+session_ref) 确定性派生: 同一会话重复导出/演进后重导出
    在目标端同名覆盖 (原位更新), 不再产生并排副本。antigravity apply 同时登记
    conversation_summaries 索引行 (只落 .db 不建索引 = 永不可见)。"""
    if target not in ("markdown", "codex", "claude", "antigravity", "zcode", "dsh") \
            and target not in _cv().SMIGRATE_TARGETS:
        raise _cv().BadMigrateTarget(f"未知迁移目标: {target!r} (可选 {'|'.join(_cv().SMIGRATE_TARGETS)}|markdown|zcode|dsh)")
    env = _cv().load_snapshot(vault, sid)
    if session_id is None:
        session_id = _deterministic_session_id(env)
    if target == "antigravity":
        exe = _cv().find_smigrate()
        if not exe:
            raise _cv().BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        res = _cv()._export_one_harness(vault, exe, sid, "antigravity", quiet=True, create_project=apply,
                                  session_id=session_id)
        out_db = Path(res["dest"])
        agy_sid = _cv()._antigravity_read_cascade_id(out_db)
        dest_str = str(out_db)
        if apply:
            if not agy_sid:
                raise _cv().BadMigrateTarget("Antigravity 导出文件缺少 cascade_id，无法写入会话目录")
            conv_dir = _cv().HOME / ".gemini" / "antigravity" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            target_db = conv_dir / f"{agy_sid}.db"
            tmp_db = conv_dir / f".{agy_sid}.tmp-{os.getpid()}"
            shutil.copy2(out_db, tmp_db)
            os.replace(tmp_db, target_db)
            dest_str = str(target_db)
            _cv()._antigravity_register_summaries(target_db, env, agy_sid)
            _record_migration(vault, target_db, sid, baseline=None, target="antigravity")
        return {"lines": res["lines"], "dropped": res["dropped"], "dest": dest_str,
                "new_sid": agy_sid, "applied": apply}
    if target == "zcode":
        return do_import_zcode(vault, env, sid, apply, session_id=session_id, skip_backup=skip_backup)
    if target == "dsh":
        import cv_dsh
        header, events, dropped, dsh_sid, canonical_cwd = cv_dsh.blocks_to_dsh(
            env, strip_thinking, session_id=session_id,
        )
        if not apply:
            (vault / "outbox").mkdir(parents=True, exist_ok=True)
            dest = vault / "outbox" / f"dsh-{sid}-{dsh_sid}.v4.jsonl"
            cv_dsh.write_dsh_session(dest, header, events, compression="none")
            return {"lines": 1 + len(events), "dropped": dropped, "dest": str(dest),
                    "new_sid": dsh_sid, "applied": False}
        sessions_root = cv_dsh.dsh_sessions_root()
        comp = cv_dsh.detect_root_compression(sessions_root)
        if comp == "zstd" and cv_dsh.zstd_backend() is None:
            raise _cv().BadMigrateTarget(
                "目标 ~/.dsh/sessions/ 使用 zstd 编码，但本机无可用 zstd 编码器 "
                "(需 Node 22.18+ / Python 3.14+ / zstandard / zstd CLI)；已阻止破坏性写入，请先用 dry-run 导出纯文本到 outbox"
            )
        fname = "session.v4.jsonl.zstd" if comp == "zstd" else "session.v4.jsonl"
        dest = sessions_root / cv_dsh.project_key(canonical_cwd) / cv_dsh.encode_segment(dsh_sid) / fname
        cv_dsh.write_dsh_session(dest, header, events, compression=comp)
        cv_dsh.invalidate_dsh_projcache(dsh_sid)
        ws_reg = cv_dsh.register_dsh_workspace(dsh_sid, canonical_cwd)
        _record_migration(vault, dest, sid, baseline=None, target="dsh")
        return {"lines": 1 + len(events), "dropped": dropped, "dest": str(dest),
                "new_sid": dsh_sid, "applied": True, "workspace_registered": ws_reg}
    if target not in ("markdown", "codex", "claude"):
        # 其余 15 个 harness 目标: outbox 档 (写入各家私有目录属平台安装器职责, 不自动落盘)
        exe = _cv().find_smigrate()
        if not exe:
            raise _cv().BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        res = _cv()._export_one_harness(vault, exe, sid, target, quiet=True, session_id=session_id)
        return {"lines": res["lines"], "dropped": res["dropped"], "dest": res["dest"],
                "new_sid": None, "applied": False}
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    if target == "markdown":  # 交接简报: 可携降级档, 不写真实会话目录, 无 apply 语义
        dest = vault / "outbox" / f"brief-{sid}.md"
        lines_text = blocks_to_brief(env, budget_tokens=budget_tokens)
        _cv().atomic_write_text(dest, lines_text)
        return {"lines": len(lines_text.splitlines()), "dropped": 0, "dest": str(dest),
                "new_sid": None, "applied": False}
    if target == "codex":
        lines, dropped, new_sid, name = blocks_to_codex(env, strip_thinking, session_id=session_id)
        dest = vault / "outbox" / name
        if apply:
            existing = sorted(_cv().CODEX_SESSIONS.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
            if existing:  # 确定性 id: 已有同名会话 = 原位更新, 避免 resume 按 uuid 歧义
                dest = existing[-1]
            else:
                day = dt.datetime.now()
                dest = _cv().CODEX_SESSIONS / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        lines, dropped = blocks_to_claude(env, strip_thinking, session_id=session_id)
        name = f"{session_id}.jsonl"
        cwd = env["capture"].get("cwd") or str(Path.cwd())
        dest = vault / "outbox" / f"claude-{sid}-{name}"
        if apply:
            dest = claude_project_dir(cwd) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    _cv().atomic_write_text(dest, "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
    if apply:
        # 迁移副本登记 (见 load_migrations): 之后 sync 扫到该文件时打上 migrated_from + 首版用量
        # 基线, owned_stats 扣基线只计迁入后真实新增。codex 目标回填了源 token_usage_record →
        # 基线 = 源用量; claude 目标不回填 usage → 无基线 (只标来源)
        s = env["stats"]
        base = ({k: s.get(k, 0) for k in ("tokens_in", "tokens_out", "cache_read", "cache_write",
                                          "total_tokens")}
                if target == "codex" and s.get("usage_events") else None)
        _record_migration(vault, dest, sid, baseline=base, target=target)
    return {"lines": len(lines), "dropped": dropped, "dest": str(dest),
            "new_sid": new_sid if target == "codex" else session_id if target == "claude" else None,
            "applied": apply}


_HARNESS_ORDER = ("zcode", "claude", "codex", "antigravity", "dsh")


def _canonical_harness(agent: str | None) -> str:
    a = (agent or "").strip().lower()
    if a in ("claude", "claude-code", "claude-transcript"):
        return "claude"
    return a or "unknown"


def _is_same_harness(src_agent: str | None, target: str) -> bool:
    tgt = str(target or "").strip().lower()
    return bool(tgt) and tgt != "markdown" and _canonical_harness(src_agent) == tgt


def _infer_migration_target(entry: dict) -> str:
    t = str(entry.get("target") or "").strip().lower()
    if t:
        return t
    norm = str(entry.get("dest") or "").replace("\\", "/").lower()
    if "/.codex/" in norm or "/rollout-" in norm:
        return "codex"
    if "/.claude/" in norm:
        return "claude"
    if "/.gemini/antigravity/" in norm:
        return "antigravity"
    if "/.dsh/" in norm:
        return "dsh"
    if "/.zcode/" in norm or norm.endswith("/db.sqlite"):
        return "zcode"
    return ""


def _load_migrated_targets(vault: Path) -> set[tuple[str, str]]:
    """读取 vault/migrations.jsonl -> {(source_sid, target)}，非 zcode 目标仅当目标文件仍存在时计为已迁移。"""
    p = vault / "migrations.jsonl"
    if not p.is_file():
        return set()
    out: set[tuple[str, str]] = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        sid = str(e.get("source") or "").strip()
        tgt = _infer_migration_target(e)
        dest = str(e.get("dest") or "").strip()
        if not sid or not tgt:
            continue
        if tgt == "zcode" or (dest and Path(dest).is_file()):
            out.add((sid, tgt))
    return out


def _validate_mig_target(target: str) -> str:
    tgt = str(target or "").strip().lower()
    if tgt not in ("markdown", "codex", "claude", "antigravity", "zcode", "dsh") \
            and tgt not in _cv().SMIGRATE_TARGETS:
        raise _cv().BadMigrateTarget(
            f"未知迁移目标: {target!r} (可选 {'|'.join(_cv().SMIGRATE_TARGETS)}|markdown|zcode|dsh)"
        )
    return tgt


def plan_batch_migration(
    vault: Path,
    *,
    source: str = "all",
    target: str = "codex",
    project: str | None = None,
    query: str | None = None,
    include_archived: bool = False,
    include_subagents: bool = False,
    skip_migrated: bool = True,
    limit: int = 0,
    ui_limit: int = 300,
) -> dict:
    """盘点并预演整端 / 批量跨 Harness 会话迁移计划 (纯只读)。"""
    tgt = _validate_mig_target(target)
    c = _cv()
    all_top = c.resolve_top_conversations(
        vault,
        include_subagents=bool(include_subagents),
        include_archived=bool(include_archived),
    )
    src_counts: dict[str, int] = {}
    for it in all_top:
        ca = _canonical_harness(it.get("agent"))
        src_counts[ca] = src_counts.get(ca, 0) + 1
    sources = [
        {"agent": ag, "count": cnt}
        for ag, cnt in sorted(
            src_counts.items(),
            key=lambda kv: (_HARNESS_ORDER.index(kv[0]) if kv[0] in _HARNESS_ORDER else 99, kv[0]),
        )
        if cnt > 0
    ]

    src_raw = str(source or "all").strip().lower()
    if not src_raw or src_raw == "all":
        want_sources = None
        src_norm = "all"
    else:
        want_sources = {_canonical_harness(x) for x in src_raw.split(",") if x.strip()}
        src_norm = ",".join(sorted(want_sources)) if len(want_sources) > 1 else next(iter(want_sources))

    src_items = [
        it for it in all_top
        if want_sources is None or _canonical_harness(it.get("agent")) in want_sources
    ]

    proj_groups: dict[str, dict] = {}
    for it in src_items:
        cwd = it.get("cwd") or ""
        if c._is_ungrouped_cwd(cwd):
            key = "__ungrouped__"
            name = "未关联工作区"
        else:
            key = cwd
            norm_p = cwd.replace("\\", "/").rstrip("/")
            name = norm_p.rsplit("/", 1)[-1] or cwd
        grp = proj_groups.get(key)
        if grp is None:
            proj_groups[key] = {"cwd": key, "name": name, "count": 1}
        else:
            grp["count"] += 1
    projects = sorted(proj_groups.values(), key=lambda g: (-g["count"], g["name"].lower()))[:200]

    proj_filter = str(project or "").strip()
    if proj_filter:
        if proj_filter == "__ungrouped__":
            matched = [it for it in src_items if c._is_ungrouped_cwd(it.get("cwd") or "")]
        else:
            matched = [it for it in src_items if c._norm_project_match(it.get("cwd") or "", proj_filter)]
            matched = c._prefer_exact_project(matched, proj_filter)
    else:
        matched = list(src_items)

    q_str = str(query or "").strip().lower()
    if q_str:
        matched = [
            it for it in matched
            if q_str in (it.get("id") or "").lower()
            or q_str in (it.get("title") or "").lower()
            or q_str in (it.get("cwd") or "").lower()
            or q_str in (it.get("agent") or "").lower()
        ]

    if limit and limit > 0:
        matched = matched[:limit]

    migrated_set = _load_migrated_targets(vault)
    annotated: list[dict] = []
    c_ready = c_mig = c_same = 0
    for it in matched:
        ca = _canonical_harness(it.get("agent"))
        same_origin = _is_same_harness(ca, tgt)
        migrated = (tgt != "markdown") and ((it["id"], tgt) in migrated_set)
        if same_origin:
            st = "skip_same_origin"
            c_same += 1
        elif skip_migrated and migrated:
            st = "skip_migrated"
            c_mig += 1
        else:
            st = "ready"
            c_ready += 1
        annotated.append({
            "id": it["id"],
            "agent": it.get("agent") or "",
            "canonical_agent": ca,
            "session_ref": it.get("session_ref") or "",
            "cwd": it.get("cwd") or "",
            "title": it.get("title") or "",
            "blocks_total": int(it.get("blocks_total") or 0),
            "created_at": it.get("created_at") or "",
            "same_origin": same_origin,
            "migrated": migrated,
            "status": st,
        })

    shown = annotated[:ui_limit] if ui_limit and ui_limit > 0 else annotated
    return {
        "source": src_norm,
        "target": tgt,
        "total_all": len(all_top),
        "total": len(annotated),
        "sources": sources,
        "projects": projects,
        "counts": {
            "total": len(annotated),
            "ready": c_ready,
            "skip_migrated": c_mig,
            "skip_same_origin": c_same,
            "projects": len(projects),
        },
        "items": shown,
        "truncated": len(annotated) > len(shown),
    }


def run_batch_migration(
    vault: Path,
    *,
    source: str = "all",
    target: str = "codex",
    selected: list[str] | None = None,
    project: str | None = None,
    query: str | None = None,
    include_archived: bool = False,
    include_subagents: bool = False,
    skip_migrated: bool = True,
    strip_thinking: bool = False,
    budget_tokens: int | None = None,
    limit: int = 0,
    apply: bool = False,
) -> dict:
    """执行整端 / 多选会话批量迁移 (默认 dry-run 至 outbox; apply=True 写入真实目录并单条容错)。"""
    tgt = _validate_mig_target(target)
    eff_apply = bool(apply) and tgt != "markdown"
    t0 = time.time()
    plan = plan_batch_migration(
        vault,
        source=source,
        target=tgt,
        project=project,
        query=query,
        include_archived=include_archived,
        include_subagents=include_subagents,
        skip_migrated=skip_migrated,
        limit=limit,
        ui_limit=0,
    )
    c = _cv()
    if selected is not None:
        if not isinstance(selected, list):
            raise ValueError("selected 必须为快照 ID 数组")
        want_ids: list[str] = []
        seen_ids: set[str] = set()
        for raw in selected:
            sid = str(raw or "").strip()
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                want_ids.append(sid)
        if not want_ids:
            raise ValueError("请至少选择一条要迁移的会话")
        by_plan = {it["id"]: it for it in plan["items"]}
        need_fallback = [sid for sid in want_ids if sid not in by_plan]
        by_all = {}
        if need_fallback:
            all_ever = c.resolve_top_conversations(vault, include_subagents=True, include_archived=True)
            by_all = {it["id"]: it for it in all_ever}
        migrated_set = _load_migrated_targets(vault)
        candidates: list[dict] = []
        for sid in want_ids:
            it = by_plan.get(sid)
            if it is not None:
                candidates.append(it)
                continue
            raw_it = by_all.get(sid)
            if raw_it is None:
                env = c.load_snapshot(vault, sid)
                cap = env.get("capture") or {}
                raw_it = {
                    "id": sid,
                    "agent": cap.get("agent") or "",
                    "cwd": cap.get("cwd") or "",
                    "title": cap.get("originator") or sid,
                    "created_at": env.get("created_at") or "",
                }
            ca = _canonical_harness(raw_it.get("agent"))
            same_origin = _is_same_harness(ca, tgt)
            migrated = (tgt != "markdown") and ((sid, tgt) in migrated_set)
            st = "skip_same_origin" if same_origin else ("skip_migrated" if (skip_migrated and migrated) else "ready")
            candidates.append({
                "id": sid,
                "agent": raw_it.get("agent") or "",
                "canonical_agent": ca,
                "cwd": raw_it.get("cwd") or "",
                "title": raw_it.get("title") or "",
                "same_origin": same_origin,
                "migrated": migrated,
                "status": st,
            })
    else:
        candidates = list(plan["items"])

    zcode_backup_name = None
    has_ready = any(it["status"] == "ready" for it in candidates)
    if tgt == "zcode" and eff_apply and has_ready:
        zdb = Path(c.ZCODE_DB)
        if not zdb.exists():
            raise c.BadMigrateTarget(f"ZCode 主库不存在: {zdb}")
        bdir = vault / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        bp = bdir / f"zcode-import-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.sqlite"
        shutil.copy2(zdb, bp)
        zcode_backup_name = bp.name

    results: list[dict] = []
    n_mig = n_skip_mig = n_skip_same = n_fail = total_lines = 0
    first_dest_dir = ""
    for it in candidates:
        sid = it["id"]
        st = it["status"]
        base_row = {
            "id": sid,
            "agent": it.get("agent") or "",
            "title": it.get("title") or "",
            "cwd": it.get("cwd") or "",
        }
        if st == "skip_same_origin":
            n_skip_same += 1
            results.append({**base_row, "action": "skipped_same_origin", "reason": "源与目标同 Harness"})
            continue
        if st == "skip_migrated":
            n_skip_mig += 1
            results.append({**base_row, "action": "skipped_migrated", "reason": "已在 migrations.jsonl 登记"})
            continue
        try:
            r = c.do_import(
                vault, sid, tgt, eff_apply,
                strip_thinking=bool(strip_thinking),
                budget_tokens=budget_tokens,
                skip_backup=(tgt == "zcode" and eff_apply),
            )
        except c.BadMigrateTarget:
            raise
        except Exception as e:
            n_fail += 1
            results.append({**base_row, "action": "failed", "error": str(e)[:200]})
            continue
        n_mig += 1
        total_lines += int(r.get("lines") or 0)
        dest_val = str(r.get("dest") or "")
        if not first_dest_dir and dest_val:
            if tgt == "zcode" and eff_apply:
                first_dest_dir = str(c.ZCODE_DB)
            else:
                first_dest_dir = str(Path(dest_val).parent)
        results.append({
            **base_row,
            "action": "migrated" if r.get("applied") else "outbox",
            "dest": dest_val,
            "new_sid": r.get("new_sid"),
            "lines": int(r.get("lines") or 0),
            "dropped": int(r.get("dropped") or 0),
        })

    if not first_dest_dir:
        first_dest_dir = str(vault / "outbox")
    return {
        "applied": eff_apply,
        "source": plan["source"],
        "target": tgt,
        "dest_dir": first_dest_dir,
        "zcode_backup": zcode_backup_name,
        "counts": {
            "matched": len(candidates),
            "migrated": n_mig,
            "skipped_migrated": n_skip_mig,
            "skipped_same_origin": n_skip_same,
            "failed": n_fail,
            "total_lines": total_lines,
        },
        "elapsed": round(time.time() - t0, 2),
        "total_results": len(results),
        "items": results[:200],
    }


def _print_import_result(r: dict, target: str):
    if r["new_sid"]:
        print(f"(session_id: {r['new_sid']})")
    where = "已写入真实会话目录" if r["applied"] else "dry-run (vault/outbox/, 加 --apply 才写真实会话目录)"
    print(f"导入 {r['lines']} 行 → {r['dest']}"
          + (f"  (丢弃无法配对的 tool_result×{r['dropped']})" if r["dropped"] else ""))
    print(f"  {where}")
    if target == "codex" and r["applied"]:
        print("  验证: codex resume --last 或 codex resume <session-id>")
    if target == "claude" and r["applied"]:
        print("  验证: claude --resume (会话列表应出现迁移会话)")
    if target == "antigravity" and r["applied"]:
        print("  验证: 完全关闭并重启 Antigravity 后在对应项目下查看导入会话")
    if target == "dsh" and r["applied"]:
        print("  验证: 打开或重启 DeepSeek Harness，在对应工作区会话列表查看导入会话")


def _check_single_sid_agent(vault: Path, sid: str, agent: str | None):
    agents = _cv()._parse_agent_filter(agent)
    if not agents:
        return
    env = _cv().load_snapshot(vault, sid)
    src_ag = ((env.get("capture") or {}).get("agent") or "").lower()
    if src_ag not in agents:
        raise _cv().BadMigrateTarget(f"快照 {sid} 来源为 {src_ag or 'unknown'!r}，不符合指定 --agent={agent!r}")


def cmd_import(args):
    vault = _cv().vault_dir(args)
    sid = getattr(args, "snapshot_id", None)
    project = getattr(args, "project", None)
    req_agent = getattr(args, "agent", None)
    if sid:
        _check_single_sid_agent(vault, sid, req_agent)
        r = _cv().do_import(vault, sid, args.target, args.apply, args.strip_thinking,
                      budget_tokens=getattr(args, "budget_tokens", None))
        _print_import_result(r, args.target)
        return
    if project:
        items = _cv()._resolve_project_items(
            vault,
            project,
            include_subagents=getattr(args, "include_subagents", False),
            limit=getattr(args, "limit", 0),
            include_archived=getattr(args, "include_archived", False),
            agent=req_agent,
        )
        if not items:
            print(f"未找到匹配项目「{project}」的顶层主对话")
            return
        for it in items:
            title = (it["title"] or "(无标题)")[:50]
            print(f"\n[{it['id']}] {it['cwd'] or '(无项目)'} — {title}")
            r = _cv().do_import(vault, it["id"], args.target, args.apply, args.strip_thinking,
                          budget_tokens=getattr(args, "budget_tokens", None))
            _print_import_result(r, args.target)
        print(f"\n共导入 {len(items)} 条主对话 → {args.target}")
        return
    if req_agent:
        res = run_batch_migration(
            vault,
            source=req_agent,
            target=args.target,
            include_archived=getattr(args, "include_archived", False),
            include_subagents=getattr(args, "include_subagents", False),
            skip_migrated=not bool(getattr(args, "no_skip_migrated", False)),
            strip_thinking=getattr(args, "strip_thinking", False),
            budget_tokens=getattr(args, "budget_tokens", None),
            limit=getattr(args, "limit", 0),
            apply=bool(args.apply),
        )
        c = res["counts"]
        mode = "写入目标目录" if res["applied"] else "dry-run (vault/outbox/)"
        print(f"整端批量迁移 [{res['source']} -> {res['target']}] ({mode}): "
              f"成功 {c['migrated']} · 跳过已迁移 {c['skipped_migrated']} · "
              f"同源跳过 {c['skipped_same_origin']} · 失败 {c['failed']} · 总行数 {c['total_lines']}")
        if res.get("dest_dir"):
            print(f"  输出位置: {res['dest_dir']}")
        return
    raise SystemExit("请指定 snapshot_id、--project <项目名/路径> 或 --agent <来源>")


def cmd_migrate(args):
    sid = _cv().cmd_export(args)
    class A:
        pass
    a = A(); a.vault = args.vault; a.snapshot_id = sid
    cmd_diff(a)
    print()
    if not args.yes:
        try:
            ans = input("继续导入? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # 非交互管道 (< /dev/null) 或 Ctrl+C: 按"取消"处理 — 保留已导出快照, 干净退出不抛栈
            print("已取消 (无交互输入; 快照已留存于 vault)")
            return
        if ans != "y":
            print("已中止 (快照已留存于 vault)")
            return
    a.target, a.apply, a.strip_thinking = args.target, args.apply, args.strip_thinking
    a.budget_tokens = getattr(args, "budget_tokens", None)
    cmd_import(a)


