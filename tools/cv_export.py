"""快照规范化导出、内容寻址、血缘回填、只读加载与可选敏感信息脱敏。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from cv_core import SnapshotNotFound, cv_mod as _cv

SNAP_COLS = (
    "id,created_at,agent,source_format,provider,model,session_ref,originator,"
    "completeness,tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,"
    "total_tokens,cost_usd,usage_events,blocks_total,blocks_dropped,path,parent_snapshot,"
    "fork_of,migrated_from,migration_baseline,cwd,parent_session_ref,summary_title"
)

_GENERIC_ORIGINATORS = {"codex_cli_rs", "codex_vscode", "claude-code", "antigravity", "dsh"}
LINEAGE_TAIL = 4096  # envelope 的 lineage 是 JSON 最后一个键 (json.dumps 保序): 读尾部即可, 免解析数 MB 全文

_SECRET_PATTERNS = (
    # PEM 私钥块
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
     "[REDACTED:PRIVATE_KEY]"),
    # GitHub token (ghp_/gho_/ghu_/ghs_/github_pat_)
    (re.compile(r"\b(?:gh[pous]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
     "[REDACTED:GITHUB_TOKEN]"),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY]"),
    # OpenAI / Anthropic / 通用 sk- 密钥
    (re.compile(r"\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED:API_KEY]"),
    # JWT 三段式令牌
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
     "[REDACTED:JWT]"),
)


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
    if path.name.endswith(".zstd"):
        import cv_dsh
        return cv_dsh.first_dsh_jsonl(path)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                return json.loads(line)
    raise SystemExit(f"空文件: {path}")


def clip(text: str) -> tuple:
    lim = getattr(_cv(), "TOOL_RESULT_LIMIT", 256 * 1024)
    if len(text) <= lim:
        return text, False
    keep = lim // 2
    return text[:keep] + "\n…[cv: truncated]…\n" + text[-keep:], True


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def zero_stats() -> dict:
    return {
        "tokens_in": 0, "tokens_out": 0, "reasoning_tokens": 0, "cache_read": 0,
        "cache_write": 0, "total_tokens": 0, "cost_usd": None, "usage_events": 0,
        "blocks_total": 0, "blocks_dropped": 0,
    }


def detect_format(first: dict, path: Path) -> str:
    t = first.get("type")
    if t in ("session_meta", "event_msg", "response_item", "turn_context"):
        return "codex-rollout"
    if t == "session" and isinstance(first.get("version"), int) and "id" in first:
        return "dsh-jsonl"
    if t in ("user", "tool_use", "tool_result"):
        # 标准 Claude Code: user/assistant 行带 message 嵌套; ZCode 变体: 平铺 content
        return "claude-projects" if "message" in first else "claude-transcript"
    if path.name.endswith(".zstd"):
        raise SystemExit(f"无法识别源格式: {path} (首行 type={t!r})")
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
    capture = {
        "agent": "codex", "source_format": "codex-rollout", "provider": None,
        "model": None, "completeness": "full",
    }
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
            blocks.append({
                "type": "compaction", "summary": (payload.get("message") or "")[:2000],
                "replaced_blocks": [pending_conv_idx, max(len(blocks) - 1, pending_conv_idx)],
                "ts": ts,
            })
            pending_conv_idx = len(blocks)
        elif t == "response_item":
            pt = payload.get("type")
            if pt == "message":
                role = payload.get("role")
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
                blocks.append({
                    "type": "tool_call", "call_id": payload.get("call_id"),
                    "name": payload.get("name"), "namespace": payload.get("namespace"),
                    "arguments": args, "ts": ts,
                })
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = payload.get("output")
                if isinstance(out, list):
                    out = "\n---\n".join(c.get("text", "") for c in out if isinstance(c, dict))
                content, _ = clip(str(out or ""))
                blocks.append({
                    "type": "tool_result", "call_id": payload.get("call_id"),
                    "content": content, "is_error": False, "ts": ts,
                })
            elif pt == "agent_message":
                text = "\n".join(c.get("text", "") for c in payload.get("content", []))
                blocks.append({
                    "type": "agent_message", "author": payload.get("author"),
                    "recipient": payload.get("recipient"), "content": text, "ts": ts,
                })
            else:
                dropped += 1
    for tu in turn_usage.values():
        for k, src in (
            ("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
            ("reasoning_tokens", "reasoning_output_tokens"),
            ("cache_read", "cached_input_tokens"),
            ("cache_write", "cache_write_input_tokens"), ("total_tokens", "total_tokens"),
        ):
            stats[k] += int(tu.get(src, 0) or 0)
        stats["usage_events"] += 1
    stats["blocks_total"], stats["blocks_dropped"] = len(blocks), dropped
    return {"capture": capture, "conversation": blocks, "stats": stats}


def export_claude(path: Path, drop_developer: bool) -> dict:
    blocks, stats = [], zero_stats()
    capture = {
        "agent": "zcode", "source_format": "claude-transcript", "provider": None,
        "model": None, "session_ref": path.stem, "completeness": "no_assistant_text",
    }
    seq = 0
    pending: dict = {}
    unmatched_results = 0
    for obj in iter_jsonl(path):
        t, ts = obj.get("type"), obj.get("timestamp", "")
        if t == "user":
            blocks.append({"type": "text", "role": "user", "content": str(obj.get("content", "")), "ts": ts})
        elif t == "tool_use":
            seq += 1
            call_id = f"cv_synth_{seq}"
            pending.setdefault(obj.get("tool_name"), []).append(call_id)
            blocks.append({
                "type": "tool_call", "call_id": call_id, "name": obj.get("tool_name"),
                "namespace": None, "arguments": obj.get("tool_input"), "ts": ts,
            })
        elif t == "tool_result":
            queue = pending.get(obj.get("tool_name"))
            call_id = queue.pop(0) if queue else None
            if call_id is None:
                unmatched_results += 1
                continue
            out = obj.get("tool_output")
            content = out.get("output", "") if isinstance(out, dict) else str(out or "")
            content, _ = clip(str(content))
            blocks.append({
                "type": "tool_result", "call_id": call_id,
                "content": content,
                "is_error": bool(obj.get("is_error") or (isinstance(out, dict) and out.get("is_error"))),
                "ts": ts,
            })
    if unmatched_results:
        capture["notes"] = f"{unmatched_results} 条无对应调用的 tool_result 被丢弃"
    stats["blocks_total"] = len(blocks)
    stats["blocks_dropped"] = unmatched_results
    return {"capture": capture, "conversation": blocks, "stats": stats}


def export_claude_code(path: Path, drop_developer: bool) -> dict:
    """标准 Claude Code 会话 (~/.claude/projects/<cwd码>/<uuid>.jsonl) → 快照。"""
    blocks, stats, dropped = [], zero_stats(), 0
    capture = {
        "agent": "claude-code", "source_format": "claude-projects", "provider": None,
        "model": None, "session_ref": path.stem, "completeness": "full",
    }
    for obj in iter_jsonl(path):
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        ts = obj.get("timestamp", "")
        if t == "user":
            if isinstance(content, str):
                blocks.append({"type": "text", "role": "user", "content": content, "ts": ts})
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "role": "user", "content": b.get("text", ""), "ts": ts})
                    elif b.get("type") == "tool_result":
                        out = b.get("content")
                        if isinstance(out, list):
                            out = chr(10).join(x.get("text", "") for x in out if isinstance(x, dict))
                        text, _ = clip(str(out or ""))
                        blocks.append({
                            "type": "tool_result", "call_id": b.get("tool_use_id"),
                            "content": text, "is_error": bool(b.get("is_error")), "ts": ts,
                        })
        else:
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
                    blocks.append({"type": "text", "role": "assistant", "content": b.get("text", ""), "ts": ts})
                elif bt == "thinking":
                    summary = b.get("thinking", "") or b.get("text", "") or ""
                    if summary:
                        blocks.append({"type": "thinking", "summary": summary, "ts": ts})
                elif bt == "tool_use":
                    blocks.append({
                        "type": "tool_call", "call_id": b.get("id"),
                        "name": b.get("name"), "namespace": None,
                        "arguments": b.get("input"), "ts": ts,
                    })
        if obj.get("cwd") and not capture.get("cwd"):
            capture["cwd"] = obj.get("cwd")
        if obj.get("version") and not capture.get("agent_version"):
            capture["agent_version"] = obj.get("version")
    if not stats["usage_events"]:
        capture["completeness"] = "full"
    stats["total_tokens"] = stats["tokens_in"] + stats["tokens_out"] + stats["cache_read"] + stats["cache_write"]
    stats["blocks_total"], stats["blocks_dropped"] = len(blocks), dropped
    return {"capture": capture, "conversation": blocks, "stats": stats}


def export_dsh(path: Path, drop_developer: bool = False) -> dict:
    """DeepSeek Harness 会话 (.v4.jsonl[.zstd] 等) → 规范快照 dict。"""
    import cv_dsh
    return cv_dsh.export_dsh(path, drop_developer)


def build_envelope(exported: dict) -> dict:
    c = _cv()
    return {
        "format_version": getattr(c, "FORMAT_VERSION", "0.1"),
        "created_at": c.now_iso(),
        "capture": exported["capture"],
        "conversation": exported["conversation"],
        "memory_hints": None,
        "stats": exported["stats"],
        "lineage": {"parent_snapshot": None, "fork_of": None},
    }


def _derive_summary_title(envelope: dict) -> str:
    c = envelope.get("capture") or {}
    orig = str(c.get("originator") or "").strip()
    if orig and orig.lower() not in _GENERIC_ORIGINATORS:
        return orig[:100]
    for b in envelope.get("conversation") or []:
        if not isinstance(b, dict) or b.get("role") != "user":
            continue
        txt = str(b.get("content") or b.get("summary") or "").strip()
        if not txt or txt.startswith(("<environment_context>", "<permissions instructions>")):
            continue
        return " ".join(txt.split())[:100]
    return orig[:100]


def register_snapshot(conn, envelope: dict, sid: str, path: Path):
    c, s = envelope["capture"], envelope["stats"]
    lin = envelope.get("lineage") or {}
    base = lin.get("migration_baseline")
    conn.execute(
        f"INSERT OR REPLACE INTO snapshots({SNAP_COLS}) VALUES({','.join('?' * 27)})",
        (
            sid, envelope["created_at"], c.get("agent"), c.get("source_format"), c.get("provider"),
            c.get("model"), c.get("session_ref"), c.get("originator"), c.get("completeness"),
            s["tokens_in"], s["tokens_out"], s["reasoning_tokens"], s["cache_read"], s["cache_write"],
            s["total_tokens"], s["cost_usd"], s["usage_events"], s["blocks_total"], s["blocks_dropped"],
            str(path), lin.get("parent_snapshot"), lin.get("fork_of"), lin.get("migrated_from"),
            json.dumps(base) if base else None,
            str(c.get("cwd") or ""),
            str(c.get("parent_session_ref") or ""),
            _derive_summary_title(envelope),
        ),
    )


def read_lineage_tail(path) -> dict:
    """从快照文件尾部解析 lineage 对象。读失败/无 lineage 返回 {}。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            n = fh.tell()
            fh.seek(max(0, n - LINEAGE_TAIL))
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return {}
    i = tail.rfind('"lineage"')
    j = tail.find("{", i) if i >= 0 else -1
    if j < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail, j)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def backfill_lineage(conn, force: bool = False) -> int:
    """用快照文件的 lineage 回填索引列 (文件为权威, 文件无值时保留库值)。返回改动行数。"""
    c = _cv()
    conn.executescript(c.SCHEMA)
    if not force and conn.execute("SELECT 1 FROM vault_meta WHERE key='lineage_backfill_v1'").fetchone():
        return 0
    changed = 0
    rows = conn.execute(
        "SELECT id, path, parent_snapshot, fork_of, migrated_from, migration_baseline FROM snapshots"
    ).fetchall()
    ids = {r["id"] for r in rows}
    for r in rows:
        lin = read_lineage_tail(r["path"]) if r["path"] else {}
        par = r["parent_snapshot"]
        if par == r["id"] or par not in ids:
            fpar = lin.get("parent_snapshot")
            par = fpar if fpar in ids and fpar != r["id"] else None
        fk = lin.get("fork_of") or r["fork_of"]
        mf = lin.get("migrated_from") or r["migrated_from"]
        base = json.dumps(lin["migration_baseline"]) if lin.get("migration_baseline") else r["migration_baseline"]
        if (par, fk, mf, base) != (r["parent_snapshot"], r["fork_of"], r["migrated_from"], r["migration_baseline"]):
            conn.execute(
                "UPDATE snapshots SET parent_snapshot=?, fork_of=?, migrated_from=?, migration_baseline=? WHERE id=?",
                (par, fk, mf, base, r["id"]),
            )
            changed += 1
    conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('lineage_backfill_v1', ?)", (c.now_iso(),))
    conn.commit()
    return changed


def _parent_chain_reaches(conn, start: str, target: str, cap: int = 256) -> bool:
    """沿 parent_snapshot 上溯, 判断是否会回到 target (挂链前的防环终检)。"""
    seen, node = set(), start
    while node and node not in seen and len(seen) < cap:
        if node == target:
            return True
        seen.add(node)
        r = conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?", (node,)).fetchone()
        node = r[0] if r else None
    return False


def redact_secrets(text: str) -> tuple[str, int]:
    """对常见 API Key / Token / 私钥块做正则打码, 返回 (脱敏后文本, 命中次数)。"""
    if not text or not isinstance(text, str):
        return text, 0
    total = 0
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out, n = pat.subn(repl, out)
        total += n
    return out, total


def redact_envelope(envelope: dict) -> tuple[dict, int]:
    """原地对 context-snapshot 信封中所有对话块文本做脱敏, 返回 (envelope, 总命中数)。"""
    hits = 0
    for blk in envelope.get("conversation") or []:
        if not isinstance(blk, dict):
            continue
        for k in ("content", "text", "summary", "output"):
            if isinstance(blk.get(k), str) and blk[k]:
                blk[k], n = redact_secrets(blk[k])
                hits += n
        if isinstance(blk.get("args"), dict):
            for ak, av in list(blk["args"].items()):
                if isinstance(av, str) and av:
                    blk["args"][ak], n = redact_secrets(av)
                    hits += n
    return envelope, hits


def cmd_export(args):
    c = _cv()
    src = Path(args.source).expanduser()
    fmt = detect_format(first_jsonl(src), src)
    exporters = {
        "codex-rollout": c.export_codex,
        "claude-transcript": c.export_claude,
        "claude-projects": c.export_claude_code,
        "dsh-jsonl": c.export_dsh,
    }
    exported = exporters[fmt](src, args.drop_developer)
    envelope = build_envelope(exported)
    do_redact = bool(getattr(args, "redact", False)) or os.environ.get("CV_REDACT_SECRETS") == "1"
    if do_redact:
        envelope, _ = redact_envelope(envelope)
    sid = snapshot_id(envelope)
    vault = c.vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    out = vault / "snapshots" / f"{sid}.json"
    conn = c.db(vault)
    conn.executescript(c.SCHEMA + c.SYNC_TABLE)
    if out.exists():
        print(f"已存在 (幂等跳过): {out.name}")
        if conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone() is None:
            register_snapshot(conn, json.loads(out.read_text(encoding="utf-8")), sid, out)
    else:
        c.atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        s = exported["stats"]
        print(f"已导出 {out.name}  格式={fmt}  块={s['blocks_total']}  入token={s['tokens_in']}  cache读={s['cache_read']}")
        register_snapshot(conn, envelope, sid, out)
    try:
        st = src.stat()
        conn.execute(
            "INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at) VALUES(?,?,?,?,?)",
            (c._norm_path(src), st.st_mtime, st.st_size, sid, c.now_iso()),
        )
    except OSError:
        pass
    try:
        c0 = envelope["capture"]
        if c0.get("session_ref") and c0.get("agent"):
            sib = conn.execute(
                "SELECT id, created_at FROM snapshots WHERE agent=? AND session_ref=? AND id<>?"
                " ORDER BY created_at DESC LIMIT 1",
                (c0.get("agent"), c0.get("session_ref"), sid),
            ).fetchone()
            row = conn.execute("SELECT parent_snapshot, created_at FROM snapshots WHERE id=?", (sid,)).fetchone()
            if sib and row and not row[0]:
                if ((sib[1] or "", sib[0]) < (row[1] or "", sid)
                        and not _parent_chain_reaches(conn, sib[0], sid)):
                    conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (sib[0], sid))
                    envelope["lineage"]["parent_snapshot"] = sib[0]
                elif sib[0] != sid:
                    print(
                        f"提示: 同会话已有更新版本 {sib[0]} ({(sib[1] or '')[:19]}),"
                        f" 本次导出内容较旧 → 不挂链, 保留为独立快照 (防演进链成环)"
                    )
    except sqlite3.Error:
        pass
    conn.commit()
    return sid


def load_snapshot(args_or_vault, sid: str) -> dict:
    c = _cv()
    vault = args_or_vault if isinstance(args_or_vault, Path) else c.vault_dir(args_or_vault)
    p = vault / "snapshots" / f"{sid}.json"
    if not p.exists():
        raise SnapshotNotFound(f"快照不存在: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
