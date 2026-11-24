# Context Vault v0.10.1 — 10×–96× Skill & Endpoint Speedup, Shared Core Dedup & Modular UI

> **EN** | A comprehensive **performance & architectural cleanup** release with zero new dependencies: Skill discovery and comparison accelerate **11×–52×** (`1,181 ms → 79 ms` warm, `3,764 ms → 421 ms` cold Claude, `783 ms → 39 ms` Antigravity plugin mode) via `os.scandir`, `<=128 KiB` full / large-asset sampled hashing with `(size, mtime_ns)` self-invalidating caches, 4-thread hashing, and flash-free `{soft:true}` UI switches; hot server endpoints (`/stats` **96×**, `/ledger/usage` **35×**, `/ledger` **20×**, `/projects` **8.2×**) gain WAL-aware caching; duplicate primitives and DDL overhead collapse into `tools/cv_core.py` (skipping redundant `WAL`/`ALTER TABLE` on opened DBs); `cv_dense.py` merges into `cv_semantic.py`; `antigravity_batch` shares `plan_antigravity_batch`; `cv_server.py` switches to flat table-driven routing (`GET_ROUTES`/`POST_ROUTES`) with stdlib `RotatingFileHandler`; and `console.js` splits cleanly into 6 per-tab modules.

> **中文** | 本版是一次纯标准库零依赖的**全栈性能提速与架构瘦身**迭代：**Skill 扫描与跨端比对提速 11×–52×**（ZCode → Antigravity 全局 1,181ms → 79.3ms、独立插件 783ms → 38.7ms、Claude 130 项 3,764ms → 420.5ms 冷 / 72.1ms 温），控制台切换目标与模式平滑无闪屏；`/stats`（**96×** → 2.1ms）、`/ledger/usage`（**35×** → 4.9ms）、`/ledger`（**20×** → 2.3ms）、`/projects`（**8.2×** → 19.7ms）全线迈入毫秒级；抽出共享底座 `tools/cv_core.py` 并消除重复工具函数与 `db()` 重复 DDL；合并 `cv_dense` 至 `cv_semantic`、统一 `antigravity_batch` 批处理规划器；将 `cv_server.py` 千行闭包拆平为 `GET_ROUTES`/`POST_ROUTES` 表驱动路由并接入标准库 `RotatingFileHandler`；将 `console.js` 无损拆分为 6 个按功能内聚的前端模块。

## ✨ Highlights / 亮点

### ⚡ 10×–96× Speedup Across Skill Migration & Console Endpoints / Skill 与热点接口全线加速
- **Layered self-invalidating Skill scanning (`tools/cv_skills.py`)**: Single-pass `os.scandir` manifests, full-byte hashing for all `.md` and files `<=128 KiB` paired with sampled hashing for large binary assets, `(size, mtime_ns)` self-invalidating caches (`_FILE_HASH_CACHE`, `_DIR_DIGEST_CACHE`, `_SKILL_META_CACHE`), `root.stat().st_mtime_ns` + 30s `source_summary` cache, 4-thread parallel directory checks (`>=12` skills), lazy shape/SKILL.md short-circuit target matching, startup `_warm_async()` pre-warming, and `/skills/list?refresh=1`.
- **Flash-free UI switching (`tools/ui/console_skills.js`)**: Switching target agent or toggling Antigravity direct/plugin mode uses `loadSkills({soft:true})`, keeping the Skill checklist intact without flashing skeletons.
- **Server caching (`tools/cv_server.py`)**: `/stats` memoized on `vault.db` WAL + `snapshots/` stamps (`202.8 ms → 2.1 ms`); `/ledger/usage` & `/projects` 30s time buckets; `cv_core.db()` caches migrated `(path, st_ino, st_ctime_ns)` stamps to skip redundant `WAL` + 7× `ALTER TABLE` probes.

### 🧹 Zero-Dep Code Dedup & Clean Architecture / 代码消重瘦身与模块化重构
- **`tools/cv_core.py` thin foundation**: Consolidates `SCHEMA`, `SnapshotNotFound`, `BadMigrateTarget`, `db()`, `atomic_write_text()`, `rotate_log_if_needed()`, `vault_dir()`, `fmt_usd()`, `estimate_text_tokens()`, `parse_budget_tokens()`, `truncate_to_tokens()`, and `cv_mod()`, eliminating 6 cross-file duplicate definition groups.
- **Merged & unified backends**: Merged `cv_dense.py` core into `tools/cv_semantic.py` (keeping a 13-line compatibility re-export facade); extracted shared `plan_antigravity_batch()` in `tools/cv_antigravity.py` and slimmed `tools/antigravity_batch.py` (349 → 213 lines).
- **Flat table-driven server & modular UI**: Replaced `build_vault_server()`'s 1,009-line nested closure with `_ServerCtx` + `GET_ROUTES` / `POST_ROUTES` + stdlib `RotatingFileHandler`; split 2,313-line `console.js` into 6 cohesive `tools/ui/console_*.js` files concatenated seamlessly by `tools/cv_ui.py` (zero frontend build step).

## ✅ Verification receipts / 验证回执
- `python -m pytest tests/ -q`: **241 passed** (including new Skill self-invalidating cache & `?refresh=1`, `/stats` cache, and `cv_core.db()` skipped-DDL tests).
- `check_console_js.py` (`emit` + `run` + `unit` `S1-S4`) & `check_console_overflow.py` (real headless Chromium **380 / 900 / 1440 / 2560px = 32/32 zero overflow**).
- `dist/ContextVault.exe` (`14.01 MiB`, `14,686,419 bytes`, SHA-256 `0826ca22b2f13abf7a97b948619ffca43b74b7e504881279882e308ed4fd476e`): verified `--check` (`OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.10.1`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.10.0...v0.10.1
