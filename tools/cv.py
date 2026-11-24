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
VERSION = "0.9.0"  # 设置与运维工作台 / 极速并发首屏 / SWR 实时额度 / 瘦身直出托盘 / 双语手册
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
CREATE INDEX IF NOT EXISTS idx_snapshots_parent ON snapshots(parent_snapshot);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_ref ON snapshots(agent, session_ref);
CREATE INDEX IF NOT EXISTS idx_probe_status_id ON probe_events(status, id);
CREATE INDEX IF NOT EXISTS idx_probe_ts ON probe_events(ts);
CREATE TABLE IF NOT EXISTS vault_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def rotate_log_if_needed(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """单文件体积超阈值时滚动为 .1 (防常驻日志无限膨胀)。"""
    try:
        if path.is_file() and path.stat().st_size >= max_bytes:
            bak = path.with_name(path.name + ".1")
            if bak.exists():
                bak.unlink()
            path.replace(bak)
    except OSError:
        pass


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


def _parent_chain_reaches(conn, start: str, target: str, cap: int = 256) -> bool:
    """沿 parent_snapshot 上溯, 判断是否会回到 target (挂链前的防环终检)。
    cap 封顶: 链上已有环或病态长链时停止深走, 只做低成本前置判断。"""
    seen, node = set(), start
    while node and node not in seen and len(seen) < cap:
        if node == target:
            return True
        seen.add(node)
        r = conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?", (node,)).fetchone()
        node = r[0] if r else None
    return False


# ---------------------------------------------------------------- 敏感信息脱敏 (可选, 零依赖)

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
    src = Path(args.source).expanduser()
    fmt = detect_format(first_jsonl(src), src)
    exporters = {"codex-rollout": export_codex, "claude-transcript": export_claude,
                 "claude-projects": export_claude_code}
    exported = exporters[fmt](src, args.drop_developer)
    envelope = build_envelope(exported)
    do_redact = bool(getattr(args, "redact", False)) or os.environ.get("CV_REDACT_SECRETS") == "1"
    redacted_n = 0
    if do_redact:
        envelope, redacted_n = redact_envelope(envelope)
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
            sib = conn.execute("SELECT id, created_at FROM snapshots WHERE agent=? AND session_ref=? AND id<>?"
                               " ORDER BY created_at DESC LIMIT 1",
                               (c0.get("agent"), c0.get("session_ref"), sid)).fetchone()
            row = conn.execute("SELECT parent_snapshot, created_at FROM snapshots WHERE id=?",
                               (sid,)).fetchone()
            if sib and row and not row[0]:  # 只补缺失的 parent, 不动既有链 (H1)
                # 时序守卫 (sync_one H1 同款思路): 候选兄弟须严格旧于本次导出 (created_at 比
                # 较, 平局按 sid) 才挂链 — 旧版本内容重导时最新兄弟比自己还新, 无条件挂链会
                # 成双向环 (V1.parent=V2 且 V2.parent=V1), 叶子去重清零且 storm_relink 只认
                # ≥2 NULL 根, 无法自愈。防环终检兜住平局边角: 兄弟链已能回到本次 sid 即放弃。
                if ((sib[1] or "", sib[0]) < (row[1] or "", sid)
                        and not _parent_chain_reaches(conn, sib[0], sid)):
                    conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (sib[0], sid))
                    envelope["lineage"]["parent_snapshot"] = sib[0]
                elif sib[0] != sid:  # sib 查询已排除 sid, 此处恒真: 守卫拦截时给可读提示
                    print(f"提示: 同会话已有更新版本 {sib[0]} ({(sib[1] or '')[:19]}),"
                          f" 本次导出内容较旧 → 不挂链, 保留为独立快照 (防演进链成环)")
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
    available_agents: list[str] = []
    try:
        c = db(vault)
        try:
            available_agents = [
                str(r[0]).strip().lower()
                for r in c.execute(
                    "SELECT agent, COUNT(*) AS n FROM snapshots WHERE agent IS NOT NULL AND agent != '' "
                    "GROUP BY lower(agent) ORDER BY n DESC"
                ).fetchall()
                if r[0]
            ]
        except sqlite3.OperationalError:
            available_agents = []
        finally:
            c.close()
    except Exception:
        available_agents = []
    return {
        "total_projects": len(projects),
        "total_conversations": len(items),
        "available_agents": available_agents,
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


# ---- 已拆解至 tools/cv_migrate.py (全量 re-export 保持向后兼容) ----
from cv_migrate import (  # noqa: F401,E402
    live_context,
    estimate_tokens,
    cmd_diff,
    _deterministic_session_id,
    blocks_to_codex,
    utc_z,
    blocks_to_claude,
    claude_project_dir,
    _zcode_project_id,
    blocks_to_zcode,
    do_import_zcode,
    estimate_text_tokens,
    _BRIEF_LINE_CAP,
    _brief_stop_point,
    compress_brief,
    blocks_to_brief,
    _record_migration,
    do_import,
    _print_import_result,
    _check_single_sid_agent,
    cmd_import,
    cmd_migrate,
)

# ---- 已拆解至 tools/cv_ops.py (全量 re-export 保持向后兼容) ----
from cv_ops import (  # noqa: F401,E402
    load_profiles,
    models_url,
    fetch_models,
    notify,
    resolve_key,
    probe_once,
    cmd_probe,
    chat_url,
    warm_messages,
    post_chat,
    cmd_warm,
    DASH_TMPL,
    cmd_dashboard,
    active_sessions,
    toast,
    cmd_watch,
)


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
        try:  # 只 connect 不建表的调用路径 (如 /stats 直查) 曾留下零表空库毒化冷路径 — 仅在缺表时建表, 避免热读路径反复跑 DDL 抢写锁
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone() is None:
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


def cv_ledger_sync_cmd(args):
    """薄接线 (延迟导入防循环: cv_ledger 顶层 import cv)。"""
    import cv_ledger
    cv_ledger.cmd_ledger_sync(args)


def cv_ledger_quota_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_quota(args)


def cv_ledger_limit_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_limit(args)


def cv_ledger_plan_token_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_plan_token(args)


def cmd_ledger_stats(args):
    vault = vault_dir(args)
    if not (vault / "vault.db").exists():
        # 无库不建库 (owned_stats 同原则, 二轮 mcp-3): 空目录裸查曾抛 no such table: ledger_events
        # 并凭空留下零表 vault.db, 把冷路径"先 init/export"的诚实指引永久毒化
        print("vault 尚无数据库 (账本为空) — 先运行 cv init 或 cv export <会话文件> 建库入账")
        return
    legacy = bool(getattr(args, "legacy_headroom", False))  # 旧 headroom 通路层段落默认隐藏
    conn = db(vault)
    try:  # 库文件在但零表 (如中断创建): 兜底建表, 与 owned_stats 同防御
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError:
        pass
    row = conn.execute("SELECT tokens_saved, cost_usd FROM ledger_events WHERE source='headroom-lifetime'").fetchone()
    if row and legacy:
        raw = json.loads(conn.execute("SELECT raw FROM ledger_events WHERE source='headroom-lifetime'").fetchone()[0])
        print("== lifetime (headroom 通路层累计) ==")
        print(f"  压缩节省: {row['tokens_saved']:,} tokens = {fmt_usd(row['cost_usd'])}")
        print(f"  缓存节省: {int(raw.get('cache_read_tokens') or 0):,} cache-read tokens = {fmt_usd(raw.get('cache_savings_usd'))}")
        print(f"  总输入:   {int(raw.get('total_input_tokens') or 0):,} tokens = {fmt_usd(raw.get('total_input_cost_usd'))}")
    rows = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c "
                        "FROM ledger_events WHERE kind='compression' GROUP BY model "
                        "ORDER BY c DESC LIMIT 10").fetchall() if legacy else []
    if rows:
        print("== 按模型 (明细) ==")
        for r in rows:
            print(f"  {r['model']:<24} 事件={r['n']:<6} 省={r['t'] or 0:>12,} tok  {fmt_usd(r['c'] or 0)}")
    zx = conn.execute("SELECT COUNT(*) n, model FROM ledger_events WHERE kind='zcode_usage'"
                      " GROUP BY model ORDER BY n DESC").fetchall() if legacy else []
    if zx:
        tot = conn.execute("SELECT COUNT(*) n, SUM(CAST(json_extract(raw,'$.input') AS INT)) i,"
                           " SUM(CAST(json_extract(raw,'$.cache_read') AS INT)) cr"
                           " FROM ledger_events WHERE kind='zcode_usage'").fetchone()
        print(f"== zcode 用量 ({tot['n']} 次调用, 审计直读) ==")
        print(f"  入 {tot['i'] or 0:,} · cache读 {tot['cr'] or 0:,}"
              f" · 模型: {', '.join(r['model'] for r in zx[:3])}")
    try:  # 本地多端直采账本 (usage_events): 无 headroom 时的主账本段
        import cv_ledger
        us = cv_ledger.usage_stats_summary(vault)
        if us.get("events_total"):
            print(f"== 本地多端账本 ({us['events_total']} 事件,"
                  f" 来源 {json.dumps(us.get('sources') or {}, ensure_ascii=False)}) ==")
            print(f"  近 5h:  总输入 {us['window_5h_total_input']:,} · 输出 {us['window_5h_tokens_out']:,}"
                  f" · 费用估算 {fmt_usd(us['window_5h_cost_est_usd'])}")
            print(f"  近 24h: 总输入 {us['window_24h_total_input']:,} · 输出 {us['window_24h_tokens_out']:,}"
                  f" · 费用估算 {fmt_usd(us['window_24h_cost_est_usd'])}")
            hit = us.get("cache_hit_rate")
            if hit is not None:
                print(f"  缓存命中率 {hit * 100:.1f}% · 缓存节省约 {fmt_usd(us['cache_save_est_usd'])}"
                      f" · 估算事件占比 {(us.get('estimated_share') or 0) * 100:.0f}%")
            print("  (cv ledger quota 看 5h 窗口恢复 ETA 与跑道; cv ledger sync 采集)")
    except sqlite3.OperationalError:
        pass  # 老库无 usage_events 表: 跳过该段, 不影响其余输出
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
    优先走零依赖原生 SQLite+Protobuf 直读 (支持 WAL 实时检测、精准 cwd、不因空/二进制工具输出失败);
    老格式原生读空时再兜底试 smigrate。自导入会话 (migrations.jsonl dest 命中) 跳过防回环。"""
    if not ANTIGRAVITY_CONV_DIR.exists():
        return (0, 0, 0)
    exe = find_smigrate()
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
            wal = dbp.parent / (dbp.name + "-wal")
            wst = wal.stat() if wal.exists() else None
            eff_mtime = max(st.st_mtime, wst.st_mtime if wst else 0.0)
            eff_size = st.st_size + (wst.st_size if wst else 0)
        except OSError:
            continue
        prev = conn.execute("SELECT mtime, size, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                            (key,)).fetchone()
        if prev and abs((prev["mtime"] or 0) - eff_mtime) < 1 and (prev["size"] or 0) == eff_size:
            continue
        if debounce and prev and prev["snapshot_id"] and _debounced(prev["synced_at"], eff_mtime):
            continue
        exported = None
        try:
            exported = export_antigravity_db(dbp, False)
        except Exception:
            exported = None
        if (not exported or exported["stats"]["blocks_total"] == 0) and exe:
            tmp = outbox / f"agy-src-{dbp.stem}.jsonl"
            try:
                r = subprocess.run([exe, "convert", str(dbp), "--format", "antigravity",
                                    "--to", "claude", "--output", str(tmp)],
                                   capture_output=True, text=True, timeout=120,
                                   creationflags=NO_WINDOW)
                if r.returncode == 0 and tmp.exists():
                    exported = export_claude_code(tmp, False)
            except Exception:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        if not exported or exported["stats"]["blocks_total"] == 0:
            # 记录空/不可解析水位, 避免每轮 2 分钟重复撞墙空耗 CPU
            conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                         " VALUES(?,?,?,?,?)",
                         (key, eff_mtime, eff_size, prev["snapshot_id"] if prev else "", now_iso()))
            continue
        exported["capture"]["agent"] = "antigravity"
        exported["capture"]["source_format"] = "antigravity-db"
        envelope = build_envelope(exported)
        if exported.get("created_at"):
            envelope["created_at"] = exported["created_at"]
        sid = snapshot_id(envelope)
        if prev and prev["snapshot_id"]:
            if prev["snapshot_id"] == sid:
                conn.execute("INSERT OR REPLACE INTO sync_sources"
                             "(path,mtime,size,snapshot_id,synced_at) VALUES(?,?,?,?,?)",
                             (key, eff_mtime, eff_size, sid, now_iso()))
                continue
            envelope["lineage"]["parent_snapshot"] = prev["snapshot_id"]
        out = vault / "snapshots" / f"{sid}.json"
        if not out.exists():
            atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        register_snapshot(conn, envelope, sid, out)
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (key, eff_mtime, eff_size, sid, now_iso()))
        n_upd += 1 if prev and prev["snapshot_id"] else 0
        n_new += 0 if (prev and prev["snapshot_id"]) else 1
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
        if len(lst) < 2:
            continue
        roots = [x for x in lst if not x[1]["parent_snapshot"]]
        if roots and len(roots) < 2:
            continue  # 一链一叶是健康态
        if not roots:
            # 0 根 = 链上有环 (旧版 export 曾把旧内容重导挂到更新的兄弟下: V1.parent=V2 且
            # V2.parent=V1, 叶子去重清零且无人自愈)。父指针越组的 (fork 副本合法指回源链)
            # 语义不明, 不动; 全员父均在组内则有限集合必有环, 与风暴同法按 mtime 重链,
            # 最旧者置根, 环自然断开
            member_ids = {x[1]["id"] for x in lst}
            if not all(x[1]["parent_snapshot"] in member_ids for x in lst):
                continue
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
    memory_add_manual,
    memory_delete,
    memory_set_pin,
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

def audit_summary_and_timeline(vault: Path, since: str, kind: str, limit: int = 200,
                               summary_only: bool = False) -> dict:
    """统一事件流: probe/switch/warm + 汇总。数据源 probe_events (哨兵/守望/预热全留痕)。
    summary_only=True 时只做单次 SQL 汇总聚合, 不序列化明细 (控制台概览卡秒回)。"""
    conn = db(vault)
    rows = []
    try:
        agg = conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN status='switch' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN status='prune' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN missing_models IS NOT NULL AND missing_models != '[]' THEN 1 ELSE 0 END), 0)"
            " FROM probe_events"
        ).fetchone()
        total, ok_probes, switches, prune_n, model_missing = (int(x or 0) for x in agg)
        if not summary_only:
            q = "SELECT * FROM probe_events WHERE 1=1"
            params = []
            if since:
                q += " AND ts >= ?"
                params.append(since)
            rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
            if kind != "all":
                rows = [r for r in rows if kind in (r["status"],)]
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
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ["--mcp", "--vault", str(vault.resolve())]
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


def mcp_status_api(vault: Path) -> dict:
    """结构化检测各客户端 MCP 接入状态 (免 spawn 慢 CLI 优先读配置, 控制台设置页用)。"""
    py, args_ = _mcp_cmd_parts(vault)

    def _has_cv(p: Path) -> bool:
        try:
            return p.is_file() and "context-vault" in p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    claude_cfg = HOME / ".claude.json"
    claude_exe = _claude_exe()
    claude_on = _has_cv(claude_cfg)
    if not claude_on and claude_exe:
        try:
            out = subprocess.run([claude_exe, "mcp", "list"], capture_output=True, text=True,
                                 timeout=5, creationflags=NO_WINDOW).stdout or ""
            claude_on = "context-vault" in out
        except Exception:
            pass
    codex_dir = HOME / ".codex"
    codex_cfg = codex_dir / "config.toml"
    cursor_dir = Path.home() / ".cursor"
    cursor_cfg = cursor_dir / "mcp.json"
    gemini_dir = HOME / ".gemini"
    agy_cfg1 = gemini_dir / "config" / "mcp_config.json"
    agy_cfg2 = gemini_dir / "antigravity" / "mcp_config.json"

    clients = [
        {"id": "claude", "name": "Claude Code", "detected": bool(claude_exe or claude_cfg.exists()),
         "installed": claude_on, "config": str(claude_cfg)},
        {"id": "codex", "name": "Codex", "detected": codex_dir.exists(),
         "installed": _has_cv(codex_cfg), "config": str(codex_cfg)},
        {"id": "cursor", "name": "Cursor", "detected": cursor_dir.exists(),
         "installed": _has_cv(cursor_cfg), "config": str(cursor_cfg)},
        {"id": "antigravity", "name": "Antigravity", "detected": gemini_dir.exists(),
         "installed": _has_cv(agy_cfg1) or _has_cv(agy_cfg2),
         "config": str(agy_cfg2 if _has_cv(agy_cfg2) else agy_cfg1)},
    ]
    return {"server_cmd": f"{py} {' '.join(args_)}", "clients": clients,
            "installed_count": sum(1 for c in clients if c["installed"]),
            "detected_count": sum(1 for c in clients if c["detected"])}


def mcp_install_api(vault: Path, client: str = "all") -> dict:
    """结构化执行 MCP 写入并返回各客户端结果与最新状态。"""
    installers = {"claude": _install_mcp_claude, "codex": _install_mcp_codex,
                  "cursor": _install_mcp_cursor, "antigravity": _install_mcp_antigravity}
    if client == "all":
        targets = list(installers)
    elif client in installers:
        targets = [client]
    else:
        raise ValueError(f"未知客户端: {client}")
    results = []
    for c in targets:
        try:
            msg = installers[c](vault)
        except Exception as e:
            msg = f"失败: {e}"
        results.append({"client": c, "message": msg})
    return {"results": results, "status": mcp_status_api(vault)}


def prune_snapshots_api(vault: Path, apply: bool = False) -> dict:
    """结构化版本裁剪接口 (复用 cmd_prune 逻辑, 供控制台设置页调用)。"""
    import io as _io
    import contextlib as _cl
    from types import SimpleNamespace as _NS
    buf = _io.StringIO()
    with _cl.redirect_stdout(buf):
        cmd_prune(_NS(vault=str(vault), apply=bool(apply)))
    out = buf.getvalue().strip()
    m = re.search(r"保留\s+(\d+)\s*/\s*(\d+)\s*版.*?回收\s+(\d+)\s*版\s*/\s*([\d.]+)\s*MB", out)
    return {
        "applied": bool(apply),
        "kept": int(m.group(1)) if m else 0,
        "total": int(m.group(2)) if m else 0,
        "drop_count": int(m.group(3)) if m else 0,
        "reclaim_mb": float(m.group(4)) if m else 0.0,
        "message": out,
    }


def backup_vault_api(vault: Path, keep: int = 10) -> dict:
    """结构化一致性备份接口 (复用 cmd_backup)。"""
    import io as _io
    import contextlib as _cl
    from types import SimpleNamespace as _NS
    buf = _io.StringIO()
    with _cl.redirect_stdout(buf):
        cmd_backup(_NS(vault=str(vault), out=None, keep=max(1, min(int(keep or 10), 50))))
    out = buf.getvalue().strip()
    backups = sorted((vault / "backups").glob("cv-backup-*.zip")) if (vault / "backups").is_dir() else []
    latest = backups[-1] if backups else None
    return {
        "ok": True,
        "name": latest.name if latest else "",
        "path": str(latest) if latest else "",
        "size_mb": round(latest.stat().st_size / 1048576, 2) if latest and latest.exists() else 0.0,
        "backups_count": len(backups),
        "message": out.splitlines()[0] if out else "备份完成",
    }


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


# ---- 已拆解至 tools/cv_antigravity.py (全量 re-export 保持向后兼容) ----
from cv_antigravity import (  # noqa: F401,E402
    SMIGRATE_CANDIDATES,
    _varint,
    _rd_varint,
    _pb_len,
    _pb_var,
    _pb_decode,
    _pb_encode,
    _session_epoch_range,
    _antigravity_build_step_metadata,
    _antigravity_fill_step_timestamps,
    _antigravity_normalize_steps,
    _antigravity_read_cascade_id,
    export_antigravity_db,
    _antigravity_resolve_project_id,
    _antigravity_set_workspace,
    _antigravity_ws_uris,
    _antigravity_first_user_text,
    _antigravity_raw_summary,
    _ts_msg,
    _antigravity_register_summaries,
    _antigravity_proto_upsert,
    _load_snap_cwd_cached,
    _collect_antigravity_migrated_dbs,
    _antigravity_proto_prune,
    _antigravity_db_score,
    _antigravity_summary_ops,
    _uri_to_norm_path,
    _antigravity_find_empty_projects,
    sync_antigravity_archived,
    _probe_antigravity_summary_writable,
    _antigravity_eligible_cwd,
    _pb_collect_strings,
    _antigravity_db_fingerprint,
    sync_to_antigravity_batch,
    cmd_sync_antigravity,
    find_smigrate,
    _deterministic_ingest_id,
    cmd_ingest,
)


# ---- (同组其余实现见 tools/cv_ops.py) ----


# ---- 已拆解至 tools/cv_server.py (全量 re-export 保持向后兼容) ----
from cv_server import (  # noqa: F401,E402
    CONSOLE_HTML,
    _KeyStub,
    build_vault_server,
    SERVE_FALLBACK_PORTS,
    _cv_service_alive,
    cmd_serve,
)

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

def _force_utf8_stdio():
    """中文 Windows 默认 GBK 控制台: 📁/✓ 等字符在 list --top 管道等场景崩 UnicodeEncodeError
    (曾只有 doctor 一处 reconfigure, 其余子命令裸奔)。main() 入口统一 stdin/stdout/stderr
    → utf-8, errors=replace 防御个别窄字符集环境; MCP 侧 reconfigure 在 mcp_server.py 自理。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 流已被接管/无 reconfigure (如测试捕获对象): 保底原样


def cmd_doctor(args):
    _force_utf8_stdio()  # ✓/✗ 需要 utf-8 (main 入口已统一, 直调 doctor 的路径在此兜底)
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


def cmd_setup(args):
    """一步式初始化向导: 建库 → 同步全端会话 → 同步多端账本 → 提取记忆 → (可选) 接入 MCP。"""
    vault = vault_dir(args)
    print(f"[1/4] 初始化 vault 目录: {vault}")
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    try:
        conn.executescript(SCHEMA + SYNC_TABLE)
        ensure_memory_schema(conn)
        conn.commit()
    finally:
        conn.close()

    print("[2/4] 扫描并同步本地会话资产...")
    sres = sync_vault(vault, quiet=True)
    print(f"  同步完成: 新增 {sres.get('new', 0)} · 更新 {sres.get('updated', 0)} · 累计 {sres.get('total', 0)} 快照")

    if not getattr(args, "no_ledger", False):
        print("[3/4] 同步多端 token 账本 (Claude/Codex/ZCode/Antigravity)...")
        try:
            import cv_ledger as _cl
            lres = _cl.sync_usage_events(vault)
            print(f"  账本同步完成: 新增事件 {lres.get('added', 0)} · 扫描文件 {lres.get('files_scanned', 0)}")
        except Exception as e:
            print(f"  账本同步跳过: {e}")
    else:
        print("[3/4] 已跳过多端账本同步 (--no-ledger)")

    print("[4/4] 提取跨会话双时态记忆...")
    mres = extract_memories_incremental(vault)
    print(f"  记忆提取完成: 新增 {mres.get('added', 0)} · 活跃 {mres.get('active', 0)} / 总计 {mres.get('total', 0)}")

    if getattr(args, "mcp", False):
        print("[附加] 自动接入全部检测到的 MCP 客户端...")
        ns = argparse.Namespace(
            vault=str(vault),
            mcp_cmd="install",
            client="all",
            all=True,
            dry_run=False,
        )
        try:
            cmd_mcp(ns)
        except Exception as e:
            print(f"  MCP 接入提示: {e}")

    print("\n初始化完成！下一步推荐:")
    print("  1. 启动 Web 控制台 + 热索引 + AML API:  cv serve  (或 python tools/cv.py serve)")
    if not getattr(args, "mcp", False):
        print("  2. 一键接入全部 MCP 客户端:             cv mcp install --all")


# ------------------------------------------------- main

def cmd_semantic(args):
    """语义增强 CLI: status / setup / refine / titles (零依赖, 未配置零外发)。"""
    import cv_semantic as _sem
    vault = vault_dir(args)
    sc = args.sem_cmd
    if sc == "status":
        st = _sem.semantic_status(vault, probe=bool(getattr(args, "probe", False)))
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    if sc == "setup":
        if getattr(args, "ollama", False):
            res = _sem.configure_ollama(
                vault,
                host=getattr(args, "host", None) or "http://127.0.0.1:11434",
                embed_model=getattr(args, "embed_model", None),
                llm_model=getattr(args, "llm_model", None),
                enable_llm=not bool(getattr(args, "no_llm", False)),
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        updates = {}
        if getattr(args, "embeddings_url", None):
            updates["embeddings_url"] = args.embeddings_url
        if getattr(args, "embed_model", None):
            updates["embeddings_model"] = args.embed_model
        if getattr(args, "llm_url", None):
            updates["llm_url"] = args.llm_url
        if getattr(args, "llm_model", None):
            updates["llm_model"] = args.llm_model
        _sem.save_ai_config(vault, updates)
        print(json.dumps(_sem.semantic_status(vault, probe=False), ensure_ascii=False, indent=2))
        return
    if sc == "refine":
        res = _sem.refine_memories_with_llm(
            vault,
            limit=int(getattr(args, "limit", 10)),
            snapshot_id=getattr(args, "snapshot", None),
            force=bool(getattr(args, "force", False)),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    if sc == "titles":
        res = _sem.generate_session_titles(
            vault,
            limit=int(getattr(args, "limit", 30)),
            force=bool(getattr(args, "force", False)),
            use_llm=not bool(getattr(args, "heuristic_only", False)),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))


def main(argv=None):
    _force_utf8_stdio()  # 全子命令统一 (曾仅 doctor 有: env -u PYTHONUTF8 下 list --top 管道崩)
    ap = argparse.ArgumentParser(
        prog="cv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Context Vault — 本地优先的 AI 编程会话资产库 (零第三方依赖)\n\n"
            "新手 3 步快速上手:\n"
            "  1. cv setup --mcp      建库 → 同步全端会话 → 账本 → 提取记忆 → 接入 MCP\n"
            "  2. cv serve --open     启动本地热索引服务并自动打开 Web 控制台\n"
            "  3. cv migrate <文件> --target codex|claude   跨工具迁移 (默认 dry-run)\n\n"
            "常用高频子命令: setup · serve · sync · migrate · mcp · ledger · memory · semantic"
        ),
        epilog="完整场景指南见 docs/USAGE.md；加 --vault <目录> 可切换库路径 (默认 ./vault)。",
    )
    ap.add_argument("--version", action="version", version=f"context-vault {VERSION}")
    ap.add_argument("--vault", default="vault", help="vault 目录 (默认 ./vault)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    e = sub.add_parser("export", help="导出会话文件为快照 (只读源文件)")
    e.add_argument("source")
    e.add_argument("--drop-developer", action="store_true")
    e.add_argument("--redact", action="store_true", help="导出前自动脱敏常见 API Key/Token/私钥 (或设 CV_REDACT_SECRETS=1)")
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
    lsy = lsub.add_parser("sync", help="多端直采入账: Claude/Codex/ZCode/Antigravity 本地日志 → usage_events (幂等, 脱离 headroom)")
    lsy.add_argument("--sources", default="all",
                     help="all|claude|codex|zcode|antigravity (可逗号组合, 默认 all)")
    lsy.add_argument("--from-agm",
                     help="可选外部管理器统计快照 URL (如本机 Antigravity-Manager; 尽力而为, 失败静默跳过)")
    lsy.add_argument("--recompute", action="store_true",
                     help="按当前 pricing.json/accounts.json 强制重算全部存量事件的账号与费用")
    lsy.set_defaults(func=cv_ledger_sync_cmd)
    lq = lsub.add_parser("quota", help="5h 滚动窗口 / 恢复 ETA / 跑道 / Top 模型与项目")
    lq.add_argument("--window", default="5h", choices=["5h", "24h", "7d", "30d"])
    lq.add_argument("--account", help="账号过滤 (accounts.json 具名账号或 client:x 伪账号)")
    lq.add_argument("--json", action="store_true", help="输出 JSON (供脚本消费)")
    lq.set_defaults(func=cv_ledger_quota_cmd)
    ll = lsub.add_parser("limit", help="额度池配置助手 (套餐预设 / 写入 accounts.json; 厂商实时额度见 --list 提示)")
    ll.add_argument("--list", action="store_true", help="打印套餐预设 (每 5h 提示次数口径) 与实时额度接入说明")
    ll.add_argument("--preset", help="套餐名 (见 --list; 记入 profile 供参考)")
    ll.add_argument("--name", help="profile 名 (默认同 --preset)")
    ll.add_argument("--tokens-5h", type=int, help="5h 滚动窗口 token 上限")
    ll.add_argument("--tokens-week", type=int, help="7 天周期 token 上限 (kind=7d)")
    ll.add_argument("--source", help="匹配来源 (逗号分隔, 如 zcode,claude)")
    ll.add_argument("--client", help="匹配客户端 (逗号分隔)")
    ll.add_argument("--model-prefix", help="匹配模型通配 (逗号分隔, 如 'glm-*')")
    ll.set_defaults(func=cv_ledger_limit_cmd)
    pt = lsub.add_parser("plan-token", help="配置智谱 Coding Plan 用户 Token (厂商实时套餐额度)")
    pt.add_argument("token", nargs="?", help="Coding Plan 用户 Token (浏览器 F12 → 请求头 Authorization; 非普通 API Key)")
    pt.add_argument("--clear", action="store_true", help="清除已存 Token")
    pt.set_defaults(func=cv_ledger_plan_token_cmd)
    _lst = lsub.add_parser("stats", help="账本汇总 (headroom 旧段落默认隐藏)")
    _lst.add_argument("--legacy-headroom", action="store_true",
                      help="补印旧 headroom 通路层 lifetime/按模型节省 (默认彻底隐藏)")
    _lst.set_defaults(func=cmd_ledger_stats)

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

    sv = sub.add_parser("serve", help="常驻服务: 热索引 + Web 控制台 + /aml/* API (127.0.0.1)")
    sv.add_argument("--port", type=int, default=8791)
    sv.add_argument("--open", action="store_true", help="启动后自动在默认浏览器打开 Web 控制台")
    sv.add_argument("--token", default=None,
                    help="启用鉴权 (或环境变量 CV_SERVE_TOKEN): 客户端首访 ?token= 自动登录, 脚本用 X-Vault-Token 头")
    sv.set_defaults(func=cmd_serve)

    b = sub.add_parser("backup", help="vault 一致性备份 (sqlite 在线快照 + snapshots 打包, 轮换保留)")
    b.add_argument("--out", default=None, help="备份目录 (默认 vault/backups; 建议定期指向外部盘)")
    b.add_argument("--keep", type=int, default=10, help="保留份数 (默认 10, 旧的自动清理)")
    b.set_defaults(func=cmd_backup)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    stp = sub.add_parser("setup", help="一步式初始化向导: 建库 → 同步会话 → 账本 → 记忆 → (可选) MCP")
    stp.add_argument("--mcp", action="store_true", help="同时自动接入全部本地 MCP 客户端")
    stp.add_argument("--no-ledger", action="store_true", help="跳过多端 token 账本同步")
    stp.set_defaults(func=cmd_setup)
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

    sem = sub.add_parser("semantic", help="语义增强层: Ollama/向量端点配置、本地 LLM 记忆精炼与自动会话标题")
    semsub = sem.add_subparsers(dest="sem_cmd", required=True)
    sem_st = semsub.add_parser("status", help="查看向量重排与本地 LLM 配置状态")
    sem_st.add_argument("--probe", action="store_true", help="同时探测本机 Ollama 服务与已下载模型")
    sem_st.set_defaults(func=cmd_semantic)
    sem_su = semsub.add_parser("setup", help="持久化配置 Ollama 或自定义 OpenAI 兼容端点到 vault/ai_config.json")
    sem_su.add_argument("--ollama", action="store_true", help="一键配置本地 Ollama (默认 127.0.0.1:11434)")
    sem_su.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama 服务地址")
    sem_su.add_argument("--embed-model", help="向量模型名 (如 nomic-embed-text / bge-m3)")
    sem_su.add_argument("--llm-model", help="本地 LLM 模型名 (如 qwen2.5:7b)")
    sem_su.add_argument("--embeddings-url", help="自定义 OpenAI 兼容 /v1/embeddings 地址")
    sem_su.add_argument("--llm-url", help="自定义 OpenAI 兼容 /v1/chat/completions 地址")
    sem_su.add_argument("--no-llm", action="store_true", help="仅配置向量重排, 不配置 LLM 精炼")
    sem_su.set_defaults(func=cmd_semantic)
    sem_rf = semsub.add_parser("refine", help="用本地/兼容 LLM 对历史快照做隐式记忆精炼")
    sem_rf.add_argument("--limit", type=int, default=10, help="本次最多精炼快照数 (默认 10)")
    sem_rf.add_argument("--snapshot", help="仅精炼指定快照 ID")
    sem_rf.add_argument("--force", action="store_true", help="忽略精炼水位重新处理")
    sem_rf.set_defaults(func=cmd_semantic)
    sem_tt = semsub.add_parser("titles", help="清洗低信息量会话标题 (有 LLM 则用 LLM, 否则用零依赖启发式提炼)")
    sem_tt.add_argument("--limit", type=int, default=30, help="本次最多更新条数 (默认 30)")
    sem_tt.add_argument("--force", action="store_true", help="强制重算所有近期会话标题")
    sem_tt.add_argument("--heuristic-only", action="store_true", help="仅用本地零依赖启发式清洗, 不调用 LLM")
    sem_tt.set_defaults(func=cmd_semantic)


    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (SnapshotNotFound, BadMigrateTarget) as e:
        # CLI 边界统一转进程退出 (领域层抛可捕获异常, HTTP 层映射 404/400, 这里兜底)
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
