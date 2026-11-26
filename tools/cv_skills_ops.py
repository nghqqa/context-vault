"""Agent Skill 写入迁移、单技能对比/移除、备份回滚与 CLI 调度 (纯 Python 3.9+ stdlib)。"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import shutil
from pathlib import Path

from cv_core import vault_dir as _vault_dir


def _sk():
    """延迟解析 cv_skills 模块，避免循环导入并保留对测试 monkeypatch 的兼容。"""
    import cv_skills
    return cv_skills


def _copy_skill_dir(src: Path, dst: Path, *, normalize: bool, target_slug: str) -> list[str]:
    """将单个 Skill 目录只读复制到目标目录 (排除缓存目录); 按需规范化副本的 SKILL.md。"""
    skm = _sk()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    rewrites: list[str] = []
    for cur, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in skm._SKIP_DIR_NAMES)
        rel_dir = Path(cur).relative_to(src)
        out_dir = dst / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        for fn in sorted(files):
            if fn in skm._SKIP_FILE_NAMES:
                continue
            sp = Path(cur) / fn
            dp = out_dir / fn
            if rel_dir == Path(".") and fn == "SKILL.md" and normalize:
                raw = sp.read_text(encoding="utf-8", errors="replace")
                new_txt, rw = skm.normalize_skill_md(raw, target_slug)
                dp.write_bytes(new_txt.replace("\r\n", "\n").encode("utf-8"))
                rewrites = rw
            else:
                shutil.copy2(sp, dp)
    return rewrites


def _write_agy_plugin_manifest(plugin_dir: Path, plugin_name: str, sources: set[str], count: int) -> Path:
    """按 Antigravity 官方 plugins.md / migrate-agent-settings 规范写入 plugin.json。"""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    src_label = ", ".join(sorted(sources)) or "external agents"
    manifest = {
        "name": plugin_name,
        "displayName": f"Migrated Skills ({src_label})",
        "version": "1.0.0",
        "description": f"{count} skills migrated from {src_label} via Context Vault.",
    }
    mp = plugin_dir / "plugin.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return mp


def migrate_skills(
    *,
    source: str = "zcode",
    target: str = "antigravity",
    selected: list[str] | None = None,
    include_plugins: bool = False,
    agy_mode: str = "skills",
    plugin_name: str = "",
    conflict: str = "skip",
    normalize: bool = True,
    workspace_dir: Path | None = None,
    apply: bool = False,
    vault: Path | None = None,
    connected_only: bool = False,
) -> dict:
    """批量迁移 Skill (默认 dry-run; apply=True 才写入目标目录)。"""
    skm = _sk()
    if conflict not in skm.VALID_CONFLICTS:
        raise ValueError(f"不支持的冲突策略: {conflict} (可选: {', '.join(skm.VALID_CONFLICTS)})")
    conn_map = skm._connected_skill_agents(vault) if connected_only else None
    if connected_only and conn_map is not None:
        if source != "all" and not conn_map.get(source, False):
            raise ValueError(f"源客户端 {source} 尚未在「设置与运维」中接入后端，无法迁移 Skill")
        if target != "workspace" and not conn_map.get(target, False):
            raise ValueError(f"目标客户端 {target} 尚未在「设置与运维」中接入后端，无法迁移 Skill")
    all_skills = skm._discover_skills_internal(source, include_plugins=include_plugins)
    if connected_only and source == "all" and conn_map is not None:
        all_skills = [s for s in all_skills if conn_map.get(s.get("source"), False)]
    if selected is not None:
        want = {str(x).strip() for x in selected if str(x).strip()}
        chosen = [s for s in all_skills if s["id"] in want or s["slug"] in want or s["rel_path"] in want]
    else:
        chosen = list(all_skills)

    dest_root, plugin_dir, resolved_pname = skm.resolve_target_root(
        target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=source, workspace_dir=workspace_dir
    )
    existing_names = skm._target_existing_names(dest_root)
    reserved_slugs: set[str] = set()
    backup_dir: Path | None = None
    backed_items: list[dict] = []
    if apply and conflict == "overwrite" and vault is not None:
        backup_dir = skm._make_skill_backup_dir(Path(vault), "overwrite")

    items: list[dict] = []
    counts = {
        "selected": len(chosen),
        "planned_copy": 0,
        "migrated": 0,
        "skipped_identical": 0,
        "skipped_conflict": 0,
        "overwritten": 0,
        "renamed": 0,
        "warnings": 0,
        "rewrites": 0,
    }
    used_sources: set[str] = set()
    prompt_tokens_est = 0

    for sk in chosen:
        base_slug = sk["slug"]
        target_slug = base_slug
        # 同一批次内出现同名 Skill (例如插件展平撞名) 自动附加序号区分
        if target_slug in reserved_slugs:
            idx = 2
            while f"{base_slug}-{idx}" in reserved_slugs or f"{base_slug}-{idx}" in existing_names:
                idx += 1
            target_slug = f"{base_slug}-{idx}"

        action = "copy"
        if target_slug in existing_names:
            if skm._target_match_skill(dest_root / target_slug, sk, normalize=normalize):
                action = "identical"
            elif conflict == "skip":
                action = "conflict_skip"
            elif conflict == "overwrite":
                action = "overwrite"
            elif conflict == "rename":
                idx = 2
                while f"{base_slug}-{idx}" in existing_names or f"{base_slug}-{idx}" in reserved_slugs:
                    idx += 1
                target_slug = f"{base_slug}-{idx}"
                action = "rename"

        dst = dest_root / target_slug
        # 防止源目录与目标目录指向同一物理路径 (自拷贝摧毁源文件!)
        try:
            if Path(sk["path"]).resolve() == dst.resolve():
                action = "identical"
        except OSError:
            pass

        reserved_slugs.add(target_slug)
        rw = sk["rewrites"] if normalize else []
        if target_slug != sk["slug"] and normalize:
            rw = list(rw) + [f"目标目录重命名为 {target_slug}"]

        if action == "identical":
            counts["skipped_identical"] += 1
        elif action == "conflict_skip":
            counts["skipped_conflict"] += 1
        else:
            counts["planned_copy"] += 1
            prompt_tokens_est += sk["prompt_tokens"]
            used_sources.add(sk["source"])
            if apply:
                if action == "overwrite" and dst.exists() and backup_dir is not None:
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(dst, backup_dir / target_slug, dirs_exist_ok=True)
                    backed_items.append({"slug": target_slug, "dest_path": str(dst)})
                rw = _copy_skill_dir(Path(sk["path"]), dst, normalize=normalize, target_slug=target_slug)
                existing_names.add(target_slug)
                counts["migrated"] += 1
                if action == "overwrite":
                    counts["overwritten"] += 1
                elif action == "rename":
                    counts["renamed"] += 1

        if sk["warnings"]:
            counts["warnings"] += 1
        if rw:
            counts["rewrites"] += 1

        items.append({
            "id": sk["id"],
            "source": sk["source"],
            "scope": sk["scope"],
            "slug": sk["slug"],
            "target_slug": target_slug,
            "action": action,
            "source_path": sk["path"],
            "dest_path": str(dst),
            "files": sk["files"],
            "size_kb": sk["size_kb"],
            "prompt_tokens": sk["prompt_tokens"],
            "rewrites": rw,
            "warnings": sk["warnings"],
        })

    manifest_path = None
    if plugin_dir is not None:
        manifest_path = str(plugin_dir / "plugin.json")
        if apply and (counts["migrated"] > 0 or (dest_root.is_dir() and any(dest_root.iterdir()))):
            total_in_plugin = sum(1 for d in dest_root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            _write_agy_plugin_manifest(plugin_dir, resolved_pname, used_sources or {source}, total_in_plugin)

    if apply and counts["migrated"] > 0:
        with skm._CACHE_LOCK:
            skm._SUMMARY_CACHE.clear()

    if backup_dir is not None and backed_items:
        skm._write_skill_backup_manifest(
            backup_dir, "overwrite", source=source, target=target,
            agy_mode=agy_mode, plugin_name=resolved_pname,
            dest_root=dest_root, workspace_dir=workspace_dir, items=backed_items,
        )

    budget_warning = ""
    if counts["planned_copy"] > 30 or prompt_tokens_est > 4000:
        budget_warning = (
            f"本次将新增 {counts['planned_copy']} 个 Skill（预计常驻 System Prompt 约 {prompt_tokens_est:,} tokens），"
            "过多 Skill 描述会占用上下文并干扰路由，建议按需勾选常用技能，或使用 Antigravity 插件模式便于随时整体开关。"
        )

    has_backup = bool(backup_dir and backup_dir.exists() and backed_items)
    return {
        "applied": bool(apply),
        "source": source,
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else None,
        "plugin_name": resolved_pname,
        "dest_root": str(dest_root),
        "plugin_manifest": manifest_path,
        "backup_dir": str(backup_dir) if has_backup else None,
        "backup_id": backup_dir.name if has_backup else None,
        "conflict": conflict,
        "normalize": bool(normalize),
        "counts": counts,
        "prompt_tokens_est": prompt_tokens_est,
        "budget_warning": budget_warning,
        "items": items,
    }


def _read_truncated_text(p: Path, limit: int | None = None) -> tuple[str, bool]:
    cap = _sk()._INSPECT_MAX_CHARS if limit is None else limit
    txt = p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    if len(txt) > cap:
        return txt[:cap], True
    return txt, False


def inspect_skill_api(
    source: str,
    skill_id: str,
    *,
    target: str = "",
    agy_mode: str = "skills",
    plugin_name: str = "",
    include_plugins: bool = False,
    normalize: bool = True,
    workspace_dir: Path | None = None,
    vault: Path | None = None,
    connected_only: bool = False,
) -> dict:
    """查看单个 Skill 内容详情，并在指定 target 下返回与目标已有版本的文本/文件差异对比。"""
    skm = _sk()
    if not str(skill_id or "").strip():
        raise ValueError("必须指定 skill_id")
    if connected_only:
        conn_map = skm._connected_skill_agents(vault)
        if source != "all" and not conn_map.get(source, False):
            raise ValueError(f"源客户端 {source} 尚未在「设置与运维」中接入后端")
    all_skills = skm._discover_skills_internal(source, include_plugins=include_plugins)
    want = str(skill_id).strip()
    sk = next(
        (s for s in all_skills if s["id"] == want or s["slug"] == want or s["rel_path"] == want),
        None,
    )
    if sk is None:
        raise ValueError(f"未在 {source} 中找到 Skill: {skill_id}")

    src_dir = Path(sk["path"])
    raw_md, raw_trunc = _read_truncated_text(src_dir / "SKILL.md")
    if normalize:
        planned_md, rewrites = skm.normalize_skill_md(raw_md, sk["slug"])
    else:
        planned_md, rewrites = raw_md, []

    src_manifest, _, _, _ = skm._scan_dir_manifest(src_dir)
    src_files = [{"rel": r, "size": sz} for r, _, sz, _ in src_manifest]

    target_info = {
        "target": target or None,
        "dest_path": None,
        "target_exists": False,
        "target_status": "new",
        "target_skill_md": "",
        "target_truncated": False,
        "target_files": [],
        "diff_lines": [],
        "diff_truncated": False,
        "files_added": [],
        "files_removed": [],
        "files_changed": [],
    }
    if target:
        dest_root, _, _ = skm.resolve_target_root(
            target, agy_mode=agy_mode, plugin_name=plugin_name,
            source_hint=sk["source"], workspace_dir=workspace_dir,
        )
        dst = dest_root / sk["slug"]
        target_info["dest_path"] = str(dst)
        if (dst / "SKILL.md").is_file():
            target_info["target_exists"] = True
            matched = skm._target_match_skill(dst, sk, normalize=normalize)
            target_info["target_status"] = "identical" if matched else "conflict"
            tgt_md, tgt_trunc = _read_truncated_text(dst / "SKILL.md")
            target_info["target_skill_md"] = tgt_md
            target_info["target_truncated"] = tgt_trunc
            diff_iter = difflib.unified_diff(
                tgt_md.splitlines(),
                planned_md.splitlines(),
                fromfile=f"目标现有 ({target}/{sk['slug']}/SKILL.md)",
                tofile=f"迁入版本 ({sk['source']}/{sk['slug']}/SKILL.md)",
                lineterm="",
                n=3,
            )
            dlines = list(diff_iter)
            if len(dlines) > skm._DIFF_MAX_LINES:
                target_info["diff_lines"] = dlines[:skm._DIFF_MAX_LINES]
                target_info["diff_truncated"] = True
            else:
                target_info["diff_lines"] = dlines
            tgt_manifest, _, _, _ = skm._scan_dir_manifest(dst)
            target_info["target_files"] = [{"rel": r, "size": sz} for r, _, sz, _ in tgt_manifest]
            src_map = {r: (p, sz, mt) for r, p, sz, mt in src_manifest}
            tgt_map = {r: (p, sz, mt) for r, p, sz, mt in tgt_manifest}
            target_info["files_added"] = sorted(r for r in src_map if r not in tgt_map)
            target_info["files_removed"] = sorted(r for r in tgt_map if r not in src_map)
            changed = []
            for r in sorted(set(src_map) & set(tgt_map)):
                if r == "SKILL.md":
                    if tgt_md.replace("\r\n", "\n") != planned_md.replace("\r\n", "\n"):
                        changed.append(r)
                else:
                    sh = skm._hash_file_entry(src_map[r][0], src_map[r][1], src_map[r][2], r.lower().endswith(".md"))
                    th = skm._hash_file_entry(tgt_map[r][0], tgt_map[r][1], tgt_map[r][2], r.lower().endswith(".md"))
                    if sh != th:
                        changed.append(r)
            target_info["files_changed"] = changed

    return {
        "skill": skm._strip_internal_fields([sk])[0],
        "source_skill_md": raw_md,
        "raw_truncated": raw_trunc,
        "planned_skill_md": planned_md,
        "normalize": bool(normalize),
        "rewrites": rewrites,
        "source_files": src_files[:200],
        **target_info,
    }


def remove_skills_api(
    *,
    target: str,
    slugs: list[str],
    agy_mode: str = "skills",
    plugin_name: str = "",
    workspace_dir: Path | None = None,
    vault: Path | None = None,
    apply: bool = False,
    connected_only: bool = False,
) -> dict:
    """安全移除目标目录下的指定 Skill (apply=True 时先完整备份再删除，禁止路径穿越)。"""
    skm = _sk()
    if connected_only and target != "workspace":
        conn_map = skm._connected_skill_agents(vault)
        if not conn_map.get(target, False):
            raise ValueError(f"目标客户端 {target} 尚未在「设置与运维」中接入后端")
    if not isinstance(slugs, list) or not slugs:
        raise ValueError("请指定至少一个要移除的 Skill slug")
    clean_slugs: list[str] = []
    seen: set[str] = set()
    for raw in slugs:
        s = skm._validate_skill_slug(raw)
        if s not in seen:
            seen.add(s)
            clean_slugs.append(s)

    dest_root, plugin_dir, resolved_pname = skm.resolve_target_root(
        target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=target, workspace_dir=workspace_dir
    )
    backup_dir: Path | None = None
    backed_items: list[dict] = []
    items: list[dict] = []
    removed = 0
    missing = 0

    for slug in clean_slugs:
        dst = skm._safe_child_dir(dest_root, slug)
        exists = (dst / "SKILL.md").is_file()
        if not exists:
            missing += 1
            items.append({"slug": slug, "dest_path": str(dst), "action": "not_found"})
            continue
        if apply:
            if vault is not None:
                if backup_dir is None:
                    backup_dir = skm._make_skill_backup_dir(Path(vault), "remove")
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(dst, backup_dir / slug, dirs_exist_ok=True)
                backed_items.append({"slug": slug, "dest_path": str(dst)})
            shutil.rmtree(dst)
            removed += 1
            items.append({"slug": slug, "dest_path": str(dst), "action": "removed"})
        else:
            items.append({"slug": slug, "dest_path": str(dst), "action": "planned_remove"})

    if backup_dir is not None and backed_items:
        skm._write_skill_backup_manifest(
            backup_dir, "remove", source=target, target=target,
            agy_mode=agy_mode, plugin_name=resolved_pname,
            dest_root=dest_root, workspace_dir=workspace_dir, items=backed_items,
        )

    if apply and removed > 0:
        if plugin_dir is not None and dest_root.is_dir():
            total_in_plugin = sum(1 for d in dest_root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            _write_agy_plugin_manifest(plugin_dir, resolved_pname, {target}, total_in_plugin)
        with skm._CACHE_LOCK:
            skm._SUMMARY_CACHE.clear()

    has_backup = bool(backup_dir and backup_dir.exists() and backed_items)
    return {
        "applied": bool(apply),
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else None,
        "plugin_name": resolved_pname,
        "dest_root": str(dest_root),
        "backup_dir": str(backup_dir) if has_backup else None,
        "backup_id": backup_dir.name if has_backup else None,
        "removed": removed,
        "missing": missing,
        "items": items,
    }


def list_skill_backups_api(vault: Path, limit: int = 20) -> list[dict]:
    """列出 vault/backups/skills-* 历史备份 (最近 limit 份，区分可一键回滚的带 manifest 备份)。"""
    broot = Path(vault) / "backups"
    if not broot.is_dir():
        return []
    dirs: list[tuple[float, Path]] = []
    try:
        with os.scandir(broot) as it:
            for ent in it:
                if ent.name.startswith("skills-") and ent.is_dir():
                    try:
                        dirs.append((ent.stat().st_mtime, Path(ent.path)))
                    except OSError:
                        continue
    except OSError:
        return []
    dirs.sort(key=lambda x: (-x[0], x[1].name), reverse=False)
    out: list[dict] = []
    for mt, bdir in dirs[:max(1, min(int(limit or 20), 100))]:
        mp = bdir / "manifest.json"
        if mp.is_file():
            try:
                m = json.loads(mp.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                m = None
            if isinstance(m, dict) and isinstance(m.get("items"), list):
                slugs = [str(it.get("slug") or "") for it in m["items"] if it.get("slug")]
                out.append({
                    "id": bdir.name,
                    "created_at": str(m.get("created_at") or dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds")),
                    "kind": str(m.get("kind") or "overwrite"),
                    "source": str(m.get("source") or ""),
                    "target": str(m.get("target") or ""),
                    "agy_mode": str(m.get("agy_mode") or ""),
                    "plugin_name": str(m.get("plugin_name") or ""),
                    "dest_root": str(m.get("dest_root") or ""),
                    "count": len(slugs),
                    "slugs": slugs,
                    "restorable": True,
                })
                continue
        legacy_slugs = sorted(
            d.name for d in bdir.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()
        )
        out.append({
            "id": bdir.name,
            "created_at": dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
            "kind": "legacy",
            "source": "",
            "target": "",
            "agy_mode": "",
            "plugin_name": "",
            "dest_root": "",
            "count": len(legacy_slugs),
            "slugs": legacy_slugs,
            "restorable": False,
        })
    return out


def rollback_skill_backup_api(vault: Path, backup_id: str, *, apply: bool = False) -> dict:
    """从指定 skills-* 备份回滚恢复原有 Skill 目录 (校验 manifest 与安全路径，防止越界)。"""
    skm = _sk()
    bid = skm._validate_backup_id(backup_id)
    broot = Path(vault) / "backups"
    bdir = skm._safe_child_dir(broot, bid)
    if not bdir.is_dir():
        raise ValueError(f"备份目录不存在: {bid}")
    mp = bdir / "manifest.json"
    if not mp.is_file():
        raise ValueError(f"备份 {bid} 缺少 manifest.json (早期无元数据备份请手动核对路径后恢复)")
    try:
        m = json.loads(mp.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        raise ValueError(f"备份清单损坏: {e}") from e
    if not isinstance(m, dict) or not m.get("target") or not isinstance(m.get("items"), list):
        raise ValueError(f"备份清单格式无效: {bid}")

    target = str(m["target"])
    agy_mode = str(m.get("agy_mode") or "skills")
    plugin_name = str(m.get("plugin_name") or "")
    ws_raw = str(m.get("workspace_dir") or "").strip()
    ws_dir = Path(ws_raw) if ws_raw else None
    source_hint = str(m.get("source") or target)
    dest_root, plugin_dir, resolved_pname = skm.resolve_target_root(
        target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=source_hint, workspace_dir=ws_dir
    )
    if m.get("dest_root") and os.path.normcase(str(Path(m["dest_root"]).resolve())) != os.path.normcase(str(dest_root.resolve())):
        raise ValueError("备份清单目标路径与安全规则校验不一致，已拒绝回滚")

    restored = 0
    missing_src = 0
    items: list[dict] = []
    for entry in m["items"]:
        if not isinstance(entry, dict) or not entry.get("slug"):
            continue
        slug = skm._validate_skill_slug(str(entry["slug"]))
        src_bak = skm._safe_child_dir(bdir, slug)
        dst = skm._safe_child_dir(dest_root, slug)
        if not src_bak.is_dir() or not (src_bak / "SKILL.md").is_file():
            missing_src += 1
            items.append({"slug": slug, "dest_path": str(dst), "action": "missing_in_backup"})
            continue
        if apply:
            dest_root.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src_bak, dst)
            restored += 1
            items.append({"slug": slug, "dest_path": str(dst), "action": "restored"})
        else:
            items.append({"slug": slug, "dest_path": str(dst), "action": "planned_restore"})

    if apply and restored > 0:
        if plugin_dir is not None and dest_root.is_dir():
            total_in_plugin = sum(1 for d in dest_root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
            _write_agy_plugin_manifest(plugin_dir, resolved_pname, {source_hint}, total_in_plugin)
        with skm._CACHE_LOCK:
            skm._SUMMARY_CACHE.clear()

    return {
        "applied": bool(apply),
        "backup_id": bid,
        "kind": str(m.get("kind") or "overwrite"),
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else None,
        "dest_root": str(dest_root),
        "restored": restored,
        "missing": missing_src,
        "items": items,
    }


# ---------------------------------------------------------------- CLI 入口

def cmd_skills(args):
    skm = _sk()
    sub = getattr(args, "skills_cmd", None) or "list"
    if sub == "list":
        res = skm.list_skills_api(
            source=args.source,
            include_plugins=bool(args.include_plugins),
            target=args.target,
            agy_mode=args.agy_mode,
            plugin_name=args.plugin_name or "",
        )
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"Skill 盘点 (source={res['source']}, include_plugins={res['include_plugins']}, target={res['target']})")
        print("  本机各端概览: " + " · ".join(
            f"{s['agent']}={s['standalone']}" + (f"(+{s['plugins']}插件)" if s['plugins'] else "")
            for s in res["sources"]
        ))
        print(f"  目标目录: {res['dest_root']}")
        if not res["skills"]:
            print("  (未发现匹配的 Skill)")
            return
        tot_tok = sum(s["prompt_tokens"] for s in res["skills"])
        print(f"  共 {res['total']} 个 Skill · 若全量迁入约占 {tot_tok:,} prompt tokens:")
        for s in res["skills"][:80]:
            st = {"new": "新增", "identical": "已一致", "conflict": "冲突"}.get(s["target_status"], s["target_status"])
            w = f" ⚠{len(s['warnings'])}" if s["warnings"] else ""
            print(f"    [{st:3s}] {s['slug']:34s} {s['source']}/{s['scope']:10s} ~{s['prompt_tokens']:3d}tok{w}")
        if res["total"] > 80:
            print(f"    … 其余 {res['total'] - 80} 个省略 (加 --json 查看全部)")
        return

    if sub == "migrate":
        vault = _vault_dir(args)
        sel = [x.strip() for x in args.only.split(",") if x.strip()] if getattr(args, "only", None) else None
        ws = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
        res = skm.migrate_skills(
            source=args.source,
            target=args.target,
            selected=sel,
            include_plugins=bool(args.include_plugins),
            agy_mode=args.agy_mode,
            plugin_name=args.plugin_name or "",
            conflict=args.conflict,
            normalize=not bool(args.no_normalize),
            workspace_dir=ws,
            apply=bool(args.apply),
            vault=vault if vault.exists() else None,
        )
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        c = res["counts"]
        mode_lbl = "已写入" if res["applied"] else "dry-run 预演"
        print(f"Skill 迁移 ({mode_lbl}): {res['source']} -> {res['target']}"
              + (f" ({res['agy_mode']})" if res["agy_mode"] else ""))
        print(f"  目标目录: {res['dest_root']}")
        if res["plugin_manifest"]:
            print(f"  插件清单: {res['plugin_manifest']}")
        print(f"  选中 {c['selected']} · 计划/写入 {c['migrated'] if res['applied'] else c['planned_copy']}"
              f" · 已一致跳过 {c['skipped_identical']} · 冲突跳过 {c['skipped_conflict']}"
              f" · 规范化改写 {c['rewrites']} · 路径/字段告警 {c['warnings']}")
        print(f"  预计新增 System Prompt 开销: ~{res['prompt_tokens_est']:,} tokens")
        if res["budget_warning"]:
            print(f"  ⚠ {res['budget_warning']}")
        for it in res["items"][:40]:
            extra = []
            if it["rewrites"]:
                extra.append("改写:" + ";".join(it["rewrites"]))
            if it["warnings"]:
                extra.append("告警:" + ";".join(it["warnings"]))
            tail = (" | " + " · ".join(extra)) if extra else ""
            print(f"    [{it['action']:13s}] {it['slug']} -> {it['target_slug']}{tail}")
        if not res["applied"] and c["planned_copy"] > 0:
            print("dry-run — 确认无误后加 --apply 执行真实写入 (源文件严格只读)")
        return

    if sub == "inspect":
        ws = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
        res = skm.inspect_skill_api(
            source=args.source,
            skill_id=args.skill_id,
            target=getattr(args, "target", "") or "",
            agy_mode=args.agy_mode,
            plugin_name=args.plugin_name or "",
            include_plugins=bool(getattr(args, "include_plugins", False)),
            normalize=not bool(getattr(args, "no_normalize", False)),
            workspace_dir=ws,
        )
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        sk = res["skill"]
        print(f"Skill 详情: {sk['slug']} ({sk['source']}/{sk['scope']}) · {sk['files']} 文件 · {sk['size_kb']} KB")
        if res["target"]:
            print(f"  对比目标: {res['dest_path']} [{res['target_status']}]")
            if res["diff_lines"]:
                for ln in res["diff_lines"][:80]:
                    print("  " + ln)
        print("\n--- SKILL.md ---")
        print(res["planned_skill_md"][:2000])
        return

    if sub == "remove":
        vault = _vault_dir(args)
        slugs = [x.strip() for x in str(args.slugs).split(",") if x.strip()]
        ws = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
        res = skm.remove_skills_api(
            target=args.target,
            slugs=slugs,
            agy_mode=args.agy_mode,
            plugin_name=args.plugin_name or "",
            workspace_dir=ws,
            vault=vault if vault.exists() else None,
            apply=bool(args.apply),
        )
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"Skill 移除 ({'已执行' if res['applied'] else 'dry-run'}): {res['dest_root']}")
        if res["backup_id"]:
            print(f"  自动备份: {res['backup_id']}")
        for it in res["items"]:
            print(f"  [{it['action']}] {it['slug']}")
        return

    if sub == "backups":
        vault = _vault_dir(args)
        bks = skm.list_skill_backups_api(vault)
        if getattr(args, "json", False):
            print(json.dumps({"backups": bks}, ensure_ascii=False, indent=2))
            return
        if not bks:
            print("暂无 Skill 备份记录 (overwrite 覆盖或 remove 移除时自动创建)")
            return
        for b in bks:
            tag = "可回滚" if b["restorable"] else "旧版仅目录"
            print(f"  {b['id']}  [{b['kind']}/{tag}]  target={b['target'] or '-'}  {b['count']} 项 ({', '.join(b['slugs'][:5])})")
        return

    if sub == "rollback":
        vault = _vault_dir(args)
        res = skm.rollback_skill_backup_api(vault, args.backup_id, apply=bool(args.apply))
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"Skill 备份回滚 ({'已恢复' if res['applied'] else 'dry-run'}): {res['backup_id']} -> {res['dest_root']}")
        for it in res["items"]:
            print(f"  [{it['action']}] {it['slug']}")
