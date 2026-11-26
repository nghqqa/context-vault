"""顶层主对话归组、ZCode 子代理/已归档会话过滤与按项目筛选。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cv_core import cv_mod as _cv


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
    if nc == nq or base == nq or nq in nc:
        return True
    if "/" not in nc and "/" in nq:
        return nq.rsplit("/", 1)[-1] == nc
    return False


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
                f"SELECT {id_col} FROM {table} WHERE {' OR '.join(conds)}"
            ) if r[0]}
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
    return _zcode_refs_matching(_cv().ZCODE_DB, "session", "id", [
        ("parent_id", "parent_id IS NOT NULL"),
    ])


def _zcode_archived_refs() -> set[str]:
    """ZCode 已归档/已删除主会话 id：兼容 CLI session 表、桌面端 v2 tasks 及已删项目。"""
    c = _cv()
    return (
        _zcode_refs_matching(c.ZCODE_DB, "session", "id", [
            ("time_deleted", "time_deleted IS NOT NULL"),
            ("time_archived", "time_archived IS NOT NULL"),
        ])
        | _zcode_refs_matching(c.ZCODE_TASKS_DB, "tasks", "task_id", [
            ("archived", "COALESCE(archived, 0) != 0"),
            ("deleted", "COALESCE(deleted, 0) != 0"),
            ("cli_deleted", "COALESCE(cli_deleted, 0) != 0"),
        ])
        | _zcode_deleted_project_task_refs(c.ZCODE_TASKS_DB)
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
    """解析 vault 中每个会话最新的顶层主对话（叶子去重 + 过滤 ZCode 子代理与归档会话）。"""
    c = _cv()
    conn = c.db(vault)
    try:
        try:
            parents = {r[0] for r in conn.execute(
                "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL"
            )}
            q = (
                "SELECT id, agent, session_ref, originator, blocks_total, created_at, path,"
                " cwd, parent_session_ref, summary_title FROM snapshots"
            )
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
            title = c._derive_summary_title(env) or raw_orig
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
            wconn = c.db(vault)
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
    exact = [
        it for it in items
        if (it.get("cwd") or "").replace("\\", "/").rstrip("/").lower() == nq
    ]
    if exact:
        return exact
    base_exact = [
        it for it in items
        if (it.get("cwd") or "").replace("\\", "/").rstrip("/").lower().rsplit("/", 1)[-1] == nq
    ]
    return base_exact or items


def _resolve_project_items(
    vault: Path,
    project: str,
    include_subagents: bool = False,
    limit: int = 0,
    include_archived: bool = False,
    agent: str | None = None,
) -> list[dict]:
    """按项目筛选顶层主对话；存在完整路径或目录名精确命中时优先精确结果。"""
    if project == "__ungrouped__":
        items = [
            it for it in resolve_top_conversations(
                vault, agent=agent, include_subagents=include_subagents,
                include_archived=include_archived,
            )
            if _is_ungrouped_cwd(it["cwd"])
        ]
    else:
        items = resolve_top_conversations(
            vault, project=project, agent=agent,
            include_subagents=include_subagents, include_archived=include_archived,
        )
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
    c = _cv()
    if project == "__ungrouped__":
        all_items = resolve_top_conversations(
            vault, agent=agent, include_subagents=include_subagents,
            include_archived=include_archived,
        )
        items = [it for it in all_items if _is_ungrouped_cwd(it["cwd"])]
    else:
        items = resolve_top_conversations(
            vault, project=project, agent=agent,
            include_subagents=include_subagents, include_archived=include_archived,
        )

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
        conn = c.db(vault)
        try:
            available_agents = [
                str(r[0]).strip().lower()
                for r in conn.execute(
                    "SELECT agent, COUNT(*) AS n FROM snapshots WHERE agent IS NOT NULL AND agent != '' "
                    "GROUP BY lower(agent) ORDER BY n DESC"
                ).fetchall()
                if r[0]
            ]
        except sqlite3.OperationalError:
            available_agents = []
        finally:
            conn.close()
    except Exception:
        available_agents = []
    return {
        "total_projects": len(projects),
        "total_conversations": len(items),
        "available_agents": available_agents,
        "projects": projects,
    }
