"""Context Vault 多端用量采集与幂等入账引擎 (纯 Python 3.9+ 标准库)。

由 tools/cv_ledger.py 拆出，集中存放 Claude Code / Codex / ZCode / Antigravity / DSH
采集器、水位检查、protobuf 解析、升级迁移与 sync_usage_events / cmd_ledger_sync。
"""
from __future__ import annotations

import bisect
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote

import cv_core

_cv = cv_core.cv_mod


def _cl():
    """延迟取得 cv_ledger 模块，兼容测试对 urlopen / agv_workspaces / _agv_base_state 等的 monkeypatch。"""
    return sys.modules.get("cv_ledger") or __import__("cv_ledger")


# ---------------------------------------------------------------- 采集器 (只读)

def _file_sig(path: Path, with_wal: bool):
    """(mtime, size[, wal_mtime, wal_size]) — antigravity 的 .db 需把 .db-wal 一并纳入水位,
    否则 WAL 未 checkpoint 时活跃会话漏采。"""
    st = path.stat()
    mtime, size = st.st_mtime, st.st_size
    if with_wal:
        wal = Path(str(path) + "-wal")
        if wal.exists():
            wst = wal.stat()
            return mtime, size, wst.st_mtime, wst.st_size
        return mtime, size, 0.0, 0
    return mtime, size


def _wm_changed(conn, source: str, path: Path, sig: tuple) -> bool:
    try:
        row = conn.execute("SELECT mtime,size,wal_mtime,wal_size FROM usage_sync_state"
                           " WHERE source=? AND path=?", (source, str(path))).fetchone()
    except sqlite3.OperationalError:
        return True
    if not row:
        return True
    cur = (row["mtime"] or 0, row["size"] or 0, row["wal_mtime"] or 0, row["wal_size"] or 0)
    want = (sig + (0.0, 0))[:4]
    return cur != want


def _wm_update(conn, source: str, path: Path, sig: tuple):
    s2 = (sig + (0.0, 0, 0))[:4]
    conn.execute("INSERT INTO usage_sync_state(source,path,mtime,size,wal_mtime,wal_size,synced_at)"
                 " VALUES(?,?,?,?,?,?,?)"
                 " ON CONFLICT(source,path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,"
                 " wal_mtime=excluded.wal_mtime, wal_size=excluded.wal_size,"
                 " synced_at=excluded.synced_at",
                 (source, str(path), s2[0], s2[1], s2[2], s2[3], _cv().now_iso()))


def collect_claude(base_dir=None, skip_fn=None):
    """~/.claude/projects/**/*.jsonl — assistant 行 message.usage (Claude 语义: input 不含 cache)。
    同文件内按 message.id (回退 requestId → uuid) 去重取 output_tokens 最大的一条:
    同一次请求的流式多内容块各自带累计 usage, 逐行入账会重复计费。
    skip_fn(path, sig) 为真时该文件未变更, 不读盘直接跳过 (水位照记)。"""
    c = _cv()
    l = _cl()
    root = Path(base_dir) if base_dir else Path(c.HOME) / ".claude" / "projects"
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.rglob("*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        groups = {}
        last_cwd = None
        try:
            for obj in c.iter_jsonl(f):
                if obj.get("type") != "assistant":
                    if obj.get("cwd"):
                        last_cwd = obj.get("cwd")
                    continue
                msg = obj.get("message") or {}
                u = msg.get("usage") or {}
                if not isinstance(u, dict) or ("input_tokens" not in u and "output_tokens" not in u):
                    continue
                if obj.get("cwd"):
                    last_cwd = obj.get("cwd")
                sid = obj.get("sessionId") or f.stem
                gkey = msg.get("id") or obj.get("requestId") or obj.get("uuid") or ""
                if not gkey:
                    continue
                tout = int(l._num(u.get("output_tokens")))
                prev = groups.get(gkey)
                if prev is not None and tout <= prev["tokens_out"]:
                    continue  # 同 message.id 的旧分块: 只保留 output 最大的一条
                groups[gkey] = {
                    "event_key": l._sha(f"claude|{sid}|{gkey}"), "source": "claude",
                    "client": "claude-code", "model": msg.get("model") or None,
                    "project": last_cwd, "session_ref": sid,
                    "occurred_at": l.ts_to_epoch(obj.get("timestamp")) or 0.0,
                    "tokens_in": max(0, int(l._num(u.get("input_tokens")))), "tokens_out": tout,
                    "reasoning_tokens": 0,
                    "cache_read": max(0, int(l._num(u.get("cache_read_input_tokens")))),
                    "cache_write": max(0, int(l._num(u.get("cache_creation_input_tokens")))),
                    "is_estimated": 0,
                    "raw": json.dumps({"requestId": obj.get("requestId"),
                                       "message_id": msg.get("id"), "cwd": last_cwd,
                                       "sidechain": bool(obj.get("isSidechain"))},
                                      ensure_ascii=False)}
        except (OSError, ValueError):
            continue  # 半截写入/编码坏行: 跳过该文件, 水位未推下轮重试
        events.extend(groups.values())
        marks.append((f, sig))
    return events, marks


def collect_codex(base_dir=None, skip_fn=None):
    """~/.codex/sessions/**/rollout-*.jsonl — event_msg/token_count 事件 (OpenAI 语义: input 含 cache)。
    顺序跟踪 prev_total, 仅 total_token_usage 真增长时取增量 (心跳重复不双计)。
    skip_fn(path, sig) 为真时该文件未变更, 不读盘直接跳过。"""
    c = _cv()
    l = _cl()
    root = Path(base_dir) if base_dir else Path(c.CODEX_SESSIONS)
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.rglob("rollout-*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        sid, cwd, model = None, None, None
        prev = None  # (in, out, cached, cw, reason) 上次累计
        try:
            for obj in c.iter_jsonl(f):
                t, payload = obj.get("type"), obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if t == "session_meta":
                    sid = payload.get("id") or payload.get("session_id") or sid
                    cwd = payload.get("cwd") or cwd
                elif t == "turn_context":
                    model = payload.get("model") or model
                    cwd = payload.get("cwd") or cwd
                elif t == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    tot = info.get("total_token_usage")
                    if not isinstance(tot, dict):
                        continue
                    settings = payload.get("thread_settings") or {}
                    if isinstance(settings, dict) and settings.get("model"):
                        model = settings["model"] or model
                    ti = int(l._num(tot.get("input_tokens")))
                    to = int(l._num(tot.get("output_tokens")))
                    tc = int(l._num(tot.get("cached_input_tokens")))
                    tcw = int(l._num(tot.get("cache_write_input_tokens")))
                    tr = int(l._num(tot.get("reasoning_output_tokens")))
                    cur = (ti, to, tc)
                    if prev is not None and cur <= prev[0:3]:
                        continue  # 心跳/回退: 累计未真增长, 不出事件
                    base = prev if prev is not None else (0, 0, 0, 0, 0)
                    d_in, d_out = ti - base[0], to - base[1]
                    d_cached, d_cw, d_reason = tc - base[2], tcw - base[3], tr - base[4]
                    prev = (ti, to, tc, tcw, tr)
                    if d_in <= 0 and d_out <= 0:
                        continue
                    sid_eff = sid or f.stem
                    cw = max(0, d_cw)
                    events.append({
                        "event_key": l._sha(f"codex|{sid_eff}|{ti}:{to}:{tc}"),
                        "source": "codex", "client": "codex-cli", "model": model or None,
                        "project": cwd, "session_ref": sid_eff,
                        "occurred_at": l.ts_to_epoch(obj.get("timestamp")) or 0.0,
                        "tokens_in": max(0, d_in - max(0, d_cached) - cw),  # 规范化: 扣除缓存
                        "tokens_out": max(0, d_out), "reasoning_tokens": max(0, d_reason),
                        "cache_read": max(0, d_cached), "cache_write": cw,
                        "is_estimated": 0,
                        "raw": json.dumps({"thread_id": sid, "model": model},
                                          ensure_ascii=False)})
        except (OSError, ValueError):
            continue
        marks.append((f, sig))
    return events, marks


def collect_zcode_db(skip_fn=None, proj_map=None):
    """ZCode 主库 ~/.zcode/cli/db/db.sqlite model_usage 表 — 全量调用真相源 (55k+ 行)。
    每行一次模型调用: id 稳定幂等键, started_at/completed_at 毫秒 epoch, input 含缓存
    (OpenAI 语义, 入库规范化扣除 cache)。
    项目归因: 主库 session 表 directory (主源, 同库 join) > proj_map (vault snapshots 兜底)。
    返回 (events, marks) 或 None (不可用→回落)。"""
    l = _cl()
    db_path = getattr(_cv(), "ZCODE_DB", None)
    if not db_path or not Path(db_path).exists():
        return None
    db_path = Path(db_path)
    try:
        sig = _file_sig(db_path, with_wal=True)
    except OSError:
        return None
    if skip_fn and skip_fn(db_path, sig):
        return [], [(db_path, sig)]  # 未变更: 不读盘
    zdb = None
    events = []
    try:
        zdb = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        tables = {r[0] for r in zdb.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r[1] for r in zdb.execute("PRAGMA table_info(model_usage)").fetchall()] \
            if "model_usage" in tables else []
        if "input_tokens" not in cols:
            return None  # 无 model_usage 表 → 回落 model-io
        sess_dir = {}
        if "session" in tables:  # 项目归因主源: session.directory (工作区路径)
            try:
                scols = [r[1] for r in zdb.execute("PRAGMA table_info(session)").fetchall()]
                if "directory" in scols:
                    for sid, d in zdb.execute(
                            "SELECT id, directory FROM session"
                            " WHERE directory IS NOT NULL"):
                        if d:
                            sess_dir[str(sid)] = str(d)
            except sqlite3.Error:
                pass
        sel_cols = [c for c in ("id", "session_id", "model_id", "agent", "started_at",
                                "completed_at", "input_tokens", "output_tokens",
                                "reasoning_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens", "provider_id")
                    if c in cols]
        for row in zdb.execute(f"SELECT {', '.join(sel_cols)} FROM model_usage"):
            r = dict(zip(sel_cols, row))
            rid = str(r.get("id") or r.get("logical_request_id") or "").strip()
            if not rid:
                continue
            raw_ms = l._num(r.get("completed_at") or r.get("started_at"), 0)
            ep = (raw_ms / 1000.0) if raw_ms > 1e12 else (float(raw_ms) if raw_ms else 0.0)
            tin_all = int(l._num(r.get("input_tokens")))
            cr = max(0, int(l._num(r.get("cache_read_input_tokens"))))
            cw = max(0, int(l._num(r.get("cache_creation_input_tokens"))))
            session_id = r.get("session_id") or None
            events.append({
                "event_key": l._sha(f"zdb|{rid}"),
                "source": "zcode", "client": "zcode", "model": r.get("model_id") or None,
                "project": sess_dir.get(str(session_id or ""))
                or (proj_map or {}).get(session_id),
                "session_ref": session_id,
                "occurred_at": ep,
                "tokens_in": max(0, tin_all - cr - cw),  # 规范化: input 含缓存 → 扣除
                "tokens_out": int(l._num(r.get("output_tokens"))),
                "reasoning_tokens": int(l._num(r.get("reasoning_tokens"))),
                "cache_read": cr, "cache_write": cw, "is_estimated": 0,
                "raw": json.dumps({"zdb_id": rid, "session_id": session_id,
                                   "agent": r.get("agent"), "provider": r.get("provider_id")},
                                  ensure_ascii=False)})
        return events, [(db_path, sig)]
    except sqlite3.Error:
        return None  # 锁/损坏: 回落 model-io, 下轮重试
    finally:
        if zdb:
            zdb.close()


def collect_zcode(base_dir=None, skip_fn=None, proj_map=None):
    """ZCode 用量采集 — 首选主库 model_usage 全量真相源 (统一口径, 不再只吃 model-io 零头);
    主库缺失/损坏时回落 ~/.zcode/cli/rollout/model-io-*.jsonl 审计文件 (双格式规范化)。
    proj_map: {session_ref: 项目路径} (sync 层从 vault snapshots 合成, 归因兜底)。
    skip_fn(path, sig) 为真时不读盘跳过。"""
    zdb = collect_zcode_db(skip_fn=skip_fn, proj_map=proj_map)
    if zdb is not None:
        return zdb  # 主库真相源 (events, marks)
    c = _cv()
    l = _cl()
    root = Path(base_dir) if base_dir else Path(c.HOME) / ".zcode" / "cli" / "rollout"
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.glob("model-io-*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        stem_session = f.stem.replace("model-io-", "")
        try:
            for obj in c.iter_jsonl(f):
                rid = obj.get("requestId")
                if not rid:
                    continue
                resp = obj.get("response") or {}
                u = resp.get("usage") or {}
                if not isinstance(u, dict):
                    continue
                mo = obj.get("model") or {}
                model = (mo.get("modelId") if isinstance(mo, dict) else None) \
                    or resp.get("modelId") or None
                if "inputTokens" in u:  # camelCase 聚合: input 已含 cache (OpenAI 语义)
                    cr = max(0, int(l._num(u.get("cacheReadTokens"))))
                    cw = max(0, int(l._num(u.get("cacheWriteTokens"))))
                    tin = max(0, int(l._num(u.get("inputTokens"))) - cr - cw)
                    tout = int(l._num(u.get("outputTokens")))
                elif "input_tokens" in u:  # snake_case 原始体: Claude 语义, input 不含 cache
                    tin = max(0, int(l._num(u.get("input_tokens"))))
                    tout = int(l._num(u.get("output_tokens")))
                    cr = max(0, int(l._num(u.get("cache_read_input_tokens"))))
                    cw = max(0, int(l._num(u.get("cache_creation_input_tokens"))))
                else:
                    continue
                events.append({
                    "event_key": l._sha(f"zcode|{rid}|{l._num(obj.get('attempt'), 0)}"),
                    "source": "zcode", "client": "zcode", "model": model,
                    "project": (proj_map or {}).get(stem_session),
                    "session_ref": stem_session,
                    "occurred_at": l.ts_to_epoch(obj.get("completedAt")) or 0.0,
                    "tokens_in": tin, "tokens_out": tout, "reasoning_tokens": 0,
                    "cache_read": cr, "cache_write": cw, "is_estimated": 0,
                    "raw": json.dumps({"requestId": rid,
                                       "provider": mo.get("providerId")
                                       if isinstance(mo, dict) else None,
                                       "duration_ms": obj.get("durationMs")},
                                      ensure_ascii=False)})
        except (OSError, ValueError):
            continue
        marks.append((f, sig))
    return events, marks


_AGV_TS_COLS = ("ts", "timestamp", "created_at", "time", "event_ts")
# 内容 blob 列 (按优先级): 真实 schema (2026-11 实测) steps(step_payload/metadata, 无时间戳列);
# "data" 为旧版假设布局, 保留兼容
_AGV_CONTENT_COLS = ("step_payload", "data")
AGV_EST_MODEL = "antigravity (估算)"  # 无模型字段的估算事件命名 (替代 "(未知)")


def _pb_varint(b: bytes, i: int):
    """protobuf varint 解码 → (value, next_index); 越界/超长返回已解码部分。"""
    v = 0
    shift = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not c & 0x80:
            return v, i
        shift += 7
        if shift > 70:
            break
    return v, i


def _meta_step_ts(meta):
    """steps.metadata protobuf → 步级 epoch 秒; 解析不出 → None。
    布局 (2026-11 真机 93/93 库逐步骤实测, 与 cv 导出产物同构):
    顶层 field1 wiretype2 = Timestamp 消息, 内层 field1 varint = epoch 秒。"""
    b = bytes(meta) if meta else b""
    i = 0
    n = len(b)
    while i < n:
        tag, j = _pb_varint(b, i)
        wt, fno = tag & 7, tag >> 3
        if fno == 0:
            return None
        if wt == 0:
            v, i = _pb_varint(b, j)
            if fno == 1 and 1_500_000_000 < v < 2_100_000_000:
                return float(v)
        elif wt == 2:
            ln, k = _pb_varint(b, j)
            i = k + ln
            if fno == 1 and ln:
                inner = b[k:i]
                if inner and inner[0] == 0x08:
                    v, _ = _pb_varint(inner, 1)
                    if 1_500_000_000 < v < 2_100_000_000:
                        return float(v)
        elif wt == 5:
            i = j + 4
        elif wt == 1:
            i = j + 8
        else:
            return None
    return None


def _pb_kv_map(val: bytes) -> dict:
    """protobuf map<string,string> 字节 → dict (map entry: f1=key, f2=value)。"""
    kv = {}
    key = None
    i = 0
    while i < len(val):
        tag, j = _pb_varint(val, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, k = _pb_varint(val, j)
            v = val[k:k + ln]
            i = k + ln
            if fno == 1:
                key = v.decode("utf-8", errors="replace")
            elif fno == 2 and key:
                kv[key] = v.decode("utf-8", errors="replace")
                key = None
        elif wt == 0:
            _, i = _pb_varint(val, j)
        else:
            break
    return kv


def _parse_gen_metadata_row(data):
    """gen_metadata.data protobuf → (tokens_in, tokens_out, cache_read, model, step_idx)
    或 None。官方精确 token 记账 (2026-11 真机 61 原生库 5,218 行校准, 覆盖 99.3%):
    行内可有多个 field7 ':​(bot-…' 字符串 (首个是 bot-id, 无 usage) — 逐个出现位置尝试:
    rfind b"\\x12" 后从其 len 字节之后 walk 至锚点 (全 varint, 干净走完才采信),
    f2=未缓存输入 / f3=输出 / f5=缓存读 (proto3 零值省略: f2/f5 键缺省即 0, 以 f3 锚定);
    尾部 f21=可读模型名 (如 'Gemini 4 Neon (Medium)'), f19=模型 id 兜底
    (如 'gemini-4-neon'), f20=KV 映射 (last_step_index 关联 steps 时间戳)。"""
    if not data:
        return None
    usage = None
    anchor = None
    pos = data.find(b":(bot-")
    while pos >= 0:
        chunk = data[max(0, pos - 40):pos]
        st = chunk.rfind(b"\x12")
        if st >= 0:
            buf = chunk[st + 2:]
            i = 0
            fields = {}
            good = True
            while i < len(buf):
                tag, j = _pb_varint(buf, i)
                if tag & 7 != 0:
                    good = False
                    break
                v, i = _pb_varint(buf, j)
                fields[tag >> 3] = v
            if good and 3 in fields:  # tokens_out 锚定 (f2/f5 允许零值省略)
                usage = (int(fields.get(2, 0)), int(fields[3]), int(fields.get(5, 0)))
                anchor = pos
                break
        pos = data.find(b":(bot-", pos + 1)
    if usage is None:
        return None
    model = mid = None
    step_idx = None
    i = anchor
    n = len(data)
    while i < n:
        tag, j = _pb_varint(data, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, k = _pb_varint(data, j)
            val = data[k:k + ln]
            i = k + ln
            if fno == 21 and model is None:
                try:
                    model = val.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            elif fno == 19 and mid is None:
                mid = val.decode("utf-8", errors="replace")
            elif fno == 20 and step_idx is None:
                kv = _pb_kv_map(val)
                try:
                    step_idx = int(kv["last_step_index"])
                except (KeyError, ValueError):
                    pass
        elif wt == 0:
            _, i = _pb_varint(data, j)
        elif wt == 5:
            i = j + 4
        elif wt == 1:
            i = j + 8
        else:
            break
    if not model:
        model = mid
    if not model:
        return None
    return usage[0], usage[1], usage[2], model, step_idx


def agv_workspaces():
    """Antigravity 摘要索引 (conversation_summaries.db, 只读) → {conv_id: 工作区路径}。
    项目归因主源; 失败/缺库返回 {} (诚实留空, 绝不编造)。"""
    p = Path(_cv().HOME) / ".gemini" / "antigravity" / "conversation_summaries.db"
    if not p.exists():
        return {}
    out = {}
    try:
        sdb = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            cols = [r[1] for r in sdb.execute(
                "PRAGMA table_info(conversation_summaries)").fetchall()]
            if "workspace_uris" not in cols:
                return {}
            for cid, wu in sdb.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries"):
                try:
                    ws = json.loads(wu) if wu else []
                except ValueError:
                    continue
                for u in ws if isinstance(ws, list) else []:
                    if isinstance(u, str) and u.startswith("file://"):
                        path = unquote(u[len("file://"):])
                        if path.startswith("/") and len(path) > 2 and path[2] == ":":
                            path = path[1:]  # /c:/x → c:/x (Windows file URI 三斜线)
                        out[str(cid)] = path
                        break
        finally:
            sdb.close()
    except sqlite3.Error:
        return out
    return out


def collect_antigravity(base_dir=None, skip_fn=None, agv_state=None):
    """~/.gemini/antigravity/conversations/<uuid>.db (SQLite, 内容为 protobuf blob)。
    记账主路径 (2026-11 真机逆向): gen_metadata.data 官方精确 token
    (tokens_in/tokens_out/cache_read + 可读模型名 + last_step_index 关联 steps 时间戳),
    is_estimated=0 — 字节估算在此类库上整体让位 (否则双重计费)。
    回退路径 (gen_metadata 缺失/零有效行, 如 smigrate 导出库):
      1) steps.metadata 步级时间戳 → 每步字节估算事件 (is_estimated=1);
      2) 无任何步级时间戳的老库 → 差量记账: 存量基准事件 agv|<conv>|base 锚在最早已知
         时间 (agv_state 提供快照/基准锚点, 兜底文件创建时间), 体积增长只对滚动增量事件
         agv|<conv>|delta 按 mtime 记账, 存量不再重复入窗口。
    agv_state: {conv_id: {"base_bytes": 已入库基准 blob 字节, "anchor": 最早锚点 epoch}}
    (由 sync 层从 usage_events 基准行 + snapshots 快照时间合成); 缺省 = 首次观测。
    项目归因: agv_workspaces() (conversation_summaries workspace_uris)。
    水位含 .db-wal; skip_fn(path, sig) 为真时不读盘跳过。"""
    l = _cl()
    root = Path(base_dir) if base_dir else Path(_cv().HOME) / ".gemini" / "antigravity" / "conversations"
    if not root.is_dir():
        return [], []
    ws_map = l.agv_workspaces()
    events, marks = [], []
    for f in sorted(root.glob("*.db")):
        try:
            sig = _file_sig(f, with_wal=True)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        conv_id = f.stem
        project = ws_map.get(conv_id)
        try:
            adb = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                cols = [r[1] for r in adb.execute("PRAGMA table_info(steps)").fetchall()]
                content_col = next((c for c in _AGV_CONTENT_COLS if c in cols), None)
                if not cols or content_col is None:
                    continue  # 无 steps 内容列: 非会话库, 跳过 (finally 兜底关连接)
                ts_col = next((c for c in _AGV_TS_COLS if c in cols), None)
                meta_expr = "metadata" if "metadata" in cols else "NULL"
                sel = f"SELECT idx, {meta_expr}, COALESCE(LENGTH({content_col}),0)"
                if ts_col:
                    sel += f", {ts_col}"
                step_rows = adb.execute(sel + " FROM steps ORDER BY idx").fetchall()
                gen_rows = []
                try:  # gen_metadata 精确记账主路径 (官方逐调用 token)
                    gen_rows = adb.execute("SELECT idx, data FROM gen_metadata").fetchall()
                except sqlite3.Error:
                    gen_rows = []
            finally:
                adb.close()
        except sqlite3.Error:
            continue  # 活跃库锁/损坏: 跳过, 水位未推下轮重试
        blob_total = 0
        step_evs = []  # (idx, epoch, bytes) 有步级时间戳的步骤
        step_ts = {}   # {int(idx): epoch} — gen_metadata 事件时间关联用
        leftover = 0
        for row in step_rows:
            idx, meta, blob_n = row[0], row[1], int(row[2] or 0)
            ts_col_v = row[3] if len(row) > 3 else None
            blob_total += blob_n
            # 时间戳优先级: metadata protobuf (原生同构) > 显式时间戳列 (旧布局)
            ts = _meta_step_ts(meta) or l.ts_to_epoch(ts_col_v)
            if ts:
                step_evs.append((idx, ts, blob_n))
                step_ts[int(idx) if str(idx).lstrip("-").isdigit() else -1] = ts
            else:
                leftover += blob_n
        gen_events = []
        if gen_rows:
            ts_keys = sorted(step_ts)
            for gidx, gdata in gen_rows:
                parsed = _parse_gen_metadata_row(bytes(gdata) if gdata else None)
                if not parsed:
                    continue  # 个别行解析不出 (0.7% 长尾) — 诚实跳过该行
                tin, tout, cr, model, sidx = parsed
                ep = sig[0]
                if sidx is not None and ts_keys:
                    k = bisect.bisect_right(ts_keys, sidx)
                    if k:  # 该步无时间戳时取 <= last_step_index 的最近一步
                        ep = step_ts[ts_keys[k - 1]]
                gen_events.append({
                    "event_key": l._sha(f"agv|{conv_id}|gm|{gidx}"),
                    "source": "antigravity", "client": "antigravity",
                    "model": model, "project": project,
                    "session_ref": conv_id, "occurred_at": ep,
                    "tokens_in": max(0, tin), "tokens_out": max(0, tout),
                    "reasoning_tokens": 0, "cache_read": max(0, cr), "cache_write": 0,
                    "is_estimated": 0,
                    "raw": json.dumps({"gen_meta_idx": str(gidx),
                                       "step_idx": str(sidx) if sidx is not None else None,
                                       "estimate_basis": "gen_metadata_exact"},
                                      ensure_ascii=False)})
        if gen_events:
            events.extend(gen_events)  # 精确记账: 字节估算整体让位 (否则双重计费)
            marks.append((f, sig))
            continue
        if step_evs:
            for idx, ep, blob_n in step_evs:
                events.append({
                    "event_key": l._sha(f"agv|{conv_id}|{idx}"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id, "occurred_at": ep,
                    "tokens_in": max(1, blob_n // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1,
                    "raw": json.dumps({"step_idx": str(idx),
                                       "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
            if leftover:  # 无时间戳步骤的字节并一条补遗, 锚在会话最早步 (历史性, 不进当前窗口)
                events.append({
                    "event_key": l._sha(f"agv|{conv_id}|base"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id, "occurred_at": step_evs[0][1],
                    "tokens_in": max(1, leftover // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1, "upsert": True,
                    "raw": json.dumps({"step_idx": "base", "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
        elif blob_total:
            # 老库兜底: 差量记账 (存量基准 + 滚动增量), 绝不把全量按 mtime 灌入当前窗口
            st = (agv_state or {}).get(conv_id) or {}
            base_bytes = int(st.get("base_bytes") or 0)
            anchor = st.get("anchor")
            if not anchor:
                try:
                    anchor = f.stat().st_ctime  # Windows=创建时间; POSIX=变更时间, 皆早于 now
                except OSError:
                    anchor = sig[0]
            if base_bytes and base_bytes <= blob_total:
                delta = blob_total - base_bytes
                if delta > 0:
                    events.append({
                        "event_key": l._sha(f"agv|{conv_id}|delta"),
                        "source": "antigravity", "client": "antigravity",
                        "model": AGV_EST_MODEL, "project": project,
                        "session_ref": conv_id, "occurred_at": sig[0],
                        "tokens_in": max(1, delta // 4), "tokens_out": 0,
                        "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                        "is_estimated": 1, "upsert": True,
                        "raw": json.dumps({"step_idx": "delta",
                                           "content_col": content_col,
                                           "estimate_basis": "blob_bytes/4"},
                                          ensure_ascii=False)})
            else:  # 首次观测: 全量作为存量基准, 锚在最早已知时间 (快照/创建时间)
                events.append({
                    "event_key": l._sha(f"agv|{conv_id}|base"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id,
                    "occurred_at": min(anchor, sig[0]) if anchor else sig[0],
                    "tokens_in": max(1, blob_total // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1,
                    "raw": json.dumps({"step_idx": "base", "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
        marks.append((f, sig))
    return events, marks


def collect_dsh(base_dir=None, skip_fn=None):
    """~/.dsh/sessions/**/*.jsonl[.zstd] — assistant/message usage (inputTokens 不含缓存)。
    同文件内按 message.id 去重取 tokens_out 最大的一条; skip_fn(path, sig) 未变更直接跳过。"""
    import cv_dsh
    l = _cl()
    root = Path(base_dir) if base_dir else cv_dsh.dsh_sessions_root()
    files = cv_dsh.discover_dsh_session_files(root)
    if not files:
        return [], []
    events, marks = [], []
    for f in files:
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        sid = None
        cwd = None
        header_ts = 0.0
        groups = {}
        try:
            for obj in cv_dsh.iter_dsh_jsonl(f):
                t = obj.get("type")
                if t == "session":
                    sid = obj.get("id") or sid
                    cwd = obj.get("cwd") or cwd
                    cat = obj.get("createdAt")
                    if isinstance(cat, (int, float)) and not isinstance(cat, bool) and cat >= 0:
                        header_ts = float(cat) / 1000.0
                    continue
                if t != "assistant/message":
                    continue
                data = obj.get("data") or {}
                if not isinstance(data, dict):
                    continue
                msg = data.get("message") or {}
                u = data.get("usage")
                if not isinstance(msg, dict) or not isinstance(u, dict):
                    continue
                if "inputTokens" not in u and "outputTokens" not in u:
                    continue
                sess_eff = str(sid or f.parent.name)
                gkey = str(msg.get("id") or f"seq:{obj.get('seq')}")
                if not gkey:
                    continue
                tin, tout, cr, cw, rt = cv_dsh._dsh_usage_normalized(u)
                prev = groups.get(gkey)
                if prev is not None and tout <= prev["tokens_out"]:
                    continue
                ms = obj.get("time")
                if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms < 0:
                    ms = cv_dsh._stream_first_ms(data.get("stream"))
                occ = (
                    float(ms) / 1000.0
                    if (isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0)
                    else header_ts
                )
                src = msg.get("source") or {}
                groups[gkey] = {
                    "event_key": l._sha(f"dsh|{sess_eff}|{gkey}"),
                    "source": "dsh",
                    "client": "dsh",
                    "model": (src.get("model") if isinstance(src, dict) else None) or None,
                    "project": cwd,
                    "session_ref": sess_eff,
                    "occurred_at": occ,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "reasoning_tokens": rt,
                    "cache_read": cr,
                    "cache_write": cw,
                    "is_estimated": 0,
                    "raw": json.dumps({
                        "message_id": msg.get("id"),
                        "provider": src.get("provider") if isinstance(src, dict) else None,
                        "cwd": cwd,
                    }, ensure_ascii=False),
                }
        except (OSError, ValueError, RuntimeError):
            continue
        events.extend(groups.values())
        marks.append((f, sig))
    return events, marks


COLLECTORS = {"claude": collect_claude, "codex": collect_codex,
              "zcode": collect_zcode, "antigravity": collect_antigravity,
              "dsh": collect_dsh}


def sync_from_agm(vault: Path, url: str, conn=None, pricing=None, accounts=None):
    """可选外部管理器快照 (如本机 Antigravity-Manager): 尽力而为, 失败静默跳过, 绝不强依赖。
    认形状: {"events":[{id,account,model,occurred_at,tokens_in,tokens_out,cache_read,cache_write}]}。
    pricing/accounts 缺省时按 vault 当前配置加载 — agm 事件同样参与账号归因与费用估算。"""
    l = _cl()
    try:
        with l.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"skipped": f"agm 不可达/超时: {e}"}
    evs = data.get("events") if isinstance(data, dict) else None
    if not isinstance(evs, list):
        return {"skipped": "agm 返回形状未识别 (缺 events 数组)"}
    if pricing is None:
        pricing = l.load_pricing(vault)
    if accounts is None:
        accounts = l.load_accounts(vault)
    own_conn = conn is None
    if own_conn:
        conn = _cv().db(vault)
        ensure_schema(conn)
    n = 0
    for e in evs:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or l._sha(json.dumps(e, sort_keys=True, ensure_ascii=False,
                                                   default=str)))
        ep = l.ts_to_epoch(e.get("occurred_at") or e.get("timestamp"))
        if ep is None:
            continue
        ev = {"event_key": l._sha(f"agm|{eid}"), "source": "agm",
              "client": e.get("client") or "agm", "model": e.get("model"),
              "project": e.get("project"), "session_ref": e.get("session_ref"),
              "occurred_at": ep,
              "tokens_in": max(0, int(l._num(e.get("tokens_in")))),
              "tokens_out": max(0, int(l._num(e.get("tokens_out")))),
              "reasoning_tokens": max(0, int(l._num(e.get("reasoning_tokens")))),
              "cache_read": max(0, int(l._num(e.get("cache_read")))),
              "cache_write": max(0, int(l._num(e.get("cache_write")))),
              "is_estimated": 1 if e.get("is_estimated") else 0,
              "raw": json.dumps({"agm_url": url}, ensure_ascii=False)}
        n += _insert_events(conn, [ev], pricing, accounts)[0]
    if own_conn:
        conn.commit()
        conn.close()
    return {"ingested": n}


# ---------------------------------------------------------------- 入账与派生列

def _insert_events(conn, events: list, pricing: dict, accounts: dict) -> tuple:
    """event_key 唯一入账 + 派生列 (account/cost/price_known) 现算。
    默认 INSERT OR IGNORE (幂等); e["upsert"]=True 的会话级估算事件改 ON CONFLICT DO UPDATE —
    文件变大时刷新体积/时间戳/费用, 不被 OR IGNORE 冻结在首次估算值。"""
    l = _cl()
    inserted = 0
    for e in events:
        rule, _ = l.price_for(pricing, e.get("model"))
        cost = l.cost_of(rule, e["tokens_in"], e["tokens_out"], e["cache_read"], e["cache_write"])
        acct = l.resolve_account(accounts, e.get("source"), e.get("client"), e.get("model"))
        args = (e["event_key"], e["source"], e.get("client"), acct, e.get("model"),
                e.get("project"), e.get("session_ref"), e["occurred_at"], e["tokens_in"],
                e["tokens_out"], e.get("reasoning_tokens") or 0, e["cache_read"],
                e["cache_write"], cost, 1 if e.get("is_estimated") else 0,
                1 if rule else 0, e.get("raw"))
        if e.get("upsert"):
            cur = conn.execute(
                "INSERT INTO usage_events(event_key,source,client,account,model,project,"
                "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,"
                "cache_write,cost_est_usd,is_estimated,price_known,raw)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_key) DO UPDATE SET occurred_at=excluded.occurred_at,"
                " tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out,"
                " cost_est_usd=excluded.cost_est_usd, raw=excluded.raw", args)
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO usage_events(event_key,source,client,account,model,project,"
                "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,"
                "cost_est_usd,is_estimated,price_known,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                args)
        if cur.rowcount:
            inserted += 1
    return inserted, len(events) - inserted


def recompute_derived(conn, pricing: dict, accounts: dict) -> int:
    """按当前 pricing/accounts 重算全部存量事件的 account/cost_est_usd/price_known (修正3)。"""
    l = _cl()
    rows = conn.execute("SELECT id,source,client,model,tokens_in,tokens_out,cache_read,cache_write"
                        " FROM usage_events").fetchall()
    n = 0
    for r in rows:
        rule, _ = l.price_for(pricing, r["model"])
        cost = l.cost_of(rule, r["tokens_in"], r["tokens_out"], r["cache_read"], r["cache_write"])
        acct = l.resolve_account(accounts, r["source"], r["client"], r["model"])
        conn.execute("UPDATE usage_events SET account=?, cost_est_usd=?, price_known=? WHERE id=?",
                     (acct, cost, 1 if rule else 0, r["id"]))
        n += 1
    return n


def _cfg_fingerprint(vault: Path) -> str:
    parts = [f"builtin:{_cl().PRICING_REV}"]  # 内置表变更 (含订阅模型补价) 也触发存量重算
    for name in ("pricing.json", "prices.json", "accounts.json"):
        p = vault / name
        if p.exists():
            try:
                st = p.stat()
                parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                pass
    return "|".join(parts) or "none"


def _maybe_recompute(conn, vault: Path, pricing: dict, accounts: dict, force: bool) -> int:
    l = _cl()
    fp = _cfg_fingerprint(vault)
    try:
        row = conn.execute("SELECT value FROM vault_meta WHERE key=?", (l.CFG_FP_KEY,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not force and row:
        if row[0] == fp:
            return 0
        # 防旧驻留进程降级覆盖: 若库内记录的 builtin:<rev> 高于当前进程, 不反向抹除新模型计价
        m_db = re.match(r"^builtin:(\d+)", str(row[0]))
        if m_db and int(m_db.group(1)) > l.PRICING_REV and fp.split("|")[1:] == str(row[0]).split("|")[1:]:
            return 0
    n = recompute_derived(conn, pricing, accounts)
    conn.execute("INSERT INTO vault_meta(key,value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (l.CFG_FP_KEY, fp))
    return n


def ensure_schema(conn):
    conn.executescript(_cl().USAGE_TABLE)


# ------------------------------------------------- 升级迁移 (vault_meta 旗标, 一次性)

def _meta_flag(conn, key: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM vault_meta WHERE key=?", (key,)).fetchone())
    except sqlite3.OperationalError:
        return False


def _set_meta_flag(conn, key: str):
    conn.execute("INSERT INTO vault_meta(key,value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _cv().now_iso()))


def agv_perstep_migration(conn) -> int:
    """一次性: AGV 记账升级为步级时间戳/差量记账 — 删除旧「会话级全量按 mtime」事件
    (event_key 是 sha1 hex 无结构差异, 以 raw 的 step_idx=conv 判别), 防止老会话历史体积
    继续污染近 5h 窗口 (新事件由采集器按真实时间重出)。
    同时清 AGV 水位: 旧水位会把全部 .db 判为未变更 → 步级事件补不进来。"""
    if _meta_flag(conn, "agv_perstep_v1"):
        return 0
    n = conn.execute("DELETE FROM usage_events WHERE source='antigravity'"
                     " AND raw LIKE '%\"step_idx\": \"conv\"%'").rowcount
    conn.execute("DELETE FROM usage_sync_state WHERE source='antigravity'")
    _set_meta_flag(conn, "agv_perstep_v1")
    return n


def agv_gen_metadata_migration(conn) -> int:
    """一次性: AGV 记账升级为 gen_metadata 官方精确 token (此前 LENGTH(step_payload)//4
    字节估算漏掉每轮 cache_read, 用量低估 ~75x 且模型糊成一行估算)。
    清空旧 antigravity 事件 + 水位, 下一轮 sync 全量重采 (精确主路径;
    gen_metadata 缺失的老库自动落字节估算兜底)。"""
    if _meta_flag(conn, "agv_gen_metadata_v1"):
        return 0
    n = conn.execute("DELETE FROM usage_events WHERE source='antigravity'").rowcount
    conn.execute("DELETE FROM usage_sync_state WHERE source='antigravity'")
    _set_meta_flag(conn, "agv_gen_metadata_v1")
    return n


def zcode_project_backfill(conn, vault: Path) -> int:
    """一次性: 存量 zcode 事件 project 回填 — 主库 session.directory 主源,
    vault snapshots (最新 cwd 优先) 兜底; 消除满屏 (未标注)。"""
    if _meta_flag(conn, "zcode_project_v1"):
        return 0
    proj = {}
    zdb_path = getattr(_cv(), "ZCODE_DB", None)
    if zdb_path and Path(zdb_path).exists():
        z = None
        try:
            z = sqlite3.connect(f"file:{Path(zdb_path).as_posix()}?mode=ro", uri=True, timeout=5)
            tabs = {r[0] for r in z.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" in tabs and "directory" in \
                    [r[1] for r in z.execute("PRAGMA table_info(session)").fetchall()]:
                for sid, d in z.execute(
                        "SELECT id, directory FROM session WHERE directory IS NOT NULL"):
                    if d:
                        proj[str(sid)] = str(d)
        except sqlite3.Error:
            pass
        finally:
            if z:
                z.close()
    proj.update(_snapshot_proj_map(conn))
    n = 0
    try:
        refs = conn.execute("SELECT DISTINCT session_ref FROM usage_events"
                            " WHERE source='zcode' AND session_ref IS NOT NULL"
                            " AND (project IS NULL OR project='')").fetchall()
    except sqlite3.OperationalError:
        refs = []
    for (ref,) in refs:
        d = proj.get(ref)
        if d:
            n += conn.execute("UPDATE usage_events SET project=?"
                              " WHERE source='zcode' AND session_ref=?", (d, ref)).rowcount
    _set_meta_flag(conn, "zcode_project_v1")
    return n


def _snapshot_proj_map(conn) -> dict:
    """vault snapshots → {session_ref: cwd}; 同会话多版本时取最新快照的 cwd。"""
    try:
        rows = conn.execute("SELECT session_ref, cwd FROM snapshots"
                            " WHERE cwd IS NOT NULL AND session_ref IS NOT NULL"
                            " ORDER BY created_at DESC").fetchall()
    except sqlite3.OperationalError:
        return {}
    m = {}
    for r in rows:
        m.setdefault(r["session_ref"], r["cwd"])
    return m


def _agv_base_state(conn) -> dict:
    """存量 AGV 基准事件 → {conv_id: {base_bytes, anchor}} (差量记账状态)。
    另合入 snapshots 里 antigravity 会话的最早 created_at 作首观测锚点 (agy-src- 前缀归一)。"""
    l = _cl()
    st = {}
    try:
        rows = conn.execute("SELECT session_ref, tokens_in, occurred_at FROM usage_events"
                            " WHERE source='antigravity' AND event_key LIKE '%|base'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        st[r["session_ref"]] = {"base_bytes": int(r["tokens_in"] or 0) * 4,
                                "anchor": r["occurred_at"]}
    try:
        snaps = conn.execute("SELECT session_ref, created_at FROM snapshots"
                             " WHERE agent LIKE 'antigravity%' AND created_at IS NOT NULL"
                             " ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:
        snaps = []
    for r in snaps:
        ep = l.ts_to_epoch(r["created_at"])
        key = r["session_ref"] or ""
        if key.startswith("agy-src-"):
            key = key[len("agy-src-"):]
        if not ep or not key:
            continue
        cur = st.get(key)
        if not cur:
            st[key] = {"base_bytes": 0, "anchor": ep}
        elif not cur.get("anchor") or ep < cur["anchor"]:
            cur["anchor"] = ep
    return st


def sync_usage_events(vault: Path, *, sources=None, from_agm: str | None = None, recompute: bool = False) -> dict:
    """多端直采入账纯数据 API (幂等; 供 CLI / HTTP / 托盘 / setup 共用, 不写 stdout)。"""
    c = _cv()
    l = _cl()
    collectors = l.COLLECTORS
    if not (vault / "vault.db").exists():
        raise FileNotFoundError("vault 尚无数据库 — 先运行 cv init 或 cv sync 建库 (usage_events 随库建表)")
    want = sources or "all"
    if isinstance(want, (list, tuple, set)):
        srcs = [str(s).strip() for s in want if str(s).strip()]
    elif want == "all":
        srcs = list(collectors)
    else:
        srcs = [s.strip() for s in str(want).split(",") if s.strip()]
    bad = [s for s in srcs if s not in collectors]
    if bad:
        raise ValueError(f"未知来源: {','.join(bad)} (可选: all|claude|codex|zcode|antigravity|dsh)")

    logs: list[str] = []
    files_scanned = 0
    conn = c.db(vault)
    try:
        ensure_schema(conn)
        try:  # vault_meta 同库兜底 (老库可能缺)
            conn.executescript(c.SCHEMA)
        except sqlite3.OperationalError:
            pass
        pricing = l.load_pricing(vault)
        accounts = l.load_accounts(vault)
        # 主库升为 zcode 真相源的一次性迁移: model-io 零头事件与 zdb 全量重叠, 不删会双计
        zdb_active = bool(getattr(c, "ZCODE_DB", None)) and Path(c.ZCODE_DB).exists()
        try:
            mig = conn.execute("SELECT value FROM vault_meta WHERE key='zcode_zdb_primary_v1'").fetchone()
        except sqlite3.OperationalError:
            mig = None
        if zdb_active and not mig:
            n_del = conn.execute("DELETE FROM usage_events WHERE source='zcode'"
                                 " AND raw LIKE '%\"requestId\"%'").rowcount
            _set_meta_flag(conn, "zcode_zdb_primary_v1")
            if n_del:
                logs.append(f"  迁移: zcode 真相源升级为主库全量, 移除 model-io 零头事件 {n_del} 条 (防双计)")
        if "antigravity" in srcs:
            n_agv = agv_perstep_migration(conn)
            if n_agv:
                logs.append(f"  迁移: AGV 记账升级为步级时间戳/差量, 移除旧会话级全量事件 {n_agv} 条"
                            " (防历史体积污染 5h 窗口)")
            n_gm = agv_gen_metadata_migration(conn)
            if n_gm:
                logs.append(f"  迁移: AGV 记账升级为 gen_metadata 官方精确 token, 清旧估算事件 {n_gm} 条"
                            " (含缓存读全量重采)")
        if "zcode" in srcs:
            n_proj = zcode_project_backfill(conn, vault)
            if n_proj:
                logs.append(f"  迁移: zcode 存量事件项目回填 {n_proj} 条 (session.directory/snapshots)")
        agv_state = l._agv_base_state(conn) if "antigravity" in srcs else {}
        proj_map = _snapshot_proj_map(conn) if "zcode" in srcs else {}
        total_new = total_dup = 0
        for src in srcs:
            changed = []  # 真变更文件集: skip_fn 判未变更 → 采集器不读盘 (水位跳过)

            def _skip(p, s, _src=src):
                if _wm_changed(conn, _src, p, s):
                    changed.append((p, s))
                    return False
                return True

            if src == "antigravity":
                events, marks = collectors["antigravity"](skip_fn=_skip, agv_state=agv_state)
            elif src == "zcode":
                events, marks = collectors["zcode"](skip_fn=_skip, proj_map=proj_map)
            else:
                events, marks = collectors[src](skip_fn=_skip)
            ins, dup = _insert_events(conn, events, pricing, accounts)
            for f, sig in marks:
                _wm_update(conn, src, f, sig)
            total_new += ins
            total_dup += dup
            files_scanned += len(marks)
            logs.append(f"  {src:<11} 新 {ins:>5} · 幂等跳过 {dup:>5} · 文件 {len(marks)}"
                        f" (变更 {len(changed)} · 跳过 {len(marks) - len(changed)})")
        agm_res = None
        if from_agm:
            agm_res = l.sync_from_agm(vault, from_agm, conn=conn, pricing=pricing, accounts=accounts)
        re_n = _maybe_recompute(conn, vault, pricing, accounts, force=bool(recompute))
        conn.commit()
        total_events = int(conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])
    finally:
        conn.close()
    summary = (f"账本同步完成: 新入账 {total_new} 条, 幂等跳过 {total_dup} 条"
               + (f", 配置变更重算 {re_n} 条" if re_n else "")
               + (f", agm: {json.dumps(agm_res, ensure_ascii=False)}" if agm_res else ""))
    logs.append(summary)
    return {
        "ok": True,
        "added": total_new,
        "skipped": total_dup,
        "files_scanned": files_scanned,
        "total_events": total_events,
        "recomputed": re_n,
        "agm": agm_res,
        "message": summary,
        "logs": logs,
    }


def cmd_ledger_sync(args):
    """cv ledger sync — 多端直采入账 (幂等; --sources 过滤; --from-agm 可选; --recompute 重算)。"""
    vault = _cv().vault_dir(args)
    try:
        res = sync_usage_events(
            vault,
            sources=getattr(args, "sources", None) or "all",
            from_agm=getattr(args, "from_agm", None),
            recompute=bool(getattr(args, "recompute", False)),
        )
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))
    for ln in res["logs"]:
        print(ln)
