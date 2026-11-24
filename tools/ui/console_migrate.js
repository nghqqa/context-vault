/* ================= 迁移工作台 (支持全库即时搜源 + 一屏双栏预览执行) ================= */
const migSeq=seqGuard(),migPoolSeq=seqGuard();
let migLastPreview=null,migRecentRows=[],migFilterTimer=null;

function migOptionHtml(rows){
  return rows.map(r=>{
    const sum=r.first?cleanSummary(r.first).text.slice(0,26):"";
    const dir=r.cwd?truncPath(r.cwd,18):"";
    const meta=[shortSid(r.sid),r.agent||"?",sum||dir||"",ftime(r.created_at)].filter(Boolean).join(" · ");
    return '<option value="'+esc(r.sid)+'">'+esc(meta)+"</option>";
  }).join("");
}
async function fillMig(){
  let d=null;
  try{d=await migPoolSeq(()=>api("/recent?limit=200&offset=0&leaves=1"))}
  catch(e){
    if(e&&e.stale)return;
    $("#mig-sid").innerHTML='<option value="">加载失败 — 服务异常</option>';
    $("#mig-src-meta").innerHTML="<span class='hint'>加载失败: "+esc(String(e&&e.message||e))+"</span>";
    $("#mig-preview").innerHTML='<div class="mig-idle">快照列表加载失败 — 服务恢复后重进本页重试。</div>';
    return}
  migRecentRows=d.rows||[];
  const sel=$("#mig-sid");
  sel.dataset.filled="1";
  const hint=$("#mig-pool-hint");
  if(hint)hint.textContent="已载入 "+num(migRecentRows.length)+" 个独立会话 · 支持搜全库";
  sel.innerHTML=migRecentRows.length
    ?migOptionHtml(migRecentRows)
    :'<option value="">（vault 为空 — 先 ↻ 立即同步）</option>';
  onMigSource();
}
function onMigFilterInput(){
  const q=($("#mig-search")&&$("#mig-search").value||"").trim();
  const clr=$("#mig-filter-clr");if(clr)clr.hidden=!q;
  clearTimeout(migFilterTimer);
  migFilterTimer=setTimeout(()=>migSearchPool(false),220);
}
function clearMigFilter(){
  const ms=$("#mig-search");if(ms)ms.value="";
  const clr=$("#mig-filter-clr");if(clr)clr.hidden=true;
  const sel=$("#mig-sid");
  sel.innerHTML=migRecentRows.length?migOptionHtml(migRecentRows):'<option value="">（无快照）</option>';
  const hint=$("#mig-pool-hint");if(hint)hint.textContent="已载入 "+num(migRecentRows.length)+" 个独立会话 · 支持搜全库";
  onMigSource();
}
async function migSearchPool(showToast){
  const q=($("#mig-search")&&$("#mig-search").value||"").trim();
  if(!q){clearMigFilter();return}
  const sel=$("#mig-sid"),hint=$("#mig-pool-hint");
  try{
    if(/^cv_[0-9a-f]{8,64}$/i.test(q)){
      const s=await migPoolSeq(()=>api("/snapshot?id="+encodeURIComponent(q)));
      const c=s.capture||{};
      const row=[{sid:q,agent:c.agent,first:c.cwd||"",cwd:c.cwd,created_at:""}];
      sel.innerHTML=migOptionHtml(row);
      if(hint)hint.textContent="按精确 SID 匹配";
      onMigSource();return;
    }
    const d=await migPoolSeq(()=>api("/search?q="+encodeURIComponent(q)+"&limit=30"));
    const hits=d.hits||[];
    const ql=q.toLowerCase();
    const local=migRecentRows.filter(r=>(r.sid||"").toLowerCase().includes(ql)||
      (r.first||"").toLowerCase().includes(ql)||(r.cwd||"").toLowerCase().includes(ql)||
      (r.agent||"").toLowerCase().includes(ql));
    const seen=new Set();const merged=[];
    for(const r of [...hits,...local]){if(r&&r.sid&&!seen.has(r.sid)){seen.add(r.sid);merged.push(r)}}
    sel.innerHTML=merged.length?migOptionHtml(merged):'<option value="">未检索到匹配「'+esc(q)+'」的快照</option>';
    if(hint)hint.textContent="检索命中 "+num(merged.length)+" 条";
    if(showToast&&!merged.length)toast("未找到匹配「"+q+"」的快照",1);
    onMigSource();
  }catch(e){
    if(e&&e.stale)return;
    if(showToast)toast("检索失败: "+String(e&&e.message||e),1);
  }
}
function migTarget(){const r=document.querySelector('input[name=tgt]:checked');return r?r.value:"codex"}
function onMigTarget(){
  const t=migTarget();
  $$("#tgt-cards .tgt-card").forEach(c=>c.classList.toggle("on",c.querySelector("input").value===t));
  const outOnly=t==="markdown";
  $("#mig-md-note").style.display=outOnly?"block":"none";
  if(outOnly)$("#mig-md-note").textContent="Markdown 目标始终导出到 outbox，生成后可直接一键复制简报正文。";
  const br=$("#mig-budget-row");if(br)br.style.display=outOnly?"flex":"none";
  $("#mig-outbox").textContent=outOnly?"导出交接简报（始终 outbox）":"生成到 outbox（安全）";
  $("#mig-apply").style.display=outOnly?"none":"inline-flex";
  renderMigPreview();
}
function onMigSource(){
  const sel=$("#mig-sid");
  const opt=sel.options[sel.selectedIndex];
  $("#mig-src-meta").innerHTML=(!opt||!opt.value)?"<span class='hint'>无可用快照 — 先 <b>↻ 立即同步</b> 或搜其他关键词</span>"
    :"已选 <span class='mono'>"+esc(opt.value)+"</span>";
  migPreview();
}
function migExecEnabled(on){
  $("#mig-outbox").disabled=!on;
  $("#mig-apply").disabled=!on;
  $("#mig-exec-hint").textContent=on?"":"预览就绪后才能执行";
}
async function migPreview(){
  const id=$("#mig-sid").value;
  if(!id){$("#mig-preview").innerHTML='<div class="mig-idle">暂无已选快照 — 在左侧搜索关键词或点击右上角 <b>↻ 立即同步</b>。</div>';
    migExecEnabled(false);return}
  $("#mig-preview").innerHTML='<div class="skel" style="width:55%"></div><div class="skel" style="width:75%"></div><div class="skel" style="width:40%"></div>';
  migLastPreview=null;migExecEnabled(false);
  try{
    const p=await migSeq(()=>api("/preview?id="+id));
    migLastPreview=p;
    renderMigPreview();
  }catch(e){
    if(e&&e.stale)return;
    $("#mig-preview").innerHTML=errEmpty("预览失败："+e.message,"migPreview()");
    migExecEnabled(false);
  }
}
function immList(p){
  const imm=[];
  if(p.no_assistant)imm.push("assistant 文本缺失（源转录不含）— 目标会话只有 user / tool 视角");
  if(p.thinking)imm.push("thinking ×"+p.thinking+" — 仅明文摘要可迁移，加密推理不可移植");
  if(p.agent_messages)imm.push("agent_message ×"+p.agent_messages+" — 降级为带标注的文本");
  if(p.dropped_source)imm.push("源文件被丢弃行 ×"+p.dropped_source+"（developer 注入 / 无移植语义）");
  return imm;
}
function targetLossNote(t){
  if(t==="markdown")return "Markdown 是降级档：仅保留停点/最近动作/活上下文尾部摘要，工具调用配对细节不进简报。";
  if(t==="antigravity")return "Antigravity 目标：经 smigrate 转换并自动补步级时间戳与项目目录；产物落 vault/outbox/，导入后出现在 Conversations 列表。";
  if(t==="zcode")return "ZCode 写回（实验性）：转换为 ZCode session/message/part 写入主库；执行写入前自动先备份 ZCode 数据库。";
  if(t==="dsh")return "DeepSeek Harness 目标：生成规范 V4 会话（.v4.jsonl.zstd）并同步登记 workspace.json；developer 注入块被过滤，未配对 tool_result 逐条计数。";
  if(t==="codex")return "Codex 目标：developer 注入块被过滤（由目标运行时重新注入）；未配对 tool_result 逐条计数进迁移报告。";
  if(t==="claude")return "Claude Code 目标：developer 注入块被过滤；未配对 tool_result 逐条计数进迁移报告。";
  return "";
}
function renderMigPreview(){
  const p=migLastPreview;
  if(!p)return;
  const t=migTarget();
  const imm=immList(p);
  const prof=document.getElementById("mig-src-profile");
  if(prof)prof.innerHTML=
    "<div class='chips'>"+
    (p.agent?"<span class='chip'>"+esc(p.agent)+"</span>":"")+
    (p.completeness&&p.completeness!=="full"?"<span class='chip warn'>"+esc(p.completeness)+"</span>":"<span class='chip ok'>完整</span>")+
    "</div>"+
    "<div class='kv'>"+kv("项目目录",esc(p.cwd||"—"))+
    kv("历史累计输入",num(p.history_tokens_in)+" tokens（源口径）")+"</div>";
  $("#mig-preview").innerHTML=
    '<div class="pv-stats">'+
    '<span class="stat-inline">活上下文 <b>'+num(p.live_blocks)+"</b> 块 · ~"+num(p.live_tokens_est)+" tok</span>"+
    (p.compactions?'<span class="stat-inline">压缩边界 <b>'+num(p.compactions)+"</b> 次</span>":"")+
    '<span class="stat-inline">构成 <b>'+(Object.entries(p.live_dist||{}).map(([k,v])=>esc(k)+"×"+num(v)).join(" · ")||"—")+"</b></span>"+
    '<span class="stat-inline">累计输入 <b>'+num(p.history_tokens_in)+"</b> tok</span>"+
    "</div>"+
    '<div class="pv-cols">'+
    '<div><h3 class="sec">丢失 / 降级说明</h3>'+
    '<ul class="immlist">'+(imm.length?imm.map(i=>"<li>"+WARN+"<span>"+i+"</span></li>").join("")
      :"<li class='good'>"+GOOD+"源端无已知损失项</li>")+"</ul>"+
    '<div class="section-note">'+esc(targetLossNote(t))+"</div></div>"+
    '<div><h3 class="sec">恢复命令（执行后生成真实 ID）</h3><div class="term"><span class="tp">预览命令</span>'+
    '<code id="mig-cmd">$ '+esc(previewCmd(t))+"</code></div>"+
    '<div class="hint" style="margin-top:8px">点击下方执行按钮，结果将替换为可直接复制的精确命令。</div></div>'+
    "</div>";
  migExecEnabled(true);
}
function previewCmd(t){
  if(t==="claude")return "claude --resume";
  if(t==="markdown")return "code vault/outbox/brief-*.md";
  if(t==="antigravity")return "dir vault\\outbox\\harness-antigravity-*.json";
  if(t==="zcode")return "zcode  # 写入模式直接入主库，重启 ZCode 生效";
  if(t==="dsh")return "打开或刷新 DeepSeek Harness（自动出现在对应工作区会话列表）";
  return "codex resume";
}
async function migGo(apply){
  const id=$("#mig-sid").value,t=migTarget();
  if(!id)return toast("没有可迁移的快照",1);
  if(!migLastPreview)return toast("预览未就绪，不能执行",1);
  if(apply){
    const sel=$("#mig-sid");const label=sel.options[sel.selectedIndex]?sel.options[sel.selectedIndex].textContent:id;
    const tgtName=agyTargetLabel(t);
    if(!await askConfirm("源快照: "+label+"\n目标: "+tgtName+"\n\n目标 agent 的会话列表将出现此会话。"))return;
  }
  const btns=[$("#mig-outbox"),$("#mig-apply")];
  btns.forEach(b=>b&&(b.disabled=true));
  $("#mig-result").innerHTML='<div class="skel" style="width:50%"></div>';
  try{
    const reqBody={id:id,target:t,apply:apply};
    if(t==="markdown"){
      const bv=Number(($("#mig-budget")&&$("#mig-budget").value)||0);
      if(bv>0)reqBody.budget_tokens=bv;
    }
    const r=await api("/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(reqBody)});
    renderMigResult(r,{id,target:t,apply});
    toast(r.applied?"已写入会话目录":"已生成到 outbox");
  }catch(e){
    $("#mig-result").innerHTML='<div class="result-card err"><div class="rt">'+ERRIC+'迁移失败</div>'+
      '<div class="hint">'+esc(e.message)+"</div>"+
      '<div style="margin-top:8px"><button class="btn sm" onclick="migGo('+(apply?"true":"false")+')">重试</button></div></div>';
    toast("迁移失败："+e.message,1);
  }
  btns.forEach(b=>b&&(b.disabled=false));
}
let lastMdBrief="";
function copyLastMdBrief(){if(!lastMdBrief)return toast("暂无可复制简报",1);copyText(lastMdBrief,"已复制 Markdown 交接简报正文")}
function renderMigResult(r,ctx){
  const fpath=String(r.dest||"");
  const fname=fpath.split(/[\\/]/).pop();
  lastMdBrief=(ctx.target==="markdown"&&r.content)?String(r.content):"";
  let cmd="",cmdNote="";
  if(ctx.target==="codex"&&r.new_sid){
    cmd=r.applied?("codex resume "+r.new_sid):"codex resume";
    cmdNote=r.applied?"session id 来自迁移结果, 可直接恢复":"outbox 产物 — 写入模式才会进会话目录; 或手动复制后再 resume";
  }else if(ctx.target==="codex"){
    cmd="codex resume";cmdNote="此产物无独立 session id (rollout 文件名内含) — 写入模式后 codex resume 列表可见";
  }else if(ctx.target==="claude"){
    cmd=r.applied?("claude --resume "+fname.replace(/\.jsonl$/i,"")):"claude --resume";
    cmdNote=r.applied?"会话 id = 写入文件名 (uuid)":"outbox 产物 — 写入模式才会进 projects 目录";
  }else if(ctx.target==="antigravity"){
    cmd=fpath;cmdNote=r.applied?"已写入 Antigravity conversations 目录，完全关闭并重启 Antigravity 后在对应项目下显示":"outbox 产物（已对齐原生步骤与项目结构）— 选择写入真实目录或 --apply 可直接落盘";
  }else if(ctx.target==="zcode"){
    cmd=fpath;cmdNote=r.applied?"已写回 ZCode 主库（已先备份原库），完全关闭并重启 ZCode 后在对应项目列表出现":"outbox 产物（ZCode payload）— 选择写入真实目录或 --apply 可写回主库";
  }else if(ctx.target==="dsh"){
    cmd=fpath;cmdNote=r.applied?"已写入 ~/.dsh/sessions 并同步登记 workspace.json，刷新或重启 DeepSeek Harness 即可在工作区列表续聊":"outbox 产物（V4 JSONL）— 选择写入真实目录或 --apply 可压缩为 .zstd 并自动登记工作区";
  }else{
    cmd="code \""+fpath+"\"";cmdNote="Markdown 简报 — 任意 agent / 编辑器可读";
  }
  const mdPart=lastMdBrief
    ?('<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'+
      '<button class="btn sm primary" onclick="copyLastMdBrief()">一键复制简报正文 ('+num(lastMdBrief.length)+' 字符)</button></div>'+
      '<details class="md-preview"><summary>展开预览生成的 Markdown 交接简报</summary><pre>'+esc(lastMdBrief)+'</pre></details>')
    :"";
  $("#mig-result").innerHTML=
    '<div class="result-card"><div class="rt">'+GOOD+
    (r.applied?"已写入目标会话目录":"已生成到 vault/outbox/")+
    (ctx.target==="markdown"?"（交接简报）":"")+"</div>"+
    '<div class="path-row"><span class="hint">文件:</span><span class="mono" style="word-break:break-all">'+esc(fpath)+"</span>"+
    '<button class="btn sm" data-v="'+esc(fpath)+'" onclick="copyData(this,\'已复制文件路径\')">复制路径</button></div>'+
    '<div class="hint">'+num(r.lines)+" 行"+(r.dropped?" · 丢弃未配对 tool_result ×"+num(r.dropped):" · 无丢弃")+
    (r.new_sid?" · session id <span class='mono'>"+esc(r.new_sid)+"</span>":"")+"</div>"+
    mdPart+
    '<div class="term"><span class="tp">'+esc(cmdNote)+"</span><code>$ "+esc(cmd)+
    '</code><br><button class="btn sm" style="margin-top:8px" data-v="'+esc(cmd)+'" onclick="copyData(this,\'已复制命令\')">复制命令</button></div></div>';
}

/* ================= 整端全量 / 批量会话迁移 ================= */
let migScope="single",mbSource="all",mbTarget="codex",mbData=null,mbSelected=new Set(),mbSelectAllMatched=true;
const mbSeq=seqGuard();
let mbFilterTimer=null;
const MB_NAMES={all:"全部源",zcode:"ZCode",claude:"Claude Code",codex:"Codex",antigravity:"Antigravity",dsh:"DeepSeek Harness"};
const MB_TGT_ORDER=["codex","claude","antigravity","zcode","dsh","markdown"];

function setMigScope(scope){
  migScope=(scope==="batch")?"batch":"single";
  const isB=(migScope==="batch");
  const ss=$("#mig-single-shell"),bs=$("#mig-batch-shell");
  if(ss)ss.hidden=isB;
  if(bs)bs.hidden=!isB;
  const b1=$("#mig-scope-single"),b2=$("#mig-scope-batch");
  if(b1){b1.classList.toggle("on",!isB);b1.setAttribute("aria-selected",!isB?"true":"false")}
  if(b2){b2.classList.toggle("on",isB);b2.setAttribute("aria-selected",isB?"true":"false")}
  const sh=$("#mig-scope-hint");
  if(sh)sh.textContent=isB
    ?"按整个 Harness 批量盘点并迁出主对话（支持按项目/已迁移/同源自动去重，一键落 outbox 或写入真实目录）"
    :"单条迁移可预览活上下文、逐项失真与精确恢复命令";
  if(isB&&!mbData)loadMigBatch();
}
function onMbFilterInput(){
  clearTimeout(mbFilterTimer);
  mbFilterTimer=setTimeout(()=>loadMigBatch({soft:true}),220);
}
function _syncMbTargetUi(){
  $$("#mb-tgt-cards .tgt-card").forEach(c=>{
    const inp=c.querySelector("input");
    const v=inp?inp.value:"";
    const on=(v===mbTarget);
    c.classList.toggle("on",on);
    if(inp)inp.checked=on;
    const oldSame=c.querySelector(".mb-same-pill");
    if(oldSame)oldSame.remove();
    if(mbSource&&mbSource!=="all"&&v===mbSource){
      const tt=c.querySelector(".tt");
      if(tt)tt.insertAdjacentHTML("beforeend",' <span class="pill muted mb-same-pill">同源</span>');
    }
  });
  const isMd=(mbTarget==="markdown");
  const note=$("#mb-md-note");if(note)note.style.display=isMd?"block":"none";
  const br=$("#mb-budget-row");if(br)br.style.display=isMd?"flex":"none";
  const ob=$("#mb-outbox");if(ob)ob.textContent=isMd?"导出批量交接简报（始终 outbox）":"批量生成到 outbox（安全）";
  const ap=$("#mb-apply");if(ap)ap.style.display=isMd?"none":"inline-flex";
}
function mbSetSource(ag){
  mbSource=ag||"all";
  mbSelectAllMatched=true;
  const ps=$("#mb-proj");if(ps)ps.value="";
  if(mbSource!=="all"&&mbTarget===mbSource){
    const alt=MB_TGT_ORDER.find(t=>t!=="markdown"&&t!==mbSource);
    if(alt)mbTarget=alt;
  }
  const rs=$("#mb-result");if(rs)rs.innerHTML="";
  _syncMbTargetUi();
  loadMigBatch();
}
function mbSetTarget(t){
  mbTarget=t||"codex";
  const rs=$("#mb-result");if(rs)rs.innerHTML="";
  _syncMbTargetUi();
  loadMigBatch({soft:true});
}
function mbSelectBatch(mode){
  const items=(mbData&&mbData.items)||[];
  if(mode==="none"){
    mbSelectAllMatched=false;
    mbSelected.clear();
  }else if(mode==="visible"){
    mbSelectAllMatched=false;
    mbSelected=new Set(items.filter(it=>!it.same_origin).map(it=>it.id));
  }else{
    mbSelectAllMatched=true;
    mbSelected=new Set(items.filter(it=>it.status==="ready").map(it=>it.id));
  }
  mbRenderList();
  mbUpdateSummary();
}
function mbToggleItem(sid,checked){
  if(mbSelectAllMatched&&mbData){
    mbSelected=new Set((mbData.items||[]).filter(it=>it.status==="ready").map(it=>it.id));
    mbSelectAllMatched=false;
  }
  if(checked)mbSelected.add(sid);
  else mbSelected.delete(sid);
  mbUpdateSummary();
}
function mbRenderSources(){
  const bar=$("#mb-src-bar");if(!bar)return;
  const srcs=(mbData&&mbData.sources)||[];
  const totAll=(mbData&&mbData.total_all)||0;
  const pills=[{agent:"all",count:totAll},...srcs];
  bar.innerHTML=pills.map(s=>{
    const on=(s.agent===mbSource);
    return '<button type="button" class="pill-btn'+(on?' on':'')+'" onclick="mbSetSource(\''+esc(s.agent)+'\')">'+
      esc(MB_NAMES[s.agent]||s.agent)+' <span class="mono" style="opacity:.78">'+num(s.count||0)+'</span></button>';
  }).join("");
}
function mbRenderProjects(){
  const sel=$("#mb-proj");if(!sel)return;
  const cur=sel.value||"";
  const projs=(mbData&&mbData.projects)||[];
  sel.innerHTML='<option value="">全部项目 ('+num(projs.length)+')</option>'+projs.map(p=>
    '<option value="'+esc(p.cwd)+'">'+esc(p.name)+' ('+num(p.count)+')</option>'
  ).join("");
  if(cur&&projs.some(p=>p.cwd===cur))sel.value=cur;
}
function mbRenderList(){
  const el=$("#mb-list");if(!el)return;
  const items=(mbData&&mbData.items)||[];
  const note=$("#mb-trunc-note");
  const c=(mbData&&mbData.counts)||{};
  if(note){
    if(mbData&&mbData.truncated){
      note.hidden=false;
      note.textContent="当前仅预览前 "+num(items.length)+" 条（共筛选命中 "+num(mbData.total||0)+
        " 条）；保持「全选整端待迁」将一次处理全部 "+num(c.ready||0)+" 条待迁移会话。";
    }else{
      note.hidden=true;
    }
  }
  if(!items.length){
    el.innerHTML='<div class="empty"><span>当前筛选条件下没有可迁移的主对话（请切换源 Harness 或勾选「包含已归档」）。</span></div>';
    return;
  }
  el.innerHTML=items.map(it=>{
    const dis=(it.status==="skip_same_origin"||(it.status==="skip_migrated"&&$("#mb-skip-mig")&&$("#mb-skip-mig").checked));
    const chk=(!dis&&mbSelected.has(it.id));
    let pill='<span class="pill ok">待迁移</span>';
    if(it.status==="skip_same_origin")pill='<span class="pill muted">同源跳过</span>';
    else if(it.status==="skip_migrated")pill='<span class="pill warn">已迁入跳过</span>';
    const cwdShort=it.cwd?truncPath(it.cwd,26):"未关联工作区";
    return '<label class="sk-row">'+
      '<input type="checkbox"'+(chk?' checked':'')+(dis?' disabled':'')+' onchange="mbToggleItem(\''+esc(it.id)+'\',this.checked)">'+
      '<div class="sk-row-b"><div class="sk-row-top">'+
      '<span class="sk-slug">'+esc((it.title||it.id).slice(0,56))+'</span>'+pill+
      '<button type="button" class="btn sm sk-inspect-btn" onclick="event.preventDefault();event.stopPropagation();snapDetail(\''+esc(it.id)+'\',\''+esc(it.created_at||"")+'\')">详情</button>'+
      '<span class="sk-tok">'+num(it.blocks_total||0)+' 块</span></div>'+
      '<div class="mb-row-meta"><span class="chip">'+esc(it.agent||it.canonical_agent||"?")+'</span>'+
      '<span class="mb-row-cwd" title="'+esc(it.cwd||"")+'">'+esc(cwdShort)+'</span>'+
      '<span class="mono">'+esc(frel(it.created_at))+'</span></div></div></label>';
  }).join("");
}
function mbPlannedCount(){
  if(!mbData)return 0;
  if(mbSelectAllMatched)return Number((mbData.counts&&mbData.counts.ready)||0);
  return mbSelected.size;
}
function mbUpdateSummary(){
  const n=mbPlannedCount();
  const c=(mbData&&mbData.counts)||{};
  const stEl=$("#mb-sel-stat");
  if(stEl){
    stEl.textContent=mbSelectAllMatched
      ?("整端待迁 "+num(n)+" / "+num(c.total||0)+" 条")
      :("已手动勾选 "+num(n)+" 条");
  }
  const sum=$("#mb-summary");
  if(sum&&mbData){
    const srcLbl=MB_NAMES[mbSource]||mbSource;
    const tgtLbl=(mbTarget==="markdown")?"Markdown 交接简报":agyTargetLabel(mbTarget);
    sum.innerHTML=
      '<div>源范围：<b>'+esc(srcLbl)+'</b> · 命中主对话 <b>'+num(c.total||0)+'</b> 条（涉及 '+num(c.projects||0)+' 个项目）</div>'+
      '<div>目标：<b>'+esc(tgtLbl)+'</b> · 计划处理 <b style="color:var(--acc-strong)">'+num(n)+'</b> 条'+
      (c.skip_migrated?' · 自动跳过已迁移 '+num(c.skip_migrated)+' 条':'')+
      (c.skip_same_origin?' · 同源跳过 '+num(c.skip_same_origin)+' 条':'')+
      (mbTarget==="zcode"?' · ZCode 写入前仅备份 1 次主库':'')+'</div>';
  }
  const ob=$("#mb-outbox"),ap=$("#mb-apply");
  if(ob)ob.disabled=(n===0);
  if(ap)ap.disabled=(n===0);
}
async function loadMigBatch(opts){
  const o=opts||{};
  const listEl=$("#mb-list");
  if(listEl){
    if(o.soft&&mbData)listEl.style.opacity="0.65";
    else listEl.innerHTML='<div style="padding:12px"><div class="skel" style="width:60%"></div><div class="skel" style="width:78%"></div></div>';
  }
  const q=(($("#mb-q")&&$("#mb-q").value)||"").trim();
  const proj=(($("#mb-proj")&&$("#mb-proj").value)||"").trim();
  const skipMig=!($("#mb-skip-mig")&&$("#mb-skip-mig").checked===false);
  const incArch=!!($("#mb-inc-arch")&&$("#mb-inc-arch").checked);
  const qs=[
    "source="+encodeURIComponent(mbSource||"all"),
    "target="+encodeURIComponent(mbTarget||"codex"),
    "skip_migrated="+(skipMig?1:0),
    "archived="+(incArch?1:0),
  ];
  if(q)qs.push("q="+encodeURIComponent(q));
  if(proj)qs.push("project="+encodeURIComponent(proj));
  try{
    const d=await mbSeq(()=>api("/migrate/batch?"+qs.join("&")));
    if(listEl)listEl.style.opacity="";
    mbData=d;
    const validIds=new Set((d.items||[]).filter(it=>!it.same_origin).map(it=>it.id));
    if(mbSelectAllMatched){
      mbSelected=new Set((d.items||[]).filter(it=>it.status==="ready").map(it=>it.id));
    }else{
      mbSelected=new Set([...mbSelected].filter(id=>validIds.has(id)));
    }
    mbRenderSources();
    mbRenderProjects();
    _syncMbTargetUi();
    mbRenderList();
    mbUpdateSummary();
  }catch(e){
    if(listEl)listEl.style.opacity="";
    if(e&&e.stale)return;
    if(listEl)listEl.innerHTML=errEmpty("批量迁移列表加载失败："+e.message,"loadMigBatch()");
  }
}
async function mbRun(apply){
  const effApply=!!apply&&mbTarget!=="markdown";
  const count=mbPlannedCount();
  if(!count)return toast("当前没有可迁移的会话（请切换源/取消跳过已迁移或勾选会话）",1);
  if(effApply){
    const srcLbl=MB_NAMES[mbSource]||mbSource;
    const msg="源 Harness: "+srcLbl+"\n目标: "+agyTargetLabel(mbTarget)+"\n预计迁移: "+num(count)+" 条主对话\n\n"+
      "单条异常不会中断整批，已迁移会话会自动登记 migrations.jsonl。";
    if(!await askConfirm(msg,"确认整端批量写入真实会话目录","确认批量迁移"))return;
  }
  const btns=[$("#mb-outbox"),$("#mb-apply")];
  btns.forEach(b=>b&&(b.disabled=true));
  const resEl=$("#mb-result");
  if(resEl)resEl.innerHTML='<div class="skel" style="width:60%"></div>';
  const q=(($("#mb-q")&&$("#mb-q").value)||"").trim();
  const proj=(($("#mb-proj")&&$("#mb-proj").value)||"").trim();
  const skipMig=!($("#mb-skip-mig")&&$("#mb-skip-mig").checked===false);
  const incArch=!!($("#mb-inc-arch")&&$("#mb-inc-arch").checked);
  const payload={
    source:mbSource||"all",
    target:mbTarget||"codex",
    project:proj||null,
    query:q||null,
    skip_migrated:skipMig,
    archived:incArch,
    apply:effApply,
  };
  if(!mbSelectAllMatched)payload.selected=[...mbSelected];
  if(mbTarget==="markdown"){
    const bv=Number(($("#mb-budget")&&$("#mb-budget").value)||0);
    if(bv>0)payload.budget_tokens=bv;
  }
  try{
    const r=await api("/migrate/batch",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)});
    const c=r.counts||{};
    const sortedItems=[...(r.items||[])].sort((a,b)=>(a.action==="failed"?-1:0)-(b.action==="failed"?-1:0));
    const actLbl={migrated:"已写入",outbox:"outbox",skipped_migrated:"已迁过跳过",skipped_same_origin:"同源跳过",failed:"失败"};
    const rowsHtml=sortedItems.slice(0,100).map(it=>{
      const cls=it.action==="failed"?"err":((it.action==="migrated"||it.action==="outbox")?"ok":"muted");
      const info=it.error||(it.lines?(num(it.lines)+" 行"):"")||(it.reason||"");
      return '<div class="sk-res-row"><span class="pill '+cls+'">'+esc(actLbl[it.action]||it.action)+'</span>'+
        '<span class="mono">'+esc(shortSid(it.id))+'</span>'+
        '<span>'+esc((it.title||"").slice(0,32))+'</span>'+
        (info?'<span class="hint">'+esc(info)+'</span>':"")+'</div>';
    }).join("");
    if(resEl){
      resEl.innerHTML=
        '<div class="result-card"><div class="rt">'+(c.failed?WARN:GOOD)+(r.applied?"整端批量迁移已写入目标目录":"整端批量预演完成（已落 vault/outbox/）")+'</div>'+
        '<div class="path-row"><span class="hint">输出位置:</span><span class="mono" style="word-break:break-all">'+esc(r.dest_dir||"")+'</span>'+
        '<button type="button" class="btn sm" data-v="'+esc(r.dest_dir||"")+'" onclick="copyData(this,\'已复制输出路径\')">复制路径</button></div>'+
        '<div class="hint">成功 '+num(c.migrated||0)+' · 跳过已迁 '+num(c.skipped_migrated||0)+
        ' · 同源跳过 '+num(c.skipped_same_origin||0)+' · 失败 '+num(c.failed||0)+
        ' · 共 '+num(c.total_lines||0)+' 行 · 耗时 '+esc(String(r.elapsed||0))+'s'+
        (r.zcode_backup?' · 已备份 ZCode 主库 '+esc(r.zcode_backup):'')+'</div>'+
        (r.applied?'<div class="hint" style="margin-top:4px">'+esc(agyApplyHint(mbTarget))+'</div>':'')+
        (rowsHtml?'<div class="sk-res-tbl">'+rowsHtml+'</div>':"")+'</div>';
    }
    toast((r.applied?"批量迁移完成：写入 ":"批量生成完成：")+num(c.migrated||0)+" 条");
    if(r.applied)await loadMigBatch({soft:true});
  }catch(e){
    if(resEl)resEl.innerHTML='<div class="result-card err"><div class="rt">'+ERRIC+'批量迁移失败</div><div class="hint">'+esc(e.message)+'</div></div>';
    toast("批量迁移失败："+e.message,1);
  }
  btns.forEach(b=>b&&(b.disabled=false));
  mbUpdateSummary();
}

