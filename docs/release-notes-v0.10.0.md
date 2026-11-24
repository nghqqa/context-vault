# Context Vault v0.10.0 — Cross-Agent Skill Bulk Migration & Antigravity Dual-Mode Switcher

> **EN** | Introduces a **zero-dependency Cross-Agent Skill (`SKILL.md`) Bulk Migration Engine** (`tools/cv_skills.py`, `cv skills`, `GET /skills/list`, `POST /skills/migrate`) and a dual-mode Migration Workbench in the Web Console (`Sessions` | `Skill Migration`). Users can selectively migrate standalone or plugin skills across **ZCode / Claude Code / Codex / Cursor / Antigravity** (plus workspace `.agents/skills/`), toggle between **direct global install** (`~/.gemini/config/skills/`) and a **reversible Antigravity plugin bundle** (`~/.gemini/config/plugins/<name>/plugin.json` + `skills/`), auto-normalize non-compliant frontmatter slugs on the target copy while keeping sources strictly read-only, catch hardcoded source-path references, monitor live System Prompt token budget warnings, and inspect clear `待复制` (dry-run) vs `已写入` (applied) status pills.

> **中文** | 本版新增**零依赖跨 Agent Skill 技能批量迁移引擎**（`tools/cv_skills.py`、`cv skills`、`GET /skills/list`、`POST /skills/migrate`）及 Web 控制台「迁移工作台」双模式界面（`会话迁移` | `Skill 技能迁移`）：支持在 **ZCode / Claude Code / Codex / Cursor / Antigravity** 及工作区 `.agents/skills/` 之间按需勾选迁移，迁往 Antigravity 时提供**「直写全局目录 (`~/.gemini/config/skills/`)」与「打包为独立插件 (`~/.gemini/config/plugins/<name>/`)」一键切换**；源目录严格只读，目标副本自动完成 Frontmatter 规范化（含大写/空格 `name` 转合规 slug）、SHA-256 幂等去重、硬编码源路径告警、`overwrite` 自动备份与 System Prompt Token 预算提醒，并修复实写后结果表状态标签准确展示 `已写入` / `待复制`。

## ✨ Highlights / 亮点

### 🧩 Cross-Agent Skill Migration Engine (`tools/cv_skills.py`) / 跨端 Skill 批量迁移引擎
- **Zero-dep read-only discovery**: Parses `SKILL.md` YAML frontmatter in pure Python 3.9+ stdlib across `~/.zcode/skills/`, `~/.zcode/cli/plugins/cache/`, `~/.claude/skills/`, `~/.codex/skills/` (skipping `.system`), `~/.gemini/config/skills/`, and `~/.cursor/skills/`.
- **Antigravity dual write mode**:
  - `skills` (Global directory): Writes `<slug>/SKILL.md` directly to `~/.gemini/config/skills/` for instant hot-reload across Antigravity.
  - `plugin` (Reversible plugin bundle): Generates `~/.gemini/config/plugins/<name>/plugin.json` + `skills/<slug>/SKILL.md`, enabling 1-click enable/disable or clean uninstall as a single unit.
- **Safety & fidelity**: Strict dry-run by default (`--apply` required to write); non-destructive target-copy slug/description normalization; CRLF-tolerant SHA-256 directory digest comparison (`identical` skip); `skip` / `overwrite` (auto-backed up to `vault/backups/skills-<ts>/`) / `rename` (`slug-2`) conflict policies; hardcoded source-path warnings (`~/.zcode`, `~/.claude`, `${CLAUDE_PLUGIN_ROOT}`, etc.) and System Prompt budget alerts (>30 skills or >4,000 tokens).

### 🖥️ Migration Workbench Dual-Mode UI / 控制台 Skill 迁移工作台
- **Top mode bar (`会话迁移` | `Skill 技能迁移`)** inside the Migration tab (`#mig`):
  - Left pane: source pills with live counts, keyword filter, opt-in `包含插件包` toggle, batch selectors (`全选当前筛选` / `仅选未同步` / `清空选择`), status pills (`新增` / `已一致` / `同名冲突`), per-skill token cost, and risk `⚠` indicators.
  - Right pane: target cards, **Antigravity direct vs plugin mode switcher** with custom plugin name input, live System Prompt token budget meter, dry-run report & confirmed write, plus clear `待复制` (dry-run) / `已写入` (applied) row labels.

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **240 passed** (including `tests/test_cv_skills.py` covering discovery, normalization, Antigravity `skills` vs `plugin` modes, idempotency, conflict policies with backup, and HTTP endpoints).
- `check_console_js.py` (`emit` + `run` + `unit`, including `S1-S3` Skill dry-run/apply & label tests) & `check_console_overflow.py` (real headless Chromium **380 / 900 / 1440 / 2560px = 32/32 zero overflow**).
- `dist/ContextVault.exe` (`13.99 MiB`, `14,673,233 bytes`, SHA-256 `01e63b5bdff4fa60ae68a275ed94aacd91741dfcb8c6115a0ce0b8a93df016c1`): verified both `--check` (`OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.10.0`).
- Live local test on real user machine: **77 / 77** standalone ZCode skills cleanly discovered, migrated to `~/.gemini/config/skills/`, and hot-loaded by Antigravity (`63` auto-invocable + `14` `disable-model-invocation: true`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.9.0...v0.10.0
