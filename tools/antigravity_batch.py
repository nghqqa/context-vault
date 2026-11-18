#!/usr/bin/env python3
"""vault → Antigravity 批量导入 (按项目分组, 一次性把全部项目会话装进 Antigravity)。

流程 (复用 cv.py 已验证的导出链路):
  ① 选出真实项目目录下的顶层主对话 (跳过 .zcode/workspace 草稿与临时目录)
  ② 确定性 cascade id = uuid5(快照id) 伪装 uuid4 格式 → 同快照重导幂等, 文件存在即跳过
  ③ 指纹去重: 已导入库 (早期手工导入, 随机 id) 按首条用户消息比对, 命中即跳过
  ④ cv._export_one_harness 转换 (smigrate + workspace/时间戳后处理, 项目自动注册)
  ⑤ 安装 conversations/<cascade>.db + 写 conversation_summaries 行 (标题/预览/时间戳/
     workspace/project_id/raw_summary protobuf, 对齐已验证的原生结构)
  ⑥ migrations.jsonl 留痕 sid→cascade, 供后续增量导入秒判

用法:
  python tools/antigravity_batch.py            # 全量 (先转换后安装, Antigravity 无需提前关闭)
  python tools/antigravity_batch.py --dry-run  # 只列计划
  python tools/antigravity_batch.py --project context-vault --limit 3
"""
from __future__ import annotations

import sys
import json
import time
import uuid
import sqlite3
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv  # noqa: E402  (单文件引擎, 复用其导出与 protobuf 助手)

HOME = Path.home()
CONV_DIR = HOME / ".gemini" / "antigravity" / "conversations"
SUMMARY_DB = HOME / ".gemini" / "antigravity" / "conversation_summaries.db"
NS = "context-vault:antigravity:"


# ------------------------------------------------- 复用 cv.py 权威实现 (单一真相源, 防协议漂移)

det_cascade = lambda sid: str(uuid.UUID(bytes=bytes(  # noqa: E731 — 向后兼容旧符号
    bytearray(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes)[:6]
    + bytes([(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes[6] & 0x0F) | 0x40])
    + bytearray(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes)[7:8]
    + bytes([(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes[8] & 0x3F) | 0x80])
    + bytearray(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes)[9:]
)))
eligible_cwd = cv._antigravity_eligible_cwd
db_first_user_text = cv._antigravity_db_fingerprint
build_raw_summary = cv._antigravity_raw_summary
ws_uris = cv._antigravity_ws_uris


def _norm_text(s: str) -> str:
    return " ".join(s.split()).lower()[:80]


def first_user_text(env: dict) -> str:
    t = cv._antigravity_first_user_text(env)
    return _norm_text(t) if t else ""


def iso_utc(epoch: int) -> str:
    return dt.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def _is_eligible_cap(cap: dict, allowed_agents: set[str],
                     arch_refs: set[str], sub_refs: set[str]) -> bool:
    """统一校验来源与归档/子代理守卫；normal 路径与 --recover 共用，杜绝绕过过滤重导已归档或自回环会话。"""
    cwd = cap.get("cwd") or ""
    if not eligible_cwd(cwd):
        return False
    ag = (cap.get("agent") or "").lower()
    if ag == "antigravity":
        return False  # 绝不将 cv sync 回灌的 Antigravity 原生会话再倒回 Antigravity
    if allowed_agents and ag not in allowed_agents:
        return False
    sref = cap.get("session_ref") or ""
    if ag == "zcode" and sref in (arch_refs | sub_refs):
        return False
    orig = str(cap.get("originator") or "")
    if cap.get("parent_session_ref") or orig.startswith("workflow subagent"):
        return False
    return True


def _collect_existing_by_ref(vault: Path, have_rows: set[str]) -> set[tuple[str, str]]:
    """已完整安装（.db 与 summaries 均在位）的 (agent, session_ref) 集合，防止同会话演化/换算法重导生成双份。"""
    out: set[tuple[str, str]] = set()
    for cid, entry in cv._collect_antigravity_migrated_dbs(vault).items():
        if entry["session_ref"] and (CONV_DIR / f"{cid}.db").exists() and cid in have_rows:
            out.add(((entry["agent"] or "").lower(), entry["session_ref"]))
    return out


# ------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default="vault")
    ap.add_argument("--project", default=None)
    ap.add_argument("--agent", default="zcode",
                    help="仅导入指定来源 agent（默认 zcode；设 all 可包含 codex/claude-code，仍排除 antigravity 与已归档）")
    ap.add_argument("--include-archived", action="store_true",
                    help="显式包含 ZCode 已归档会话（默认跳过）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recover", action="store_true",
                    help="以 migrations.jsonl 为准恢复: 曾导入过但目标端缺失的会话（仍严格应用归档与来源筛选）")
    args = ap.parse_args()
    vault = cv.vault_dir(argparse.Namespace(vault=args.vault, cmd="batch")).resolve()
    agent_arg = None if str(args.agent or "").strip().lower() in ("", "all") else args.agent
    allowed_agents = set(cv._parse_agent_filter(agent_arg))
    arch_refs = set() if args.include_archived else cv._zcode_archived_refs()
    sub_refs = cv._zcode_subagent_refs()

    if args.project:
        items = cv._resolve_project_items(
            vault, args.project, include_archived=args.include_archived, agent=agent_arg)
    else:
        items = cv.resolve_top_conversations(
            vault, agent=agent_arg, include_archived=args.include_archived)
    items = [it for it in items if eligible_cwd(it.get("cwd") or "")
             and (it.get("agent") or "").lower() != "antigravity"]
    if args.limit:
        items = items[:args.limit]

    # summaries 已注册的会话 id: db 文件在但行缺失 = 半安装, 需补装而不是跳过;
    # 已注册的库才有指纹效力 (无行的半安装库不算, 否则会挡住自己的补装)
    have_rows: set[str] = set()
    if SUMMARY_DB.exists():
        try:
            conn0 = sqlite3.connect(SUMMARY_DB, timeout=30)
            have_rows = {r[0] for r in conn0.execute("SELECT conversation_id FROM conversation_summaries")}
            conn0.close()
        except sqlite3.Error:
            pass
    existing_fps: set[str] = set()
    if CONV_DIR.exists():
        for f in CONV_DIR.glob("*.db"):
            if f.stem not in have_rows:
                continue
            fp = db_first_user_text(f)
            if fp:
                existing_fps.add(fp)
    print(f"既有库指纹: {len(existing_fps)} 条 (来自 {CONV_DIR})")

    # migrations.jsonl 留痕 → sid→cascade 秒判
    done_cascades: dict[str, str] = {}
    mj = vault / "migrations.jsonl"
    if mj.exists():
        for line in mj.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            dest = o.get("dest") or ""
            if "antigravity" in dest and "conversations" in dest:
                done_cascades[o.get("source", "")] = Path(dest).stem

    existing_refs = _collect_existing_by_ref(vault, have_rows)
    plan, skipped = [], {"dup": 0, "done": 0}
    seen_cascades: set[str] = set()
    for it in items:
        sid = it["id"]
        ag = (it.get("agent") or "").lower()
        sref = it.get("session_ref") or ""
        try:
            env = cv.load_snapshot(vault, sid)
        except (ValueError, OSError):
            continue
        if not _is_eligible_cap(env.get("capture") or {}, allowed_agents, arch_refs, sub_refs):
            continue
        cascade = cv._deterministic_session_id(env)
        prev_cid = done_cascades.get(sid)
        if ((CONV_DIR / f"{cascade}.db").exists() and cascade in have_rows) \
                or (prev_cid and (CONV_DIR / f"{prev_cid}.db").exists() and prev_cid in have_rows) \
                or (sref and (ag, sref) in existing_refs):
            skipped["done"] += 1
            continue
        fp = first_user_text(env)
        if fp and fp in existing_fps:
            skipped["dup"] += 1
            continue
        if cascade in seen_cascades:
            continue
        seen_cascades.add(cascade)
        plan.append((it, cascade))

    if args.recover:
        # 恢复: 曾导入过 (migrations 有痕) 但目标端缺失的会话 — 仍严格遵守归档/来源/防自回环守卫
        conn_v = cv.db(vault)
        srows = conn_v.execute(
            "SELECT id, agent, session_ref, created_at FROM snapshots").fetchall()
        parents = {r["parent_snapshot"] for r in conn_v.execute(
            "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
        conn_v.close()
        by_ref: dict[tuple, list] = {}
        for r in srows:
            if r["id"] in parents:
                continue
            by_ref.setdefault((r["agent"] or "", r["session_ref"] or ""), []).append(r)
        for sid in done_cascades:
            try:
                env0 = cv.load_snapshot(vault, sid)
            except (ValueError, OSError):
                continue
            cap0 = env0.get("capture") or {}
            if not _is_eligible_cap(cap0, allowed_agents, arch_refs, sub_refs):
                continue
            ag0 = (cap0.get("agent") or "").lower()
            sref0 = cap0.get("session_ref") or ""
            cands = by_ref.get((cap0.get("agent") or "", sref0), [])
            leaf = max(cands, key=lambda r: r["created_at"] or "")["id"] if cands else sid
            try:
                env = cv.load_snapshot(vault, leaf)
            except (ValueError, OSError):
                continue
            cap = env.get("capture") or {}
            if not _is_eligible_cap(cap, allowed_agents, arch_refs, sub_refs):
                continue
            cascade = cv._deterministic_session_id(env)
            if ((CONV_DIR / f"{cascade}.db").exists() and cascade in have_rows) \
                    or (sref0 and (ag0, sref0) in existing_refs):
                skipped["done"] += 1
                continue
            fp = first_user_text(env)
            if fp and fp in existing_fps:
                skipped["dup"] += 1
                continue
            if cascade in seen_cascades:
                continue
            seen_cascades.add(cascade)
            plan.append(({"id": leaf, "cwd": cap.get("cwd") or "",
                          "title": cap.get("originator") or ""}, cascade))

    by_proj: dict[str, list] = {}
    for it, cascade in plan:
        by_proj.setdefault((it.get("cwd") or "").replace("\\", "/").rstrip("/"), []).append((it, cascade))
    print(f"待导入 {len(plan)} 条 / 已存在跳过 {skipped['done']} / 指纹重复跳过 {skipped['dup']}；"
          f"涉及项目 {len(by_proj)} 个")
    for p, lst in sorted(by_proj.items(), key=lambda x: -len(x[1]))[:20]:
        print(f"  {len(lst):4}  {p}")
    if args.dry_run:
        return

    exe = cv.find_smigrate()
    if not exe:
        raise SystemExit("未找到 smigrate: uv tool install session-migrate")

    # ① 并行转换 (只写 vault/outbox, 不碰 Antigravity)
    t0 = time.time()
    converted, errors = {}, []

    def _conv(it, cascade):
        sid = it["id"]
        try:
            res = cv._export_one_harness(vault, exe, sid, "antigravity", quiet=True,
                                         create_project=True, session_id=cascade)
            return (sid, it, cascade, Path(res["dest"]), None)
        except Exception as exc:  # 单条失败不断链
            return (sid, it, cascade, None, str(exc)[:160])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(_conv, it, cascade) for it, cascade in plan]
        for n, fut in enumerate(as_completed(futs), 1):
            sid, it, cascade, out, err = fut.result()
            if err:
                errors.append((sid, err))
            else:
                converted[sid] = (it, cascade, out)
            if n % 10 == 0 or n == len(plan):
                print(f"  转换 {n}/{len(plan)}  ({time.time() - t0:.0f}s, 失败 {len(errors)})")

    # ② 串行安装 + summaries (Antigravity 在跑也可写: 新文件 + WAL 短事务)
    installed, fails = 0, []
    conn_s = sqlite3.connect(SUMMARY_DB, timeout=30)
    try:
        conn_s.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    for sid, (it, cascade, out) in converted.items():
        try:
            env = cv.load_snapshot(vault, sid)
            cwd = it.get("cwd") or ""
            ws_raw, enc = ws_uris(cwd)
            rng = cv._session_epoch_range(env)
            epoch_mod = rng[1] if rng else int(time.time())
            epoch_in = rng[0] if rng else epoch_mod
            cdb = sqlite3.connect(out, timeout=15)
            step_count = cdb.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            r = cdb.execute("SELECT COALESCE(MAX(idx),0) FROM steps WHERE step_type=14").fetchone()
            input_idx = int(r[0] or 0)
            traj = cdb.execute("SELECT trajectory_id FROM trajectory_meta ORDER BY rowid LIMIT 1").fetchone()
            blob = cdb.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
            cdb.close()
            fields, _ = cv._pb_decode(bytes(blob[0])) if blob and blob[0] else ({}, b"")
            pid_vals = fields.get((18, 2))
            project_id = pid_vals[0].decode("ascii", "ignore") if pid_vals else "outside-of-project"

            title = (it.get("title") or "(无标题)").strip()[:60]
            preview = first_user_text(env)[:200] or title
            f17u = str(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid + ":f17"))
            raw = build_raw_summary(title, step_count, epoch_mod, epoch_in,
                                    traj[0] if traj else cascade, ws_raw, enc, cascade,
                                    project_id, f17u)

            target = CONV_DIR / f"{cascade}.db"
            tmp = CONV_DIR / f".{cascade}.tmp-{cv.os.getpid()}"
            cv.shutil.copy2(out, tmp)
            cv.os.replace(tmp, target)
            out.unlink(missing_ok=True)

            conn_s.execute(
                "INSERT OR REPLACE INTO conversation_summaries("
                "conversation_id, title, preview, step_count, last_modified_time, workspace_uris,"
                " status, source, project_id, agent_name, parent_conversation_id, nesting_depth,"
                " battle_id, winning_conversation_id, not_fully_idle, killed,"
                " last_user_input_time, last_user_input_step_index, app_data_dir,"
                " raw_summary, group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cascade, title, preview, step_count, iso_utc(epoch_mod),
                 json.dumps([enc]), "CASCADE_RUN_STATUS_IDLE", "", project_id, "", "", 0,
                 "", "", 0, 0, iso_utc(epoch_in), input_idx, "antigravity", raw, ""))
            cv._antigravity_proto_upsert(cascade, raw)  # proto 是 app 真源, 不写=重启被清
            cv._record_migration(vault, target, sid, baseline=None)
            installed += 1
            if installed % 20 == 0:
                conn_s.commit()
                print(f"  安装 {installed} …")
        except Exception as exc:
            fails.append((sid, str(exc)[:200]))
    conn_s.commit()
    conn_s.close()

    print(f"\n完成: 安装 {installed} / 失败 {len(fails)} / 转换失败 {len(errors)}  "
          f"({time.time() - t0:.0f}s)")
    for sid, e in errors[:5]:
        print(f"  conv-err {sid[:14]}: {e}")
    for sid, e in fails[:8]:
        print(f"  inst-err {sid[:14]}: {e}")
    if not fails and not errors:
        print("重启 Antigravity 即可看到各项目下的历史会话。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
