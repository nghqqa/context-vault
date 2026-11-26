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
from cv_core import (  # noqa: F401,E402 — re-exported as public engine API
    BadMigrateTarget,
    SCHEMA,
    SnapshotNotFound,
    atomic_write_text,
    db,
    estimate_text_tokens,
    fmt_usd,
    rotate_log_if_needed,
    vault_dir,
)
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
VERSION = "0.13.1"  # 引擎模块化瘦身 / HTTP 与 UI 模板去重 / macOS 零阻塞回环绑定
TOOL_RESULT_LIMIT = 256 * 1024  # spec §8.2 裁决: 单块截断阈值 256KB
HOME = Path.home()
HEADROOM_DIR = HOME / ".headroom"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

# ---------------------------------------------------------------- vault 基础

# vault 基础原语 (SCHEMA / rotate_log_if_needed / vault_dir / db / atomic_write_text) 已收敛至 cv_core



def cmd_init(args):
    vault = vault_dir(args)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "outbox").mkdir(parents=True, exist_ok=True)
    conn = db(vault)
    conn.executescript(SCHEMA + SYNC_TABLE)
    conn.commit()
    print(f"vault 就绪: {vault.resolve()}")


# ---- 已拆解至 tools/cv_export.py (全量 re-export 保持向后兼容) ----
from cv_export import (  # noqa: F401,E402
    LINEAGE_TAIL,
    SNAP_COLS,
    _GENERIC_ORIGINATORS,
    _SECRET_PATTERNS,
    _derive_summary_title,
    _parent_chain_reaches,
    backfill_lineage,
    build_envelope,
    canonical_bytes,
    clip,
    cmd_export,
    detect_format,
    export_claude,
    export_claude_code,
    export_codex,
    export_dsh,
    first_jsonl,
    iter_jsonl,
    load_snapshot,
    now_iso,
    read_lineage_tail,
    redact_envelope,
    redact_secrets,
    register_snapshot,
    snapshot_id,
    zero_stats,
)


# ---- 已拆解至 tools/cv_projects.py (全量 re-export 保持向后兼容) ----
from cv_projects import (  # noqa: F401,E402
    _is_ungrouped_cwd,
    _norm_project_match,
    _norm_win_path,
    _parse_agent_filter,
    _prefer_exact_project,
    _resolve_project_items,
    _zcode_archived_refs,
    _zcode_deleted_project_task_refs,
    _zcode_refs_matching,
    _zcode_subagent_refs,
    group_conversations_by_project,
    resolve_top_conversations,
)


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
    blocks_to_dsh,
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
    plan_batch_migration,
    run_batch_migration,
    cmd_import,
    cmd_migrate,
)

# ---- 已拆解至 tools/cv_ops.py (全量 re-export 保持向后兼容) ----
from cv_ops import (  # noqa: F401,E402
    DASH_TMPL,
    active_sessions,
    audit_summary_and_timeline,
    backup_vault_api,
    chat_url,
    cmd_audit,
    cmd_backup,
    cmd_dashboard,
    cmd_fork,
    cmd_probe,
    cmd_prune,
    cmd_repair,
    cmd_tree,
    cmd_warm,
    cmd_watch,
    fetch_models,
    fork_snapshot,
    load_profiles,
    models_url,
    notify,
    post_chat,
    probe_once,
    prune_snapshots_api,
    resolve_key,
    storm_relink,
    toast,
    warm_messages,
)


# ---- 已拆解至 tools/cv_ledger_cli.py (全量 re-export 保持向后兼容) ----
from cv_ledger_cli import (  # noqa: F401,E402
    QUOTA_PER_USD,
    cmd_ledger_import_headroom,
    cmd_ledger_import_relay,
    cmd_ledger_import_zcode,
    cmd_ledger_stats,
    cv_ledger_limit_cmd,
    cv_ledger_plan_token_cmd,
    cv_ledger_quota_cmd,
    cv_ledger_sync_cmd,
    fetch_relay_usage,
    fetch_relay_web_logs,
    owned_stats,
)


# ------------------------------------------------- sync: 自动入库 (增量 + lineage)

SYNC_TABLE = """
CREATE TABLE IF NOT EXISTS sync_sources(
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER, snapshot_id TEXT, synced_at TEXT);
"""

SNAPSHOT_MIN_INTERVAL = int(os.environ.get("CV_SNAPSHOT_INTERVAL_MIN", "30")) * 60
SNAPSHOT_IDLE_SEC = 600
ZCODE_DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
ZCODE_TASKS_DB = HOME / ".zcode" / "v2" / "tasks-index.sqlite"
# model_usage 逐请求记录 (带 session_id); OpenAI 语义: input 已含 cache 读 (实证 0 行 cache_read>input)
ZCODE_USAGE_SQL = ("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                   " COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_read_input_tokens),0),"
                   " COALESCE(SUM(cache_creation_input_tokens),0) FROM model_usage WHERE session_id=?")
ANTIGRAVITY_CONV_DIR = HOME / ".gemini" / "antigravity" / "conversations"

# ---- 已拆解至 tools/cv_sync.py (全量 re-export 保持向后兼容与测试 monkeypatch) ----
from cv_sync import (  # noqa: F401,E402
    _debounced,
    _norm_path,
    _sync_antigravity_dbs,
    _sync_zcode_db,
    _zcode_usage_stats,
    backfill_zcode_usage,
    cmd_sync,
    export_zcode_session,
    load_migrations,
    scan_sources,
    sync_one,
    sync_vault,
)


# cmd_prune / storm_relink / cmd_repair 已归并至 tools/cv_ops.py (顶部 re-export)


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


# fork_snapshot / cmd_fork / cmd_tree / audit_summary_and_timeline / cmd_audit 已归并至 tools/cv_ops.py (顶部 re-export)


# ---- 已拆解至 tools/cv_mcp.py (全量 re-export 保持向后兼容) ----
from cv_mcp import (  # noqa: F401,E402
    _claude_exe,
    _detect_mcp_client_app,
    _has_local_bin_cmd,
    _install_mcp_antigravity,
    _install_mcp_claude,
    _install_mcp_codex,
    _install_mcp_cursor,
    _is_real_home_dir,
    _mcp_cmd_parts,
    _parse_toml_table_header,
    _resolve_mcp_targets,
    _uninstall_mcp_antigravity,
    _uninstall_mcp_claude,
    _uninstall_mcp_codex,
    _uninstall_mcp_cursor,
    _uninstall_mcp_json_file,
    cmd_mcp,
    mcp_install_api,
    mcp_status_api,
    mcp_uninstall_api,
)


# prune_snapshots_api / backup_vault_api 已归并至 tools/cv_ops.py (顶部 re-export)


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

# cmd_backup 已归并至 tools/cv_ops.py (顶部 re-export)


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
    import cv_dsh
    sources = [
        ("ZCode 主库", ZCODE_DB.exists(), str(ZCODE_DB)),
        ("Codex 会话", CODEX_SESSIONS.exists(), str(CODEX_SESSIONS)),
        ("Claude Projects", CLAUDE_PROJECTS.exists(), str(CLAUDE_PROJECTS)),
        ("Claude 转录", (HOME / ".claude" / "transcripts").exists(), str(HOME / ".claude/transcripts")),
        ("Antigravity", ANTIGRAVITY_CONV_DIR.exists(), str(ANTIGRAVITY_CONV_DIR)),
        ("DeepSeek Harness", cv_dsh.dsh_sessions_root().exists(), str(cv_dsh.dsh_sessions_root())),
    ]
    any_src = False
    for name, good, detail in sources:
        any_src |= good
        print(f"  {'✓' if good else '·'} {name:<15} {detail}")

    print("\n[3/3] 可选增强组件与 MCP 状态")
    smig = find_smigrate()
    print(f"  {'✓' if smig else '·'} session-migrate  {smig or '未安装 (uv tool install session-migrate 解锁 18 格式桥接)'}")
    zbk = cv_dsh.zstd_backend()
    print(f"  {'✓' if zbk else '·'} DSH Zstd 编解码  {zbk or '未就绪 (安装 zstandard / Node>=22.18 / zstd CLI 以支持 .v4.jsonl.zstd)'}")
    hr_ok = (HEADROOM_DIR / "proxy_savings.json").exists() or (HEADROOM_DIR / "savings_events.jsonl").exists()
    print(f"  {'✓' if hr_ok else '·'} headroom 账本    {HEADROOM_DIR if hr_ok else '未接入 (可选通路层省钱统计)'}")
    try:
        mst = mcp_status_api(vault, probe_cli=False)
        parts = []
        for c in mst.get("clients", []):
            tag = "已接入" if c.get("installed") else ("仅残留" if c.get("residual") else ("未接入" if c.get("app_detected") else "未安装"))
            parts.append(f"{c['id']}={tag}")
        print(f"  {'✓' if mst.get('installed_count') else '·'} MCP 接入摘要     {' · '.join(parts)}")
    except Exception:
        pass

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
        print("[3/4] 同步多端 token 账本 (Claude/Codex/ZCode/Antigravity/DSH)...")
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

    mcp_clients_raw = getattr(args, "mcp_clients", None)
    if mcp_clients_raw:
        clist = [x.strip() for x in str(mcp_clients_raw).split(",") if x.strip()]
        print(f"[附加] 按需接入指定 MCP 客户端 ({', '.join(clist)})...")
        try:
            res = mcp_install_api(vault, client=None, clients=clist)
            for item in res["results"]:
                print(f"  [{item['client']}] {item['message']}")
        except Exception as e:
            print(f"  MCP 接入提示: {e}")
    elif getattr(args, "mcp", False):
        print("[附加] 自动接入全部检测到的 MCP 客户端...")
        ns = argparse.Namespace(
            vault=str(vault),
            mcp_cmd="install",
            client="all",
            clients=None,
            all=True,
            dry_run=False,
        )
        try:
            cmd_mcp(ns)
        except Exception as e:
            print(f"  MCP 接入提示: {e}")

    print("\n初始化完成！下一步推荐:")
    print("  1. 启动 Web 控制台 + 热索引 + AML API:  cv serve  (或 python tools/cv.py serve)")
    if not getattr(args, "mcp", False) and not mcp_clients_raw:
        print("  2. 按需/全量接入 MCP 客户端:           cv mcp install --clients claude,codex,dsh  (或 --all)")


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


def cmd_skills(args):
    """跨工具 Skill 盘点与批量迁移 (延迟导入 cv_skills)。"""
    import cv_skills
    cv_skills.cmd_skills(args)


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
    i.add_argument("--target", choices=["codex", "claude", "markdown", "antigravity", "zcode", "dsh"], required=True)
    i.add_argument("--project", help="按项目名或路径批量导入顶层主对话")
    i.add_argument("--agent", help="严格按来源 agent 过滤导入 (如 zcode；不带入 codex 等其他来源)")
    i.add_argument("--limit", type=int, default=0, help="按项目导入时的最多会话数 (0=全部)")
    i.add_argument("--include-subagents", action="store_true", help="包含子代理与已归档会话")
    i.add_argument("--include-archived", action="store_true", help="包含 ZCode 已归档会话 (不含子代理)")
    i.add_argument("--apply", action="store_true", help="写入真实会话目录 (默认 dry-run)")
    i.add_argument("--no-skip-migrated", action="store_true", help="整端批量导入时不跳过已迁移会话")
    i.add_argument("--strip-thinking", action="store_true", help="不迁移 reasoning 摘要")
    i.add_argument("--budget-tokens", type=int, default=None,
                   help="markdown 目标的简报 token 预算 (建议 2000|4000|8000; 缺省按 48KB 推导)")
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="一键: 导出→预览→导入")
    m.add_argument("source")
    m.add_argument("--target", choices=["codex", "claude", "markdown", "antigravity", "zcode", "dsh"], required=True)
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

    mc = sub.add_parser("mcp", help="MCP 记忆后端按需接入与管理")
    msub = mc.add_subparsers(dest="mcp_cmd", required=True)
    mi = msub.add_parser("install")
    mi.add_argument("--client", choices=["claude", "codex", "cursor", "antigravity", "dsh"], default="claude")
    mi.add_argument("--clients", help="多选逗号分隔客户端 (如 claude,codex,dsh)")
    mi.add_argument("--all", action="store_true", help="接入全部已检测到主程序的客户端 (跳过仅残留目录)")
    mi.set_defaults(func=cmd_mcp)
    mu = msub.add_parser("uninstall")
    mu.add_argument("--client", choices=["claude", "codex", "cursor", "antigravity", "dsh"], default="cursor")
    mu.add_argument("--clients", help="多选逗号分隔要移除的客户端 (如 cursor,dsh)")
    mu.add_argument("--all", action="store_true", help="从全部客户端配置中移除 context-vault")
    mu.set_defaults(func=cmd_mcp)
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
    lsy = lsub.add_parser("sync", help="多端直采入账: Claude/Codex/ZCode/Antigravity/DSH 本地日志 → usage_events (幂等, 脱离 headroom)")
    lsy.add_argument("--sources", default="all",
                     help="all|claude|codex|zcode|antigravity|dsh (可逗号组合, 默认 all)")
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
    stp.add_argument("--mcp", action="store_true", help="同时自动接入全部已检测到主程序的 MCP 客户端")
    stp.add_argument("--mcp-clients", help="按需接入逗号分隔的 MCP 客户端 (如 claude,codex,dsh)")
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

    sk = sub.add_parser("skills", help="跨工具 Skill 技能盘点与批量迁移 (ZCode/Claude/Codex/Antigravity/Cursor/DSH)")
    sksub = sk.add_subparsers(dest="skills_cmd", required=True)
    sk_ls = sksub.add_parser("list", help="盘点各端本地 Skill 数量、预估 Prompt Token 与目标冲突状态")
    sk_ls.add_argument("--source", default="zcode", choices=["zcode", "claude", "codex", "antigravity", "cursor", "dsh", "all"],
                       help="来源工具 (默认 zcode)")
    sk_ls.add_argument("--target", default="antigravity",
                       choices=["antigravity", "claude", "codex", "cursor", "zcode", "dsh", "workspace"],
                       help="对比目标 (默认 antigravity)")
    sk_ls.add_argument("--agy-mode", default="skills", choices=["skills", "plugin"],
                       help="Antigravity 落盘模式: skills=全局目录 | plugin=独立插件包")
    sk_ls.add_argument("--plugin-name", default="", help="Antigravity 插件模式名称 (默认 migrated-from-<source>)")
    sk_ls.add_argument("--include-plugins", action="store_true", help="同时包含插件缓存目录内的 Skill")
    sk_ls.add_argument("--json", action="store_true", help="输出 JSON")
    sk_ls.set_defaults(func=cmd_skills)

    sk_mg = sksub.add_parser("migrate", help="批量迁移 Skill (默认 dry-run 预演, 加 --apply 才写入)")
    sk_mg.add_argument("--from", dest="source", default="zcode",
                       choices=["zcode", "claude", "codex", "antigravity", "cursor", "dsh", "all"],
                       help="来源工具 (默认 zcode)")
    sk_mg.add_argument("--to", dest="target", default="antigravity",
                       choices=["antigravity", "claude", "codex", "cursor", "zcode", "dsh", "workspace"],
                       help="目标工具 (默认 antigravity)")
    sk_mg.add_argument("--only", help="仅迁移指定 Skill (逗号分隔 slug 或 id)")
    sk_mg.add_argument("--include-plugins", action="store_true", help="同时包含插件缓存目录内的 Skill")
    sk_mg.add_argument("--agy-mode", default="skills", choices=["skills", "plugin"],
                       help="迁往 Antigravity 模式: skills=直写全局目录 | plugin=打包为可一键开关插件")
    sk_mg.add_argument("--plugin-name", default="", help="Antigravity 插件名 (默认 migrated-from-<source>)")
    sk_mg.add_argument("--conflict", default="skip", choices=["skip", "overwrite", "rename"],
                       help="同名不同内容处理策略: skip(默认安全) | overwrite(覆盖前自动备份) | rename")
    sk_mg.add_argument("--no-normalize", action="store_true", help="保持原 SKILL.md 不规范化 frontmatter")
    sk_mg.add_argument("--workspace", help="当 --to workspace 时指定目标项目根目录")
    sk_mg.add_argument("--apply", action="store_true", help="真实写入目标目录 (默认 dry-run)")
    sk_mg.add_argument("--json", action="store_true", help="输出 JSON")
    sk_mg.set_defaults(func=cmd_skills)

    sk_in = sksub.add_parser("inspect", help="预览 Skill 内容并与目标已有版本对比 Diff")
    sk_in.add_argument("--source", default="zcode", choices=["zcode", "claude", "codex", "antigravity", "cursor", "dsh", "all"])
    sk_in.add_argument("--id", dest="skill_id", required=True, help="Skill id 或 slug")
    sk_in.add_argument("--target", default="", choices=["", "antigravity", "claude", "codex", "cursor", "zcode", "dsh", "workspace"])
    sk_in.add_argument("--agy-mode", default="skills", choices=["skills", "plugin"])
    sk_in.add_argument("--plugin-name", default="")
    sk_in.add_argument("--include-plugins", action="store_true")
    sk_in.add_argument("--no-normalize", action="store_true")
    sk_in.add_argument("--workspace")
    sk_in.add_argument("--json", action="store_true")
    sk_in.set_defaults(func=cmd_skills)

    sk_rm = sksub.add_parser("remove", help="安全移除目标端已安装的 Skill (写入前自动完整备份)")
    sk_rm.add_argument("--target", required=True, choices=["antigravity", "claude", "codex", "cursor", "zcode", "dsh", "workspace"])
    sk_rm.add_argument("--slugs", required=True, help="逗号分隔的 Skill 目录名")
    sk_rm.add_argument("--agy-mode", default="skills", choices=["skills", "plugin"])
    sk_rm.add_argument("--plugin-name", default="")
    sk_rm.add_argument("--workspace")
    sk_rm.add_argument("--apply", action="store_true", help="执行真实移除 (默认 dry-run)")
    sk_rm.add_argument("--json", action="store_true")
    sk_rm.set_defaults(func=cmd_skills)

    sk_bk = sksub.add_parser("backups", help="列出历史 Skill 覆盖/移除备份")
    sk_bk.add_argument("--json", action="store_true")
    sk_bk.set_defaults(func=cmd_skills)

    sk_rb = sksub.add_parser("rollback", help="从指定备份 ID 恢复 Skill")
    sk_rb.add_argument("backup_id")
    sk_rb.add_argument("--apply", action="store_true", help="执行真实恢复 (默认 dry-run)")
    sk_rb.add_argument("--json", action="store_true")
    sk_rb.set_defaults(func=cmd_skills)


    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (SnapshotNotFound, BadMigrateTarget) as e:
        # CLI 边界统一转进程退出 (领域层抛可捕获异常, HTTP 层映射 404/400, 这里兜底)
        raise SystemExit(str(e))


if __name__ == "__main__":
    sys.modules.setdefault("cv", sys.modules[__name__])
    main()
