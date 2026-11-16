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


# ------------------------------------------------- 确定性 id (uuid5 → uuid4 格式位)

def det_cascade(sid: str) -> str:
    b = bytearray(uuid.uuid5(uuid.NAMESPACE_URL, NS + sid).bytes)
    b[6] = (b[6] & 0x0F) | 0x40  # version 4
    b[8] = (b[8] & 0x3F) | 0x80  # variant 10xx
    return str(uuid.UUID(bytes=bytes(b)))


# ------------------------------------------------- 项目筛选

SKIP_MARKS = (".zcode/workspace", "appdata/local/temp", "/tmp/", "\\tmp\\", "cvtest")


def eligible_cwd(cwd: str) -> bool:
    if not cwd:
        return False
    n = cwd.replace("\\", "/").lower()
    return not any(m in n for m in SKIP_MARKS)


# ------------------------------------------------- 指纹: 首条用户消息

def _pb_strings(data: bytes, depth: int = 0) -> list[str]:
    """递归收集 protobuf 里所有可读字符串 (不依赖精确字段布局)。"""
    out: list[str] = []
    if depth > 6:
        return out
    i = 0
    try:
        while i < len(data):
            tag, i = cv._rd_varint(data, i)
            k, wt = tag >> 3, tag & 7
            if wt == 0:
                _, i = cv._rd_varint(data, i)
            elif wt == 2:
                ln, i = cv._rd_varint(data, i)
                raw = data[i:i + ln]
                i += ln
                try:
                    s = raw.decode("utf-8")
                    if sum(32 <= ord(c) < 127 or ord(c) > 0x2E7F for c in s) > len(s) * 0.9:
                        out.append(s)
                    else:
                        out += _pb_strings(raw, depth + 1)
                except (UnicodeDecodeError, ValueError):
                    out += _pb_strings(raw, depth + 1)
            else:
                break
    except (IndexError, ValueError):
        pass
    return out


def _norm_text(s: str) -> str:
    return " ".join(s.split()).lower()[:80]


def first_user_text(env: dict) -> str:
    for b in env.get("conversation") or []:
        role = (b.get("role") or b.get("type") or "").lower()
        if role == "user":
            text = (b.get("content") or b.get("summary") or "").strip()
            if text:
                return _norm_text(text)
    return ""


def db_first_user_text(db_path: Path) -> str:
    """从已安装库提取首条用户输入文本 (step_type=14 USER_INPUT)。"""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        row = conn.execute(
            "SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx LIMIT 1").fetchone()
        conn.close()
        if not row or not row[0]:
            return ""
        strs = _pb_strings(bytes(row[0]))
        return _norm_text(max(strs, key=len)) if strs else ""
    except sqlite3.Error:
        return ""


# ------------------------------------------------- raw_summary protobuf (对齐已验证模板)

def _ts_msg(epoch: int, nanos: int = 0) -> bytes:
    return cv._pb_var(1, max(1, int(epoch))) + cv._pb_var(2, nanos)


def _ws_msg(ws_raw: str) -> bytes:
    return cv._pb_len(1, ws_raw.encode("utf-8")) + cv._pb_len(3, b"")


def build_raw_summary(title: str, step_count: int, epoch_mod: int, epoch_input: int,
                      traj_id: str, ws_raw: str, enc_uri: str, cascade: str,
                      project_id: str, f17_uuid: str) -> bytes:
    t = title.encode("utf-8")
    f15 = cv._pb_len(1, t) + cv._pb_len(7, _ts_msg(epoch_mod))
    f17 = (cv._pb_len(1, _ws_msg(ws_raw)) + cv._pb_len(2, _ts_msg(epoch_input))
           + cv._pb_len(3, f17_uuid.encode("ascii"))
           + cv._pb_len(6, cascade.encode("ascii"))
           + cv._pb_len(7, enc_uri.encode("utf-8"))
           + cv._pb_len(18, (project_id or "outside-of-project").encode("ascii")))
    out = (cv._pb_len(1, t) + cv._pb_var(2, max(1, step_count))
           + cv._pb_len(3, _ts_msg(epoch_mod)) + cv._pb_len(4, traj_id.encode("ascii"))
           + cv._pb_var(5, 1) + cv._pb_len(7, _ts_msg(epoch_input))
           + cv._pb_len(9, _ws_msg(ws_raw)) + cv._pb_len(10, _ts_msg(epoch_input))
           + cv._pb_len(15, f15) + cv._pb_var(16, 1) + cv._pb_len(17, f17)
           + cv._pb_var(22, 4) + cv._pb_var(28, 4))
    return out


def ws_uris(cwd: str) -> tuple[str, str]:
    """与 cv._antigravity_set_workspace 同规则: 盘符小写, 冒号 %3A 编码。"""
    posix = cwd.replace("\\", "/").rstrip("/")
    if len(posix) >= 2 and posix[1] == ":":
        posix = posix[0].lower() + posix[1:]
    return "file:///" + posix, "file:///" + quote(posix, safe="/")


def iso_utc(epoch: int) -> str:
    return dt.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


# ------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default="vault")
    ap.add_argument("--project", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    vault = Path(args.vault).expanduser().resolve()

    items = cv.resolve_top_conversations(vault)
    if args.project:
        items = cv._resolve_project_items(vault, args.project)
    items = [it for it in items if eligible_cwd(it.get("cwd") or "")]
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

    plan, skipped = [], {"dup": 0, "done": 0}
    for it in items:
        sid = it["id"]
        cwd = it.get("cwd") or ""
        cascade = done_cascades.get(sid) or det_cascade(sid)
        if (CONV_DIR / f"{cascade}.db").exists() and cascade in have_rows:
            skipped["done"] += 1
            continue
        fp = ""
        try:
            fp = first_user_text(cv.load_snapshot(vault, sid))
        except (ValueError, OSError):
            pass
        if fp and fp in existing_fps:
            skipped["dup"] += 1
            continue
        plan.append((it, cascade))

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
