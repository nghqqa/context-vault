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
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from collections import OrderedDict
import hmac
import uuid
from pathlib import Path

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from cv_ui import load_console_html as _load_console_html, load_dashboard_html as _load_dashboard_html
from cv_index import (  # noqa: F401 — re-exported as public engine API
    VaultIndex,
    ColdRanker,
    DEFAULT_WEIGHTS,
    IDENT_BOOST,
    UNIFORM,
    block_text,
    decay_factor,
    fold_chains,
    format_snapshot_messages,
    ident_set,
    rrf_fuse,
    score_blocks,
    tokenize,
)

FORMAT_VERSION = "0.1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # noconsole 进程 spawn 控制台程序不弹窗   # context-snapshot 格式版本
VERSION = "0.6.0"          # 工具版本 (发布节奏独立于格式)
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
  parent_snapshot TEXT, fork_of TEXT, migrated_from TEXT, migration_baseline TEXT,
  cwd TEXT, parent_session_ref TEXT, summary_title TEXT);
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
    raw = str(getattr(args, "vault", None) or "vault")
    p = Path(raw).expanduser()
    if raw == "vault" and not (p / "vault.db").exists() and getattr(args, "cmd", None) != "init":
        repo_vault = Path(__file__).resolve().parent.parent / "vault"
        if (repo_vault / "vault.db").exists():
            return repo_vault
    return p


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
    for col in ("parent_snapshot", "fork_of", "migrated_from", "migration_baseline",
                "cwd", "parent_session_ref", "summary_title"):
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
    conn.executescript(SCHEMA + SYNC_TABLE)
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
                           "content": content,
                           "is_error": bool(obj.get("is_error")
                                            or (isinstance(out, dict) and out.get("is_error"))),
                           "ts": ts})
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
                                       "content": text, "is_error": bool(b.get("is_error")), "ts": ts})
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
             "fork_of,migrated_from,migration_baseline,cwd,parent_session_ref,summary_title")

_GENERIC_ORIGINATORS = {"codex_cli_rs", "codex_vscode", "claude-code", "antigravity"}


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
        (sid, envelope["created_at"], c.get("agent"), c.get("source_format"), c.get("provider"),
         c.get("model"), c.get("session_ref"), c.get("originator"), c.get("completeness"),
         s["tokens_in"], s["tokens_out"], s["reasoning_tokens"], s["cache_read"], s["cache_write"],
         s["total_tokens"], s["cost_usd"], s["usage_events"], s["blocks_total"], s["blocks_dropped"],
         str(path), lin.get("parent_snapshot"), lin.get("fork_of"), lin.get("migrated_from"),
         json.dumps(base) if base else None,
         str(c.get("cwd") or ""),
         str(c.get("parent_session_ref") or ""),
         _derive_summary_title(envelope)))


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
    conn.executescript(SCHEMA + SYNC_TABLE)  # 曾只建 SCHEMA: 全新 vault 上向 sync_sources INSERT 直接崩 (二轮 migrate-1)
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
    try:  # 同会话已有演进链 (手动 export 落在 sync 链之后 / vault.db 重建后重注册) → 挂为最新版本
        c0 = envelope["capture"]
        if c0.get("session_ref") and c0.get("agent"):
            sib = conn.execute("SELECT id FROM snapshots WHERE agent=? AND session_ref=? AND id<>?"
                               " ORDER BY created_at DESC LIMIT 1",
                               (c0.get("agent"), c0.get("session_ref"), sid)).fetchone()
            row = conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?", (sid,)).fetchone()
            if sib and row and not row[0]:  # 只补缺失的 parent, 不动既有链 (H1)
                conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (sib[0], sid))
                envelope["lineage"]["parent_snapshot"] = sib[0]
    except sqlite3.Error:
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

def _norm_project_match(cwd: str, query: str) -> bool:
    if not query:
        return True
    nc = cwd.replace("\\", "/").rstrip("/").lower()
    nq = query.replace("\\", "/").rstrip("/").lower()
    if not nq:
        return True
    if not nc:
        return False
    base = nc.rsplit("/", 1)[-1]
    return nc == nq or base == nq or nq in nc


def _zcode_refs_matching(db_path: Path, table: str, id_col: str,
                         conds_by_col: list[tuple[str, str]]) -> set[str]:
    """从 ZCode SQLite 只读查询满足条件的 session/task id 集合（列或库缺失时安全返回空集）。"""
    if not db_path.exists():
        return set()
    try:
        zdb = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in zdb.execute(f"PRAGMA table_info({table})")}
            if id_col not in cols:
                return set()
            conds = [sql for col, sql in conds_by_col if col in cols]
            if not conds:
                return set()
            return {r[0] for r in zdb.execute(
                f"SELECT {id_col} FROM {table} WHERE {' OR '.join(conds)}") if r[0]}
        finally:
            zdb.close()
    except sqlite3.Error:
        return set()


def _norm_win_path(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _zcode_deleted_project_task_refs(db_path: Path) -> set[str]:
    """从 ZCode v2 tasks-index.sqlite 查找属于已删除项目（projects.deleted != 0）的 task_id。"""
    if not db_path.exists():
        return set()
    try:
        zdb = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in zdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "tasks" not in tables or "projects" not in tables:
                return set()
            tcols = {r[1] for r in zdb.execute("PRAGMA table_info(tasks)")}
            pcols = {r[1] for r in zdb.execute("PRAGMA table_info(projects)")}
            if not {"task_id", "project_id"} <= tcols or not {"project_id", "deleted"} <= pcols:
                return set()
            refs = {r[0] for r in zdb.execute(
                "SELECT t.task_id FROM tasks t JOIN projects p ON t.project_id = p.project_id "
                "WHERE COALESCE(p.deleted, 0) != 0 AND t.task_id IS NOT NULL"
            ) if r[0]}
            # 兼容未回填 project_id 的历史任务：仅当工作区根属于已删项目且不属于任何活跃项目时视作已删
            if "project_roots" in tables and "workspace_path" in tcols:
                rcols = {r[1] for r in zdb.execute("PRAGMA table_info(project_roots)")}
                if {"project_id", "root_path"} <= rcols:
                    act_roots = {_norm_win_path(r[0]) for r in zdb.execute(
                        "SELECT pr.root_path FROM projects p JOIN project_roots pr "
                        "ON p.project_id = pr.project_id "
                        "WHERE COALESCE(p.deleted, 0) = 0 AND pr.root_path IS NOT NULL"
                    ) if r[0]}
                    del_roots = {_norm_win_path(r[0]) for r in zdb.execute(
                        "SELECT pr.root_path FROM projects p JOIN project_roots pr "
                        "ON p.project_id = pr.project_id "
                        "WHERE COALESCE(p.deleted, 0) != 0 AND pr.root_path IS NOT NULL"
                    ) if r[0]} - act_roots
                    if del_roots:
                        for tid, wp in zdb.execute(
                            "SELECT task_id, workspace_path FROM tasks "
                            "WHERE (project_id IS NULL OR project_id = '') "
                            "AND task_id IS NOT NULL AND workspace_path IS NOT NULL"
                        ):
                            if _norm_win_path(wp) in del_roots:
                                refs.add(tid)
            return refs
        finally:
            zdb.close()
    except sqlite3.Error:
        return set()


def _zcode_subagent_refs() -> set[str]:
    return _zcode_refs_matching(ZCODE_DB, "session", "id", [
        ("parent_id", "parent_id IS NOT NULL"),
    ])


def _zcode_archived_refs() -> set[str]:
    """ZCode 已归档/已删除主会话 id：兼容 CLI session 表、桌面端 v2 tasks 及已删项目。"""
    return (
        _zcode_refs_matching(ZCODE_DB, "session", "id", [
            ("time_deleted", "time_deleted IS NOT NULL"),
            ("time_archived", "time_archived IS NOT NULL"),
        ])
        | _zcode_refs_matching(ZCODE_TASKS_DB, "tasks", "task_id", [
            ("archived", "COALESCE(archived, 0) != 0"),
            ("deleted", "COALESCE(deleted, 0) != 0"),
            ("cli_deleted", "COALESCE(cli_deleted, 0) != 0"),
        ])
        | _zcode_deleted_project_task_refs(ZCODE_TASKS_DB)
    )


def _parse_agent_filter(agent: str | None) -> list[str]:
    if not agent:
        return []
    return sorted({a.strip().lower() for a in str(agent).split(",") if a.strip()})


def resolve_top_conversations(
    vault: Path,
    project: str | None = None,
    agent: str | None = None,
    include_subagents: bool = False,
    limit: int = 0,
    include_archived: bool = False,
) -> list[dict]:
    """解析 vault 中每个会话最新的顶层主对话（叶子去重 + 过滤 ZCode 子代理与归档会话）。
    索引加速: cwd / parent_session_ref / summary_title 缓存在 snapshots 索引列, 老行首扫懒回填后免解析大 JSON。"""
    conn = db(vault)
    try:
        try:
            parents = {r[0] for r in conn.execute(
                "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
            q = ("SELECT id, agent, session_ref, originator, blocks_total, created_at, path,"
                 " cwd, parent_session_ref, summary_title FROM snapshots")
            params = []
            agents = _parse_agent_filter(agent)
            if agents:
                ph = ",".join("?" * len(agents))
                q += f" WHERE lower(agent) IN ({ph})"
                params.extend(agents)
            rows = conn.execute(q, params).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()

    # 每个会话仅保留最新/最多块的一条叶子快照，消除分裂链重复
    best_by_sess = {}
    for r in rows:
        if r["id"] in parents:
            continue
        key = (r["agent"] or "", r["session_ref"] or r["id"])
        score = (r["blocks_total"] or 0, r["created_at"] or "", r["id"])
        prev = best_by_sess.get(key)
        if prev is None or score > prev[0]:
            best_by_sess[key] = (score, r)

    hidden_zcode_refs = set()
    if not include_subagents:
        hidden_zcode_refs |= _zcode_subagent_refs()
    if not include_subagents and not include_archived:
        hidden_zcode_refs |= _zcode_archived_refs()

    items = []
    backfill_rows = []
    for _, r in best_by_sess.values():
        sid = r["id"]
        ag = r["agent"] or ""
        sref = r["session_ref"] or ""
        if ag == "zcode" and sref in hidden_zcode_refs:
            continue
        p = Path(r["path"]) if r["path"] else (vault / "snapshots" / f"{sid}.json")
        if not p.exists():
            alt = vault / "snapshots" / f"{sid}.json"
            if not alt.exists():
                continue
            p = alt
        if r["cwd"] is not None and r["parent_session_ref"] is not None and r["summary_title"] is not None:
            cwd = r["cwd"]
            parent_sref = r["parent_session_ref"]
            raw_orig = str(r["originator"] or "")
            title = r["summary_title"] or raw_orig
        else:
            try:
                env = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            cap = env.get("capture") or {}
            cwd = str(cap.get("cwd") or "")
            parent_sref = str(cap.get("parent_session_ref") or "")
            raw_orig = str(cap.get("originator") or r["originator"] or "")
            title = _derive_summary_title(env) or raw_orig
            backfill_rows.append((cwd, parent_sref, title, sid))
        if not include_subagents:
            if parent_sref or raw_orig.startswith("workflow subagent"):
                continue
        if project and not _norm_project_match(cwd, project):
            continue
        items.append({
            "id": sid,
            "agent": ag,
            "session_ref": sref,
            "cwd": cwd,
            "title": title,
            "blocks_total": r["blocks_total"] or 0,
            "created_at": r["created_at"] or "",
        })

    if backfill_rows:
        try:
            wconn = db(vault)
            try:
                wconn.executemany(
                    "UPDATE snapshots SET cwd=?, parent_session_ref=?, summary_title=? WHERE id=?",
                    backfill_rows,
                )
                wconn.commit()
            finally:
                wconn.close()
        except sqlite3.Error:
            pass

    items.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)
    if limit and limit > 0:
        items = items[:limit]
    return items


def _prefer_exact_project(items: list[dict], project: str) -> list[dict]:
    """存在完整路径或目录名精确命中时优先精确结果，避免 paper-studio 误吞 paper-studio-team。"""
    nq = project.replace("\\", "/").rstrip("/").lower()
    exact = [it for it in items
             if (it.get("cwd") or "").replace("\\", "/").rstrip("/").lower() == nq]
    if exact:
        return exact
    base_exact = [it for it in items
                  if (it.get("cwd") or "").replace("\\", "/").rstrip("/").lower().rsplit("/", 1)[-1] == nq]
    return base_exact or items


def _resolve_project_items(
    vault: Path,
    project: str,
    include_subagents: bool = False,
    limit: int = 0,
    include_archived: bool = False,
    agent: str | None = None,
) -> list[dict]:
    """按项目筛选顶层主对话；存在完整路径或目录名精确命中时优先精确结果，避免 paper-studio 误吞 paper-studio-team。"""
    if project == "__ungrouped__":
        items = [it for it in resolve_top_conversations(
                     vault, agent=agent, include_subagents=include_subagents,
                     include_archived=include_archived)
                 if _is_ungrouped_cwd(it["cwd"])]
    else:
        items = resolve_top_conversations(
            vault, project=project, agent=agent,
            include_subagents=include_subagents, include_archived=include_archived)
        items = _prefer_exact_project(items, project)
    if limit and limit > 0:
        items = items[:limit]
    return items


def _is_ungrouped_cwd(cwd: str) -> bool:
    if not cwd:
        return True
    return "/.zcode/workspace/" in cwd.replace("\\", "/").lower()


def group_conversations_by_project(
    vault: Path,
    project: str | None = None,
    include_subagents: bool = False,
    include_archived: bool = False,
    agent: str | None = None,
) -> dict:
    """将顶层主对话按项目文件夹归组（ZCode 侧栏树语义），未分组任务归到末尾。"""
    if project == "__ungrouped__":
        all_items = resolve_top_conversations(
            vault, agent=agent, include_subagents=include_subagents,
            include_archived=include_archived)
        items = [it for it in all_items if _is_ungrouped_cwd(it["cwd"])]
    else:
        items = resolve_top_conversations(
            vault, project=project, agent=agent,
            include_subagents=include_subagents, include_archived=include_archived)

    grouped = {}
    ungrouped = []
    for it in items:
        cwd = it["cwd"] or ""
        if _is_ungrouped_cwd(cwd):
            ungrouped.append(it)
            continue
        norm = cwd.replace("\\", "/").rstrip("/")
        name = norm.rsplit("/", 1)[-1] or cwd
        grp = grouped.get(cwd)
        if grp is None:
            grp = {
                "name": name,
                "cwd": cwd,
                "is_ungrouped": False,
                "latest": it["created_at"] or "",
                "conversations": [],
            }
            grouped[cwd] = grp
        grp["conversations"].append(it)
        if (it["created_at"] or "") > grp["latest"]:
            grp["latest"] = it["created_at"] or ""

    projects = sorted(grouped.values(), key=lambda g: (g["latest"], g["name"]), reverse=True)
    for g in projects:
        g["count"] = len(g["conversations"])
    if ungrouped:
        projects.append({
            "name": "未分组任务",
            "cwd": "__ungrouped__",
            "is_ungrouped": True,
            "latest": max((it["created_at"] or "" for it in ungrouped), default=""),
            "count": len(ungrouped),
            "conversations": ungrouped,
        })
    return {
        "total_projects": len(projects),
        "total_conversations": len(items),
        "projects": projects,
    }


def cmd_list(args):
    vault = vault_dir(args)
    if getattr(args, "top", False) or getattr(args, "project", None) or getattr(args, "agent", None):
        items = resolve_top_conversations(
            vault,
            project=getattr(args, "project", None),
            agent=getattr(args, "agent", None),
            include_subagents=getattr(args, "include_subagents", False),
            include_archived=getattr(args, "include_archived", False),
        )
        if not items:
            print("(未找到匹配的顶层主对话)")
            return
        groups = {}
        for it in items:
            groups.setdefault(it["cwd"] or "(无项目)", []).append(it)
        for cwd, convs in groups.items():
            print(f"📁 {cwd} ({len(convs)} 条主对话):")
            for c in convs:
                title = (c["title"] or "(无标题)")[:60]
                print(f"  {c['id']}  {c['created_at'][:10]}  块={c['blocks_total']:<5}  {title}")
        return
    conn = db(vault)
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

def _deterministic_session_id(env: dict) -> str:
    """同一源会话 (agent+session_ref) 恒定派生 uuid4 格式 id。
    重复导出/会话演进后重导出 = 目标端同名覆盖 (原位更新), 不再产生并排副本;
    uuid5 摘要改写版本位为 4 — smigrate/_require_uuid4 与各目标格式都强校验 uuid4。"""
    ref = (env.get("capture") or {}).get("session_ref") or env.get("_sid") or snapshot_id(env)
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
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9-]", "-", cwd.replace("\\", "/"))


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
    rng = _session_epoch_range(env)
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
        or _antigravity_first_user_text(env) or "(导入会话)"
    return {"session": {"id": sess_id, "project_id": _zcode_project_id(cwd) if cwd else None,
                        "slug": sess_id, "directory": cwd, "path": cwd, "title": title[:120],
                        "time_created": t0, "time_updated": t1},
            "messages": messages, "parts": parts}


def do_import_zcode(vault: Path, env: dict, sid: str, apply: bool,
                    session_id: str | None = None, db_path: Path | None = None) -> dict:
    """写回 ZCode 主库 (实验性): 默认 dry-run 出行数预览; --apply 先备份再事务写入。
    会话 id 确定性派生 → 重导 = 同会话原位更新 (messages/parts 重写)。
    ZCode 运行中内存有缓存, 写入后需重启 ZCode 才能在会话列表看到。"""
    sess = blocks_to_zcode(env, False, session_id=session_id)
    sess_id = sess["session"]["id"]
    dest = str(db_path or ZCODE_DB)
    if not apply:
        preview = vault / "outbox" / f"zcode-import-{sid[:14]}.json"
        atomic_write_text(preview, json.dumps(
            {"session_id": sess_id, "dest": dest, "title": sess["session"]["title"],
             "messages": len(sess["messages"]), "parts": len(sess["parts"]),
             "cwd": sess["session"]["directory"]}, ensure_ascii=False, indent=1))
        return {"lines": len(sess["messages"]), "dropped": 0, "dest": str(preview),
                "new_sid": sess_id, "applied": False}
    zdb = Path(dest)
    if not zdb.exists():
        raise BadMigrateTarget(f"ZCode 主库不存在: {zdb}")
    backup_dir = vault / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"zcode-import-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.sqlite"
    shutil.copy2(zdb, backup)
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
            raise BadMigrateTarget(f"写入后完整性检查失败: {ok} (备份: {backup.name})")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_migration(vault, zdb, sid, baseline=None)
    return {"lines": len(sess["messages"]), "dropped": 0,
            "dest": f"{dest} (备份 {backup.name}; 重启 ZCode 后生效)",
            "new_sid": sess_id, "applied": True}


def estimate_text_tokens(text: str) -> int:
    """文本 token 粗估 (预算装配用, 诚实标注为估算): CJK ≈ 1 字/token, ASCII ≈ 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4 + 1


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
           f"- 来源快照: `{env.get('_sid', snapshot_id(env))}` (可用 cv import --target codex|claude 精确迁移)"]

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


def _record_migration(vault: Path, dest: Path, sid: str, baseline=None):
    with (vault / "migrations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"dest": _norm_path(dest), "source": sid, "baseline": baseline,
                             "at": now_iso()}, ensure_ascii=False) + "\n")


def do_import(vault: Path, sid: str, target: str, apply: bool, strip_thinking=False,
              session_id: str | None = None, budget_tokens: int | None = None) -> dict:
    """迁移执行核心 (CLI 与控制台 HTTP 共用)。返回 {lines, dropped, dest, new_sid, applied}。
    session_id 缺省按源会话 (agent+session_ref) 确定性派生: 同一会话重复导出/演进后重导出
    在目标端同名覆盖 (原位更新), 不再产生并排副本。antigravity apply 同时登记
    conversation_summaries 索引行 (只落 .db 不建索引 = 永不可见)。"""
    if target not in ("markdown", "codex", "claude", "antigravity", "zcode") \
            and target not in SMIGRATE_TARGETS:
        raise BadMigrateTarget(f"未知迁移目标: {target!r} (可选 {'|'.join(SMIGRATE_TARGETS)}|markdown|zcode)")
    env = load_snapshot(vault, sid)
    if session_id is None:
        session_id = _deterministic_session_id(env)
    if target == "antigravity":
        exe = find_smigrate()
        if not exe:
            raise BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        res = _export_one_harness(vault, exe, sid, "antigravity", quiet=True, create_project=apply,
                                  session_id=session_id)
        out_db = Path(res["dest"])
        agy_sid = _antigravity_read_cascade_id(out_db)
        dest_str = str(out_db)
        if apply:
            if not agy_sid:
                raise BadMigrateTarget("Antigravity 导出文件缺少 cascade_id，无法写入会话目录")
            conv_dir = HOME / ".gemini" / "antigravity" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            target_db = conv_dir / f"{agy_sid}.db"
            tmp_db = conv_dir / f".{agy_sid}.tmp-{os.getpid()}"
            shutil.copy2(out_db, tmp_db)
            os.replace(tmp_db, target_db)
            dest_str = str(target_db)
            _antigravity_register_summaries(target_db, env, agy_sid)
            _record_migration(vault, target_db, sid, baseline=None)
        return {"lines": res["lines"], "dropped": res["dropped"], "dest": dest_str,
                "new_sid": agy_sid, "applied": apply}
    if target == "zcode":
        return do_import_zcode(vault, env, sid, apply)
    if target not in ("markdown", "codex", "claude"):
        # 其余 15 个 harness 目标: outbox 档 (写入各家私有目录属平台安装器职责, 不自动落盘)
        exe = find_smigrate()
        if not exe:
            raise BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        res = _export_one_harness(vault, exe, sid, target, quiet=True, session_id=session_id)
        return {"lines": res["lines"], "dropped": res["dropped"], "dest": res["dest"],
                "new_sid": None, "applied": False}
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    if target == "markdown":  # 交接简报: 可携降级档, 不写真实会话目录, 无 apply 语义
        dest = vault / "outbox" / f"brief-{sid}.md"
        lines_text = blocks_to_brief(env, budget_tokens=budget_tokens)
        atomic_write_text(dest, lines_text)
        return {"lines": len(lines_text.splitlines()), "dropped": 0, "dest": str(dest),
                "new_sid": None, "applied": False}
    if target == "codex":
        lines, dropped, new_sid, name = blocks_to_codex(env, strip_thinking, session_id=session_id)
        dest = vault / "outbox" / name
        if apply:
            existing = sorted(CODEX_SESSIONS.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
            if existing:  # 确定性 id: 已有同名会话 = 原位更新, 避免 resume 按 uuid 歧义
                dest = existing[-1]
            else:
                day = dt.datetime.now()
                dest = CODEX_SESSIONS / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        lines, dropped = blocks_to_claude(env, strip_thinking, session_id=session_id)
        name = f"{session_id}.jsonl"
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
        _record_migration(vault, dest, sid, baseline=base)
    return {"lines": len(lines), "dropped": dropped, "dest": str(dest),
            "new_sid": new_sid if target == "codex" else session_id if target == "claude" else None,
            "applied": apply}


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


def _check_single_sid_agent(vault: Path, sid: str, agent: str | None):
    agents = _parse_agent_filter(agent)
    if not agents:
        return
    env = load_snapshot(vault, sid)
    src_ag = ((env.get("capture") or {}).get("agent") or "").lower()
    if src_ag not in agents:
        raise BadMigrateTarget(f"快照 {sid} 来源为 {src_ag or 'unknown'!r}，不符合指定 --agent={agent!r}")


def cmd_import(args):
    vault = vault_dir(args)
    sid = getattr(args, "snapshot_id", None)
    project = getattr(args, "project", None)
    req_agent = getattr(args, "agent", None)
    if sid:
        _check_single_sid_agent(vault, sid, req_agent)
        r = do_import(vault, sid, args.target, args.apply, args.strip_thinking,
                      budget_tokens=getattr(args, "budget_tokens", None))
        _print_import_result(r, args.target)
        return
    if project:
        items = _resolve_project_items(
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
            r = do_import(vault, it["id"], args.target, args.apply, args.strip_thinking,
                          budget_tokens=getattr(args, "budget_tokens", None))
            _print_import_result(r, args.target)
        print(f"\n共导入 {len(items)} 条主对话 → {args.target}")
        return
    raise SystemExit("请指定 snapshot_id 或 --project <项目名/路径>")


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
    a.budget_tokens = getattr(args, "budget_tokens", None)
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
    if not (vault / "vault.db").exists():
        # 无库: 直接返回零口径, 不在读取路径上凭空建库 — 曾让冷路径"账本为空。先运行…"的
        # 诚实指引被空表 vault.db 永久替换 (二轮 mcp-3)
        return {"snapshots": 0, "disk_snapshots": 0, "sessions": 0, "tokens_in": 0, "tokens_out": 0,
                "cache_read": 0, "cache_write": 0, "total_tokens": 0, "input_effective": 0,
                "cache_hit_rate": 0.0, "by_agent": [],
                "source": "vault-owned (no vault.db yet)"}
    snap_dir = vault / "snapshots"
    try:
        disk_snaps = sum(1 for e in os.scandir(snap_dir)
                         if e.is_file() and e.name.startswith("cv_") and e.name.endswith(".json")) \
            if snap_dir.is_dir() else 0
    except OSError:
        disk_snaps = 0
    conn = db(vault)
    try:
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
    finally:
        conn.close()
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
    return {"snapshots": versions, "disk_snapshots": disk_snaps, "sessions": len(rows),
            "tokens_in": ti, "tokens_out": to,
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
    key = _norm_path(path)  # Windows 大小写失配曾让 export/sync 各写一行水位, 同文件双键 (二轮 ledger-1)
    row = conn.execute("SELECT mtime, size, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                       (key,)).fetchone()
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
                         " VALUES(?,?,?,?,?)", (key, st.st_mtime, st.st_size, sid, now_iso()))
            return "skip"
        envelope["lineage"]["parent_snapshot"] = row["snapshot_id"]  # 同源演进 → 会话树
    out = vault / "snapshots" / f"{sid}.json"
    registered = conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone()
    if not out.exists():
        atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
    if not registered:
        # H1 同款守卫: 内容寻址 sid 已在库 (历史版本) 时不得用 parent=None 的 envelope
        # 重注册 — 会抹掉链上 parent (托盘升级换键首轮曾把全库演进链抹断)
        register_snapshot(conn, envelope, sid, out)
    conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                 " VALUES(?,?,?,?,?)", (key, st.st_mtime, st.st_size, sid, now_iso()))
    return "update" if row else "new"


ZCODE_DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
ZCODE_TASKS_DB = HOME / ".zcode" / "v2" / "tasks-index.sqlite"
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
                if out is not None or state.get("status") == "error":
                    # status=error 的工具调用 output 常为空, 错误信息在 state.error
                    content, _ = clip(str(out if out is not None else state.get("error") or ""))
                    blocks.append({"type": "tool_result", "call_id": call_id,
                                   "content": content,
                                   "is_error": state.get("status") == "error", "ts": ts})
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


ANTIGRAVITY_CONV_DIR = HOME / ".gemini" / "antigravity" / "conversations"


def _sync_antigravity_dbs(conn, vault: Path, debounce=False) -> tuple:
    """Antigravity 本地会话回灌: ~/.gemini/antigravity/conversations/*.db → 快照。
    键 antigravity:<cascade>; smigrate convert --format antigravity --to claude 中转后按
    claude-projects 适配器解析 (原生库 source=1 需 smigrate 本机补丁才能读, 见 memory)。
    自导入会话 (migrations.jsonl dest 命中) 跳过, 防导出↔回灌回环吸入。"""
    exe = find_smigrate()
    if not exe or not ANTIGRAVITY_CONV_DIR.exists():
        return (0, 0, 0)
    imported: set[str] = set()
    for v in load_migrations(vault).values():
        stem = Path(v.get("dest", "")).stem
        if stem:
            imported.add(stem.lower())
    outbox = vault / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    n_new = n_upd = 0
    for dbp in sorted(ANTIGRAVITY_CONV_DIR.glob("*.db")):
        cascade = dbp.stem.lower()
        if cascade in imported:
            continue
        key = f"antigravity:{cascade}"
        try:
            st = dbp.stat()
        except OSError:
            continue
        prev = conn.execute("SELECT mtime, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                            (key,)).fetchone()
        if prev and abs((prev["mtime"] or 0) - st.st_mtime) < 1:
            continue
        if debounce and prev and prev["snapshot_id"] and _debounced(prev["synced_at"], st.st_mtime):
            continue
        tmp = outbox / f"agy-src-{dbp.stem}.jsonl"
        try:
            r = subprocess.run([exe, "convert", str(dbp), "--format", "antigravity",
                                "--to", "claude", "--output", str(tmp)],
                               capture_output=True, text=True, timeout=300,
                               creationflags=NO_WINDOW)
            if r.returncode != 0 or not tmp.exists():
                continue
            exported = export_claude_code(tmp, False)
        except Exception:
            continue
        finally:
            tmp.unlink(missing_ok=True)
        if exported["stats"]["blocks_total"] == 0:
            continue
        exported["capture"]["agent"] = "antigravity"
        exported["capture"]["source_format"] = "antigravity-db"
        envelope = build_envelope(exported)
        sid = snapshot_id(envelope)
        if prev and prev["snapshot_id"]:
            if prev["snapshot_id"] == sid:
                conn.execute("INSERT OR REPLACE INTO sync_sources"
                             "(path,mtime,size,snapshot_id,synced_at) VALUES(?,?,?,?,?)",
                             (key, st.st_mtime, st.st_size, sid, now_iso()))
                continue
            envelope["lineage"]["parent_snapshot"] = prev["snapshot_id"]
        out = vault / "snapshots" / f"{sid}.json"
        if not out.exists():
            atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        register_snapshot(conn, envelope, sid, out)
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (key, st.st_mtime, st.st_size, sid, now_iso()))
        n_upd += 1 if prev else 0
        n_new += 0 if prev else 1
    return (n_new, n_upd, 0)


def sync_vault(vault: Path, root: Path | None = None, debounce: bool = False, quiet: bool = False) -> dict:
    """增量扫描本地多工具会话源并写入 vault（CLI 与 HTTP /sync 共用）。"""
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    warnings: list[str] = []
    t0 = time.time()
    try:
        conn.executescript(SCHEMA + SYNC_TABLE)
        n_lin = backfill_lineage(conn)       # 每 vault 一次: 历史演进链索引回填
        n_zu = backfill_zcode_usage(conn)    # 每 vault 一次: zcode 历史快照用量回填
        if not conn.execute("SELECT 1 FROM vault_meta WHERE key='sync_keys_norm_v1'").fetchone():
            # 水位键规范化迁移: _norm_path 引入前 sync_sources 存在原始大小写键, 与新键失配曾让
            # 升级首轮把全部源当新会话重导出 (链被 parent=None envelope 抹断, 2026-11-08 实发)
            rows = conn.execute("SELECT path, mtime, size, snapshot_id, synced_at FROM sync_sources").fetchall()
            seen = {}
            for r in rows:
                k = _norm_path(r["path"])
                old = seen.get(k)
                if old is None or (r["mtime"] or 0) > (old["mtime"] or 0):
                    seen[k] = r
            conn.execute("DELETE FROM sync_sources")
            for r in seen.values():
                conn.execute("INSERT OR REPLACE INTO sync_sources VALUES(?,?,?,?,?)",
                             (_norm_path(r["path"]), r["mtime"], r["size"], r["snapshot_id"], r["synced_at"]))
            conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('sync_keys_norm_v1', ?)", (now_iso(),))
            conn.commit()
        if (n_lin or n_zu) and not quiet:
            print(f"  一次性修复: lineage 回填 {n_lin} 行 · zcode 用量回填 {n_zu} 行")
        migrations = load_migrations(vault)
        files = scan_sources(root)
        n_new = n_upd = n_skip = n_empty = 0
        for f in sorted(files):
            try:
                r = sync_one(conn, vault, f, migrations, debounce)
            except (SystemExit, Exception) as e:
                msg = f"跳过 {f.name}: {str(e)[:80]}"
                warnings.append(msg)
                if not quiet:
                    print(f"  ! {msg}")
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
            n_new += z_new
            n_upd += z_upd
        except Exception as e:
            msg = f"zcode 库同步跳过: {str(e)[:80]}"
            warnings.append(msg)
            if not quiet:
                print(f"  ! {msg}")
        try:
            a_new, a_upd, _ = _sync_antigravity_dbs(conn, vault, debounce)
            n_new += a_new
            n_upd += a_upd
        except Exception as e:
            msg = f"antigravity 会话回灌跳过: {str(e)[:80]}"
            warnings.append(msg)
            if not quiet:
                print(f"  ! {msg}")
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    return {
        "new": n_new,
        "updated": n_upd,
        "skipped": n_skip,
        "empty": n_empty,
        "total": total,
        "elapsed": round(time.time() - t0, 2),
        "lineage_backfill": n_lin,
        "zcode_usage_backfill": n_zu,
        "warnings": warnings,
    }


def cmd_sync(args):
    res = sync_vault(
        vault_dir(args),
        root=args.root,
        debounce=bool(getattr(args, "debounce", False)),
        quiet=False,
    )
    print(f"sync 完成: 新增 {res['new']} · 更新 {res['updated']} · 跳过 {res['skipped']}(未变/异常)"
          f" · 空会话 {res['empty']} | vault 总快照 {res['total']} | 耗时 {res['elapsed']:.1f}s")


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
    removed = 0
    for r in drop:
        try:
            os.unlink(r["path"])  # 内容寻址文件; 索引层由下一次 rebuild 全量重建自然剔除
        except OSError:
            continue  # Windows 共享冲突 (并发读者持句柄): 盘库同进退 — 留行下次 prune 再试, 不产生孤儿
        conn.execute("DELETE FROM snapshots WHERE id=?", (r["id"],))
        removed += 1
    conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (now_iso(), "vault", "gc", "prune", None, None,
                  json.dumps({"dropped": removed, "reclaimed_mb": round(reclaim / 1e6, 1),
                              "kept": len(rows) - len(drop),
                              "backup": "vault/backups 最新一份"}, ensure_ascii=False), None))
    conn.commit()
    print(f"已回收 {removed} 版 / {reclaim/1e6:.0f} MB"
          + (f" ({len(drop) - removed} 版因文件被占用跳过, 下次 prune 再试)" if removed != len(drop) else "")
          + " (GC 事件已写入审计时间线)")


def storm_relink(conn) -> int:
    """断链重链 (2026-11-08 风暴修复): 同会话出现 ≥2 个 parent=NULL 的根 = 演进链被抹断
    (水位键失配首轮 sync 曾把全库源当新会话重注册)。按快照文件 mtime (= 版本被捕获的
    真实时序) 恢复每会话链条。fork 子树按 fork_of 分组独立成链; 无 session_ref 的行不参与。"""
    rows = conn.execute("SELECT id, agent, session_ref, parent_snapshot, fork_of, path"
                        " FROM snapshots").fetchall()
    groups = {}
    for r in rows:
        if not r["session_ref"]:
            continue
        try:
            mt = os.path.getmtime(r["path"]) if r["path"] else 0
        except OSError:
            mt = 0
        groups.setdefault((r["agent"], r["session_ref"], r["fork_of"] or ""), []).append((mt, r))
    n = 0
    for lst in groups.values():
        roots = [x for x in lst if not x[1]["parent_snapshot"]]
        if len(roots) < 2:
            continue  # 一链一叶是健康态
        lst = sorted(lst, key=lambda x: x[0])
        prev = None
        for mt, r in lst:
            if r["parent_snapshot"] != prev:
                conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (prev, r["id"]))
                n += 1
            prev = r["id"]
    conn.commit()
    return n


def cmd_repair(args):
    """一次性数据修复的显式入口 (cv sync 首次运行也会自动做): 从快照文件回填 lineage 索引列,
    zcode 历史快照按版本时间回填用量。幂等, 可重复执行。"""
    vault = vault_dir(args)
    conn = db(vault)
    n_lin = backfill_lineage(conn, force=True)
    n_zu = backfill_zcode_usage(conn, force=True)
    n_st = storm_relink(conn)
    own = owned_stats(vault)
    print(f"lineage 回填: {n_lin} 行 · zcode 用量回填: {n_zu} 行 · 断链重链: {n_st} 行")
    print(f"账本口径: {own['sessions']} 独立会话 / {own['snapshots']} 快照版本 · "
          f"规范化总输入 {own['input_effective']:,} tok · 命中率 {own['cache_hit_rate']*100:.1f}%")


# ------------------------------------------------- memory: M3 记忆层 (零依赖规则引擎)
# 实现已解耦至 tools/cv_memory.py (与 cv_index/cv_ui 同模式): cv 侧只留 CLI/HTTP 薄壳。
# re-export 保持 cv.* 公开符号不变 (tests / mcp_server / serve 路由共用);
# 本行位置即原定义位 — db/SCHEMA/now_iso 已就绪, cv_memory 内部为函数级惰性获取。
from cv_memory import (  # noqa: F401,E402
    MEMORY_TABLE,
    DECISION_MARKERS,
    PREFERENCE_MARKERS,
    PITFALL_MARKERS,
    CONSTRAINT_MARKERS,
    SUPERSEDE_MARKERS,
    MAX_MEMORY_PER_SNAPSHOT,
    extract_memories,
    extract_memories_v2,
    ensure_memory_schema,
    recompute_supersession,
    ENGINE_PROTOCOL_DOC,
    RuleMemoryEngine,
    TemporalMemoryEngine,
    load_memory_engine,
    extract_memories_incremental,
    memory_dedup,
    memory_counts_api,
    memory_search_api,
)


def cmd_memory(args):
    if not hasattr(args, "engine"):
        args.engine = None  # search/list 子命令未注册 --engine, 统一兜底
    vault = vault_dir(args)
    engine_name = args.engine or os.environ.get("CV_MEMORY_ENGINE", "v2")
    if args.memory_cmd == "extract":
        res = extract_memories_incremental(
            vault,
            engine_name=engine_name,
            force=bool(getattr(args, "force", False)),
            snapshot_id=getattr(args, "snapshot", None),
        )
        print(f"记忆提取完成: {res['total']} 条 (新增处理 {res['scanned']} 快照 / +{res['added']} 条,"
              f" engine={res['engine']}) · "
              + " · ".join(f"{k}×{v}" for k, v in sorted(res["by_kind"].items())))
        return
    if args.memory_cmd == "dedup":
        res = memory_dedup(vault)
        print(f"记忆去重完成: 合并 -{res['removed']} 条重复摘录, 剩余 {res['total']} 条 · "
              + " · ".join(f"{k}×{v}" for k, v in sorted(res["by_kind"].items())))
        return
    conn = db(vault)
    try:
        # 老库迁移必须走 ensure_memory_schema (先补列再建索引 — 直接 executescript
        # MEMORY_TABLE 会在缺 topic_key 列的老库上炸 no such column)
        ensure_memory_schema(conn)
        if args.memory_cmd == "search":
            history = bool(getattr(args, "history", False))
            ms = memory_search_api(vault, args.query, args.limit,
                                   include_history=history)
            if not ms:
                print(f"无相关记忆: {args.query}")
                return
            for m in ms:
                tag = ""
                if (m.get("status") or "active") != "active":
                    tag = f" [已推翻 ← #{m.get('superseded_by')}]"
                prov = m.get("project") or "?"
                if m.get("block_idx") is not None:
                    prov += f" · {m.get('snapshot', '')[:18]}#{m['block_idx']}"
                if m.get("event_ts"):
                    prov += f" · {str(m['event_ts'])[:16]}"
                print(f"[{m['kind']:<10}]{tag} {m['content'][:100]}  ({prov})")
        elif args.memory_cmd == "list":
            q = "SELECT kind, COUNT(*) n FROM memory_items GROUP BY kind"
            for r in conn.execute(q):
                print(f"  {r[0]:<10} ×{r[1]}")
            hist = "WHERE status='active'" if not getattr(args, "history", False) else ""
            for r in conn.execute(f"SELECT * FROM memory_items {hist} ORDER BY id DESC LIMIT ?",
                                  (args.limit,)):
                prov = r["project"] or "?"
                if r["block_idx"] is not None:
                    prov += f" · {r['snapshot_id'][:18]}#{r['block_idx']}"
                tag = "" if (r["status"] or "active") == "active" else " [已推翻]"
                print(f"[{r['kind']:<10}]{tag} {r['content'][:90]}  ({prov})")
    finally:
        conn.close()


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


# ------------------------------------------------- fork: 快照分叉 (M3)

SMIGRATE_TARGETS = ("claude", "codex", "pi", "omp", "opencode", "copilot", "antigravity",
                    "cursor", "vibe", "muse", "qwen", "kimi", "grok", "kilo", "openhands",
                    "hermes", "mastracode", "devin")


def _export_one_harness(vault: Path, exe: str, sid: str, to: str, quiet: bool = False,
                        create_project: bool = False, session_id: str | None = None) -> dict:
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    # ① 快照 → claude 中转 (复用已验证的 do_import)
    mid = do_import(vault, sid, "claude", apply=False)
    # ② claude → 目标 harness
    ts = int(time.time())
    ext = {"claude": "jsonl", "codex": "jsonl"}.get(to, "json")
    out = vault / "outbox" / f"harness-{to}-{sid[:14]}-{ts}.{ext}"
    cmd = [exe, "convert", mid["dest"], "--to", to, "--output", str(out)]
    if session_id:
        cmd += ["--session-id", session_id]  # 确定性 id: 同快照重导出幂等, 批量导入以之去重
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=300, creationflags=NO_WINDOW)
    if r.returncode != 0 or not out.exists():
        msg = f"smigrate convert 失败: {(r.stderr or r.stdout)[:300]}"
        if quiet:
            raise BadMigrateTarget(msg)
        raise SystemExit(msg)
    if to == "antigravity":
        # 先标准化 trajectory_meta (source=1) 与项目元数据，再按原生 CortexStep.metadata
        # 完整结构同步写入 steps.metadata 与 step_payload Field 5 (否则重整重置 blob 并显示 '2027y')
        env_snap = load_snapshot(vault, sid)
        ws_folder = (env_snap.get("capture") or {}).get("cwd") or ""
        rng = _session_epoch_range(env_snap)
        _antigravity_set_workspace(out, ws_folder, rng[1] if rng else int(time.time()),
                                   create_project=create_project)
        n = _antigravity_fill_step_timestamps(out, env_snap)
        _antigravity_normalize_steps(out)  # planner 文本 f1→f3 等原生形状对齐 (否则会话打不开)
        if n and not quiet:
            print(f"  已补 {n} 步原生格式 metadata 与 step_payload 时间戳")
    if not quiet:
        print(f"已导出 → {to}: {out}")
        print(f"  ({mid['lines']} 行会话, stateless replay; 按目标 harness 的方式导入该文件即可)")
    return {"dest": str(out), "lines": mid["lines"], "dropped": mid["dropped"]}


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
    sid = getattr(args, "snapshot_id", None)
    project = getattr(args, "project", None)
    req_agent = getattr(args, "agent", None)
    if sid:
        _check_single_sid_agent(vault, sid, req_agent)
        env = load_snapshot(vault, sid)
        _export_one_harness(vault, exe, sid, args.to,
                            session_id=_deterministic_session_id(env))  # 确定性 id: 重导幂等
        return
    if project:
        items = _resolve_project_items(
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
            _export_one_harness(vault, exe, it["id"], args.to,
                                session_id=_deterministic_session_id(
                                    load_snapshot(vault, it["id"])))
        print(f"\n共导出 {len(items)} 条主对话 → {args.to}")
        return
    raise SystemExit("请指定 snapshot_id 或 --project <项目名/路径>")


def fork_snapshot(vault: Path, source_sid: str, message: str | None = None) -> dict:
    """在历史节点开分支: 复制快照为新内容寻址实体, lineage.fork_of 指向原快照 (CLI 与 POST /fork 共用)。"""
    env = load_snapshot(vault, source_sid)
    env["lineage"]["fork_of"] = source_sid
    if message:
        env["capture"]["fork_note"] = message
    sid = snapshot_id(env)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        try:
            existing_fork_of = (json.loads(out.read_text(encoding="utf-8")).get("lineage") or {}).get("fork_of")
        except (ValueError, OSError):
            existing_fork_of = None
        if existing_fork_of != source_sid:
            sid = "cv_" + hashlib.sha256(
                canonical_bytes(env) + b"|fork_of:" + str(source_sid).encode()
            ).hexdigest()[:16]
            out = vault / "snapshots" / f"{sid}.json"
    existed = out.exists()
    if not existed:
        atomic_write_text(out, json.dumps(env, ensure_ascii=False, indent=1))
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        register_snapshot(conn, env, sid, out)
        conn.commit()
    finally:
        conn.close()
    return {"fork_sid": sid, "source_sid": source_sid, "existed": existed,
            "message": message or "", "path": str(out)}


def cmd_fork(args):
    """在历史节点开分支: 复制快照为新内容寻址实体, lineage.fork_of 指向原快照。"""
    vault = vault_dir(args)
    res = fork_snapshot(vault, args.snapshot_id, getattr(args, "message", None))
    sid = res["fork_sid"]
    if res["existed"]:
        print(f"该分叉已存在 (幂等): {sid}.json")
    else:
        print(f"已分叉: {args.snapshot_id} → {sid}"
              + (f" · 备注: {args.message}" if getattr(args, "message", None) else ""))
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
    rows = []
    try:
        rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        if kind != "all":
            rows = [r for r in rows if kind in (r["status"],)]
        total = conn.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0]
        model_missing = sum(1 for r in conn.execute(
            "SELECT missing_models FROM probe_events WHERE missing_models != '[]'"
            " AND missing_models IS NOT NULL"))
        switches = conn.execute("SELECT COUNT(*) FROM probe_events WHERE status='switch'").fetchone()[0]
        ok_probes = conn.execute("SELECT COUNT(*) FROM probe_events WHERE status='ok'").fetchone()[0]
        prune_n = conn.execute("SELECT COUNT(*) FROM probe_events WHERE status='prune'").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        return {"since": since or "(全部)", "total_events": 0,
                "summary": {"ok_probes": 0, "abnormal": 0, "switches": 0, "model_missing_events": 0},
                "timeline": []}  # 零表空库: 诚实空态 (M5 同族, 二轮 serve-7)
    conn.close()
    err_probes = total - ok_probes - switches - prune_n  # prune=GC 留痕, 非探测异常
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


def _install_mcp_antigravity(vault: Path) -> str:
    gemini_dir = HOME / ".gemini"
    if not gemini_dir.exists():
        return "未找到 ~/.gemini (antigravity 未安装)"
    cfg = gemini_dir / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    raw = cfg.read_text(encoding="utf-8").strip() if cfg.exists() else ""
    if raw:
        data = json.loads(raw)
        cfg.with_suffix(".json.bak-cv").write_text(json.dumps(data, indent=2), encoding="utf-8")
    servers = data.setdefault("mcpServers", {})
    if "context-vault" in servers:
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    servers["context-vault"] = {"command": py, "args": args}
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "已写入 ~/.gemini/config/mcp_config.json"


def cmd_mcp(args):
    vault = vault_dir(args)
    clients = {"claude": _install_mcp_claude, "codex": _install_mcp_codex,
               "cursor": _install_mcp_cursor, "antigravity": _install_mcp_antigravity}
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
        agy = HOME / ".gemini" / "config" / "mcp_config.json"
        print(f"[antigravity] {'✓ 已配置' if agy.exists() and 'context-vault' in agy.read_text(encoding='utf-8') else '✗ 未安装'}")


# ------------------------------------------------- ingest: 经 session-migrate 接入任意 harness

SMIGRATE_CANDIDATES = ("smigrate", os.path.expanduser("~/.local/bin/smigrate"))


def _varint(v: int) -> bytes:
    out = b""
    while True:
        b7 = v & 0x7F
        v >>= 7
        out += bytes([b7 | (0x80 if v else 0)])
        if not v:
            return out


def _rd_varint(b: bytes, i: int):
    v, sh = 0, 0
    while True:
        b7 = b[i]; i += 1
        v |= (b7 & 0x7F) << sh; sh += 7
        if not b7 & 0x80:
            return v, i


def _pb_len(k: int, data: bytes) -> bytes:
    return _varint((k << 3) | 2) + _varint(len(data)) + data


def _pb_var(k: int, v: int) -> bytes:
    return _varint((k << 3) | 0) + _varint(v)


def _pb_decode(data: bytes) -> tuple:
    """解码 wire-format protobuf 为 ({(field_num, wire_type): [values]}, 未知尾部)。"""
    fields = {}
    i = 0
    while i < len(data):
        pos = i
        try:
            k, i = _rd_varint(data, i)
            w, kn = k & 7, k >> 3
            if kn == 0:
                return fields, data[pos:]
            if w == 0:
                v, i = _rd_varint(data, i)
                fields.setdefault((kn, 0), []).append(v)
            elif w == 2:
                ln, i = _rd_varint(data, i)
                if i + ln > len(data):
                    return fields, data[pos:]
                fields.setdefault((kn, 2), []).append(data[i:i + ln])
                i += ln
            elif w == 5:
                if i + 4 > len(data):
                    return fields, data[pos:]
                fields.setdefault((kn, 5), []).append(data[i:i + 4])
                i += 4
            elif w == 1:
                if i + 8 > len(data):
                    return fields, data[pos:]
                fields.setdefault((kn, 1), []).append(data[i:i + 8])
                i += 8
            else:
                return fields, data[pos:]
        except IndexError:
            return fields, data[pos:]
    return fields, b""


def _pb_encode(fields: dict, tail: bytes = b"") -> bytes:
    """按 (field_num, wire_type) 升序编码 protobuf（支持多字节 tag）。"""
    out = []
    for kn, w in sorted(fields):
        tag = _varint((kn << 3) | w)
        for v in fields[(kn, w)]:
            if w == 0:
                out.append(tag + _varint(v))
            elif w == 2:
                out.append(tag + _varint(len(v)) + v)
            else:
                out.append(tag + v)
    if tail:
        out.append(tail)
    return b"".join(out)


def _session_epoch_range(env: dict):
    """源会话块的 (最早, 最晚) epoch 秒; 无块时间戳时回退 env.created_at; 均无返回 None。"""
    blocks = [b for b in (env.get("conversation") or []) if isinstance(b, dict) and b.get("ts")]

    def to_epoch(s):
        try:
            return int(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return None

    epoch = [t for t in (to_epoch(b["ts"]) for b in blocks) if t is not None]
    if epoch:
        return (min(epoch), max(epoch))
    c_ts = to_epoch(env.get("created_at")) if env.get("created_at") else None
    return (c_ts, c_ts) if c_ts is not None else None


def _antigravity_build_step_metadata(
    sec: int, step_type: int, status: int, idx: int,
    trajectory_id: str, cascade_id: str, turn_id: int, is_last: bool,
) -> bytes:
    """按原生 CortexStepMetadata protobuf 构造单步 metadata：
    f1=created_at, f3=step_source(4=user/2=agent), f6=completed_at, f12=exec_uuid,
    f20={f1:trajectory_id, f2:idx, f4:cascade_id}, f26=status_transitions, f35=step_hash。"""
    import hashlib
    ts_msg = _pb_var(1, max(1, int(sec)))
    exec_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{cascade_id or 'cv'}:turn:{turn_id}")).encode("ascii")
    step_hash = hashlib.md5(f"{cascade_id}:{idx}".encode("utf-8")).hexdigest().encode("ascii")
    req = (
        (_pb_len(1, trajectory_id.encode("ascii")) if trajectory_id else b"")
        + (_pb_var(2, idx) if idx > 0 else b"")
        + (_pb_len(4, cascade_id.encode("ascii")) if cascade_id else b"")
    )
    st_val = status if status else 3
    trans = _pb_len(1, _pb_var(1, st_val) + _pb_len(2, ts_msg))
    parts = [
        _pb_len(1, ts_msg),
        _pb_var(3, 4 if step_type == 14 else 2),
    ]
    if step_type != 14 or is_last:
        parts.append(_pb_len(6, ts_msg))
    parts.append(_pb_len(12, exec_uuid))
    if req:
        parts.append(_pb_len(20, req))
    parts.append(_pb_len(26, trans))
    parts.append(_pb_len(35, step_hash))
    return b"".join(parts)


def _antigravity_fill_step_timestamps(db_path: Path, env: dict) -> int:
    """smigrate 的 antigravity 产物 steps.metadata 为空且 step_payload 缺少 Field 5 (metadata) —
    实机验证 language_server 重建摘要时直接反序列化 step_payload，缺失 Field 5 与 f6/f20/f26
    会导致 last_modified_time 与 last_user_input_time 退回 0001-01-01 ('2027y')。
    同步写入 steps.metadata 列与 step_payload Field 5。返回修复行数。"""
    import sqlite3
    rng = _session_epoch_range(env)
    now_ep = int(time.time())
    start, end = rng if rng else (now_ep, now_ep)
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(steps)")}
        if not cols:
            conn.close()
            return 0
        meta_row = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trajectory_meta'").fetchone():
            meta_row = conn.execute(
                "SELECT trajectory_id, cascade_id FROM trajectory_meta ORDER BY rowid LIMIT 1"
            ).fetchone()
        traj_id = (meta_row[0] or "") if meta_row else ""
        casc_id = (meta_row[1] or "") if meta_row else ""
        has_payload = "step_payload" in cols
        has_type = "step_type" in cols
        has_status = "status" in cols
        sel_cols = ["idx"]
        if has_type:
            sel_cols.append("step_type")
        if has_status:
            sel_cols.append("status")
        if has_payload:
            sel_cols.append("step_payload")
        rows = conn.execute(f"SELECT {', '.join(sel_cols)} FROM steps ORDER BY CAST(idx AS INTEGER)").fetchall()
        n = len(rows)
        if not n:
            conn.close()
            return 0
        turn_id = 0
        updates = []
        for k, row in enumerate(rows):
            pos = 1
            idx = row[0]
            step_type = row[pos] if has_type else 15
            pos += int(has_type)
            status = row[pos] if has_status else 3
            pos += int(has_status)
            payload = row[pos] if has_payload else None
            if step_type == 14:
                turn_id += 1
            sec = start + (end - start) * k // max(n - 1, 1)
            meta_bytes = _antigravity_build_step_metadata(
                sec, int(step_type or 0), int(status or 0), int(idx or 0),
                traj_id, casc_id, turn_id, is_last=(k == n - 1),
            )
            if has_payload and payload is not None:
                p_fields, p_tail = _pb_decode(bytes(payload))
                p_fields[(5, 2)] = [meta_bytes]
                updates.append((meta_bytes, _pb_encode(p_fields, p_tail), idx))
            else:
                updates.append((meta_bytes, idx))
        if has_payload and len(updates[0]) == 3:
            conn.executemany("UPDATE steps SET metadata=?, step_payload=? WHERE idx=?", updates)
        else:
            conn.executemany("UPDATE steps SET metadata=? WHERE idx=?", [(u[0], u[-1]) for u in updates])
        conn.commit()
        conn.close()
        return n
    except sqlite3.Error:
        return 0


def _antigravity_normalize_steps(db_path: Path) -> int:
    """smigrate 的 planner 文本写在 wrapper f1, 而 app 原生形状是 f3 (纯文本字符串) + f12 状态
    + f11; 形状不符时 Antigravity 会话窗口渲染崩溃 (Cannot read properties of undefined
    (reading 'end'), 2026-11-16 用户实测; 参照被 app 自己重写过的导入库 a1933337 逆得)。
    - type15 wrapper: f1 文本 → f3; 补 f12=2 (状态) 与 f11 {0,0}
    - type15 metadata: f6 同值补 f7/f8 (完成时间戳); 全部 Timestamp 子消息补 nanos=0
    返回改写行数。"""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=15)
    n = 0
    try:
        rows = conn.execute("SELECT idx, metadata, step_payload FROM steps"
                            " WHERE step_type = 15 ORDER BY idx").fetchall()
        for idx, md, sp in rows:
            if not sp:
                continue
            outer, tail = _pb_decode(bytes(sp))
            changed = False
            if (20, 2) in outer:
                wfields, wtail = _pb_decode(bytes(outer[(20, 2)][0]))
                if (1, 2) in wfields and (3, 2) not in wfields:
                    wfields[(3, 2)] = [bytes(wfields[(1, 2)][0])]
                    del wfields[(1, 2)]
                    wfields[(11, 2)] = [_pb_var(1, 0) + _pb_var(2, 0)]
                    wfields[(12, 0)] = [2]
                    outer[(20, 2)] = [_pb_encode(wfields, wtail)]
                    changed = True
            new_md = md
            if md:
                mfields, mtail = _pb_decode(bytes(md))
                mchanged = False
                for key in ((1, 2), (6, 2)):
                    if key in mfields:
                        sub, st = _pb_decode(bytes(mfields[key][0]))
                        if (1, 0) in sub and (2, 0) not in sub:
                            sub[(2, 0)] = [0]
                            mfields[key] = [_pb_encode(sub, st)]
                            mchanged = True
                if (6, 2) in mfields:
                    ts = bytes(mfields[(6, 2)][0])
                    if (7, 2) not in mfields:
                        mfields[(7, 2)] = [ts]
                        mchanged = True
                    if (8, 2) not in mfields:
                        mfields[(8, 2)] = [ts]
                        mchanged = True
                if mchanged:
                    new_md = _pb_encode(mfields, mtail)
                if new_md and ((5, 2) not in outer or outer[(5, 2)][0] != new_md):
                    outer[(5, 2)] = [new_md]
                    changed = True
            if changed or new_md != md:
                conn.execute("UPDATE steps SET metadata=?, step_payload=? WHERE idx=?",
                             (new_md, _pb_encode(outer, tail), idx))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _antigravity_read_cascade_id(db_path: Path) -> str:
    """从导出库读取 cascade_id (优先 trajectory_meta, 回退 blob field 6)。"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trajectory_meta'").fetchone():
            r = conn.execute("SELECT cascade_id FROM trajectory_meta ORDER BY rowid LIMIT 1").fetchone()
            if r and r[0]:
                conn.close()
                return str(r[0])
        row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
        conn.close()
        if row and row[0]:
            fields, _ = _pb_decode(bytes(row[0]))
            vals = fields.get((6, 2))
            if vals:
                return vals[0].decode("ascii", "ignore")
    except sqlite3.Error:
        pass
    return ""


def _antigravity_resolve_project_id(ws_folder: str, enc_uri: str, create: bool = True) -> str:
    """从 ~/.gemini/config/projects/*.json 匹配项目 UUID; 未登记且配置目录存在时仅在 create=True 时自动注册。"""
    cfg_dir = HOME / ".gemini" / "config"
    if not cfg_dir.exists():
        return ""
    proj_dir = cfg_dir / "projects"
    proj_name = ws_folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or ws_folder
    target_uri = enc_uri.rstrip("/").lower()
    fallback_id = ""
    if proj_dir.exists():
        for jf in sorted(proj_dir.glob("*.json")):
            try:
                obj = json.loads(jf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(obj, dict) or not obj.get("id"):
                continue
            for res in (obj.get("projectResources") or {}).get("resources") or []:
                if not isinstance(res, dict):
                    continue
                uri = res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri") or ""
                if uri and uri.rstrip("/").lower() == target_uri:
                    return str(obj["id"])
            if not fallback_id and obj.get("name") == proj_name:
                fallback_id = str(obj["id"])
    if fallback_id:
        return fallback_id
    if not create:
        return ""
    proj_dir.mkdir(parents=True, exist_ok=True)
    pid = str(uuid.uuid4())
    is_git = (Path(ws_folder) / ".git").exists()
    res_entry = {"gitFolder": {"folderUri": enc_uri}} if is_git else {"folderUri": enc_uri}
    payload = {
        "id": pid,
        "name": proj_name,
        "projectResources": {"resources": [res_entry]},
        "permissionGrants": {"v2Migrated": True},
        "settings": {},
        "isWorkspaceOnly": False,
    }
    atomic_write_text(proj_dir / f"{pid}.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return pid


def _antigravity_set_workspace(db_path: Path, ws_folder: str, last_epoch: int, cascade: str = "",
                               create_project: bool = True) -> bool:
    """标准化 trajectory_meta (source=1) 并将源项目目录、f3 UUID 与 project_id 写入
    trajectory_metadata_blob.main — 对齐原生结构，避免启动重整时清空元数据并归回底部未分组。"""
    import sqlite3
    from urllib.parse import quote as _q
    conn = sqlite3.connect(db_path, timeout=15)
    has_tm = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trajectory_meta'").fetchone())
    if has_tm:
        conn.execute("UPDATE trajectory_meta SET source=1 WHERE trajectory_type=4 AND source=17")
        if not cascade:
            r = conn.execute("SELECT cascade_id FROM trajectory_meta ORDER BY rowid LIMIT 1").fetchone()
            if r and r[0]:
                cascade = str(r[0])
    row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    data = bytes(row[0]) if row and row[0] else b""
    fields, tail = _pb_decode(data)
    eff_cascade = cascade or (fields.get((6, 2), [b""])[0].decode("ascii", "ignore"))
    if cascade:
        fields[(6, 2)] = [cascade.encode("ascii")]
    fields[(2, 2)] = [_pb_var(1, max(1, int(last_epoch)))]
    if eff_cascade and ((3, 2) not in fields or fields[(3, 2)][0] == eff_cascade.encode("ascii")):
        fields[(3, 2)] = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{eff_cascade}:meta")).encode("ascii")]
    elif (3, 2) not in fields:
        fields[(3, 2)] = [str(uuid.uuid4()).encode("ascii")]
    if ws_folder:
        posix = ws_folder.replace("\\", "/")
        if len(posix) >= 2 and posix[1] == ":":
            posix = posix[0].lower() + posix[1:]  # 原生形态: 盘符小写 (file:///d:/GBM-DRACI)
        ws_raw = "file:///" + posix
        enc = "file:///" + _q(posix, safe="/")  # file:///d%3A/GBM-DRACI
        fields[(1, 2)] = [b"\n" + _varint(len(ws_raw.encode("utf-8"))) + ws_raw.encode("utf-8") + b"\x1a\x00"]
        fields[(7, 2)] = [enc.encode("utf-8")]
        proj_id = _antigravity_resolve_project_id(ws_folder, enc, create=create_project)
        fields[(18, 2)] = [(proj_id or "outside-of-project").encode("ascii")]
    else:
        fields[(18, 2)] = [b"outside-of-project"]
    out = _pb_encode(fields, tail)
    conn.execute("INSERT OR REPLACE INTO trajectory_metadata_blob(id, data) VALUES('main', ?)", (out,))
    # 原生库 PRAGMA user_version=1; smigrate 默认写出 0，启动重整会误判未初始化并清空 main blob
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return True


# ------------------------------------------------- antigravity 索引行登记 (导入可见性)

def _antigravity_ws_uris(cwd: str) -> tuple[str, str]:
    """与 _antigravity_set_workspace 同规则: 盘符小写原始形态 + %3A 编码形态。"""
    from urllib.parse import quote as _q
    posix = (cwd or "").replace("\\", "/").rstrip("/")
    if not posix:
        return "", ""
    if len(posix) >= 2 and posix[1] == ":":
        posix = posix[0].lower() + posix[1:]
    return "file:///" + posix, "file:///" + _q(posix, safe="/")


def _antigravity_first_user_text(env: dict) -> str:
    for b in env.get("conversation") or []:
        if (b.get("role") or "").lower() == "user":
            text = (b.get("content") or "").strip()
            if text:
                return " ".join(text.split())
    return ""


def _antigravity_raw_summary(title: str, step_count: int, epoch_mod: int, epoch_in: int,
                             traj_id: str, ws_raw: str, enc: str, cascade: str,
                             project_id: str, f17_uuid: str) -> bytes:
    """conversation_summaries.raw_summary protobuf — 结构从原生+已验证导入行逆向 (a1933337 样本):
    f1 title / f2 step_count / f3,f7,f10 Timestamp / f4 trajectory_id / f5=1 / f9 Workspace /
    f15{title,ts} / f16=1 / f17{Workspace,ts,uuid,cascade,enc_uri,project_id} / f22=4 / f28=4。"""
    t = title.encode("utf-8")
    ts_mod, ts_in = _ts_msg(epoch_mod), _ts_msg(epoch_in)
    ws = _pb_len(1, ws_raw.encode("utf-8")) + _pb_len(3, b"")
    f15 = _pb_len(1, t) + _pb_len(7, ts_mod)
    f17 = (_pb_len(1, ws) + _pb_len(2, ts_in) + _pb_len(3, f17_uuid.encode("ascii"))
           + _pb_len(6, cascade.encode("ascii")) + _pb_len(7, enc.encode("utf-8"))
           + _pb_len(18, (project_id or "outside-of-project").encode("ascii")))
    return (_pb_len(1, t) + _pb_var(2, max(1, step_count)) + _pb_len(3, ts_mod)
            + _pb_len(4, traj_id.encode("ascii")) + _pb_var(5, 1) + _pb_len(7, ts_in)
            + _pb_len(9, ws) + _pb_len(10, ts_in) + _pb_len(15, f15) + _pb_var(16, 1)
            + _pb_len(17, f17) + _pb_var(22, 4) + _pb_var(28, 4))


def _ts_msg(epoch: int, nanos: int = 0) -> bytes:
    return _pb_var(1, max(1, int(epoch))) + _pb_var(2, nanos)


def _antigravity_register_summaries(target_db: Path, env: dict, cascade: str) -> bool:
    """写 ~/.gemini/antigravity/conversation_summaries 索引行 (标题/预览/时间戳/项目归属)。
    会话列表只读这张索引表 — 只落 .db 不建行 = 永不可见。app 运行中可直接写 (WAL 短事务);
    结构对齐已验证导入 (2026-11-16 批量 132 条全活)。"""
    import sqlite3 as _s
    summary_db = HOME / ".gemini" / "antigravity" / "conversation_summaries.db"
    if not summary_db.exists():
        return False  # app 未装此 HOME: 不代建索引库 (真库 schema 属于 Antigravity)
    cwd = (env.get("capture") or {}).get("cwd") or ""
    ws_raw, enc = _antigravity_ws_uris(cwd)
    rng = _session_epoch_range(env)
    epoch_mod = rng[1] if rng else int(time.time())
    epoch_in = rng[0] if rng else epoch_mod
    title = (env.get("capture") or {}).get("originator") \
        or _antigravity_first_user_text(env) or "(无标题)"
    title = title.strip()[:60]
    preview = _antigravity_first_user_text(env)[:200] or title
    conn = _s.connect(target_db, timeout=15)
    try:
        step_count = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
        input_idx = int(conn.execute(
            "SELECT COALESCE(MAX(idx),0) FROM steps WHERE step_type=14").fetchone()[0] or 0)
        traj = conn.execute(
            "SELECT trajectory_id FROM trajectory_meta ORDER BY rowid LIMIT 1").fetchone()
        blob = conn.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    finally:
        conn.close()
    fields, _ = _pb_decode(bytes(blob[0])) if blob and blob[0] else ({}, b"")
    pid_vals = fields.get((18, 2))
    project_id = pid_vals[0].decode("ascii", "ignore") if pid_vals else "outside-of-project"
    f17u = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{cascade}:agy-summary"))
    raw = _antigravity_raw_summary(title, step_count, epoch_mod, epoch_in,
                                   traj[0] if traj else cascade, ws_raw, enc, cascade,
                                   project_id, f17u)
    def _iso(epoch: int) -> str:
        return dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S+00:00")
    sconn = _s.connect(summary_db, timeout=30)
    try:
        sconn.execute("PRAGMA busy_timeout=30000")
        cols = {r[1] for r in sconn.execute("PRAGMA table_info(conversation_summaries)")}
        if "title" not in cols:
            return False  # 非预期 schema (空库/老版本): 不写脏行, 诚实放弃
        sconn.execute(
            "INSERT OR REPLACE INTO conversation_summaries("
            "conversation_id, title, preview, step_count, last_modified_time, workspace_uris,"
            " status, source, project_id, agent_name, parent_conversation_id, nesting_depth,"
            " battle_id, winning_conversation_id, not_fully_idle, killed,"
            " last_user_input_time, last_user_input_step_index, app_data_dir,"
            " raw_summary, group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cascade, title, preview, step_count, _iso(epoch_mod),
             json.dumps([enc]), "CASCADE_RUN_STATUS_IDLE", "", project_id, "", "", 0,
             "", "", 0, 0, _iso(epoch_in), input_idx, "antigravity", raw, ""))
        sconn.commit()
    finally:
        sconn.close()
    _antigravity_proto_upsert(cascade, raw)
    return True


def _antigravity_proto_upsert(cascade: str, raw: bytes) -> bool:
    """agyhub_summaries_proto.pb 是 app 的真源缓存: 启动时按它重建 conversation_summaries 表,
    表里有而 proto 里没有的行一律清除 (2026-11-16 实证: 重启后 158→51)。条目结构
    (原生样本逆向): top f1 repeated = {f1: cascade, f2: raw_summary 字节 (与表同源)}。
    幂等 upsert: 同 cid 先摘除再追加。app 运行中写入有被关停时回写覆盖的风险 —
    批量恢复请在完全退出 Antigravity 后执行。"""
    pb = HOME / ".gemini" / "antigravity" / "agyhub_summaries_proto.pb"
    if not pb.parent.exists():
        return False
    top_fields: dict = {}
    if pb.exists() and pb.stat().st_size:
        try:
            top_fields, _ = _pb_decode(pb.read_bytes())
        except Exception:
            top_fields = {}  # 坏文件从空重建 (app 侧会重新吸收 .db)
    entries = [e for e in top_fields.get((1, 2), [])
               if _pb_decode(e)[0].get((1, 2), [b""])[0]
               .decode("ascii", "ignore").lower() != cascade.lower()]
    entries.append(_pb_len(1, cascade.encode("ascii")) + _pb_len(2, raw))
    out = b"".join(_pb_len(1, e) for e in entries)
    tmp = pb.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_bytes(out)
    os.replace(tmp, pb)
    return True


def _load_snap_cwd_cached(vault: Path, sid: str, cache: dict[str, str],
                          path_hint: str | None = None) -> str:
    if sid in cache:
        return cache[sid]
    cwd = ""
    cands = ([Path(path_hint)] if path_hint else []) + [vault / "snapshots" / f"{sid}.json"]
    for cand in cands:
        if cand.exists():
            try:
                env = json.loads(cand.read_text(encoding="utf-8"))
                cwd = (env.get("capture") or {}).get("cwd") or ""
                break
            except (ValueError, OSError):
                pass
    cache[sid] = cwd
    return cwd


def _collect_antigravity_migrated_dbs(vault: Path) -> dict[str, dict]:
    """汇总由 Context Vault 迁移到 Antigravity 的 <cascade_id>.db 与源快照映射。"""
    conn = db(vault)
    try:
        try:
            rows = conn.execute("SELECT id, agent, session_ref, originator, path FROM snapshots").fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        conn.close()
    snap_by_id = {r["id"]: dict(r) for r in rows}
    mapped: dict[str, dict] = {}

    outbox = vault / "outbox"
    if outbox.exists():
        for mf in sorted(outbox.glob("*.session-migrate.json")):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            tgt = meta.get("target") or {}
            cid = str(tgt.get("session_id") or "").strip()
            if tgt.get("format") != "antigravity" or not cid:
                continue
            parts = mf.name.split("-")
            pref = parts[2] if len(parts) >= 3 and parts[2].startswith("cv_") else ""
            if not pref:
                continue
            hits = [s for sid, s in snap_by_id.items() if sid.startswith(pref)]
            uniq_refs = {(s["agent"], s["session_ref"]) for s in hits}
            if len(hits) == 1 or len(uniq_refs) == 1:
                s = hits[0]
                mapped[cid] = {
                    "cid": cid,
                    "sid": s["id"],
                    "agent": s["agent"] or "",
                    "session_ref": s["session_ref"] or "",
                    "title": s["originator"] or "",
                    "cwd": str(tgt.get("cwd") or ""),
                    "path_hint": s["path"],
                    "in_migrations": False,
                    "mig_seq": -1,
                }

    mig_file = vault / "migrations.jsonl"
    if mig_file.exists():
        for seq, line in enumerate(mig_file.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            dest = str(m.get("dest") or "")
            norm_dest = dest.replace("\\", "/").lower()
            if "/antigravity/conversations/" not in norm_dest or not norm_dest.endswith(".db"):
                continue
            cid = Path(dest).stem
            sid = str(m.get("source") or "")
            s = snap_by_id.get(sid)
            if cid and s:
                prev = mapped.get(cid) or {}
                mapped[cid] = {
                    "cid": cid,
                    "sid": sid,
                    "agent": s["agent"] or "",
                    "session_ref": s["session_ref"] or "",
                    "title": s["originator"] or "",
                    "cwd": prev.get("cwd") or "",
                    "path_hint": s["path"],
                    "in_migrations": True,
                    "mig_seq": seq,
                }
    return mapped


def _antigravity_proto_prune(proto_path: Path, cids: set[str],
                             delete: bool = False) -> tuple[set[str], str | None]:
    """查询或清理 agyhub_summaries_proto.pb 中指定会话的摘要缓存（逐条目保留原 wire bytes）。"""
    if not proto_path.exists() or not cids:
        return set(), None
    norm_cids = {c.lower() for c in cids}
    try:
        raw = proto_path.read_bytes()
    except OSError as e:
        return set(), str(e)
    fields, tail = _pb_decode(raw)
    entries = fields.get((1, 2), [])
    hits: set[str] = set()
    kept = []
    for entry in entries:
        sub, _ = _pb_decode(entry)
        cid_b = sub.get((1, 2), [b""])[0]
        cid = cid_b.decode("ascii", "ignore")
        if cid.lower() in norm_cids:
            hits.add(cid.lower())
        else:
            kept.append(entry)
    if delete and hits:
        if kept:
            fields[(1, 2)] = kept
        else:
            fields.pop((1, 2), None)
        out = _pb_encode(fields, tail)
        tmp = proto_path.with_name(f".{proto_path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_bytes(out)
            os.replace(tmp, proto_path)
        except OSError as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return set(), str(e)
    return hits, None


def _antigravity_db_score(dbp: Path, entry: dict) -> tuple[int, int, int, float]:
    steps = -1
    mtime = 0.0
    if dbp.exists():
        try:
            mtime = dbp.stat().st_mtime
            conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
            try:
                steps = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            pass
    return (steps, 1 if entry.get("in_migrations") else 0, int(entry.get("mig_seq", -1)), mtime)


def _antigravity_summary_ops(summary_db: Path, cids: list[str],
                             delete: bool = False) -> tuple[set[str], str | None]:
    """查询或删除 conversation_summaries.db 中指定会话的摘要与 FTS 缓存行。"""
    if not summary_db.exists() or not cids:
        return set(), None
    try:
        conn = sqlite3.connect(
            f"file:{summary_db}?mode=ro" if not delete else str(summary_db),
            uri=not delete,
            timeout=1.5,
        )
    except sqlite3.Error as e:
        return set(), str(e)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        targets = [t for t in ("conversation_summaries", "conversations_fts") if t in tables]
        if not targets:
            return set(), None
        hits: set[str] = set()
        for i in range(0, len(cids), 200):
            chunk = cids[i:i + 200]
            ph = ",".join("?" * len(chunk))
            for tbl in targets:
                hits.update(r[0] for r in conn.execute(
                    f"SELECT DISTINCT conversation_id FROM {tbl} WHERE conversation_id IN ({ph})", chunk))
            if delete:
                for tbl in targets:
                    conn.execute(f"DELETE FROM {tbl} WHERE conversation_id IN ({ph})", chunk)
        if delete:
            conn.commit()
        return hits, None
    except sqlite3.Error as e:
        if delete:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return set(), str(e)
    finally:
        conn.close()


def _uri_to_norm_path(uri: str) -> str:
    """将 Antigravity file:// URI 转成与 cwd 一致的小写正斜杠路径。"""
    from urllib.parse import unquote as _unq
    u = (uri or "").strip()
    if u.lower().startswith("file://"):
        p = _unq(u[7:])
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return _norm_win_path(p)
    return _norm_win_path(u)


def _antigravity_find_empty_projects(
    vault: Path,
    conv_dir: Path,
    summary_db: Path,
    mapped: dict,
    cwd_cache: dict[str, str],
    removed_cids: set[str],
    project: str | None = None,
) -> tuple[list[dict], str | None]:
    """识别已无任何剩余会话引用、且曾有迁移来源的空壳 Antigravity 项目注册文件（~/.gemini/config/projects/*.json）。"""
    proj_dir = HOME / ".gemini" / "config" / "projects"
    if not proj_dir.exists():
        return [], None

    migrated_cwds = {
        _norm_win_path(
            e["cwd"] or _load_snap_cwd_cached(vault, e["sid"], cwd_cache, e.get("path_hint"))
        )
        for e in mapped.values()
    }
    migrated_cwds.discard("")
    if not migrated_cwds:
        return [], None

    used_pids: set[str] = set()
    used_paths: set[str] = set()
    if conv_dir.exists():
        for dbp in conv_dir.glob("*.db"):
            if dbp.stem.lower() in removed_cids:
                continue
            try:
                dc = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=2)
                try:
                    row = dc.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
                    if row and row[0]:
                        fields, _ = _pb_decode(bytes(row[0]))
                        for raw_pid in fields.get((18, 2), []):
                            pid = raw_pid.decode("utf-8", "ignore").strip().lower()
                            if pid and pid != "outside-of-project":
                                used_pids.add(pid)
                        for raw_ws in fields.get((3, 2), []):
                            wf, _ = _pb_decode(raw_ws)
                            for raw_u in wf.get((1, 2), []):
                                np = _uri_to_norm_path(raw_u.decode("utf-8", "ignore"))
                                if np:
                                    used_paths.add(np)
                        for raw_u in fields.get((7, 2), []):
                            np = _uri_to_norm_path(raw_u.decode("utf-8", "ignore"))
                            if np:
                                used_paths.add(np)
                finally:
                    dc.close()
            except sqlite3.Error:
                pass

    if summary_db.exists():
        try:
            sc = sqlite3.connect(f"file:{summary_db}?mode=ro", uri=True, timeout=2)
            try:
                cols = {r[1] for r in sc.execute("PRAGMA table_info(conversation_summaries)")}
                if "conversation_id" in cols:
                    sel = [c for c in ("conversation_id", "project_id", "workspace_uris") if c in cols]
                    for r in sc.execute(f"SELECT {', '.join(sel)} FROM conversation_summaries"):
                        rd = dict(zip(sel, r))
                        if str(rd.get("conversation_id") or "").lower() in removed_cids:
                            continue
                        pid = str(rd.get("project_id") or "").strip().lower()
                        if pid:
                            used_pids.add(pid)
                        raw_uris = rd.get("workspace_uris") or ""
                        if raw_uris:
                            try:
                                uri_list = json.loads(raw_uris) if isinstance(raw_uris, str) else raw_uris
                            except ValueError:
                                uri_list = []
                            for u in uri_list if isinstance(uri_list, list) else []:
                                np = _uri_to_norm_path(str(u))
                                if np:
                                    used_paths.add(np)
            finally:
                sc.close()
        except sqlite3.Error as e:
            return [], str(e)

    empty: list[dict] = []
    for jf in sorted(proj_dir.glob("*.json")):
        try:
            obj = json.loads(jf.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(obj, dict) or not obj.get("id") or obj.get("sidecar"):
            continue
        pid = str(obj["id"])
        pname = str(obj.get("name") or "")
        uris = [
            str(res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri") or "")
            for res in (obj.get("projectResources") or {}).get("resources") or []
            if isinstance(res, dict)
        ]
        proj_paths = [np for u in uris if (np := _uri_to_norm_path(u))]
        if pid.lower() in used_pids or any(pp in used_paths for pp in proj_paths):
            continue
        # 只清曾有 Vault 迁移记录覆盖的项目，避免误删用户在 Antigravity 里单独打开的空工作区
        if not any(pp in migrated_cwds for pp in proj_paths):
            continue
        cwd_repr = proj_paths[0] if proj_paths else pname
        if project and not _norm_project_match(cwd_repr, project):
            continue
        empty.append({"id": pid, "name": pname, "path": str(jf), "cwd": cwd_repr})
    if project and empty:
        empty = _prefer_exact_project(empty, project)
    return empty, None


def sync_antigravity_archived(vault: Path, project: str | None = None,
                              apply: bool = False, dedup: bool = False,
                              only_agent: str | None = None) -> dict:
    """移除已导入 Antigravity 的 ZCode 已归档/已删除会话（含 .db、摘要索引与 agyhub_summaries_proto.pb），
    并清理已无任何剩余会话的迁移空项目配置；可选 --dedup 去重、--only-agent 清理非目标来源。"""
    arch_refs = _zcode_archived_refs()
    mapped = _collect_antigravity_migrated_dbs(vault)
    keep_agents = set(_parse_agent_filter(only_agent))
    conv_dir = HOME / ".gemini" / "antigravity" / "conversations"
    agy_dir = conv_dir.parent
    summary_db = agy_dir / "conversation_summaries.db"
    proto_pb = agy_dir / "agyhub_summaries_proto.pb"

    cwd_cache: dict[str, str] = {}
    candidates = []
    by_conv: dict[tuple[str, str], list[dict]] = {}
    for entry in mapped.values():
        if entry["agent"] == "zcode" and entry["session_ref"] in arch_refs:
            candidates.append({**entry, "reason": "archived"})
        elif keep_agents and (entry["agent"] or "").lower() not in keep_agents:
            candidates.append({**entry, "reason": f"agent:{entry['agent'] or 'unknown'}"})
        elif dedup and (entry["agent"] or "").lower() == "antigravity":
            # 原生 Antigravity 会话经 cv sync 回灌后再被批导重装产生的自回环副本
            candidates.append({**entry, "reason": "self_loop"})
        elif dedup:
            gkey = (entry["agent"] or "", entry["session_ref"] or "") if entry["session_ref"] else ("sid", entry["sid"])
            by_conv.setdefault(gkey, []).append(entry)

    if dedup:
        for gkey, group in by_conv.items():
            existing = [e for e in group if (conv_dir / f"{e['cid']}.db").exists()]
            if len(existing) <= 1:
                continue
            det_cid = ""
            if gkey[0] and gkey[1] and gkey[0] != "sid":
                det_cid = _deterministic_session_id({"capture": {"agent": gkey[0], "session_ref": gkey[1]}}).lower()
            existing.sort(
                key=lambda e: (1 if (det_cid and e["cid"].lower() == det_cid) else 0,)
                + _antigravity_db_score(conv_dir / f"{e['cid']}.db", e),
                reverse=True,
            )
            for dup in existing[1:]:
                candidates.append({**dup, "reason": "dup"})

    filtered = []
    for entry in candidates:
        if not entry["cwd"]:
            entry["cwd"] = _load_snap_cwd_cached(vault, entry["sid"], cwd_cache, entry.get("path_hint"))
        if project and not _norm_project_match(entry["cwd"], project):
            continue
        filtered.append(entry)
    if project and filtered:
        filtered = _prefer_exact_project(filtered, project)

    all_cids = [c["cid"] for c in filtered]
    sum_hits_raw, sum_err = _antigravity_summary_ops(summary_db, all_cids, delete=False)
    sum_hits = {c.lower() for c in sum_hits_raw}
    pb_hits, pb_err = _antigravity_proto_prune(proto_pb, set(all_cids), delete=False)

    targets = []
    for c in filtered:
        dbp = conv_dir / f"{c['cid']}.db"
        has_db = dbp.exists()
        has_sum = c["cid"].lower() in sum_hits
        has_pb = c["cid"].lower() in pb_hits
        if has_db or has_sum or has_pb:
            targets.append({**c, "db_path": str(dbp), "has_db": has_db,
                            "has_summary": has_sum, "has_proto": has_pb})

    removed_cids = {t["cid"].lower() for t in targets}
    empty_projects, proj_err = _antigravity_find_empty_projects(
        vault, conv_dir, summary_db, mapped, cwd_cache, removed_cids, project=project
    )

    removed_dbs = 0
    removed_projects = 0
    cleaned_summaries: set[str] = set()
    cleaned_proto: set[str] = set()
    if apply and (targets or empty_projects):
        if targets:
            target_cids = [t["cid"] for t in targets]
            cleaned_summaries, del_err = _antigravity_summary_ops(summary_db, target_cids, delete=True)
            if del_err:
                raise BadMigrateTarget(
                    f"无法清理 Antigravity 摘要库 ({del_err})；请先完全关闭 Antigravity 后重试 --apply"
                )
            cleaned_proto, pdel_err = _antigravity_proto_prune(proto_pb, set(target_cids), delete=True)
            if pdel_err:
                raise BadMigrateTarget(
                    f"无法清理 Antigravity agyhub_summaries_proto.pb ({pdel_err})；请先完全关闭 Antigravity 后重试 --apply"
                )
            for t in targets:
                base = Path(t["db_path"])
                for p in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
                    if p.exists():
                        p.unlink()
                        if p == base:
                            removed_dbs += 1
        elif proj_err:
            raise BadMigrateTarget(
                f"无法读取 Antigravity 摘要库 ({proj_err})；请先完全关闭 Antigravity 后重试 --apply"
            )
        elif summary_db.exists():
            _probe_antigravity_summary_writable(summary_db, "清理 Antigravity 空项目配置")
        for ep in empty_projects:
            jp = Path(ep["path"])
            if jp.exists():
                jp.unlink()
                removed_projects += 1

    return {
        "applied": apply,
        "targets": targets,
        "empty_projects": empty_projects,
        "db_files": sum(1 for t in targets if t["has_db"]),
        "summary_ids": sum(1 for t in targets if t["has_summary"]),
        "proto_ids": sum(1 for t in targets if t["has_proto"]),
        "dup_count": sum(1 for t in targets if t["reason"] == "dup"),
        "agent_count": sum(1 for t in targets if str(t["reason"]).startswith("agent:")),
        "removed_dbs": removed_dbs,
        "removed_projects": removed_projects,
        "cleaned_summaries": len(cleaned_summaries),
        "cleaned_proto": len(cleaned_proto),
        "summary_check_error": sum_err or pb_err or proj_err,
    }


def _probe_antigravity_summary_writable(summary_db: Path, action: str = "操作") -> None:
    """校验 Antigravity 摘要库未被客户端锁定；占用时抛 BadMigrateTarget。"""
    if not summary_db.exists():
        return
    try:
        pconn = sqlite3.connect(str(summary_db), timeout=1.5)
        try:
            pconn.execute("BEGIN IMMEDIATE")
            pconn.rollback()
        finally:
            pconn.close()
    except sqlite3.Error as le:
        raise BadMigrateTarget(
            f"无法{action} ({le})；请先完全关闭 Antigravity 后重试 --apply"
        )


def _antigravity_eligible_cwd(cwd: str) -> bool:
    if not cwd:
        return False
    n = cwd.replace("\\", "/").lower()
    return not any(m in n for m in (".zcode/workspace", "appdata/local/temp", "/tmp/", "\\tmp\\", "cvtest"))


def _pb_collect_strings(data: bytes, depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 6 or not data:
        return out
    fields, tail = _pb_decode(data)
    if tail:
        return out
    for (_, wt), vals in fields.items():
        if wt != 2:
            continue
        for raw in vals:
            try:
                s = raw.decode("utf-8")
                if s and sum(32 <= ord(c) < 127 or ord(c) > 0x2E7F for c in s) > len(s) * 0.9:
                    out.append(s)
                else:
                    out += _pb_collect_strings(raw, depth + 1)
            except UnicodeDecodeError:
                out += _pb_collect_strings(raw, depth + 1)
    return out


def _antigravity_db_fingerprint(db_path: Path) -> str:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        strs = _pb_collect_strings(bytes(row[0]))
        return " ".join(max(strs, key=len).split()).lower()[:80] if strs else ""
    except sqlite3.Error:
        return ""


def sync_to_antigravity_batch(vault: Path, agent: str = "zcode", project: str | None = None,
                              include_archived: bool = False, limit: int = 0,
                              apply: bool = False) -> dict:
    """差量同步活跃顶层主对话 → Antigravity（Web 控制台与批量同步共用，默认仅 preview）。"""
    conv_dir = HOME / ".gemini" / "antigravity" / "conversations"
    summary_db = conv_dir.parent / "conversation_summaries.db"
    eff_agent = None if str(agent or "").strip().lower() in ("", "all") else agent
    allowed_agents = set(_parse_agent_filter(eff_agent))
    arch_refs = set() if include_archived else _zcode_archived_refs()
    sub_refs = _zcode_subagent_refs()

    if project:
        items = _resolve_project_items(vault, project, limit=limit,
                                       include_archived=include_archived, agent=eff_agent)
    else:
        items = resolve_top_conversations(vault, agent=eff_agent, include_archived=include_archived, limit=limit)
    items = [it for it in items if _antigravity_eligible_cwd(it.get("cwd") or "")
             and (it.get("agent") or "").lower() != "antigravity"]

    have_rows: set[str] = set()
    summary_error = None
    if summary_db.exists():
        try:
            sc = sqlite3.connect(f"file:{summary_db}?mode=ro", uri=True, timeout=5)
            try:
                have_rows = {str(r[0]).lower() for r in sc.execute("SELECT conversation_id FROM conversation_summaries") if r[0]}
            finally:
                sc.close()
        except sqlite3.Error as e:
            summary_error = str(e)

    existing_fps: set[str] = set()
    if conv_dir.exists() and have_rows:
        for dbp in conv_dir.glob("*.db"):
            if dbp.stem.lower() in have_rows:
                fp = _antigravity_db_fingerprint(dbp)
                if fp:
                    existing_fps.add(fp)

    existing_refs: set[tuple[str, str]] = set()
    for cid, ent in _collect_antigravity_migrated_dbs(vault).items():
        if ent["session_ref"] and (conv_dir / f"{cid}.db").exists() and cid.lower() in have_rows:
            existing_refs.add(((ent["agent"] or "").lower(), ent["session_ref"]))

    plan = []
    seen_cascades: set[str] = set()
    skipped_done = 0
    skipped_dup = 0
    for it in items:
        sid = it["id"]
        try:
            env = load_snapshot(vault, sid)
        except (ValueError, OSError, SnapshotNotFound):
            continue
        cap = env.get("capture") or {}
        ag = (cap.get("agent") or "").lower()
        sref = cap.get("session_ref") or ""
        orig = str(cap.get("originator") or "")
        if ag == "antigravity" or (allowed_agents and ag not in allowed_agents):
            continue
        if ag == "zcode" and sref in (arch_refs | sub_refs):
            continue
        if cap.get("parent_session_ref") or orig.startswith("workflow subagent"):
            continue
        cascade = _deterministic_session_id(env).lower()
        if ((conv_dir / f"{cascade}.db").exists() and cascade in have_rows) \
                or (sref and (ag, sref) in existing_refs):
            skipped_done += 1
            continue
        fp = _antigravity_first_user_text(env).lower()[:80]
        if fp and fp in existing_fps:
            skipped_dup += 1
            continue
        if cascade in seen_cascades:
            continue
        seen_cascades.add(cascade)
        plan.append({
            "sid": sid,
            "agent": ag,
            "cwd": cap.get("cwd") or "",
            "title": orig or fp or sid,
            "cascade": cascade,
        })

    by_proj: dict[str, int] = {}
    for p in plan:
        key = _norm_win_path(p["cwd"]) or "(无项目)"
        by_proj[key] = by_proj.get(key, 0) + 1
    proj_summary = [{"cwd": k, "count": n} for k, n in sorted(by_proj.items(), key=lambda x: (-x[1], x[0]))]

    imported = 0
    errors: list[dict] = []
    if apply and plan:
        if not find_smigrate():
            raise BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        _probe_antigravity_summary_writable(summary_db, "写入 Antigravity 摘要库")
        for p in plan:
            try:
                do_import(vault, p["sid"], "antigravity", apply=True, session_id=p["cascade"])
                imported += 1
            except (BadMigrateTarget, SnapshotNotFound, SystemExit, Exception) as exc:
                errors.append({"sid": p["sid"], "title": p["title"][:40], "error": str(exc)[:160]})

    return {
        "applied": apply,
        "planned": len(plan),
        "imported": imported,
        "failed": len(errors),
        "errors": errors[:10],
        "skipped_done": skipped_done,
        "skipped_dup": skipped_dup,
        "projects": proj_summary,
        "items": plan[:50],
        "summary_error": summary_error,
    }


def cmd_sync_antigravity(args):
    """清理已导入 Antigravity 的 ZCode 已归档/已删除会话及空项目注册 (默认 dry-run)。"""
    vault = vault_dir(args)
    only_ag = getattr(args, "only_agent", None)
    res = sync_antigravity_archived(
        vault,
        project=getattr(args, "project", None),
        apply=getattr(args, "apply", False),
        dedup=getattr(args, "dedup", False),
        only_agent=only_ag,
    )
    targets = res["targets"]
    empty_projects = res.get("empty_projects") or []
    if not targets and not empty_projects:
        print("Antigravity 中未发现仍存在的待清理导入会话（已与需求一致）。")
        return
    by_proj: dict[str, list[dict]] = {}
    for t in targets:
        by_proj.setdefault(t["cwd"] or "(无项目)", []).append(t)
    for cwd, lst in sorted(by_proj.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"📁 {cwd} ({len(lst)} 条):")
        for t in lst:
            title = (t["title"] or "(无标题)")[:45]
            tags = [k for k, ok in (("db", t["has_db"]), ("summary", t["has_summary"]), ("proto", t["has_proto"])) if ok]
            if t["reason"] in ("dup", "self_loop"):
                tags.append(str(t["reason"]))
            elif str(t["reason"]).startswith("agent:"):
                tags.append(str(t["reason"]))
            print(f"  {t['cid'][:12]}  [{','.join(tags)}]  {t['sid']}  {title}")
    if empty_projects:
        print(f"🗂 空项目注册 ({len(empty_projects)} 个，清理后已无任何关联对话):")
        for ep in empty_projects:
            print(f"  {ep['id'][:12]}  [project.json]  {ep['name']}  {ep['cwd']}")
    if res["summary_check_error"]:
        print(f"提示: 只读检查摘要缓存受限 ({res['summary_check_error']}); --apply 时请先关闭 Antigravity。")
    if res["applied"]:
        print(f"\n已同步清理 Antigravity: 删除 {res['removed_dbs']} 个 .db 文件, "
              f"清理 {res['cleaned_summaries']} 条摘要索引, {res['cleaned_proto']} 条 proto 缓存, "
              f"清理 {res['removed_projects']} 个空项目配置。")
        print("请重新打开 Antigravity 验证左侧项目与会话列表（全部归档与历史记录仍保留在 Context Vault 与原工具中）。")
    else:
        extra = []
        if res["dup_count"]:
            extra.append(f"{res['dup_count']} 条重复副本")
        if res["agent_count"]:
            extra.append(f"{res['agent_count']} 条非目标来源")
        if empty_projects:
            extra.append(f"{len(empty_projects)} 个空项目配置")
        print(f"\ndry-run: 共待清理 {len(targets)} 条（含 {res['db_files']} 个 .db 文件, "
              f"{res['summary_ids']} 条摘要索引, {res['proto_ids']} 条 proto 缓存"
              + (f", {', '.join(extra)}" if extra else "") + "）。")
        cmd_hint = "cv sync-antigravity --apply"
        if getattr(args, "dedup", False):
            cmd_hint += " --dedup"
        if only_ag:
            cmd_hint += f" --only-agent {only_ag}"
        print(f"请先完全退出 Antigravity（确认任务管理器/托盘无残留），再运行: {cmd_hint}")


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
    conn.executescript(SCHEMA + SYNC_TABLE)  # 曾只建 SCHEMA: 全新 vault 上 export 向 sync_sources
    register_snapshot(conn, envelope, sid, out)  # INSERT 直接 no such table 崩 (二轮 migrate-1)
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

DASH_TMPL = _load_dashboard_html()


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
# 索引实现已解耦至 tools/cv_index.py, 前端模板解耦至 tools/ui/ (经 cv_ui 组装)

CONSOLE_HTML = _load_console_html()


class _KeyStub:
    key = None


def build_vault_server(vault: Path, port: int, token: str | None = None):
    """构建热索引 HTTP 服务 (不阻塞); cmd_serve 与 Pro 托盘共用, 避免实现漂移。
    token: 商业化/局域网档的可选鉴权 — 设置后所有请求需凭据 (X-Vault-Token 头 /
    Cookie vault_token / 首访 ?token= 自动 Set-Cookie), 未授权一律 401。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    # Windows 下默认 allow_reuse_address=1 允许对活动监听端口静默双绑: 第二个 cv serve 收不到
    # 任何流量却写自己的 serve.json 误导 MCP 读到别的 vault — 恒置 False, 绑定冲突走显式回退
    ThreadingHTTPServer.allow_reuse_address = False
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
            # parse_qs 会解码百分号编码键名 (%74oken= 也是有效凭据) — 先解码再匹配,
            # 且大小写不敏感, 否则编码形态的 token 明文绕过脱敏 (二轮 serve-8)
            line = self.requestline
            try:
                line = urllib.parse.unquote(line)
            except Exception:
                pass
            line = re.sub(r"([?&])token=[^& ]*", r"\1token=***", line, flags=re.IGNORECASE)
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
                header = self.headers.get("X-Vault-Token")
                cookie = ""
                for part in (self.headers.get("Cookie") or "").split(";"):
                    if part.strip().startswith("vault_token="):
                        cookie = part.split("=", 1)[1].strip()
                        break
                qtok = "" if post else parse_qs(urlparse(self.path).query).get("token", [""])[0]
                # 候选顺序 header > ?token= > cookie: 旧 cookie (token 轮换前/他实例所种, cookie 不按
                # 端口隔离) 曾先占位导致 ?token= 首访永久 401 且 HttpOnly 前端清不掉 (二轮 serve-5)
                ok = False
                for cand in (header, qtok, cookie):
                    if cand and hmac.compare_digest(str(cand), str(token)):
                        ok = True
                        break
                if not ok:
                    self._json({"error": "unauthorized: 需要 token (?token= 首访, 或 X-Vault-Token 头)"}, 401)
                    return False
                if "vault_token=" not in (self.headers.get("Cookie") or "") or (cookie and cookie != token):
                    self._new_cookie = f"vault_token={token}; Path=/; HttpOnly; SameSite=Strict"  # 失效 cookie 轮换刷新
                return True
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._guard(False):
                return
            try:
                if u.path == "/":
                    self._send(_load_console_html().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if u.path == "/healthz":
                    idx.request_rebuild(60)  # 后台单飞重建; 本请求拿现态秒回, 增长期绝不阻塞
                    # 单次读取 meta/term_count: 快照数与词项数出自同一代索引 (v2 SQLite 倒排)
                    return self._json({"service": "context-vault", "status": "ok",
                                       "snapshots": len(idx.meta), "terms": idx.term_count})
                if u.path == "/audit":
                    return self._json(audit_summary_and_timeline(
                        vault, q.get("since", ""), "all"))
                if u.path == "/memory":
                    try:
                        mlim = max(1, min(int(q.get("limit", 8)), 200))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    mkind = q.get("kind", "")
                    return self._json({
                        "query": q.get("q", ""),
                        "kind": mkind,
                        "counts": memory_counts_api(vault),
                        "memories": memory_search_api(vault, q.get("q", ""), mlim, kind=mkind,
                                                      include_history=(q.get("history") == "1")),
                    })
                if u.path == "/probe_events":
                    conn = db(vault)  # ThreadingHTTPServer 每请求短连接; 显式关闭归还 WAL 句柄
                    try:
                        plim = int(q.get("limit", 100))
                    except ValueError:
                        conn.close()
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    try:
                        rows = conn.execute("SELECT ts,profile,status,http_code,latency_ms,models,missing_models"
                                            " FROM probe_events ORDER BY id DESC LIMIT ?",
                                            (plim,)).fetchall()
                    except sqlite3.OperationalError:
                        rows = []  # 零表空库: 诚实空态, 不落 500 (二轮 concurrency-1)
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
                        num_ok = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
                        try:
                            pj = json.loads(prices_p.read_text(encoding="utf-8"))
                            # bool 是 int 子类, float(True)=1.0 会编造假成本 (二轮 regression-1)
                            prices = {m: v for m, v in pj.items()
                                      if isinstance(v, dict) and num_ok(v.get("input")) and num_ok(v.get("output"))}
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
                    conn.close()
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
                    if q.get("hybrid") == "1":  # 可选稠密重排 (CV_EMBEDDINGS_URL 配置时生效;
                        # 未配置/故障时 hybrid_search 内部回退纯结构化, 行为不变)
                        try:
                            from cv_dense import DenseProvider, EmbeddingCache, hybrid_search
                            return self._json(hybrid_search(
                                idx, vault, q.get("q", ""), lim,
                                provider=DenseProvider(), cache=EmbeddingCache(vault)))
                        except Exception:
                            pass
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
                    meta, sids = idx.meta, idx.sids  # 单次引用读取 (换装是整体换装, 属性读自洽)
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
                        if agent:
                            rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                             " parent_snapshot FROM snapshots WHERE session_ref=? AND agent=?"
                                             " ORDER BY created_at", (ref, agent)).fetchall()
                        else:
                            rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                             " parent_snapshot FROM snapshots WHERE session_ref=?"
                                             " ORDER BY created_at", (ref,)).fetchall()
                        c.close()
                    except sqlite3.Error:
                        rows = []
                    child = {r[5] for r in rows if r[5]}  # parent_snapshot 列 (index 5); 曾误用 x[4] 块数
                    days = {}
                    for r in rows:
                        days.setdefault(str(r["created_at"] or "")[:10], []).append(
                            {"sid": r["id"], "time": str(r["created_at"] or "")[11:19],
                             "tokens_in": r["tokens_in"] or 0, "total_tokens": r["total_tokens"] or 0,
                             "blocks": r["blocks_total"] or 0,
                             "leaf": r["id"] not in child})
                    return self._json({"session": ref, "agent": agent,
                                       "days": [{"day": d, "versions": days[d]}
                                                for d in sorted(days)]})
                if u.path == "/stats":
                    return self._json({"stats": ledger_summary(vault), "owned": owned_stats(vault)})
                if u.path == "/projects":
                    return self._json(group_conversations_by_project(
                        vault,
                        project=q.get("project") or None,
                        include_subagents=(q.get("subagents") == "1"),
                        include_archived=(q.get("archived") == "1"),
                        agent=q.get("agent") or None,
                    ))
                if u.path == "/antigravity/sync":
                    only_ag = q["only_agent"].strip() or None if "only_agent" in q else "zcode"
                    res = sync_antigravity_archived(
                        vault,
                        project=q.get("project") or None,
                        apply=False,
                        dedup=(q.get("dedup", "1") == "1"),
                        only_agent=only_ag,
                    )
                    return self._json(self._agy_sync_json(res))
                if u.path == "/antigravity/batch":
                    try:
                        lim = int(q.get("limit", 0) or 0)
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    res = sync_to_antigravity_batch(
                        vault,
                        agent=q.get("agent", "zcode"),
                        project=q.get("project") or None,
                        include_archived=(q.get("archived") == "1"),
                        limit=lim,
                        apply=False,
                    )
                    return self._json(res)
                if u.path == "/messages":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    try:
                        off = max(0, int(q.get("offset", 0) or 0))
                        lim = max(1, min(int(q.get("limit", 40) or 40), 100))
                    except ValueError:
                        return self._json({"error": "invalid offset/limit: 需要整数"}, 400)
                    env_m = idx.load_env(qid)
                    if not env_m:
                        return self._json({"error": "not found"}, 404)
                    res_m = format_snapshot_messages(
                        env_m,
                        scope=q.get("scope", "live"),
                        offset=off,
                        limit=lim,
                        query=q.get("q", ""),
                    )
                    res_m["id"] = qid
                    return self._json(res_m)
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
                                       "lineage": env.get("lineage") or {},
                                       "stats": self._stats_overlay(qid, env["stats"]), "block_dist": dist})
                self._json({"error": "unknown route",
                            "routes": ["/", "/healthz", "/search?q=", "/context?q=", "/preview?id=",
                                       "/recent", "/projects", "/timeline?session=", "/stats",
                                       "/snapshot?id=", "/messages?id=", "/memory", "/ledger", "/probe_events",
                                       "/antigravity/sync", "/antigravity/batch",
                                       "POST /sync", "POST /migrate", "POST /migrate_project",
                                       "POST /fork", "POST /memory/extract", "POST /memory/dedup",
                                       "POST /antigravity/sync", "POST /antigravity/batch", "POST /probe"]}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        @staticmethod
        def _agy_sync_json(res: dict) -> dict:
            return {
                "applied": res["applied"],
                "targets_count": len(res["targets"]),
                "targets": [
                    {
                        "cid": t["cid"],
                        "sid": t["sid"],
                        "agent": t["agent"],
                        "cwd": t["cwd"],
                        "title": t["title"],
                        "reason": t["reason"],
                        "has_db": t["has_db"],
                        "has_summary": t["has_summary"],
                        "has_proto": t["has_proto"],
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
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
                if not isinstance(body, dict):  # [1,2]/null/"str"/42 曾落 500 兜底回显异常 (二轮 serve-6)
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
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
                                      bool(body.get("apply")),
                                      budget_tokens=body.get("budget_tokens"))
                    except SnapshotNotFound as e:  # 存在性检查到加载之间被删的竞态兜底
                        return self._json({"error": str(e)}, 404)
                    except BadMigrateTarget as e:  # HTTP 层没有 argparse choices, 未知 target 挡在这
                        return self._json({"error": str(e)}, 400)
                    return self._json(r)
                if u.path == "/migrate_project":
                    proj = str(body.get("project") or "").strip()
                    if not proj:
                        return self._json({"error": "missing project: 需要 project 参数"}, 400)
                    target = str(body.get("target") or "antigravity")
                    try:
                        lim = int(body.get("limit") or 0)
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    items = _resolve_project_items(
                        vault, proj, limit=lim,
                        include_archived=bool(body.get("archived")),
                        agent=str(body.get("agent") or "").strip() or None,
                    )
                    if not items:
                        return self._json({"error": f"未找到项目的顶层主对话: {proj}"}, 404)
                    apply_flag = bool(body.get("apply")) and target in ("codex", "claude", "antigravity", "zcode")
                    results = []
                    try:
                        for it in items:
                            results.append(do_import(vault, it["id"], target, apply_flag))
                    except SnapshotNotFound as e:
                        return self._json({"error": str(e)}, 404)
                    except BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json({
                        "project": proj,
                        "target": target,
                        "applied": apply_flag,
                        "count": len(results),
                        "dest_dir": str(Path(results[0]["dest"]).parent),
                        "files": [r["dest"] for r in results],
                        "total_lines": sum(r["lines"] for r in results),
                    })
                if u.path == "/sync":
                    res = sync_vault(vault, debounce=bool(body.get("debounce")), quiet=True)
                    idx.rebuild()
                    if body.get("extract_memory", True):
                        try:
                            mres = extract_memories_incremental(vault)
                            res["memories_added"] = mres["added"]
                            res["memories_total"] = mres["total"]
                        except Exception:
                            res["memories_added"] = 0
                    return self._json(res)
                if u.path == "/memory/extract":
                    mres = extract_memories_incremental(vault, force=bool(body.get("force")))
                    return self._json(mres)
                if u.path == "/memory/dedup":
                    return self._json(memory_dedup(vault))
                if u.path == "/fork":
                    fid = str(body.get("id") or "")
                    if not self._sid_ok(fid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    msg = str(body.get("message") or "").strip()[:200] or None
                    try:
                        fres = fork_snapshot(vault, fid, msg)
                    except SnapshotNotFound as e:
                        return self._json({"error": str(e)}, 404)
                    idx.rebuild()
                    return self._json(fres)
                if u.path == "/antigravity/sync":
                    only_ag = str(body["only_agent"]).strip() or None if "only_agent" in body else "zcode"
                    try:
                        res = sync_antigravity_archived(
                            vault,
                            project=str(body.get("project") or "").strip() or None,
                            apply=bool(body.get("apply")),
                            dedup=bool(body.get("dedup", True)),
                            only_agent=only_ag,
                        )
                    except BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(self._agy_sync_json(res))
                if u.path == "/antigravity/batch":
                    try:
                        lim = int(body.get("limit") or 0)
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    ag = str(body.get("agent") if "agent" in body else "zcode").strip()
                    try:
                        res = sync_to_antigravity_batch(
                            vault,
                            agent=ag,
                            project=str(body.get("project") or "").strip() or None,
                            include_archived=bool(body.get("archived")),
                            limit=lim,
                            apply=bool(body.get("apply")),
                        )
                    except BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(res)
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
    print(f"vault 热索引就绪: {len(idx.meta)} 快照, {idx.term_count} 词项 (引擎 v2, SQLite 倒排)")
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
    vault = vault_dir(args)
    db_ok = (vault / "vault.db").exists()
    own = owned_stats(vault) if db_ok else {"snapshots": 0, "disk_snapshots": 0, "sessions": 0}
    svc_port = 8791
    if (vault / "serve.json").exists():
        try:
            svc_port = int(json.loads((vault / "serve.json").read_text(encoding="utf-8")).get("port") or 8791)
        except (OSError, ValueError):
            pass
    svc_ok = _cv_service_alive(svc_port)

    print(f"Context Vault v{VERSION} · 环境诊断")
    print("\n[1/3] 核心资产库")
    print(f"  {'✓' if db_ok else '✗'} vault 数据库   {vault.resolve() / 'vault.db'}")
    if db_ok:
        sync_note = ("盘库一致" if own["disk_snapshots"] == own["snapshots"]
                     else f"在库文件 {own['disk_snapshots']} ≠ 台账记录 {own['snapshots']}")
        print(f"  ✓ 资产库存     {own['sessions']} 独立会话 / {own['snapshots']} 快照版本 ({sync_note})")
    print(f"  {'✓' if svc_ok else '·'} 常驻热索引   "
          + (f"运行中 http://127.0.0.1:{svc_port}" if svc_ok else "未启动 (按需运行 cv serve; MCP 自动冷回退)"))

    print("\n[2/3] 本地会话源 (发现任一即可同步)")
    sources = [
        ("ZCode 主库", ZCODE_DB.exists(), str(ZCODE_DB)),
        ("Codex 会话", CODEX_SESSIONS.exists(), str(CODEX_SESSIONS)),
        ("Claude Projects", CLAUDE_PROJECTS.exists(), str(CLAUDE_PROJECTS)),
        ("Claude 转录", (HOME / ".claude" / "transcripts").exists(), str(HOME / ".claude/transcripts")),
        ("Antigravity", ANTIGRAVITY_CONV_DIR.exists(), str(ANTIGRAVITY_CONV_DIR)),
    ]
    any_src = False
    for name, good, detail in sources:
        any_src |= good
        print(f"  {'✓' if good else '·'} {name:<15} {detail}")

    print("\n[3/3] 可选增强组件 (缺失不影响核心功能)")
    smig = find_smigrate()
    print(f"  {'✓' if smig else '·'} session-migrate  {smig or '未安装 (uv tool install session-migrate 解锁 18 格式桥接)'}")
    hr_ok = (HEADROOM_DIR / "proxy_savings.json").exists() or (HEADROOM_DIR / "savings_events.jsonl").exists()
    print(f"  {'✓' if hr_ok else '·'} headroom 账本    {HEADROOM_DIR if hr_ok else '未接入 (可选通路层省钱统计)'}")

    core_ok = db_ok or any_src
    print("\n结论: " + ("核心环境就绪" if core_ok else "尚未初始化 vault (运行 python tools/cv.py init 或 sync)"))


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

    li = sub.add_parser("list", help="列出 vault 内快照或各项目顶层主对话")
    li.add_argument("--top", action="store_true", help="仅列出各项目顶层主对话 (过滤历史版本与子代理)")
    li.add_argument("--project", help="按项目名或路径过滤顶层主对话")
    li.add_argument("--agent", help="按来源 agent 过滤 (如 zcode 或 zcode,claude-code)")
    li.add_argument("--include-subagents", action="store_true", help="包含子代理与已归档会话")
    li.add_argument("--include-archived", action="store_true", help="包含 ZCode 已归档会话 (不含子代理)")
    li.set_defaults(func=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("snapshot_id")
    s.set_defaults(func=cmd_show)

    d = sub.add_parser("diff", help="迁移前预览")
    d.add_argument("snapshot_id")
    d.set_defaults(func=cmd_diff)

    i = sub.add_parser("import", help="快照 → 目标 agent 会话文件")
    i.add_argument("snapshot_id", nargs="?")
    i.add_argument("--target", choices=["codex", "claude", "markdown", "antigravity", "zcode"], required=True)
    i.add_argument("--project", help="按项目名或路径批量导入顶层主对话")
    i.add_argument("--agent", help="严格按来源 agent 过滤导入 (如 zcode；不带入 codex 等其他来源)")
    i.add_argument("--limit", type=int, default=0, help="按项目导入时的最多会话数 (0=全部)")
    i.add_argument("--include-subagents", action="store_true", help="包含子代理与已归档会话")
    i.add_argument("--include-archived", action="store_true", help="包含 ZCode 已归档会话 (不含子代理)")
    i.add_argument("--apply", action="store_true", help="写入真实会话目录 (默认 dry-run)")
    i.add_argument("--strip-thinking", action="store_true", help="不迁移 reasoning 摘要")
    i.add_argument("--budget-tokens", type=int, default=None,
                   help="markdown 目标的简报 token 预算 (建议 2000|4000|8000; 缺省按 48KB 推导)")
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="一键: 导出→预览→导入")
    m.add_argument("source")
    m.add_argument("--target", choices=["codex", "claude", "markdown", "antigravity", "zcode"], required=True)
    m.add_argument("--apply", action="store_true")
    m.add_argument("--yes", action="store_true")
    m.add_argument("--strip-thinking", action="store_true")
    m.add_argument("--drop-developer", action="store_true")
    m.add_argument("--budget-tokens", type=int, default=None,
                   help="markdown 目标的简报 token 预算 (建议 2000|4000|8000)")
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
    eh.add_argument("snapshot_id", nargs="?")
    eh.add_argument("--to", required=True, choices=SMIGRATE_TARGETS)
    eh.add_argument("--project", help="按项目名或路径批量导出顶层主对话")
    eh.add_argument("--agent", help="严格按来源 agent 过滤导出 (如 zcode)")
    eh.add_argument("--limit", type=int, default=0, help="按项目导出时的最多会话数 (0=全部)")
    eh.add_argument("--include-subagents", action="store_true", help="包含子代理与已归档会话")
    eh.add_argument("--include-archived", action="store_true", help="包含 ZCode 已归档会话 (不含子代理)")
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
    me = msub.add_parser("extract", help="提取决策/摘要/偏好记忆 (增量幂等, --force 可全量重提)")
    me.add_argument("--snapshot", help="只处理单个快照")
    me.add_argument("--engine", help="记忆引擎 (默认 v2 双时态; 'rule'=v0.1 规则引擎; 或 module:Class 动态加载)")
    me.add_argument("--force", action="store_true", help="忽略已提取水位, 全量重新提取")
    me.set_defaults(func=cmd_memory)
    ms = msub.add_parser("search", help="检索记忆 (默认只回当前有效记忆, 被推翻的不污染)")
    ms.add_argument("query")
    ms.add_argument("--limit", type=int, default=8)
    ms.add_argument("--history", action="store_true",
                    help="追溯决策演变链 (含已 superseded 的旧记忆及其指向)")
    ms.set_defaults(func=cmd_memory)
    ml = msub.add_parser("list", help="最近记忆 (默认只列 active; --history 含已推翻)")
    ml.add_argument("--limit", type=int, default=20)
    ml.add_argument("--history", action="store_true", help="包含已推翻记忆")
    ml.set_defaults(func=cmd_memory)
    mdd = msub.add_parser("dedup", help="合并 (kind, content) 完全相同的跨快照重复摘录")
    mdd.set_defaults(func=cmd_memory)

    mc = sub.add_parser("mcp", help="MCP 记忆后端一键接入客户端")
    msub = mc.add_subparsers(dest="mcp_cmd", required=True)
    mi = msub.add_parser("install")
    mi.add_argument("--client", choices=["claude", "codex", "cursor", "antigravity"], default="claude")
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
    sa = sub.add_parser("sync-antigravity", help="清理已导入 Antigravity 的 ZCode 已归档/已删除会话 (默认 dry-run)")
    sa.add_argument("--project", help="仅处理匹配指定项目名或路径的会话")
    sa.add_argument("--dedup", action="store_true", help="同时清理同快照重复导入 Antigravity 的旧副本")
    sa.add_argument("--only-agent", help="同时清理由 Context Vault 导入的非指定来源 agent 会话 (如 zcode 会移除已导入的 codex/claude-code)")
    sa.add_argument("--apply", action="store_true", help="实际从 Antigravity 会话目录与摘要索引移除 (请先关闭 Antigravity)")
    sa.set_defaults(func=cmd_sync_antigravity)


    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (SnapshotNotFound, BadMigrateTarget) as e:
        # CLI 边界统一转进程退出 (领域层抛可捕获异常, HTTP 层映射 404/400, 这里兜底)
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
