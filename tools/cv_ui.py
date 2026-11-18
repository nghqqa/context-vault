"""UI 静态模板加载器 — 将 tools/ui/ 下的独立 CSS/HTML/JS 组装为自包含单页字符串。

保持零第三方依赖与单 <style>/单 <script> 自包含产物，同时支持：
1. 源码模式：直接从 tools/ui/ 读取，前端享有原生语法高亮、node --check 与刷新热加载；
2. PyInstaller 单文件打包模式：自动从 sys._MEIPASS/ui 读取并内存缓存。
"""

from __future__ import annotations

import sys
from pathlib import Path

_FROZEN_CACHE: dict[str, str] = {}


def ui_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "ui"
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent / "ui"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def load_console_js() -> str:
    if _is_frozen() and "js" in _FROZEN_CACHE:
        return _FROZEN_CACHE["js"]
    js = (ui_dir() / "console.js").read_text(encoding="utf-8")
    if _is_frozen():
        _FROZEN_CACHE["js"] = js
    return js


def load_console_html() -> str:
    if _is_frozen() and "console" in _FROZEN_CACHE:
        return _FROZEN_CACHE["console"]
    d = ui_dir()
    shell = (d / "console.html").read_text(encoding="utf-8")
    css = (d / "console.css").read_text(encoding="utf-8")
    js = (d / "console.js").read_text(encoding="utf-8")
    out = shell.replace("/*__CONSOLE_CSS__*/", css).replace("/*__CONSOLE_JS__*/", js)
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
