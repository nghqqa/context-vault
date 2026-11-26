# Context Vault v0.13.1 — Modular Engine Refactor & Loopback Bind Hardening

> **EN** | Maintenance and architecture release delivering a **6/6 green CI matrix** (fixing the `macos-latest` loopback bind hang), zero-regression **26-module stdlib decomposition** across five oversized engine files, **single-source SSR UI templates** + shared loopback HTTP server, and full removal of the legacy `cv_dense` compatibility shim.

> **中文** | 本次维护与架构重构版本修复 `macos-latest` 回环绑定卡顿使 **GitHub CI 全矩阵 6/6 绿灯**，并将 5 个超千行核心引擎文件无损拆分为 **26 个纯标准库模块**（保持 100% re-export 与测试 `monkeypatch` 兼容），同时统一回环 HTTP 服务与控制台迁移模板信源、清理 `cv_dense` 遗留兼容壳。

## ✨ Highlights / 亮点

### 🌐 Loopback Server Unification & macOS CI Fix / 回环服务统一与 macOS CI 修复
- **Shared `LocalThreadingHTTPServer` (`tools/cv_core.py`, `cv_server.py`, `aml_adapter.py`)**: Extracted a shared `LocalThreadingHTTPServer` subclass overriding `server_bind()` to bypass `socket.getfqdn()` reverse-DNS lookup on `127.0.0.1`, eliminating the macOS CI hang in `test_http_server_route_validation_and_body_guards` and deduplicating server classes between the Web Console and AML adapter.

### 🧩 Zero-Regression Modular Decomposition (26 Runtime Modules) / 无损模块化瘦身
- **5 Oversized Modules Split Cleanly (`tools/cv*.py`)**:
  - `cv.py` (`2,630 → 1,125` lines) split into `cv_export.py`, `cv_projects.py`, `cv_ledger_cli.py`, and consolidated operational commands in `cv_ops.py`.
  - `cv_index.py` (`1,681 → 1,067` lines) extracted stateless tokenization, BM25 scoring, chain folding, and `ColdRanker` into `cv_rank.py`.
  - `cv_dsh.py` (`1,456 → 990` lines) extracted path encoding, discovery, and 5-tier Zstd codecs into `cv_dsh_codec.py` (with dynamic `_active_zstd_backend()` hook).
  - `cv_skills.py` (`1,650 → 952` lines) extracted write migration, unified-diff inspection, removal, backup/rollback, and `cmd_skills` into `cv_skills_ops.py` (using lazy `_sk()` lookup with zero circular imports).
  - `cv_antigravity.py` (`1,573 → 893` lines) extracted Protobuf codecs, step timestamp/payload normalization, `export_antigravity_db`, and summary index syncing into `cv_antigravity_pb.py`.
- **100% Backward Compatibility**: All top-level re-exports and dynamic test hooks remain intact (`test_improvements.py` asserts identity across all 8 split submodules).

### 🎨 UI Template Single-Sourcing & Legacy Cleanup / 前端模板单一信源与遗留清理
- **SSR Placeholder Expansion (`cv_ui.py`, `tools/ui/console.html`)**: Unified single-session and batch-session target cards (`#tgt-cards` / `#mb-tgt-cards`) and Markdown budget `<select>` options (`#mig-budget` / `#mb-budget`) under shared SSR templates while keeping strict single-`<style>` and single-`<script>` self-containment.
- **Compatibility Shim Removal & Build Sync**: Deleted legacy `tools/cv_dense.py` (updating `benchmarks/run_bench.py`, `tests/test_cv_dense.py`, `tests/test_semantic.py` directly to `cv_semantic`) and synchronized all 26 modules in `pyproject.toml`, `pro/build.py`, and `.github/workflows/ci.yml`.

## ✅ Verification receipts / 验证回执
- `python -m pytest -q`: **259 passed** (0 failed, 0 skipped — includes extended modularization, subclassing & template expansion assertions in `test_improvements.py`).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `S1-S8`, `MB1-MB4` & `0.13.1` status): **console unit OK** (`style=1 script=1`).
- `python tools/check_console_overflow.py`: **32/32 OK** (`380 / 900 / 1440 / 2560px` × 4 console sections — zero page-level overflow).
- Stdlib-only 26-module engine import smoke check (forward & reverse load order): **`ALL_26_IMPORTS_OK`** (`REVERSE_26_AND_VERSION_OK: 0.13.1`).
- GitHub Actions CI matrix (`ubuntu-24.04` 3.9, `ubuntu-latest` 3.10/3.12, `windows-latest` 3.10/3.12, `macos-latest` 3.12): **6/6 jobs green** (`run 45777967940`).
- `dist/ContextVault.exe` (`14.43 MiB`, `15,134,687 bytes`, SHA-256 `a290b69839554f0e905af002e8a69fa1c8b71b06ca0757690ae55ad69c84c7a3`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.13.1`) + `tools/list` (`6` tools).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.13.0...v0.13.1
