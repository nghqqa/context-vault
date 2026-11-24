---
name: Context Vault Console
description: Local-first AI session specimen ledger & bidirectional migration workbench (`tools/ui/console.*`)
colors:
  primary: "#e1251b"
  primary-ink-light: "#c81e14"
  primary-ink-dark: "#ff645a"
  canvas-light: "#f4f4f2"
  canvas-dark: "#18191b"
  surface-light: "#ffffff"
  surface-dark: "#202225"
  subtle-light: "#f8f8f6"
  subtle-dark: "#272a2e"
  line-light: "#d4d4d0"
  line-dark: "#383c42"
  ink-light: "#0a0a0a"
  ink-dark: "#ededeb"
  ink-secondary-light: "#333333"
  ink-secondary-dark: "#b8bcc2"
  ink-muted-light: "#666662"
  ink-muted-dark: "#8a8f96"
  warning-light: "#9a5200"
  warning-dark: "#f5b027"
  danger-light: "#990f0f"
  danger-dark: "#ff7070"
typography:
  body:
    fontFamily: '"Helvetica Neue", "Inter", "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif'
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.58
  label:
    fontFamily: '"JetBrains Mono", "Cascadia Code", Consolas, "SF Mono", ui-monospace, monospace'
    fontSize: "12px"
    fontWeight: 600
    letterSpacing: "0.03em"
rounded:
  sm: "2px"
  md: "2px"
  lg: "4px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 13px"
  button-secondary:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.ink-light}"
    rounded: "{rounded.sm}"
    height: "34px"
    padding: "0 13px"
  card:
    backgroundColor: "{colors.surface-light}"
    rounded: "{rounded.md}"
---

# Design System: Context Vault Console

## Overview

**Creative North Star: "Archival Specimen Ledger"**

Context Vault Console is a zero-dependency local web workbench served directly from Python (`CONSOLE_HTML` in `tools/cv.py`) over `127.0.0.1:8791`. Every conversation snapshot (`cv_*`) is treated as a verifiable archival specimen rather than ephemeral chat noise. Surfaces rely on disciplined 1px hairline divisions over cool mineral paper (`#f3f5f7`) or deep graphite canvas (`#0d1014`), paired with tabular numerals (`tnum`), crisp monochrome linear SVGs, and one restrained forest-teal semantic seal (`#0e7568`).

**Key Characteristics:**
- Adaptive dual-scheme tokens via CSS `light-dark()` backed by `<meta name="color-scheme" content="light dark">` and a 3-state footer toggle (`跟随` / `浅色` / `深色`).
- Hairline-integrated ledger strips (`.kpi-band`) and container-responsive zero-overflow tables (`@container tbl`).
- Honest, strict visual boundary between dry-run `vault/outbox/` operations (`primary` teal action) and confirmed live tool directory writes (`danger` action).

## Colors

Restrained neutral mineral slate surfaces paired with a single forest-teal accent and semantic warning/danger tokens calibrated for >=4.5:1 WCAG AA contrast in both light and dark modes.

### Primary
- **Forest Teal Fill** (`#0e7568`): Solid primary action buttons (`.btn.primary`), active nav accent indicator, selected target tile borders, active radio/checkbox `accent-color`.
- **Forest Teal Ink** (`light-dark(#0a5a50, #4fd1c5)`): High-contrast accent text, snapshot ID links (`.sid`), active navigation labels, `caret-color`, `:focus-visible` outlines, and positive/healthy telemetry readouts.
- **Teal Tint & Border** (`--acc-soft: light-dark(#e6f3f0, rgba(79,209,197,.14))`, `--acc-line: light-dark(#b7dcd4, rgba(79,209,197,.32))`): Active navigation item plates, selected target cards, success result containers, and selection highlights.

### Neutral
- **Canvas** (`light-dark(#f3f5f7, #0d1014)`): Root workspace viewport background (`--bg-canvas`).
- **Surface** (`light-dark(#ffffff, #14191f)`): Elevated ledger sheets, sidebar chrome, topbar, tables, modals, and drawer (`--bg-surface`).
- **Subtle & Inset** (`--bg-subtle: light-dark(#f8fafb, #1a2028)`, `--bg-inset: light-dark(#eaeef2, #222933)`): Table headers, segmented control wells, Antigravity maintenance row, skeleton shimmer base.
- **Hairline Rules** (`--line: light-dark(#dfe3e8, #27303c)`, `--line-soft: light-dark(#edf0f3, #1e2630)`, `--line-strong: light-dark(#c6cdd5, #3a4656)`): Structural 1px borders separating panes, table cells, and ledger segments.
- **Ink Ramp**: Primary `--ink: light-dark(#15191d, #edf1f5)`, Secondary `--ink-2: light-dark(#424a52, #b6c0ca)`, Muted metadata `--ink-3: light-dark(#5e6770, #8d98a5)`.

### Semantic States
- **Warning** (`--warn: light-dark(#9a5b05, #f5b942)` / `--warn-bg` / `--warn-line`): Decision memory tags, incomplete transcription flags, switch detections, and pending warnings.
- **Danger / Error** (`--err: light-dark(#b42323, #f87171)` / `--err-bg` / `--err-line`): Service disconnects, unresponsive upstream relay states, and destructive/confirmed live directory write buttons (`.btn.danger`).

**The Single Seal Rule.** Reserve forest-teal solid fills (`#0e7568`) exclusively for primary constructive CTAs (`筛选`, `生成到 outbox`, `导出整项`, `一键补齐导入`); keep navigation highlights soft-tinted (`--acc-soft`) so interactive emphasis never competes across cards.

## Typography

- **UI & Body Font:** `"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif` (zero external webfont requests; offline-safe).
- **Monospace / Telemetry Font:** `"Cascadia Code", Consolas, "SF Mono", ui-monospace, monospace` (for snapshot IDs, timestamps, file paths, KPI readouts, terminal commands).

### Hierarchy
- **KPI Hero Figure**: `23px` monospace bold (`650`), `-0.02em` tracking, `tabular-nums` (`tnum`).
- **Page / Modal Title (`h1`, `h3`)**: `15px`, weight `650`.
- **Body / Button / Control**: `13px–13.5px`, line-height `1.58`.
- **Compact Metadata / Table Headers**: `12px–12.5px`, weight `600` on headers and card titles.

**The Numeric Truth Rule.** All counts, latencies, token quantities, and table timestamps enable `font-variant-numeric: tabular-nums` and separate abbreviated readouts (`19.70B`) cleanly from exact integer readouts (`19,704,665,238`) with zero unstyled CJK uppercase breakage.

## Layout

- **Desktop App Shell (`>900px`)**: Two-track grid `var(--sidebar-w) minmax(0,1fr)` (`216px` sidebar + fluid workspace) locked to `100dvh` with inner scrolling in `.content` (`max-width: 1420px`, `20px 26px 56px` padding) across 4 focused tabs (`概览`, `快照资产`, `迁移`, `设置与运维`).
- **Overview (`#t-ov`)**: 4-cell integrated `.kpi-band` directly above `minmax(0,7fr) minmax(0,5fr)` `.ov-grid` (left: `最近入库会话`; right rail: `多端资产分布` `#ov-agents`, `最新记忆摘要` `#ov-mem`), with collapsible disclosure `#ov-quota-card` underneath showing live summary text.
- **Migration (`#t-mig`)**: Split workbench `minmax(0,440px) minmax(0,1fr)` pairing source search/selection + 2-column target format tiles on the left with real-time fidelity preview + outbox/apply execution on the right so Steps 1–4 fit simultaneously within `1440×900`.
- **Container-Adaptive Tables (`@container tbl`)**: Desktop (`tbl-ov`, `tbl-snap`) enforces `table-layout: fixed` with budgeted `<colgroup>` widths for zero horizontal scrollbars; under `<560px` container width, rows transform into labeled key-value ledger cards with multi-line wrapped summaries (`.sum-main`).
- **Mobile Shell (`<=900px`)**: `.sidebar` converts to a 2-row CSS Grid (row 1: brand left + 3-state theme switcher right; row 2: equal `repeat(4, minmax(0, 1fr))` nav tabs across full width), and `.topbar` wraps cleanly without page overflow down to `380px`.

## Elevation & Depth

Surfaces distinguish depth cleanly through tonal shifts (`--bg-canvas` -> `--bg-surface` -> `--bg-subtle`) and `1px` borders (`--line`, `--line-soft`) rather than heavy drop shadows:
- **Resting Card / Shell Shadow (`--shadow-1`)**: `0 1px 2px light-dark(rgba(16,20,24,.04), rgba(0,0,0,.28))`.
- **Top-Layer Modal / Drawer / Popover Toast Shadow (`--shadow-2`)**: `0 14px 36px light-dark(rgba(16,20,24,.15), rgba(0,0,0,.55))` with `rgba(12,16,20,.38–.40)` backdrop scrim on `<dialog>::backdrop`.

## Shapes

- **Compact Controls & Inputs (`--rad-s`)**: `6px` border radius on `.btn`, inputs, selects, target cards, and terminal command boxes.
- **Primary Cards & Result Panes (`--rad-m`)**: `9px` radius on `.card`, `.cardbox`, `.kpi-band`, `.proj-dock`, `.result-card`, and `#toast`.
- **Confirm Modals (`--rad-l`)**: `13px` radius on `#confirm-dlg`.
- **Status Pills**: Full pill `999px` only on concise status indicators (`.status-pill`, `.pill`).

## Components

- **Buttons (`.btn`)**: `34px` default / `27px` `.btn.sm`; hairline border (`--line-strong`), subtle press feedback (`translateY(1px)`), disabled `0.48` opacity; monochrome `1.5–1.6px` stroke inline SVGs matched to label weight.
- **Data Tables (`.cardbox` + `.tbl-wrap`)**: Sticky headers (`top: 0`), `44px` uniform row height on desktop (`sid-wrap` prevents version badge wrapping), full-width click affordability paired with accessible `<button class="sid">` / `.sumbtn` focus triggers.
- **Project Workbench Dock (`.proj-dock`)**: Three cleanly separated horizontal bands: (1) source & archive filter + expand/collapse, (2) generic 10-target export format selector + conditional `#tree-apply` (auto-disabled for non-writable targets), (3) distinct subtle-tinted `.proj-sync` bar strictly for Antigravity bidirectional cleanup/batch import.
- **Native Top-Layer Overlays**: Slide-over Detail `<dialog class="drawer" closedby="any">`, safety `<dialog id="confirm-dlg">` defaulting focus onto `取消`, and non-modal `<div id="toast" popover="manual">` always stacking cleanly above open dialogs.
- **Markdown & Telemetry Sanitizers**: Strips raw `**`/`__`/`` ` `` markdown syntax and messy local URLs before table/memory display; maps memory kinds and status indicators into clear Chinese pills.

## Do's and Don'ts

### Do:
- **Do** keep `CONSOLE_HTML` strictly self-contained with a single `<script>` tag (required by `tools/check_console_js.py`) and zero external CDN requests.
- **Do** keep desktop table `.tbl-wrap` widths fitted inside their container budgets (`scrollWidth <= clientWidth` at `1440px`) and verify both desktop inner-wrap and mobile stacked behavior after table column edits.
- **Do** maintain clear separation between dry-run `outbox` buttons and confirmed live-directory actions, auto-disabling `#tree-apply` for targets without live write support.
- **Do** use `1.5–1.6px` stroke inline monochrome `<svg>` icons (`aria-hidden="true"` when decorative beside visible text) across every button, accordion, and card icon.

### Don't:
- **Don't** add external frontend packages, build pipelines, `<script>` tags inside `<head>`, or Unicode emoji/glyph characters standing in for UI icons.
- **Don't** remove `:focus-visible` rings on inputs/selects (`outline: none` without replacement) or leak unstripped raw Markdown syntax / duplicated status strings (`ERR ERR`) into summaries or pills.
- **Don't** auto-open secondary disclosure sections (`#ov-usage-card`) or hide core mobile navigation/theme controls at `<=900px`.
