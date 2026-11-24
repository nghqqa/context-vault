# Context Vault v0.12.0 — Skill Diff/Rollback Workbench, Concurrency/Security Hardening & Core Modularization

> **EN** | Upgrades the **Skill Migration Workbench** into a complete audit-and-recovery lifecycle—pre-migration unified diff & file-delta inspection (`cv skills inspect`, `GET /skills/inspect`), path-traversal-guarded safe removal with automatic backups (`cv skills remove`, `POST /skills/remove`), one-click rollback (`cv skills backups/rollback`, `GET /skills/backups`, `POST /skills/rollback`), and same-origin target auto-switching—paired with **concurrency/security hardening** (eliminating process-wide `redirect_stdout`, adding write/tray mutexes, IPv6 `[::1]` guard & pre-body auth rejection, security response headers, HTML/XML XSS escaping, full-config zip backups) and **modular Python/UI decomposition** across Python 3.9–3.12 stdlib-only engines.

> **中文** | 本版将 **Skill 工作台**补全为可审、可删、可反悔的完整闭环：迁入前支持 unified diff 与附属文件增删改清单预览（`cv skills inspect`、`GET /skills/inspect`）、目标端支持路径穿越拦截与备份前先留档的安全移除（`cv skills remove`、`POST /skills/remove`）、支持校验 `manifest.json` 的一键备份回滚（`cv skills backups/rollback`），并在切源撞同源时自动切换目标端；同时完成**并发与安全加固**（彻底清零 `redirect_stdout`、补齐 HTTP 与托盘写入互斥锁、修复托盘同步后索引与缓存刷新、兼容 IPv6 `[::1]` 回环与安全响应头、报表与弹窗防注入、全配置 zip 备份）以及**核心前后端模块化拆分**（保持 100% 纯 Python 标准库、零第三方依赖与单文件自包含控制台）。

## ✨ Highlights / 亮点

### 🎯 Complete Skill Workbench Lifecycle / Skill 预览、安全移除与备份回滚闭环
- **Pre-Migration Inspection & Diff (`inspect_skill_api`, `GET /skills/inspect`, `cv skills inspect`)**: Inspect normalized `SKILL.md` text, line-by-line `unified_diff`, and `files_added` / `files_removed` / `files_changed` asset deltas inside the Web Console `#sk-drawer` before overwriting anything, with automatic alternate-target switching when source equals target (`isSkillSameOrigin()`).
- **Safe Removal & Verified Rollback (`remove_skills_api`, `list_skill_backups_api`, `rollback_skill_backup_api`)**: Enforces slug and `backup_id` sanitization, automatically creates `vault/backups/skills-*` + `manifest.json` snapshots before removals or `--conflict overwrite`, updates Antigravity `plugin.json` counts in plugin mode, and verifies `manifest.json` `dest_root` integrity prior to restoring via `POST /skills/rollback` or `cv skills rollback`.

### 🔒 Concurrency, Security & Reliability Hardening / 并发互斥、安全加固与可靠性提升
- **Zero `redirect_stdout` + Explicit Write Locks**: Extracted silent structured APIs (`sync_usage_events()` fixing a silently ignored `cv setup` call, `prune_snapshots_api()`, `backup_vault_api()`), added `_ServerCtx._write_lock` (`cv_server.py`) and `TrayState._op_lock` (`pro/tray_app.py`), fixed tray `sync_now()` missing index rebuild/cache invalidation, isolated `.backup-tmp-{pid}-{tid}.db` temp files, and included `migrations.jsonl`, `accounts.json`, `semantic.json`, `pricing.json`, and `prices.json` in vault backups.
- **HTTP & Output Hardening (`cv_server.py`, `cv_ops.py`)**: Accepts IPv6 `Host: [::1]`, rejects unauthenticated `POST` requests with `Connection: close` before draining bodies, attaches `X-Content-Type-Options: nosniff` / `X-Frame-Options: DENY` / `Referrer-Policy: no-referrer`, logs unhandled 500 tracebacks to `http_logger`, escapes all dynamic fields in `cmd_dashboard()` (`html.escape` + cross-platform `webbrowser.open()`), and escapes XML/PowerShell quotes in `toast()`.
- **Performance, CLI & CI (`cv_dsh.py`, `cv_ledger.py`, `cv.py`, `ci.yml`)**: Compresses dual Zstd frames (`.v4.jsonl.zstd`) in a single Node subprocess; switches `_runway()` to SQL `COALESCE(SUM(...), 0)` aggregations; adds `DSH Zstd` + `MCP 接入摘要` to `cv doctor` and multi-client `--clients` / `--mcp-clients` flags to `cv mcp` / `cv setup`; adds Ubuntu 24.04 Python 3.9, macOS Python 3.12, 20-module stdlib check, and `pip` package smoke verification to CI.

### 🧩 Core Modularization (100% Backward Compatible) / 核心模块拆解瘦身
- **Backend Stdlib Split (`pyproject.toml`, `pro/build.py`)**: Extracted [`cv_mcp.py`](tools/cv_mcp.py) (541 lines) and [`cv_sync.py`](tools/cv_sync.py) (467 lines) from [`cv.py`](tools/cv.py) (trimmed to 2,627 lines), and [`cv_ledger_collect.py`](tools/cv_ledger_collect.py) (1,175 lines) from [`cv_ledger.py`](tools/cv_ledger.py) (trimmed to 1,419 lines) with dynamic monkeypatch-safe re-exports.
- **Frontend UI Split (`cv_ui.py`)**: Extracted [`console_memory.js`](tools/ui/console_memory.js) (154 lines) and [`console_reader.js`](tools/ui/console_reader.js) (254 lines) from [`console_snapshots.js`](tools/ui/console_snapshots.js) (trimmed to 504 lines) while preserving the single `<style>` + single `<script>` self-contained HTML invariant.

## ✅ Verification receipts / 验证回执
- `python -m pytest -q`: **258 passed** (0 failed, 0 skipped).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `S1-S8` Skills Workbench & `0.12.0` status): **console unit OK**.
- `python tools/check_console_overflow.py`: **28/28 OK** (`380 / 900 / 1440 / 2560px` × all 7 console views — zero page-level overflow).
- Stdlib-only 20-module engine import smoke check & `pro/*.py` `py_compile`: **`ALL_20_IMPORTS_OK`**.
- `dist/ContextVault.exe` (`14.41 MiB`, `15,109,334 bytes`, SHA-256 `55ceca62f7ce1c9fed81179a352de3cd6af22be8ae40442dfba37f01fd95aa93`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.12.0`) + `tools/list` (`6` tools).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.11.0...v0.12.0
