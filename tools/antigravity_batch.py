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


from cv_antigravity import _is_eligible_antigravity_cap as _is_eligible_cap, plan_antigravity_batch  # noqa: E402,F401


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

    planned = plan_antigravity_batch(
        vault,
        conv_dir=CONV_DIR,
        summary_db=SUMMARY_DB,
        agent=args.agent,
        project=args.project,
        include_archived=args.include_archived,
        limit=args.limit,
        recover=args.recover,
    )
    print(f"既有库指纹: {planned['existing_fps_count']} 条 (来自 {CONV_DIR})")
    plan = planned["plan"]

    by_proj: dict[str, list] = {}
    for p in plan:
        by_proj.setdefault((p.get("cwd") or "").replace("\\", "/").rstrip("/"), []).append(p)
    print(f"待导入 {len(plan)} 条 / 已存在跳过 {planned['skipped_done']} / 指纹重复跳过 {planned['skipped_dup']}；"
          f"涉及项目 {len(by_proj)} 个")
    for proj_path, lst in sorted(by_proj.items(), key=lambda x: -len(x[1]))[:20]:
        print(f"  {len(lst):4}  {proj_path}")
    if args.dry_run:
        return

    exe = cv.find_smigrate()
    if not exe:
        raise SystemExit("未找到 smigrate: uv tool install session-migrate")

    # ① 并行转换 (只写 vault/outbox, 不碰 Antigravity)
    t0 = time.time()
    converted, errors = {}, []

    def _conv(p_item):
        sid = p_item["sid"]
        cascade = p_item["cascade"]
        try:
            res = cv._export_one_harness(vault, exe, sid, "antigravity", quiet=True,
                                         create_project=True, session_id=cascade)
            return (sid, p_item, cascade, Path(res["dest"]), None)
        except Exception as exc:  # 单条失败不断链
            return (sid, p_item, cascade, None, str(exc)[:160])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(_conv, p_item) for p_item in plan]
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
