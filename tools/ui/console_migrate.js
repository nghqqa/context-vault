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

