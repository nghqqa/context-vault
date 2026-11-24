# Context Vault v0.13.0 — Full-Harness Batch Session Migration Workbench

> **EN** | Introduces **Full-Harness / Batch Session Migration** across the Web Console, HTTP API, and CLI: inspect and export all top-level conversations from any source harness (or across all sources—with automatic aggregation of `claude`, `claude-code`, and `claude-transcript` under `claude`), filter by project or keyword, automatically deduplicate same-origin and previously migrated sessions via `vault/migrations.jsonl` (`"target"` tracking), isolate per-item failures, and perform a single bulk DB backup before writing to ZCode—paired with strict sibling-node HTML nesting fixes and real-visibility Playwright layout probes.

> **中文** | 本版正式推出**整端全量 / 批量会话迁移**全链路能力：可在 Web 控制台「迁移工作台 → 会话迁移 → 整端批量迁移」、HTTP 接口与命令行一次性盘点并迁移整个 Harness（或跨多端筛选后）的全部主对话（自动聚合 `claude` 系列子格式），按项目/关键词/已迁移状态过滤，自动跳过同源及已在 `vault/migrations.jsonl` 登记目标端的会话，支持单条异常隔离容错、Markdown 批量预算简报与 ZCode 写回整批前单次主库备份；并修复切页壳层错位嵌套问题、补强 Playwright 真实显隐高度巡检。

## ✨ Highlights / 亮点

### 🚀 Full-Harness Batch Session Migration / 整端全量与批量会话迁移
- **Backend Planning & Execution Engine (`cv_migrate.py`, `cv.py`)**: Added `plan_batch_migration()` and `run_batch_migration()` supporting source/project/query filtering, canonical Claude harness grouping, `vault/migrations.jsonl` `"target"` persistence for per-target deduplication, dry-run outbox safety (`target="markdown"` enforces `applied=False`), single bulk `ZCODE_DB` backup (`skip_backup=True` per item), and per-session exception isolation (`"failed"` item row without aborting remaining sessions).
- **CLI & HTTP API (`cv_migrate.py`, `cv.py`, `cv_server.py`)**: Updated `cv import --agent <source> --target <target> [--no-skip-migrated] [--apply]` to run full-harness batch migrations without requiring `--project` or `snapshot_id`; added serialized `GET /migrate/batch` and `POST /migrate/batch` endpoints with `ctx._write_lock` + `ctx.cache.clear()` and strict `400`/`404` validation.

### 🖥️ Batch Migration Workbench & Probe Hardening / 前端整端批量工作台与防回归加固
- **Web Console Dual-Granularity Session Migration (`console.html`, `console.css`, `console_migrate.js`)**: Added **单条精调 / 整端批量迁移** switcher (`.mig-scopebar`) and `#mig-batch-shell` two-column workbench with source pills, project/keyword/archived/dedup filters, alternate-target auto-switching when source equals target, full-match submission beyond the 300-row UI preview limit, outbox/live apply confirmation, and per-item result breakdown cards.
- **Sibling Shell Fix & Visibility-Verified Layout Probes (`console.html`, `check_console_overflow.py`, `check_console_js.py`)**: Fixed `#mig-single-shell` closing tag nesting so `#mig-batch-shell` renders as a peer under `#mig-session-pane`; upgraded `check_console_overflow.py` to assert `#mig-batch-shell` and `#mig-skills-pane` real `is_visible()` and `bounding_box()["height"] > 100` across all 4 viewports; added `MB1-MB4` console unit assertions in `check_console_js.py`.

## ✅ Verification receipts / 验证回执
- `python -m pytest -q`: **259 passed** (0 failed, 0 skipped — includes `test_batch_session_migration_plan_run_http_and_cli`).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `S1-S8`, `MB1-MB4` & `0.13.0` status): **console unit OK** (`style=1 script=1`).
- `python tools/check_console_overflow.py`: **32/32 OK** (`380 / 900 / 1440 / 2560px` × all 8 console views including visibility-verified `mig:bat` & `mig:sk` — zero page-level overflow).
- Stdlib-only 20-module engine import smoke check & `pro/*.py` `py_compile`: **`ALL_20_IMPORTS_OK`**.
- `dist/ContextVault.exe` (`14.42 MiB`, `15,121,015 bytes`, SHA-256 `5f2962e57fdcb7ec4d501d80e7e4e5a022c753fb350ec1fa2d1e4c26a86040b1`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.13.0`) + `tools/list` (`6` tools).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.12.0...v0.13.0
