# Context Vault v0.15.2 — Vault Core Brand Icon, Swiss Grid UI Polish, Pure Local Ops Log & POSIX Antigravity URI Fix

> **EN** | Product polish and cross-platform reliability release: introduces the bespoke **Vault Core** geometric brand mark (with native Win32 multi-size DIB `.ico` resources and Shell cache refresh for Windows Explorer), elevates the local Web Console & static Token Dashboard with Swiss-grid WCAG AA typography/contrast refinements, converts the Ops timeline from legacy upstream probe logs into a **pure local ops log** (`/ops/log`), fixes four-slash `file:////` POSIX workspace URIs when migrating to Antigravity on Linux/macOS, and ships SEO/OG metadata + clock-independent CI tests.

> **中文** | 本次版本围绕**品牌识别升级、界面精细打磨、纯本地运维日志**与 **Linux/macOS 跨平台修复**展开：正式启用原创「**Vault Core 几何金库门**」品牌标识（Win32 原生 16–128 DIB + 256 PNG `.ico` 资源确保 Windows 资源管理器一致渲染），完成瑞士国际主义网格 UI 与 WCAG AA 对比度审查精修；彻底告别历史探针记录，升级为**纯本地运维日志**（`/ops/log` + 静态看板 `ops_events`）；同时修复 Linux / macOS 迁移会话到 Antigravity 时的 `file:////` 四斜杠 URI 缺陷并加固 SEO/OG 与跨平台 CI。

## ✨ Highlights / 更新亮点

- **Vault Core Brand Mark & Native Win32 `.ico` Packaging (`pro/tray_ui.py`, `pro/build.py`, `tools/ui/console.html`, `docs/site/index.html`)**:
  - Replaced generic tray visuals with the Swiss-grid geometric Vault Core monogram (`#16181c` deep ink body + architectural `C` doorway + Swiss-red `#e1251b` latch) unified across tray icon, Web Console, Token Dashboard, Website, and OG social cards.
  - Rewrote `.ico` generation to emit native 32-bit BGRA bottom-up DIB (`16`–`128px`) plus PNG (`256px`) with `SHChangeNotify` / `ie4uinit.exe` cache invalidation so Windows Explorer displays crisp icons at every DPI scale.
- **Swiss Grid Console Remediation & Static Asset Refresh (`tools/ui/console.css`, `console_*.js`, `docs/assets/*.png`)**:
  - Audited typography scale, spacing rhythm, WCAG AA text contrast (`--ink-3` light/dark calibration), Retina 2× dark/light screenshots, and 5-viewport Chromium layout stability.
- **Pure Local Ops Log & Probe Legacy Cleanup (`tools/cv_core.py`, `tools/cv_ops.py`, `tools/cv_server.py`, `tools/ui/console_ops.js`, `tools/ui/dashboard.html`)**:
  - Added `ops_events` table and `GET /ops/log` aggregating `ops_events`, `vault/backups/cv-backup-*.zip`, and `vault/migrations.jsonl` while isolating/ignoring legacy `probe_events`.
  - Renamed the Ops bottom card from “审计与运维时间线” to **“本地运维日志”** (`全部 / 同步 / 备份/裁剪 / 迁移 / MCP / 失败` filters) and updated `dashboard.html` accordingly while keeping `/audit` backwards-compatible.
- **POSIX Antigravity `file:///` URI Fix & SEO Hardening (`tools/cv_antigravity_pb.py`, `docs/site/`, `tests/`)**:
  - Normalized `_antigravity_ws_uris` and `_antigravity_set_workspace` so Linux/macOS paths like `/home/alice/my proj` emit RFC 3-slash `file:///home/alice/my%20proj` instead of `file:////home/...`, matching Antigravity workspace projects cleanly on Ubuntu (`test_antigravity_posix_workspace_uris_no_quad_slash`).
  - Added `robots.txt`, `sitemap.xml`, Open Graph / Twitter Card (`og-image.png`), `SoftwareApplication` JSON-LD on `docs/site/`, and made `test_agv_hub_discovery_and_path_a` relative to UTC `now`.

## ✅ Verification receipts / 验证回执
- `python -W error::SyntaxWarning -W error::DeprecationWarning -m py_compile` (`tools/*.py`, `pro/*.py`, `tests/*.py`): **50 files OK** (zero warnings).
- `python -m pytest -W error::SyntaxWarning -q`: **275 passed** (0 failed, 0 skipped).
- `python tools/check_console_js.py` (`emit` + `node --check` + `run` + `unit` covering `0.15.2` status & `O5` `/ops/log`): **`console unit OK`**.
- `python tools/check_console_overflow.py`: **5 viewports × 8 routes/tabs OK** (`380 / 900 / 1000 / 1440 / 2560px` — zero page-level overflow).
- Stdlib-only 26-module engine import smoke check (forward & reverse load order): **26 modules fwd+rev OK**, `UA=ContextVault/0.15.2`.
- `dist/ContextVault.exe` (`17.47 MiB`, `18,319,783 bytes`, SHA-256 `19180a8a40038b8f18cc0b9ca8eb230cd9f2a7174f61ccf337511b462373f2f3`): verified `--check` (`自检结论: OK`), `--mcp` stdio JSON-RPC `initialize` (`v0.15.2`) + `tools/list` (`8` tools), and PE `RT_ICON` (`7` entries: `16..128` DIB + `256` PNG).

**Full diff**: https://github.com/nghqqa/context-vault/compare/v0.15.1...v0.15.2
