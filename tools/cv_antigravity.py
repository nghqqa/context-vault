"""Antigravity 原生 .db/Protobuf 双向适配、摘要索引登记、同步清理与 18 格式 harness 桥接 (smigrate)。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from cv_core import cv_mod as _cv  # noqa: E402
from cv_antigravity_pb import (  # noqa: F401
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
    _pb_str,
    _pb_epoch_iso,
    _agv_uri_to_cwd,
    export_antigravity_db,
    _antigravity_read_cascade_id,
    _antigravity_resolve_project_id,
    _antigravity_set_workspace,
    _antigravity_ws_uris,
    _antigravity_first_user_text,
    _antigravity_raw_summary,
    _ts_msg,
    _antigravity_register_summaries,
    _antigravity_proto_upsert,
)


# ------------------------------------------------- ingest: 经 session-migrate 接入任意 harness

SMIGRATE_CANDIDATES = ("smigrate", os.path.expanduser("~/.local/bin/smigrate"))

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


def _is_eligible_antigravity_cap(
    cap: dict,
    allowed_agents: set[str],
    arch_refs: set[str],
    sub_refs: set[str],
) -> bool:
    """统一校验来源与归档/子代理守卫；常规批导与 --recover 共用，杜绝复活已归档或自回环会话。"""
    if not _antigravity_eligible_cwd(cap.get("cwd") or ""):
        return False
    ag = (cap.get("agent") or "").lower()
    if ag == "antigravity":
        return False
    if allowed_agents and ag not in allowed_agents:
        return False
    sref = cap.get("session_ref") or ""
    if ag == "zcode" and sref in (arch_refs | sub_refs):
        return False
    orig = str(cap.get("originator") or "")
    if cap.get("parent_session_ref") or orig.startswith("workflow subagent"):
        return False
    return True


def plan_antigravity_batch(
    vault: Path,
    *,
    conv_dir: Path | None = None,
    summary_db: Path | None = None,
    agent: str = "zcode",
    project: str | None = None,
    include_archived: bool = False,
    limit: int = 0,
    recover: bool = False,
) -> dict:
    """生成待导入 Antigravity 的幂等计划 (供 Web 控制台与 antigravity_batch CLI 共用)。"""
    conv_dir = Path(conv_dir) if conv_dir is not None else (_cv().HOME / ".gemini" / "antigravity" / "conversations")
    summary_db = Path(summary_db) if summary_db is not None else (conv_dir.parent / "conversation_summaries.db")
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
            sc = sqlite3.connect(f"file:{summary_db}?mode=ro", uri=True, timeout=15)
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
    done_cascades: dict[str, str] = {}
    mj = vault / "migrations.jsonl"
    if mj.exists():
        for line in mj.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            dest = o.get("dest") or ""
            if "antigravity" in dest and "conversations" in dest:
                done_cascades[o.get("source", "")] = Path(dest).stem.lower()
    for cid, ent in _collect_antigravity_migrated_dbs(vault).items():
        if ent["session_ref"] and (conv_dir / f"{cid}.db").exists() and cid.lower() in have_rows:
            existing_refs.add(((ent["agent"] or "").lower(), ent["session_ref"]))

    plan = []
    seen_cascades: set[str] = set()
    skipped_done = 0
    skipped_dup = 0

    def _consider(sid: str, fallback_cwd: str = "", fallback_title: str = "") -> None:
        nonlocal skipped_done, skipped_dup
        try:
            env = _cv().load_snapshot(vault, sid)
        except (ValueError, OSError, _cv().SnapshotNotFound):
            return
        cap = env.get("capture") or {}
        if not _is_eligible_antigravity_cap(cap, allowed_agents, arch_refs, sub_refs):
            return
        ag = (cap.get("agent") or "").lower()
        sref = cap.get("session_ref") or ""
        cascade = _cv()._deterministic_session_id(env).lower()
        prev_cid = done_cascades.get(sid)
        if ((conv_dir / f"{cascade}.db").exists() and cascade in have_rows) \
                or (recover and prev_cid and (conv_dir / f"{prev_cid}.db").exists() and prev_cid in have_rows) \
                or (sref and (ag, sref) in existing_refs):
            skipped_done += 1
            return
        fp = _antigravity_first_user_text(env).lower()[:80]
        if fp and fp in existing_fps:
            skipped_dup += 1
            return
        if cascade in seen_cascades:
            return
        seen_cascades.add(cascade)
        plan.append({
            "sid": sid,
            "agent": ag,
            "cwd": cap.get("cwd") or fallback_cwd,
            "title": str(cap.get("originator") or fallback_title or fp or sid),
            "cascade": cascade,
        })

    for it in items:
        _consider(it["id"], it.get("cwd") or "", it.get("originator") or "")

    if recover and done_cascades:
        conn_v = _cv().db(vault)
        try:
            srows = conn_v.execute("SELECT id, agent, session_ref, created_at FROM snapshots").fetchall()
            parents = {r["parent_snapshot"] for r in conn_v.execute(
                "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
        finally:
            conn_v.close()
        by_ref: dict[tuple, list] = {}
        for r in srows:
            if r["id"] not in parents:
                by_ref.setdefault((r["agent"] or "", r["session_ref"] or ""), []).append(r)
        for sid0 in done_cascades:
            try:
                env0 = _cv().load_snapshot(vault, sid0)
            except (ValueError, OSError, _cv().SnapshotNotFound):
                continue
            cap0 = env0.get("capture") or {}
            if not _is_eligible_antigravity_cap(cap0, allowed_agents, arch_refs, sub_refs):
                continue
            cands = by_ref.get((cap0.get("agent") or "", cap0.get("session_ref") or ""), [])
            leaf = max(cands, key=lambda r: r["created_at"] or "")["id"] if cands else sid0
            _consider(leaf)

    return {
        "plan": plan,
        "skipped_done": skipped_done,
        "skipped_dup": skipped_dup,
        "existing_fps_count": len(existing_fps),
        "summary_error": summary_error,
        "conv_dir": conv_dir,
        "summary_db": summary_db,
    }


def sync_to_antigravity_batch(vault: Path, agent: str = "zcode", project: str | None = None,
                              include_archived: bool = False, limit: int = 0,
                              apply: bool = False) -> dict:
    """差量同步活跃顶层主对话 → Antigravity（Web 控制台与批量同步共用，默认仅 preview）。"""
    planned = plan_antigravity_batch(
        vault, agent=agent, project=project, include_archived=include_archived, limit=limit, recover=False
    )
    plan = planned["plan"]
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
        _probe_antigravity_summary_writable(planned["summary_db"], "写入 Antigravity 摘要库")
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
        "skipped_done": planned["skipped_done"],
        "skipped_dup": planned["skipped_dup"],
        "projects": proj_summary,
        "items": plan[:50],
        "summary_error": planned["summary_error"],
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

