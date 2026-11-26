# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Local-first AI software developers using multiple coding agents concurrently (`Claude Code`, `Codex`, `ZCode`, `Antigravity`, `DeepSeek Harness`, `Cursor`, `OpenCode`, `Qwen Code`, `Kimi CLI`, `Copilot`) on Windows, macOS, and Linux workstations (`380px` compact panels through `1440px` and `2560px` wide displays). They inspect session history, search past project contexts, curate cross-session memories, compile project rules, migrate conversations and skills across tools with explicit fidelity preview, audit token/cache ledgers & 5h quota windows, and manage local MCP bindings and encrypted/verified backups.

## Product Purpose

Context Vault Console is the local HTTP workbench served by `cv serve` and the packaged `ContextVault.exe` Windows tray daemon (`http://127.0.0.1:8791`). It transforms proprietary, vendor-locked AI coding sessions into immutable, content-addressed JSON snapshots (`vault/snapshots/cv_*.json`) backed by SQLite full-text and lineage indexing (`vault.db`), letting users inspect, search, sync, migrate, compile memory rules, and audit their AI work without leaving loopback or sending data externally.

## Positioning

Unlike ephemeral one-shot session converters, Context Vault acts as a permanent, auditable asset vault:

- Immutable content-addressed storage with `parent_snapshot` version lineage (`leaves=1` deduplication and timeline drill-down).
- Honest migration preview & execution: dry-run to `vault/outbox/` by default; explicit double-confirmed writes (`--apply`) for real tool directories; transparent reporting of non-portable items (`thinking`, dropped blocks, missing assistant text), batch harness migration, and cross-tool `SKILL.md` migration with unified diffs and rollback backups.
- Zero-proxy accounting aggregated directly from embedded session usage, `ZCode` `model_usage`, and IDE/plan quota feeds.
- Bilingual/temporal conflict-superseding long-term memory (`MEMORY.md`, `vault.db`) compiling to protected `AGENTS.md` / `CLAUDE.md` / `.cursorrules`.

## Operating Context

- Self-contained UI templates under `tools/ui/` (`console.html`, `console.css`, `console_core.js`, `console_overview.js`, `console_snapshots.js`, `console_memory.js`, `console_migrate.js`, `console_skills.js`, `console_ops.js`, `dashboard.html`) assembled by `tools/cv_ui.py` with **zero third-party runtime dependencies** (no npm bundle, no external CDNs, works completely offline on loopback).
- Standalone Windows tray UI in `pro/tray_ui.py` & `pro/tray_app.py` (Swiss high-DPI layered popover with `Tab`/`Up`/`Down`/`Space`/`Enter` keyboard navigation and `Vault Core` multi-DPI `.ico`).
- Public product & release portal at `docs/site/index.html` (`https://vault.nghqqa.cn/`), sharing the exact same Swiss design tokens.
- Loopback security enforcement in HTTP server: Host/Origin must resolve to `127.0.0.1` / `localhost` / `::1`; `POST` requires `application/json`; optional `CV_SERVE_TOKEN` strips query token into an `HttpOnly` cookie on initial load.

## Capabilities and Constraints

- **Core Console Sections (`TABS = ["ov", "snap", "mig", "ops"]`)**:
  - Overview (`ov`): 4-cell KPI ledger, recent sessions, multi-agent distribution, latest memory highlights, and collapsible 5h quota & multi-window usage card (`/ledger/usage` + CSV/Markdown export).
  - Snapshot Assets (`snap`): searchable snapshot table (`@container` responsive without column overlap), Project Tree (`Antigravity` cleanup & batch import), and Long-Term Memory library (compose, pin/unpin, confirm-before-delete, dedup, rule compiler).
  - Migration Workbench (`mig`): unified 3-mode control — Single Session (`#mig-scope-single`), Batch Harness Migration (`#mig-scope-batch`), and Cross-Tool Skill Migration (`#mig-tab-skills`).
  - Settings & Ops (`ops`): On-demand MCP client management (install/uninstall with `.bak-cv`), optional local Ollama embeddings & LLM refinement, quota token config (confirm-before-clear), zip backup creation/verification, version pruning, and audit timeline.
- **HTTP API Contracts** (served on loopback):
  - Read: `/healthz`, `/ledger`, `/ledger/usage`, `/ledger/export`, `/recent`, `/search`, `/projects`, `/snapshot`, `/preview`, `/timeline`, `/memory`, `/memory/compile_rules`, `/skills`, `/skills/inspect`, `/skills/backups`, `/migrate/batch/list`, `/audit`, `/ops/mcp`, `/ops/status`, `/ops/backups`, `/ops/backup/verify`.
  - Write: `POST /sync`, `POST /migrate`, `POST /migrate_project`, `POST /migrate/batch/run`, `POST /antigravity/sync`, `POST /antigravity/batch`, `POST /skills/migrate`, `POST /skills/remove`, `POST /skills/rollback`, `POST /memory/*`, `POST /semantic/*`, `POST /ops/*`.

## Brand Commitments

- Name: **Context Vault**
- Mark: **Vault Core (`几何金库门`)** — deep carbon badge `#16181c`, precision white `C` vault door frame, Signal Red `#e1251b` lock bolt, equidistant negative-space channels (`2.3/21`), Swiss `rx=3/21` outer corner, dark-mode-only `#424852` rim stroke.
- Voice: Direct, factual, technically honest Chinese UI copy — explicitly distinguishes estimated vs exact counts, dry-run `outbox` vs irreversible real-directory writes, and keeps semantic greens (`--ok`) / ambers (`--pin`/`--warn`) strictly separated from Signal Red brand accents and errors (`--err`).

## Verification & Evidence

- Automated JS single-file compiler & unit regression: `tools/check_console_js.py` (`emit` / `run` / `unit`).
- Automated headless Chromium responsive overflow & column-collision probe across `380px`, `900px`, `1000px`, `1440px`, `2560px`: `tools/check_console_overflow.py`.
- Full Python & CLI test suite: `pytest tests/ -q`.

## Product Principles

1. **Honesty before decoration**: Never invent metrics, hide migration losses, blur `vault/outbox/` vs live writes, or color positive/online states red.
2. **Keyboard and high-density fluency**: Keep scan lines tight, tabular numerals aligned, full keyboard operation (`1–4`, `/`, `Ctrl/⌘+K`, `?`, Win32 tray `Tab`/`↑↓`/`Space`), and zero table column collisions at every breakpoint.
3. **Zero external weight**: Everything renders fast and offline from bundled vanilla HTML, CSS, JS, and standard-library Python.
4. **Progressive disclosure over clutter**: Keep default screens scannable; fold quota charts (`#ov-quota-card`), expandable JSON outputs, and secondary disclosures cleanly.

## Accessibility & Inclusion

- Semantic `<html lang="zh-CN">`, skip-link to `<main id="main-content">`, keyboard-accessible command palette (`role="combobox"`/`role="listbox"`/`role="option"`/`aria-activedescendant`), button semantics for interactive feed rows and hotkey triggers, `outline: none` zeroed out in favor of crisp `:focus-visible` rings, confirm dialogs on destructive memory/token deletions, and WCAG AA contrast (`>=4.5:1`).
