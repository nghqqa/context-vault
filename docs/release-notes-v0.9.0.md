# Context Vault v0.9.0 — Instant SWR Console, Settings & Ops Workbench, 14 MB Zero-Python Tray + MCP

> **EN** | A full-stack polish and speed release: Web Console first-load drops from ~6s to **< 100ms** via parallel progressive rendering, non-blocking SWR quota caching, WAL-aware response caching, gzip compression, and 4 new SQLite secondary indexes; adds the **Settings & Ops (`5`)** workbench (1-click MCP install, Ollama semantic config, quota token, backup/prune), empty-vault 3-step onboarding, budgeted Markdown 1-click copy, and agent filter pills; slims the Windows tray binary `ContextVault.exe` by **-47.6%** (26.7 MB → **14.0 MB**) while embedding a native zero-Python `--mcp` stdio server; ships sanitized console screenshots and a new English user guide (`docs/USAGE.en.md`).

> **中文** | 本版是一次从底层引擎、前端体验、二进制体积到双语文档的全栈精修：通过**首屏并发渐进渲染 + 外网/LSP 实时额度 SWR 后台刷新 + WAL 感知微缓存 + 自动 gzip + 4 个高频 SQLite 二级索引**，将控制台首屏从 ~6 秒压缩至 **< 100ms（热态 65ms）**；新增第 5 分区 **「设置与运维」工作台**（MCP 一键接入、Ollama 语义层配置与精炼、额度 Token、一键备份/裁剪）、**空库 3 步新手向导**、**Markdown 简报预算档位与一键复制** 及 **快照来源筛选胶囊**；Windows 独立托盘单文件 `ContextVault.exe` 体积直砍一半（26.7 MB → **14.0 MB，-47.6%**）并原生内置免 Python `--mcp` 服务与手动检查更新；同步补齐脱敏高清截图与英文手册 `docs/USAGE.en.md`。

## ✨ Highlights / 亮点

### ⚡ Sub-100ms Console & Non-Blocking SWR Backend / 首屏秒开与后端全面加固
- **Parallel progressive boot**: `/healthz` + 6 Overview cards load concurrently and render independently; the Snapshots tab lazy-loads on demand; Overview audit uses single-query `/audit?summary_only=1` (43 KB → **151 B**).
- **SWR + single-flight quota caching**: `remote_quota_cached()` and `agv_quota_cached()` serve cached values in 0ms while refreshing asynchronously; cold starts return immediately with `quota_pending` so slow networks or closed IDEs never freeze the UI.
- **WAL-aware server cache + zero-DDL hot reads + gzip + 413 hardening**: Per-server cache keyed on `.db` + `.db-wal` stamps (auto-invalidated on writes), automatic `gzip` for responses ≥ 4 KB, 32 MB `POST` payload cap (`413`), 5 MB log rotation (`serve_http.log` / `tray.log`), graceful client-disconnect handling, and 4 new SQLite indexes (`snapshots(parent_snapshot)`, `snapshots(agent, session_ref)`, `probe_events(status, id)`, `probe_events(ts)`).

### 🛠️ Settings & Ops Workbench + Onboarding + UX Polish / 设置与运维工作台与交互闭环
- **Settings & Ops (`5`)**: Inspect and 1-click install MCP across Claude Code / Codex / Cursor / Antigravity, configure & run local Ollama embeddings/LLM memory refinement & titles, save/clear Coding Plan token & trigger ledger sync, and run online zip backups or dry-run/apply snapshot pruning right in the browser.
- **Empty-vault 3-step onboarding**: Fresh installs guide users through Sync → MCP → Ledger with zero CLI commands required.
- **Migration & Snapshots UX**: Token budget selector (`2K / 4K / 8K / Unlimited`) + 1-click Markdown brief copy & inline preview; quick agent filter pills (`Antigravity / ZCode / Codex / Claude`) on the snapshot list.

### 🪟 Slim 14.0 MB `ContextVault.exe` (-47.6%) with Native `--mcp` / 14 MB 精简单文件内置原生 MCP
- Excluded unused scientific/test packages (`numpy`, `scipy`, `pandas`, `matplotlib`, `pytest`, `tkinter`) from the PyInstaller bundle: **26.67 MiB → 13.98 MiB**, cutting cold unpack time in half.
- `ContextVault.exe --mcp --vault <dir>` acts as a native stdio MCP server — Windows users can register MCP in Claude / Codex / Cursor / Antigravity with **zero Python installation**.
- Added manual-only **Check for updates (GitHub)** tray menu item (zero background telemetry) and 5 MB `tray.log` rotation.

### 📖 Visual Showcase & Bilingual Manuals / 高清截图与双语手册
- Added zero-PII Retina dark/light screenshots in `docs/assets/` embedded across `README.md`, `README.zh.md`, `docs/USAGE.md`, and the brand-new English guide **[docs/USAGE.en.md](docs/USAGE.en.md)**, plus a dedicated Windows `.exe` onboarding & SmartScreen/uninstall guide.

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **236 passed** (including SWR non-blocking tests, gzip, 413 guard, log rotation, `/ops/*`, `/recent?agent=`, and docs hygiene).
- `check_console_js.py` (`emit` + `run` + `unit`) & `check_console_overflow.py` (real headless Chromium **380 / 900 / 1440 / 2560px × 5 tabs = 28/28 zero overflow**).
- `dist/ContextVault.exe` (`13.98 MiB`, SHA-256 `020974c6a14ef88490e08d54771ecfdd9d7baf3b033b8a67fb40ccb98925e895`): verified both `--check` (`OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.9.0`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.8.0...v0.9.0
