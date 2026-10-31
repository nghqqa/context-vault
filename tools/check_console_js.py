#!/usr/bin/env python3
"""抽取 CONSOLE_HTML 的 <script> 供 node --check 校验 (CI 用, 跨 shell 安全)。

用法: python tools/check_console_js.py   # 产出 console_check.js
"""
import re
from pathlib import Path

src = Path(__file__).resolve().parent / "cv.py"
html = re.search(r'CONSOLE_HTML = r?"""(.*?)"""', src.read_text(encoding="utf-8"), re.S).group(1)
js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
Path("console_check.js").write_text(js, encoding="utf-8")
print(f"console_check.js: {len(js)} chars")
