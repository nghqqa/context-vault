# Context Vault v0.8.0 — Multi-Client Ledger, Native Antigravity Sync, AML & Tray Exe

> **EN** | A major usability, observability, and architecture release: local multi-client usage ledger (`cv_ledger`) with 5h rolling quota ETA and Antigravity per-model tracking, native zero-dependency Antigravity `.db`/WAL reader (0.2s sync) with O(1) incremental index dedup, Swiss International Style console + redesigned quota cards + full memory CRUD/pinning, unified AML `/aml/*` tenant-isolated adapter (`cv-aml` & `Dockerfile.aml`), modularized CLI architecture + optional Ollama/LLM semantic layer (`cv semantic`), `pip install -e .` + `cv setup --mcp` + `cv serve --open`, and a ready-to-run Windows tray binary (`ContextVault.exe`).

> **中文** | 本版全面升级本地可观测性、跨端同步速度、架构清晰度与开箱体验：新增零外部依赖的**本地多端用量账本**（5h 滚动配额 ETA 与 Antigravity 分模型精确统计）、**原生 Antigravity `.db`/WAL 增量读取器**（同步由 ~60s 降至 ~0.2s）与 O(1) 增量索引去重、**瑞士国际主义重构 Web 控制台**（新版配额进度卡 + 记忆增删改置顶 + Antigravity/动态来源筛选）、**统一 AML `/aml/*` 多租户接口与容器镜像**、**`cv.py` 模块化拆分与可选 Ollama/本地 LLM 语义层**、`pip install -e .` / `cv setup --mcp` / `cv serve --open` 一键上手流，并在 Release 附件中首次提供开箱即用的 Windows 托盘程序 **`ContextVault.exe`**。

## ✨ Highlights / 亮点

### 🪟 Windows Tray App (Ready-to-Run Binary) / Windows 托盘程序直出
- **Download `ContextVault.exe`** from Release assets below — double-click to run the resident hot-index server (`127.0.0.1:8791`), Web Console, periodic multi-client sync, upstream sentinel, switch watcher, and daily zip backups with zero Python setup.
- 在下方 Release 附件直接下载 **`ContextVault.exe`**，双击即可在系统托盘常驻运行热索引服务、Web 控制台、定时增量同步、上游哨兵、切换守望与每日自动备份，免配 Python 环境。

### 📊 Local Multi-Client Ledger & Redesigned Quota Panel / 多端账本与新版配额面板
- Direct local ingestion across Claude Code, Codex, ZCode, and Antigravity (`cv_ledger`) with builtin pricing, cache-savings stats, 5h/24h/7d/30d windows, rolling cooldown ETA, and real-time Antigravity LSP / Zhipu quota cards.
- Console redesigned in Swiss International Style with dark/light modes, high-contrast dual-track quota progress bars, full memory CRUD/pinning, and dynamic agent filtering in the project tree.

### ⚡ Native Antigravity `.db`/WAL Sync & O(1) Incremental Dedup / 原生 WAL 同步与极速增量索引
- Zero-dependency stdlib SQLite + protobuf decoder (`cv_antigravity.py`) reads active Antigravity `.db`/`.db-wal` sessions directly with exact `cwd` attribution and WAL-aware watermarks — bypassing `smigrate` failures and cutting sync time from ~60s to ~0.2s.
- `cv_index.py` adds `idx_blockhash_h` + in-memory dedup set to eliminate quadratic full-table scans during bulk incremental indexing.

### 🧩 AML Multi-Tenant Adapter, MCP Budget & Secret Redaction / AML 适配、预算裁剪与脱敏
- Unified `/aml/*` routes on `cv serve` plus standalone `cv-aml` and `Dockerfile.aml` with tenant isolation, `request_id` idempotency (`409` conflict), and shared live index visibility.
- MCP `budget_tokens` trimming across retrieval tools, `--redact` secret redaction on export/briefs, and CodingVault-Bench panels D/E/F.

### 🏗️ Modular Core, Semantic Layer & 3-Minute Onboarding / 模块化架构、语义层与极速上手
- Split the 6,000-line `tools/cv.py` into focused modules (`cv_server.py`, `cv_antigravity.py`, `cv_migrate.py`, `cv_ops.py`) while keeping zero third-party dependencies for the core engine.
- Added `cv semantic` (`cv_semantic.py`) for optional local Ollama embeddings reranking, LLM memory distillation, and auto session titles (off by default; zero outbound traffic unless configured).
- Added `pyproject.toml` (`pip install -e .` exposing `cv`, `cv-mcp`, `cv-aml`), `cv setup --mcp`, `cv serve --open`, upgraded `cv --help`, and completely rewritten [docs/USAGE.md](docs/USAGE.md).

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **234 passed** (Ubuntu + Windows × Python 3.10 / 3.12 CI green, including hot/cold equivalence, AML concurrency/idempotency, console JS unit checks, and Chromium multi-viewport layout probe).
- `dist/ContextVault.exe --check`: packaged via PyInstaller (`--onefile --noconsole`) and smoke-tested cleanly on Windows.

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.7.0...v0.8.0
