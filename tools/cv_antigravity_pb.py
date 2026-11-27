"""Antigravity 原生 .db/Protobuf 编解码、metadata 标准化与摘要索引写回原语 (纯 Python 3.9+ stdlib)。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from cv_core import cv_mod as _cv


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


def _pb_str(fields: dict, fn: int) -> str:
    vals = fields.get((fn, 2))
    if not vals:
        return ""
    raw = vals[0]
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _pb_epoch_iso(meta_bytes: bytes | None) -> str:
    if not meta_bytes:
        return ""
    mf, _ = _pb_decode(bytes(meta_bytes))
    ts_raw = mf.get((1, 2))
    if not ts_raw:
        return ""
    tf, _ = _pb_decode(bytes(ts_raw[0]))
    secs = tf.get((1, 0))
    if not secs or not secs[0]:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(secs[0]), tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _agv_uri_to_cwd(uri: str) -> str:
    """将 file:///e:/context-vault 或 file:///e%3A/context-vault 规范化为本地路径。"""
    import urllib.parse
    s = (uri or "").strip()
    if not s:
        return ""
    if s.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        p = urllib.parse.unquote(parsed.path or "")
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1].upper() + p[2:]
            p = p.replace("/", "\\")
        return p
    return s


def export_antigravity_db(db_path: Path, drop_developer: bool = False) -> dict:
    """零依赖原生只读解析 Antigravity conversations/*.db (含 WAL 模式):
    直接解码 trajectory_metadata_blob + steps protobuf, 无需外部 smigrate,
    彻底规避 smigrate 遇空/二进制 generic 工具结果报错丢会话以及 cwd 失真问题。"""
    dbp = Path(db_path)
    cv_mod = _cv()
    stats = cv_mod.zero_stats()
    # 只读 URI 打开 (自动读取同目录 .db-wal, 永不修改 Antigravity 原库)
    conn = sqlite3.connect(f"file:{dbp.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "steps" not in tables:
            return {"capture": {"agent": "antigravity", "source_format": "antigravity-db",
                                "session_ref": dbp.stem, "cwd": "", "completeness": "empty"},
                    "stats": stats, "conversation": [], "created_at": cv_mod.now_iso()}

        cascade_id = dbp.stem
        if "trajectory_meta" in tables:
            row = conn.execute("SELECT cascade_id FROM trajectory_meta ORDER BY rowid LIMIT 1").fetchone()
            if row and row[0]:
                cascade_id = str(row[0])

        cwd = ""
        created_at = ""
        if "trajectory_metadata_blob" in tables:
            mrow = conn.execute("SELECT data FROM trajectory_metadata_blob ORDER BY rowid LIMIT 1").fetchone()
            if mrow and mrow[0]:
                mf, _ = _pb_decode(bytes(mrow[0]))
                ws_raw = mf.get((1, 2))
                if ws_raw:
                    wsf, _ = _pb_decode(bytes(ws_raw[0]))
                    cwd = _agv_uri_to_cwd(_pb_str(wsf, 1) or _pb_str(wsf, 2))
                if not cwd:
                    cwd = _agv_uri_to_cwd(_pb_str(mf, 7))
                ts_raw = mf.get((2, 2))
                if ts_raw:
                    tf, _ = _pb_decode(bytes(ts_raw[0]))
                    secs = tf.get((1, 0))
                    if secs and secs[0]:
                        try:
                            created_at = dt.datetime.fromtimestamp(
                                int(secs[0]), tz=dt.timezone.utc
                            ).astimezone().isoformat(timespec="seconds")
                        except (OSError, OverflowError, ValueError):
                            pass

        blocks: list[dict] = []
        has_assistant = False
        first_ts = ""
        last_ts = ""
        rows = conn.execute(
            "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY CAST(idx AS INTEGER)"
        ).fetchall()
        for _idx, stype, md, sp in rows:
            if not sp:
                continue
            ts = _pb_epoch_iso(md)
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts
            outer, _ = _pb_decode(bytes(sp))
            if stype == 14:  # User input
                u_raw = outer.get((19, 2))
                if not u_raw:
                    continue
                uf, _ = _pb_decode(bytes(u_raw[0]))
                txt = _pb_str(uf, 2).strip()
                if not txt:
                    sub3 = uf.get((3, 2))
                    if sub3:
                        sf3, _ = _pb_decode(bytes(sub3[0]))
                        txt = _pb_str(sf3, 1).strip()
                if txt:
                    blk = {"type": "text", "role": "user", "content": txt}
                    if ts:
                        blk["ts"] = ts
                    blocks.append(blk)
            elif stype == 15:  # Planner response (thinking + visible reply + tool calls)
                p_raw = outer.get((20, 2))
                if not p_raw:
                    continue
                pf, _ = _pb_decode(bytes(p_raw[0]))
                thought = _pb_str(pf, 3).strip()
                reply = (_pb_str(pf, 1) or _pb_str(pf, 8)).strip()
                if thought:
                    blk = {"type": "thinking", "summary": thought}
                    if ts:
                        blk["ts"] = ts
                    blocks.append(blk)
                if reply:
                    has_assistant = True
                    blk = {"type": "text", "role": "assistant", "content": reply}
                    if ts:
                        blk["ts"] = ts
                    blocks.append(blk)
                for tc_raw in pf.get((7, 2)) or []:
                    tcf, _ = _pb_decode(bytes(tc_raw))
                    cid = _pb_str(tcf, 1)
                    name = _pb_str(tcf, 2) or "tool"
                    args_str = _pb_str(tcf, 3)
                    try:
                        args_obj = json.loads(args_str) if args_str else {}
                    except ValueError:
                        args_obj = {"raw": args_str}
                    blk = {"type": "tool_call", "call_id": cid, "name": name, "args": args_obj}
                    if ts:
                        blk["ts"] = ts
                    blocks.append(blk)
            elif stype == 132:  # Generic tool result (空/二进制结果安全降级, 永不丢整会话)
                cid, tname = "", ""
                if md:
                    mf2, _ = _pb_decode(bytes(md))
                    f4 = mf2.get((4, 2))
                    if f4:
                        cf, _ = _pb_decode(bytes(f4[0]))
                        cid = _pb_str(cf, 1)
                        tname = _pb_str(cf, 2)
                out_txt = ""
                g_raw = outer.get((140, 2))
                if g_raw:
                    gf, _ = _pb_decode(bytes(g_raw[0]))
                    res_raw = gf.get((2, 2))
                    if res_raw:
                        rf, _ = _pb_decode(bytes(res_raw[0]))
                        out_txt = _pb_str(rf, 1)
                clipped, _trunc = cv_mod.clip(out_txt or "(empty/binary tool output)")
                blk = {"type": "tool_result", "call_id": cid, "name": tname or "tool", "output": clipped}
                if ts:
                    blk["ts"] = ts
                blocks.append(blk)
        stats["blocks_total"] = len(blocks)
        completeness = "full" if has_assistant else ("no_assistant_text" if blocks else "empty")
        return {
            "capture": {
                "agent": "antigravity",
                "source_format": "antigravity-db",
                "session_ref": cascade_id,
                "source_session_id": cascade_id,
                "originator": "antigravity",
                "cwd": cwd,
                "completeness": completeness,
            },
            "stats": stats,
            "conversation": blocks,
            "created_at": last_ts or created_at or first_ts or cv_mod.now_iso(),
        }
    finally:
        conn.close()


def _antigravity_read_cascade_id(db_path: Path) -> str:
    """从导出库读取 cascade_id (优先 trajectory_meta, 回退 blob field 6)。"""
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
    cfg_dir = _cv().HOME / ".gemini" / "config"
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
    _cv().atomic_write_text(proj_dir / f"{pid}.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return pid


def _antigravity_set_workspace(db_path: Path, ws_folder: str, last_epoch: int, cascade: str = "",
                               create_project: bool = True) -> bool:
    """标准化 trajectory_meta (source=1) 并将源项目目录、f3 UUID 与 project_id 写入
    trajectory_metadata_blob.main — 对齐原生结构，避免启动重整时清空元数据并归回底部未分组。"""
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
    ws_raw, enc = _antigravity_ws_uris(ws_folder)
    if ws_raw:
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
    """统一构造 Windows 与 POSIX 工作区的三斜杠 file:/// URI (原始形态 + URL 编码形态)。"""
    from urllib.parse import quote as _q
    posix = (cwd or "").strip().replace("\\", "/")
    if not posix:
        return "", ""
    if posix != "/":
        posix = posix.rstrip("/") or ("/" if posix.startswith("/") else "")
    if len(posix) >= 2 and posix[1] == ":":
        posix = posix[0].lower() + posix[1:]
    rel = posix.lstrip("/")
    return "file:///" + rel, "file:///" + _q(rel, safe="/")


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
    summary_db = _cv().HOME / ".gemini" / "antigravity" / "conversation_summaries.db"
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
    conn = sqlite3.connect(target_db, timeout=15)
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
    sconn = sqlite3.connect(summary_db, timeout=30)
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
    pb = _cv().HOME / ".gemini" / "antigravity" / "agyhub_summaries_proto.pb"
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
