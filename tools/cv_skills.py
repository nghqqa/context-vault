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

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from cv_core import cv_mod as _cv, vault_dir as _vault_dir

ALL_AGENTS = ("zcode", "claude", "codex", "antigravity", "cursor", "dsh")
VALID_TARGETS = ("antigravity", "claude", "codex", "cursor", "zcode", "dsh", "workspace")
VALID_CONFLICTS = ("skip", "overwrite", "rename")
VALID_AGY_MODES = ("skills", "plugin")

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", ".svn", "node_modules", ".pytest_cache"})
_SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_FAST_HASH_THRESHOLD = 131072  # 128 KiB: 以下全字节哈希, 以上二进制抽样头/中/尾+size 避免每次读取数十 MB 素材
_SAMPLE_CHUNK = 16384
_SUMMARY_TTL = 30.0
_INSPECT_MAX_CHARS = 65536
_DIFF_MAX_LINES = 400
_SKILL_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_BACKUP_ID_RE = re.compile(r"^skills-[A-Za-z0-9._-]+$")

_CACHE_LOCK = threading.RLock()
_FILE_HASH_CACHE: dict[tuple[str, int, int, bool], str] = {}
_DIR_DIGEST_CACHE: dict[tuple, tuple[str, int, int]] = {}
_SKILL_META_CACHE: dict[tuple, dict] = {}
_SUMMARY_CACHE: dict[str, tuple[float, tuple, list[dict]]] = {}


def clear_cache() -> None:
    """清空 Skill 文件哈希、元数据与来源统计缓存 (写入后及手动刷新时调用)。"""
    with _CACHE_LOCK:
        _FILE_HASH_CACHE.clear()
        _DIR_DIGEST_CACHE.clear()
        _SKILL_META_CACHE.clear()
        _SUMMARY_CACHE.clear()


# 源端硬编码路径/宏提示 (延续 Context Vault "诚实迁移报告" 基因)
_HARDCODE_PATTERNS = [
    (re.compile(r"~[/\\]\.zcode\b|\.zcode[/\\]", re.I), "引用了 ~/.zcode 路径"),
    (re.compile(r"~[/\\]\.claude\b|\.claude[/\\]", re.I), "引用了 ~/.claude 路径"),
    (re.compile(r"~[/\\]\.codex\b|\.codex[/\\]", re.I), "引用了 ~/.codex 路径"),
    (re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?", re.I), "引用了 ${CLAUDE_PLUGIN_ROOT} 变量"),
    (re.compile(r"~[/\\]\.cursor\b|\.cursor[/\\]", re.I), "引用了 ~/.cursor 路径"),
    (re.compile(r"~[/\\]\.dsh\b|\.dsh[/\\]", re.I), "引用了 ~/.dsh 路径"),
]


def _home() -> Path:
    return Path(_cv().HOME)


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

def _scan_dir_manifest(root: Path) -> tuple[list[tuple[str, str, int, int]], int, int, list[str]]:
    """单次 os.scandir 遍历 Skill 目录, 返回 (sorted_manifest, file_count, total_bytes, top_sub_dirs)。
    manifest 项为 (rel_posix, full_path, size, mtime_ns)。"""
    manifest: list[tuple[str, str, int, int]] = []
    sub_dirs: list[str] = []
    total_bytes = 0
    stack: list[tuple[str, str, bool]] = [(str(root), "", True)]
    while stack:
        cur_dir, rel_prefix, is_top = stack.pop()
        try:
            with os.scandir(cur_dir) as it:
                entries = list(it)
        except OSError:
            continue
        entries.sort(key=lambda e: e.name)
        child_dirs: list[tuple[str, str, bool]] = []
        for ent in entries:
            name = ent.name
            try:
                is_dir = ent.is_dir(follow_symlinks=True)
            except OSError:
                continue
            if is_dir:
                if name in _SKIP_DIR_NAMES:
                    continue
                if is_top:
                    sub_dirs.append(name)
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                child_dirs.append((ent.path, rel, False))
                continue
            if name in _SKIP_FILE_NAMES:
                continue
            try:
                st = ent.stat()
            except OSError:
                continue
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            manifest.append((rel, ent.path, st.st_size, st.st_mtime_ns))
            total_bytes += st.st_size
        # 逆序入栈以保持字典正序弹出
        for cd in reversed(child_dirs):
            stack.append(cd)
    manifest.sort(key=lambda x: x[0])
    return manifest, len(manifest), total_bytes, sub_dirs


def _hash_file_entry(full_path: str, size: int, mtime_ns: int, is_md: bool) -> str:
    """带自失效内存缓存的单文件摘要 (key 含 size/mtime_ns, 改文件立即失效)。"""
    key = (os.path.normcase(full_path), size, mtime_ns, is_md)
    with _CACHE_LOCK:
        hit = _FILE_HASH_CACHE.get(key)
        if hit is not None:
            return hit
    try:
        if is_md or size <= _FAST_HASH_THRESHOLD:
            data = Path(full_path).read_bytes()
            if is_md:
                data = data.replace(b"\r\n", b"\n")
            dig = hashlib.sha256(data).hexdigest()
        else:
            h = hashlib.sha256(f"s:{size}:".encode("ascii"))
            with open(full_path, "rb") as fh:
                h.update(fh.read(_SAMPLE_CHUNK))
                mid = max(0, (size - _SAMPLE_CHUNK) // 2)
                if mid > 0:
                    fh.seek(mid)
                h.update(fh.read(_SAMPLE_CHUNK))
                tail = max(0, size - _SAMPLE_CHUNK)
                if tail > 0:
                    fh.seek(tail)
                h.update(fh.read(_SAMPLE_CHUNK))
            dig = h.hexdigest()
    except OSError:
        return ""
    with _CACHE_LOCK:
        if len(_FILE_HASH_CACHE) >= 20000:
            _FILE_HASH_CACHE.clear()
        _FILE_HASH_CACHE[key] = dig
    return dig


def _dir_digest(
    root: Path,
    skill_md_override: bytes | None = None,
    *,
    _manifest: tuple[list[tuple[str, str, int, int]], int, int] | None = None,
) -> tuple[str, int, int]:
    """计算 Skill 目录内容指纹 (sha256[:16]), 同时返回 (file_count, total_bytes)。"""
    if _manifest is None:
        manifest, n_files, total_bytes, _ = _scan_dir_manifest(root)
    else:
        manifest, n_files, total_bytes = _manifest
    override_hash = (
        hashlib.sha256(skill_md_override.replace(b"\r\n", b"\n")).hexdigest()
        if skill_md_override is not None else ""
    )
    dkey = (
        os.path.normcase(str(root)),
        override_hash,
        tuple((rel, sz, mt) for rel, _, sz, mt in manifest),
    )
    with _CACHE_LOCK:
        hit = _DIR_DIGEST_CACHE.get(dkey)
        if hit is not None:
            return hit
    h = hashlib.sha256()
    for rel, full_path, sz, mt in manifest:
        if override_hash and rel == "SKILL.md":
            fhash = override_hash
        else:
            fhash = _hash_file_entry(full_path, sz, mt, rel.lower().endswith(".md"))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(fhash.encode("ascii"))
        h.update(b"\0")
    res = (h.hexdigest()[:16], n_files, total_bytes)
    with _CACHE_LOCK:
        if len(_DIR_DIGEST_CACHE) >= 4000:
            _DIR_DIGEST_CACHE.clear()
        _DIR_DIGEST_CACHE[dkey] = res
    return res


def _estimate_prompt_tokens(name: str, description: str) -> int:
    """估算该 Skill 在 Agent progressive disclosure 中常驻 system prompt 的 token 成本。"""
    s = f"{name}: {description}"
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cjk
    return max(8, int(cjk * 1.1 + other / 4.0) + 6)


def _scan_warnings(
    skill_dir: Path,
    text: str,
    meta: dict,
    source: str,
    script_files: list[str] | None = None,
) -> list[str]:
    warns: list[str] = []
    if not str(meta.get("description") or "").strip():
        warns.append("缺少 description（迁移时将自动补齐）")
    elif len(str(meta.get("description") or "")) > 1200:
        warns.append("description 较长（>1200 字符，较占系统提示词）")
    seen_w = set()
    scan_texts = [text]
    if script_files is not None:
        candidates = script_files[:12]
    else:
        scripts_dir = skill_dir / "scripts"
        candidates = [
            str(fp) for fp in sorted(scripts_dir.rglob("*"))[:12]
            if fp.is_file() and fp.stat().st_size <= 65536 and fp.suffix.lower() in (".py", ".sh", ".ps1", ".js", ".ts", ".md")
        ] if scripts_dir.is_dir() else []
    for sp_str in candidates:
        try:
            sp = Path(sp_str)
            if sp.stat().st_size <= 65536:
                scan_texts.append(sp.read_text(encoding="utf-8", errors="replace"))
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
    elif agent == "dsh":
        import cv_dsh
        out.append(("standalone", cv_dsh.dsh_skills_dir()))
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
    if target == "dsh":
        import cv_dsh
        return cv_dsh.dsh_skills_dir(), None, ""
    ws = Path(workspace_dir).expanduser().resolve() if workspace_dir else Path.cwd().resolve()
    return ws / ".agents" / "skills", None, ""


def _iter_skill_files(root: Path, skip_dot_dirs: bool = True):
    """基于 os.scandir 发现 SKILL.md (Windows 上复用系统目录遍历 stat 缓存, 比 os.walk 更快)。"""
    if not root.is_dir():
        return
    stack = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        has_skill_md = False
        sub_dirs: list[str] = []
        for ent in entries:
            name = ent.name
            if name == "SKILL.md":
                try:
                    if ent.is_file():
                        has_skill_md = True
                        break
                except OSError:
                    pass
            try:
                if ent.is_dir(follow_symlinks=True):
                    if name in _SKIP_DIR_NAMES or (skip_dot_dirs and name.startswith(".")):
                        continue
                    sub_dirs.append(ent.path)
            except OSError:
                continue
        if has_skill_md:
            yield Path(cur) / "SKILL.md"
            # 找到一个 skill 目录后不再向其内部子目录递归找嵌套 SKILL.md (避免把 examples/ 里的模板误当独立 skill)
            continue
        sub_dirs.sort(reverse=True)
        stack.extend(sub_dirs)


def _inspect_skill_dir(sdir: Path, root: Path, ag: str, scope: str, rel: str) -> dict | None:
    """单次扫描并缓存单个 Skill 的元数据、规范化改写与目录内容指纹。"""
    manifest, n_files, total_bytes, sub_dirs = _scan_dir_manifest(sdir)
    smd_entry = next((m for m in manifest if m[0] == "SKILL.md"), None)
    if smd_entry is None:
        return None
    _, smd_path, smd_sz, smd_mt = smd_entry
    dir_slug = _slugify(sdir.name, "skill")
    scripts_list = [
        (r, p, sz, mt) for r, p, sz, mt in manifest
        if r.startswith("scripts/") and sz <= 65536
        and os.path.splitext(r)[1].lower() in (".py", ".sh", ".ps1", ".js", ".ts", ".md")
    ][:12]
    mkey = (
        os.path.normcase(str(sdir)), ag, smd_sz, smd_mt,
        tuple((r, sz, mt) for r, _, sz, mt in scripts_list),
    )
    with _CACHE_LOCK:
        cached_meta = _SKILL_META_CACHE.get(mkey)
    if cached_meta is None:
        try:
            raw_text = Path(smd_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        meta, body, has_fm = parse_frontmatter(raw_text)
        meta_name = str(meta.get("name") or "").strip()
        desc = str(meta.get("description") or "").strip() or _extract_fallback_description(body, dir_slug)
        norm_text, rewrites = normalize_skill_md(raw_text, dir_slug)
        warnings = _scan_warnings(sdir, raw_text, meta, ag, script_files=[p for _, p, _, _ in scripts_list])
        ptok = _estimate_prompt_tokens(dir_slug, desc)
        smd_raw_hash = _hash_file_entry(smd_path, smd_sz, smd_mt, True)
        smd_norm_hash = (
            hashlib.sha256(norm_text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
            if rewrites else smd_raw_hash
        )
        cached_meta = {
            "raw_name": meta_name or sdir.name,
            "description": desc,
            "version": str(meta.get("version") or ""),
            "has_frontmatter": has_fm,
            "rewrites": rewrites,
            "warnings": warnings,
            "prompt_tokens": ptok,
            "norm_text": norm_text if rewrites else None,
            "smd_raw_hash": smd_raw_hash,
            "smd_norm_hash": smd_norm_hash,
        }
        with _CACHE_LOCK:
            if len(_SKILL_META_CACHE) >= 4000:
                _SKILL_META_CACHE.clear()
            _SKILL_META_CACHE[mkey] = cached_meta

    digest, _, _ = _dir_digest(sdir, _manifest=(manifest, n_files, total_bytes))
    if cached_meta["norm_text"] is not None:
        norm_digest, _, _ = _dir_digest(
            sdir,
            skill_md_override=cached_meta["norm_text"].encode("utf-8"),
            _manifest=(manifest, n_files, total_bytes),
        )
    else:
        norm_digest = digest

    parts = rel.split("/")
    group = parts[0] if (scope == "plugin" and len(parts) >= 2) else ("standalone" if scope == "standalone" else "plugin")
    shape_sig = tuple((r, sz) for r, _, sz, _ in manifest if os.path.splitext(r)[1].lower() != ".md")
    md_names = tuple(r for r, _, _, _ in manifest if os.path.splitext(r)[1].lower() == ".md")
    return {
        "id": f"{ag}:{scope}:{rel}",
        "source": ag,
        "scope": scope,
        "group": group,
        "slug": dir_slug,
        "raw_name": cached_meta["raw_name"],
        "description": cached_meta["description"],
        "version": cached_meta["version"],
        "path": str(sdir),
        "rel_path": rel,
        "digest": digest,
        "norm_digest": norm_digest,
        "files": n_files,
        "size_kb": round(total_bytes / 1024.0, 1),
        "sub_dirs": sub_dirs,
        "prompt_tokens": cached_meta["prompt_tokens"],
        "has_frontmatter": cached_meta["has_frontmatter"],
        "rewrites": cached_meta["rewrites"],
        "warnings": cached_meta["warnings"],
        "_smd_raw_hash": cached_meta["smd_raw_hash"],
        "_smd_norm_hash": cached_meta["smd_norm_hash"],
        "_shape": (shape_sig, md_names),
    }


def _strip_internal_fields(skills: list[dict]) -> list[dict]:
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in skills]


def _discover_skills_internal(
    source: str = "zcode",
    *,
    include_plugins: bool = False,
) -> list[dict]:
    agents = list(ALL_AGENTS) if source == "all" else [source]
    for a in agents:
        if a not in ALL_AGENTS:
            raise ValueError(f"不支持的来源: {a} (可选: {', '.join(ALL_AGENTS)}, all)")
    jobs: list[tuple[Path, Path, str, str, str]] = []
    seen_paths: set[str] = set()
    for ag in agents:
        for scope, root in _source_roots(ag, include_plugins=include_plugins):
            for smd in _iter_skill_files(root, skip_dot_dirs=(ag == "codex")):
                sdir = smd.parent
                key_path = os.path.normcase(os.path.abspath(str(sdir)))
                if key_path in seen_paths:
                    continue
                seen_paths.add(key_path)
                rel = sdir.relative_to(root).as_posix()
                jobs.append((sdir, root, ag, scope, rel))
    if len(jobs) >= 12:
        with ThreadPoolExecutor(max_workers=4) as pool:
            raw_items = list(pool.map(lambda j: _inspect_skill_dir(*j), jobs))
    else:
        raw_items = [_inspect_skill_dir(*j) for j in jobs]
    found = [it for it in raw_items if it is not None]
    found.sort(key=lambda x: (0 if x["scope"] == "standalone" else 1, x["source"], x["slug"], x["rel_path"]))
    return found


def discover_skills(
    source: str = "zcode",
    *,
    include_plugins: bool = False,
) -> list[dict]:
    """扫描指定源（或 all）下的全部 Skill，返回结构化元数据列表。"""
    return _strip_internal_fields(_discover_skills_internal(source, include_plugins=include_plugins))


def _roots_stamp() -> tuple:
    stamps = []
    for ag in ALL_AGENTS:
        for scope, root in _source_roots(ag, include_plugins=True):
            try:
                mt = root.stat().st_mtime_ns
            except OSError:
                mt = -1
            stamps.append((ag, scope, mt))
    return tuple(stamps)


def _skill_agent_app_detected(ag: str) -> bool:
    """判断 Skill 源对应客户端主程序/目录是否在本机真实存在 (保持测试 HOME 隔离)。"""
    c = _cv()
    if ag == "zcode":
        if (_home() / ".zcode").exists():
            return True
        return bool(c._is_real_home_dir() and shutil.which("zcode"))
    return bool(c._detect_mcp_client_app(ag))


def _connected_skill_agents(vault: Path | None = None) -> dict[str, bool]:
    """读取「设置与运维」MCP 接入状态，映射到各 Skill 源/目标是否允许展示与迁移。"""
    c = _cv()
    vdir = vault if vault is not None else _vault_dir(None)
    st = c.mcp_status_api(vdir, probe_cli=False)
    by_id = {row["id"]: row for row in st.get("clients", [])}
    res = {ag: bool(by_id.get(ag, {}).get("installed")) for ag in ALL_AGENTS if ag != "zcode"}
    # ZCode 无独立 MCP 配置文件，复用已安装的 Claude Code / Codex 后端连接状态
    res["zcode"] = bool(_skill_agent_app_detected("zcode") and (res.get("claude") or res.get("codex")))
    return res


def source_summary(use_cache: bool = True, vault: Path | None = None) -> list[dict]:
    """统计各端 Skill 数量 (默认带根目录 mtime 自失效与 30s 缓存, 实时附加主程序探测与 MCP 接入状态)。"""
    import cv_dsh
    hkey = os.path.normcase(str(_home().resolve())) + "|" + os.path.normcase(str(cv_dsh.dsh_skills_dir().resolve()))
    rstamp = _roots_stamp()
    now = time.monotonic()
    conn_map = _connected_skill_agents(vault)
    if use_cache:
        with _CACHE_LOCK:
            ent = _SUMMARY_CACHE.get(hkey)
            if ent is not None and ent[1] == rstamp and (now - ent[0]) < _SUMMARY_TTL:
                return [
                    {**x, "app_detected": _skill_agent_app_detected(x["agent"]),
                     "mcp_connected": bool(conn_map.get(x["agent"], False))}
                    for x in ent[2]
                ]
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
            "app_detected": _skill_agent_app_detected(ag),
            "mcp_connected": bool(conn_map.get(ag, False)),
        })
    with _CACHE_LOCK:
        _SUMMARY_CACHE[hkey] = (now, rstamp, [dict(x) for x in out])
    return out


def _target_match_skill(dst: Path, sk: dict, *, normalize: bool = True) -> bool:
    """惰性精准比对目标目录与源 Skill 是否一致: 先查 SKILL.md 哈希与非 md 文件结构, 不一致立刻短路跳过大文件读取。"""
    smd = dst / "SKILL.md"
    if not smd.is_file():
        return False
    try:
        st = smd.stat()
    except OSError:
        return False
    dst_smd_hash = _hash_file_entry(str(smd), st.st_size, st.st_mtime_ns, True)
    allowed_smd = (
        (sk["_smd_raw_hash"], sk["_smd_norm_hash"]) if normalize else (sk["_smd_raw_hash"],)
    )
    if "_smd_raw_hash" in sk and dst_smd_hash not in allowed_smd:
        return False
    manifest, n_files, total_bytes, _ = _scan_dir_manifest(dst)
    if "_shape" in sk:
        dst_shape = (
            tuple((r, sz) for r, _, sz, _ in manifest if os.path.splitext(r)[1].lower() != ".md"),
            tuple(r for r, _, _, _ in manifest if os.path.splitext(r)[1].lower() == ".md"),
        )
        if dst_shape != sk["_shape"]:
            return False
    dig, _, _ = _dir_digest(dst, _manifest=(manifest, n_files, total_bytes))
    allowed = (sk["digest"], sk["norm_digest"]) if normalize else (sk["digest"],)
    return dig in allowed


def _target_existing_names(dest_root: Path) -> set[str]:
    """仅列出目标目录下已有 Skill 目录名集合 (零文件内容读取)。"""
    if not dest_root.is_dir():
        return set()
    names: set[str] = set()
    try:
        with os.scandir(dest_root) as it:
            for ent in it:
                if ent.name.startswith("."):
                    continue
                try:
                    if ent.is_dir() and (Path(ent.path) / "SKILL.md").is_file():
                        names.add(ent.name)
                except OSError:
                    continue
    except OSError:
        pass
    return names


def _validate_skill_slug(slug: str) -> str:
    s = str(slug or "").strip()
    if not s or s in (".", "..") or not _SKILL_SLUG_RE.match(s):
        raise ValueError(f"非法 Skill 目录名: {slug!r}")
    return s


def _validate_backup_id(backup_id: str) -> str:
    b = str(backup_id or "").strip()
    if not b or b in (".", "..") or not _BACKUP_ID_RE.match(b):
        raise ValueError(f"非法 Skill 备份 ID: {backup_id!r}")
    return b


def _safe_child_dir(parent: Path, name: str) -> Path:
    parent_res = parent.resolve()
    child_res = (parent_res / name).resolve()
    if child_res.parent != parent_res:
        raise ValueError(f"路径越界拒绝: {name!r}")
    return child_res


def _make_skill_backup_dir(vault: Path, kind: str) -> Path:
    broot = Path(vault) / "backups"
    broot.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    stem = f"skills-{kind}-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}"
    cand = broot / stem
    idx = 2
    while cand.exists():
        cand = broot / f"{stem}-{idx}"
        idx += 1
    return cand


def _write_skill_backup_manifest(
    backup_dir: Path,
    kind: str,
    *,
    source: str,
    target: str,
    agy_mode: str,
    plugin_name: str,
    dest_root: Path,
    workspace_dir: Path | None,
    items: list[dict],
) -> Path:
    manifest = {
        "id": backup_dir.name,
        "created_at": _cv().now_iso(),
        "kind": kind,
        "source": source,
        "target": target,
        "agy_mode": agy_mode if target == "antigravity" else "",
        "plugin_name": plugin_name,
        "workspace_dir": str(workspace_dir.resolve()) if workspace_dir else "",
        "dest_root": str(dest_root),
        "items": items,
    }
    mp = backup_dir / "manifest.json"
    _cv().atomic_write_text(mp, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return mp


def _list_target_skills(dest_root: Path, target: str, limit: int = 200) -> list[dict]:
    """列出目标目录下已安装的 Skill 简要清单 (复用缓存目录扫描, 供目标端移除与对比使用)。"""
    if not dest_root.is_dir():
        return []
    dirs: list[Path] = []
    try:
        with os.scandir(dest_root) as it:
            for ent in it:
                if ent.name.startswith("."):
                    continue
                try:
                    if ent.is_dir() and (Path(ent.path) / "SKILL.md").is_file():
                        dirs.append(Path(ent.path))
                except OSError:
                    continue
    except OSError:
        return []
    dirs.sort(key=lambda p: p.name.lower())
    out: list[dict] = []
    for dst in dirs[:limit]:
        meta = _inspect_skill_dir(dst, dest_root, target, "target", dst.name)
        if meta is None:
            continue
        out.append({
            "slug": dst.name,
            "name": meta["raw_name"],
            "description": meta["description"],
            "files": meta["files"],
            "size_kb": meta["size_kb"],
            "path": meta["path"],
        })
    return out


def list_skills_api(
    source: str = "zcode",
    *,
    include_plugins: bool = False,
    target: str = "antigravity",
    agy_mode: str = "skills",
    plugin_name: str = "",
    workspace_dir: Path | None = None,
    refresh: bool = False,
    vault: Path | None = None,
    connected_only: bool = False,
) -> dict:
    """供 Web 控制台与 CLI 使用的盘点接口: 返回源 Skill 列表 + 相对目标的状态。
    connected_only=True 时仅保留「设置与运维」已接入后端的客户端 (且源端排除 0 Skill 工具)。"""
    if refresh:
        clear_cache()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sum_fut = pool.submit(source_summary, not refresh, vault)
        all_sources = sum_fut.result()
        connected_agents = [s["agent"] for s in all_sources if s.get("mcp_connected")]
        if connected_only:
            sources = [s for s in all_sources if s.get("mcp_connected") and int(s.get("total") or 0) > 0]
            if not connected_agents:
                return {
                    "source": "",
                    "include_plugins": bool(include_plugins),
                    "target": "",
                    "agy_mode": None,
                    "plugin_name": "",
                    "dest_root": "",
                    "plugin_dir": None,
                    "sources": [],
                    "connected_agents": [],
                    "total": 0,
                    "skills": [],
                    "target_skills": [],
                }
            valid_src = {s["agent"] for s in sources}
            if source != "all" and source not in valid_src:
                source = sources[0]["agent"] if sources else ""
            if target != "workspace" and target not in connected_agents:
                target = next((a for a in connected_agents if a != source), connected_agents[0])
            dest_root, plugin_dir, resolved_pname = resolve_target_root(
                target, agy_mode=agy_mode, plugin_name=plugin_name,
                source_hint=(source or connected_agents[0]), workspace_dir=workspace_dir,
            )
            if not sources:
                return {
                    "source": "",
                    "include_plugins": bool(include_plugins),
                    "target": target,
                    "agy_mode": agy_mode if target == "antigravity" else None,
                    "plugin_name": resolved_pname,
                    "dest_root": str(dest_root),
                    "plugin_dir": str(plugin_dir) if plugin_dir else None,
                    "sources": [],
                    "connected_agents": connected_agents,
                    "total": 0,
                    "skills": [],
                    "target_skills": _list_target_skills(dest_root, target),
                }
            skills = _discover_skills_internal(source, include_plugins=include_plugins)
            if source == "all":
                skills = [s for s in skills if s.get("source") in valid_src]
        else:
            sources = all_sources
            skills = _discover_skills_internal(source, include_plugins=include_plugins)
            dest_root, plugin_dir, resolved_pname = resolve_target_root(
                target, agy_mode=agy_mode, plugin_name=plugin_name, source_hint=source, workspace_dir=workspace_dir
            )

        existing_names = _target_existing_names(dest_root)
        to_check = [sk for sk in skills if sk["slug"] in existing_names]
        if len(to_check) >= 12:
            matched_flags = list(pool.map(lambda s: _target_match_skill(dest_root / s["slug"], s, normalize=True), to_check))
            for sk, ok in zip(to_check, matched_flags):
                sk["target_status"] = "identical" if ok else "conflict"
            for sk in skills:
                if sk["slug"] not in existing_names:
                    sk["target_status"] = "new"
        else:
            for sk in skills:
                slug = sk["slug"]
                if slug not in existing_names:
                    sk["target_status"] = "new"
                elif _target_match_skill(dest_root / slug, sk, normalize=True):
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
        "sources": sources,
        "connected_agents": connected_agents,
        "total": len(skills),
        "skills": _strip_internal_fields(skills),
        "target_skills": _list_target_skills(dest_root, target),
    }


from cv_skills_ops import (  # noqa: F401,E402
    _copy_skill_dir,
    _write_agy_plugin_manifest,
    migrate_skills,
    _read_truncated_text,
    inspect_skill_api,
    remove_skills_api,
    list_skill_backups_api,
    rollback_skill_backup_api,
    cmd_skills,
)

