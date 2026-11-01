#!/usr/bin/env python3
"""抽取 CONSOLE_HTML 的 <script> 供 node 校验 (CI 用, 跨 shell 安全)。

两步: python tools/check_console_js.py emit  -> 产出 console_check.js (语法检查用)
      python tools/check_console_js.py run   -> 产出 console_run.js (DOM mock + node 实际执行,
                                                能抓住 const 重复声明/作用域错误——
                                                node --check 只查语法, 抓不住这两类)
用法(CI): python tools/check_console_js.py emit && node --check console_check.js
          python tools/check_console_js.py run  && node console_run.js
"""
import re
import sys
from pathlib import Path

src = Path(__file__).resolve().parent / "cv.py"
html = re.search(r'CONSOLE_HTML = r?"""(.*?)"""', src.read_text(encoding="utf-8"), re.S).group(1)
js = re.search(r"<script>(.*)</script>", html, re.S).group(1)

out = sys.argv[1] if len(sys.argv) > 1 else "emit"
if out == "run":
    mock = ('const _el=()=>({innerHTML:"",textContent:"",classList:{add(){},toggle(){},remove(){}},style:{},'
            'dataset:{},setAttribute(){},removeAttribute(){},addEventListener(){},options:[],value:""});'
            'const document={querySelector:_el,querySelectorAll:()=>[],getElementById:_el,addEventListener(){},body:_el()};'
            'const location={search:"",hash:""};const window={addEventListener(){},location:{}};'
            'const navigator={clipboard:{writeText:async()=>{}}};'
            'const setTimeout=()=>{};const setInterval=()=>{};const clearTimeout=()=>{};'  # 定时器全替掉, 否则真实 30s 轮询挂住 node 不退出
            'const fetch=async()=>({ok:true,json:async()=>({})});'
            'process.on("unhandledRejection",e=>{console.error("unhandledRejection:",e);process.exit(1)});')
    Path("console_run.js").write_text(mock + js, encoding="utf-8")
    print(f"console_run.js: {len(js)} chars (execute mode)")
else:
    Path("console_check.js").write_text(js, encoding="utf-8")
    print(f"console_check.js: {len(js)} chars")
