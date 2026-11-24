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
        'DATA.usage={now:1,generated_at:"2026-11-21T12:00:00+08:00",window:"5h",events_total:3,'
        'accounts_configured:true,sources:{zcode:2,claude:1,antigravity:5},'
        'window_sources:{zcode:2,claude:1},'
        'totals:{events:2,tokens_in:1000,tokens_out:200,reasoning_tokens:0,cache_read:800,'
        'cache_write:50,total_input:1850,total_output:200,cost_est_usd:1.234,cost_known_events:2},'
        'cache_hit_rate:0.432,cache_save_est_usd:0.12,'
        'precision:{exact_events:2,estimated_events:1,estimated_share:0.333},'
        'by_model:[{name:"glm-5",events:2,tokens_in:900,tokens_out:200,reasoning_tokens:0,'
        'cache_read:800,cache_write:50,total_input:1750,total_output:200,cost_est_usd:1.2,'
        'cost_known_events:2,estimated_events:1}],'
        'by_project:[{name:"proj-alpha",events:2,tokens_in:900,tokens_out:200,reasoning_tokens:0,'
        'cache_read:800,cache_write:50,total_input:1750,total_output:200,cost_est_usd:1.2,'
        'cost_known_events:2,estimated_events:0}],'
        'by_account:[{name:"main",events:2,tokens_in:900,tokens_out:200,reasoning_tokens:0,'
        'cache_read:800,cache_write:50,total_input:1750,total_output:200,cost_est_usd:1.2,'
        'cost_known_events:2,estimated_events:0}],'
        'by_client:[{name:"zcode",events:2,tokens_in:900,tokens_out:200,reasoning_tokens:0,'
        'cache_read:800,cache_write:50,total_input:1750,total_output:200,cost_est_usd:1.2,'
        'cost_known_events:2,estimated_events:1}],'
        'by_day:[{day:"2026-11-19",events:1,total_input:600,total_output:100,cost_est_usd:0.4},'
        '{day:"2026-11-20",events:1,total_input:900,total_output:150,cost_est_usd:0.6},'
        '{day:"2026-11-21",events:2,total_input:1750,total_output:200,cost_est_usd:1.2}],'
        'fiveh:{totals:{events:2,tokens_in:1000,tokens_out:200,reasoning_tokens:0,cache_read:800,'
        'cache_write:50,total_input:1850,total_output:200,cost_est_usd:1.234,cost_known_events:2},'
        'accounts:[{account:"main",client:"zcode",events:2,used:1850,weight:"total_tokens",'
        'used_weighted:1850,limit:220000,pct:0.0084,mode:"limit",peak_5h:null,pct_of_peak:null,share:0.9,'
        'eta:{"50%":"2026-11-21T14:30:00+08:00","80%":"2026-11-21T15:30:00+08:00",'
        '"100%":"2026-11-21T16:30:00+08:00"},configured:true},'
        '{account:"client:antigravity",client:"antigravity",events:1,used:400,weight:"total_tokens",'
        'used_weighted:400,limit:null,pct:null,mode:"consumption",peak_5h:640,pct_of_peak:0.625,share:0.1,'
        'eta:{"50%":"2026-11-21T15:00:00+08:00","80%":"2026-11-21T15:30:00+08:00",'
        '"100%":"2026-11-21T16:30:00+08:00"},configured:false}]},'
        'runway:[{account:"main",kind:"7d",cap:1000000,metric:"tokens",used_tokens:1850,'
        'used_usd:1.234,remaining:998150,days_left:5.2,rate_tokens_per_day:190000,'
        'rate_usd_per_day:1.2}],'
        'remote_quota:{ok:true,plan:"PRO",limits:[{type:"TOKENS_LIMIT",pct_used:42.5,'
        'reset_at:"2026-11-21T16:00:00+08:00"}]},'
        'remote_quota_agv:{ok:true,plan:"Antigravity Free",limits:['
        '{type:"gemini 5h",pct_used:12.0,reset_at:"2026-11-21T18:00:00+08:00"},'
        '{type:"claude/gpt weekly",pct_used:5.0,reset_at:"2026-11-23T01:58:00+08:00"}]}};'
        'const fetch=async(u,opts)=>({ok:true,json:async()=>{'
        'if(u.indexOf("/ledger/usage")===0)return DATA.usage;'  # 必须在 /ledger 前缀之前判断
        'if(u.indexOf("/ledger")===0)return DATA.ledger;'
        'if(u.indexOf("/recent")===0)return DATA.recent;'
        'if(u.indexOf("/projects")===0)return DATA.projects;'
        'if(u.indexOf("/memory/extract")===0)return {scanned:2,added:3,total:3,by_kind:{decision:2,preference:1}};'
        'if(u.indexOf("/memory/dedup")===0)return {removed:12,total:3,by_kind:{decision:1,preference:2}};'
        'if(u.indexOf("/memory/add")===0)return {id:99,kind:"decision",project:"proj",content:"手动红线",pinned:true,existed:false};'
        'if(u.indexOf("/memory/pin")===0)return {id:1,pinned:true,updated:1};'
        'if(u.indexOf("/memory/delete")===0)return {id:1,deleted:1};'
        'if(u.indexOf("/memory")===0)return {memories:[{id:1,kind:"decision",content:"结论：采用单文件 SQLite WAL",project:"proj",snapshot:"cv_abc123def45678",hits:1,pinned:false}],counts:{total:1,unique:1,by_kind:{decision:1}}};'
        'if(u.indexOf("/messages")===0)return {id:"cv_abc123def45678",scope:"live",total:2,total_live:2,total_all:2,has_more:false,items:[{idx:0,type:"text",role:"user",text:"请检查 `cv.py`\\n```python\\nprint(1)\\n```",truncated:false,full_chars:30},{idx:1,type:"tool_result",role:"tool",name:"bash",is_error:false,text:"OK",truncated:false,full_chars:2}]};'
        'if(u.indexOf("/fork")===0)return {fork_sid:"cv_ff00112233445566",source_sid:"cv_abc123def45678",existed:false};'
        'if(u.indexOf("/snapshot")===0)return {id:"cv_abc123def45678",capture:{agent:"codex",cwd:"D:\\\\proj",completeness:"full"},lineage:{fork_of:"cv_0000111122223333"},stats:{blocks_total:3},block_dist:{text:3}};'
        'if(u.indexOf("/preview")===0)return {id:"cv_abc123def45678",live_blocks:3,live_tokens_est:40,history_tokens_in:100};'
        'if(u.indexOf("/timeline")===0)return {days:[{day:"2026-11-01",versions:[{sid:"cv_abc123def45678",time:"00:00:00",blocks:3,tokens_in:100,leaf:true}]}]};'
        'if(u.indexOf("/search")===0)return DATA.search;'
        'if(u.indexOf("/healthz")===0)return {snapshots:1,terms:1};'
        'if(u.indexOf("/sync")===0)return {new:1,updated:0,total:2};'
        'if(u.indexOf("/ops/mcp/install")===0)return {results:[{client:"codex",message:"已安装 (跳过)"}],status:{server_cmd:"python mcp_server.py",clients:[{id:"codex",name:"Codex",detected:true,installed:true,config:"~/.codex/config.toml"}]}};'
        'if(u.indexOf("/ops/mcp")===0)return {server_cmd:"python mcp_server.py",clients:[{id:"codex",name:"Codex",detected:true,installed:true,config:"~/.codex/config.toml"}],installed_count:1,detected_count:1};'
        'if(u.indexOf("/ops/status")===0)return {version:"0.10.3",vault:"D:\\\\vault",frozen:false,snapshots:2,terms:10,db_mb:1.2,index_mb:0.5,backups_count:1,latest_backup:{name:"cv-backup-20261124.zip",size_mb:1.1},plan_token_set:true,plan_token_hint:"abcd…wxyz",accounts_configured:true,semantic:{embeddings:{configured:false},llm:{configured:false}}};'
        'if(u.indexOf("/ops/backup")===0)return {ok:true,name:"cv-backup-20261124.zip",size_mb:1.1,message:"备份完成"};'
        'if(u.indexOf("/ops/prune")===0)return {applied:false,drop_count:3,reclaim_mb:1.5,message:"保留 10 / 13 版 · 回收 3 版 / 1.5 MB"};'
        'if(u.indexOf("/ops/ledger_sync")===0)return {ok:true,message:"账本同步完成: 新入账 0 条"};'
        'if(u.indexOf("/ops/plan_token")===0)return {ok:true,plan_token_set:true,plan_token_hint:"abcd…wxyz"};'
        'if(u.indexOf("/skills/list")===0)return {source:"zcode",target:"antigravity",agy_mode:"skills",plugin_name:"",dest_root:"~/.gemini/config/skills",sources:[{agent:"zcode",standalone:2,plugins:0,total:2,exists:true},{agent:"antigravity",standalone:0,plugins:0,total:0,exists:false}],total:1,skills:[{id:"zcode:standalone:archify",source:"zcode",scope:"standalone",group:"standalone",slug:"archify",raw_name:"Archify",description:"Diagram generator",files:3,size_kb:12.0,prompt_tokens:28,target_status:"new",rewrites:["规范化 name"],warnings:[]}]};'
        'if(u.indexOf("/skills/migrate")===0){const _ap=!!(opts&&opts.body&&JSON.parse(opts.body).apply);return {applied:_ap,source:"zcode",target:"antigravity",agy_mode:"plugin",plugin_name:"migrated-from-zcode",dest_root:"~/.gemini/config/plugins/migrated-from-zcode/skills",plugin_manifest:"~/.gemini/config/plugins/migrated-from-zcode/plugin.json",counts:{selected:1,planned_copy:_ap?0:1,migrated:_ap?1:0,skipped_identical:0,skipped_conflict:0,rewrites:1,warnings:0},prompt_tokens_est:28,budget_warning:"",items:[{slug:"archify",target_slug:"archify",action:"copy",rewrites:["规范化 name"],warnings:[]}]};}'
        'if(u.indexOf("/semantic/")===0)return {ok:true};'
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
        'await toggleMemCompose();'
        'if(_el("#snap-list").innerHTML.indexOf("mem-compose")<0){console.error("M4: 手动记忆表单未渲染");process.exit(30);}'
        '_el("#mc-txt").value="手动红线测试";await submitManualMem();'
        'if(_el("#toast").textContent.indexOf("长期记忆库")<0){console.error("M4b: 手动记忆保存未提示");process.exit(31);}'
        'await toggleMemPin(1,true);await deleteMemItem(1);'
        'openCmdk();_el("#cmdk-q").value="SQLite";await runCmdkSearch();'
        'if(_el("#cmdk-body").innerHTML.indexOf("跨会话记忆")<0){console.error("K1: 命令面板未渲染记忆分栏");process.exit(32);}'
        'closeCmdk();'
        'dwCurSid="cv_abc123def45678";dwMsgQuery="print";await loadDrawerMessages(true);'
        'const mp=_el("#dw-msg-pane").innerHTML;'
        'if(mp.indexOf("code-blk")<0||mp.indexOf("<mark")<0||mp.indexOf("RESULT · bash")<0){console.error("M2: 对话正文代码块/高亮/工具名未渲染");process.exit(12);}'
        'dwMsgQuery="";await snapDetail("cv_abc123def45678","2026-11-01T00:00:00");'
        'if(_el("#dw-body").innerHTML.indexOf("分叉来源")<0){console.error("F1: 抽屉分叉溯源未渲染");process.exit(13);}'
        # Q1: 单张主卡 (5h 进度条 + 恢复 ETA + 跑道) — /ledger/usage fixture
        'const qd=_el("#ov-quota").innerHTML;'
        'if(qd.indexOf("5 小时额度窗口")<0||qd.indexOf("winbar")<0){console.error("Q1: 额度窗口卡未渲染:",qd.slice(0,160));process.exit(16);}'
        'if(qd.indexOf("恢复至 50%")<0||qd.indexOf("剩余跑道")<0){console.error("Q1b: 恢复 ETA/跑道未渲染");process.exit(17);}'
        # Q1c: 纯消耗模式 (无上限) 进度条按 7d 峰值比填充 + 窗口回落 ETA 常显 (绝不隐藏)
        'if(qd.indexOf("窗口回落 50%")<0||qd.indexOf("完全冷却")<0||qd.indexOf("峰值比")<0){console.error("Q1c: 消费模式回落 ETA/峰值比未渲染");process.exit(24);}'
        # Q1d: 卡片摘要 = 全账号 5h 总输入 (sumEl 口径修复)
        'if(_el("#ov-quota-sum").textContent.indexOf("5h 已用")<0){console.error("Q1d: 摘要未按 fiveh 总输入口径");process.exit(25);}'
        # Q1e: 底部 ZCode 历史累计库小字 (model_usage 真相源)
        'if(qd.indexOf("ZCode 历史累计库")<0){console.error("Q1e: zcode 历史库脚注未渲染");process.exit(26);}'
        # Q2: 排行切换 (模型 → 项目), 含估算徽标
        'if(_el("#ov-quota-ranks").innerHTML.indexOf("glm-5")<0||_el("#ov-quota-ranks").innerHTML.indexOf("含估算")<0){console.error("Q2a: 模型排行/估算徽标未渲染");process.exit(18);}'
        'setUsageRank("by_project");'
        'if(_el("#ov-quota-ranks").innerHTML.indexOf("proj-alpha")<0){console.error("Q2b: 项目排行切换未渲染");process.exit(19);}'
        'setUsageRank("by_model");'
        # Q3: 窗口切换器 (5h/24h/7d/30d) — 切到 30d 重拉窗口数据, 趋势图与按模型表随窗口; 全卡无 headroom
        'if(qd.indexOf("ov-wseg")<0||qd.indexOf(\'data-w="30d"\')<0){console.error("Q3a: 窗口切换器未渲染");process.exit(20);}'
        'await setUsageWindow("30d");'
        'const qd3=_el("#ov-quota").innerHTML;'
        'if(qd3.indexOf("mcol")<0||qd3.indexOf("近 14 天消耗趋势")<0){console.error("Q3b: 30d 趋势未随窗口切换渲染:",qd3.slice(0,160));process.exit(27);}'
        'if(qd3.indexOf("headroom")>=0){console.error("Q3c: 主卡内 headroom 痕迹应已移除");process.exit(21);}'
        'await setUsageWindow("5h");'
        # Q4: 厂商实时套餐额度 (plan level + 百分比 + 重置倒计时)
        'const qd2=_el("#ov-quota").innerHTML;'
        'if(qd2.indexOf("套餐余额")<0||qd2.indexOf("42.5")<0||qd2.indexOf("重置")<0){console.error("Q4: 厂商实时额度未渲染:",qd2.slice(0,200));process.exit(22);}'
        # Q5: Antigravity 本机 RPC 实时额度区块
        'if(qd2.indexOf("Antigravity 额度")<0||qd2.indexOf("gemini 5h")<0){console.error("Q5: AGV 实时额度未渲染");process.exit(23);}'
        # Q6: AGV 额度失败不再静默 — 本机有 antigravity 用量记录而 RPC 失败时必须显示轻提示 (含原因)
        'DATA.usage.remote_quota_agv={ok:false,error:"Antigravity language_server 未发现 (IDE 未运行?)"};'
        'renderQuota(DATA.usage,usageLedger);'
        'const qd6=_el("#ov-quota").innerHTML;'
        'if(qd6.indexOf("暂不可用")<0||qd6.indexOf("IDE 未运行")<0){console.error("Q6: AGV 失败提示未渲染 (静默吞错)");process.exit(28);}'
        'if(qd6.indexOf("winbar")<0){console.error("Q6b: 失败提示不应挤掉额度主区块");process.exit(29);}'
        'DATA.usage.remote_quota_agv={ok:true,plan:"Antigravity Free",limits:['
        '{type:"gemini 5h",pct_used:12.0,reset_at:"2026-11-21T18:00:00+08:00"},'
        '{type:"claude/gpt weekly",pct_used:5.0,reset_at:"2026-11-23T01:58:00+08:00"}]};'
        'renderQuota(DATA.usage,usageLedger);'
        'await syncVaultNow();'
        'await loadOps(false);'
        'if(_el("#ops-mcp-box").innerHTML.indexOf("Codex")<0||_el("#ops-maint-box").innerHTML.indexOf("0.10.3")<0){console.error("O1: 设置与运维页未渲染");process.exit(33);}'
        'await opsInstallMcp("codex");await opsPrune(false);'
        'if(_el("#ops-maint-out").innerHTML.indexOf("回收 3 版")<0){console.error("O2: 裁剪预览未渲染");process.exit(34);}'
        'setSnapAgent("codex");await tick();'
        'setMigMode("skills");await tick();await tick();'
        'if(_el("#sk-list").innerHTML.indexOf("archify")<0){console.error("S1: Skill 列表未渲染");process.exit(35);}'
        'await skRunMigrate(false);'
        'if(_el("#sk-result").innerHTML.indexOf("plugin.json")<0||_el("#sk-result").innerHTML.indexOf("待复制")<0){console.error("S2: Skill 迁移预演未渲染");process.exit(36);}'
        'const _skP=skRunMigrate(true);confirmRes(true);confirmRes=null;await _skP;'
        'if(_el("#sk-result").innerHTML.indexOf("已写入")<0||_el("#sk-result").innerHTML.indexOf("待复制")>=0){console.error("S3: Skill 实写状态标签错误");process.exit(37);}'
        'onSkillAgyModeChange();await tick();await tick();'
        'if(_el("#sk-list").style.opacity!==""||_el("#sk-list").innerHTML.indexOf("archify")<0){console.error("S4: soft 切换模式透明度未复位");process.exit(38);}'
        'if(TABS.length!==4||TABS.indexOf("probe")>=0){console.error("T1: 控制台应为无哨兵的四分区");process.exit(39);}'
        'if(_el("#ov-agents").innerHTML.indexOf("codex")<0){console.error("O3: 概览多端资产分布未渲染");process.exit(40);}'
        'console.log("console unit OK: B03 token-strip / B04 loadOv / B06 empty+hit / T4 project-tree / M1 memory / M3 dedup / M2 msg-reader / F1 fork / Q1-Q6 quota / O1-O3 ops+agents / S1-S4 skills-migrate / T1 four-tabs / sync+agy");'
        'process.exit(0);'
        '})().catch(e=>{console.error("unit failed:",(e&&e.message)||e);process.exit(6)});')
    Path("console_unit.js").write_text(mock + js + tests, encoding="utf-8")
    print(f"console_unit.js: {len(js)} chars (unit mode)")
else:
    Path("console_check.js").write_text(js, encoding="utf-8")
    print(f"console_check.js: {len(js)} chars")
