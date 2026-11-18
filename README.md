[**简体中文**](README.zh.md) | **English**

# Context Vault

[Website](https://nghqqa.github.io/context-vault/) · [![CI](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml/badge.svg)](https://github.com/nghqqa/context-vault/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nghqqa/context-vault)](https://github.com/nghqqa/context-vault/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> **A local-first asset vault for AI coding sessions** — snapshots you own, migration that's honest, a ledger you can audit, memory that cites its sources.

Your AI coding sessions are your most valuable work asset — yet they're locked inside each agent's private format. Switch models, switch relays, switch tools, and the context resets to zero. Context Vault turns it into a **neutral asset on your own machine**. Agents come and go; the vault stays.

- 79.7% of developers use two or more AI coding tools every week (Stack Overflow 2026) — context scattered across vendors.
- Every migration tool on the market is a one-shot **porter**. None of them is an **asset vault** (persistent store, content addressing, lineage, ledger, audit trail).
- Lock-in is not going to fix itself: vendors benefit from it.

## ✨ Features

- **Snapshot vault** — export Codex / Claude Code / ZCode / Antigravity / 18 bridged harnesses into the vendor-neutral [context-snapshot](docs/snapshot-format.md) format: content-addressed, immutable, idempotent, with automatic lineage (`parent_snapshot` trees & forks) and daily retention pruning (`cv prune`).
- **Handoff brief** — `--target markdown --budget-tokens 4000` assembles a utility-maximized brief under an explicit token budget (stop state / active decisions / disproven dead-ends / touched-file topology / live-context tail), folding repeated file reads and duplicate errors, with compression stats.
- **Vault backup** — `cv backup`: consistent sqlite snapshot + rotated zips; optional `CV_SERVE_TOKEN` auth for the resident console.
- **Honest migration & bidirectional sync** — bidirectional Codex ⇄ Claude Code, native Antigravity `.db`/Proto round-trip & project-tree batch sync/cleanup, experimental ZCode write-back (with auto-backup), dry-run by default, and a per-migration report of exactly what survives and what doesn't.
- **Deep retrieval core** — code-aware tokenization (camelCase/snake_case/path/error-code composite+subword dual indexing), block-role-weighted BM25 (global IDF + length normalization), lineage folding (chain hits collapse to the latest version), time decay and optional dense-vector RRF; a compact SQLite inverted index replaces the giant pickle.
- **Open reproducible benchmark** — `python benchmarks/run_bench.py` reproduces a 144-query, 5-dimension ablation (full v0→v2 evolution) in ~30s; see [docs/BENCHMARK.md](docs/BENCHMARK.md).
- **Token ledger** — per-model costs, cache hit rates aggregated from usage records *inside your own session files*. No proxy required.
- **Cross-session temporal memory** — decisions, preferences, summaries, pitfalls and hard constraints auto-extracted from history with exact provenance (snapshot#block + real session time) and topic signatures; when a decision is superseded, stale memories are marked and hidden by default (`include_history` traces the full evolution chain).
- **MCP backend** — 6 tools (`vault_remember` etc.) turn any MCP client (Claude Code / Codex / Cursor) into a remembering agent. One command to install everywhere.
- **Upstream sentinel** — probes relay `/v1/models`, alerts and leaves an audit trail when a model silently disappears.

## 🚀 Quick Start

```bash
python tools/cv.py doctor          # environment check (zero dependencies, Python 3.9+)
python tools/cv.py sync            # auto-ingest all local session sources (incremental + lineage)
python tools/cv.py serve           # web console at http://127.0.0.1:8791
python tools/cv.py mcp install --all   # register MCP backend in claude / codex / cursor
```

Migrate a session (dry-run by default — nothing touches real session dirs without `--apply`):

```bash
python tools/cv.py migrate ~/.claude/transcripts/ses_xxx.jsonl --target codex
python tools/cv.py import cv_xxxx --target codex --apply
```

## 📦 What survives a migration

Honesty first — this is the whole table, not a highlight reel:

| Data | Status | Notes |
|---|---|---|
| Conversation & paired tool calls/results | ✅ carried | unpaired results are dropped and **counted** in the report |
| Reasoning / thinking summaries | ⚠️ partial | signatures & encrypted content are not portable — dropped by [spec §3.2](docs/snapshot-format.md) |
| Compaction boundaries | ✅ carried | become summary blocks; stateless-replay semantics |
| Developer-injected blocks (env etc.) | ❌ filtered | re-injected by the target agent's runtime |
| Token usage records | ✅ carried | lands in the ledger, not the migrated session |
| Sub-agent transcripts · images | ❌ not yet | |
| env / hooks / MCP config / permissions | ❌ stays with source | cannot and should not be moved |

## 🧠 Memory backend — API at a glance

| MCP tool | Returns |
|---|---|
| `vault_remember(query)` | long-term memory: past decisions, project conclusions, user preferences (each cites its source snapshot) |
| `vault_context(query)` | relevant excerpts from history for the task at hand |
| `vault_search(query)` | full-text search across every snapshot |
| `vault_recent(limit)` | recent snapshots |
| `vault_snapshot(id)` | single snapshot detail (block distribution, live context, tokens) |
| `vault_stats()` | token ledger summary |

Ask your agent at the start of a task: *"what did we decide about X before?"* — it queries your own history, locally.

## 🗄️ How it works

```
vault/
├── snapshots/cv_*.json   # immutable, content-addressed (sha256 → id); lineage inside
├── vault.db              # index: search terms, ledger, memories, audit, sync watermarks
├── outbox/               # dry-run migration output (never touches real dirs)
└── backups/              # cv backup: consistent zips, rotated
```

Two rules that don't bend: **snapshots are immutable** (any derived data lives in the index layer, never rewritten into snapshots), and **source session files are read-only** (the vault never modifies what an agent wrote). `cv serve` binds to loopback only, validates Host/Origin, and requires JSON content-type on writes — local processes work, drive-by web pages don't.

## Verified on a real machine (2026-10-27/29)

- 195 MB Codex rollout → 12 MB snapshot in **1.5 s**; 39 compaction boundaries, 224M tokens of usage, 838 tool-call pairs intact.
- **Bidirectional migration, both directions real-machine verified**: Codex→Claude resumes and continues accurately in Claude 2.1.181; Claude→Codex restores **373,160 tokens of full context** in Codex 0.153.4.
- Reverse-engineered and dodged the two-generation Codex format trap (numeric vs string `ordinal`, resume reading the SQLite thread-store, misleading "rollout is empty").
- Test suite: **124+ tests** including a hot/cold equivalence test — the resident index and the file-scan fallback must return identical results ("the daemon accelerates; it is never a dependency"). CI also runs JS syntax/runtime/unit checks and a real-browser layout probe (380–2560px × four sections, page-overflow detection).

## Design principles

1. **Read-only sources, never rewrite traffic** — the asset layer is strictly separate from the transport layer (proxies/gateways). Remove the proxy, reinstall it: the vault loses nothing.
2. **Honest over complete** — snapshots carry `completeness` flags; migration diffs list what cannot move; ledger estimates are labeled as estimates.
3. **Stateless replay** — server-side state is not portable; the live context is what follows the last compaction boundary, with summaries injected as the new session's first message.

## Dependencies & data sources

**Core features have zero external dependencies** (pure Python stdlib; the CLI is a single file). The token ledger works from usage records inside your own session files. Optional integrations (headroom proxy, relay billing APIs) are enhancements with environment-variable fallbacks (`CV_RELAY_PROFILES`, `CV_RELAY_KEY`) — see [README.zh.md](README.zh.md) for the full boundary table. Uninstall every integration: the vault loses one byte of nothing.

## 🆚 How this relates to neighbors

| | Context Vault | porters (session-migrate · txcript · `/import`) | memory layers (Mem0 · Letta) | proxies (headroom · LiteLLM) |
|---|---|---|---|---|
| Layer | **asset vault** | converter | memory | transport |
| Immutable content-addressed store + lineage | ✅ | — | — | — |
| Bidirectional migration | ✅ native 2 + bridged 18 | ✅ | — | — |
| Token ledger from session files | ✅ | — | — | own traffic only |
| Memory with provenance | ✅ (temporal engine v2, with conflict superseding; pluggable) | — | ✅ | — |
| Open format spec | ✅ [CC-BY](docs/snapshot-format.md) (txcript mapping included) | txcript `Simple` | — | — |

Porters are upstream adapters, not competitors — `cv ingest` and `cv export-harness` literally shell out to session-migrate for 18 formats. The vault owns what they don't: storage, lineage, ledger, audit.

## Harness matrix

| Channel | Coverage |
|---|---|
| Native read+write (real-machine verified) | Codex · Claude Code · Antigravity `.db`/Proto · ZCode transcripts (read + experimental write-back) |
| Bridged read (`cv ingest`, 18 formats) | opencode · cursor · qwen · kimi · grok · antigravity · copilot · devin · hermes · kilo · mastracode · muse · omp · pi · vibe · openhands … |
| Bridged write (`cv export-harness`, 18 targets) | same list |
| Ledger sources | headroom · relay billing API · ZCode usage audit |
| Not supported | harnesses with encrypted local stores (e.g. Windsurf) |

## Roadmap

- **M0 ✅** format spec v0.1 · dual-source export · ledger import · probe
- **M1 ✅** SQLite vault · bidirectional migration (verified both ways) · diff preview
- **M2 ✅** resident service + web console · MCP backend · cache warming · sentry/watch · 18-format bridged intake
- **M3 ✅** memory layer (pluggable engine) · fork/tree lineage · audit timeline
- **M4 ✅** deep retrieval core (code-aware BM25 + lineage folding + SQLite inverted index) · temporal memory (superseded conflict resolution) · budgeted handoff compression · CodingVault-Bench open ablation
- **Next** launch & feedback · optional dense vectors by default · optional encryption

## FAQ

**How is this different from just reading `~/.claude/projects` myself?** You can — once, for one format, on one machine. The vault gives you one queryable store across every harness and month, with lineage when sessions evolve, an audit trail when upstreams change, and memory extraction over all of it.

**Does anything leave my machine?** No. No telemetry, no accounts, no API keys needed for core features. `serve` binds to 127.0.0.1; `probe` only calls the relay endpoints you configure.

**Is it free?** The core is MIT, forever. A closed-source Windows tray companion (auto-sync, daily backup, resident console) exists; the open core works fully without it.

## Contributing

Issues and PRs welcome. Format-spec discussions are the highest-value contributions — [snapshot-format.md](docs/snapshot-format.md) is the product.

## License

Code **MIT** · format spec **CC-BY** ([snapshot-format.md](docs/snapshot-format.md)). Open format for the ecosystem seat; companion conveniences fund the work.
