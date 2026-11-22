"""Antigravity 原生 .db/Protobuf 双向适配、摘要索引登记、同步清理与 18 格式 harness 桥接 (smigrate)。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path


def _cv():
    import cv
    return cv


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
    conn = _cv().db(vault)
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
        return _cv()._norm_win_path(p)
    return _cv()._norm_win_path(u)


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
    proj_dir = _cv().HOME / ".gemini" / "config" / "projects"
    if not proj_dir.exists():
        return [], None

    migrated_cwds = {
        _cv()._norm_win_path(
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
        if project and not _cv()._norm_project_match(cwd_repr, project):
            continue
        empty.append({"id": pid, "name": pname, "path": str(jf), "cwd": cwd_repr})
    if project and empty:
        empty = _cv()._prefer_exact_project(empty, project)
    return empty, None


def sync_antigravity_archived(vault: Path, project: str | None = None,
                              apply: bool = False, dedup: bool = False,
                              only_agent: str | None = None) -> dict:
    """移除已导入 Antigravity 的 ZCode 已归档/已删除会话（含 .db、摘要索引与 agyhub_summaries_proto.pb），
    并清理已无任何剩余会话的迁移空项目配置；可选 --dedup 去重、--only-agent 清理非目标来源。"""
    arch_refs = _cv()._zcode_archived_refs()
    mapped = _collect_antigravity_migrated_dbs(vault)
    keep_agents = set(_cv()._parse_agent_filter(only_agent))
    conv_dir = _cv().HOME / ".gemini" / "antigravity" / "conversations"
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
                det_cid = _cv()._deterministic_session_id({"capture": {"agent": gkey[0], "session_ref": gkey[1]}}).lower()
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
        if project and not _cv()._norm_project_match(entry["cwd"], project):
            continue
        filtered.append(entry)
    if project and filtered:
        filtered = _cv()._prefer_exact_project(filtered, project)

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
                raise _cv().BadMigrateTarget(
                    f"无法清理 Antigravity 摘要库 ({del_err})；请先完全关闭 Antigravity 后重试 --apply"
                )
            cleaned_proto, pdel_err = _antigravity_proto_prune(proto_pb, set(target_cids), delete=True)
            if pdel_err:
                raise _cv().BadMigrateTarget(
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
            raise _cv().BadMigrateTarget(
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
        raise _cv().BadMigrateTarget(
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
    conv_dir = _cv().HOME / ".gemini" / "antigravity" / "conversations"
    summary_db = conv_dir.parent / "conversation_summaries.db"
    eff_agent = None if str(agent or "").strip().lower() in ("", "all") else agent
    allowed_agents = set(_cv()._parse_agent_filter(eff_agent))
    arch_refs = set() if include_archived else _cv()._zcode_archived_refs()
    sub_refs = _cv()._zcode_subagent_refs()

    if project:
        items = _cv()._resolve_project_items(vault, project, limit=limit,
                                       include_archived=include_archived, agent=eff_agent)
    else:
        items = _cv().resolve_top_conversations(vault, agent=eff_agent, include_archived=include_archived, limit=limit)
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
            env = _cv().load_snapshot(vault, sid)
        except (ValueError, OSError, _cv().SnapshotNotFound):
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
        cascade = _cv()._deterministic_session_id(env).lower()
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
        key = _cv()._norm_win_path(p["cwd"]) or "(无项目)"
        by_proj[key] = by_proj.get(key, 0) + 1
    proj_summary = [{"cwd": k, "count": n} for k, n in sorted(by_proj.items(), key=lambda x: (-x[1], x[0]))]

    imported = 0
    errors: list[dict] = []
    if apply and plan:
        if not _cv().find_smigrate():
            raise _cv().BadMigrateTarget("未找到 smigrate: uv tool install session-migrate")
        _probe_antigravity_summary_writable(summary_db, "写入 Antigravity 摘要库")
        for p in plan:
            try:
                _cv().do_import(vault, p["sid"], "antigravity", apply=True, session_id=p["cascade"])
                imported += 1
            except (_cv().BadMigrateTarget, _cv().SnapshotNotFound, SystemExit, Exception) as exc:
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
    vault = _cv().vault_dir(args)
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
        if os.name == "nt":
            # Windows: cmd /c where 探 PATH (smigrate 是 uv tool 外部命令, 常不在裸 Path 里)
            found = _cv().subprocess.run(["cmd", "/c", "where", cand],
                                   capture_output=True, creationflags=_cv().NO_WINDOW).returncode == 0
        else:
            # POSIX: 没有 cmd, 曾直接 FileNotFoundError 崩 (CI ubuntu job 的 doctor 实证) —
            # shutil.which 等价覆盖 PATH 探测
            found = shutil.which(cand) is not None
        if found or Path(cand).exists():
            return cand
    return None


def _deterministic_ingest_id(src_path: Path) -> str:
    """同一源文件内容恒定派生 uuid4 格式 session id (喂给 smigrate --session-id)。
    smigrate 对无内禀会话 id 的源会随机生成 → 同文件重复 ingest 的 session_ref 每次不同,
    内容寻址 sid 随之漂移, 快照重复堆积。uuid4 版本位改写同 _deterministic_session_id。"""
    digest = hashlib.sha256(src_path.read_bytes()).hexdigest()
    b = bytearray(uuid.uuid5(uuid.NAMESPACE_URL, f"context-vault:ingest:{digest}").bytes)
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def cmd_ingest(args):
    """任意 harness 会话 → 快照。链路: smigrate convert --to claude → claude-projects 适配器。
    覆盖 smigrate 支持的 17+ harness (opencode/cursor/qwen/kimi/grok/antigravity/pi/omp/...)。"""
    src_path = Path(args.source).expanduser()
    if not src_path.exists():
        raise SystemExit(f"文件不存在: {src_path}")
    exe = _cv().find_smigrate()
    if not exe:
        raise SystemExit("未找到 smigrate。安装: uv tool install session-migrate (MIT, Python>=3.11 独立环境)")
    vault = _cv().vault_dir(args)
    (vault / "ingest").mkdir(parents=True, exist_ok=True)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)  # 曾只建 ingest: 全新 vault 落快照裸 FileNotFoundError
    tmp_out = vault / "ingest" / f"sm_{int(time.time())}_{uuid.uuid4().hex[:8]}.jsonl"
    det_ref = _deterministic_ingest_id(src_path)
    cmd = [exe, "convert", str(src_path), "--to", "claude", "--output", str(tmp_out),
           "--session-id", det_ref]  # 确定性 id: 同文件重 convert 产物一致 (对照 _export_one_harness)
    if args.format:
        cmd += ["--format", args.format]
    if args.cwd:
        cmd += ["--cwd", args.cwd]
    r = _cv().subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=_cv().NO_WINDOW)
    if r.returncode != 0 or not tmp_out.exists():
        raise SystemExit(f"smigrate convert 失败: {(r.stderr or r.stdout)[:400]}")
    exported = _cv().export_claude_code(tmp_out, args.drop_developer)
    origin = args.format or "harness"
    exported["capture"]["source_format"] = f"via-smigrate:{origin}"
    exported["capture"]["agent"] = origin  # 归属原始 harness, 而非 claude 中转层
    exported["capture"]["origin_path"] = str(src_path)
    exported["capture"]["session_ref"] = det_ref  # 适配器拿 tmp 文件名 stem (sm_<ts>_<rand>)
    # 当 session_ref: 不固定则同文件重 ingest 的内容寻址 sid 每次漂移, 快照重复堆积
    envelope = _cv().build_envelope(exported)
    sid = _cv().snapshot_id(envelope)
    out = vault / "snapshots" / f"{sid}.json"
    conn = _cv().db(vault)
    conn.executescript(_cv().SCHEMA + _cv().SYNC_TABLE)  # 曾只建 SCHEMA: 全新 vault 上 register 直接
    if out.exists():                          # no such table 崩 (二轮 migrate-1)
        print(f"已存在 (幂等跳过): {out.name}")
        if conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone() is None:
            # 库里没这行 (如 vault.db 重建过): 用磁盘文件重注册 (cmd_export 同款)
            _cv().register_snapshot(conn, json.loads(out.read_text(encoding="utf-8")), sid, out)
        # 已在库则不重注册: INSERT OR REPLACE 会用 parent=None 的新 envelope 抹掉链上
        # parent_snapshot, 演进链断裂后同会话 usage 双计 (对齐 cmd_export 的 H1 守卫)
    else:
        _cv().atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        s = exported["stats"]
        print(f"已接入 {out.name}  源={exported['capture']['source_format']}  "
              f"块={s['blocks_total']}  入token={s['tokens_in']}")
        _cv().register_snapshot(conn, envelope, sid, out)
    conn.commit()
    if not args.keep_temp:
        tmp_out.unlink(missing_ok=True)

