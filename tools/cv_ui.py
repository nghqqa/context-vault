"""UI 静态模板加载器 — 将 tools/ui/ 下的独立 CSS/HTML/JS 组装为自包含单页字符串。

保持零第三方依赖与单 <style>/单 <script> 自包含产物，同时支持：
1. 源码模式：直接从 tools/ui/ 读取，前端享有原生语法高亮、node --check 与刷新热加载；
2. PyInstaller 单文件打包模式：自动从 sys._MEIPASS/ui 读取并内存缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

_FROZEN_CACHE: dict[str, str] = {}
_CONSOLE_JS_PARTS = (
    "console_core.js",
    "console_overview.js",
    "console_memory.js",
    "console_snapshots.js",
    "console_reader.js",
    "console_migrate.js",
    "console_skills.js",
    "console_ops.js",
)
_MIG_TARGETS = (
    ("codex", "Codex", "rollout 文件 · codex resume", "Codex", "rollout 文件 · codex resume"),
    ("claude", "Claude Code", "projects jsonl · --resume", "Claude Code", "projects jsonl · --resume"),
    ("antigravity", "Antigravity", "原生 .db（重启 app 生效）", "Antigravity", "原生 .db（重启 app 生效）"),
    ("zcode", "ZCode 写回", "实验性 · 先备份后写主库", "ZCode 写回", "实验性 · 整批前备份 1 次"),
    ("dsh", "DeepSeek Harness", "V4 会话 · ~/.dsh/sessions", "DeepSeek Harness", "V4 会话 · ~/.dsh/sessions"),
    (
        "markdown",
        "Markdown 交接简报 · 任意 agent",
        "≤48KB 结构化交接简报（停点 / 最近动作 / 活上下文尾部）",
        "Markdown 交接简报",
        "批量输出结构化简报至 outbox",
    ),
)
_MIG_BUDGET_OPTS = (
    ("4000", "4,000 tokens（推荐 · 五槽位智能压缩）", "4,000 tokens（推荐 · 五槽位压缩）"),
    ("2000", "2,000 tokens（紧凑速览）", "2,000 tokens（紧凑速览）"),
    ("8000", "8,000 tokens（详尽交接）", "8,000 tokens（详尽交接）"),
    ("", "不限预算（完整简报 ≤48KB）", "不限预算（单条 ≤48KB）"),
)


def _render_mig_targets(batch: bool) -> str:
    name = "mb-tgt" if batch else "tgt"
    rows = []
    for val, s_title, s_desc, b_title, b_desc in _MIG_TARGETS:
        title, desc = (b_title, b_desc) if batch else (s_title, s_desc)
        on_cls = " on" if val == "codex" else ""
        chk = " checked" if val == "codex" else ""
        cb = f"mbSetTarget('{val}')" if batch else "onMigTarget()"
        rows.append(
            f'              <label class="tgt-card{on_cls}">'
            f'<input type="radio" name="{name}" value="{val}"{chk} onchange="{cb}">\n'
            f'                <span><span class="tt">{title}</span><br><span class="td">{desc}</span></span></label>'
        )
    return "\n".join(rows)


def _render_mig_budgets(batch: bool) -> str:
    rows = []
    for val, s_label, b_label in _MIG_BUDGET_OPTS:
        sel = " selected" if val == "4000" else ""
        lbl = b_label if batch else s_label
        rows.append(f'                <option value="{val}"{sel}>{lbl}</option>')
    return "\n".join(rows)


def ui_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "ui"
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent / "ui"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _read_console_js(d: Path) -> str:
    return "\n".join((d / name).read_text(encoding="utf-8") for name in _CONSOLE_JS_PARTS)


def load_console_js() -> str:
    if _is_frozen() and "js" in _FROZEN_CACHE:
        return _FROZEN_CACHE["js"]
    js = _read_console_js(ui_dir())
    if _is_frozen():
        _FROZEN_CACHE["js"] = js
    return js


def load_console_html() -> str:
    if _is_frozen() and "console" in _FROZEN_CACHE:
        return _FROZEN_CACHE["console"]
    d = ui_dir()
    shell = (d / "console.html").read_text(encoding="utf-8")
    css = (d / "console.css").read_text(encoding="utf-8")
    js = _read_console_js(d)
    out = (
        shell.replace("<!--__MIG_SINGLE_TARGETS__-->", _render_mig_targets(False))
        .replace("<!--__MIG_SINGLE_BUDGETS__-->", _render_mig_budgets(False))
        .replace("<!--__MIG_BATCH_TARGETS__-->", _render_mig_targets(True))
        .replace("<!--__MIG_BATCH_BUDGETS__-->", _render_mig_budgets(True))
        .replace("/*__CONSOLE_CSS__*/", css)
        .replace("/*__CONSOLE_JS__*/", js)
    )
    if _is_frozen():
        _FROZEN_CACHE["console"] = out
    return out


def load_dashboard_html() -> str:
    if _is_frozen() and "dashboard" in _FROZEN_CACHE:
        return _FROZEN_CACHE["dashboard"]
    out = (ui_dir() / "dashboard.html").read_text(encoding="utf-8")
    if _is_frozen():
        _FROZEN_CACHE["dashboard"] = out
    return out
