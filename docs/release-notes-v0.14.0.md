# Context Vault v0.14.0 — Bidirectional MCP, Scoped Filtering & Project Rules Compiler

> **EN** | Feature release upgrading the MCP backend from a read-only lookup toolset to a **bidirectional, scope-aware memory & handoff protocol (6 → 8 tools)**, introducing **cold/hot equivalent `project` / `agent` / `since` scoped filtering**, and adding **`cv memory compile-rules`** to automatically compile active project decisions and constraints into sentinel-protected `AGENTS.md` / `CLAUDE.md` / `.cursorrules` files from both CLI and Web Console.

> **中文** | 本次功能版本将 MCP 记忆后端从纯只读查询升级为**支持双向记忆沉淀与作用域过滤的协作协议（6 → 8 个工具）**，贯通**冷/热三路严格等价的 `project` / `agent` / `since` 范围过滤原语**，并新增 **`cv memory compile-rules` 项目规则编译器**（CLI + HTTP + Web 控制台），可将项目活跃红线与决策按预算安全编译为受哨兵保护的 `AGENTS.md` / `CLAUDE.md` / `.cursorrules`。

## ✨ Highlights / 亮点

### 🔍 Scoped Filtering (`project` / `agent` / `since`) & Three-Way Equivalence / 作用域过滤与冷热三路等价
- **Unified Scope Primitives (`tools/cv_rank.py`, `cv_index.py`, `cv_semantic.py`, `cv_memory.py`)**: Added segment-boundary `project_match_rank` / `match_project_scope` (exact path, basename, and segment prefix/suffix matching without arbitrary substring bleed like `"app"` hitting `"my-app-v2"`), standard client alias expansion in `match_agent_scope` (`claude`, `dsh`, `antigravity`), Python 3.9+-compatible calendar-validated & timezone-aware `normalize_since` / `_ts_ge`, and `match_snapshot_scope`.
- **Chain-Leaf-Aware Filtering Across All 3 Paths**: Updated `VaultIndex.search/context`, `ColdRanker.search/context`, `hybrid_search`, and `memory_search_api` so `fold=True` collapses evolution chains to their latest leaf before evaluating `since` / `project` / `agent`, ensuring active long-lived chains keep ancestor hits while resident HTTP, readonly `search_index.db`, and cold file scans remain 100% equivalent.

### 🔁 Bidirectional MCP Backend (6 → 8 Tools) / 双向 MCP 协作协议（8 个工具）
- **`vault_memorize` (Direct Agent Memory Write-Back)**: Agents can persist high-value `decision` / `constraint` / `pitfall` / `preference` / `summary` items straight to `memory_items` (`snapshot_id="mcp"`, shown as `Agent 录入`), supporting `pinned`, `supersedes` conflict chaining, and per-project deduplication while keeping snapshot JSON files strictly immutable.
- **`vault_handoff` (`build_handoff_api` + `GET /handoff`)**: Generates a budgeted five-slot handoff brief (`id` or latest main conversation for `project`) bundled with active project memories, removes `compress_brief` double-counted `out_tokens`, and widens the MCP `vault_handoff` output cap to 128 KiB so trailing memories are never clipped.
- **`vault_remember` Upgrades & Resilient Cold Fallback (`tools/mcp_server.py`)**: Now emits `#<id>` prefixes and `Agent 录入` / `手动录入` provenance labels for direct consumption by `vault_memorize(supersedes=...)`, validates `kind` and clamps `limit` (`1..200`) identically across hot and cold paths, and classifies HTTP errors cleanly so `401`/`403` and legacy server `404 {"error": "unknown route"}` fall back to local cold scans without poisoning negative caches.

### 📜 Project Rules Compiler (`cv memory compile-rules`) / 项目规则编译器
- **Priority-Packed Rule Compilation (`tools/cv_memory.py`, `cv.py`, `cv_server.py`, `tools/ui/console_memory.js`)**: Packs active project memories in priority order (`constraint` → pinned → `decision` → `pitfall` → `preference` → optional `summary`) within `budget_tokens` using `truncate_to_tokens` under tight budgets, sanitizes `<!--`/`-->` markers inside content, defaults to `dry-run` with unified diff preview, and uses `<!-- context-vault:rules:start ... -->` / `<!-- context-vault:rules:end -->` sentinels + `.bak-cv` backups on `--apply` to preserve handwritten prose.
- **Safety & Legacy Compatibility**: Blocks writing empty rule blocks when no memories match (unless updating an existing sentinel), supports readonly legacy v0.1 `memory_items` tables lacking `status`/`pinned`, enforces filename/extension allowlists on both `GET` and `POST /memory/compile_rules` (rejecting non-rule reads and directories with HTTP 400), and adds the **Compile Project Rules** panel to the Web Console Memory view.

### 🧹 Engineering Hygiene & CI Modernization / 工程卫生与 CI 升级
- Zero `SyntaxWarning` / `DeprecationWarning` across all 44 `tools/*.py` and `tests/*.py` files, unified `cv_core.user_agent()` tracking `cv.VERSION`, GitHub Actions updated to Node 24-native versions (`actions/checkout@v7`, `actions/setup-python@v6`, Pages v5/v6 actions), and README 26-module / 5-native-harness documentation alignment.

## ✅ Verification receipts / 验证回执
- `python -W error::SyntaxWarning -W error::DeprecationWarning -m py_compile` (`tools/*.py`, `tests/*.py`): **44 files OK** (zero warnings).
- `python -m pytest -W error::SyntaxWarning -q`: **268 passed** (0 failed, 0 skipped — includes comprehensive `tests/test_v014_features.py` regression suite).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `MR1a-MR1d` & `0.14.0` status): **CONSOLE_JS_OK** (`style=1 script=1`).
- `python tools/check_console_overflow.py`: **32/32 OK** (`380 / 900 / 1440 / 2560px` × 4 console sections — zero page-level overflow).
- Stdlib-only 26-module engine import smoke check (forward & reverse load order): **`IMPORTS_OK: 26 modules fwd+rev, UA=ContextVault/0.14.0`**.
- `dist/ContextVault.exe` (`14.45 MiB`, `15,156,184 bytes`, SHA-256 `b1822e06bd73198cafc9dc5f88a7525fb44308426c74e737f11cf630a7738752`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.14.0`) + `tools/list` (`8` tools).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.13.1...v0.14.0
