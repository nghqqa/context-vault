#!/usr/bin/env python3
"""抽取 CONSOLE_HTML 的 <script> 供 node 校验 (CI 用, 跨 shell 安全)。

三步: python tools/check_console_js.py emit  -> 产出 console_check.js (语法检查用)
      python tools/check_console_js.py run   -> 产出 console_run.js (DOM mock + node 实际执行,
                                                能抓住 const 重复声明/作用域错误——
                                                node --check 只查语法, 抓不住这两类)
      python tools/check_console_js.py unit  -> 产出 console_unit.js (带数据的函数级回归:
                                                loadOv/snapSearch/boot token 清理走真实分支)
用法(CI): python tools/check_console_js.py emit && node --check console_check.js
          python tools/check_console_js.py run  && node console_run.js
          python tools/check_console_js.py unit && node console_unit.js
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
elif out == "unit":
    # DOM mock 按选择符缓存元素 (断言要读回 innerHTML/value); fetch 按端点分发合成数据。
    # 定时器保持真实 (boot 链路要能 await), 结尾显式 process.exit 防轮询挂住。
    mock = (
        'const _els={};'
        'const _el=k=>_els[k]||(_els[k]={innerHTML:"",textContent:"",value:"",hidden:false,style:{},'
        'dataset:{},options:[],offsetHeight:0,classList:{add(){},toggle(){},remove(){},contains:()=>false},'
        'setAttribute(){},removeAttribute(){},addEventListener(){},focus(){}});'
        'const document={querySelector:s=>_el(s),querySelectorAll:()=>[],getElementById:s=>_el("#"+s),'
        'addEventListener(){},body:_el("body")};'
        'let _replace=null;'
        'const history={replaceState:(a,b,c)=>{_replace=c;}};'
        'const location={search:"?token=unit-token",hash:"",pathname:"/"};'
        'const navigator={clipboard:{writeText:async()=>{}}};'
        'const window={addEventListener(){},history,navigator};'
        'const DATA={ledger:{owned:{snapshots:2,sessions:2,tokens_in:100,tokens_out:10,cache_read:60,'
        'cache_write:10,total_tokens:120,cache_hit_rate:0.353,'
        'by_agent:[{agent:"codex",snapshots:2,sessions:2,tokens_in:100}]},lifetime:{},by_model:[],'
        'monthly:[],memories:0,zcode_usage:{"glm-5":{n:3,tin:5000,tout:900,cost:null}},zcode_snapshots:0},'
        'recent:{total:1,rows:[{sid:"cv_abc123def45678",agent:"codex",first:"hello world",'
        'created_at:"2026-11-01T00:00:00",cwd:"D:\\\\proj",completeness:"full",blocks:3}]},search:{hits:[]}};'
        'const fetch=async(u)=>({ok:true,json:async()=>{'
        'if(u.indexOf("/ledger")===0)return DATA.ledger;'
        'if(u.indexOf("/recent")===0)return DATA.recent;'
        'if(u.indexOf("/probe_events")===0)return [];'
        'if(u.indexOf("/memory")===0)return [];'
        'if(u.indexOf("/audit")===0)return {summary:{ok_probes:1,abnormal:0,switches:0,'
        'model_missing_events:0},total_events:2};'
        'if(u.indexOf("/search")===0)return DATA.search;'
        'if(u.indexOf("/healthz")===0)return {snapshots:1,terms:1};'
        'return {};}});'
        'process.on("unhandledRejection",e=>{console.error("unhandledRejection:",e);process.exit(9)});')
    tests = (
        # B03: 首访 ?token= 种成 cookie 后必须从地址栏清掉
        '(async()=>{'
        'const tick=()=>new Promise(r=>setTimeout(r,10));'
        'await boot();await tick();await tick();'
        'if(_replace===null){console.error("B03: 地址栏 token 未清理");process.exit(1);}'
        # B04: zcode 用量存在但无 zcode 快照时, loadOv 曾对 const cand 重新赋值崩溃
        'const band=_el("#ov-band").innerHTML;'
        'if(band.indexOf("加载失败")>=0){console.error("B04: loadOv 崩溃:",band.slice(0,160));process.exit(2);}'
        'if(band.indexOf("zcode")<0){console.error("B04: zcode 用量行未进概览带");process.exit(3);}'
        # B06: 搜索无结果时 || 恒真导致空态永不可达
        '_el("#snap-q").value="数据库备份";'
        'await snapSearch();'
        'if(_el("#snap-list").innerHTML.indexOf("没有命中")<0){console.error("B06: 搜索空态缺失");process.exit(4);}'
        'DATA.search.hits=[{sid:"cv_abc123def45678",score:2,agent:"codex",first:"hello world",'
        'created_at:"2026-11-01T00:00:00",cwd:"D:\\\\proj",completeness:"full",blocks:3}];'
        'await snapSearch();'
        'if(_el("#snap-list").innerHTML.indexOf("cardbox")<0){console.error("B06: 命中表格未渲染");process.exit(5);}'
        'console.log("console unit OK: B03 token-strip / B04 loadOv / B06 empty+hit states");'
        'process.exit(0);'
        '})().catch(e=>{console.error("unit failed:",(e&&e.message)||e);process.exit(6)});')
    Path("console_unit.js").write_text(mock + js + tests, encoding="utf-8")
    print(f"console_unit.js: {len(js)} chars (unit mode)")
else:
    Path("console_check.js").write_text(js, encoding="utf-8")
    print(f"console_check.js: {len(js)} chars")
