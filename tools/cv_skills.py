"""跨工具 Agent Skill 技能盘点、体检与批量迁移引擎 (纯 Python 3.9+ 标准库, 零第三方依赖)。

铁律与会话迁移完全一致:
1. 源 Skill 目录严格只读, 绝不修改或删除任何源端文件;
2. 默认 dry-run 产出完整诚实报告 (重名冲突 / Frontmatter 规范化 / 硬编码源路径告警 / System Prompt Token 预估),
   显式 apply=True 才写入目标目录;
3. 迁往 Antigravity 支持两种落盘模式:
   - agy_mode="skills": 直写全局 ~/.gemini/config/skills/<slug>/
   - agy_mode="plugin": 打包为可一键开关的插件 ~/.gemini/config/plugins/<plugin_name>/ (含官方 plugin.json + skills/)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

ALL_AGENTS = ("zcode", "claude", "codex", "antigravity", "cursor")
VALID_TARGETS = ("antigravity", "claude", "codex", "cursor", "zcode", "workspace")
VALID_CONFLICTS = ("skip", "overwrite", "rename")
VALID_AGY_MODES = ("skills", "plugin")

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", ".svn", "node_modules", ".pytest_cache"})
_SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})

# 源端硬编码路径/宏提示 (延续 Context Vault "诚实迁移报告" 基因)
_HARDCODE_PATTERNS = [
    (re.compile(r"~[/\\]\.zcode\b|\.zcode[/\\]", re.I), "引用了 ~/.zcode 路径"),
    (re.compile(r"~[/\\]\.claude\b|\.claude[/\\]", re.I), "引用了 ~/.claude 路径"),
    (re.compile(r"~[/\\]\.codex\b|\.codex[/\\]", re.I), "引用了 ~/.codex 路径"),
    (re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?", re.I), "引用了 ${CLAUDE_PLUGIN_ROOT} 变量"),
    (re.compile(r"~[/\\]\.cursor\b|\.cursor[/\\]", re.I), "引用了 ~/.cursor 路径"),
]


def _home() -> Path:
    import cv
    return Path(cv.HOME)


def _slugify(raw: str, fallback: str = "skill") -> str:
    """将 Skill 名称或目录名规范化为安全的小写连字符 slug (防路径穿越)。"""
    s = str(raw or "").strip()
    s = s.replace("\\", "/").split("/")[-1]
    s = re.sub(r"[\s_]+", "-", s).lower()
    # 保留 ASCII 字母数字与连字符; 若全为非 ASCII (如纯中文目录名), 保留安全中文字符
    ascii_part = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    if ascii_part:
        return ascii_part[:80]
    safe_uni = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "-", s).strip("-.")
    return (safe_uni[:64] if safe_uni else fallback)


def _safe_plugin_name(raw: str, source: str = "zcode") -> str:
    default = f"migrated-from-{_slugify(source, 'agent')}"
    cand = _slugify(raw or default, default)
    return cand or default


# ---------------------------------------------------------------- YAML Frontmatter 零依赖解析

def parse_frontmatter(text: str) -> tuple[dict, str, bool]:
    """解析 SKILL.md 顶部的 --- ... --- YAML frontmatter (零 PyYAML 依赖)。
    返回 (meta_dict, body_text, has_frontmatter)。"""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    if not norm.startswith("---\n") and norm != "---":
        return {}, text, False
    lines = norm.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text, False

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")
    meta: dict = {}
    i = 0
    while i < len(fm_lines):
        ln = fm_lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", ln)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", ">-", "|", "|-"):
            fold = val.startswith(">")
            buf = []
            i += 1
            while i < len(fm_lines):
                sub = fm_lines[i]
                if sub.startswith((" ", "\t")) or not sub.strip():
                    buf.append(sub.strip() if fold else sub.lstrip(" "))
                    i += 1
                else:
                    break
            joined = " ".join(x for x in buf if x) if fold else "\n".join(buf).strip()
            meta[key] = joined.strip()
            continue
        if not val:
            # 可能是缩进子块 (如 metadata:)
            sub_lines = []
            i += 1
            while i < len(fm_lines):
                sub = fm_lines[i]
                if sub.startswith((" ", "\t")) or not sub.strip():
                    sub_lines.append(sub)
                    i += 1
                else:
                    break
            meta[key] = "\n".join(sub_lines).strip()
            continue
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        meta[key] = val
        i += 1
    return meta, body, True


def _extract_fallback_description(body: str, slug: str) -> str:
    """当源 SKILL.md 缺失 description 时, 从正文首段/标题提取可读描述。"""
    for line in body.replace("\r\n", "\n").splitlines():
        s = line.strip()
        if not s or s.startswith("```") or s.startswith("---"):
            continue
        s = re.sub(r"^#+\s*", "", s).strip()
        if len(s) >= 4:
            return s[:220]
    return f"Migrated skill: {slug}"


def normalize_skill_md(text: str, target_slug: str) -> tuple[str, list[str]]:
    """无损规范化目标副本的 SKILL.md frontmatter:
    - 确保存在合法 YAML frontmatter;
    - 将含大写/空格的 name 规范化为小写连字符 slug (符合 Antigravity / Codex 要求);
    - 补齐缺失的 description;
    - 完整保留正文内容与非冲突扩展字段。返回 (new_text, rewrites_list)。"""
    meta, body, has_fm = parse_frontmatter(text)
    rewrites: list[str] = []
    orig_name = str(meta.get("name") or "").strip()
    orig_desc = str(meta.get("description") or "").strip()

    need_rewrite = False
    if not has_fm:
        need_rewrite = True
        rewrites.append("补齐缺失的 YAML frontmatter")
    if not orig_name:
        need_rewrite = True
        rewrites.append(f"补齐 name: {target_slug}")
    elif orig_name != target_slug:
        need_rewrite = True
        rewrites.append(f"规范化 name: {orig_name!r} -> {target_slug!r}")

    desc = orig_desc
    if not desc:
        desc = _extract_fallback_description(body, target_slug)
        need_rewrite = True
        rewrites.append("从正文自动补齐 description")

    if not need_rewrite:
        return text, []

    # 用 JSON 双引号安全转义单行字符串 (YAML 1.2 完全兼容 JSON 字符串字面量, 零依赖且不怕冒号/引号)
    safe_desc = json.dumps(desc.replace("\n", " ").strip(), ensure_ascii=False)
    fm_out = [
        "---",
        f"name: {target_slug}",
        f"description: {safe_desc}",
    ]
    if meta.get("version"):
        fm_out.append(f"version: {json.dumps(str(meta['version']), ensure_ascii=False)}")
    if meta.get("license"):
        fm_out.append(f"license: {json.dumps(str(meta['license']), ensure_ascii=False)}")
    fm_out.append("---")
    new_body = body if has_fm else text.lstrip("\ufeff\r\n")
    return "\n".join(fm_out) + "\n\n" + new_body, rewrites


# ---------------------------------------------------------------- 扫描与指纹

def _dir_digest(root: Path, skill_md_override: bytes | None = None) -> tuple[str, int, int]:
    """计算 Skill 目录内容指纹 (sha256[:16]), 同时返回 (file_count, total_bytes)。"""
    h = hashlib.sha256()
    n_files = 0
    total_bytes = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIR_NAMES)
        for fn in sorted(files):
            if fn in _SKIP_FILE_NAMES:
                continue
            fp = Path(cur) / fn
            rel = fp.relative_to(root).as_posix()
            try:
                data = skill_md_override if (skill_md_override is not None and rel == "SKILL.md") else fp.read_bytes()
            except OSError:
                continue
            if rel.lower().endswith(".md"):
                data = data.replace(b"\r\n", b"\n")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(data)
            h.update(b"\0")
            n_files += 1
            total_bytes += len(data)
    return h.hexdigest()[:16], n_files, total_bytes


def _estimate_prompt_tokens(name: str, description: str) -> int:
    """估算该 Skill 在 Agent progressive disclosure 中常驻 system prompt 的 token 成本。"""
    s = f"{name}: {description}"
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cjk
    return max(8, int(cjk * 1.1 + other / 4.0) + 6)


def _scan_warnings(skill_dir: Path, text: str, meta: dict, source: str) -> list[str]:
    warns: list[str] = []
    if not str(meta.get("description") or "").strip():
        warns.append("缺少 description（迁移时将自动补齐）")
    elif len(str(meta.get("description") or "")) > 1200:
        warns.append("description 较长（>1200 字符，较占系统提示词）")
    seen_w = set()
    # 扫描 SKILL.md 与 scripts/ 下文本文件 (限 64KB, 快速无阻塞)
    scan_texts = [text]
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        for fp in sorted(scripts_dir.rglob("*"))[:12]:
            if fp.is_file() and fp.stat().st_size <= 65536 and fp.suffix.lower() in (".py", ".sh", ".ps1", ".js", ".ts", ".md"):
                try:
                    scan_texts.append(fp.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    joined = "\n".join(scan_texts)
    for pat, label in _HARDCODE_PATTERNS:
        if pat.search(joined) and label not in seen_w:
            seen_w.add(label)
            warns.append(label)
    return warns


def _source_roots(agent: str, include_plugins: bool = False) -> list[tuple[str, Path]]:
    """返回 [(scope_kind, root_dir), ...], scope_kind 为 'standalone' 或 'plugin'。"""
    h = _home()
    out: list[tuple[str, Path]] = []
    if agent == "zcode":
        out.append(("standalone", h / ".zcode" / "skills"))
        if include_plugins:
            out.append(("plugin", h / ".zcode" / "cli" / "plugins" / "cache"))
    elif agent == "claude":
        out.append(("standalone", h / ".claude" / "skills"))
        if include_plugins:
            out.append(("plugin", h / ".claude" / "plugins" / "cache"))
    elif agent == "codex":
        out.append(("standalone", h / ".codex" / "skills"))
    elif agent == "antigravity":
        out.append(("standalone", h / ".gemini" / "config" / "skills"))
        if include_plugins:
            out.append(("plugin", h / ".gemini" / "config" / "plugins"))
    elif agent == "cursor":
        out.append(("standalone", h / ".cursor" / "skills"))
    return out


def resolve_target_root(
    target: str,
    *,
    agy_mode: str = "skills",
    plugin_name: str = "",
    source_hint: str = "zcode",
    workspace_dir: Path | None = None,
) -> tuple[Path, Path | None, str]:
    """解析目标写入根目录。返回 (skills_dest_root, plugin_dir_or_none, resolved_plugin_name)。"""
    if target not in VALID_TARGETS:
        raise ValueError(f"不支持的目标: {target} (可选: {', '.join(VALID_TARGETS)})")
    h = _home()
    if target == "antigravity":
        if agy_mode not in VALID_AGY_MODES:
            raise ValueError(f"不支持的 agy_mode: {agy_mode} (可选: skills | plugin)")
        if agy_mode == "plugin":
            pname = _safe_plugin_name(plugin_name, source_hint)
            pdir = h / ".gemini" / "config" / "plugins" / pname
            return pdir / "skills", pdir, pname
        return h / ".gemini" / "config" / "skills", None, ""
    if target == "claude":
        return h / ".claude" / "skills", None, ""
    if target == "codex":
        return h / ".codex" / "skills", None, ""
    if target == "cursor":
        return h / ".cursor" / "skills", None, ""
    if target == "zcode":
        return h / ".zcode" / "skills", None, ""
    ws = Path(workspace_dir).expanduser().resolve() if workspace_dir else Path.cwd().resolve()
    return ws / ".agents" / "skills", None, ""


def _iter_skill_files(root: Path, skip_dot_dirs: bool = True):
    if not root.is_dir():
        return
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs
            if d not in _SKIP_DIR_NAMES and (not skip_dot_dirs or not d.startswith("."))
        )
        if "SKILL.md" in files:
            yield Path(cur) / "SKILL.md"
            # 找到一个 skill 目录后不再向其内部子目录递归找嵌套 SKILL.md (避免把 examples/ 里的模板误当独立 skill)
            dirs[:] = []


def discover_skills(
    source: str = "zcode",
    *,
    include_plugins: bool = False,
) -> list[dict]:
    """扫描指定源（或 all）下的全部 Skill，返回结构化元数据列表。"""
    agents = list(ALL_AGENTS) if source == "all" else [source]
    for a in agents:
        if a not in ALL_AGENTS:
            raise ValueError(f"不支持的来源: {a} (可选: {', '.join(ALL_AGENTS)}, all)")
    found: list[dict] = []
    seen_paths: set[str] = set()
    for ag in agents:
        for scope, root in _source_roots(ag, include_plugins=include_plugins):
            for smd in _iter_skill_files(root, skip_dot_dirs=(ag == "codex")):
                sdir = smd.parent
                key_path = str(sdir.resolve()).lower()
                if key_path in seen_paths:
                    continue
                seen_paths.add(key_path)
                try:
                    raw_text = smd.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta, body, has_fm = parse_frontmatter(raw_text)
                dir_slug = _slugify(sdir.name, "skill")
                meta_name = str(meta.get("name") or "").strip()
                slug = dir_slug
                desc = str(meta.get("description") or "").strip() or _extract_fallback_description(body, slug)
                rel = sdir.relative_to(root).as_posix()
                # 提取所属分类/插件名 (如 xiaohongshu-skills / 01-内容创作)
                parts = rel.split("/")
                group = parts[0] if (scope == "plugin" and len(parts) >= 2) else ("standalone" if scope == "standalone" else "plugin")
                digest, n_files, total_bytes = _dir_digest(sdir)
                norm_text, rewrites = normalize_skill_md(raw_text, slug)
                norm_digest, _, _ = _dir_digest(sdir, skill_md_override=norm_text.encode("utf-8")) if rewrites else (digest, n_files, total_bytes)
                warnings = _scan_warnings(sdir, raw_text, meta, ag)
                ptok = _estimate_prompt_tokens(slug, desc)
                sub_dirs = sorted(d.name for d in sdir.iterdir() if d.is_dir() and d.name not in _SKIP_DIR_NAMES)
                sid = f"{ag}:{scope}:{rel}"
                found.append({
                    "id": sid,
                    "source": ag,
                    "scope": scope,
                    "group": group,
                    "slug": slug,
                    "raw_name": meta_name or sdir.name,
                    "description": desc,
                    "version": str(meta.get("version") or ""),
                    "path": str(sdir),
                    "rel_path": rel,
                    "digest": digest,
                    "norm_digest": norm_digest,
                    "files": n_files,
                    "size_kb": round(total_bytes / 1024.0, 1),
                    "sub_dirs": sub_dirs,
                    "prompt_tokens": ptok,
                    "has_frontmatter": has_fm,
                    "rewrites": rewrites,
                    "warnings": warnings,
                })
    found.sort(key=lambda x: (0 if x["scope"] == "standalone" else 1, x["source"], x["slug"], x["rel_path"]))
    return found


def source_summary() -> list[dict]:
    """快速统计各端 Skill 数量 (供控制台顶部来源切换胶囊展示)。"""
    out = []
    for ag in ALL_AGENTS:
        st_roots = _source_roots(ag, include_plugins=False)
        pl_roots = [r for r in _source_roots(ag, include_plugins=True) if r[0] == "plugin"]
        n_st = sum(1 for _, r in st_roots for _ in _iter_skill_files(r, skip_dot_dirs=(ag == "codex")))
        n_pl = sum(1 for _, r in pl_roots for _ in _iter_skill_files(r, skip_dot_dirs=True))
        out.append({
            "agent": ag,
            "standalone": n_st,
            "plugins": n_pl,
            "total": n_st + n_pl,
            "exists": any(r.is_dir() for _, r in st_roots + pl_roots),
        })
    return out


def _target_existing_digests(dest_root: Path) -> dict[str, tuple[str, Path]]:
    """读取目标目录下已存在的 skill_slug -> (digest, path)。"""
    out: dict[str, tuple[str, Path]] = {}
    if not dest_root.is_dir():
        return out
    for d in sorted(dest_root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / "SKILL.md").is_file():
            dig, _, _ = _dir_digest(d)
            out[d.name] = (dig, d)
    return out


def list_skills_api(
    source: str = "zcode",
    *,
    include_plugins: bool = False,
    target: str = "antigravity",
    agy_mode: str = "skills",
    plugin_name: str = "",
    workspace_dir: Path | None = None,
) -> dict:
    """供 Web 控制台与 CLI 使用的盘点接口: 返回源 Skill 列表 + 相对目标的状态 (new / identical / conflict)。"""
    skills = discover_skills(source, include_plugins=include_plugins)
    dest_root, plugin_dir, resolved_pname = resolve_target_root(
        target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=source, workspace_dir=workspace_dir
    )
    existing = _target_existing_digests(dest_root)
    for sk in skills:
        ex = existing.get(sk["slug"])
        if ex is None:
            sk["target_status"] = "new"
        elif ex[0] in (sk["digest"], sk["norm_digest"]):
            sk["target_status"] = "identical"
        else:
            sk["target_status"] = "conflict"
    return {
        "source": source,
        "include_plugins": bool(include_plugins),
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else None,
        "plugin_name": resolved_pname,
        "dest_root": str(dest_root),
        "plugin_dir": str(plugin_dir) if plugin_dir else None,
        "sources": source_summary(),
        "total": len(skills),
        "skills": skills,
    }


def _copy_skill_dir(src: Path, dst: Path, *, normalize: bool, target_slug: str) -> list[str]:
    """将单个 Skill 目录只读复制到目标目录 (排除缓存目录); 按需规范化副本的 SKILL.md。"""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    rewrites: list[str] = []
    for cur, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIR_NAMES)
        rel_dir = Path(cur).relative_to(src)
        out_dir = dst / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        for fn in sorted(files):
            if fn in _SKIP_FILE_NAMES:
                continue
            sp = Path(cur) / fn
            dp = out_dir / fn
            if rel_dir == Path(".") and fn == "SKILL.md" and normalize:
                raw = sp.read_text(encoding="utf-8", errors="replace")
                new_txt, rw = normalize_skill_md(raw, target_slug)
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
) -> dict:
    """批量迁移 Skill (默认 dry-run; apply=True 才写入目标目录)。"""
    if conflict not in VALID_CONFLICTS:
        raise ValueError(f"不支持的冲突策略: {conflict} (可选: {', '.join(VALID_CONFLICTS)})")
    all_skills = discover_skills(source, include_plugins=include_plugins)
    if selected is not None:
        want = {str(x).strip() for x in selected if str(x).strip()}
        chosen = [s for s in all_skills if s["id"] in want or s["slug"] in want or s["rel_path"] in want]
    else:
        chosen = list(all_skills)

    dest_root, plugin_dir, resolved_pname = resolve_target_root(
        target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=source, workspace_dir=workspace_dir
    )
    existing = _target_existing_digests(dest_root)
    reserved_slugs: set[str] = set()
    backup_dir: Path | None = None
    if apply and conflict == "overwrite" and vault is not None:
        backup_dir = Path(vault) / "backups" / f"skills-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"

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
            while f"{base_slug}-{idx}" in reserved_slugs or f"{base_slug}-{idx}" in existing:
                idx += 1
            target_slug = f"{base_slug}-{idx}"

        ex = existing.get(target_slug)
        cmp_dig = sk["norm_digest"] if normalize else sk["digest"]
        action = "copy"
        if ex is not None:
            if ex[0] in (sk["digest"], cmp_dig):
                action = "identical"
            elif conflict == "skip":
                action = "conflict_skip"
            elif conflict == "overwrite":
                action = "overwrite"
            elif conflict == "rename":
                idx = 2
                while f"{base_slug}-{idx}" in existing or f"{base_slug}-{idx}" in reserved_slugs:
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
                rw = _copy_skill_dir(Path(sk["path"]), dst, normalize=normalize, target_slug=target_slug)
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

    budget_warning = ""
    if counts["planned_copy"] > 30 or prompt_tokens_est > 4000:
        budget_warning = (
            f"本次将新增 {counts['planned_copy']} 个 Skill（预计常驻 System Prompt 约 {prompt_tokens_est:,} tokens），"
            "过多 Skill 描述会占用上下文并干扰路由，建议按需勾选常用技能，或使用 Antigravity 插件模式便于随时整体开关。"
        )

    return {
        "applied": bool(apply),
        "source": source,
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else None,
        "plugin_name": resolved_pname,
        "dest_root": str(dest_root),
        "plugin_manifest": manifest_path,
        "backup_dir": str(backup_dir) if (backup_dir and backup_dir.exists()) else None,
        "conflict": conflict,
        "normalize": bool(normalize),
        "counts": counts,
        "prompt_tokens_est": prompt_tokens_est,
        "budget_warning": budget_warning,
        "items": items,
    }


# ---------------------------------------------------------------- CLI 入口

def cmd_skills(args):
    sub = getattr(args, "skills_cmd", None) or "list"
    if sub == "list":
        res = list_skills_api(
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
        import cv
        vault = cv.vault_dir(args)
        sel = [x.strip() for x in args.only.split(",") if x.strip()] if getattr(args, "only", None) else None
        ws = Path(args.workspace).expanduser() if getattr(args, "workspace", None) else None
        res = migrate_skills(
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
