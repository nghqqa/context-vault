# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Local-first AI software developers using multiple coding agents concurrently (ZCode, Codex, Claude Code, Antigravity, Cursor, OpenCode, Qwen Code, Kimi CLI, Copilot) on desktop workstations (`380px` compact panels through `1440px` and `2560px` wide displays). They inspect session history, search past project contexts, sync conversations across tools, verify migration fidelity before touching live agent directories, audit token/cache ledgers, and monitor upstream relay endpoints.

## Product Purpose

Context Vault Console is the local HTTP workbench served by `cv serve` and the packaged `ContextVault.exe` tray daemon (`http://127.0.0.1:8791`). It transforms proprietary, vendor-locked AI coding sessions into immutable, content-addressed JSON snapshots (`vault/snapshots/cv_*.json`) backed by SQLite full-text and lineage indexing (`vault.db`), letting users inspect, search, sync, migrate, and audit their AI work without leaving loopback or sending data externally.

## Positioning

Unlike ephemeral one-shot session converters, Context Vault acts as a permanent, auditable asset vault:

- Immutable content-addressed storage with `parent_snapshot` version lineage (`leaves=1` deduplication and timeline drill-down).
- Honest migration preview & execution: dry-run to `vault/outbox/` by default; explicit double-confirmed writes (`--apply`) for real tool directories; transparent reporting of non-portable items (`thinking`, dropped blocks, missing assistant text).
- Zero-proxy accounting aggregated directly from embedded session usage and `ZCode` `model_usage`.

## Operating Context

- Self-contained single-file HTML/CSS/JS template (`CONSOLE_HTML` inside `tools/cv.py`) with **zero third-party runtime dependencies** (no npm bundle, no external CDNs, works completely offline on loopback).
- Loopback security enforcement in HTTP server: Host/Origin must resolve to `127.0.0.1` / `localhost` / `::1`; `POST` requires `application/json`; optional `CV_SERVE_TOKEN` strips query token into an `HttpOnly` cookie on initial load.
- Primary operational rhythm: periodic background sync & tray operation mixed with rapid burst inspection — locating an active project conversation, importing/cleaning Antigravity sessions without opening PowerShell, checking relay `/v1/models` health, or generating a cross-agent handoff brief.

## Capabilities and Constraints

- **Core Sections**: Overview (`ov`), Snapshot Assets (`snap` — table view + project folder tree + Antigravity sync/batch), Migration Workbench (`mig` — source selection, target selection, fidelity preview, outbox/apply execution), Upstream Sentinel (`probe` — live/recent relay probe status).
- **HTTP API Contracts** (served on loopback):
  - Read: `/healthz`, `/ledger`, `/recent` (`limit<=200`, `offset`, `order`, `leaves=1`), `/search`, `/projects`, `/snapshot`, `/preview`, `/timeline`, `/memory`, `/audit`, `/probe_events`, `/antigravity/sync`, `/antigravity/batch`.
  - Write: `POST /sync`, `POST /migrate`, `POST /migrate_project`, `POST /antigravity/sync`, `POST /antigravity/batch`, `POST /probe`.
- **Inferred Scope for Redesign** *(inferred from explicit user redesign request `"全面修改为现代控制台"` and desktop screenshot evidence)*: preserve every existing workflow, endpoint, shortcut (`1–4`), and automated regression contract (`tools/check_console_js.py`, `tools/check_console_overflow.py`, `tests/`), while eliminating desktop table horizontal overflow, deduplicating redundant navigation/search chrome, removing raw internal data leaks (`**` Markdown, `ERR ERR`, `Relay · 999555999` repetition), unlocking full-vault searchable source selection in Migration, and condensing Project Tree toolbars.

## Brand Commitments

- Name: **Context Vault**
- Voice: Direct, factual, technically honest Chinese UI copy — explicitly distinguishes estimated vs exact counts, dry-run outbox vs irreversible real-directory writes, and sample-scoped sentinel metrics vs uptime claims.

## Evidence on Hand

- Verified live workspace vault data: `1,931+` snapshot versions, `1,544+` independent sessions, `691k+` indexed terms, ZCode `model_usage`, and `/probe_events` telemetry.
- Verified automated regression scripts: `tools/check_console_js.py`, `tools/check_console_overflow.py`, `tests/test_cv.py`.

## Product Principles

1. **Honesty before decoration**: Never invent metrics, hide migration losses, or blur the boundary between safe `vault/outbox/` exports and writes to live agent directories.
2. **Keyboard and high-density fluency**: Keep scan lines tight, tabular numerals aligned, and primary inspect/search/export/migrate actions reachable without multi-tier horizontal scrolling.
3. **Zero external weight**: Everything renders fast and offline from embedded vanilla HTML, CSS, and JavaScript inside `tools/cv.py`.
4. **Progressive disclosure over clutter**: Keep default screens focused on immediate work (recent sessions, actionable project sync, clean migration flow) and tuck secondary audit/caliber details behind clean disclosures.

## Accessibility & Inclusion

- Semantic landmarks, headings, table `<th scope="col">`, native `<dialog>` modals/drawers (`closedby="any"`), `popover="manual"` top-layer status toasts, `prefers-reduced-motion` compliance, visible focus rings, and ≥4.5:1 text / ≥3:1 UI contrast.
