# Context Vault v0.15.0 — Bounded Decay Retrieval, Ledger Export, Backup Verification & Self-Updater

> **EN** | Product & retrieval-quality release introducing **bounded & query-differentiated time decay + a `no_decay` toggle** (lifting CodingVault-Bench `Ours` Recall@1 from `0.8070 → 0.9298` and `temporal_update` Recall@1 from `0.4762 → 0.8095` with zero stale-version intrusion), **CSV / Markdown token ledger export**, **backup inventory/CRC+SQLite integrity verification & SQL-pushed audit timeline filters** in CLI/HTTP/Web Console, and a **SHA-256-verified Windows tray self-updater**.

> **中文** | 本次聚焦**产品体验与检索质量提升**：推出**有界/分化时间衰减与「不衰减 (`no_decay`)」开关**（CodingVault-Bench `Ours` 综合 `Recall@1` 由 `0.8070 → 0.9298`，`temporal_update` 由 `0.4762 → 0.8095`，同时保持版本入侵率 `0.0`）；新增**用量账本 CSV / Markdown 报表导出**、**备份清单与 ZIP CRC + SQLite `quick_check` 校验**、**审计时间线与异常筛选**，以及**内置 SHA-256 强制校验的 Windows 托盘自动更新闭环**。

## ✨ Highlights / 亮点

### 🎯 Bounded & Differentiated Time Decay + `no_decay` Toggle / 有界分化衰减与不衰减开关
- **Bounded & Identifier-Aware Floor (`tools/cv_rank.py`, `cv_index.py`)**: Replaced unbounded exponential decay with `DEFAULT_DECAY_FLOOR = 0.65` (overridable via `CV_DECAY_FLOOR`) and automatic `query_decay_floor()` lifting to `0.80` for technical identifiers, paths, and error codes so authoritative historical decisions and root causes remain retrievable while fresh context stays prioritized.
- **Three-Way Equivalent `no_decay` Toggle (`cv_server.py`, `cv_semantic.py`, `mcp_server.py`, `tools/ui/console_snapshots.js`)**: Wired `no_decay` / `decay=0` end-to-end across HTTP `GET /search` & `GET /context`, `hybrid_search`, MCP `vault_search` & `vault_context` (matching hot HTTP, readonly `search_index.db`, and cold `ColdRanker`), and the **Snapshots** toolbar `#snap-nodecay`（「不衰减」）button.
- **CodingVault-Bench (162 Gold Queries)**: `Ours` **Recall@1 `0.8070 → 0.9298` (+12.28pp)**, **MRR@10 `0.8711 → 0.9509` (+7.98pp)**, `temporal_update` **Recall@1 `0.4762 → 0.8095` (+33.33pp)**, **Recall@5 `1.0000`**, stale-version intrusion **`0.0000`**, abstention accuracy **`0.8333`**.

### 📊 Token Ledger CSV / Markdown Export / 用量账本报表导出
- **CLI, HTTP & Web Console (`cv_ledger.py`, `cv_ledger_cli.py`, `cv.py`, `cv_server.py`, `tools/ui/console_overview.js`)**: Added `export_ledger_report` and `cv ledger export --format csv|markdown|md --window 5h|24h|7d|30d|all [--account ...] [--out ...]`, `GET /ledger/export` (attachment download or `?json=1`, HTTP 400 validation), and **导出 CSV / 导出 MD** controls in the Overview Quota header.

### 🛡️ Backup Integrity Verification, Audit Filtering & Safe Downloads / 备份校验、安全下载与审计筛选
- **Backup Inventory & CRC+SQLite Verification (`cv_ops.py`, `cv.py`, `cv_server.py`, `tools/ui/console_ops.js`)**: Added `list_backups_api`, traversal-guarded `resolve_backup_file`, `verify_backup_api` (`ZipFile.testzip()` CRC-32 + extracted `vault.db` `PRAGMA quick_check(1)` + archive SHA-256), CLI `cv backup --list` / `--verify [name]`, HTTP `/ops/backups`, `/ops/backup/verify`, `/ops/backup/download` (with automatic gzip skip for `application/zip` responses), and collapsible backup rows in **Settings & Ops**.
- **Audit Kind Filtering**: Updated `audit_summary_and_timeline` to push `kind` predicates (`all` / `error` / `abnormal` / `switch` / `ok` / `model_missing`) into SQL before `LIMIT`, expanded `/audit` cache keys with `kind` & `limit`, and added the full-width **审计与运维时间线** card to the Web Console.

### 🔄 Windows Tray SHA-256 Self-Updater / Windows 托盘 SHA-256 自更新
- **Verified Pure-Stdlib Update Pipeline (`cv_ops.py`, `pro/tray_app.py`, `pro/build.py`)**: Added `parse_sha256sums`, `select_release_assets`, streaming `download_and_verify_exe` (strict SHA-256 mismatch rejection & `.part` cleanup, HTTP forbidden except loopback test origins), and `write_windows_update_script`. Frozen Windows tray runs confirm via native dialog, verify SHA-256, apply the update batch script, and relaunch; other modes safely open the release page. Build script now emits `dist/SHA256SUMS.txt` alongside `ContextVault.exe`.

## ✅ Verification receipts / 验证回执
- `python -W error::SyntaxWarning -W error::DeprecationWarning -m py_compile` (`tools/*.py`, `pro/*.py`, `tests/*.py`): **47 files OK** (zero warnings).
- `python -m pytest -W error::SyntaxWarning -q`: **273 passed** (0 failed, 0 skipped — includes comprehensive `tests/test_v015_features.py` regression suite).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `B07`, `Q7`, `O4`, `O5` & `0.15.0` status): **`console unit OK`** (`style=1 script=1`).
- `python tools/check_console_overflow.py`: **32/32 OK** (`380 / 900 / 1440 / 2560px` × 4 console sections — zero page-level overflow).
- Stdlib-only 26-module engine import smoke check (forward & reverse load order): **26 modules fwd+rev OK**, `UA=ContextVault/0.15.0`.
- `python benchmarks/run_bench.py`: `Ours` Recall@1 **`0.9298`**, Recall@5 **`1.0`**, MRR@10 **`0.9509`**, stale intrusion **`0.0`**, abstention **`0.8333`**, `temporal_update` Recall@1 **`0.8095`**.
- `dist/ContextVault.exe` (`14.47 MiB`, `15,170,917 bytes`, SHA-256 `abd8d65d50f8d3aaa838431e429bc6654976a34f79fbd7bd9f3d0e2834cf94de`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.15.0`) + `tools/list` (`8` tools, `no_decay` on `vault_search` & `vault_context`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.14.0...v0.15.0
