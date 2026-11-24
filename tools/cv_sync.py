"""Context Vault 多源会话增量扫描与 Sync 引擎 (纯 Python 3.9+ 标准库)。

由 tools/cv.py 拆出，集中管理 Codex / Claude / ZCode / Antigravity / DSH
会话扫描、版本限频 (_debounced)、增量快照落盘与 lineage 维护。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path

import cv_core

_cv = cv_core.cv_mod


def _debounced(prev_synced_at, src_mtime: float) -> bool:
    """托盘周期同步的版本限频: 源 10 分钟内仍有更新 (活跃) 且距上次入库不足 30 分钟 → 本轮不出
    版本, 水位不推, 下轮再判; 源空闲后立即捕获最终态。托盘 2 分钟一轮, 长会话每条消息全量复制
    一版曾致 4 天 +1603 版本 / 2.98GB。CV_SNAPSHOT_INTERVAL_MIN=0 关闭。"""
    c = _cv()
    if not prev_synced_at or c.SNAPSHOT_MIN_INTERVAL <= 0:
        return False
    now = time.time()
    if now - src_mtime >= c.SNAPSHOT_IDLE_SEC:
        return False
    try:
        last = dt.datetime.fromisoformat(prev_synced_at).timestamp()
    except (TypeError, ValueError):
        return False
    return now - last < c.SNAPSHOT_MIN_INTERVAL


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
    import cv_dsh
    c = _cv()
    roots = [("codex", Path(c.CODEX_SESSIONS).glob("*/*/*/rollout-*.jsonl")),
             ("claude-projects", (Path(c.HOME) / ".claude" / "projects").glob("*/*.jsonl")),
             ("claude-transcripts", (Path(c.HOME) / ".claude" / "transcripts").glob("*.jsonl")),
             ("dsh", cv_dsh.discover_dsh_session_files())]
    for r in (extra_roots or []):
        p = Path(r).expanduser()
        roots.append((f"extra:{p.name}", p.rglob("*.jsonl")))
        roots.append((f"extra-dsh:{p.name}", cv_dsh.discover_dsh_session_files(p)))
    files = []
    seen = set()
    for _, it in roots:
        for f in it:
            try:
                if f.is_file() and f.stat().st_size > 0:
                    k = _norm_path(f)
                    if k not in seen:
                        seen.add(k)
                        files.append(f)
            except OSError:
                continue
    return files


def sync_one(conn, vault: Path, path: Path, migrations=None, debounce=False) -> str:
    """单文件入库: 新增/更新(带 lineage)/跳过。返回状态。
    debounce: 托盘周期同步传 True — 活跃会话限频出版本 (见 _debounced); 手动 cv sync 立即捕获。"""
    c = _cv()
    st = path.stat()
    key = _norm_path(path)  # Windows 大小写失配曾让 export/sync 各写一行水位, 同文件双键 (二轮 ledger-1)
    row = conn.execute("SELECT mtime, size, snapshot_id, synced_at FROM sync_sources WHERE path=?",
                       (key,)).fetchone()
    if row and abs(row["mtime"] - st.st_mtime) < 1 and row["size"] == st.st_size:
        return "skip"
    if debounce and row and row["snapshot_id"] and _debounced(row["synced_at"], st.st_mtime):
        return "skip"
    fmt = c.detect_format(c.first_jsonl(path), path)
    exporters = {"codex-rollout": c.export_codex, "claude-transcript": c.export_claude,
                 "claude-projects": c.export_claude_code, "dsh-jsonl": c.export_dsh}
    exported = exporters[fmt](path, False)
    envelope = c.build_envelope(exported)
    if exported["stats"]["blocks_total"] == 0:
        return "empty"  # 辅助文件/空会话不入库
    m = (migrations or {}).get(_norm_path(path))
    if m:  # --apply 迁移副本: 标注来源与首版用量基线 (lineage 不进内容哈希, 链上每版都带)
        envelope["lineage"]["migrated_from"] = m.get("source")
        if m.get("baseline"):
            envelope["lineage"]["migration_baseline"] = m["baseline"]
    sid = c.snapshot_id(envelope)
    if row and row["snapshot_id"]:
        if row["snapshot_id"] == sid:
            # 内容寻址未变 (仅 mtime 抖动/追加导出器忽略的辅助行): 链不推进, 只推水位 —
            # 先挂 parent 再算 sid 会得到 parent==id 自环, 叶子过滤把会话从账本清零
            out = vault / "snapshots" / f"{sid}.json"
            if not out.exists():  # 快照文件丢了 (如手删): 重新落盘, 无链可挂
                c.atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
                c.register_snapshot(conn, envelope, sid, out)
            conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                         " VALUES(?,?,?,?,?)", (key, st.st_mtime, st.st_size, sid, c.now_iso()))
            return "skip"
        envelope["lineage"]["parent_snapshot"] = row["snapshot_id"]  # 同源演进 → 会话树
    out = vault / "snapshots" / f"{sid}.json"
    registered = conn.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone()
    if not out.exists():
        c.atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
    if not registered:
        # H1 同款守卫: 内容寻址 sid 已在库 (历史版本) 时不得用 parent=None 的 envelope
        # 重注册 — 会抹掉链上 parent (托盘升级换键首轮曾把全库演进链抹断)
        c.register_snapshot(conn, envelope, sid, out)
    conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                 " VALUES(?,?,?,?,?)", (key, st.st_mtime, st.st_size, sid, c.now_iso()))
    return "update" if row else "new"


def _zcode_usage_stats(u) -> dict:
    """ZCODE_USAGE_SQL 一行 → stats 字段 (total = 输入 + 输出; 输入已含缓存)。"""
    return {"tokens_in": u[1], "tokens_out": u[2], "reasoning_tokens": u[3], "cache_read": u[4],
            "cache_write": u[5], "total_tokens": u[1] + u[2], "usage_events": u[0]}


def export_zcode_session(db_conn, srow) -> dict:
    """Zcode 主转录库 (~/.zcode/cli/db/db.sqlite) 的单会话导出。
    结构实测: message.data{role,modelId} 按 sequence; part.data{type}: text/reasoning/tool
    (tool 自带 state.input/output 无需配对); synthetic part 过滤; signature 丢弃 (spec §3.2)。"""
    c = _cv()
    blocks, stats = [], c.zero_stats()
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
                    content, _ = c.clip(str(out if out is not None else state.get("error") or ""))
                    blocks.append({"type": "tool_result", "call_id": call_id,
                                   "content": content,
                                   "is_error": state.get("status") == "error", "ts": ts})
    stats["blocks_total"] = len(blocks)
    try:  # 用量: 旧适配器从不读 model_usage, zcode 快照 stats 曾全 0 (账本低估 + 详情/预览显示 0)
        u = db_conn.execute(c.ZCODE_USAGE_SQL, (sid,)).fetchone()
    except sqlite3.Error:
        u = None  # 老版 zcode 无 model_usage 表: 源无用量数据, 诚实留 0
    if u and u[0]:
        stats.update(_zcode_usage_stats(u))
    return {"capture": capture, "conversation": blocks, "stats": stats}


def backfill_zcode_usage(conn, force: bool = False) -> int:
    """zcode 历史快照的用量回填 (索引层, 零磁盘代价)。旧适配器未读 model_usage, 历史版本
    stats 全 0; 快照文件不可变不重写 — model_usage 逐请求带 session_id 与完成时间, 按每个版本
    的 created_at 精确算出"截至当时"的累计用量写入索引列。每 vault 一次 (vault_meta 标记)。"""
    c = _cv()
    conn.executescript(c.SCHEMA)
    if not force and conn.execute("SELECT 1 FROM vault_meta WHERE key='zcode_usage_backfill_v1'").fetchone():
        return 0
    n = 0
    zdb_p = Path(c.ZCODE_DB)
    if zdb_p.exists():
        zdb = sqlite3.connect(f"file:{zdb_p}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, session_ref, created_at FROM snapshots"
                                " WHERE source_format='zcode-sqlite' AND COALESCE(usage_events,0)=0").fetchall()
            for r in rows:
                try:
                    cutoff = int(dt.datetime.fromisoformat(r["created_at"]).timestamp() * 1000)
                    u = zdb.execute(c.ZCODE_USAGE_SQL + " AND COALESCE(completed_at, started_at) <= ?",
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
    conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('zcode_usage_backfill_v1', ?)", (c.now_iso(),))
    conn.commit()
    return n


def _sync_zcode_db(conn, vault: Path, debounce=False) -> tuple:
    """zcode 主库增量同步: session.time_updated 水位, 键 zcode:<session_id>。"""
    c = _cv()
    zdb_p = Path(c.ZCODE_DB)
    if not zdb_p.exists():
        return (0, 0, 0)
    db_conn = sqlite3.connect(f"file:{zdb_p}?mode=ro", uri=True)
    db_conn.row_factory = sqlite3.Row
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
        envelope = c.build_envelope(exported)
        sid = c.snapshot_id(envelope)
        if prev and prev["snapshot_id"]:
            if prev["snapshot_id"] == sid:
                # 内容未变仅 time_updated 前进: 只推水位, 不挂 parent (自环会把会话从账本清零)
                conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                             " VALUES(?,?,?,?,?)", (key, mtime, 0, sid, c.now_iso()))
                continue
            envelope["lineage"]["parent_snapshot"] = prev["snapshot_id"]
        out = vault / "snapshots" / f"{sid}.json"
        if not out.exists():
            c.atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        c.register_snapshot(conn, envelope, sid, out)
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (key, mtime, 0, sid, c.now_iso()))
        n_upd += 1 if prev else 0
        n_new += 0 if prev else 1
    db_conn.close()
    return (n_new, n_upd, 0)


def _sync_antigravity_dbs(conn, vault: Path, debounce=False) -> tuple:
    """Antigravity 本地会话回灌: ~/.gemini/antigravity/conversations/*.db → 快照。
    优先走零依赖原生 SQLite+Protobuf 直读 (支持 WAL 实时检测、精准 cwd、不因空/二进制工具输出失败);
    老格式原生读空时再兜底试 smigrate。自导入会话 (migrations.jsonl dest 命中) 跳过防回环。"""
    c = _cv()
    agy_dir = Path(c.ANTIGRAVITY_CONV_DIR)
    if not agy_dir.exists():
        return (0, 0, 0)
    exe = c.find_smigrate()
    imported: set[str] = set()
    for v in load_migrations(vault).values():
        stem = Path(v.get("dest", "")).stem
        if stem:
            imported.add(stem.lower())
    outbox = vault / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    n_new = n_upd = 0
    for dbp in sorted(agy_dir.glob("*.db")):
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
            exported = c.export_antigravity_db(dbp, False)
        except Exception:
            exported = None
        if (not exported or exported["stats"]["blocks_total"] == 0) and exe:
            tmp = outbox / f"agy-src-{dbp.stem}.jsonl"
            try:
                r = c.subprocess.run([exe, "convert", str(dbp), "--format", "antigravity",
                                      "--to", "claude", "--output", str(tmp)],
                                     capture_output=True, text=True, timeout=120,
                                     creationflags=c.NO_WINDOW)
                if r.returncode == 0 and tmp.exists():
                    exported = c.export_claude_code(tmp, False)
            except Exception:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        if not exported or exported["stats"]["blocks_total"] == 0:
            # 记录空/不可解析水位, 避免每轮 2 分钟重复撞墙空耗 CPU
            conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                         " VALUES(?,?,?,?,?)",
                         (key, eff_mtime, eff_size, prev["snapshot_id"] if prev else "", c.now_iso()))
            continue
        exported["capture"]["agent"] = "antigravity"
        exported["capture"]["source_format"] = "antigravity-db"
        envelope = c.build_envelope(exported)
        if exported.get("created_at"):
            envelope["created_at"] = exported["created_at"]
        sid = c.snapshot_id(envelope)
        if prev and prev["snapshot_id"]:
            if prev["snapshot_id"] == sid:
                conn.execute("INSERT OR REPLACE INTO sync_sources"
                             "(path,mtime,size,snapshot_id,synced_at) VALUES(?,?,?,?,?)",
                             (key, eff_mtime, eff_size, sid, c.now_iso()))
                continue
            envelope["lineage"]["parent_snapshot"] = prev["snapshot_id"]
        out = vault / "snapshots" / f"{sid}.json"
        if not out.exists():
            c.atomic_write_text(out, json.dumps(envelope, ensure_ascii=False, indent=1))
        c.register_snapshot(conn, envelope, sid, out)
        conn.execute("INSERT OR REPLACE INTO sync_sources(path,mtime,size,snapshot_id,synced_at)"
                     " VALUES(?,?,?,?,?)", (key, eff_mtime, eff_size, sid, c.now_iso()))
        n_upd += 1 if prev and prev["snapshot_id"] else 0
        n_new += 0 if (prev and prev["snapshot_id"]) else 1
    return (n_new, n_upd, 0)


def sync_vault(vault: Path, root: Path | None = None, debounce: bool = False, quiet: bool = False) -> dict:
    """增量扫描本地多工具会话源并写入 vault（CLI 与 HTTP /sync 共用）。"""
    c = _cv()
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    conn = c.db(vault)
    warnings: list[str] = []
    t0 = time.time()
    try:
        conn.executescript(c.SCHEMA + c.SYNC_TABLE)
        n_lin = c.backfill_lineage(conn)    # 每 vault 一次: 历史演进链索引回填
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
            conn.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES('sync_keys_norm_v1', ?)", (c.now_iso(),))
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
        _cv().vault_dir(args),
        root=args.root,
        debounce=bool(getattr(args, "debounce", False)),
        quiet=False,
    )
    print(f"sync 完成: 新增 {res['new']} · 更新 {res['updated']} · 跳过 {res['skipped']}(未变/异常)"
          f" · 空会话 {res['empty']} | vault 总快照 {res['total']} | 耗时 {res['elapsed']:.1f}s")
