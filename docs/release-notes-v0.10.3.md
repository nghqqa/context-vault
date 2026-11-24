# Context Vault v0.10.3 — Widescreen UI Polish, 2-Column Skill Workbench & Aligned Cards

> **EN** | A visual-ergonomics & widescreen proportion release: widens the desktop console container (`1420px → 1600px`), reverses the **Skill Migration** workbench into a wide container-query 2-column master list paired with a sticky compact action rail (`<=1100px` cleanly collapses to single-column), balances Overview into a flush `7 : 4.5` split with session-sorted multi-agent counts and expanded memory highlights, unifies the Snapshots source-filter + counter subbar, and aligns all Settings & Ops `2×2` cards to equal height with full MCP config path display.

> **中文** | 本版是一次聚焦**宽屏排版比例与细节打磨**的体验更新：全局内容容器放宽至 `1600px`；将「**Skill 技能迁移**」彻底调整为**左宽（容器查询自适应双列列表 + 视口自适应高度）+ 右紧凑吸顶操作栏**，首屏即可浏览 14+ 项 Skill 而不受右侧控件高度牵制；「**概览**」页微调为 `7 : 4.5` 并实现左右两栏底部精确齐平（多端资产按会话数降序、记忆摘要增至 7 条）；「**快照资产**」合并来源筛选与统计条；「**设置与运维**」2×2 卡片改为等高对齐、按钮贴底并放宽桌面端 MCP 配置路径展示长度。

## ✨ Highlights / 亮点

### 🖥 Widescreen Console Polish Across All 4 Tabs / 全四区宽屏比例与对齐精调
- **Reversed Skill Migration Workbench (`.sk-shell` + `.sk-list-wrap`)**: Uses `minmax(0, 1fr)` left checklist (`@container sklist >= 680px` switches seamlessly into a 2-column Swiss grid with `clamp(460px, calc(100dvh - 285px), 780px)` viewport scaling) paired with a `380px–440px` sticky right configuration rail (`tools/ui/console.html`, `console.css`).
- **Flush Overview & Sorted Agent Ledger (`tools/ui/console_overview.js`, `console.css`)**: `.ov-grid` (`7fr : 4.5fr`, `align-items: stretch`) matches left table height to the right rail; `#ov-agents` sorts by independent session count descending and `/memory` shows up to `7` entries.
- **Unified Snapshots Subbar & Aligned 2×2 Settings Grid (`tools/ui/console.html`, `console_ops.js`, `console.css`)**: Merges `#snap-agent-bar` + `#snap-meta` into `.snap-subbar`; aligns `.ops-grid` card padding, bottom action buttons (`margin-top: auto`), and extends desktop MCP path display up to `56` chars (`34 → 56`).
- **Zero-Overflow Guarantee**: Verified CSS cascade ordering so `380px` mobile stacking remains 100% overflow-free.

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **241 passed**.
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` with `T1` 4-tab & `O3` `#ov-agents` checks): passed.
- `python tools/check_console_overflow.py`: **28/28 OK** (real headless Chromium `380 / 900 / 1440 / 2560px` × `ov`, `snap`, `snap:tree`, `snap:mem`, `mig`, `mig:sk`, `ops` — zero page-level overflow).
- `dist/ContextVault.exe` (`14.00 MiB`, `14,680,535 bytes`, SHA-256 `c24768c338d355d22db6c6ad8d2f21cb236b012dac64f33b85bc0828dfdd019d`): verified `--check` (`OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.10.3`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.10.2...v0.10.3
