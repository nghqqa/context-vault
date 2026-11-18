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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv_ui  # noqa: E402

html = cv_ui.load_console_html()
assert html.count("<script>") == 1 and html.count("</script>") == 1, "CONSOLE_HTML 必须严格保持单 <script> 自包含约束"
assert html.count("<style>") == 1 and html.count("</style>") == 1, "CONSOLE_HTML 必须严格保持单 <style> 自包含约束"
js = re.search(r"<script>(.*)</script>", html, re.S).group(1)

out = sys.argv[1] if len(sys.argv) > 1 else "emit"
if out == "run":
    mock = ('const _el=()=>({innerHTML:"",textContent:"",classList:{add(){},toggle(){},remove(){}},style:{},'
            'dataset:{},setAttribute(){},removeAttribute(){},addEventListener(){},options:[],value:"",'
            'open:false,matches:()=>false,showModal(){},close(){},showPopover(){},hidePopover(){}});'
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
        'setAttribute(){},removeAttribute(){},addEventListener(){},focus(){},'
        'open:false,matches:()=>false,showModal(){},close(){},showPopover(){},hidePopover(){}});'
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
        'created_at:"2026-11-01T00:00:00",cwd:"D:\\\\proj",completeness:"full",blocks:3}]},search:{hits:[]},'
        'projects:{total_projects:1,total_conversations:1,projects:[{name:"proj",cwd:"D:\\\\proj",'
        'is_ungrouped:false,latest:"2026-11-01T00:00:00",count:1,conversations:[{id:"cv_abc123def45678",'
        'agent:"codex",title:"hello world",blocks_total:3,created_at:"2026-11-01T00:00:00"}]}]}};'
        'const fetch=async(u)=>({ok:true,json:async()=>{'
        'if(u.indexOf("/ledger")===0)return DATA.ledger;'
        'if(u.indexOf("/recent")===0)return DATA.recent;'
        'if(u.indexOf("/projects")===0)return DATA.projects;'
        'if(u.indexOf("/probe_events")===0)return [];'
        'if(u.indexOf("/memory/extract")===0)return {scanned:2,added:3,total:3,by_kind:{decision:2,preference:1}};'
        'if(u.indexOf("/memory/dedup")===0)return {removed:12,total:3,by_kind:{decision:1,preference:2}};'
        'if(u.indexOf("/memory")===0)return {memories:[{kind:"decision",content:"结论：采用单文件 SQLite WAL",project:"proj",snapshot:"cv_abc123def45678",hits:1}],counts:{total:1,unique:1,by_kind:{decision:1}}};'
        'if(u.indexOf("/messages")===0)return {id:"cv_abc123def45678",scope:"live",total:2,total_live:2,total_all:2,has_more:false,items:[{idx:0,type:"text",role:"user",text:"请检查 `cv.py`\\n```python\\nprint(1)\\n```",truncated:false,full_chars:30},{idx:1,type:"tool_result",role:"tool",name:"bash",is_error:false,text:"OK",truncated:false,full_chars:2}]};'
        'if(u.indexOf("/fork")===0)return {fork_sid:"cv_ff00112233445566",source_sid:"cv_abc123def45678",existed:false};'
        'if(u.indexOf("/snapshot")===0)return {id:"cv_abc123def45678",capture:{agent:"codex",cwd:"D:\\\\proj",completeness:"full"},lineage:{fork_of:"cv_0000111122223333"},stats:{blocks_total:3},block_dist:{text:3}};'
        'if(u.indexOf("/preview")===0)return {id:"cv_abc123def45678",live_blocks:3,live_tokens_est:40,history_tokens_in:100};'
        'if(u.indexOf("/timeline")===0)return {days:[{day:"2026-11-01",versions:[{sid:"cv_abc123def45678",time:"00:00:00",blocks:3,tokens_in:100,leaf:true}]}]};'
        'if(u.indexOf("/audit")===0)return {summary:{ok_probes:1,abnormal:0,switches:0,'
        'model_missing_events:0},total_events:2};'
        'if(u.indexOf("/search")===0)return DATA.search;'
        'if(u.indexOf("/healthz")===0)return {snapshots:1,terms:1};'
        'if(u.indexOf("/sync")===0)return {new:1,updated:0,total:2};'
        'if(u.indexOf("/antigravity/sync")===0)return {targets_count:1,targets:[{cid:"11111111",cwd:"D:\\\\proj",title:"arch",reason:"archived"}],empty_projects:[{id:"p1",name:"p1",cwd:"d:/p1"}],db_files:1,summary_ids:1,proto_ids:1};'
        'if(u.indexOf("/antigravity/batch")===0)return {planned:1,skipped_done:1,skipped_dup:0,projects:[{cwd:"d:/proj",count:1}]};'
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
        'await loadProjectTree();'
        'const sl=_el("#snap-list").innerHTML;'
        'if(sl.indexOf("proj-card")<0||sl.indexOf("proj-sync")<0){console.error("T4: 项目树或同步条未渲染");process.exit(7);}'
        'await agySyncPreview();'
        'if(_el("#tree-export-res").innerHTML.indexOf("待清理 Antigravity")<0){console.error("A1: 清理预览未渲染");process.exit(8);}'
        'await agyBatchPreview();'
        'if(_el("#tree-export-res").innerHTML.indexOf("待补齐导入 Antigravity")<0){console.error("A2: 批导预览未渲染");process.exit(10);}'
        'await loadMemoryView();'
        'if(_el("#snap-list").innerHTML.indexOf("mem-dock")<0||_el("#snap-list").innerHTML.indexOf("采用单文件")<0){console.error("M1: 记忆库视图未渲染");process.exit(11);}'
        'if(_el("#snap-meta").innerHTML.indexOf("合并后")<0){console.error("M1: 去重后计数未展示");process.exit(14);}'
        'await extractMemoriesNow(false);'
        'await dedupMemoriesNow();'
        'if(_el("#toast").textContent.indexOf("已合并")<0){console.error("M3: 去重结果未提示");process.exit(15);}'
        'dwCurSid="cv_abc123def45678";dwMsgQuery="print";await loadDrawerMessages(true);'
        'const mp=_el("#dw-msg-pane").innerHTML;'
        'if(mp.indexOf("code-blk")<0||mp.indexOf("<mark")<0||mp.indexOf("RESULT · bash")<0){console.error("M2: 对话正文代码块/高亮/工具名未渲染");process.exit(12);}'
        'dwMsgQuery="";await snapDetail("cv_abc123def45678","2026-11-01T00:00:00");'
        'if(_el("#dw-body").innerHTML.indexOf("分叉来源")<0){console.error("F1: 抽屉分叉溯源未渲染");process.exit(13);}'
        'await syncVaultNow();'
        'console.log("console unit OK: B03 token-strip / B04 loadOv / B06 empty+hit / T4 project-tree / M1 memory / M3 dedup / M2 msg-reader / F1 fork / sync+agy");'
        'process.exit(0);'
        '})().catch(e=>{console.error("unit failed:",(e&&e.message)||e);process.exit(6)});')
    Path("console_unit.js").write_text(mock + js + tests, encoding="utf-8")
    print(f"console_unit.js: {len(js)} chars (unit mode)")
else:
    Path("console_check.js").write_text(js, encoding="utf-8")
    print(f"console_check.js: {len(js)} chars")
