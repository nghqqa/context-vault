# Context Vault v0.10.2 — Pure 4-Tab Zero-Key Console, Multi-Agent Overview & Lightweight Tray

> **EN** | A focused **product-clarity & subtraction release** with zero third-party dependencies: removes the legacy Headroom-era Sentinel (`哨兵`) tab, `/probe_events` & `/audit` overview cards, and 30s background polling from the Web Console so all core features remain **100% local and zero-API-key** across 4 clean sections (`Overview` / `Snapshots` / `Migrate` / `Settings & Ops`). Replaces the Overview right rail with a zero-extra-request **Multi-Agent Asset Distribution (`多端资产分布`)** card backed by `/ledger` (click any client to jump directly to its snapshots) alongside expanded memory highlights (`6` items) and aligned `4`-column mobile navigation. Also slims down the Windows tray app (`ContextVault.exe`) to pure local indexing, native zero-Python MCP, multi-client sync, memory distillation, ledger capture, and daily zip backups by removing background probe and switch-watcher threads.

> **中文** | 本版是一次**聚焦主线与纯粹本地化**的减法迭代：彻底移除早期 Headroom 时期的「哨兵」独立分区、概览页 `/probe_events` 与 `/audit` 探测卡片及后台 30s 轮询，让 Web 控制台回归极简的 **四分区（概览 / 快照资产 / 迁移 / 设置与运维）** 与 **零 API Key、纯本地** 产品心智；概览右栏替换为 **零额外请求** 的「**多端资产分布**」卡片（直接复用 `/ledger` 数据，点击任意客户端一键直达筛选快照）与扩展记忆摘要（最多 6 条），移动端导航同步对齐为 4 列。Windows 绿色托盘版（`ContextVault.exe`）也同步移除后台探测与切换守望线程/菜单，更加轻量纯粹。

## ✨ Highlights / 亮点

### 🧭 Pure 4-Tab Zero-Key Web Console / 极简四分区零外发控制台
- **Simplified Navigation & Layout (`tools/ui/console.html`, `console_core.js`, `console.css`)**: Consolidated sidebar tabs from 5 to 4 (`1 概览` / `2 快照资产` / `3 迁移` / `4 设置与运维`), matching the `repeat(4, minmax(0, 1fr))` responsive mobile navigation cleanly down to `380px` width.
- **Multi-Agent Asset Distribution (`tools/ui/console_overview.js`)**: New `#ov-agents` right-rail card displays per-client session counts, snapshot versions, and total input tokens directly from the existing `/ledger` payload (zero additional HTTP round-trips) with click-through filtering into `快照资产`.
- **Cleaner Overview & Docs**: Expanded `最新记忆摘要` to `6` items, removed unused `.probe-*` / `.tbl-probe` / `.lat*` styles while keeping shared ledger strip styles intact, and updated `DESIGN.md`, `USAGE.md`, `README*.md`, and `docs/site/index.html` to reflect the 4-tab design (`100%` backward-compatible CLI & `vault.db` schema).

### 🪶 Lightweight Zero-Probe Desktop Tray / 轻量化桌面托盘 (`pro/tray_app.py`)
- **Two-Thread Core Architecture**: Removed periodic upstream sentinel (`start_sentinel`) and Headroom switch-watcher (`start_watch`) threads along with their tray menu items; keeps only the resident hot-index/MCP service (`127.0.0.1:8791`) and automatic session sync / memory extraction / ledger sync / daily zip backup loop.

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **241 passed**.
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` with `T1` 4-tab & `O3` `#ov-agents` assertions): passed.
- `python tools/check_console_overflow.py`: **28/28 OK** (real headless Chromium `380 / 900 / 1440 / 2560px` × `ov`, `snap`, `snap:tree`, `snap:mem`, `mig`, `mig:sk`, `ops` — zero page-level overflow).
- `dist/ContextVault.exe` (`14.00 MiB`, `14,679,818 bytes`, SHA-256 `80067d5afc6d54b0b33f136c84d1766d3b7828a750fb48ca5cba25058ac182d7`): verified `--check` (`OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.10.2`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.10.1...v0.10.2
