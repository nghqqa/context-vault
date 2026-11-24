# Context Vault v0.11.0 — DeepSeek Harness 5-in-1 Native Support, Selective MCP & Backend-Aligned Skills

> **EN** | Introduces full **5-in-1 native support for DeepSeek Harness (`dsh`)**—V4/V3/V2 `.jsonl` and concatenated Zstd `.jsonl.zstd` intake/export (zero-hard-dep 5-tier Zstd codec chain), cross-tool V4 migration with automatic `storages/workspace.json` registration and `session_projcache` invalidation, Token ledger collector + DeepSeek V4/Flash pricing, bidirectional `SKILL.md` migration, and global `$DSH_HOME/cordis.patch.yml` MCP injection—alongside **selective MCP onboarding & 5-client safe uninstallation** (distinguishing real client binaries from uninstall residual directories) and **backend-aligned Skill discovery** (`connected_only=1` hides unconnected clients and 0-skill sources).

> **中文** | 本版新增 **DeepSeek Harness (`dsh`) 五合一原生适配**（V4/V3/V2 `.jsonl` 与拼接式 `.jsonl.zstd` 零强依赖五级编解码、跨工具 V4 迁移并自动登记 `workspace.json` 与清理 `session_projcache`、Token 账本直采与 DeepSeek V4/Flash 费率、双向 Skill 迁移、`cordis.patch.yml` 全局 MCP 注入），并将 **MCP 记忆后端** 重构为**真实程序探测 + 卸载残留识别 + 勾选按需接入 + 单项安全移除**，同时将 **Skill 技能盘点/迁移** 与已接入 MCP 后端强一致对齐（默认隐藏未接入后端与 0 Skill 工具）。

## ✨ Highlights / 亮点

### 🧠 DeepSeek Harness (`dsh`) 5-in-1 Native Adaptation / DeepSeek Harness 五合一原生适配
- **Zero-Hard-Dep V4/Zstd Protocol Engine (`tools/cv_dsh.py`)**: Exact port of DSH segment/project-key encoding, 5-tier Zstd codec chain (`stdlib 3.14+ → zstandard → pyzstd → node:zlib → zstd CLI`) with torn crash-tail frame recovery, V4 `role="tool"` + `toolCallId` and V3 nested `tool-result` export, and spec-compliant V4 `.v4.jsonl.zstd` write-back (`workspace.json` canonical `realpath` registration + `session_projcache` cleanup).
- **Token Ledger, Bidirectional Skills & Cordis MCP (`cv_ledger.py`, `cv_skills.py`, `cv_dsh.py`)**: Bumps `PRICING_REV = 5` with `deepseek-v4*` / `deepseek-v4-flash*` / `deepseek-flash*` rates; adds bidirectional `dsh` Skill inventory/migration and idempotent `$DSH_HOME/cordis.patch.yml` global MCP injection.
- **Real-Machine Verified**: Verified live in **DeepSeek Harness Desktop**—migrated an Antigravity session into V4 `.jsonl.zstd` (`ContextVault` workspace rendered natively), spawned `mcp_server.py` directly via Cordis patch for live `vault_*` tool calls, and invoked migrated `/tdd` Skill directly in composer.

### 🔌 Selective MCP Onboarding & 5-Client Safe Uninstall / 按需接入与 5 端安全移除
- **Binary vs. Residual Detection (`tools/cv.py`, `tools/cv_dsh.py`)**: `_detect_mcp_client_app()` checks real executables/CLIs/VS Code extensions so leftover config dirs from uninstalled clients (e.g. `~/.cursor`) are flagged `residual: true` instead of being blindly written by `--all`.
- **Per-Client Safe Removal (`POST /ops/mcp/uninstall`, `cv mcp uninstall`)**: Strips only the `context-vault` entry across Claude Code, Codex (TOML table state machine), Cursor, Antigravity (both config paths), and DeepSeek Harness (`cordis.patch.yml` YAML block/child pruning) while writing `.bak-cv` backups and keeping all other user servers intact.
- **Clean Console UX (`tools/ui/console_ops.js`, `console.html`, `console.css`)**: Replaces blind "Install All" with per-row checkboxes, `接入已选 (N)`, and collapses undetected/uninstalled clients under `其他受支持的客户端`.

### 🎯 Backend-Aligned Skill Workbench / 与已接入后端一致的 Skill 工作台
- **Single Source of Truth (`tools/cv_skills.py`, `cv_server.py`, `console_skills.js`, `console_ops.js`)**: `connected_only=1` filters Skill sources to **connected MCP clients with `total > 0`**, renders target cards dynamically only for connected backends (`ZCode` inherits active `claude`/`codex` backend status), invalidates stale Skill caches right after MCP changes, and enforces HTTP `400` server-side if targeting an unconnected client.

## ✅ Verification receipts / 验证回执
- `python -m pytest -q`: **251 passed**.
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` with `connected_only=1`, residual MCP & DSH checks): **console unit OK**.
- `python tools/check_console_overflow.py`: **28/28 OK** (real headless Chromium `380 / 900 / 1440 / 2560px` × all tabs & subtabs — zero page-level overflow).
- `dist/ContextVault.exe` (`14.39 MiB`, `15,088,560 bytes`, SHA-256 `91cdd955eb46c2934a1634f94ecdea8c3ec4137b10772901d2f3a65139a3afa1`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.11.0`) + `tools/list` (`6` tools).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.10.3...v0.11.0
