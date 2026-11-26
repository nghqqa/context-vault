# Context Vault v0.15.1 — Long Project Path Memory Fix & Cross-Platform 6/6 CI Green

> **EN** | Patch release fixing project-path truncation in `memory_add_manual` (`120 → 512` chars with cross-platform basename fallback) so deep workspace directories and `/private/var/...` macOS paths match accurately across `memory_search_api` and `cv memory compile-rules`, bringing the entire 6-runner Windows/Linux/macOS CI matrix to **100% green (`274 passed`)**.

> **中文** | 本次补丁修复 `memory_add_manual` 对长项目路径的 `120` 字符硬截断问题（放宽至 `512` 字符并增加跨平台目录基名兜底），彻底解决深层 monorepo 与 macOS 临时路径尾部被截断导致的 `match_project_scope` / `compile_project_rules` 漏匹配缺陷，实现 Windows / Linux / macOS 全平台 **6/6 CI 全绿（`274 passed`）**。

## ✨ Fixes / 修复内容

- **Preserve Long Project Paths in `memory_add_manual` (`tools/cv_memory.py`, `tests/test_v014_features.py`)**:
  - Raised the path preservation threshold in `memory_add_manual()` from `120` to `512` characters and normalized path separators (`\\` → `/`) before extracting basename fallback for ultra-long paths (`> 512` chars).
  - Prevents deep project directories (such as `/private/var/folders/.../my-proj` on macOS CI runners or nested Windows/POSIX monorepos) from losing their project directory name and missing rule compilation (`compile_project_rules`).
  - Added regression coverage (`test_memory_add_manual_and_compile_rules_long_project_path`) exercising both `> 130`-char real directories and `> 512`-char synthetic paths.

## ✅ Verification receipts / 验证回执
- `python -W error::SyntaxWarning -W error::DeprecationWarning -m py_compile` (`tools/*.py`, `pro/*.py`, `tests/*.py`): **49 files OK** (zero warnings).
- `python -m pytest -W error::SyntaxWarning -q`: **274 passed** (0 failed, 0 skipped).
- GitHub Actions CI (`windows-latest` 3.10/3.12, `ubuntu-latest` 3.10/3.12, `ubuntu-24.04` 3.9, `macos-latest` 3.12): **6/6 green**.
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `0.15.1` status & `B07`, `Q7`, `O4-O5`): **`console unit OK`**.
- `python tools/check_console_overflow.py`: **32/32 OK** (`380 / 900 / 1440 / 2560px` × 4 console sections — zero page-level overflow).
- Stdlib-only 26-module engine import smoke check (forward & reverse load order): **26 modules fwd+rev OK**, `UA=ContextVault/0.15.1`.
- `dist/ContextVault.exe` (`14.47 MiB`, `15,170,777 bytes`, SHA-256 `bcf2cf959b3bd47cc69e8f6dee1fa4521c8fdb87928898134702cf75d49946cb`): verified `--check` (`自检结论: OK`) and `--mcp` stdio JSON-RPC `initialize` (`v0.15.1`) + `tools/list` (`8` tools, `no_decay` on `vault_search` & `vault_context`).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.15.0...v0.15.1
