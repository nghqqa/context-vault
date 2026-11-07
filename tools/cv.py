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
import threading
import time
import urllib.error
import urllib.request
from array import array
from collections import OrderedDict
import hmac
import uuid
from pathlib import Path

FORMAT_VERSION = "0.1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # noconsole 进程 spawn 控制台程序不弹窗   # context-snapshot 格式版本
VERSION = "0.5.0"          # 工具版本 (发布节奏独立于格式)
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
  usage_events INTEGER, blocks_total INTEGER, blocks_dropped INTEGER, path TEXT,
  parent_snapshot TEXT, fork_of TEXT, migrated_from TEXT, migration_baseline TEXT);
CREATE TABLE IF NOT EXISTS ledger_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, occurred_at TEXT, kind TEXT,
  model TEXT, provider TEXT, client TEXT, tokens_saved INTEGER, cost_usd REAL,
  snapshot_id TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS probe_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, profile TEXT, endpoint TEXT,
  status TEXT, http_code INTEGER, latency_ms INTEGER, models TEXT, missing_models TEXT);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_events(occurred_at);
CREATE TABLE IF NOT EXISTS vault_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def vault_dir(args) -> Path:
    return Path(args.vault).expanduser()


def db(vault: Path) -> sqlite3.Connection:
    # WAL + busy_timeout: 托盘常驻多线程 (serve/哨兵/守望/sync) 与 CLI 并发写不再 database is locked
    conn = sqlite3.connect(vault / "vault.db", timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # 其他连接持锁时切不上就下次再切, 不阻塞本次
    # 索引层派生数据 (快照文件本身不变), 老库逐列迁移:
    #   parent_snapshot — sync 增量演进的上一版, owned_stats 据此只数每条演进链的叶子
    #   fork_of         — cv fork 的源节点 (原只活在 JSON 里, 树查询/统计要读全文件)
    #   migrated_from / migration_baseline — --apply 迁移副本的源快照与首版复制来的用量基线
    for col in ("parent_snapshot", "fork_of", "migrated_from", "migration_baseline"):
        try:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在 = 老库迁移完成; 表尚未建由调用方 executescript(SCHEMA)
    return conn


def atomic_write_text(path: Path, text: str):
    """同目录临时文件 + os.replace 原子落盘; 进程中途被杀不留截断文件 (快照/水位/基线共用)。"""
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
            # replaced_blocks = 上次压缩边界之后到当前最后一个会话块 (含端点) —
            # 旧算式 [0, max(pending_conv_idx-1,0)] 首压缩恒 [0,0]、后续漏掉两压缩之间的块
            blocks.append({"type": "compaction", "summary": (payload.get("message") or "")[:2000],
                           "replaced_blocks": [pending_conv_idx, max(len(blocks) - 1, pending_conv_idx)],
                           "ts": ts})
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
    capture = {"agent": "zcode", "source_format": "claude-transcript", "provider": None,
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
             "total_tokens,cost_usd,usage_events,blocks_total,blocks_dropped,path,parent_snapshot,"
             "fork_of,migrated_from,migration_baseline")


def register_snapshot(conn, envelope: dict, sid: str, path: Path):
    c, s = envelope["capture"], envelope["stats"]
    lin = envelope.get("lineage") or {}
    base = lin.get("migration_baseline")
    conn.execute(
        f"INSERT OR REPLACE INTO snapshots({SNAP_COLS}) VALUES({','.join('?' * 24)})",
        (sid, envelope["created_at"], c.get("agent"), c.get("source_format"), c.get("provider"),
         c.get("model"), c.get("session_ref"), c.get("originator"), c.get("completeness"),
         s["tokens_in"], s["tokens_out"], s["reasoning_tokens"], s["cache_read"], s["cache_write"],
         s["total_tokens"], s["cost_usd"], s["usage_events"], s["blocks_total"], s["blocks_dropped"],
         str(path), lin.get("parent_snapshot"), lin.get("fork_of"), lin.get("migrated_from"),
         json.dumps(base) if base else None))


LINEAGE_TAIL = 4096  # envelope 的 lineage 是 JSON 最后一个键 (json.dumps 保序): 读尾部即可, 免解析数 MB 全文


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
    """用快照文件的 lineage 回填索引列 (文件为权威, 文件无值时保留库值)。返回改动行数。
    parent_snapshot 列在 09f90a6 才加入且 ALTER 未回填: 此前登记的版本在库里全是孤立叶子
    (实测 915 行 / 921 个幻影叶子), owned_stats 的叶子去重对历史数据失效。每个 vault 执行
    一次 (vault_meta 标记), force=True 无视标记 (cv repair)。"""
    conn.executescript(SCHEMA)
    if not force and conn.execute("SELECT 1 FROM vault_meta WHERE key='lineage_backfill_v1'").fetchone():
        return 0
    changed = 0
    rows = conn.execute("SELECT id, path, parent_snapshot, fork_of, migrated_from, migration_baseline"
                        " FROM snapshots").fetchall()
    ids = {r["id"] for r in rows}
    for r in rows:
        lin = read_lineage_tail(r["path"]) if r["path"] else {}
        # 库值有效 (存在且非自环) 则优先 — prune 重链后文件 lineage 仍指向已删版本, 不得复活悬空指针;
        # 库值缺失/悬空时才用文件值, 文件值也无效则置空 (防幻影叶子)
        par = r["parent_snapshot"]
        if par == r["id"] or par not in ids:
            fpar = lin.get("parent_snapshot")
            par = fpar if fpar in ids and fpar != r["id"] else None
        fk = lin.get("fork_of") or r["fork_of"]
        mf = lin.get("migrated_from") or r["migrated_from"]
        base = json.dumps(lin["migration_baseline"]) if lin.get("migration_baseline") else r["migration_baseline"]
        if (par, fk, mf, base) != (r["parent_snapshot"], r["fork_of"], r["migrated_from"], r["migration_baseline"]):
            conn.execute("UPDATE snapshots SET parent_snapshot=?, fork_of=?, migrated_from=?,"
                         " migration_baseline=? WHERE id=?", (par, fk, mf, base, r["id"]))
            changed += 1
    conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('lineage_backfill_v1', ?)", (now_iso(),))
    conn.commit()
    return changed


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
    conn = db(vault)
    conn.executescript(SCHEMA)
    if out.exists():
        print(f"已存在 (幂等跳过): {out.name}")
        if conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone() is None:
            # 库里没这行 (如 vault.db 重建过): 用磁盘文件注册 — 它的 lineage 可能带 sync 挂的 parent
            register_snapshot(conn, json.loads(out.read_text(encoding="utf-8")), sid, out)
        # 库里已有则不重注册: INSERT OR REPLACE 会用 parent=None 的新 envelope 覆盖 sync
        # 挂好的 parent_snapshot, 演进链断裂后同会话 usage 双计 (全面测试 H1 的回归)
    else:
        atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        s = exported["stats"]
        print(f"已导出 {out.name}  格式={fmt}  块={s['blocks_total']}  入token={s['tokens_in']}  cache读={s['cache_read']}")
        register_snapshot(conn, envelope, sid, out)
    try:  # 登记 sync_sources: 手动 export 的会话也有源路径可查 (prune 源存活判定/后续 sync 增量)
        st = src.stat()
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (_norm_path(src), st.st_mtime, st.st_size, sid, now_iso()))
    except OSError:
        pass
    conn.commit()
    return sid


class SnapshotNotFound(LookupError):
    """快照不存在 — 领域层可捕获错误。此前抛 SystemExit, 它不是 Exception 子类,
    HTTP 层的 except Exception 接不住, 控制台 /migrate 会直接断连而非返回 404 JSON。"""


class BadMigrateTarget(ValueError):
    """迁移目标不合法 (HTTP 层映射 400; CLI 由 argparse choices 前置拦截)。"""


def load_snapshot(args_or_vault, sid: str) -> dict:
    vault = args_or_vault if isinstance(args_or_vault, Path) else vault_dir(args_or_vault)
    p = vault / "snapshots" / f"{sid}.json"
    if not p.exists():
        raise SnapshotNotFound(f"快照不存在: {p}")
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


def blocks_to_brief(env: dict, max_chars: int = 48 * 1024) -> str:
    """Markdown 交接简报: 精确迁移之外的可携降级档 (Beacon 模式) — 任何 agent 都吃 Markdown。
    内容: 它停在哪儿 / 最近动作 / 活上下文尾部; 密钥类内容不特殊处理 (简报只含会话内已明文的内容)。"""
    from collections import Counter
    c, s = env["capture"], env["stats"]
    blocks = env["conversation"]
    dist = Counter(b["type"] for b in blocks)
    out = [f"# 会话交接简报 — {c.get('agent', '?')} · {str(env.get('created_at', ''))[:10]}",
           "",
           f"- 工作目录: `{c.get('cwd', '?')}`",
           f"- 完整性: {c.get('completeness', '?')} · 块 {s['blocks_total']} · "
           f"输入 {s['tokens_in']:,} tok · 压缩边界 {dist.get('compaction', 0)}",
           f"- 来源快照: `{env.get('_sid', snapshot_id(env))}` (可用 cv import --target codex|claude 精确迁移)",
           "", "## 它停在哪儿", ""]
    for b in reversed(blocks):  # 最后一条 assistant 输出 = 停点
        # 停点检索 (2026-11-02 审查 #5): 原生 Codex/Claude exporter 的普通 assistant 消息
        # 是 text 块 (role=assistant), agent_message 只是多 agent 场景的类型 — 两者都要认
        if b["type"] == "agent_message" or (b["type"] == "text" and b.get("role") == "assistant"):
            txt = (b.get("content") or "").strip()
            out += [txt[:2000] + ("…[截断]" if len(txt) > 2000 else ""), ""]
            break
    else:
        out += ["(会话中无 assistant 文本输出)", ""]
    out += ["## 最近动作 (工具调用, 末 12 条)", ""]
    calls = [b for b in blocks if b["type"] == "tool_call"][-12:]
    for b in reversed(calls):
        arg = json.dumps(b.get("arguments", {}), ensure_ascii=False)[:110]
        out.append(f"- `{b.get('name', '?')}` {arg}")
    if not calls:
        out.append("(无工具调用)")
    out += ["", "## 活上下文尾部 (最后 30 块)", ""]
    for b in blocks[-30:]:
        role = b.get("role") or b["type"]
        text = (b.get("content") or b.get("summary") or "").strip().replace("\n", " ")
        if b["type"] == "compaction":
            text = "(压缩边界) " + (b.get("summary") or "")[:150]
        if b["type"] in ("tool_call", "tool_result"):
            text = (b.get("name") or "") + " " + text[:110]
        if text:
            out.append(f"- **{role}**: {text[:220]}")
    brief = "\n".join(out)
    if len(brief) > max_chars:
        brief = brief[:max_chars] + "\n\n…[简报超限截断; 完整内容请用精确迁移 cv import]"
    return brief


def do_import(vault: Path, sid: str, target: str, apply: bool, strip_thinking=False) -> dict:
    """迁移执行核心 (CLI 与控制台 HTTP 共用)。返回 {lines, dropped, dest, new_sid, applied}。"""
    if target not in ("markdown", "codex", "claude"):
        raise BadMigrateTarget(f"未知迁移目标: {target!r} (可选 markdown|codex|claude)")
    env = load_snapshot(vault, sid)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    if target == "markdown":  # 交接简报: 可携降级档, 不写真实会话目录, 无 apply 语义
        dest = vault / "outbox" / f"brief-{sid}.md"
        lines_text = blocks_to_brief(env)
        atomic_write_text(dest, lines_text)
        return {"lines": len(lines_text.splitlines()), "dropped": 0, "dest": str(dest),
                "new_sid": None, "applied": False}
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
    atomic_write_text(dest, "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
    if apply:
        # 迁移副本登记 (见 load_migrations): 之后 sync 扫到该文件时打上 migrated_from + 首版用量
        # 基线, owned_stats 扣基线只计迁入后真实新增。codex 目标回填了源 token_usage_record →
        # 基线 = 源用量; claude 目标不回填 usage → 无基线 (只标来源)
        s = env["stats"]
        base = ({k: s.get(k, 0) for k in ("tokens_in", "tokens_out", "cache_read", "cache_write",
                                          "total_tokens")}
                if target == "codex" and s.get("usage_events") else None)
        with (vault / "migrations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"dest": _norm_path(dest), "source": sid, "baseline": base,
                                 "at": now_iso()}, ensure_ascii=False) + "\n")
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
        atomic_write_text(baseline_p, json.dumps(baseline, ensure_ascii=False, indent=1))
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
            raw = json.dumps(obj, ensure_ascii=False)
            dup = conn.execute("SELECT 1 FROM ledger_events WHERE source='headroom-savings' AND raw=?",
                               (raw,)).fetchone()
            if dup:
                continue  # 明细幂等: 重跑导入不翻倍 (lifetime 侧本就先删后插; 全面测试 H2)
            conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                         "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("headroom-savings", obj.get("ts"), "compression",
                          obj.get("model"), obj.get("source"), obj.get("client"),
                          int(obj.get("saved", 0)), float(obj.get("cost_usd", 0)), None, raw))
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
    """Vault 自有账本: 快照内嵌 usage 聚合 (零外部依赖, 人人开箱即有)。
    与 headroom lifetime (压缩/缓存节省金额) 分轨: 那是通路层增强, 这是资产层自有数据。

    口径 (2026-11-02 审查 #1): sync 对同一会话保存多个累计快照 (演进链), 全版本求和会把
    同一批 usage 逐版本重复累计。用量与 by_agent 只累计 lineage 叶子 (未被任何更新的
    parent_snapshot 引用 = 每条演进链的当前版本); fork 是独立资产副本, 计自己的叶子。
      snapshots  — 快照版本总数 (含被演进替代的历史版本)
      sessions   — 独立会话数 (叶子数; 手动 export 的无 lineage 重复会各自成链, 此口径的诚实上界)"""
    conn = db(vault)
    try:  # 只 connect 不建表的调用路径 (如 /stats 直查) 曾留下零表空库毒化冷路径 — 这里兜底建表
        conn.executescript(SCHEMA)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    leaf = ("id NOT IN (SELECT parent_snapshot FROM snapshots"
            " WHERE parent_snapshot IS NOT NULL)")
    versions = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    rows = conn.execute(f"SELECT agent, source_format, tokens_in, tokens_out, cache_read, cache_write,"
                        f" total_tokens, migration_baseline FROM snapshots WHERE {leaf}").fetchall()
    ti = to = cr = cw = tt = denom = 0
    agg = {}
    for r in rows:
        i, o, c, w, t = (int(r[k] or 0) for k in ("tokens_in", "tokens_out", "cache_read", "cache_write",
                                                   "total_tokens"))
        if r["migration_baseline"]:
            # --apply 迁移副本的首版复制了源会话用量 (codex 目标回填 token_usage_record):
            # 扣掉登记的基线, 只计迁入后真实新增, 源会话用量不再被副本重复计一次
            try:
                b = json.loads(r["migration_baseline"]) or {}
            except ValueError:
                b = {}
            i = max(0, i - int(b.get("tokens_in", 0) or 0))
            o = max(0, o - int(b.get("tokens_out", 0) or 0))
            c = max(0, c - int(b.get("cache_read", 0) or 0))
            w = max(0, w - int(b.get("cache_write", 0) or 0))
            t = max(0, t - int(b.get("total_tokens", 0) or 0))
        ti += i; to += o; cr += c; cw += w; tt += t
        # 命中率分母 = 规范化总输入 (2026-11-02 审查 #2): Claude 语义 (claude-projects 与 ingest
        # 产物) 的 tokens_in 只是未缓存输入, 总输入 = 未缓存+读+写; OpenAI 语义 (codex/zcode)
        # 的 input 已含缓存 (实证 zcode model_usage 0 行 cache_read>input), 总输入 = input
        sf = r["source_format"] or ""
        denom += (i + c + w) if (sf == "claude-projects" or sf.startswith("via-smigrate")) else i
        a = agg.setdefault(r["agent"], [0, 0])
        a[0] += 1
        a[1] += i
    hit = (cr / denom) if denom else 0.0
    return {"snapshots": versions, "sessions": len(rows), "tokens_in": ti, "tokens_out": to,
            "cache_read": cr, "cache_write": cw, "total_tokens": tt,
            "input_effective": denom,  # 规范化总输入 (各来源语义归一后可相加), 概览累计输入用它
            "cache_hit_rate": round(hit, 3),
            "by_agent": [{"agent": k, "snapshots": v[0], "sessions": v[0], "tokens_in": v[1]}
                         for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])],
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
        print(f"  缓存节省: {int(raw.get('cache_read_tokens') or 0):,} cache-read tokens = {fmt_usd(raw.get('cache_savings_usd'))}")
        print(f"  总输入:   {int(raw.get('total_input_tokens') or 0):,} tokens = {fmt_usd(raw.get('total_input_cost_usd'))}")
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
        print(f"== vault 自有账本 ({own['sessions']} 独立会话 / {own['snapshots']} 快照版本,"
              f" 零外部依赖, 用量按 lineage 叶子去重) ==")
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
        atomic_write_text(wm_p, json.dumps(wm))
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


# ------------------------------------------------- sync: 自动入库 (增量 + lineage)

SYNC_TABLE = """
CREATE TABLE IF NOT EXISTS sync_sources(
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER, snapshot_id TEXT, synced_at TEXT);
"""

SNAPSHOT_MIN_INTERVAL = int(os.environ.get("CV_SNAPSHOT_INTERVAL_MIN", "30")) * 60
SNAPSHOT_IDLE_SEC = 600


def _debounced(prev_synced_at, src_mtime: float) -> bool:
    """托盘周期同步的版本限频: 源 10 分钟内仍有更新 (活跃) 且距上次入库不足 30 分钟 → 本轮不出
    版本, 水位不推, 下轮再判; 源空闲后立即捕获最终态。托盘 2 分钟一轮, 长会话每条消息全量复制
    一版曾致 4 天 +1603 版本 / 2.98GB。CV_SNAPSHOT_INTERVAL_MIN=0 关闭。"""
    if not prev_synced_at or SNAPSHOT_MIN_INTERVAL <= 0:
        return False
    now = time.time()
    if now - src_mtime >= SNAPSHOT_IDLE_SEC:
        return False
    try:
        last = dt.datetime.fromisoformat(prev_synced_at).timestamp()
    except (TypeError, ValueError):
        return False
    return now - last < SNAPSHOT_MIN_INTERVAL


def _norm_path(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def load_migrations(vault: Path) -> dict:
    """vault/migrations.jsonl: do_import --apply 登记的迁移副本 {规范化 dest → {source, baseline}}。"""
    out = {}
    p = vault / "migrations.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                out[e["dest"]] = e
            except (ValueError, KeyError, TypeError):
                continue
    return out


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


def sync_one(conn, vault: Path, path: Path, migrations=None, debounce=False) -> str:
    """单文件入库: 新增/更新(带 lineage)/跳过。返回状态。
    debounce: 托盘周期同步传 True — 活跃会话限频出版本 (见 _debounced); 手动 cv sync 立即捕获。"""
    st = path.stat()
    row = conn.execute("SELECT mtime, size, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                       (str(path),)).fetchone()
    if row and abs(row["mtime"] - st.st_mtime) < 1 and row["size"] == st.st_size:
        return "skip"
    if debounce and row and row["snapshot_id"] and _debounced(row["synced_at"], st.st_mtime):
        return "skip"
    fmt = detect_format(first_jsonl(path), path)
    exporters = {"codex-rollout": export_codex, "claude-transcript": export_claude,
                 "claude-projects": export_claude_code}
    exported = exporters[fmt](path, False)
    envelope = build_envelope(exported)
    if exported["stats"]["blocks_total"] == 0:
        return "empty"  # 辅助文件/空会话不入库
    m = (migrations or {}).get(_norm_path(path))
    if m:  # --apply 迁移副本: 标注来源与首版用量基线 (lineage 不进内容哈希, 链上每版都带)
        envelope["lineage"]["migrated_from"] = m.get("source")
        if m.get("baseline"):
            envelope["lineage"]["migration_baseline"] = m["baseline"]
    sid = snapshot_id(envelope)
    if row and row["snapshot_id"]:
        if row["snapshot_id"] == sid:
            # 内容寻址未变 (仅 mtime 抖动/追加导出器忽略的辅助行): 链不推进, 只推水位 —
            # 先挂 parent 再算 sid 会得到 parent==id 自环, 叶子过滤把会话从账本清零
            out = vault / "snapshots" / f"{sid}.json"
            if not out.exists():  # 快照文件丢了 (如手删): 重新落盘, 无链可挂
                atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
                register_snapshot(conn, envelope, sid, out)
            conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                         " VALUES(?,?,?,?,?)", (str(path), st.st_mtime, st.st_size, sid, now_iso()))
            return "skip"
        envelope["lineage"]["parent_snapshot"] = row["snapshot_id"]  # 同源演进 → 会话树
    out = vault / "snapshots" / f"{sid}.json"
    if not out.exists():
        atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
    register_snapshot(conn, envelope, sid, out)
    conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                 " VALUES(?,?,?,?,?)", (str(path), st.st_mtime, st.st_size, sid, now_iso()))
    return "update" if row else "new"


ZCODE_DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
# model_usage 逐请求记录 (带 session_id); OpenAI 语义: input 已含 cache 读 (实证 0 行 cache_read>input)
ZCODE_USAGE_SQL = ("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                   " COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                   " COALESCE(SUM(cache_creation_input_tokens),0) FROM model_usage WHERE session_id=?")


def _zcode_usage_stats(u) -> dict:
    """ZCODE_USAGE_SQL 一行 → stats 字段 (total = 输入 + 输出; 输入已含缓存)。"""
    return {"tokens_in": u[1], "tokens_out": u[2], "reasoning_tokens": u[3], "cache_read": u[4],
            "cache_write": u[5], "total_tokens": u[1] + u[2], "usage_events": u[0]}


def export_zcode_session(db_conn, srow) -> dict:
    """Zcode 主转录库 (~/.zcode/cli/db/db.sqlite) 的单会话导出。
    结构实测: message.data{role,modelId} 按 sequence; part.data{type}: text/reasoning/tool
    (tool 自带 state.input/output 无需配对); synthetic part 过滤; signature 丢弃 (spec §3.2)。"""
    blocks, stats = [], zero_stats()
    sid = srow["id"]
    capture = {"agent": "zcode", "source_format": "zcode-sqlite",
               "provider": None, "model": None, "session_ref": sid,
               "completeness": "full"}
    if srow["directory"]:
        capture["cwd"] = srow["directory"]
    if srow["title"]:
        capture["originator"] = str(srow["title"])[:80]
    first_ts = None
    for m in db_conn.execute("SELECT id, data, time_created FROM message WHERE session_id=?"
                             " ORDER BY sequence", (sid,)):
        try:
            md = json.loads(m["data"])
        except ValueError:
            continue
        role = md.get("role")
        if role not in ("user", "assistant"):
            continue
        if not capture.get("model") and md.get("modelId"):
            capture["model"] = md.get("modelId")
        ts = dt.datetime.fromtimestamp(m["time_created"] / 1000,
                                       dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") \
            if m["time_created"] else ""
        first_ts = first_ts or ts
        for p in db_conn.execute("SELECT data FROM part WHERE message_id=? ORDER BY sequence",
                                 (m["id"],)):
            try:
                pd = json.loads(p["data"])
            except ValueError:
                continue
            pt = pd.get("type")
            if pt == "text" and not pd.get("synthetic"):
                blocks.append({"type": "text", "role": role,
                               "content": pd.get("text", "") or "", "ts": ts})
            elif pt == "reasoning":
                summary = pd.get("text", "") or ""
                if summary:
                    blocks.append({"type": "thinking", "summary": summary, "ts": ts})
            elif pt == "tool":
                state = pd.get("state") or {}
                call_id = pd.get("callID")
                name = pd.get("tool")
                inp = state.get("input")
                blocks.append({"type": "tool_call", "call_id": call_id, "name": name,
                               "namespace": None, "arguments": inp, "ts": ts})
                out = state.get("output")
                if out is not None:
                    content, _ = clip(str(out))
                    blocks.append({"type": "tool_result", "call_id": call_id,
                                   "content": content, "is_error": False, "ts": ts})
    stats["blocks_total"] = len(blocks)
    try:  # 用量: 旧适配器从不读 model_usage, zcode 快照 stats 曾全 0 (账本低估 + 详情/预览显示 0)
        u = db_conn.execute(ZCODE_USAGE_SQL, (sid,)).fetchone()
    except sqlite3.Error:
        u = None  # 老版 zcode 无 model_usage 表: 源无用量数据, 诚实留 0
    if u and u[0]:
        stats.update(_zcode_usage_stats(u))
    return {"capture": capture, "conversation": blocks, "stats": stats}


def backfill_zcode_usage(conn, force: bool = False) -> int:
    """zcode 历史快照的用量回填 (索引层, 零磁盘代价)。旧适配器未读 model_usage, 历史版本
    stats 全 0; 快照文件不可变不重写 — model_usage 逐请求带 session_id 与完成时间, 按每个版本
    的 created_at 精确算出"截至当时"的累计用量写入索引列。每 vault 一次 (vault_meta 标记)。"""
    conn.executescript(SCHEMA)
    if not force and conn.execute("SELECT 1 FROM vault_meta WHERE key='zcode_usage_backfill_v1'").fetchone():
        return 0
    n = 0
    if ZCODE_DB.exists():
        zdb = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, session_ref, created_at FROM snapshots"
                                " WHERE source_format='zcode-sqlite' AND COALESCE(usage_events,0)=0").fetchall()
            for r in rows:
                try:
                    cutoff = int(dt.datetime.fromisoformat(r["created_at"]).timestamp() * 1000)
                    u = zdb.execute(ZCODE_USAGE_SQL + " AND COALESCE(completed_at, started_at) <= ?",
                                    (r["session_ref"], cutoff)).fetchone()
                except (TypeError, ValueError, sqlite3.Error):
                    continue
                if u and u[0]:
                    st = _zcode_usage_stats(u)
                    conn.execute("UPDATE snapshots SET tokens_in=?, tokens_out=?, reasoning_tokens=?,"
                                 " cache_read=?, cache_write=?, total_tokens=?, usage_events=? WHERE id=?",
                                 (st["tokens_in"], st["tokens_out"], st["reasoning_tokens"], st["cache_read"],
                                  st["cache_write"], st["total_tokens"], st["usage_events"], r["id"]))
                    n += 1
        except sqlite3.Error:
            pass  # 无 model_usage 表: 源无数据, 照常打标记
        finally:
            zdb.close()
    conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('zcode_usage_backfill_v1', ?)", (now_iso(),))
    conn.commit()
    return n


def _sync_zcode_db(conn, vault: Path, debounce=False) -> tuple:
    """zcode 主库增量同步: session.time_updated 水位, 键 zcode:<session_id>。"""
    if not ZCODE_DB.exists():
        return (0, 0, 0)
    import sqlite3 as _s
    db_conn = _s.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
    db_conn.row_factory = _s.Row
    n_new = n_upd = 0
    for srow in db_conn.execute("SELECT * FROM session WHERE time_deleted IS NULL"
                                " ORDER BY time_updated"):
        key = f"zcode:{srow['id']}"
        mtime = (srow["time_updated"] or 0) / 1000
        prev = conn.execute("SELECT mtime, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                            (key,)).fetchone()
        if prev and mtime <= prev["mtime"]:
            continue  # 水位必须严格前进 (旧的 <1s 容差会吞掉秒内新消息的增量)
        if debounce and prev and prev["snapshot_id"] and _debounced(prev["synced_at"], mtime):
            continue  # 活跃会话限频: 30 分钟最多一版, 空闲后捕获最终态
        try:
            exported = export_zcode_session(db_conn, srow)
        except Exception:
            continue
        if exported["stats"]["blocks_total"] == 0:
            continue
        envelope = build_envelope(exported)
        sid = snapshot_id(envelope)
        if prev and prev["snapshot_id"]:
            if prev["snapshot_id"] == sid:
                # 内容未变仅 time_updated 前进: 只推水位, 不挂 parent (自环会把会话从账本清零)
                conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                             " VALUES(?,?,?,?,?)", (key, mtime, 0, sid, now_iso()))
                continue
            envelope["lineage"]["parent_snapshot"] = prev["snapshot_id"]
        out = vault / "snapshots" / f"{sid}.json"
        if not out.exists():
            atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        register_snapshot(conn, envelope, sid, out)
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (key, mtime, 0, sid, now_iso()))
        n_upd += 1 if prev else 0
        n_new += 0 if prev else 1
    db_conn.close()
    return (n_new, n_upd, 0)


def cmd_sync(args):
    vault = vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    conn.executescript(SCHEMA + SYNC_TABLE)
    n_lin = backfill_lineage(conn)       # 每 vault 一次: 历史演进链索引回填
    n_zu = backfill_zcode_usage(conn)    # 每 vault 一次: zcode 历史快照用量回填
    if n_lin or n_zu:
        print(f"  一次性修复: lineage 回填 {n_lin} 行 · zcode 用量回填 {n_zu} 行")
    migrations = load_migrations(vault)
    debounce = bool(getattr(args, "debounce", False))
    files = scan_sources(args.root)
    n_new = n_upd = n_skip = n_empty = 0
    t0 = time.time()
    for f in sorted(files):
        try:
            r = sync_one(conn, vault, f, migrations, debounce)
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
    try:
        z_new, z_upd, _ = _sync_zcode_db(conn, vault, debounce)
        n_new += z_new; n_upd += z_upd
    except Exception as e:
        print(f"  ! zcode 库同步跳过: {str(e)[:80]}")
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"sync 完成: 新增 {n_new} · 更新 {n_upd} · 跳过 {n_skip}(未变/异常)"
          f" · 空会话 {n_empty} | vault 总快照 {total} | 耗时 {time.time()-t0:.1f}s")


def cmd_prune(args):
    """中间版本 GC: 每会话每天保留最后一版 + 每会话最新版本 (叶子), 保留集按时间重链。
    默认 dry-run 只报告; --apply 先做一次 vault 备份再执行。
    恒保留: 叶子 (账本/详情口径)、fork 实体 (用户资产)、源已消失会话的整链 (vault 是唯一副本)。"""
    from types import SimpleNamespace
    vault = vault_dir(args)
    conn = db(vault)
    conn.executescript(SCHEMA + SYNC_TABLE)  # sync_sources: 源存活判定要用
    rows = conn.execute("SELECT id, agent, session_ref, created_at, path, parent_snapshot, fork_of"
                        " FROM snapshots").fetchall()
    parents = {r["parent_snapshot"] for r in rows if r["parent_snapshot"]}
    # 源存活判定 (会话级): 文件类看 sync_sources 里登记的源路径; zcode 看 session 是否还在主库
    zalive = None
    if ZCODE_DB.exists():
        z = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
        zalive = {x[0] for x in z.execute("SELECT id FROM session WHERE time_deleted IS NULL")}
        z.close()
    srcmap = {r[0]: r[1] for r in conn.execute("SELECT snapshot_id, path FROM sync_sources")}
    sess_rows = {}
    for r in rows:
        sess_rows.setdefault((r["agent"], r["session_ref"]), []).append(r)
    sess_alive = {}
    for key, lst in sess_rows.items():
        alive = False
        for r in lst:
            if key[0] == "zcode":
                alive = zalive is not None and r["session_ref"] in zalive
            else:
                sp = srcmap.get(r["id"])
                alive = bool(sp) and Path(sp).exists()
            if alive:
                break
        sess_alive[key] = alive
    dead_sessions = {k[1] for k, alive in sess_alive.items() if not alive}
    keep = set()
    for r in rows:
        if r["id"] not in parents:
            keep.add(r["id"])                                    # 叶子恒留 (账本口径)
        if r["fork_of"] or r["session_ref"] in dead_sessions:
            keep.add(r["id"])                                    # fork / 唯一副本: 整链保留
    day_last = {}
    for r in rows:
        k = (r["agent"], r["session_ref"], (r["created_at"] or "")[:10])
        if k not in day_last or (r["created_at"] or "") > (day_last[k]["created_at"] or ""):
            day_last[k] = r
    for r in day_last.values():
        keep.add(r["id"])                                        # 每会话每天最后一版 (按天记录)
    drop = [r for r in rows if r["id"] not in keep]
    reclaim = sum((os.path.getsize(r["path"]) if r["path"] and os.path.exists(r["path"]) else 0)
                  for r in drop)
    print(f"保留 {len(rows) - len(drop)} / {len(rows)} 版 (叶子 {sum(1 for r in rows if r['id'] in keep and r['id'] not in parents)} · "
          f"每日记录 {len(day_last)} · fork/孤本整链 {sum(1 for r in rows if r['fork_of'] or r['session_ref'] in dead_sessions)}) · "
          f"回收 {len(drop)} 版 / {reclaim/1e6:.0f} MB")
    if not drop:
        return
    if not args.apply:
        print("dry-run — 加 --apply 执行 (执行前自动做一次 vault 备份)")
        return
    bargs = SimpleNamespace(vault=str(vault), out=None, keep=10)
    cmd_backup(bargs)                                            # 删前完整备份 (快照+账本 zip)
    # 保留集按会话重链: 每链一个叶子 (账本 sessions 口径不因 prune 膨胀); 索引层操作, 文件不动
    kept = {}
    for r in rows:
        if r["id"] in keep:
            kept.setdefault((r["agent"], r["session_ref"]), []).append(r)
    for lst in kept.values():
        lst.sort(key=lambda r: (r["created_at"] or "", r["id"]))
        prev = None
        for r in lst:
            if r["parent_snapshot"] != prev:
                conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (prev, r["id"]))
            prev = r["id"]
    for r in drop:
        try:
            os.unlink(r["path"])  # 内容寻址文件; 索引层由下一次 rebuild 全量重建自然剔除
        except OSError:
            pass
        conn.execute("DELETE FROM snapshots WHERE id=?", (r["id"],))
    conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (now_iso(), "vault", "gc", "prune", None, None,
                  json.dumps({"dropped": len(drop), "reclaimed_mb": round(reclaim / 1e6, 1),
                              "kept": len(rows) - len(drop),
                              "backup": "vault/backups 最新一份"}, ensure_ascii=False), None))
    conn.commit()
    print(f"已回收 {len(drop)} 版 / {reclaim/1e6:.0f} MB (GC 事件已写入审计时间线)")


def cmd_repair(args):
    """一次性数据修复的显式入口 (cv sync 首次运行也会自动做): 从快照文件回填 lineage 索引列,
    zcode 历史快照按版本时间回填用量。幂等, 可重复执行。"""
    vault = vault_dir(args)
    conn = db(vault)
    n_lin = backfill_lineage(conn, force=True)
    n_zu = backfill_zcode_usage(conn, force=True)
    own = owned_stats(vault)
    print(f"lineage 回填: {n_lin} 行 · zcode 用量回填: {n_zu} 行")
    print(f"账本口径: {own['sessions']} 独立会话 / {own['snapshots']} 快照版本 · "
          f"规范化总输入 {own['input_effective']:,} tok · 命中率 {own['cache_hit_rate']*100:.1f}%")


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
    if not hasattr(args, "engine"):
        args.engine = None  # search/list 子命令未注册 --engine, 统一兜底
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
    """serve /memory 与 MCP vault_remember 共用。query 为空 = 返回最新 N 条 (概览速览)。"""
    conn = db(vault)
    conn.executescript(SCHEMA + MEMORY_TABLE)
    terms = tokenize(query)
    rows = conn.execute("SELECT * FROM memory_items").fetchall()
    if not terms:  # 速览模式: 按 id 倒序取最新, 不计命中 (浏览不污染 last_used 统计)
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [{"kind": r["kind"], "content": r["content"], "project": r["project"],
                 "snapshot": r["snapshot_id"], "score": 0} for r in rows[:limit]]
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

SMIGRATE_TARGETS = ("claude", "codex", "pi", "omp", "opencode", "copilot", "antigravity",
                    "cursor", "vibe", "muse", "qwen", "kimi", "grok", "kilo", "openhands",
                    "hermes", "mastracode", "devin")


def cmd_export_harness(args):
    """快照 → 任意 harness 会话 (写入侧桥接, 18 目标)。
    链路: 快照 → 生成 claude 会话(经 do_import, stateless replay 语义) → smigrate convert --to。
    产物落 vault/outbox/ (由用户放入目标 harness; 写入其会话目录涉及各家私有布局, 不自动落盘)。"""
    if args.to not in SMIGRATE_TARGETS:
        raise SystemExit(f"不支持的目标 {args.to}; 可选: {', '.join(SMIGRATE_TARGETS)}")
    exe = find_smigrate()
    if not exe:
        raise SystemExit("未找到 smigrate: uv tool install session-migrate")
    vault = vault_dir(args)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    # ① 快照 → claude 中转 (复用已验证的 do_import)
    mid = do_import(vault, args.snapshot_id, "claude", apply=False)
    # ② claude → 目标 harness
    ts = int(time.time())
    ext = {"claude": "jsonl", "codex": "jsonl"}.get(args.to, "json")
    out = vault / "outbox" / f"harness-{args.to}-{args.snapshot_id[:14]}-{ts}.{ext}"
    r = subprocess.run([exe, "convert", mid["dest"], "--to", args.to,
                        "--output", str(out)], capture_output=True, text=True,
                       timeout=300, creationflags=NO_WINDOW)
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"smigrate convert 失败: {(r.stderr or r.stdout)[:300]}")
    print(f"已导出 → {args.to}: {out}")
    print(f"  ({mid['lines']} 行会话, stateless replay; 按目标 harness 的方式导入该文件即可)")


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
        try:
            existing_fork_of = (json.loads(out.read_text(encoding="utf-8")).get("lineage") or {}).get("fork_of")
        except (ValueError, OSError):
            existing_fork_of = None
        if existing_fork_of != args.snapshot_id:
            # lineage (含 fork_of) 不参与内容哈希 (spec §7): 同内容 fork 会撞祖先或其他 fork,
            # fork 的 fork 曾被"已存在"静默吞掉 (全面测试 M4)。fork 的身份 = 内容 + fork_of,
            # 撞到非本源实体就确定性派生; 重复 fork 同源仍幂等
            sid = "cv_" + hashlib.sha256(
                canonical_bytes(env) + b"|fork_of:" + str(args.snapshot_id).encode()
            ).hexdigest()[:16]
            out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        print(f"该分叉已存在 (幂等): {out.name}")
    else:
        atomic_write_text(out, json.dumps(env, ensure_ascii=False, indent=1))
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
        atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
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
:root{--bg:#fafafa;--card:#ffffff;--ink:#09090b;--dim:#a1a1aa;--acc:#18181b;--good:#16a34a;--warn:#d97706}
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
.bar .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#16a34a,var(--good))}
.bar .val{width:130px;font-size:12px;color:var(--ink)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #202836}
th{color:var(--dim);font-weight:500}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.ok{background:var(--good)} .err{background:#ef4444} .warm{background:var(--warn)}
.months{display:flex;align-items:flex-end;gap:6px;height:120px}
.mcol{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:4px}
.mcol .stick{width:100%;max-width:46px;background:linear-gradient(180deg,var(--acc),#e4e4e7);border-radius:4px 4px 0 0}
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


def block_text(b: dict) -> str:
    """块的检索文本。热索引 (VaultIndex) 与冷回退 (mcp_server) 共用同一实现 —
    两侧命中集必须一致, 不允许各自维护副本 (2026-11-02 审查 #7)。"""
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
    """块级检索评分, 与热索引同语义: 词项 ∈ set(tokenize(块文本)) 计 1 分/词项/块
    (索引侧 postings 即按 set(tokenize) 落键)。冷回退必须经此函数, 不得自写匹配。"""
    tset = set(terms)
    if not tset:
        return []
    hits = []
    for i, b in enumerate(env["conversation"]):
        if not isinstance(b, dict):
            continue
        try:
            text = block_text(b)
        except (AttributeError, TypeError, KeyError, ValueError):
            continue
        if not text:
            continue
        n = len(tset & set(tokenize(text)))
        if n:
            hits.append((n, i, text))
    return hits


class VaultIndex:
    """内存倒排索引: term -> [(sid, block_idx)]; 指纹失效自动重建。"""

    def __init__(self, vault: Path, block_text_fn):
        self.vault = vault
        self.block_text = block_text_fn
        self.fingerprint = None
        # 唯一状态句柄: (index, meta, sids, mtimes) 整体换装, 并发读者一次读取永远自洽
        self._state = ({}, {}, [], {})
        self._env_cache = OrderedDict()  # sid -> env, 小 LRU (快照内容寻址不可变, 无失效问题)
        self._rebuild_lock = threading.Lock()  # 单飞: 重建期间其他请求拿现态秒回, 不排队不并发重建
        self._rebuild_pending = False          # 后台重建已在排队/执行中
        self._last_rebuild = 0.0               # monotonic; 节流基线 (库在增长时指纹永远追不上, 必须节流)
        self._last_persist = 0.0               # 缓存落盘节流 (0.4GB 写盘, 增量后 ≥PERSIST_MIN_SEC 才重写)
        self.stats = {"full": 0, "incr": 0, "cache_load": 0}  # 重建方式计数 (诊断 + 测试断言)
        if self._load_cache():
            self._last_rebuild = time.monotonic()
            self.rebuild()  # 追赶: 缓存之后的新增走增量 (毫秒级), 删除/变更自动转全量
        else:
            self.rebuild(force=True)

    # 只读视图 (兼容外部/测试); 需要多字段一致性的代码请一次读 _state
    @property
    def index(self): return self._state[0]
    @property
    def meta(self): return self._state[1]
    @property
    def _sids(self): return self._state[2]
    @property
    def _mtimes(self): return self._state[3]

    # postings 位编码: q = (sid 序号 << 24) | 块号。tuple 存 4900 万条 ≈ 4.5GB, 位编码 ≈ 0.4GB。
    # 块号 < 2**24 (单快照 1670 万块, 实际最大 ~3000), sid 序号 < 2**39 (容量充足)。
    _BLOCK_BITS = 24
    _BLOCK_MASK = (1 << 24) - 1

    ENV_CACHE_MAX = 64  # ≈64 × 数 MB 解析对象, 上限半 GB 级; 全量常驻曾是 12GB+ 内存的根源
    PERSIST_MIN_SEC = 300  # 增量后缓存重写最小间隔 (0.4GB 写盘, 别跟着 2 分钟 sync 跑)

    def load_env(self, sid):
        """按需读快照全文 (磁盘 JSON -> 解析), LRU 缓存; 不存在/损坏返回 None。"""
        env = self._env_cache.pop(sid, None)
        if env is None:
            try:
                env = json.loads((self.vault / "snapshots" / f"{sid}.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            env["_sid"] = sid
        self._env_cache[sid] = env  # 重插入 = LRU 触碰
        while len(self._env_cache) > self.ENV_CACHE_MAX:
            self._env_cache.popitem(last=False)
        return env

    def _scan(self):
        """(指纹, {stem: file}, {stem: mtime}) — 一次 glob 供指纹与差分共用。"""
        files = {f.stem: f for f in sorted((self.vault / "snapshots").glob("cv_*.json"))}
        mtimes = {}
        for sid, f in files.items():
            try:
                mtimes[sid] = int(f.stat().st_mtime)
            except OSError:
                continue  # glob 与 stat 之间被删: 本轮按不存在处理, 存活即下轮增量追赶 (全面测试 M9)
        fp = hashlib.sha256("|".join(f"{sid}:{m}" for sid, m in sorted(mtimes.items())).encode()).hexdigest()
        return fp, files, mtimes

    def _index_one(self, f, meta, index, sids, mtimes):
        """解析并索引单个快照。可见顺序是硬约束: 先 meta (healthz/recent 口径),
        再 sids (postings 解码表), 最后 postings — 并发读者任何时刻都只见完整条目。
        结构不完整 (缺 capture/conversation/stats) 或类型异常的快照整体跳过, 不拖垮重建。"""
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            capture, conv, stats = env["capture"], env["conversation"], env["stats"]
            meta_row = {"agent": capture.get("agent"),
                        "first": next(((b.get("content") or b.get("summary") or "")[:64]
                                       for b in conv
                                       if b.get("role") == "user"
                                       and (b.get("content") or b.get("summary"))), ""),
                        "created_at": env.get("created_at"),
                        "cwd": capture.get("cwd"),
                        "blocks": stats["blocks_total"],
                        "tokens_in": stats["tokens_in"],
                        "completeness": capture.get("completeness")}
        except (ValueError, OSError, KeyError, TypeError):
            return False
        sid = f.stem
        meta[sid] = meta_row
        sid_code = len(sids) << self._BLOCK_BITS
        sids.append(sid)
        try:
            mtimes[sid] = int(f.stat().st_mtime)
        except OSError:
            mtimes[sid] = 0  # 解析期间被删: 内容已读入照常入索引, 下轮指纹自然剔除 (M9 同族)
        for i, b in enumerate(conv):
            if not isinstance(b, dict):
                continue
            try:
                text = self.block_text(b)
            except (AttributeError, TypeError, KeyError, ValueError):
                continue
            for t in set(tokenize(text)):
                arr = index.get(t)
                if arr is None:
                    arr = index[t] = array("q")
                arr.append(sid_code | i)
        return True

    _CACHE_VER = 1

    def _persist(self, force=False):
        """索引缓存落盘: 启动秒级加载 + 增量追赶, 免 3-4 分钟冷建。失败静默 (纯加速件),
        但要清掉半截 tmp、且失败不白吃 300s 节流 (30s 后即可重试)。"""
        now = time.monotonic()
        if not force and now - self._last_persist < self.PERSIST_MIN_SEC:
            return
        tmp = None
        try:
            import pickle
            index, meta, sids, mtimes = self._state  # 一次读取, 与换装原子对齐
            p = self.vault / "index_cache.pkl"
            tmp = p.parent / (p.name + f".{os.getpid()}.tmp")
            with open(tmp, "wb") as fh:
                pickle.dump({"ver": self._CACHE_VER, "fp": self.fingerprint,
                             "meta": meta, "index": index,
                             "sids": sids, "mtimes": mtimes}, fh, protocol=4)
            os.replace(tmp, p)  # 原子换文件, 并发读者 (其他实例) 不见半截
            self._last_persist = now
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)  # 失败不留半截残骸
                except OSError:
                    pass
            self._last_persist = now - self.PERSIST_MIN_SEC + 30  # 30s 后即可重试, 不白吃整个节流窗

    def _load_cache(self):
        """启动加载索引缓存。只做结构与版本校验, 指纹可以落后 —— 落后部分由随后的
        rebuild() 增量追赶 (纯新增毫秒级); 任何加载失败 (含损坏/截断的 pkl) 一律回退全量。"""
        try:
            import pickle
            p = self.vault / "index_cache.pkl"
            if not p.exists():
                return False
            with open(p, "rb") as fh:
                d = pickle.load(fh)
            if d.get("ver") != self._CACHE_VER or not isinstance(d.get("index"), dict):
                return False
            if not isinstance(d.get("meta"), dict) or len(d.get("sids") or []) != len(d["meta"]):
                return False
            self._state = (d["index"], d["meta"], d["sids"], d["mtimes"])
            self.fingerprint = d["fp"]  # 缓存时刻的指纹; rebuild() 以此差分追赶
            self.stats["cache_load"] += 1
            return True
        except Exception:  # 纯加速件: 损坏/截断 pkl (UnpicklingError) 等任何意外都回退全量重建
            return False

    def rebuild(self, force=False, stale_after=0.0):
        """stale_after>0: 距上次重建不足该秒数时直接返回现态 (热路径防重建风暴);
        已有重建在跑时非阻塞跳过。快照内容寻址只增不改 → 指纹变化若纯为新增,
        走增量追加 (毫秒级); 删除/变更才全量重建。"""
        now = time.monotonic()
        if not force and stale_after and now - self._last_rebuild < stale_after:
            return
        if not self._rebuild_lock.acquire(blocking=False):
            return
        try:
            fp, files, mtimes = self._scan()
            if not force and fp == self.fingerprint:
                return
            index, meta, sids, mtimes_known = self._state  # 一次读取: 增量在现容器上只追加 (单调, 无撕裂)
            added = [f for sid, f in files.items() if sid not in mtimes_known]
            dirty = (set(mtimes_known) - files.keys()) or \
                    any(mtimes[s] != mtimes_known[s] for s in mtimes_known if s in mtimes)
            if not force and self.fingerprint is not None and not dirty and added:
                for f in added:  # 增量: 只解析新文件, 原地追加 (快照不可变, 老条目永不失效)
                    self._index_one(f, meta, index, sids, mtimes_known)
                self.fingerprint = fp
                self.stats["incr"] += 1
                self._persist()
                return
            # 全量: staging 建好后对 _state 单赋值换装 — 读者一次读 _state, 任何时刻自洽不撕裂
            new_index, new_meta, new_sids, new_mtimes = {}, {}, [], {}
            for f in files.values():
                self._index_one(f, new_meta, new_index, new_sids, new_mtimes)
            self._state = (new_index, new_meta, new_sids, new_mtimes)
            self.fingerprint = fp
            self.stats["full"] += 1
            self._persist(force=True)
        finally:
            self._last_rebuild = time.monotonic()  # 失败也计时, 别让坏请求反复触发
            self._rebuild_lock.release()

    def request_rebuild(self, stale_after=60.0):
        """热路径 (/healthz, /recent) 专用: 节流 + 后台单飞重建, 本请求立即返回现态。
        库在持续增长 (托盘每 2 分钟 sync) 时指纹永远追不上, 同步重建会让请求线程挂死数分钟。"""
        now = time.monotonic()
        if now - self._last_rebuild < stale_after:
            return
        self._last_rebuild = now  # 排队即计时, 防 N 个请求同时排满
        if self._rebuild_pending:
            return
        self._rebuild_pending = True
        def _bg():
            try:
                self.rebuild()  # 指纹门控; 已有重建在跑则单飞锁直接跳过
            finally:
                self._rebuild_pending = False
        threading.Thread(target=_bg, daemon=True).start()

    def search(self, query: str, limit: int) -> dict:
        from collections import Counter
        terms = tokenize(query)
        index, meta, sids = self._state[:3]  # 单次读取, 全量换装期间不见撕裂
        sc = Counter()
        for t in set(terms):  # 与冷回退 score_blocks 同语义: 每词项每块只计 1 分
            for v in index.get(t, ()):
                sc[sids[v >> self._BLOCK_BITS]] += 1
        # 并列得分必须与冷回退同序 (冷: created_at 倒序稳定, 同 created_at 按文件序) —
        # most_common 的插入序受哈希种子影响, limit 截断边界曾冷热命中不同 (全面测试 M7)
        pairs = sorted(sc.items())
        pairs.sort(key=lambda kv: meta[kv[0]].get("created_at") or "", reverse=True)
        pairs.sort(key=lambda kv: -kv[1])
        return {"query": query, "terms": terms,
                "hits": [{"sid": sid, "score": n, **meta[sid]}
                         for sid, n in pairs[:limit]]}

    def context(self, query: str, limit: int) -> dict:
        from collections import Counter
        terms = tokenize(query)
        index, meta, sids = self._state[:3]  # 单次读取, 全量换装期间不见撕裂
        pairs = Counter()
        for t in set(terms):  # 与冷回退 score_blocks 同语义: 每 (词项, 块) 计 1 分
            for v in index.get(t, ()):
                pairs[(sids[v >> self._BLOCK_BITS], v & self._BLOCK_MASK)] += 1
        ordered = sorted(pairs.items(), key=lambda kv: kv[0])            # (sid, 块号) 升序兜底
        ordered.sort(key=lambda kv: meta[kv[0][0]].get("created_at") or "", reverse=True)
        ordered.sort(key=lambda kv: -kv[1])                              # 得分降序 (稳定)
        out, seen = [], set()
        for (sid, i), n in ordered:
            if len(out) >= limit:
                break
            if sid in seen:
                continue
            seen.add(sid)
            env = self.load_env(sid)
            if env is None:
                continue
            b = env["conversation"][i]
            text = self.block_text(b)
            pos = text.lower().find(terms[0]) if terms else 0
            start = max(0, pos - 200)
            out.append({"sid": sid, "block": i, "role": b.get("role") or b["type"],
                        "score": n, "excerpt": text[start:start + 400],
                        "cwd": meta[sid].get("cwd")})
        return {"query": query, "terms": terms, "results": out}


CONSOLE_HTML = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Context Vault 控制台</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E%3Crect x='2.5' y='2.5' width='15' height='15' rx='3.5' fill='none' stroke='%230e7568' stroke-width='1.7'/%3E%3Crect x='6.5' y='8' width='7' height='6' rx='1' fill='%230e7568'/%3E%3Cpath d='M7.5 8V6.5a2.5 2.5 0 0 1 5 0V8' fill='none' stroke='%230e7568' stroke-width='1.7' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
/* ================= 设计 tokens: 色彩 / 间距 4-8px / 字号 / 圆角 / 边框 / 阴影 / 控件高度 ================= */
:root{
  --bg-canvas:#f2f3f5; --bg-surface:#fff; --bg-subtle:#f7f8fa; --bg-inset:#edeff2;
  --line:#e3e6ea; --line-soft:#ecedef; --line-strong:#d3d7dc;
  --ink:#191c1f; --ink-2:#45494e; --ink-3:#6e747b;
  --acc:#0e7568; --acc-strong:#0b5d53; --acc-soft:#e4f1ee; --acc-line:#bfdfd8; --acc-ink:#fff;
  --warn:#a16207; --warn-bg:#fdf6e7; --warn-line:#ecd9a8;
  --err:#b42323; --err-bg:#fdf0ef; --err-line:#efc5c2;
  --sans:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
  --mono:"Cascadia Code",Consolas,"SF Mono",ui-monospace,monospace;
  --fs-xs:12px; --fs-sm:12.5px; --fs-md:14px; --fs-lg:15px;
  --rad-s:6px; --rad-m:10px; --rad-l:14px;
  --ctl-h:34px;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px;
  --shadow-1:0 1px 2px rgba(16,20,24,.05);
  --shadow-2:0 10px 30px rgba(16,20,24,.16);
  --sidebar-w:224px; --topbar-h:52px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:light}
body{background:var(--bg-canvas);color:var(--ink);font:var(--fs-md)/1.6 var(--sans);
  font-feature-settings:"tnum";-webkit-font-smoothing:antialiased;
  display:grid;grid-template-columns:var(--sidebar-w) minmax(0,1fr);height:100vh;overflow:hidden}
::selection{background:var(--acc-soft)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:5px;border:2px solid var(--bg-canvas)}
::-webkit-scrollbar-thumb:hover{background:#c3c8ce}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:4px}
button,input,select{font:inherit;color:inherit}
svg{flex:none}

/* ================= 左侧导航 ================= */
.sidebar{background:var(--bg-subtle);border-right:1px solid var(--line);
  display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:10px;padding:0 16px;height:var(--topbar-h);
  border-bottom:1px solid var(--line-soft);font-weight:650;font-size:var(--fs-lg);letter-spacing:.01em}
.side-nav{padding:10px 8px;display:flex;flex-direction:column;gap:2px}
.side-nav button{display:flex;align-items:center;gap:10px;width:100%;background:none;border:none;
  color:var(--ink-2);padding:8px 12px;border-radius:var(--rad-s);cursor:pointer;font-size:13.5px;
  text-align:left;position:relative;transition:background .12s ease,color .12s ease}
.side-nav button:hover{background:var(--bg-inset);color:var(--ink)}
.side-nav button.on{background:var(--acc-soft);color:var(--acc-strong);font-weight:600}
.side-nav button.on::before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:3px;
  border-radius:0 3px 3px 0;background:var(--acc)}
.side-nav .kbd{margin-left:auto;font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3);
  border:1px solid var(--line);border-radius:4px;padding:0 5px;line-height:18px;background:var(--bg-surface)}
.side-foot{margin-top:auto;padding:12px 14px 14px;border-top:1px solid var(--line-soft);
  font-size:var(--fs-xs);color:var(--ink-3);display:flex;flex-direction:column;gap:6px}
.side-foot .health-line{display:flex;align-items:center;gap:7px;color:var(--ink-2)}
.side-foot .health-line b{font-family:var(--mono);font-weight:600;color:var(--ink)}
.privacy{display:flex;align-items:center;gap:6px;line-height:1.5}

/* ================= 顶栏: 当前分区 / 全局搜索 / 服务状态 ================= */
.workspace{display:flex;flex-direction:column;min-width:0;min-height:0}
.topbar{height:var(--topbar-h);flex:none;display:flex;align-items:center;gap:14px;
  padding:0 20px;background:var(--bg-surface);border-bottom:1px solid var(--line)}
.topbar h1{font-size:var(--fs-lg);font-weight:650;white-space:nowrap}
.gsearch{flex:1;max-width:520px;margin:0 auto;position:relative;display:flex}
.gsearch input[type=search]{width:100%;height:var(--ctl-h);background:var(--bg-subtle);border:1px solid var(--line);
  border-radius:var(--rad-m);padding:0 12px 0 32px;font-size:13.5px;outline:none;
  transition:border-color .12s ease,background .12s ease}
.gsearch input[type=search]:focus{border-color:var(--acc);background:var(--bg-surface)}
.gsearch .gs-ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--ink-3);pointer-events:none}
.gsearch kbd{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-family:var(--mono);
  font-size:11.5px;color:var(--ink-3);border:1px solid var(--line);border-radius:4px;
  padding:0 5px;background:var(--bg-surface);pointer-events:none}
.status-pill{display:inline-flex;align-items:center;gap:8px;font-size:var(--fs-xs);color:var(--ink-2);
  border:1px solid var(--line);border-radius:999px;padding:4px 12px;background:var(--bg-subtle);white-space:nowrap}
.status-pill .pulse{width:7px;height:7px;border-radius:50%;background:var(--acc);
  box-shadow:0 0 0 0 rgba(14,117,104,.4);animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(14,117,104,.35)}70%{box-shadow:0 0 0 6px rgba(14,117,104,0)}100%{box-shadow:0 0 0 0 rgba(14,117,104,0)}}
.status-pill.err{color:var(--err);border-color:var(--err-line);background:var(--err-bg)}
.status-pill.err .pulse{background:var(--err);animation:none}
.status-pill.auth{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.status-pill.auth .pulse{background:var(--warn);animation:none}

.content{flex:1;overflow-y:auto;padding:22px 28px 64px}
.content-inner{max-width:1400px;margin:0 auto}
section{animation:fadein .16s ease-out}
@keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}

/* ================= 通用元素 ================= */
.card{background:var(--bg-surface);border:1px solid var(--line);border-radius:var(--rad-m);
  box-shadow:var(--shadow-1)}
.card>.card-h{display:flex;align-items:center;gap:8px;padding:12px 16px;
  font-size:var(--fs-xs);font-weight:600;color:var(--ink-3);letter-spacing:.04em;
  border-bottom:1px solid var(--line-soft)}
.card>.card-h .sub{font-weight:400;letter-spacing:0}
.card>.card-b{padding:6px 8px}
.src{font-size:var(--fs-xs);color:var(--ink-3)}
.hint{color:var(--ink-3);font-size:var(--fs-sm)}
.mono{font-family:var(--mono);font-size:var(--fs-sm)}
.nowr{white-space:nowrap}
.num{font-variant-numeric:tabular-nums}
.tbl-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:var(--fs-xs);font-weight:600;color:var(--ink-2);text-align:left;letter-spacing:.03em;
  padding:8px 12px;border-bottom:1px solid var(--line);background:var(--bg-subtle);white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid var(--line-soft);vertical-align:middle}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{font-family:var(--mono);font-size:var(--fs-sm)}
.tbl-44 td{height:46px}
.linklike{background:none;border:none;padding:0;cursor:pointer;text-align:left}
.sid{color:var(--acc-strong);font-family:var(--mono);font-size:var(--fs-sm)}
.sid:hover{text-decoration:underline;text-underline-offset:3px}
.chip{display:inline-block;font-size:var(--fs-xs);padding:1px 8px;border-radius:5px;
  background:var(--bg-subtle);color:var(--ink-2);border:1px solid var(--line);white-space:nowrap}
.chip.warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.chip.ok{color:var(--acc-strong);background:var(--acc-soft);border-color:var(--acc-line)}
.chip.err{color:var(--err);background:var(--err-bg);border-color:var(--err-line)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px;vertical-align:1px}
.dot.ok{background:var(--acc)} .dot.err{background:var(--err)} .dot.warn{background:var(--warn)} .dot.sw{background:var(--warn)} .dot.off{background:var(--line-strong)}
.pill{display:inline-block;font-size:var(--fs-xs);padding:1px 8px;border-radius:999px;font-weight:600;font-family:var(--mono)}
.pill.ok{color:var(--acc-strong);background:var(--acc-soft)}
.pill.err{color:var(--err);background:var(--err-bg)}
.pill.warn{color:var(--warn);background:var(--warn-bg)}
.empty{padding:32px 20px;text-align:center;color:var(--ink-3);font-size:13px;line-height:1.9}
.empty code,.hint code{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-2);
  background:var(--bg-subtle);border:1px solid var(--line-soft);padding:1px 6px;border-radius:5px;
  overflow-wrap:anywhere}  /* 空态里的长环境变量示例曾把 380px 视口撑到 681px (全面测试 console-3) */
.empty .btn{margin-top:12px}
.skel{height:13px;border-radius:5px;background:linear-gradient(90deg,var(--bg-inset) 25%,var(--line-soft) 50%,var(--bg-inset) 75%);
  background-size:200% 100%;animation:shimmer 1.3s linear infinite;margin:10px 0}
@keyframes shimmer{to{background-position:-200% 0}}
.dir{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3);white-space:nowrap;
  display:inline-flex;align-items:center;gap:5px;max-width:100%;overflow:hidden}
.dir span{overflow:hidden;text-overflow:ellipsis}

/* 按钮 / 表单 */
.btn{display:inline-flex;align-items:center;gap:7px;background:var(--bg-surface);border:1px solid var(--line-strong);
  border-radius:var(--rad-s);padding:0 14px;height:var(--ctl-h);font-size:13.5px;cursor:pointer;
  transition:border-color .12s ease,background .12s ease,transform .08s ease;white-space:nowrap}
.btn:hover{border-color:#bfc5cb;background:var(--bg-subtle)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--acc);border-color:var(--acc);color:var(--acc-ink);font-weight:600}
.btn.primary:hover{background:var(--acc-strong);border-color:var(--acc-strong)}
.btn.danger{background:var(--bg-surface);border-color:var(--err-line);color:var(--err)}
.btn.danger:hover{background:var(--err-bg);border-color:var(--err)}
.btn.sm{height:28px;padding:0 10px;font-size:var(--fs-xs)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
input[type=search],select{background:var(--bg-surface);border:1px solid var(--line-strong);color:var(--ink);
  border-radius:var(--rad-s);height:var(--ctl-h);padding:0 12px;font-size:13.5px;outline:none;
  transition:border-color .12s ease}
input[type=search]::placeholder{color:var(--ink-3)}
input[type=search]:focus,select:focus{border-color:var(--acc)}
select{appearance:none;-webkit-appearance:none;padding-right:30px;cursor:pointer;
  min-width:0;width:100%;max-width:100%;  /* option 文本不可断行, width:auto 曾按文本撑爆窄视口 (全面测试 M11 复核补充) */
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%236e747b' stroke-width='2.5' stroke-linecap='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 10px center}

/* KPI */
.kpi-band{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:var(--bg-surface);border:1px solid var(--line);border-radius:var(--rad-m);
  padding:14px 16px;min-width:0;box-shadow:var(--shadow-1)}
.kpi .v{font-family:var(--mono);font-size:22px;font-weight:650;letter-spacing:-.01em;line-height:1.25;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .v small{font-size:13px;font-weight:500;color:var(--ink-3);margin-left:4px}
.kpi .exact{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .k{font-size:var(--fs-xs);color:var(--ink-3);margin-top:6px;line-height:1.45;cursor:help}
.kpi-bad{color:var(--err)}
tr.rowlink{cursor:pointer}
tr.rowlink:hover{background:var(--bg-subtle)}
.quick-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}
.quick{display:flex;align-items:center;gap:12px;background:var(--bg-surface);border:1px solid var(--line);
  border-radius:var(--rad-m);padding:14px 16px;cursor:pointer;text-align:left;width:100%;
  transition:border-color .12s ease,box-shadow .12s ease;box-shadow:var(--shadow-1)}
.quick:hover{border-color:var(--acc-line);box-shadow:0 2px 8px rgba(14,117,104,.08)}
.quick .q-ic{width:34px;height:34px;border-radius:var(--rad-s);background:var(--acc-soft);color:var(--acc-strong);
  display:flex;align-items:center;justify-content:center;flex:none}
.quick .q-t{font-weight:600;font-size:13.5px}
.quick .q-d{font-size:var(--fs-xs);color:var(--ink-3);margin-top:1px;line-height:1.45}
.quick .q-go{margin-left:auto;color:var(--ink-3)}

/* 布局网格 */
.ov-grid{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:12px;margin-top:14px;align-items:start}
.ov-side{display:flex;flex-direction:column;gap:12px}
.row{display:flex;justify-content:space-between;gap:10px;font-size:var(--fs-sm);padding:7px 12px;
  border-bottom:1px solid var(--line-soft)}
.row:last-child{border-bottom:none}
.row .l{color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.row .r{color:var(--ink-2);white-space:nowrap;font-family:var(--mono);font-size:var(--fs-xs)}
details.card>summary{list-style:none;display:flex;align-items:center;gap:8px;padding:12px 16px;cursor:pointer;
  font-size:var(--fs-xs);font-weight:600;color:var(--ink-3);letter-spacing:.04em;user-select:none}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary .tw{transition:transform .15s ease;color:var(--ink-3)}
details.card[open]>summary .tw{transform:rotate(90deg)}
details.card .card-b{border-top:1px solid var(--line-soft)}
.foot{display:flex;justify-content:space-between;align-items:center;padding:9px 16px;
  border-top:1px solid var(--line-soft);font-size:var(--fs-xs);color:var(--ink-2)}
.foot a{cursor:pointer;color:var(--acc-strong)}
.foot a:hover{text-decoration:underline;text-underline-offset:3px}
.kv{display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 14px;font-size:13px}
.kv b{color:var(--ink-3);font-weight:500;white-space:nowrap}
.kv span{font-variant-numeric:tabular-nums;min-width:0;overflow-wrap:anywhere}
.section-note{font-size:var(--fs-xs);color:var(--ink-3);line-height:1.7;margin-top:10px}

/* 月度走势 */
.months{display:flex;gap:10px;height:150px;padding:24px 4px 0;max-width:680px}
.yax{width:52px;flex:none;display:flex;flex-direction:column;justify-content:space-between;align-items:flex-end;
  font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3);font-variant-numeric:tabular-nums;padding-bottom:18px}
.bars{flex:1;display:flex;gap:6px;height:100%;
  background:repeating-linear-gradient(to top,transparent 0 36px,var(--line-soft) 36px 37px)}
.mcol{flex:1;position:relative;min-width:0}
.mcol .val{position:absolute;left:50%;transform:translateX(-50%);
  /* 100% 高柱子的标签会逃出容器上沿压到卡片标题 — clamp 在容器顶内 */
  bottom:min(calc(22px + var(--h,50)*.96%),calc(100% - 4px));
  font-family:var(--mono);font-size:11.5px;color:var(--ink-2);white-space:nowrap;font-variant-numeric:tabular-nums}
.mcol .stick{position:absolute;left:50%;bottom:22px;width:32px;max-width:60%;height:3%;
  max-height:calc(100% - 26px);background:var(--acc);border-radius:4px 4px 0 0;transform:translateX(-50%);
  transform-origin:bottom;animation:grow .5s cubic-bezier(.2,.7,.3,1) backwards;animation-delay:calc(var(--i,0)*40ms)}
@keyframes grow{from{transform:translateX(-50%) scaleY(0)}}
.mcol .lb{position:absolute;left:0;right:0;bottom:0;text-align:center;font-size:11.5px;color:var(--ink-3);
  font-variant-numeric:tabular-nums}

/* 表格容器 (卡片化) */
.cardbox{border:1px solid var(--line);border-radius:var(--rad-m);background:var(--bg-surface);
  box-shadow:var(--shadow-1);overflow:clip}
.cardbox .tbl-wrap{overflow-x:auto}
.cardbox th{background:var(--bg-subtle)}
.sum-line{display:flex;align-items:center;gap:6px;overflow:hidden;white-space:nowrap;height:100%;min-width:0}
.sum-main{overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}
.mini-tag{flex:none;font-family:var(--mono);font-size:11.5px;color:var(--ink-3);
  background:var(--bg-subtle);border:1px solid var(--line-soft);border-radius:4px;padding:0 5px;line-height:16px}
.relay-badge{display:inline-flex;align-items:center;gap:6px;background:var(--bg-subtle);
  border:1px solid var(--line);border-radius:6px;padding:2px 8px;line-height:1.5;vertical-align:middle}
.relay-badge .rl{color:var(--ink-3);font-size:11.5px}
.relay-badge .rn{font-family:var(--mono);color:var(--ink);font-weight:600;font-size:var(--fs-xs)}
.lat{font-family:var(--mono);font-size:var(--fs-sm);white-space:nowrap}
.lat.good{color:var(--acc-strong)}.lat.mid{color:var(--ink-2)}.lat.warm{color:var(--warn)}
.latbar{width:52px;height:3px;background:var(--bg-inset);border-radius:2px;overflow:hidden;
  display:inline-block;vertical-align:middle;margin-left:8px}
.latbar i{display:block;height:100%;background:currentColor;border-radius:2px}

/* 控件行 */
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.controls input[type=search]{flex:1;min-width:240px;padding-left:32px;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%236e747b' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m20 20-3.5-3.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:10px center}
.meta-line{display:flex;align-items:center;gap:10px;font-size:var(--fs-xs);color:var(--ink-3);margin:0 2px 10px}
.meta-line .xbtn{background:none;border:none;color:var(--acc-strong);cursor:pointer;font-size:var(--fs-xs);
  padding:2px 6px;border-radius:4px}
.meta-line .xbtn:hover{background:var(--acc-soft)}
.pager{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 2px;
  font-size:var(--fs-sm);color:var(--ink-2)}
.pager .btn{height:28px;padding:0 12px;font-size:var(--fs-xs)}

/* 迁移工作台 */
.mig-wrap{display:flex;flex-direction:column;gap:12px}
.mig-steps{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}
.mig-step{background:var(--bg-surface);border:1px solid var(--line);border-radius:var(--rad-m);box-shadow:var(--shadow-1)}
.mig-step>.card-h .stepno{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;
  border-radius:50%;background:var(--acc);color:var(--acc-ink);font-size:11.5px;font-weight:700;font-family:var(--mono)}
.mig-step .card-b{padding:12px 14px}
.tgt-cards{display:flex;flex-direction:column;gap:8px}
.tgt-card{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:var(--rad-s);
  padding:10px 12px;cursor:pointer;transition:border-color .12s ease,background .12s ease;background:var(--bg-surface)}
.tgt-card:hover{border-color:var(--line-strong)}
.tgt-card.on{border-color:var(--acc);background:var(--acc-soft)}
.tgt-card input{margin-top:3px;accent-color:var(--acc)}
.tgt-card .tt{font-weight:600;font-size:13.5px}
.tgt-card .td{font-size:var(--fs-xs);color:var(--ink-3);margin-top:1px;line-height:1.5}
.apply-toggle{display:flex;gap:8px;margin-top:10px}
.apply-toggle button{flex:1;display:flex;flex-direction:column;align-items:flex-start;gap:2px;
  border:1px solid var(--line);background:var(--bg-surface);border-radius:var(--rad-s);padding:8px 12px;cursor:pointer}
.apply-toggle button .at-t{font-size:13px;font-weight:600}
.apply-toggle button .at-d{font-size:11.5px;color:var(--ink-3);line-height:1.4}
.apply-toggle button.on{border-color:var(--acc);background:var(--acc-soft)}
.apply-toggle button.on.risky{border-color:var(--err);background:var(--err-bg)}
.apply-toggle button.on.risky .at-t{color:var(--err)}
.sel-meta{margin-top:8px;font-size:var(--fs-xs);color:var(--ink-2);line-height:1.6}
.sel-meta .mono{font-size:11.5px}
.immlist{list-style:none;padding:4px 0}
.immlist li{display:flex;gap:8px;font-size:var(--fs-sm);color:var(--ink-2);padding:4px 0;line-height:1.55}
.immlist li svg{flex:none;margin-top:3px}
.immlist li.good{color:var(--acc-strong)}
/* 会话时间线 (抽屉): 按天分组的版本链 */
.tl-day{padding:4px 0;border-bottom:1px solid var(--line-soft)}
.tl-day:last-child{border-bottom:none}
.tl-d{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3)}
.tl-row{display:flex;align-items:center;gap:8px;font-size:var(--fs-sm);padding:3px 0;color:var(--ink-2)}
.tl-row.cur{color:var(--ink);font-weight:600}
.term{background:#101418;color:#dfe5ea;border-radius:var(--rad-s);padding:10px 14px;
  font-family:var(--mono);font-size:var(--fs-sm);margin-top:12px;line-height:1.7;overflow-x:auto;white-space:nowrap}
.term .tp{display:block;color:#8b93a3;font-size:11.5px;letter-spacing:.06em;margin-bottom:3px;
  font-family:var(--sans);white-space:normal}
.term code{color:#7ee2a8}
.exec-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.mig-idle{border:1px dashed var(--line-strong);border-radius:var(--rad-m);padding:26px 20px;text-align:center;
  color:var(--ink-3);font-size:13px;line-height:1.9;background:var(--bg-surface)}
.mig-idle code{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-2);
  background:var(--bg-subtle);padding:2px 7px;border-radius:5px}
.result-card{border:1px solid var(--acc-line);background:var(--acc-soft);border-radius:var(--rad-m);padding:14px 16px}
.result-card.err{border-color:var(--err-line);background:var(--err-bg)}
.result-card .rt{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13.5px;margin-bottom:6px}
.result-card .path-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0}

/* 哨兵 */
.stat-inline{display:inline-flex;align-items:baseline;gap:6px;font-size:var(--fs-sm);color:var(--ink-2);
  padding:6px 12px;background:var(--bg-surface);border:1px solid var(--line);border-radius:var(--rad-s)}
.stat-inline b{font-family:var(--mono);font-weight:650;font-variant-numeric:tabular-nums;color:var(--ink)}
.probe-state{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;
  font-size:13px;font-weight:600;border:1px solid var(--line)}
.probe-state.off{color:var(--ink-3);background:var(--bg-subtle)}
.probe-state.ok{color:var(--acc-strong);background:var(--acc-soft);border-color:var(--acc-line)}
.probe-state.err{color:var(--err);background:var(--err-bg);border-color:var(--err-line)}
.probe-state.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-line)}

/* 抽屉 */
.backdrop{position:fixed;inset:0;background:rgba(16,20,24,.32);opacity:0;transition:opacity .2s ease;z-index:40}
.backdrop.show{opacity:1}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(480px,94vw);background:var(--bg-surface);
  border-left:1px solid var(--line);z-index:41;overflow-y:auto;padding:20px 24px 28px;
  transform:translateX(28px);opacity:0;transition:transform .22s cubic-bezier(.2,.7,.3,1),opacity .18s ease;
  box-shadow:var(--shadow-2)}
.drawer.show{transform:none;opacity:1}
.dw-head{display:flex;align-items:flex-start;gap:12px}
.dw-head h3{font-size:var(--fs-lg);font-weight:650;margin-top:2px}
.dw-sub{font-family:var(--mono);font-size:var(--fs-xs);color:var(--ink-3);word-break:break-all;margin:2px 0 0}
.dw-close{margin-left:auto;flex:none;background:none;border:1px solid transparent;border-radius:6px;
  color:var(--ink-3);cursor:pointer;padding:5px;display:inline-flex}
.dw-close:hover{color:var(--ink);border-color:var(--line);background:var(--bg-inset)}
.dw-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:14px 0 2px}
.drawer h3.sec{font-size:var(--fs-xs);font-weight:600;color:var(--ink-3);letter-spacing:.04em;margin:20px 0 8px}
.dw-actions{display:flex;gap:10px;margin-top:22px;padding-top:16px;border-top:1px solid var(--line-soft)}
.dw-actions .btn{flex:1;justify-content:center}

/* toast */
#toast{position:fixed;bottom:24px;right:24px;background:var(--bg-surface);border:1px solid var(--line);
  border-radius:var(--rad-m);padding:12px 18px;max-width:440px;font-size:13px;line-height:1.6;
  box-shadow:var(--shadow-2);transform:translateY(8px);opacity:0;transition:transform .16s ease,opacity .16s ease;
  pointer-events:none;z-index:50}
#toast.show{transform:none;opacity:1}
#toast.err{border-color:var(--err-line)}

/* 动效偏好 */
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}
}

/* 响应式: 窄屏收起侧栏 → 顶部横排 */
@media(max-width:900px){
  body{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);height:auto;min-height:100vh;overflow:auto}
  /* 裸 1fr 的 auto 最小尺寸曾让收起后的侧栏行 (brand+nav ~456px) 撑开整页横滚;
     minmax(0,1fr) 让 .sidebar 的 overflow-x:auto 真正在栏内滚动 */
  .sidebar{flex-direction:row;align-items:center;border-right:none;border-bottom:1px solid var(--line);
    overflow-x:auto;padding:0 8px}
  .brand{border-bottom:none;padding:0 10px 0 6px;height:48px}
  .side-nav{flex-direction:row;padding:6px 4px}
  .side-nav button{width:auto;padding:7px 12px;white-space:nowrap}
  .side-nav button.on::before{left:8px;right:8px;top:auto;bottom:0;width:auto;height:3px;border-radius:3px 3px 0 0}
  .side-nav .kbd{display:none}
  .side-foot{display:none}
  .content{overflow:visible;padding:16px 14px 56px}
  .topbar{padding:0 12px;gap:10px}
  .topbar h1{display:none}
  .gsearch{max-width:none}
  .kpi-band,.quick-row,.mig-steps{grid-template-columns:minmax(0,1fr)}
  .ov-grid{grid-template-columns:minmax(0,1fr)}  /* 裸 1fr 的 min-content 曾撑爆窄视口整页 (全面测试 M10/M11) */
  .tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tbl-wrap table{min-width:640px}
  .kv{grid-template-columns:120px minmax(0,1fr)}
}
@media(max-width:1280px) and (min-width:901px){
  .kpi-band{grid-template-columns:repeat(2,1fr)}
  .quick-row{grid-template-columns:repeat(3,1fr)}
}
</style></head><body>

<aside class="sidebar">
  <div class="brand">
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
      <rect x="2.5" y="2.5" width="15" height="15" rx="3.5" stroke="#0e7568" stroke-width="1.7"/>
      <rect x="6.5" y="8" width="7" height="6" rx="1" fill="#0e7568"/>
      <path d="M7.5 8V6.5a2.5 2.5 0 0 1 5 0V8" stroke="#0e7568" stroke-width="1.7" stroke-linecap="round"/>
    </svg>
    Context Vault
  </div>
  <nav class="side-nav" aria-label="控制台分区">
    <button data-t="ov" class="on" aria-current="page">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M2 13.5h12M4 13.5V8m4 5.5V3.5m4 10V6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      概览<span class="kbd">1</span></button>
    <button data-t="snap">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><ellipse cx="7" cy="7" rx="4.5" ry="4.5" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      快照资产<span class="kbd">2</span></button>
    <button data-t="mig">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M2 8h11m0 0-3.5-3.5M13 8l-3.5 3.5M14 2.5v11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".4"/><path d="M2 8h9m0 0-3-3M11 8l-3 3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      迁移<span class="kbd">3</span></button>
    <button data-t="probe">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity=".45"/><circle cx="8" cy="8" r="2.6" stroke="currentColor" stroke-width="1.6"/><path d="M8 1.6V4M8 12v2.4M1.6 8H4M12 8h2.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      哨兵<span class="kbd">4</span></button>
  </nav>
  <div class="side-foot">
    <span class="privacy">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
        <rect x="3" y="7" width="10" height="7" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
        <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" stroke="currentColor" stroke-width="1.4"/>
      </svg>
      本地回环 · 数据不出机器
    </span>
    <span class="health-line"><span class="dot ok" id="foot-dot"></span><span id="foot-health">连接中…</span></span>
  </div>
</aside>

<div class="workspace">
  <header class="topbar">
    <h1 id="sec-title">概览</h1>
    <div class="gsearch">
      <span class="gs-ic"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span>
      <input type="search" id="g-q" placeholder="搜索全部历史会话 — 项目、模型、任务关键词" aria-label="全局搜索历史会话">
      <kbd>Enter</kbd>
    </div>
    <span class="status-pill" id="status" role="status"><span class="pulse"></span>连接中</span>
  </header>

  <div class="content"><div class="content-inner">

  <!-- ============ 概览 ============ -->
  <section id="t-ov" aria-label="概览">
    <div class="quick-row">
      <button class="quick" onclick="setTab('snap')">
        <span class="q-ic"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><ellipse cx="7" cy="7" rx="4.5" ry="4.5" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span>
        <span><span class="q-t">查看快照</span><br><span class="q-d" id="q-snap-d">浏览全部库存与搜索</span></span>
        <span class="q-go">→</span>
      </button>
      <button class="quick" onclick="setTab('mig')">
        <span class="q-ic"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 8h11m0 0-3.5-3.5M13 8l-3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        <span><span class="q-t">生成交接</span><br><span class="q-d">选快照 → 预览损失 → 导出 / 迁移</span></span>
        <span class="q-go">→</span>
      </button>
      <button class="quick" onclick="setTab('probe')">
        <span class="q-ic"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity=".45"/><circle cx="8" cy="8" r="2.6" stroke="currentColor" stroke-width="1.6"/><path d="M8 1.6V4M8 12v2.4M1.6 8H4M12 8h2.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg></span>
        <span><span class="q-t">服务状态</span><br><span class="q-d" id="q-svc-d">服务健康与上游哨兵</span></span>
        <span class="q-go">→</span>
      </button>
    </div>

    <div class="kpi-band" id="ov-band">
      <div class="skel" style="width:180px"></div><div class="skel" style="width:160px"></div>
      <div class="skel" style="width:170px"></div><div class="skel" style="width:150px"></div>
    </div>

    <div class="ov-grid">
      <div class="card">
        <div class="card-h">最近入库会话</div>
        <div id="ov-latest"><div class="skel" style="height:110px"></div></div>
      </div>
      <div class="ov-side">
        <div class="card"><div class="card-h">哨兵 · 最近探测</div><div id="ov-probe"><div class="hint" style="padding:8px 12px">加载中…</div></div></div>
        <div class="card"><div class="card-h">最新记忆</div><div id="ov-mem"><div class="hint" style="padding:8px 12px">加载中…</div></div></div>
        <details class="card">
          <summary><span class="tw">▶</span>审计 · 库内事件<span class="sub">（点开查看汇总与时间线）</span></summary>
          <div class="card-b" id="ov-audit"><div class="hint" style="padding:8px 12px">加载中…</div></div>
        </details>
      </div>
    </div>

    <details class="card" style="margin-top:12px">
      <summary><span class="tw">▶</span>用量明细<span class="sub">（数据源与口径逐项标注）</span></summary>
      <div class="card-b" id="ov-usage" style="padding:12px 16px 16px"><div class="skel" style="width:40%"></div></div>
    </details>
  </section>

  <!-- ============ 快照资产 ============ -->
  <section id="t-snap" style="display:none" aria-label="快照资产">
    <div class="controls">
      <input type="search" id="snap-q" placeholder="搜索全部历史会话 — 项目名、模型、任务关键词…" aria-label="搜索快照"
        onkeydown="if(event.key==='Enter')snapSearch()">
      <button class="btn" onclick="snapSearch()">搜索</button>
      <button class="btn" onclick="loadSnaps()">最近</button>
      <button class="btn" id="snap-sort" onclick="snapSort()">时间 ↓</button>
      <button class="btn" id="snap-leaves" onclick="snapLeavesToggle()" aria-pressed="true"
        title="每个会话只列演进链的最新版本; 再点显示全部历史版本">仅最新版本</button>
    </div>
    <div class="meta-line" id="snap-meta"></div>
    <div id="snap-list" aria-live="polite"><div class="skel" style="width:50%"></div><div class="skel" style="width:72%"></div><div class="skel" style="width:60%"></div></div>
  </section>

  <!-- ============ 迁移工作台 ============ -->
  <section id="t-mig" style="display:none" aria-label="迁移工作台">
    <div class="mig-wrap">
      <div class="mig-steps">
        <div class="mig-step">
          <div class="card-h"><span class="stepno">1</span>选择源快照</div>
          <div class="card-b">
            <label for="mig-sid" class="hint" style="display:block;margin-bottom:6px">最近 50 个快照；也可从快照资产搜索或详情抽屉带入</label>
            <select id="mig-sid" onchange="onMigSource()"><option value="">加载中…</option></select>
            <div class="sel-meta" id="mig-src-meta"></div>
          </div>
        </div>
        <div class="mig-step">
          <div class="card-h"><span class="stepno">2</span>选择目标</div>
          <div class="card-b">
            <div class="tgt-cards" id="tgt-cards" role="radiogroup" aria-label="迁移目标">
              <label class="tgt-card on"><input type="radio" name="tgt" value="codex" checked onchange="onMigTarget()">
                <span><span class="tt">Codex</span><br><span class="td">生成 rollout 会话文件，codex resume 续写</span></span></label>
              <label class="tgt-card"><input type="radio" name="tgt" value="claude" onchange="onMigTarget()">
                <span><span class="tt">Claude Code</span><br><span class="td">生成 projects 会话 jsonl，claude --resume 恢复</span></span></label>
              <label class="tgt-card"><input type="radio" name="tgt" value="markdown" onchange="onMigTarget()">
                <span><span class="tt">Markdown 简报 · 任意 agent</span><br><span class="td">≤48KB 交接简报（停在哪 / 最近动作 / 活上下文尾部）</span></span></label>
            </div>
            <div class="apply-toggle" id="apply-toggle">
              <button class="on" data-apply="0" onclick="setApply(false)">
                <span class="at-t">写入 outbox（默认 · 安全）</span>
                <span class="at-d">产物落 vault/outbox/，绝不触碰真实会话目录</span></button>
              <button data-apply="1" onclick="setApply(true)">
                <span class="at-t">写入真实会话目录</span>
                <span class="at-d">目标 agent 的 resume 列表将出现此会话（需确认）</span></button>
            </div>
            <div class="hint" id="mig-md-note" style="display:none;margin-top:8px">
              Markdown 目标始终导出到 outbox，无写入会话目录语义。</div>
          </div>
        </div>
      </div>

      <div class="mig-step">
        <div class="card-h"><span class="stepno">3</span>迁移预览<span class="sub">（切源自动重算 · 目标相关损失绑定当前目标）</span></div>
        <div class="card-b"><div id="mig-preview">
          <div class="mig-idle">选中源快照后，这里展示<b>保真度预览</b>：将保留的上下文块、预计 tokens、损失清单与恢复命令。<br>
            迁移采用 stateless replay —— 压缩边界之后为活上下文，摘要注入为新会话首条消息。</div>
        </div></div>
      </div>

      <div class="mig-step">
        <div class="card-h"><span class="stepno">4</span>执行并查看结果</div>
        <div class="card-b">
          <div class="exec-bar">
            <button class="btn primary" id="mig-outbox" onclick="migGo(false)">生成到 outbox（安全）</button>
            <button class="btn danger" id="mig-apply" onclick="migGo(true)">写入目标会话目录</button>
            <span class="hint" id="mig-exec-hint"></span>
          </div>
          <div id="mig-result" style="margin-top:12px" aria-live="polite"></div>
          <div class="section-note">写入真实目录后用 <b>codex resume</b> / <b>claude --resume</b> 恢复；恢复命令将按迁移结果里的真实 session id / 文件生成。</div>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ 哨兵 ============ -->
  <section id="t-probe" style="display:none" aria-label="哨兵">
    <div class="controls">
      <span class="probe-state off" id="probe-state"><span class="dot off"></span>读取中…</span>
      <button class="btn primary" id="probe-btn" onclick="probeNow()">立即探测上游</button>
      <span class="hint" id="probe-last"></span>
    </div>
    <div class="controls" style="margin-top:2px">
      <span class="stat-inline">监控节点 <b id="sn-nodes">—</b></span>
      <span class="stat-inline" id="sn-avail-wrap">最近样本 ok 率 <b id="sn-avail">—</b></span>
      <span class="stat-inline">平均延迟 (ok 事件) <b id="sn-lat">—</b></span>
      <span class="hint" id="sn-note">样本口径见下方说明</span>
    </div>
    <div id="probe-detail" style="margin-bottom:12px"></div>
    <div id="probe-list" aria-live="polite"><div class="skel" style="width:55%"></div><div class="skel" style="width:42%"></div></div>
    <div class="section-note" id="probe-caliber">统计只基于上面列出的最近事件样本（/probe_events 最近 200 条），
      不代表全天覆盖；未配置节点时不会显示"全部上游正常"。</div>
  </section>

  </div></div>
</div>

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
/* ================= 基础工具 ================= */
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=v=>(+v||0).toLocaleString("en-US");
/* 缩写值 + 精确值并陈: 展示缩写, title 与 exact 行给精确值 */
const bn=v=>{v=+v||0;return v>=1e9?(v/1e9).toFixed(2)+"B":v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e4?(v/1e3).toFixed(1)+"k":num(v)};
const numT=v=>'<span class="num" title="'+num(v)+'">'+bn(v)+"</span>";
const usd=v=>v==null?"—":"$"+(+v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const ftime=s=>{if(!s)return"—";const d=new Date(s);return isNaN(d)?String(s).slice(0,19)
  :d.toLocaleString("zh-CN",{year:"2-digit",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})};
const frel=s=>{if(!s)return"";const d=new Date(s);if(isNaN(d))return String(s).slice(0,19);
  const m=Math.round((Date.now()-d.getTime())/6e4);
  return m<1?"刚刚":m<60?m+" 分钟前":m<1440?Math.round(m/60)+" 小时前":Math.round(m/1440)+" 天前"};
const shortSid=s=>{s=String(s||"");return s.length>22?s.slice(0,14)+"…"+s.slice(-5):s};
const truncPath=(p,n)=>{p=String(p||"");return p.length>n?p.slice(0,n-1)+"…":p};

let toastTimer;
function toast(m,err){const t=$("#toast");t.textContent=m;t.classList.toggle("err",!!err);t.classList.add("show");
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove("show"),5200)}

class ApiError extends Error{constructor(msg,code){super(msg);this.code=code}}
let bootRetry=null;
function scheduleBoot(){if(bootRetry)return;bootRetry=setTimeout(()=>{bootRetry=null;boot()},4000)}
function markSvcBad(){ /* 数据域 fetch 失败 = 服务不可达: 状态徽章不得继续撒谎"服务正常" (全面测试 M13) */
  const s=$("#status");
  if(!s.classList.contains("err")){s.classList.add("err");
    s.innerHTML="<span class='pulse'></span>服务异常 · 4s 后重试";}
  const fd=$("#foot-dot");if(fd)fd.className="dot err";
  const fh=$("#foot-health");if(fh)fh.textContent="未连接";
  scheduleBoot();
}
async function api(p,opt){
  let r;
  try{r=await fetch(p,opt)}catch(e){markSvcBad();throw e}
  if(!r.ok){
    const j=await r.json().catch(()=>({}));
    throw new ApiError(j.error||("HTTP "+r.status),r.status);
  }
  return r.json();
}
/* 竞态守卫: 每个加载域一个序号, 旧响应到达时直接丢弃, 不覆盖新选择 */
function seqGuard(container){
  const box={seq:0};
  return async fn=>{
    const my=++box.seq;
    const r=await fn();
    if(my!==box.seq)throw{stale:true};
    return r;
  };
}

const WARN='<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M8 2L15 14H1L8 2Z" stroke="#a16207" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6.5v3.5M8 12v.5" stroke="#a16207" stroke-width="1.5" stroke-linecap="round"/></svg>';
const GOOD='<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.3" stroke="#0e7568" stroke-width="1.5"/><path d="M5.4 8.2l1.8 1.8 3.4-3.8" stroke="#0e7568" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ERRIC='<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.3" stroke="#b42323" stroke-width="1.5"/><path d="M8 4.8v4M8 11.2v.4" stroke="#b42323" stroke-width="1.6" stroke-linecap="round"/></svg>';
const errEmpty=(msg,retry)=>'<div class="empty">'+ERRIC+'<div>'+esc(msg)+"</div>"+
  (retry?'<button class="btn sm" onclick="'+retry+'">重试</button>':"")+"</div>";

/* 单元格允许裸字符串或 {t,num} 对象; 行数组可挂 __tr 属性输出 <tr> 附加属性 */
const cell=c=>(c&&typeof c==="object")?c:{t:c};
const table=(heads,rows,cls)=>{if(!rows||!rows.length)return"";
  const hs=heads.map((h,i)=>{const c=cell(rows[0][i]);return "<th"+(c.num?' class="num"':"")+">"+h+"</th>"}).join("");
  const bs=rows.map(r=>{const open="<tr"+(r.__tr?" "+r.__tr:"")+">";
    return open+r.map(c0=>{const c=cell(c0);return "<td"+(c.num?' class="num"':"")+">"+c.t+"</td>"}).join("")+"</tr>"}).join("");
  return '<div class="tbl-wrap"><table'+(cls?' class="'+cls+'"':"")+">"+
    "<thead><tr>"+hs+"</tr></thead><tbody>"+bs+"</tbody></table></div>"};

/* 会话摘要清洗 (剥本地 URL / 路径残尾 → 标签) */
const cleanSummary=s=>{let t=String(s||"");const tags=[];
  t=t.replace(/https?:\/\/\S+/g," ");
  t=t.replace(/\[([^\]\n]{1,24})\]\([^)\n]{0,40}/g,(m,g)=>{tags.push(g);return " "});
  t=t.replace(/\(([A-Za-z]:\\?[^)\n]{0,30})/g,(m,g)=>{tags.push(g);return " "});
  t=t.replace(/\b[A-Za-z]:\\[^\s",;)]+/g,(m)=>{tags.push(m);return " "});
  t=t.replace(/\s+/g," ").trim();
  return {text:t||"(无文本)",tags:[...new Set(tags)].slice(0,2)}};
const sumCell=s=>{const c=cleanSummary(s);
  return '<span class="sum-line" title="'+esc(s||"")+'"><span class="sum-main">'+esc(c.text)+"</span>"+
    c.tags.map(t=>'<span class="mini-tag">'+esc(t)+"</span>").join("")+"</span>"};
const dirCell=(p,max)=>'<span class="dir" title="'+esc(p||"—")+'"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/></svg><span>'+esc(truncPath(p||"—",max||34))+"</span></span>";
const agentCell=a=>"<span class='chip'>"+esc(a||"?")+"</span>";
const compCell=r=>r&&r!=="full"
  ?'<span class="chip warn" title="源转录不含 assistant 文本等限制 — 详见快照详情">'+esc(r)+"</span>"
  :'<span style="color:var(--ink-3)">完整</span>';
const relayBadge=p=>'<span class="relay-badge" title="上游中转节点 '+esc(p||"")+'">'+
  '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>'+
  '<span class="rl">Relay ·</span><span class="rn">'+esc(p||"—")+"</span></span>";
const copyText=(t,msg)=>{const done=()=>toast(msg||"已复制");
  if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(t).then(done).catch(()=>fallbackCopy(t,done));
  else fallbackCopy(t,done)};
const fallbackCopy=(t,done)=>{const ta=document.createElement("textarea");ta.value=t;
  ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();
  try{document.execCommand("copy");done()}catch(e){toast("复制失败",1)}
  ta.remove()};

/* ================= 分区导航 + 深链 ================= */
const TABS=["ov","snap","mig","probe"];
const TAB_TITLE={ov:"概览",snap:"快照资产",mig:"迁移",probe:"哨兵"};
function setTab(t){
  if(!TABS.includes(t))t="ov";
  $$("nav.side-nav button").forEach(x=>{const on=x.dataset.t===t;
    x.classList.toggle("on",on);on?x.setAttribute("aria-current","page"):x.removeAttribute("aria-current")});
  TABS.forEach(k=>{$("#t-"+k).style.display=k===t?"":"none"});
  $("#sec-title").textContent=TAB_TITLE[t];
  if(t==="mig"&&!$("#mig-sid").dataset.filled)fillMig();
  if(t==="probe"&&!$("#probe-list").dataset.loaded)loadProbe();
  if(window.history)history.replaceState(null,"","#"+t);
}
$$("nav.side-nav button").forEach(b=>b.onclick=()=>setTab(b.dataset.t));
document.addEventListener("keydown",e=>{
  const t=e.target,tag=t&&t.tagName;
  if(tag&&/^(INPUT|SELECT|TEXTAREA)$/.test(tag))return;
  if(drawerOpen)return;
  const n=+e.key;  /* 1-4 快捷切分区 (侧栏 kbd 提示) */
  if(n>=1&&n<=TABS.length){setTab(TABS[n-1]);e.preventDefault()}
});
$("#g-q").addEventListener("keydown",e=>{
  if(e.key!=="Enter")return;
  const q=e.target.value.trim();
  setTab("snap");
  $("#snap-q").value=q;
  q?snapSearch():loadSnaps();
});

/* ================= 服务健康 ================= */
function stripToken(){
  /* B03 配套: 首访 ?token= 已种成 HttpOnly cookie, 地址栏不再保留凭据
     (分享/截屏/历史记录里不泄漏 token; 失败时不清理, 用户还需带 token 重试) */
  if(/[?&]token=/.test(location.search||"")&&window.history)
    history.replaceState(null,"",location.pathname+(location.hash||""));
}
async function checkHealth(){
  const h=await api("/healthz");
  const s=$("#status");s.classList.remove("err","auth");
  s.innerHTML='<span class="pulse"></span>服务正常 · '+num(h.snapshots)+" 快照 · "+num(h.terms)+" 索引词项";
  $("#foot-dot").className="dot ok";
  $("#foot-health").innerHTML="健康 · <b>"+num(h.snapshots)+"</b> 快照";
  $("#q-svc-d").textContent="服务正常 · "+num(h.snapshots)+" 快照 · 上游哨兵";
  $("#q-snap-d").textContent=num(h.snapshots)+" 个快照版本待浏览";
  return h;
}
let healthSeq=seqGuard(null);
async function boot(){
  try{
    await healthSeq(async()=>checkHealth());
    stripToken();
    loadOv();loadSnaps();
  }catch(e){
    const s=$("#status");
    if(e&&e.code===401){s.classList.remove("err");s.classList.add("auth");
      s.innerHTML='<span class="pulse"></span>需要鉴权 — 请用带 token 的链接重新打开 (凭据不入页面)';}
    else{s.classList.add("err");
      s.innerHTML="<span class='pulse'></span>服务异常 · 4s 后重试";}
    $("#foot-dot").className="dot err";
    $("#foot-health").textContent=e&&e.code===401?"需要鉴权":"未连接";
    scheduleBoot();  /* 常驻重启/部署窗口期自动恢复 (scheduleBoot 去重, 不与 api 侧失败双开循环) */
  }
}

/* ================= 概览 ================= */
const ovSeq=seqGuard(null);
const kvRow=(k,v)=>'<div class="row"><span class="l">'+k+'</span><span class="r">'+v+"</span></div>";

async function loadOv(){
  $("#ov-band").innerHTML='<div class="skel" style="width:180px"></div><div class="skel" style="width:160px"></div><div class="skel" style="width:170px"></div><div class="skel" style="width:150px"></div>';
  try{
    const d=await ovSeq(()=>api("/ledger"));
    const lt=d.lifetime||{};const ow=d.owned||{};
    const zu=d.zcode_usage||{};
    /* 独立会话数: 后端没有该口径时显示版本数并注明, 不编造 */
    const sessions=(ow.sessions!=null)?ow.sessions:null;
    const hit=(ow.cache_hit_rate!=null)?ow.cache_hit_rate:null;
    const hitSem=ow.semantics||"cache 读 / 全部输入 (按来源口径规范化的分母)";
    const inEff=(ow.input_effective!=null)?ow.input_effective:((ow.tokens_in||0));
    /* zcode model_usage 是 OpenAI 语义: input 已含 cache 读 (实证 0 行 cache_read>input) —
       曾把 tin+cr 相加, zcode 输入被计两次 (KPI 虚高约一倍) */
    const ztIn=Object.values(zu).reduce((s,a)=>s+(a.tin||0),0);
    /* 快照账本已含 zcode 叶子用量 (索引层回填); 有 model_usage 全量时用它替换快照里的 zcode 部分,
       不重复相加 (model_usage 还覆盖未入库/已删会话, 是 zcode 的真相源) */
    const zOwned=(((ow.by_agent||[]).find(a=>a.agent==="zcode"))||{}).tokens_in||0;
    const totalIn=(inEff||0)-(ztIn>0?zOwned:0)+ztIn;
    const band=
      kpi(num(ow.snapshots||0),"快照版本","vault/snapshots 内容寻址快照 · 含被演进替代的历史版本")+
      kpi(sessions==null?"—":num(sessions),"独立会话",sessions==null?"后端未提供独立会话口径":"按 lineage 演进链去重后的会话数")+
      kpi('<span title="'+num(totalIn)+'">'+bn(totalIn)+'</span><small>tokens</small>',
        '累计输入 tokens<div class="exact">精确 '+num(totalIn)+'</div>',
        "口径: 快照内嵌 usage 规范化总输入 ("+num(inEff||0)+", Claude 语义补回 cache 读/写) · zcode 以 model_usage 全量调用 ("+num(ztIn)+") 替换快照内的 zcode 部分 · 均含 cache 读")+
      kpi(hit==null?"—":'<span class="'+(hit>1?"kpi-bad":"")+'">'+(hit*100).toFixed(1)+"%</span>",
        "缓存命中率",
        "口径: "+hitSem+" · 数据源: vault 快照内嵌 usage");
    $("#ov-band").innerHTML=band;

    /* 最近入库会话: 每会话只列演进链最新版本 (同一会话的历史版本不再刷屏) */
    let rc=null;try{rc=await api("/recent?limit=10&leaves=1")}catch(e){}
    const total=(rc&&rc.total)||0;
    const arr=(rc&&rc.rows)||[];
    $("#ov-latest").innerHTML=(arr.length?table(["来源","会话摘要","位置","入库时间"],
      arr.map(s=>{const tr=[agentCell(s.agent),sumCell(s.first),dirCell(s.cwd,26),
        '<span class="nowr" title="'+esc(s.created_at||"")+'">'+frel(s.created_at)+"</span>"];
        tr.__tr='class="rowlink" tabindex="0" role="button" aria-label="打开快照详情" '+
          'onclick="snapDetail(\''+s.sid+'\',\''+(s.created_at||"")+'\')" '+
          'onkeydown="if(event.key===\'Enter\')snapDetail(\''+s.sid+'\',\''+(s.created_at||"")+'\')"';
        return tr}),"tbl-44")
      :'<div class="empty">还没有会话入库<br>先运行 <code>python tools/cv.py sync</code> 或单文件 <code>cv export &lt;会话文件&gt;</code>'+
       '<br><button class="btn sm" onclick="loadOv()">重试</button></div>')+
      '<div class="foot"><span>显示最近 '+arr.length+" 个会话 · 共 "+num(total)+" 个会话 / "+
        num((rc&&rc.versions_total)||total)+" 个快照版本</span>"+
      '<a onclick="setTab(\'snap\')">查看全部 →</a></div>';

    /* 右栏: 哨兵 / 记忆 / 审计 (可展开) */
    let pv=[];try{pv=await api("/probe_events?limit=3")}catch(e){}
    $("#ov-probe").innerHTML=Array.isArray(pv)&&pv.length?pv.map(e=>{
      const st=e.status||"?";const cls=st==="ok"?"ok":(st==="http_error"||st==="error")?"err":"warn";
      return '<div class="row"><span class="l"><span class="dot '+cls+'"></span>'+relayBadge(e.profile)+
      ' <span class="pill '+cls+'">'+esc(st)+'</span></span><span class="r">'+frel(e.ts)+"</span></div>"}).join("")
      :'<div class="hint" style="padding:8px 12px">暂无探测记录 · 到哨兵页立即探测，或等托盘周期探测</div>';
    let mr=[];try{mr=await api("/memory?q=&limit=3");mr=(mr&&mr.memories)||[]}catch(e){mr=[]}
    $("#ov-mem").innerHTML=mr.length?mr.map(m=>'<div class="row"><span class="l" title="'+esc(m.content)+
      '">['+esc(m.kind)+"] "+esc(String(m.content).slice(0,36))+
      '</span><span class="r">'+esc(m.project||"—")+"</span></div>").join("")
      :'<div class="hint" style="padding:8px 12px">暂无记忆 · <code>cv memory extract</code> 提取</div>';
    let au=null;try{au=await api("/audit")}catch(e){}
    $("#ov-audit").innerHTML=au?
      '<div style="padding:4px 8px 8px">'+
      kvRow("累计事件",num(au.total_events||0))+
      kvRow("探测 ok / 异常",num((au.summary||{}).ok_probes||0)+" / "+num((au.summary||{}).abnormal||0))+
      kvRow("上游切换",num((au.summary||{}).switches||0))+
      kvRow("模型消失事件",num((au.summary||{}).model_missing_events||0))+
      '</div><div class="foot"><span>事件全量 '+num(au.total_events||0)+" 条</span>"+
      '<a onclick="setTab(\'probe\')">去哨兵查看 →</a></div>'
      :'<div class="hint" style="padding:8px 12px">—</div>';

    renderUsage(d);
  }catch(e){
    if(e&&e.stale)return;
    $("#ov-band").innerHTML=errEmpty("概览加载失败："+e.message,"loadOv()");
    $("#ov-latest").innerHTML="";$("#ov-usage").innerHTML="";
  }
}
const kpi=(v,k,tip)=>'<div class="kpi"'+(tip?' title="'+esc(tip)+'"':"")+'><div class="v">'+v+
  '</div><div class="k">'+k+"</div></div>";

/* 用量明细 (details 区): headroom 与 zcode 是可选数据源, 未接入给说明不占主体 */
function renderUsage(d){
  const lt=d.lifetime||{};const ow=d.owned||{};
  const zu=d.zcode_usage||{};
  const zrows=Object.entries(zu);
  const anyPrice=zrows.some(([,a])=>a.cost!=null);
  let h="";
  h+='<div class="card-h" style="padding-left:0;border-bottom:none">ZCode 用量 <span class="sub">— 数据源: ZCode 主库 model_usage 全量调用记录; 无价目模型不计成本 (订阅制口径)</span></div>';
  h+=zrows.length?table(anyPrice?["模型","调用次数","输入 tok","cache 读","成本 (按价目)"]:["模型","调用次数","输入 tok","cache 读"],
    zrows.map(([m,a])=>{const r=['<span class="mono">'+esc(m)+"</span>",{t:num(a.n),num:1},
      {t:'<span title="'+num(a.tin)+'">'+bn(a.tin)+"</span>",num:1},
      {t:'<span title="'+num(a.cr)+'">'+bn(a.cr)+"</span>",num:1}];
      if(anyPrice)r.push({t:a.cost==null?"未提供价目":'<span title="$'+a.cost+'">'+usd(a.cost)+"</span>",num:1});
      return r;}))
    :'<div class="hint" style="padding:6px 0">未接入 — 运行 <code>cv ledger import-zcode</code> 或由托盘自动导入; 此处不留空卡。</div>';
  h+='<div class="card-h" style="padding-left:0;border-bottom:none;margin-top:16px">headroom 通路节省 <span class="sub">— 数据源: headroom 导入; 未接入不显示走势</span></div>';
  const hasLt=(lt.saved_usd||0)+(lt.cache_savings_usd||0)>0;
  const ms=d.monthly||[];
  if(!hasLt&&!ms.length){
    h+='<div class="hint" style="padding:6px 0">未接入 — 运行 <code>cv ledger import-headroom</code> 导入; 金额类指标只在有数据源时出现, 不以 0 占位。</div>';
  }else{
    if(ms.length){
      const mx=Math.max(...ms.map(m=>m.c||0),1);
      const fmt=v=>"$"+(v>=1000?(v/1000).toFixed(1)+"k":v.toFixed(0));
      /* 月少时收紧图宽 (~90px/月), 避免 2 根柱子摊满整卡显得稀疏; 680px CSS 上限兜底 */
      const mw=Math.min(680,ms.length*90+120);
      h+='<div class="months" style="max-width:'+mw+'px"><div class="yax"><span>'+fmt(mx)+'</span><span>'+fmt(mx/2)+
        '</span><span>$0</span></div><div class="bars">'+ms.map((m,i)=>{const hh=Math.max(100*(m.c||0)/mx,3);
        return '<div class="mcol"><div class="val" style="--h:'+hh.toFixed(1)+'">'+usd(m.c||0)+
        '</div><div class="stick" style="height:'+hh.toFixed(2)+'%;--i:'+i+'" title="'+
        esc(m.m+" · "+usd(m.c))+'"></div><div class="lb">'+esc(String(m.m).slice(2))+"</div></div>"}).join("")+"</div></div>";
    }
    const bym=d.by_model||[];
    h+=bym.length?table(["模型","事件","节省 tokens","节省金额 (估算, 非账单)"],
      bym.map(m=>['<span class="mono">'+esc(m.model||"未知模型")+"</span>",
        {t:num(m.n),num:1},{t:'<span title="'+num(m.t)+'">'+bn(m.t)+"</span>",num:1},{t:usd(m.c),num:1}])):"";
    if(hasLt)h+='<div class="section-note">lifetime 汇总: 压缩节省 '+num(lt.tokens_saved||0)+
      " tokens ≈ "+usd(lt.saved_usd)+" · 缓存节省 "+usd(lt.cache_savings_usd)+
      " — 按官方价目折算 (估算, 非账单)。</div>";
  }
  $("#ov-usage").innerHTML=h;
}

/* ================= 快照资产 ================= */
const snapsSeq=seqGuard(null);
let snapPage=0,snapAsc=false,snapMode="recent",snapQuery="",snapLeaves=true;
function snapLeavesToggle(){ /* 同会话演进链折叠为一行 (×N 版本) ↔ 全部历史版本 */
  snapLeaves=!snapLeaves;snapPage=0;
  const b=$("#snap-leaves");b.textContent=snapLeaves?"仅最新版本":"全部版本";
  b.setAttribute("aria-pressed",snapLeaves?"true":"false");
  renderSnaps();
}
const LIM=30;

async function loadSnaps(page){
  if(typeof page==="number")snapPage=Math.max(0,page);else snapPage=0;
  snapMode="recent";snapQuery="";
  $("#snap-q").value="";
  await renderSnaps();
}
function snapSort(){
  snapAsc=!snapAsc;snapPage=0;
  $("#snap-sort").textContent=snapAsc?"时间 ↑":"时间 ↓";
  if(snapMode==="search")snapSearch(true);else renderSnaps();
}
async function snapSearch(keepPage){
  const q=$("#snap-q").value.trim();
  if(!q)return loadSnaps();
  if(!keepPage)snapPage=0;
  snapMode="search";snapQuery=q;
  $("#snap-list").innerHTML='<div class="skel" style="width:40%"></div><div class="skel" style="width:70%"></div>';
  try{
    const d=await snapsSeq(()=>api("/search?q="+encodeURIComponent(q)+"&limit=20"));
    renderSnapTable(d.hits||[],{search:q,hits:d.hits?d.hits.length:0});
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("搜索失败："+e.message,"snapSearch(true)");
    $("#snap-meta").textContent="";
  }
}
async function renderSnaps(){
  $("#snap-list").innerHTML='<div class="skel" style="width:45%"></div><div class="skel" style="width:66%"></div>';
  try{
    const d=await snapsSeq(()=>api("/recent?limit="+LIM+"&offset="+(snapPage*LIM)+"&order="+(snapAsc?"asc":"desc")+
      "&leaves="+(snapLeaves?1:0)));
    const rows=d.rows||[],total=d.total||0;
    renderSnapTable(rows,{recent:true,total,rows:rows.length,versionsTotal:d.versions_total});
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("快照列表加载失败："+e.message,"renderSnaps()");
    $("#snap-meta").textContent="";
  }
}
const sidBtn=sid=>"<button class='linklike sid' title=\""+esc(sid)+"（点击打开详情）\" onclick=\"event.stopPropagation();snapDetail('"+sid+"')\">"+esc(shortSid(sid))+"</button>";
/* 复制类按钮经 data-v 传值: Windows 路径反斜杠不会被 onclick 里的字符串字面量转义吃掉 */
const copyBtn=sid=>"<button class='btn sm' title='复制完整 SID' aria-label='复制 SID' data-v=\""+esc(sid)+"\" onclick=\"event.stopPropagation();copyData(this,'已复制完整 SID')\">复制 SID</button>";
function copyData(btn,msg){copyText(btn.dataset.v,msg||"已复制")}
function copySid(sid){copyText(sid,"已复制完整 SID: "+sid)}
function renderSnapTable(rows,opt){
  let meta="";
  if(opt.search){
    meta='搜索「'+esc(opt.search)+'」— <b class="num">'+num(opt.hits||0)+"</b> 条命中"+
      ' <button class="xbtn" onclick="loadSnaps()">清空搜索 ×</button>';
  }else if(snapLeaves){
    meta="最近入库 · 共 <b class='num'>"+num(opt.total||0)+"</b> 个会话 (每会话显示最新版本 · 库存 <b class='num'>"+
      num(opt.versionsTotal||opt.total||0)+"</b> 个快照版本)";
  }else{
    meta="最近入库 · 共 <b class='num'>"+num(opt.total||0)+"</b> 个快照版本 (含演进历史版本)";
  }
  $("#snap-meta").innerHTML=meta;
  const pager=(total,rowsN)=>{
    if(total<=LIM)return"";
    return '<div class="pager"><span>第 <b class="num">'+num(snapPage*LIM+1)+"–"+num(snapPage*LIM+rowsN)+
      "</b> 条 · 共 <b class='num'>"+num(total)+"</b> 条</span><span>"+
      '<button class="btn"'+(snapPage<=0?" disabled":"")+' onclick="loadSnaps('+(snapPage-1)+')">← 上一页</button> '+
      '<button class="btn"'+(snapPage*LIM+LIM>=total?" disabled":"")+' onclick="loadSnaps('+(snapPage+1)+')">下一页 →</button></span></div>'};
  let body;
  if(!rows.length){
    body=opt.search
      ?'<div class="empty">'+WARN+'<div>没有命中「'+esc(opt.search)+'」— 换个关键词试试：项目名、文件路径、任务描述都行</div>'+
       '<button class="btn" onclick="loadSnaps()">清空搜索，显示最近</button></div>'
      :'<div class="empty">vault 还是空的<br>导出第一个会话：<code>python tools/cv.py sync</code>'+
       '<br>或单文件：<code>python tools/cv.py export ~/.codex/sessions/…/rollout-*.jsonl</code></div>';
  }else{
    body='<div class="cardbox">'+
      table(opt.search
        ?["快照","会话摘要","来源","得分","完整性","时间","目录",""]
        :["快照","会话摘要","来源","块数","完整性","时间","目录",""],
        rows.map(r=>{const tr=[sidBtn(r.sid)+(r.versions>1?' <span class="chip" title="该会话共 '+
            num(r.versions)+' 个演进版本 (此行为最新版本)">×'+num(r.versions)+"</span>":""),
          sumCell(r.first),agentCell(r.agent),
          {t:opt.search?num(Math.round(r.score)):num(r.blocks),num:1},
          compCell(r.completeness),
          '<span class="mono nowr" title="'+esc(r.created_at||"")+'">'+ftime(r.created_at)+"</span>",
          dirCell(r.cwd,30),copyBtn(r.sid)];
          tr.__tr='class="rowlink" tabindex="0" role="button" aria-label="打开快照详情 '+esc(shortSid(r.sid))+
            '" onclick="snapDetail(\''+r.sid+'\',\''+(r.created_at||"")+'\')" '+
            'onkeydown="if(event.key===\'Enter\')snapDetail(\''+r.sid+'\',\''+(r.created_at||"")+'\')"';
          return tr}),"tbl-44")+"</div>";
  }
  const pg=opt.search?"":pager(opt.total||0,opt.rows||0);
  $("#snap-list").innerHTML=pg+body+(opt.search?"":pg);
}

/* ================= 快照详情抽屉 ================= */
let drawerOpen=false,lastFocus=null;
function openDrawer(){
  if(drawerOpen)return;drawerOpen=true;lastFocus=document.activeElement;
  const d=$("#drawer"),b=$("#backdrop");d.hidden=false;b.hidden=false;
  void d.offsetHeight; /* 同步 reflow 触发过渡 */
  d.classList.add("show");b.classList.add("show");
  $("#dw-close").focus();
}
function closeDrawer(){
  if(!drawerOpen)return;drawerOpen=false;
  const d=$("#drawer"),b=$("#backdrop");
  d.classList.remove("show");b.classList.remove("show");
  setTimeout(()=>{if(drawerOpen)return;  /* 关后 220ms 内已重开: 旧定时器不得把开着抽屉藏掉 (曾卡死 drawerOpen, 全面测试 M12) */
    d.hidden=true;b.hidden=true},220);
  if(lastFocus&&lastFocus.focus)lastFocus.focus();
}
document.addEventListener("keydown",e=>{
  if(e.key==="Escape")closeDrawer();
  if(e.key==="Tab"&&drawerOpen){ /* 焦点陷阱 */
    const f=$("#drawer").querySelectorAll("button,[href],input,select,textarea");
    if(!f.length)return;
    const first=f[0],last=f[f.length-1];
    if(e.shiftKey&&document.activeElement===first){last.focus();e.preventDefault()}
    else if(!e.shiftKey&&document.activeElement===last){first.focus();e.preventDefault()}
  }});
$("#backdrop").addEventListener("click",closeDrawer);

async function snapDetail(sid,ts){
  openDrawer();
  $("#dw-sub").textContent=sid;
  $("#dw-body").innerHTML='<div class="skel" style="width:55%"></div><div class="skel" style="width:82%"></div><div class="skel" style="width:64%"></div>';
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
      (c.source_format?"<span class='chip'>"+esc(c.source_format)+"</span>":"")+
      (c.completeness&&c.completeness!=="full"?"<span class='chip warn'>"+esc(c.completeness)+"</span>":"<span class='chip ok'>完整</span>")+
      "<span class='hint'>"+ftime(ts||"")+"</span></div>"+
      "<div class='kv' style='margin-top:12px'>"+
      kv("项目目录",esc(c.cwd||"—"))+
      (p?kv("活上下文",num(p.live_blocks)+" 块 · ~"+num(p.live_tokens_est)+" tokens"+(p.compactions?"（"+p.compactions+" 次压缩边界之后）":"")):"")+
      (p?kv("历史累计输入",num(p.history_tokens_in)+" tokens（源口径"+(c.source_format&&c.source_format!=="codex-rollout"?"，未含 cache 读":"，含 cache 读")+"）"):"")+
      kv("块总数",num(s.blocks_total)+" 块"+(s.blocks_dropped?" · 丢弃 "+num(s.blocks_dropped):""))+
      "</div>"+
      "<h3 class='sec'>块构成</h3><div class='dw-chips'>"+(dist||"<span class='hint'>—</span>")+"</div>"+
      (p?"<h3 class='sec'>可迁移性限制</h3><ul class='immlist'>"+
        (imm.length?imm.map(i=>"<li>"+WARN+"<span>"+i+"</span></li>").join(""):"<li class='good'>"+GOOD+"无可迁移性损失</li>")+"</ul>":"")+
      "<h3 class='sec'>会话时间线</h3><div id='dw-tl'><div class='skel' style='width:70%'></div></div>"+
      "<div class='section-note'>详情来自元数据接口（/snapshot + /preview），不含全文；全文查看：<code>cv show "+esc(shortSid(sid))+"</code></div>"+
      "<div class='dw-actions'>"+
      "<button class='btn primary' onclick=\"toMig('"+sid+"')\">迁移此快照 →</button>"+
      "<button class='btn' onclick=\"copySid('"+sid+"')\">复制 SID</button></div>";
    (async()=>{ /* 会话时间线: 该会话全部版本按天分组 (leaf=当前版本) */
      try{
        const tl=await api("/timeline?session="+encodeURIComponent(c.session_ref||"")+"&agent="+encodeURIComponent(c.agent||""));
        const rows=(tl.days||[]).map(day=>"<div class='tl-day'><span class='tl-d'>"+esc(day.day)+"</span>"+
          day.versions.map(v=>"<div class='tl-row"+(v.sid===sid?" cur":"")+"'>"+
            "<span class='mono'>"+v.time+"</span> · "+num(v.blocks)+" 块 · 入 "+num(v.tokens_in)+
            (v.leaf?" <span class='chip ok'>最新</span>":"")+
            " <button class='linklike' onclick=\"snapDetail('"+v.sid+"','"+(v.sid===sid?(ts||""):"")+"')\">"+esc(shortSid(v.sid))+"</button></div>").join("")+
          "</div>").join("");
        $("#dw-tl").innerHTML=rows||"<span class='hint'>—</span>";
      }catch(e){$("#dw-tl").innerHTML="<span class='hint'>时间线加载失败: "+esc(String(e&&e.message||e))+"</span>"}
    })();
  }catch(e){$("#dw-body").innerHTML=errEmpty("详情加载失败："+e.message,"snapDetail('"+sid+"')")}
}
const kv=(k,v)=>"<b>"+k+"</b><span>"+(v==null?"—":v)+"</span>";

/* ================= 迁移工作台 ================= */
const migSeq=seqGuard(null);
let migApply=false,migLastPreview=null;

async function fillMig(){
  let d=null;
  try{d=await api("/recent?limit=50&offset=0")}
  catch(e){ /* 网络失败不得谎报"vault 为空", 也不置 filled — 下次进分区自动重拉 (全面测试 M14) */
    $("#mig-sid").innerHTML='<option value="">加载失败 — 服务异常</option>';
    $("#mig-src-meta").innerHTML="<span class='hint'>加载失败: "+esc(String(e&&e.message||e))+"</span>";
    $("#mig-preview").innerHTML='<div class="mig-idle">快照列表加载失败 — 服务恢复后重进本页重试。</div>';
    return}
  const rows=d.rows||[];
  const sel=$("#mig-sid");
  sel.dataset.filled="1";
  sel.innerHTML=rows.length
    ?rows.map(r=>'<option value="'+r.sid+'">'+esc(shortSid(r.sid))+" · "+esc(r.agent||"?")+" · "+
      num(r.blocks)+" 块 · "+ftime(r.created_at)+"</option>").join("")
    :'<option value="">（vault 为空 — 先 cv sync 入库）</option>';
  onMigSource();
}
function migTarget(){const r=document.querySelector('input[name=tgt]:checked');return r?r.value:"codex"}
function onMigTarget(){
  const t=migTarget();
  $$("#tgt-cards .tgt-card").forEach(c=>c.classList.toggle("on",c.querySelector("input").value===t));
  const md=t==="markdown";
  $("#mig-md-note").style.display=md?"block":"none";
  $("#apply-toggle").style.display=md?"none":"flex";
  if(md&&migApply)setApply(false);
  $("#mig-outbox").textContent=md?"导出交接简报（始终 outbox）":"生成到 outbox（安全）";
  $("#mig-apply").style.display=md?"none":"inline-flex";
  renderMigPreview();  /* 目标相关损失/命令与目标绑定 — 重校准渲染, 不重复请求 */
}
function setApply(v){
  migApply=v;
  $$("#apply-toggle button").forEach(b=>{const on=(b.dataset.apply==="1")===v;
    b.classList.toggle("on",on);b.classList.toggle("risky",on&&v)});
}
function onMigSource(){
  const sel=$("#mig-sid");
  const opt=sel.options[sel.selectedIndex];
  $("#mig-src-meta").innerHTML=(!opt||!opt.value)?"<span class='hint'>无可用快照 — 先 <code>cv sync</code> 入库</span>"
    :"已选 <span class='mono'>"+esc(opt.value)+"</span> · "+esc((opt.textContent||"").split(" · ").slice(1).join(" · "));
  migPreview();
}
function migExecEnabled(on){
  $("#mig-outbox").disabled=!on;
  $("#mig-apply").disabled=!on;
  $("#mig-exec-hint").textContent=on?"":"预览就绪后才能执行";
}
async function migPreview(){
  const id=$("#mig-sid").value;
  if(!id){$("#mig-preview").innerHTML='<div class="mig-idle">vault 为空 — 先 <code>cv sync</code> 或 <code>cv export</code> 入库后再迁移。</div>';
    migExecEnabled(false);return}
  $("#mig-preview").innerHTML='<div class="skel" style="width:55%"></div><div class="skel" style="width:75%"></div><div class="skel" style="width:40%"></div>';
  migLastPreview=null;migExecEnabled(false);
  try{
    const p=await migSeq(()=>api("/preview?id="+id));
    migLastPreview=p;
    renderMigPreview();
  }catch(e){
    if(e&&e.stale)return;
    $("#mig-preview").innerHTML=errEmpty("预览失败："+e.message,"migPreview()");
    migExecEnabled(false);
  }
}
function immList(p){
  const imm=[];
  if(p.no_assistant)imm.push("assistant 文本缺失（源转录不含）— 目标会话只有 user / tool 视角");
  if(p.thinking)imm.push("thinking ×"+p.thinking+" — 仅明文摘要可迁移，加密推理不可移植");
  if(p.agent_messages)imm.push("agent_message ×"+p.agent_messages+" — 降级为带标注的文本");
  if(p.dropped_source)imm.push("源文件被丢弃行 ×"+p.dropped_source+"（developer 注入 / 无移植语义）");
  return imm;
}
function targetLossNote(t){
  /* 目标绑定损失: 现有 /preview 不含逐目标损失明细 — 如实标注, 不冒充完整 */
  if(t==="markdown")return "Markdown 是降级档：仅保留停点/最近动作/活上下文尾部摘要，工具调用配对细节不进简报。";
  if(t==="codex")return "Codex 目标：developer 注入块被过滤（由目标运行时重新注入）；未配对 tool_result 逐条计数进迁移报告。";
  if(t==="claude")return "Claude Code 目标：developer 注入块被过滤；未配对 tool_result 逐条计数进迁移报告。";
  return "";
}
function renderMigPreview(){
  const p=migLastPreview;
  if(!p)return;
  const t=migTarget();
  const imm=immList(p);
  $("#mig-preview").innerHTML=
    '<div class="kv">'+
    kv("活上下文（保留）",num(p.live_blocks)+" 块 · ~"+num(p.live_tokens_est)+" tokens"+
      (p.compactions?"（"+p.compactions+" 次压缩边界之后；压缩摘要将注入为新会话首条消息）":""))+
    kv("活上下文构成",Object.entries(p.live_dist||{}).map(([k,v])=>esc(k)+" ×"+num(v)).join("　")||"—")+
    kv("历史累计输入",num(p.history_tokens_in)+" tokens（源口径）")+
    "</div>"+
    "<h3 class='sec' style='margin:14px 0 4px;font-size:12px;font-weight:600;color:var(--ink-3);letter-spacing:.04em'>丢失 / 降级</h3>"+
    '<ul class="immlist">'+(imm.length?imm.map(i=>"<li>"+WARN+"<span>"+i+"</span></li>").join("")
      :"<li class='good'>"+GOOD+"源端无已知损失项</li>")+"</ul>"+
    '<div class="section-note">'+esc(targetLossNote(t))+
    "（现有 /preview 接口未提供逐目标的处理明细 — 以执行后迁移报告的逐条计数为准）</div>"+
    '<div class="term"><span class="tp">恢复命令 · 按执行结果里的真实 session id / 文件生成</span><code id="mig-cmd">$ '+
    esc(previewCmd(t))+"</code></div>";
  migExecEnabled(true);
}
function previewCmd(t){
  return t==="claude"?"claude --resume":t==="markdown"?"code vault/outbox/brief-*.md":"codex resume";
}
async function migGo(apply){
  const id=$("#mig-sid").value,t=migTarget();
  if(!id)return toast("没有可迁移的快照",1);
  if(!migLastPreview)return toast("预览未就绪，不能执行",1);
  if(apply){
    const sel=$("#mig-sid");const label=sel.options[sel.selectedIndex]?sel.options[sel.selectedIndex].textContent:id;
    const tgtName=t==="codex"?"Codex 会话目录":"Claude Code projects 目录";
    if(!confirm("确认写入真实会话目录？\n\n源快照: "+label+"\n目标: "+tgtName+"\n\n目标 agent 的 resume 列表将出现此会话。"))return;
  }
  const btns=[$("#mig-outbox"),$("#mig-apply")];
  btns.forEach(b=>b&&(b.disabled=true));
  $("#mig-result").innerHTML='<div class="skel" style="width:50%"></div>';
  try{
    const r=await api("/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,target:t,apply:apply})});
    renderMigResult(r,{id,target:t,apply});
    toast(r.applied?"已写入会话目录":"已生成到 outbox");
  }catch(e){
    $("#mig-result").innerHTML='<div class="result-card err"><div class="rt">'+ERRIC+'迁移失败</div>'+
      '<div class="hint">'+esc(e.message)+"</div>"+
      '<div style="margin-top:8px"><button class="btn sm" onclick="migGo('+(apply?"true":"false")+')">重试</button></div></div>';
    toast("迁移失败："+e.message,1);
  }
  btns.forEach(b=>b&&(b.disabled=false));
}
function renderMigResult(r,ctx){
  const fpath=String(r.dest||"");
  const fname=fpath.split(/[\\/]/).pop();
  let cmd="",cmdNote="";
  if(ctx.target==="codex"&&r.new_sid){
    cmd=r.applied?("codex resume "+r.new_sid):"codex resume";
    cmdNote=r.applied?"session id 来自迁移结果, 可直接恢复":"outbox 产物 — 写入模式才会进会话目录; 或手动复制后再 resume";
  }else if(ctx.target==="codex"){
    cmd="codex resume";cmdNote="此产物无独立 session id (rollout 文件名内含) — 写入模式后 codex resume 列表可见";
  }else if(ctx.target==="claude"){
    cmd=r.applied?("claude --resume "+fname.replace(/\.jsonl$/i,"")):"claude --resume";
    cmdNote=r.applied?"会话 id = 写入文件名 (uuid)":"outbox 产物 — 写入模式才会进 projects 目录";
  }else{
    cmd="code \""+fpath+"\"";cmdNote="Markdown 简报 — 任意 agent / 编辑器可读";
  }
  $("#mig-result").innerHTML=
    '<div class="result-card"><div class="rt">'+GOOD+
    (r.applied?"已写入目标会话目录":"已生成到 vault/outbox/")+
    (ctx.target==="markdown"?"（交接简报）":"")+"</div>"+
    '<div class="path-row"><span class="hint">文件:</span><span class="mono" style="word-break:break-all">'+esc(fpath)+"</span>"+
    '<button class="btn sm" data-v="'+esc(fpath)+'" onclick="copyData(this,\'已复制路径\')">复制路径</button></div>'+
    '<div class="hint">'+num(r.lines)+" 行"+(r.dropped?" · 丢弃未配对 tool_result ×"+num(r.dropped):" · 无丢弃")+
    (r.new_sid?" · session id <span class='mono'>"+esc(r.new_sid)+"</span>":"")+"</div>"+
    '<div class="term"><span class="tp">'+esc(cmdNote)+"</span><code>$ "+esc(cmd)+
    '</code><br><button class="btn sm" style="margin-top:8px" data-v="'+esc(cmd)+'" onclick="copyData(this,\'已复制命令\')">复制命令</button></div></div>';
}

/* ================= 哨兵 ================= */
const probeSeq=seqGuard(null);
const shortModels=s=>{try{const m=JSON.parse(s);return m.by_agent?Object.entries(m.by_agent).map(([k,v])=>k+"×"+v).join(" · ")+" 活跃会话":""}catch(e){return""}};
function probeStateOf(rows){
  if(!rows.length)return {cls:"off",txt:"未配置 / 暂无探测记录"};
  const by={};
  rows.forEach(r=>{if(r.profile&&!(r.profile in by))by[r.profile]=r});
  const states=Object.values(by);
  const missing=states.some(r=>r.missing_models&&r.missing_models!=="[]");
  const err=states.some(r=>r.status==="http_error"||r.status==="error");
  if(missing)return {cls:"err",txt:"模型缺失 — 见事件详情"};
  if(err)return {cls:"err",txt:"存在失败节点 — 见事件详情"};
  const okAll=states.every(r=>r.status==="ok");
  if(okAll)return {cls:"ok",txt:states.length+" 个节点最近探测正常（基于最近样本）"};
  return {cls:"warn",txt:"部分节点待确认 — 见事件详情"};
}
async function loadProbe(){
  const el=$("#probe-list");
  el.innerHTML='<div class="skel" style="width:60%"></div><div class="skel" style="width:45%"></div>';
  try{
    const rows=await probeSeq(()=>api("/probe_events?limit=200"));
    const st=probeStateOf(rows);
    $("#probe-state").className="probe-state "+st.cls;
    $("#probe-state").innerHTML='<span class="dot '+st.cls+'"></span>'+esc(st.txt);
    $("#probe-last").textContent=rows.length?("最新事件 "+frel(rows[0].ts)+" · 30 秒自动刷新（仅本页可见时）"):"";
    /* 头部统计: 明确"最近样本"口径 */
    const nodes=new Set(rows.map(r=>r.profile).filter(Boolean));
    const okN=rows.filter(r=>r.status==="ok").length;
    const lats=rows.filter(r=>r.status==="ok"&&r.latency_ms).map(r=>r.latency_ms);
    $("#sn-nodes").textContent=nodes.size||"—";
    $("#sn-avail").textContent=rows.length?((okN/rows.length)*100).toFixed(1)+"%（"+okN+"/"+rows.length+"）":"—";
    $("#sn-avail-wrap").title="口径: 最近 "+rows.length+" 条事件 (即下方样本) 内 status=ok 的占比 — 非全天可用率";
    $("#sn-lat").textContent=lats.length?Math.round(lats.reduce((a,b)=>a+b,0)/lats.length)+" ms":"—";
    $("#sn-note").textContent=rows.length?("样本: 最近 "+rows.length+" 条事件（/probe_events limit=200）"):"尚无样本";
    /* 逐节点最新状态 */
    const by={};
    rows.forEach(r=>{if(r.profile&&!(r.profile in by))by[r.profile]=r});
    const profs=Object.entries(by);
    $("#probe-detail").innerHTML=profs.length?profs.map(([p,r])=>{
      const cls=r.status==="ok"?"ok":(r.status==="http_error"||r.status==="error")?"err":"warn";
      const txt=r.missing_models&&r.missing_models!=="[]"?("模型缺失 "+r.missing_models)
        :r.status==="ok"?("HTTP "+(r.http_code||200)+" · "+(r.latency_ms||"—")+" ms · 模型可用")
        :r.status==="switch"?("切换检测 · "+(shortModels(r.models)||"profile 变更"))
        :(r.status==="http_error"||r.status==="error")?("HTTP "+(r.http_code||"—")+" · 上游无响应 / 非 2xx")
        :esc(r.status||"—");
      return '<span class="chip" style="margin:0 8px 8px 0"><span class="dot '+cls+'" style="margin-right:6px"></span>'+relayBadge(p)+" "+esc(txt)+"</span>";
    }).join(""):"";
    const httpCell=r=>{
      if(r.status==="ok")return '<span class="pill ok">'+(r.http_code||200)+" OK</span>";
      if(r.status==="switch")return '<span class="pill warn">SWITCH</span>';
      if(r.status==="http_error"||r.status==="error")return '<span class="pill err">'+(r.http_code||"ERR")+" ERR</span>";
      return '<span class="pill warn">'+esc(r.status||"—")+"</span>"};
    const latCell=r=>{
      if(!r.latency_ms)return '<span class="hint">—</span>';
      const v=r.latency_ms;const cls=v<800?"good":v<=1500?"mid":"warm";
      const pct=Math.min(100,Math.round(v/2000*100));
      return '<span class="lat '+cls+'">'+v+" ms"+'<span class="latbar"><i style="width:'+pct+'%"></i></span></span>'};
    const detCell=r=>{
      if(r.missing_models&&r.missing_models!=="[]")return '<span style="color:var(--err)">模型消失 '+esc(r.missing_models)+"</span>";
      if(r.status==="ok")return '<span class="hint">模型可用性校验通过</span> <span class="mini-tag">GET /v1/models</span>';
      if(r.status==="switch")return '<span class="hint">切换检测 · '+esc(shortModels(r.models)||"profile 变更")+"</span>";
      if(r.status==="http_error"||r.status==="error")return '<span class="hint">上游无响应 / 非 2xx</span>';
      return '<span class="hint">—</span>'};
    el.innerHTML=rows.length?'<div class="cardbox">'+
      table(["时间","上游节点 (Profile)","HTTP 状态","响应延迟","详情"],
        rows.map(r=>{const cls=r.status==="ok"?"ok":(r.status==="warm"||r.status==="probe")?"warn":(r.status==="switch")?"sw":"err";
          return ['<span class="dot '+cls+'"></span><span class="mono nowr" title="'+esc(r.ts||"")+'">'+ftime(r.ts)+"</span>",
            relayBadge(r.profile),httpCell(r),latCell(r),detCell(r)]}))+"</div>"
      :'<div class="empty">'+WARN+'<div>暂无探测事件 — 还不能断言任何上游状态。</div>'+
       '<div class="hint" style="margin-top:6px;line-height:1.8">配置探测节点（二选一）：<br>'+
       '<code>~/.headroom/switcher-profiles.json</code> 或环境变量 <code>CV_RELAY_PROFILES=\'[{"id":"myrelay","openai_api_url":"https://host","default_model":"m"}]\'</code><br>'+
       '配置后点「立即探测上游」，或由托盘每 5 分钟自动探测。</div>'+
       '<button class="btn" onclick="probeNow()">立即探测上游</button></div>';
    el.dataset.loaded=1;
  }catch(e){
    if(e&&e.stale)return;
    el.innerHTML=errEmpty("哨兵事件加载失败："+e.message,"loadProbe()");
    $("#probe-state").className="probe-state err";
    $("#probe-state").innerHTML='<span class="dot err"></span>事件加载失败';
  }
}
let probing=false;
async function probeNow(){
  if(probing)return;probing=true;
  const b=$("#probe-btn");b&&(b.disabled=true);
  $("#probe-state").className="probe-state warn";
  $("#probe-state").innerHTML='<span class="dot warn"></span>探测中…';
  toast("探测中…");
  try{
    const r=await api("/probe",{method:"POST",headers:{"Content-Type":"application/json"}});
    const bad=r.alerts&&r.alerts.length;
    toast(bad?"发现异常："+r.alerts.map(a=>"["+a.profile+"] "+(a.kind==="model_missing"?"模型消失 "+JSON.stringify(a.detail):a.kind)).join("；")
      :"探测完成 — 本轮无新增异常",bad);
    delete $("#probe-list").dataset.loaded;loadProbe();
  }catch(e){
    toast("探测失败："+e.message,1);
    $("#probe-state").className="probe-state err";
    $("#probe-state").innerHTML='<span class="dot err"></span>探测请求失败';
  }
  b&&(b.disabled=false);probing=false;
}

/* ================= 启动 ================= */
const h0=(location.hash||"").slice(1);
setTab(TABS.includes(h0)?h0:"ov");
boot();
setInterval(()=>{  /* 哨兵 30s 轮询: 仅哨兵页可见且文档可见时 */
  if($("#t-probe").style.display!=="none"&&document.visibilityState==="visible"){
    delete $("#probe-list").dataset.loaded;loadProbe()}
},30000);
</script></body></html>
"""


class _KeyStub:
    key = None


def build_vault_server(vault: Path, port: int, token: str | None = None):
    """构建热索引 HTTP 服务 (不阻塞); cmd_serve 与 Pro 托盘共用, 避免实现漂移。
    token: 商业化/局域网档的可选鉴权 — 设置后所有请求需凭据 (X-Vault-Token 头 /
    Cookie vault_token / 首访 ?token= 自动 Set-Cookie), 未授权一律 401。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    sys.path.insert(0, str(Path(__file__).parent))
    from mcp_server import ledger_summary  # ledger_summary: /stats 端点 (曾漏导入致 500)
    idx = VaultIndex(vault, block_text)  # block_text 用本模块规范实现, 与 mcp 冷回退同源

    class H(BaseHTTPRequestHandler):
        # HTTP/1.0 下 Chromium 系 webview 会复用已被服务端关闭的连接, fetch 全部挂起
        # (HTML 导航走新连接所以页面能开, 数据接口全卡"加载中")。1.1 + 全端点 Content-Length = 正确 keep-alive。
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            # 请求落盘: 诊断"浏览器 fetch 挂起"类问题的关键证据 (请求到没到服务)。
            # token 脱敏 (B03): ?token= 会随 requestline 落盘, 不能明文进日志。
            line = re.sub(r"([?&])token=[^& ]*", r"\1token=***", self.requestline)
            try:
                with (vault / "serve_http.log").open("a", encoding="utf-8") as fh:
                    fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} "
                             f"{self.client_address[0]} \"{line}\" {code} "
                             f"UA={str(self.headers.get('User-Agent', ''))[:48]}\n")
            except OSError:
                pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            """统一响应出口: _new_cookie (首访 ?token= 种 cookie) 对 HTML 与 JSON 一视同仁 —
            曾只在 _json 发, 致控制台首访 HTML 后的 fetch 全部 401。"""
            self.send_response(code)
            nc = getattr(self, "_new_cookie", None)
            if nc:
                self.send_header("Set-Cookie", nc)
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", code)

        def _stats_overlay(self, sid: str, s: dict) -> dict:
            """快照文件不可变: zcode 历史版本的用量在索引层回填 (backfill_zcode_usage), 文件里
            仍是 0 — 详情/迁移预览优先展示索引里的回填值, 不再对有用量的会话显示"0 tokens"。"""
            if s.get("usage_events"):
                return s
            try:
                c = db(vault)
                r = c.execute("SELECT tokens_in, tokens_out, reasoning_tokens, cache_read, cache_write,"
                              " total_tokens, usage_events FROM snapshots WHERE id=?", (sid,)).fetchone()
                c.close()
            except sqlite3.Error:
                return s
            return {**s, **dict(r)} if r and r["usage_events"] else s

        @staticmethod
        def _sid_ok(sid: str) -> bool:
            # 快照 id 形态校验 (cv_ + hex): 挡路径穿越与任意文件读; 同时给迁移一个
            # 稳定的 JSON 400/404, 而不是让 load_snapshot 的 SystemExit 逃过 except 断连
            import re as _re
            return bool(_re.fullmatch(r"cv_[0-9a-f]{8,64}", str(sid or "")))

        def _guard(self, post: bool) -> bool:
            """本地访问防护: Host 必须回环 (挡 DNS rebinding 读会话史); Origin 若带必须回环
            (挡恶意网页跨站); POST 强制 application/json (跨站简单请求发不出该类型 = 强制预检,
            预检无 CORS 应答即被浏览器拦截)。本地进程 (MCP 热索引/托盘/CLI) 不受影响。"""
            host = (self.headers.get("Host") or "").strip().lower()
            hn = host.rsplit(":", 1)[0].strip("[]") if host.count(":") == 1 else host.strip("[]")
            if hn not in ("127.0.0.1", "localhost", "::1"):
                self._json({"error": f"forbidden: host '{host}' 不在本地白名单"}, 403)
                return False
            org = self.headers.get("Origin")
            if org:
                if (urlparse(org).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                    self._json({"error": f"forbidden: origin '{org}' 非本地"}, 403)
                    return False
            if post:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    self._json({"error": "forbidden: POST 需要 Content-Type: application/json"}, 403)
                    return False
            if token:  # 鉴权档: 头 / Cookie / 首访 query(种 cookie) 三种凭据形态
                given = self.headers.get("X-Vault-Token")
                if not given:
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        if part.strip().startswith("vault_token="):
                            given = part.split("=", 1)[1].strip()
                            break
                if not given and not post:
                    given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
                if hmac.compare_digest(str(given), str(token)):
                    if "vault_token=" not in (self.headers.get("Cookie") or ""):
                        self._new_cookie = f"vault_token={token}; Path=/; HttpOnly; SameSite=Strict"
                    return True
                self._json({"error": "unauthorized: 需要 token (?token= 首访, 或 X-Vault-Token 头)"}, 401)
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._guard(False):
                return
            try:
                if u.path == "/":
                    self._send(CONSOLE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if u.path == "/healthz":
                    idx.request_rebuild(60)  # 后台单飞重建; 本请求拿现态秒回, 增长期绝不阻塞
                    st = idx._state  # 单次读取: 快照数与词项数出自同一代索引
                    return self._json({"service": "context-vault", "status": "ok",
                                       "snapshots": len(st[1]), "terms": len(st[0])})
                if u.path == "/audit":
                    return self._json(audit_summary_and_timeline(
                        vault, q.get("since", ""), "all"))
                if u.path == "/memory":
                    try:
                        mlim = int(q.get("limit", 8))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    return self._json({"query": q.get("q", ""),
                                       "memories": memory_search_api(vault, q.get("q", ""), mlim)})
                if u.path == "/probe_events":
                    conn = db(vault)  # ThreadingHTTPServer 每请求短连接; 显式关闭归还 WAL 句柄
                    try:
                        plim = int(q.get("limit", 100))
                    except ValueError:
                        conn.close()
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    rows = conn.execute("SELECT ts,profile,status,http_code,latency_ms,models,missing_models"
                                        " FROM probe_events ORDER BY id DESC LIMIT ?",
                                        (plim,)).fetchall()
                    conn.close()
                    return self._json([dict(r) for r in rows])
                if u.path == "/ledger":
                    conn = db(vault)
                    conn.executescript(SCHEMA)  # 只 connect 不建表会留零表空库毒化冷路径 (全面测试 M5)
                    try:
                        mem_n = conn.execute("SELECT COUNT(*) n FROM memory_items").fetchone()["n"]
                    except sqlite3.OperationalError:
                        mem_n = 0
                    prices = {}
                    prices_p = vault / "prices.json"  # {"模型": {"input":x,"output":y,"cache_read":z}} $/Mtok
                    if prices_p.exists():
                        try:
                            pj = json.loads(prices_p.read_text(encoding="utf-8"))
                            prices = {m: v for m, v in pj.items()
                                      if isinstance(v, dict) and "input" in v and "output" in v}
                        except (ValueError, AttributeError, OSError):
                            pass  # 非法 JSON/顶层非 dict/prices 是目录: 无价目诚实显示, 不 500
                    zcode_usage = {}
                    zcode_snapshots = 0
                    try:  # 真相源: zcode 主库 model_usage (全量调用); model-io 审计文件仅零头
                        zdb = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
                        try:
                            for r in zdb.execute("SELECT model_id, COUNT(*), COALESCE(SUM(input_tokens),0),"
                                                 " COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_input_tokens),0)"
                                                 " FROM model_usage GROUP BY model_id"):
                                zcode_usage.setdefault(r[0] or "未知",
                                                       {"n": r[1], "tin": r[2], "tout": r[3], "cr": r[4]})
                        finally:
                            zdb.close()
                    except sqlite3.Error:
                        pass  # 无 zcode / 无表 = 无自有用量, 合法空态
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
                        try:  # 值级坏形状 (字符串/null/list) 只损失该模型成本, 不拖垮整个 /ledger
                            a["cost"] = round((a["tin"] * float(pr["input"]) + a["tout"] * float(pr["output"])
                                               + a["cr"] * float(pr.get("cache_read") or 0)) / 1e6, 2)
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
                    raw = json.loads(lt["raw"]) if lt else {}
                    return self._json({"lifetime": {"tokens_saved": lt["tokens_saved"] if lt else 0,
                                                    "saved_usd": lt["cost_usd"] if lt else 0, **raw},
                                       "by_model": [dict(r) for r in by_model],
                                       "monthly": [dict(r) for r in monthly],
                                       "owned": owned_stats(vault), "memories": mem_n,
                                       "zcode_usage": zcode_usage, "zcode_snapshots": zcode_snapshots})
                if u.path == "/preview":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    env2 = idx.load_env(qid)
                    if not env2:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    blocks = env2["conversation"]
                    dist = dict(Counter(b["type"] for b in blocks))
                    live, comp = live_context(blocks)
                    live_dist = dict(Counter(b["type"] for b in live))
                    s = self._stats_overlay(qid, env2["stats"])
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
                    try:
                        lim = int(q.get("limit", 5))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    return self._json(idx.search(q.get("q", ""), lim))
                if u.path == "/context":
                    idx.rebuild()
                    try:
                        lim = int(q.get("limit", 3))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    return self._json(idx.context(q.get("q", ""), lim))
                if u.path == "/recent":
                    idx.request_rebuild(60)  # 后台单飞重建; 分页总数与徽章同源同新鲜度且秒回
                    st = idx._state  # 真正的单次读取: 曾写成两次独立下标, 换装插进中间时 KeyError 500
                    meta, sids = st[1], st[2]
                    total = len(meta)  # 可列出库存 = 唯一对外口径: 徽章/资产卡/分页同一数字
                    try:
                        lim = max(1, min(int(q.get("limit", 5)), 200))
                        off = max(int(q.get("offset", 0)), 0)
                    except ValueError:
                        return self._json({"error": "invalid limit/offset: 需要整数"}, 400)
                    desc = q.get("order", "desc") != "asc"
                    # 迭代只追加的 sids 而非 dict.items(): 增量插入并发时 dict 迭代会 RuntimeError
                    pairs = [(sid, meta[sid]) for sid in sids]
                    versions = {}
                    if q.get("leaves") == "1":
                        # 每个会话只列演进链的当前版本 (叶子), 附该会话的版本数 (×N 徽章);
                        # 口径与 owned_stats 一致 (库内 parent_snapshot), 库不可读时退化为全量
                        try:
                            c = db(vault)
                            parents = {r[0] for r in c.execute(
                                "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
                            per_sess = {(r[0], r[1]): r[2] for r in c.execute(
                                "SELECT agent, session_ref, COUNT(*) FROM snapshots GROUP BY agent, session_ref")}
                            sess_of = {r[0]: (r[1], r[2]) for r in c.execute(
                                "SELECT id, agent, session_ref FROM snapshots")}
                            c.close()
                            pairs = [kv for kv in pairs if kv[0] not in parents]
                            versions = {sid: per_sess.get(sess_of.get(sid), 1) for sid, _ in pairs}
                        except sqlite3.Error:
                            pass
                        total = len(pairs)  # leaves 模式分页按会话数
                    rows = sorted(pairs, key=lambda kv: kv[1]["created_at"] or "",
                                  reverse=desc)[off:off + lim]
                    return self._json({"total": total, "versions_total": len(meta),
                                       "rows": [{"sid": k, **v, **({"versions": versions[k]}
                                                                   if k in versions else {})}
                                                for k, v in rows]})
                if u.path == "/timeline":
                    # 会话时间线: 该 session 的全部版本按天分组 (T3 leaves 的下钻视图)
                    ref = q.get("session", "")
                    agent = q.get("agent", "")
                    if not ref:
                        return self._json({"error": "invalid session: 需要 session_ref"}, 400)
                    try:
                        c = db(vault)
                        rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                         " parent_snapshot FROM snapshots WHERE session_ref=? AND agent=?"
                                         " ORDER BY created_at", (ref, agent or "%")).fetchall()
                        c.close()
                    except sqlite3.Error:
                        rows = []
                    days = {}
                    for r in rows:
                        days.setdefault(str(r["created_at"] or "")[:10], []).append(
                            {"sid": r["id"], "time": str(r["created_at"] or "")[11:19],
                             "tokens_in": r["tokens_in"] or 0, "total_tokens": r["total_tokens"] or 0,
                             "blocks": r["blocks_total"] or 0,
                             "leaf": r["id"] not in {x[0] for x in rows if x[4]}})
                    return self._json({"session": ref, "agent": agent,
                                       "days": [{"day": d, "versions": days[d]}
                                                for d in sorted(days)]})
                if u.path == "/stats":
                    return self._json({"stats": ledger_summary(vault), "owned": owned_stats(vault)})
                if u.path == "/snapshot":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    env = idx.load_env(qid)
                    if not env:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    dist = dict(Counter(b["type"] for b in env["conversation"]))
                    return self._json({"id": q.get("id"), "capture": env["capture"],
                                       "stats": self._stats_overlay(qid, env["stats"]), "block_dist": dist})
                self._json({"error": "unknown route",
                            "routes": ["/", "/healthz", "/search?q=", "/context?q=", "/preview?id=",
                                       "/recent", "/timeline?session=", "/stats", "/snapshot?id=", "/ledger", "/probe_events",
                                       "POST /migrate", "POST /probe"]}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            import urllib.parse as _up
            u = _up.urlparse(self.path)
            # 先按 Content-Length 读净 body 再守卫: 守卫拒绝 (401/403) 若不读 body 就应答,
            # 残留字节会被 keep-alive 连接的下一轮当成请求行解析, 污染整条连接 (全面测试 M1)
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return
            raw = self.rfile.read(n) if n > 0 else b""
            if not self._guard(True):
                return
            try:
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._json({"error": "invalid JSON body"}, 400)  # 客户端错误, 不落 500 兜底
                if u.path == "/migrate":
                    # 回归: 不存在的 sid 曾让 load_snapshot 抛 SystemExit (BaseException),
                    # 逃过 except Exception 断连无 JSON; 先校验形态与存在性, 稳定 400/404
                    mid = str(body.get("id", ""))
                    if not self._sid_ok(mid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    if not (vault / "snapshots" / f"{mid}.json").exists():
                        return self._json({"error": f"snapshot not found: {mid}"}, 404)
                    try:
                        r = do_import(vault, mid, body.get("target", "codex"),
                                      bool(body.get("apply")))
                    except SnapshotNotFound as e:  # 存在性检查到加载之间被删的竞态兜底
                        return self._json({"error": str(e)}, 404)
                    except BadMigrateTarget as e:  # HTTP 层没有 argparse choices, 未知 target 挡在这
                        return self._json({"error": str(e)}, 400)
                    return self._json(r)
                if u.path == "/probe":
                    baseline_p = vault / "probe_baseline.json"
                    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
                    alerts = probe_once(vault, load_profiles(), resolve_key(_KeyStub()), baseline,
                                        verbose=False)
                    atomic_write_text(baseline_p, json.dumps(baseline, ensure_ascii=False, indent=1))
                    return self._json({"alerts": alerts})
                self._json({"error": "unknown route"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    # 首选端口可能落进 Windows 动态保留区段 (Hyper-V/WinNAT 重启后漂移, 绑定报 WinError 10013) —
    # 托盘曾把任何绑定失败都当成"已有实例"直接退出, 11-06 起整整停摆。现在: 首选端口失败时先探活,
    # 真有 Context Vault 实例在跑才按单实例语义抛出; 否则依次退到备用端口, 实际地址写 serve.json
    srv, last_err = None, None
    for cand in ([port] if port == 0 else [port, *SERVE_FALLBACK_PORTS, 0]):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", cand), H)
            break
        except OSError as e:
            if cand == port and _cv_service_alive(port):
                raise  # 另一个 Context Vault 实例已占用: 交调用方处理 (托盘单实例退出)
            last_err = e
    if srv is None:
        raise last_err
    actual = srv.server_address[1]
    if port and actual != port:
        print(f"  ⚠ 端口 {port} 不可用 ({last_err}) — 改用 {actual}, 实际地址见 vault/serve.json",
              file=sys.stderr)
    try:  # 服务发现: MCP 热路径/托盘按此找到实际地址 (显式 CV_VAULT_SVC 仍优先)
        atomic_write_text(vault / "serve.json", json.dumps(
            {"url": f"http://127.0.0.1:{actual}", "port": actual, "pid": os.getpid(),
             "started": now_iso()}))
    except OSError:
        pass
    return srv, idx


SERVE_FALLBACK_PORTS = [18791, 28791]


def _cv_service_alive(port: int) -> bool:
    """该端口上是否已有 Context Vault 服务在应答 (区分"重复实例"与"端口被系统保留/他用")。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz",
                                     headers={"User-Agent": "ContextVault/0.1"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("service") == "context-vault"
    except urllib.error.HTTPError as e:
        return e.code == 401  # 鉴权档实例拒绝无凭据探活: 也说明是我们的服务
    except Exception:
        return False


def cmd_serve(args):
    token = args.token or os.environ.get("CV_SERVE_TOKEN")
    srv, idx = build_vault_server(vault_dir(args), args.port, token=token)
    port = srv.server_address[1]  # 首选端口被保留时会退到备用端口: 打印实际地址
    st = idx._state
    print(f"vault 热索引就绪: {len(st[1])} 快照, {len(st[0])} 词项")
    print(f"Context Vault 服务已启动: http://127.0.0.1:{port} (Ctrl+C 退出)")
    if token:
        print("  鉴权已启用: 首访 http://127.0.0.1:{}/?token=*** 自动登录; "
              "MCP/脚本侧设 CV_SERVE_TOKEN 同值".format(port))
    print("  MCP server 自动发现本服务走热索引 (vault/serve.json; CV_VAULT_SVC 可显式覆盖)")
    print("  本地访问防护已启用: Host/Origin 回环校验 + POST 强制 application/json"
          " (浏览器跨站与 DNS rebinding 会被 403 拦截, 本地进程不受影响)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")

# ------------------------------------------------- backup: vault 备份 (一致性 + 轮换)

def cmd_backup(args):
    """vault 一致性备份: vault.db 走 sqlite 在线 backup API (WAL 下直接拷文件可能撕裂),
    snapshots/ 原样打包 (不可变), 基线/水位随包; 按时间戳轮换保留 N 份。"""
    import zipfile
    vault = vault_dir(args)
    if not (vault / "vault.db").exists():
        raise SystemExit(f"vault 未初始化: {vault} (先 cv init / cv export)")
    out_dir = Path(args.out).expanduser() if args.out else vault / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-f%03d" % (dt.datetime.now().microsecond // 1000))
    out = out_dir / f"cv-backup-{ts}.zip"
    tmp_db = out_dir / f".backup-tmp-{os.getpid()}.db"
    n_snap = 0
    try:
        src = db(vault)
        dst = sqlite3.connect(tmp_db)
        src.backup(dst)  # 在线一致快照, 不锁常驻服务 (托盘 serve/sync 可同时跑)
        dst.close()
        src.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "vault.db")
            for f in sorted((vault / "snapshots").glob("cv_*.json")):
                z.write(f, f"snapshots/{f.name}")
                n_snap += 1
            for name in ("probe_baseline.json", "relay_watermark.json"):
                p = vault / name
                if p.exists():
                    z.write(p, name)
    finally:
        tmp_db.unlink(missing_ok=True)
    backups = sorted(out_dir.glob("cv-backup-*.zip"))
    for old in backups[:-int(args.keep)] if int(args.keep) > 0 and len(backups) > int(args.keep) else []:
        old.unlink()
    size_mb = out.stat().st_size / 1048576
    print(f"备份完成: {out} ({n_snap} 快照, {size_mb:.1f} MB, 留存 {min(len(backups), int(args.keep))}/{args.keep} 份)")
    print("  恢复: 解压到空目录, 以 --vault 指向它 (vault.db + snapshots/ 完整可用)")


# ------------------------------------------------- doctor: 环境体检

def cmd_doctor(args):
    try:  # 中文 Windows GBK 控制台下 ✓/✗ 会 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
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
    i.add_argument("--target", choices=["codex", "claude", "markdown"], required=True)
    i.add_argument("--apply", action="store_true", help="写入真实会话目录 (默认 dry-run)")
    i.add_argument("--strip-thinking", action="store_true", help="不迁移 reasoning 摘要")
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="一键: 导出→预览→导入")
    m.add_argument("source")
    m.add_argument("--target", choices=["codex", "claude", "markdown"], required=True)
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
    sy.add_argument("--debounce", action="store_true",
                    help="周期同步用: 活跃会话 30 分钟最多存一版 (CV_SNAPSHOT_INTERVAL_MIN 可调), 空闲后捕获最终态")
    sy.set_defaults(func=cmd_sync)

    eh = sub.add_parser("export-harness", help="快照 → 任意 harness 会话文件 (18 目标, 经 smigrate)")
    eh.add_argument("snapshot_id")
    eh.add_argument("--to", required=True, choices=SMIGRATE_TARGETS)
    eh.set_defaults(func=cmd_export_harness)
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
    sv.add_argument("--token", default=None,
                    help="启用鉴权 (或环境变量 CV_SERVE_TOKEN): 客户端首访 ?token= 自动登录, 脚本用 X-Vault-Token 头")
    sv.set_defaults(func=cmd_serve)

    b = sub.add_parser("backup", help="vault 一致性备份 (sqlite 在线快照 + snapshots 打包, 轮换保留)")
    b.add_argument("--out", default=None, help="备份目录 (默认 vault/backups; 建议定期指向外部盘)")
    b.add_argument("--keep", type=int, default=10, help="保留份数 (默认 10, 旧的自动清理)")
    b.set_defaults(func=cmd_backup)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("repair", help="一次性数据修复: lineage 索引回填 + zcode 历史用量回填 (幂等)"
                   ).set_defaults(func=cmd_repair)
    pr = sub.add_parser("prune", help="中间版本 GC: 每会话每天保留最后一版 + 叶子; fork/孤本整链保留")
    pr.add_argument("--apply", action="store_true", help="执行 (默认 dry-run; 执行前自动做一次 vault 备份)")
    pr.set_defaults(func=cmd_prune)


    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (SnapshotNotFound, BadMigrateTarget) as e:
        # CLI 边界统一转进程退出 (领域层抛可捕获异常, HTTP 层映射 404/400, 这里兜底)
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
