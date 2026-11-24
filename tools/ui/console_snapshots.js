/* ================= 快照资产 & 项目树 & 跨会话记忆库 ================= */
const snapsSeq=seqGuard();
let snapPage=0,snapAsc=false,snapMode="recent",snapQuery="",snapLeaves=true,snapTreeMode=false,snapMemMode=false,snapAgent="",memKind="all",snapInputTimer=null,treeTarget="antigravity",treeApply=false,treeAgent="zcode",treeArchived=false;
function setSnapAgent(a){
  snapAgent=String(a||"");snapPage=0;snapMode="recent";
  $$("#snap-agent-bar .pill-btn").forEach(b=>b.classList.toggle("on",(b.dataset.a||"")===snapAgent));
  renderSnaps();
}
const projOpenState={};

function updateSnapTreeBtn(){
  const b=$("#snap-tree-btn"),lbBtn=$("#snap-list-btn"),mbBtn=$("#snap-mem-btn"),qEl=$("#snap-q");
  const isList=!snapTreeMode&&!snapMemMode;
  if(b){b.setAttribute("aria-pressed",snapTreeMode?"true":"false");b.classList.toggle("on",snapTreeMode)}
  if(mbBtn){mbBtn.setAttribute("aria-pressed",snapMemMode?"true":"false");mbBtn.classList.toggle("on",snapMemMode)}
  if(lbBtn){lbBtn.setAttribute("aria-pressed",isList?"true":"false");lbBtn.classList.toggle("on",isList)}
  const sb=$("#snap-sort"),lb=$("#snap-leaves"),ab=$("#snap-agent-bar");
  if(sb)sb.style.display=isList?"":"none";
  if(lb)lb.style.display=isList?"":"none";
  if(ab)ab.style.display=isList?"":"none";
  if(qEl){
    qEl.placeholder=snapMemMode?"检索跨会话记忆 — 历史决策、架构约定或用户偏好…"
      :snapTreeMode?"即时筛选项目名或目录路径…"
      :"检索全库历史会话 — 关键词、项目名或文件路径…";
  }
}
function snapTreeToggle(){
  snapMemMode=false;snapTreeMode=!snapTreeMode;
  updateSnapTreeBtn();
  if(snapTreeMode){loadProjectTree()}else{snapMode==="search"?snapSearch(true):renderSnaps()}
}
function snapMemToggle(){
  snapTreeMode=false;snapMemMode=!snapMemMode;
  updateSnapTreeBtn();
  if(snapMemMode){loadMemoryView()}else{snapMode==="search"?snapSearch(true):renderSnaps()}
}
function openMemoryView(){
  setTab("snap");
  snapTreeMode=false;snapMemMode=true;
  updateSnapTreeBtn();
  loadMemoryView();
}
function onSnapQueryInput(){
  clearTimeout(snapInputTimer);
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  if(snapMemMode){snapInputTimer=setTimeout(()=>loadMemoryView(),220);return}
  if(snapTreeMode){snapInputTimer=setTimeout(()=>loadProjectTree(),220);return}
  if(!q&&snapMode==="search"){snapInputTimer=setTimeout(()=>loadSnaps(),180)}
}
function snapLeavesToggle(){
  snapLeaves=!snapLeaves;snapPage=0;
  const b=$("#snap-leaves");b.textContent=snapLeaves?"仅最新版本":"全部版本";
  b.setAttribute("aria-pressed",snapLeaves?"true":"false");
  if(snapTreeMode||snapMemMode){snapTreeMode=false;snapMemMode=false;updateSnapTreeBtn()}
  if(snapMode==="search"){snapSearch();return}
  renderSnaps();
}
async function refreshSnaps(){
  if(snapMemMode){try{await loadMemoryView()}catch(e){}return}
  if(snapTreeMode){try{await loadProjectTree()}catch(e){}return}
  if(snapMode==="search"){try{await snapSearch()}catch(e){}return}
  await renderSnaps();
}
const LIM=30;

async function loadSnaps(page){
  snapTreeMode=false;snapMemMode=false;updateSnapTreeBtn();
  if(typeof page==="number")snapPage=Math.max(0,page);else snapPage=0;
  snapMode="recent";snapQuery="";
  $("#snap-q").value="";
  await renderSnaps();
}
function snapSort(){
  snapAsc=!snapAsc;snapPage=0;
  $("#snap-sort").textContent=snapAsc?"时间 ↑":"时间 ↓";
  if(snapMode==="search")snapSearch(true);else renderSnaps();
}
async function snapSearch(keepPage){
  const q=$("#snap-q").value.trim();
  if(snapMemMode)return loadMemoryView();
  if(snapTreeMode)return loadProjectTree();
  if(!q)return loadSnaps();
  if(!keepPage)snapPage=0;
  snapMode="search";snapQuery=q;
  $("#snap-list").innerHTML='<div class="skel" style="width:40%"></div><div class="skel" style="width:70%"></div>';
  try{
    const d=await snapsSeq(()=>api("/search?q="+encodeURIComponent(q)+"&limit=50"));
    renderSnapTable(d.hits||[],{search:q,hits:d.total!=null?d.total:(d.hits?d.hits.length:0)});
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("搜索失败："+e.message,"snapSearch(true)");
    $("#snap-meta").textContent="";
  }
}
function setMemKind(k){
  memKind=k||"all";
  loadMemoryView();
}
async function extractMemoriesNow(force){
  const btn=$("#mem-ext-btn"),old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="提取中…"}
  try{
    const r=await api("/memory/extract",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:!!force})});
    toast("记忆提取完成：扫描 "+num(r.scanned||0)+" 快照 · 新增 +"+num(r.added||0)+" · 总记忆 "+num(r.total||0));
    loadOv();
    if(snapMemMode)await loadMemoryView();
  }catch(e){
    toast("记忆提取失败："+e.message,1);
  }
  if(btn){btn.disabled=false;btn.textContent=old}
}
async function dedupMemoriesNow(){
  try{
    const r=await api("/memory/dedup",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    if(!r.removed){toast("没有可合并的重复摘录，记忆库已是最简");return}
    toast("已合并重复摘录 −"+num(r.removed||0)+" 条 · 剩余 "+num(r.total||0)+" 条");
    loadOv();
    if(snapMemMode)await loadMemoryView();
  }catch(e){
    toast("去重失败："+e.message,1);
  }
}
let memComposeOpen=false;
function toggleMemCompose(){
  memComposeOpen=!memComposeOpen;
  return loadMemoryView();
}
async function submitManualMem(){
  const k=($("#mc-kind")&&$("#mc-kind").value)||"decision";
  const proj=($("#mc-proj")&&$("#mc-proj").value||"").trim()||"manual";
  const content=($("#mc-txt")&&$("#mc-txt").value||"").trim();
  const pin=!!($("#mc-pin")&&$("#mc-pin").checked);
  if(!content){toast("请先输入记忆内容",1);return}
  try{
    await api("/memory/add",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({kind:k,project:proj,content:content,pinned:pin})});
    memComposeOpen=false;
    toast("已写入长期记忆库");
    loadOv();
    await loadMemoryView();
  }catch(e){toast("写入失败："+e.message,1)}
}
async function toggleMemPin(id,nextPin){
  try{
    await api("/memory/pin",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,pinned:!!nextPin})});
    toast(nextPin?"已置顶该记忆":"已取消置顶");
    loadOv();
    await loadMemoryView();
  }catch(e){toast("操作失败："+e.message,1)}
}
async function deleteMemItem(id){
  try{
    await api("/memory/delete",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id})});
    toast("已删除记忆条目 #"+id);
    loadOv();
    await loadMemoryView();
  }catch(e){toast("删除失败："+e.message,1)}
}
async function loadMemoryView(){
  snapMemMode=true;snapTreeMode=false;updateSnapTreeBtn();
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  $("#snap-list").innerHTML='<div class="skel" style="width:50%"></div><div class="skel" style="width:72%"></div>';
  try{
    const qs=["limit=80","q="+encodeURIComponent(q)];
    if(memKind&&memKind!=="all")qs.push("kind="+encodeURIComponent(memKind));
    const d=await snapsSeq(()=>api("/memory?"+qs.join("&")));
    const arr=(d&&d.memories)||[];
    const cnts=(d&&d.counts&&d.counts.by_kind)||{};
    const totAll=(d&&d.counts&&d.counts.total)||arr.length;
    const uq=(d&&d.counts&&d.counts.unique);
    $("#snap-meta").innerHTML=(q?('记忆检索「'+esc(q)+'」· '):'跨会话长期记忆库 · ')+
      "展示 <b class='num'>"+num(arr.length)+"</b> 条去重记忆（库内原始 <b class='num'>"+num(totAll)+"</b> 条"+
      (uq!=null?(" · 合并后 <b class='num'>"+num(uq)+"</b> 条"):"")+" · 每条点击来源快照可核实原话）"+
      (q?' <button class="xbtn" onclick="$(\'#snap-q\').value=\'\';loadMemoryView()">清空检索 ×</button>':"");
    const pill=(k,lbl,n)=>'<button type="button" class="btn sm'+(memKind===k?' on':'')+'" onclick="setMemKind(\''+k+'\')">'+lbl+(n!=null?(' <span class="mono">('+num(n)+')</span>'):'')+'</button>';
    const dock='<div class="mem-dock">'+
      '<div class="mem-pills" role="group" aria-label="记忆类型筛选">'+
        pill("all","全部",totAll)+
        pill("decision","决策",cnts.decision||0)+
        pill("preference","偏好",cnts.preference||0)+
        pill("pitfall","踩坑",cnts.pitfall||0)+
        pill("constraint","红线",cnts.constraint||0)+
        pill("summary","压缩摘要",cnts.summary||0)+
      '</div>'+
      '<div class="mem-acts">'+
        '<button class="btn sm'+(memComposeOpen?' on':'')+'" onclick="toggleMemCompose()">+ 手动记一条</button>'+
        '<button class="btn sm primary" id="mem-ext-btn" onclick="extractMemoriesNow(false)">增量提取记忆</button>'+
        '<button class="btn sm" onclick="dedupMemoriesNow()" title="合并 (类型, 内容) 完全相同的跨快照重复摘录，保留最早一条">去重合并</button>'+
        '<button class="btn sm" onclick="extractMemoriesNow(true)" title="忽略已提取水位，重新扫描全库快照提取记忆">全量重提</button>'+
      '</div></div>';
    const composeHtml=memComposeOpen?(
      '<div class="mem-compose">'+
        '<div class="mem-compose-row">'+
          '<select id="mc-kind" aria-label="记忆类型">'+
            '<option value="decision">决策 (decision)</option>'+
            '<option value="constraint">架构红线 (constraint)</option>'+
            '<option value="pitfall">踩坑记录 (pitfall)</option>'+
            '<option value="preference">个人偏好 (preference)</option>'+
          '</select>'+
          '<input type="text" id="mc-proj" placeholder="归属项目名 (如 context-vault)" aria-label="归属项目">'+
          '<label class="hint" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer"><input type="checkbox" id="mc-pin" checked> 置顶优先</label>'+
          '<button type="button" class="btn sm primary" onclick="submitManualMem()">保存记忆</button>'+
          '<button type="button" class="btn sm" onclick="toggleMemCompose()">取消</button>'+
        '</div>'+
        '<textarea id="mc-txt" placeholder="输入长期有效的架构决策、踩坑结论或个人偏好（将立即对 MCP vault_remember 生效）…"></textarea>'+
      '</div>'
    ):"";
    if(!arr.length){
      $("#snap-list").innerHTML=dock+composeHtml+'<div class="empty">'+WARN+
        '<div>'+(q?('没有匹配「'+esc(q)+'」的跨会话记忆'):'当前分类下暂无记忆条目')+'</div>'+
        '<div class="hint">记忆由规则引擎从历史对话自动提取，也可点击「+ 手动记一条」录入关键红线，永不修改原快照。</div>'+
        '<button class="btn primary" onclick="extractMemoriesNow(false)">立即从全库快照提取</button></div>';
      return;
    }
    const listHtml=arr.map(m=>{
      const km=memKindMeta(m.kind);
      const cleanTxt=stripMd(m.content).replace(/\s+/g," ").trim();
      const sid=m.snapshot||"";
      const validSid=/^cv_[0-9a-f]{8,64}$/i.test(sid);
      const isPin=!!m.pinned;
      const ops=(m.id!=null)?(
        '<button type="button" class="mem-op'+(isPin?' pin-on':'')+'" onclick="toggleMemPin('+m.id+','+(!isPin)+')">'+(isPin?'已置顶':'置顶')+'</button>'+
        '<button type="button" class="mem-op del" onclick="deleteMemItem('+m.id+')">删除</button>'
      ):"";
      return '<div class="mem-row'+(isPin?' pinned':'')+'">'+
        '<div class="mem-row-top">'+
          (isPin?'<span class="chip err">置顶</span>':'')+
          '<span class="chip '+km.cls+'">'+esc(km.t)+'</span>'+
          '<span class="chip">'+esc(m.project||"全局")+'</span>'+
          (m.hits>0?'<span class="mini-tag">引用 ×'+num(m.hits)+'</span>':'')+
          '<span class="mem-row-meta">'+
            (validSid?('来源 '+sidBtn(sid)):(sid==="manual"?"手动录入":esc(sid)))+
            ops+
          '</span>'+
        '</div>'+
        '<div class="mem-row-txt">'+highlightPlain(esc(cleanTxt),q)+'</div>'+
      '</div>';
    }).join("");
    $("#snap-list").innerHTML=dock+composeHtml+'<div class="mem-list">'+listHtml+'</div>';
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("记忆库加载失败："+e.message,"loadMemoryView()");
    $("#snap-meta").textContent="";
  }
}
async function syncVaultNow(){
  const btns=[$("#sync-now-btn"),$("#top-sync-btn")].filter(Boolean);
  const olds=btns.map(b=>b.innerHTML);
  btns.forEach(b=>{b.disabled=true;b.textContent="同步中…"});
  try{
    const r=await api("/sync",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    const h=await checkHealth();
    if(h)lastSnapTotal=h.snapshots;
    loadOv();
    await refreshSnaps();
    toast("入库同步完成：新增 "+num(r.new||0)+" · 更新 "+num(r.updated||0)+
      (r.memories_added?(" · 新增记忆 +"+num(r.memories_added)):"")+" · 总快照 "+num(r.total||0));
  }catch(e){
    toast("同步失败："+e.message,1);
  }
  btns.forEach((b,i)=>{b.disabled=false;b.innerHTML=olds[i]});
}
async function loadProjectTree(){
  snapTreeMode=true;snapMemMode=false;updateSnapTreeBtn();
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  $("#snap-list").innerHTML='<div class="skel" style="width:45%"></div><div class="skel" style="width:72%"></div><div class="skel" style="width:60%"></div>';
  try{
    const qs=[];
    if(q)qs.push("project="+encodeURIComponent(q));
    if(treeAgent)qs.push("agent="+encodeURIComponent(treeAgent));
    if(treeArchived)qs.push("archived=1");
    const url="/projects"+(qs.length?("?"+qs.join("&")):"");
    const d=await snapsSeq(()=>api(url));
    renderProjectTree(d,q);
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("项目树加载失败："+e.message,"loadProjectTree()");
    $("#snap-meta").textContent="";
  }
}
function toggleAllProjects(open){
  $$("#snap-list details.proj-card").forEach(el=>{
    el.open=!!open;
    if(el.dataset&&el.dataset.cwd)projOpenState[el.dataset.cwd]=!!open;
  });
}
function onProjCardToggle(el){
  if(el&&el.dataset&&el.dataset.cwd)projOpenState[el.dataset.cwd]=!!el.open;
}
const CHEV_IC='<svg class="tw" width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const PROJ_FOLD_IC='<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 4.5a1.3 1.3 0 0 1 1.3-1.3h2.8l1.3 1.3h5.3a1.3 1.3 0 0 1 1.3 1.3v6.2a1.3 1.3 0 0 1-1.3 1.3H3.3A1.3 1.3 0 0 1 2 12V4.5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const PROJ_CHAT_IC='<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 3.5h10a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H6.5L3.5 14v-2.3H3a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const treeCanApply=t=>t==="codex"||t==="claude"||t==="antigravity"||t==="zcode";
function onTreeTgtChange(val){
  treeTarget=val;
  const ok=treeCanApply(val);
  const cb=$("#tree-apply"),lbl=$("#tree-apply-wrap"),hint=$("#tree-apply-hint");
  if(!ok){
    treeApply=false;
    if(cb){cb.checked=false;cb.disabled=true}
  }else{
    if(cb)cb.disabled=false;
  }
  if(lbl)lbl.style.opacity=ok?"1":".5";
  if(hint)hint.textContent=ok?"":"（该目标仅导出至 vault/outbox/）";
}
function renderProjectTree(d,q){
  const projs=(d&&d.projects)||[];
  const tp=d.total_projects||projs.length,tc=d.total_conversations||0;
  const applyOk=treeCanApply(treeTarget);
  if(!applyOk)treeApply=false;
  $("#snap-meta").innerHTML=(q?('项目筛选「'+esc(q)+'」· '):'按项目树折叠浏览 · ')+
    "共 <b class='num'>"+num(tp)+"</b> 个项目文件夹 · <b class='num'>"+num(tc)+
    "</b> 条顶层主对话（已过滤历史版本与子代理碎片"+(treeArchived?"；含已归档":"")+"）"+
    (q?' <button class="xbtn" onclick="$(\'#snap-q\').value=\'\';loadProjectTree()">查看全部项目 ×</button>':"");
  const baseAgentOpts=[
    ["zcode","仅 ZCode（默认）"],
    ["","全部来源（ZCode / Antigravity / Claude / Codex…）"],
    ["antigravity","仅 Antigravity"],
    ["claude-code","仅 Claude Code"],
    ["codex","仅 Codex"]
  ];
  const seenAg=new Set(baseAgentOpts.map(x=>x[0]));
  const extraAgs=((d&&d.available_agents)||[]).filter(a=>a&&!seenAg.has(String(a).toLowerCase()));
  const agentOptHtml=baseAgentOpts.concat(extraAgs.map(a=>[String(a).toLowerCase(),"仅 "+String(a)])).map(pair=>
    '<option value="'+esc(pair[0])+'"'+(treeAgent===pair[0]?" selected":"")+'>'+esc(pair[1])+'</option>'
  ).join("");
  const bar='<div class="proj-dock">'+
    '<div class="proj-toolbar">'+
      '<label for="tree-agent">来源筛选</label>'+
      '<select id="tree-agent" onchange="treeAgent=this.value;loadProjectTree()">'+
        agentOptHtml+
      '</select>'+
      '<label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer">'+
        '<input type="checkbox" id="tree-arch"'+(treeArchived?' checked':'')+' style="accent-color:var(--acc)" onchange="treeArchived=this.checked;loadProjectTree()">包含已归档</label>'+
      '<div class="pt-end">'+
        '<button class="btn sm" onclick="toggleAllProjects(true)">全部展开</button>'+
        '<button class="btn sm" onclick="toggleAllProjects(false)">全部收起</button>'+
      '</div>'+
    '</div>'+
    '<div class="proj-batch-row">'+
      '<label for="tree-tgt">单条 / 整项导出目标</label>'+
      '<select id="tree-tgt" onchange="onTreeTgtChange(this.value)">'+
        '<option value="antigravity"'+(treeTarget==="antigravity"?" selected":"")+'>Antigravity（原生 .db 会话）</option>'+
        '<option value="claude"'+(treeTarget==="claude"?" selected":"")+'>Claude Code</option>'+
        '<option value="codex"'+(treeTarget==="codex"?" selected":"")+'>Codex</option>'+
        '<option value="markdown"'+(treeTarget==="markdown"?" selected":"")+'>Markdown 简报（outbox）</option>'+
        '<option value="zcode"'+(treeTarget==="zcode"?" selected":"")+'>ZCode 写回（实验性）</option>'+
        '<option value="cursor"'+(treeTarget==="cursor"?" selected":"")+'>Cursor</option>'+
        '<option value="opencode"'+(treeTarget==="opencode"?" selected":"")+'>OpenCode</option>'+
        '<option value="qwen"'+(treeTarget==="qwen"?" selected":"")+'>Qwen Code</option>'+
        '<option value="kimi"'+(treeTarget==="kimi"?" selected":"")+'>Kimi CLI</option>'+
        '<option value="copilot"'+(treeTarget==="copilot"?" selected":"")+'>Copilot</option>'+
      '</select>'+
      '<label id="tree-apply-wrap" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;opacity:'+(applyOk?"1":".5")+'">'+
        '<input type="checkbox" id="tree-apply"'+(treeApply&&applyOk?' checked':'')+(applyOk?'':' disabled')+' style="accent-color:var(--acc)" onchange="treeApply=this.checked">同时写入真实目录</label>'+
      '<span class="hint" id="tree-apply-hint">'+(applyOk?'':'（该目标仅导出至 vault/outbox/）')+'</span>'+
    '</div>'+
    '<div class="proj-sync">'+
      '<span class="ps-lbl">Antigravity 免命令行双向维护</span>'+
      '<span class="hint">清理归档/已删/重复 · 按当前来源一键补齐活跃会话</span>'+
      '<div class="ps-right">'+
        '<button class="btn sm" id="agy-sync-btn" onclick="agySyncPreview()">清理 Antigravity</button>'+
        '<button class="btn sm primary" id="agy-batch-btn" onclick="agyBatchPreview()">一键补齐导入 → Antigravity</button>'+
      '</div>'+
    '</div>'+
  '</div>'+
  '<div id="tree-export-res" hidden></div>';
  if(!projs.length){
    $("#snap-list").innerHTML=bar+'<div class="empty">'+WARN+'<div>没有匹配「'+esc(q||"")+'」的项目或主对话</div>'+
      (q?'<button class="btn" onclick="$(\'#snap-q\').value=\'\';loadProjectTree()">显示全部项目</button>'
        :'<button class="btn" onclick="loadSnaps()">返回列表</button>')+'</div>';
    return;
  }
  const cards=projs.map((g,i)=>{
    const cwdKey=String(g.cwd||g.name||i);
    const isOpen=(cwdKey in projOpenState)?projOpenState[cwdKey]:(i<2);
    const open=isOpen?" open":"";
    const cwdDisp=g.is_ungrouped?"ZCode / 默认工作区未分组任务":g.cwd;
    const rows=(g.conversations||[]).map(c=>{
      const cleaned=cleanSummary(c.title||"(无标题)");
      const sid=c.id;
      return '<div class="proj-row" onclick="snapDetail(\''+sid+'\',\''+esc(c.created_at||"")+'\')">'+
        '<div class="proj-row-main">'+
          '<div class="proj-row-t" title="'+esc(c.title||"")+'">'+esc(cleaned.text)+'</div>'+
          '<div class="proj-row-sub">'+
            agentCell(c.agent)+
            '<span class="mono">'+esc(shortSid(sid))+'</span>'+
            '<span>'+num(c.blocks_total)+' 块</span>'+
            '<span title="'+esc(c.created_at||"")+'">'+ftime(c.created_at)+'</span>'+
          '</div>'+
        '</div>'+
        '<div class="proj-row-act">'+
          '<button class="btn sm" data-sid="'+esc(sid)+'" data-title="'+esc(cleaned.text)+'" onclick="event.stopPropagation();quickExportConv(this)">导出</button>'+
          '<button class="btn sm" onclick="event.stopPropagation();snapDetail(\''+sid+'\',\''+esc(c.created_at||"")+'\')">详情</button>'+
        '</div>'+
      '</div>';
    }).join("");
    return '<details class="card proj-card" data-cwd="'+esc(cwdKey)+'"'+open+' ontoggle="onProjCardToggle(this)">'+
      '<summary class="proj-head">'+
        CHEV_IC+
        '<span class="proj-icon">'+(g.is_ungrouped?PROJ_CHAT_IC:PROJ_FOLD_IC)+'</span>'+
        '<span class="proj-meta">'+
          '<span class="proj-name">'+esc(g.name)+'</span>'+
          '<span class="proj-cwd" title="'+esc(cwdDisp)+'">'+esc(cwdDisp)+'</span>'+
        '</span>'+
        '<span class="chip">'+num(g.count)+' 条</span>'+
        '<span class="proj-time nowr" title="'+esc(g.latest||"")+'">'+frel(g.latest)+'</span>'+
        '<button class="btn sm primary" data-proj="'+esc(g.cwd)+'" data-name="'+esc(g.name)+'" data-count="'+num(g.count)+'" onclick="event.preventDefault();event.stopPropagation();quickExportProj(this)">导出整项 ('+num(g.count)+')</button>'+
      '</summary>'+
      '<div class="proj-body">'+rows+'</div>'+
    '</details>';
  }).join("");
  $("#snap-list").innerHTML=bar+'<div class="proj-tree">'+cards+'</div>';
}
function showTreeExportRes(html,isErr){
  const box=$("#tree-export-res");
  if(!box)return;
  box.hidden=false;
  box.className=isErr?"result-card err":"result-card";
  box.innerHTML=html;
}
function agyTargetLabel(t){
  if(t==="antigravity")return "Antigravity conversations 目录（需重启 Antigravity 生效）";
  if(t==="zcode")return "ZCode 主数据库（写回前自动备份，需重启 ZCode 生效）";
  if(t==="codex")return "Codex 会话目录";
  if(t==="claude")return "Claude Code projects 目录";
  return t+" 会话目录";
}
function agyApplyHint(t){
  if(t==="antigravity")return "已写入 conversations 目录，完全重启 Antigravity 生效";
  if(t==="zcode")return "已写回 ZCode 主库（已自动备份），完全重启 ZCode 生效";
  return "已写入目标原生会话目录";
}
async function agySyncPreview(){
  const btn=$("#agy-sync-btn"),old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="检查中…"}
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  const qs=["dedup=1","only_agent=zcode"];
  if(q)qs.push("project="+encodeURIComponent(q));
  try{
    const r=await api("/antigravity/sync?"+qs.join("&"));
    const tc=r.targets_count||0,ec=(r.empty_projects||[]).length;
    if(!tc&&!ec){
      showTreeExportRes('<div class="rt">'+GOOD+'Antigravity 已与 ZCode 完全一致</div>'+
        '<div class="hint">未发现需清理的已归档/已删项目对话、重复副本或空项目配置。</div>',false);
      toast("Antigravity 已是最新状态");
    }else{
      const sample=(r.targets||[]).slice(0,8).map(t=>'<li><code>'+esc(t.cid.slice(0,8))+'</code> · '+esc(t.cwd||"")+' · '+esc(t.title||"(无标题)")+' <span class="chip">'+esc(t.reason)+'</span></li>').join("");
      const emp=(r.empty_projects||[]).slice(0,8).map(e=>'<li>'+esc(e.name)+' <span class="mono">('+esc(e.cwd||e.id.slice(0,8))+')</span></li>').join("");
      showTreeExportRes(
        '<div class="rt">'+WARN+'待清理 Antigravity：'+num(tc)+' 条导入会话 · '+num(ec)+' 个空项目配置</div>'+
        '<div class="hint">含 '+num(r.db_files||0)+' 个 .db 文件、'+num(r.summary_ids||0)+' 条摘要索引、'+num(r.proto_ids||0)+' 条 proto 缓存'+
        (r.dup_count?' · '+num(r.dup_count)+' 条重复副本':'')+(r.agent_count?' · '+num(r.agent_count)+' 条非 ZCode 来源':'')+'。</div>'+
        (sample?'<ul class="immlist" style="margin-top:8px">'+sample+(tc>8?'<li class="hint">…其余 '+num(tc-8)+' 条省略</li>':'')+'</ul>':'')+
        (emp?'<div class="hint" style="margin-top:6px"><b>空项目注册：</b></div><ul class="immlist">'+emp+(ec>8?'<li class="hint">…其余 '+num(ec-8)+' 个省略</li>':'')+'</ul>':'')+
        (r.summary_check_error?'<div class="hint" style="margin-top:6px;color:var(--warn)">提示: 当前摘要库只读受限（'+esc(r.summary_check_error)+'）；执行前请先完全退出 Antigravity。</div>':'')+
        '<div class="exec-bar" style="margin-top:10px"><button class="btn danger sm" id="agy-sync-apply-btn" onclick="agySyncApply()">确认执行清理</button></div>',false);
    }
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'清理预览失败</div><div class="hint">'+esc(e.message)+'</div>',true);
    toast("清理预览失败："+e.message,1);
  }
  if(btn){btn.disabled=false;btn.textContent=old}
}
async function agySyncApply(){
  if(!await askConfirm("清理仅从 Antigravity 移除已归档/已删/重复副本及空项目配置；Context Vault 与 ZCode 原始记录完整保留。\n\n注意：若 Antigravity 客户端正在运行会锁住摘要库，建议在浏览器打开本页面并先完全退出 Antigravity。","确认清理 Antigravity","确认清理"))return;
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  const btn=$("#agy-sync-apply-btn");if(btn){btn.disabled=true;btn.textContent="清理中…"}
  try{
    const r=await api("/antigravity/sync",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({project:q||"",dedup:true,only_agent:"zcode",apply:true})});
    showTreeExportRes(
      '<div class="rt">'+GOOD+'Antigravity 清理完成</div>'+
      '<div class="hint">删除 '+num(r.removed_dbs||0)+' 个 .db 文件 · 清理 '+num(r.cleaned_summaries||0)+' 条摘要 · '+
      num(r.cleaned_proto||0)+' 条 proto 缓存 · 移除 '+num(r.removed_projects||0)+' 个空项目配置。重启 Antigravity 后生效。</div>',false);
    toast("Antigravity 清理完成");
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'清理被中止</div><div class="hint">'+esc(e.message)+'</div>'+
      '<div class="hint" style="margin-top:4px">请先在任务管理器/托盘完全退出 Antigravity（保留当前浏览器页），再点重试。</div>'+
      '<div style="margin-top:8px"><button class="btn danger sm" onclick="agySyncApply()">重试清理</button></div>',true);
    toast("清理失败："+e.message,1);
  }
}
async function agyBatchPreview(){
  const btn=$("#agy-batch-btn"),old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="扫描中…"}
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  const ag=treeAgent||"all";
  const qs=["agent="+encodeURIComponent(ag)];
  if(q)qs.push("project="+encodeURIComponent(q));
  if(treeArchived)qs.push("archived=1");
  try{
    const r=await api("/antigravity/batch?"+qs.join("&"));
    if(!r.planned){
      showTreeExportRes('<div class="rt">'+GOOD+'没有需要补齐导入的对话</div>'+
        '<div class="hint">已存在跳过 '+num(r.skipped_done||0)+' 条 · 指纹去重跳过 '+num(r.skipped_dup||0)+' 条（来源：'+esc(ag)+'）。</div>',false);
      toast("全部符合条件的对话均已在 Antigravity");
    }else{
      const ps=(r.projects||[]).slice(0,8).map(p=>'<li><span class="mono">'+esc(p.cwd)+'</span> — '+num(p.count)+' 条</li>').join("");
      showTreeExportRes(
        '<div class="rt">'+WARN+'待补齐导入 Antigravity：'+num(r.planned)+' 条主对话（涉及 '+num((r.projects||[]).length)+' 个项目）</div>'+
        '<div class="hint">已存在跳过 '+num(r.skipped_done||0)+' 条 · 指纹重复跳过 '+num(r.skipped_dup||0)+' 条 · 来源筛选：'+esc(ag)+'。</div>'+
        (ps?'<ul class="immlist" style="margin-top:8px">'+ps+'</ul>':'')+
        (r.summary_error?'<div class="hint" style="margin-top:6px;color:var(--warn)">摘要库只读提示：'+esc(r.summary_error)+'</div>':'')+
        '<div class="exec-bar" style="margin-top:10px"><button class="btn primary sm" id="agy-batch-apply-btn" onclick="agyBatchApply()">确认导入 Antigravity</button></div>',false);
    }
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'扫描失败</div><div class="hint">'+esc(e.message)+'</div>',true);
    toast("扫描失败："+e.message,1);
  }
  if(btn){btn.disabled=false;btn.textContent=old}
}
async function agyBatchApply(){
  if(!await askConfirm("将把待补齐的活跃主对话转换为 Antigravity 原生 .db 并登记摘要与项目目录（已存在对话自动跳过不重复导入）。\n\n注意：若 Antigravity 正在运行会锁住摘要库，请先退出 Antigravity 后执行。","确认批量导入 Antigravity","确认导入"))return;
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  const ag=treeAgent||"all";
  const btn=$("#agy-batch-apply-btn");if(btn){btn.disabled=true;btn.textContent="批量导入中…"}
  try{
    const r=await api("/antigravity/batch",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({agent:ag,project:q||"",archived:!!treeArchived,apply:true})});
    showTreeExportRes(
      '<div class="rt">'+GOOD+'批量补齐导入完成：成功 '+num(r.imported||0)+' 条'+(r.failed?'（失败 '+num(r.failed)+' 条）':'')+'</div>'+
      '<div class="hint">已跳过已有 '+num((r.skipped_done||0)+(r.skipped_dup||0))+' 条 · 完全重启 Antigravity 后在对应项目下显示。</div>',r.failed>0);
    toast("导入 Antigravity 完成：+"+num(r.imported||0));
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'批量导入失败</div><div class="hint">'+esc(e.message)+'</div>'+
      '<div style="margin-top:8px"><button class="btn primary sm" onclick="agyBatchApply()">重试导入</button></div>',true);
    toast("批量导入失败："+e.message,1);
  }
}
async function quickExportConv(btn){
  const sid=btn.dataset.sid,title=btn.dataset.title||sid;
  const tgt=($("#tree-tgt")&&$("#tree-tgt").value)||treeTarget||"antigravity";
  const canApply=tgt==="codex"||tgt==="claude"||tgt==="antigravity"||tgt==="zcode";
  const apply=canApply&&!!(($("#tree-apply")&&$("#tree-apply").checked)||treeApply);
  if(apply){
    if(!await askConfirm("源对话: "+title.slice(0,60)+"\n目标: "+agyTargetLabel(tgt)+"\n\n将写入目标工具的真实会话数据。", "确认写入真实会话目录", "确认写入"))return;
  }
  const old=btn.textContent;
  btn.disabled=true;btn.textContent="导出中…";
  try{
    const r=await api("/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:sid,target:tgt,apply:apply})});
    showTreeExportRes(
      '<div class="rt">'+GOOD+(r.applied?'已导入单条对话到真实会话目录「':'已导出单条对话「')+esc(title.slice(0,40))+'」→ '+esc(tgt)+'</div>'+
      '<div class="path-row"><span class="hint">文件:</span><span class="mono" style="word-break:break-all">'+esc(r.dest||"")+'</span>'+
      '<button class="btn sm" data-v="'+esc(r.dest||"")+'" onclick="copyData(this,\'已复制文件路径\')">复制路径</button></div>'+
      '<div class="hint">'+num(r.lines||0)+' 行'+(r.dropped?' · 丢弃未配对 tool_result ×'+num(r.dropped):'')+
      (r.applied?' · '+agyApplyHint(tgt):'')+'</div>',false);
    toast((r.applied?"已写入 → ":"已导出 → ")+tgt);
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'导出失败</div><div class="hint">'+esc(e.message)+'</div>',true);
    toast("导出失败："+e.message,1);
  }
  btn.disabled=false;btn.textContent=old;
}
async function quickExportProj(btn){
  const proj=btn.dataset.proj,name=btn.dataset.name||proj,cnt=btn.dataset.count||"全部";
  const tgt=($("#tree-tgt")&&$("#tree-tgt").value)||treeTarget||"antigravity";
  const canApply=tgt==="codex"||tgt==="claude"||tgt==="antigravity"||tgt==="zcode";
  const apply=canApply&&!!(($("#tree-apply")&&$("#tree-apply").checked)||treeApply);
  if(apply){
    if(!await askConfirm("项目: "+name+"（"+cnt+" 条主对话）\n目标: "+agyTargetLabel(tgt)+"\n\n将批量写入目标工具的真实会话目录。", "确认批量写入真实目录", "确认写入"))return;
  }
  const old=btn.textContent;
  btn.disabled=true;btn.textContent="批量导出中…";
  try{
    const body={project:proj,target:tgt,apply:apply};
    if(treeAgent)body.agent=treeAgent;
    if(treeArchived)body.archived=true;
    const r=await api("/migrate_project",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)});
    showTreeExportRes(
      '<div class="rt">'+GOOD+(r.applied?'已批量导入项目「':'已批量导出项目「')+esc(name)+'」共 '+num(r.count||0)+' 条主对话 → '+esc(tgt)+'</div>'+
      '<div class="path-row"><span class="hint">输出目录:</span><span class="mono" style="word-break:break-all">'+esc(r.dest_dir||"")+'</span>'+
      '<button class="btn sm" data-v="'+esc(r.dest_dir||"")+'" onclick="copyData(this,\'已复制输出目录\')">复制目录</button></div>'+
      '<div class="hint">共 '+num(r.total_lines||0)+' 行 · '+(r.applied?agyApplyHint(tgt):'产物已全部落入 vault/outbox/')+'</div>',false);
    toast((r.applied?"已导入项目「":"已导出项目「")+name+"」×"+num(r.count||0));
  }catch(e){
    showTreeExportRes('<div class="rt">'+ERRIC+'批量导出失败</div><div class="hint">'+esc(e.message)+'</div>',true);
    toast("批量导出失败："+e.message,1);
  }
  btn.disabled=false;btn.textContent=old;
}
async function renderSnaps(){
  $("#snap-list").innerHTML='<div class="skel" style="width:45%"></div><div class="skel" style="width:66%"></div>';
  try{
    const d=await snapsSeq(()=>api("/recent?limit="+LIM+"&offset="+(snapPage*LIM)+"&order="+(snapAsc?"asc":"desc")+
      "&leaves="+(snapLeaves?1:0)+(snapAgent?"&agent="+encodeURIComponent(snapAgent):"")));
    const rows=d.rows||[],total=d.total||0;
    renderSnapTable(rows,{recent:true,total,rows:rows.length,versionsTotal:d.versions_total});
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("快照列表加载失败："+e.message,"renderSnaps()");
    $("#snap-meta").textContent="";
  }
}
const sidBtn=sid=>"<button class='linklike sid' title=\""+esc(sid)+"（点击打开详情）\" onclick=\"event.stopPropagation();snapDetail('"+sid+"')\">"+esc(shortSid(sid))+"</button>";
const copyBtn=sid=>"<button class='btn sm' title='复制完整 SID' aria-label='复制 SID' data-v=\""+esc(sid)+"\" onclick=\"event.stopPropagation();copyData(this,'已复制完整 SID')\">复制 SID</button>";
function copyData(btn,msg){copyText(btn.dataset.v,msg||"已复制")}
function copySid(sid){copyText(sid,"已复制完整 SID: "+sid)}
function renderSnapTable(rows,opt){
  let meta="";
  if(opt.search){
    meta='筛选「'+esc(opt.search)+'」— <b class="num">'+num(opt.hits||0)+"</b> 条命中"+
      ' <button class="xbtn" onclick="loadSnaps()">清空筛选 ×</button>';
  }else if(snapLeaves){
    meta="最近入库 · 共 <b class='num'>"+num(opt.total||0)+"</b> 个独立会话 (每会话显示最新版本 · 库存 <b class='num'>"+
      num(opt.versionsTotal||opt.total||0)+"</b> 个快照版本)";
  }else{
    meta="最近入库 · 共 <b class='num'>"+num(opt.total||0)+"</b> 个快照版本 (含演进历史版本)";
  }
  $("#snap-meta").innerHTML=meta;
  const pager=(total,rowsN)=>{
    if(total<=LIM)return"";
    return '<div class="pager"><span>第 <b class="num">'+num(snapPage*LIM+1)+"–"+num(snapPage*LIM+rowsN)+
      "</b> 条 · 共 <b class='num'>"+num(total)+"</b> 条</span><span>"+
      '<button class="btn"'+(snapPage<=0?" disabled":"")+' onclick="loadSnaps('+(snapPage-1)+')">← 上一页</button> '+
      '<button class="btn"'+(snapPage*LIM+LIM>=total?" disabled":"")+' onclick="loadSnaps('+(snapPage+1)+')">下一页 →</button></span></div>'};
  let body;
  if(!rows.length){
    body=opt.search
      ?'<div class="empty">'+WARN+'<div>没有命中「'+esc(opt.search)+'」— 换个关键词试试：项目名、文件路径、任务描述都行</div>'+
       '<button class="btn" onclick="loadSnaps()">清空筛选，显示最近</button></div>'
      :'<div class="empty">vault 还是空的<br>点右上角 <b>↻ 立即同步</b> 或运行：<code>python tools/cv.py sync</code></div>';
  }else{
    const maxSc=opt.search?Math.max(...rows.map(x=>+(x.score)||1),1):1;
    body='<div class="cardbox">'+
      table(opt.search
        ?["快照","会话摘要","来源","相关度","时间","目录",""]
        :["快照","会话摘要","来源","块数","时间","目录",""],
        rows.map(r=>{
          const srcHtml=agentCell(r.agent)+(r.completeness&&r.completeness!=="full"
            ?' <span class="chip warn" title="转录不完整: '+esc(r.completeness)+'">!</span>':"");
          const verHtml=r.versions>1?'<span class="mini-tag" title="共 '+num(r.versions)+' 个版本">×'+num(r.versions)+'</span>':"";
          const scPct=opt.search?Math.max(12,Math.min(100,Math.round((+(r.score)||1)/maxSc*100))):0;
          const numCol=opt.search
            ?{t:'<span class="rel-cell" title="词项命中得分 '+num(Math.round(r.score||0))+'"><span class="relbar"><i style="width:'+scPct+'%"></i></span><span>'+num(Math.round(r.score||0))+'</span></span>',num:1}
            :{t:num(r.blocks),num:1};
          const tr=['<span class="sid-wrap">'+sidBtn(r.sid)+verHtml+'</span>',
            sumCell(r.first),srcHtml,numCol,
            '<span class="mono nowr" title="'+esc(r.created_at||"")+'">'+ftime(r.created_at)+"</span>",
            dirCell(r.cwd,22),copyBtn(r.sid)];
          tr.__tr='class="rowlink" onclick="snapDetail(\''+r.sid+'\',\''+(r.created_at||"")+'\')"';
          return tr}),"tbl-44 fixed-cols tbl-snap")+"</div>";
  }
  const pg=opt.search?"":pager(opt.total||0,opt.rows||0);
  $("#snap-list").innerHTML=pg+body+(opt.search?"":pg);
}

/* ================= 快照详情抽屉 ================= */
const drawerIsOpen=()=>$("#drawer").open;
function openDrawer(){
  const d=$("#drawer");
  if(d.open)return;
  d.showModal();
}
function closeDrawer(){
  const d=$("#drawer");
  if(d.open)d.close();
}

let confirmRes=null;
$("#confirm-dlg").addEventListener("close",()=>{
  const v=$("#confirm-dlg").returnValue;
  $("#confirm-dlg").returnValue="";
  $("#confirm-t").textContent="确认写入真实会话目录";
  $("#confirm-ok").textContent="确认写入";
  const r=confirmRes;confirmRes=null;
  if(r)r(v==="ok");
});
const askConfirm=(msg,title,okText)=>new Promise(res=>{confirmRes=res;
  $("#confirm-t").textContent=title||"确认写入真实会话目录";
  $("#confirm-ok").textContent=okText||"确认写入";
  $("#confirm-msg").textContent=msg;
  $("#confirm-dlg").showModal();});

let dwSeq=0,dwMsgSeq=0,dwCurSid="",dwView="meta",dwWide=false,dwMsgScope="live",dwMsgQuery="",dwMsgItems=[];
function toggleDrawerWide(force){
  dwWide=(typeof force==="boolean")?force:!dwWide;
  $("#drawer").classList.toggle("wide",dwWide);
  const b=$("#dw-wide-btn");if(b)b.classList.toggle("on",dwWide);
}
function setDrawerView(v){
  dwView=v==="msg"?"msg":"meta";
  const mOn=dwView==="meta";
  const bm=$("#dw-tab-meta"),bg=$("#dw-tab-msg");
  if(bm){bm.classList.toggle("on",mOn);bm.setAttribute("aria-selected",mOn?"true":"false")}
  if(bg){bg.classList.toggle("on",!mOn);bg.setAttribute("aria-selected",!mOn?"true":"false")}
  $("#dw-body").hidden=!mOn;
  $("#dw-msg-pane").hidden=mOn;
  if(!mOn){
    if(!dwWide)toggleDrawerWide(true);
    if(dwCurSid&&$("#dw-msg-pane").dataset.sid!==dwCurSid)loadDrawerMessages(true);
  }
}
function copyMsgIdx(idx){
  const it=dwMsgItems[idx];
  if(!it)return;
  copyText(String(it.text||""),"已复制该条内容");
}
function highlightPlain(escapedTxt,q){
  const term=String(q||"").trim();
  if(!term)return escapedTxt;
  const escTerm=esc(term).replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
  if(!escTerm)return escapedTxt;
  try{return escapedTxt.replace(new RegExp("("+escTerm+")","gi"),'<mark class="hl">$1</mark>')}
  catch(e){return escapedTxt}
}
function renderRichMsgText(raw,q){
  const s=String(raw||"");
  if(!s)return '<span class="hint">(空)</span>';
  const re=/```([A-Za-z0-9_+-]{0,24})?\n([\s\S]*?)```/g;
  let out="",last=0,m;
  const fmtProse=seg=>{
    if(!seg)return"";
    const parts=seg.split(/(`[^`\n]{1,120}`)/g);
    return parts.map(p=>{
      if(p.length>=2&&p.startsWith("`")&&p.endsWith("`")){
        return '<span class="inline-code">'+highlightPlain(esc(p.slice(1,-1)),q)+'</span>';
      }
      return highlightPlain(esc(p),q);
    }).join("");
  };
  while((m=re.exec(s))!==null){
    out+=fmtProse(s.slice(last,m.index));
    const lang=(m[1]||"code").trim();
    const code=m[2]||"";
    out+='<div class="code-blk"><div class="code-hd"><span>'+esc(lang)+'</span>'+
      '<button type="button" class="msg-cp" data-v="'+esc(code)+'" onclick="copyData(this,\'已复制代码块\')">复制代码</button></div>'+
      '<pre><code>'+highlightPlain(esc(code),q)+'</code></pre></div>';
    last=re.lastIndex;
  }
  out+=fmtProse(s.slice(last));
  return out;
}
function renderMsgItem(it,arrIdx){
  const bt=it.type||"text",tx=String(it.text||""),idxLbl="#"+(it.idx>=0?it.idx+1:"摘要");
  if(bt==="thinking"){
    const pv=stripMd(tx).replace(/\s+/g," ").trim().slice(0,72)||"(推理摘要)";
    return '<details class="msg-fold thinking"><summary><span class="mf-tag">THINKING</span>'+
      '<span class="mf-sum">'+esc(pv)+'</span><span class="msg-idx">'+idxLbl+'</span></summary>'+
      '<pre>'+highlightPlain(esc(tx),dwMsgQuery)+'</pre></details>';
  }
  if(bt==="tool_call"||bt==="tool_result"){
    const isErr=!!it.is_error;
    const tName=it.name?(" · "+esc(it.name)):"";
    const tag=bt==="tool_call"?("TOOL · "+esc(it.name||"call")):(isErr?("RESULT · ERR"+tName):("RESULT"+tName));
    const pv=tx.replace(/\s+/g," ").trim().slice(0,68)||"—";
    return '<details class="msg-fold'+(isErr?" err":"")+'"><summary><span class="mf-tag">'+tag+'</span>'+
      '<span class="mf-sum">'+esc(pv)+'</span><span class="msg-idx">'+num(it.full_chars||tx.length)+' 字</span></summary>'+
      '<pre>'+highlightPlain(esc(tx),dwMsgQuery)+'</pre></details>';
  }
  const roleCls=it.role==="user"?"user":(bt==="compaction"?"compaction":"assistant");
  const roleLbl=bt==="compaction"?"COMPACTION · 压缩摘要":(it.role==="user"?"USER":(bt==="agent_message"?"AGENT":"ASSISTANT"));
  return '<div class="msg-card '+roleCls+'">'+
    '<div class="msg-hd"><span class="msg-role">'+roleLbl+'</span><span class="msg-idx">'+idxLbl+'</span>'+
    (it.ts?'<span class="hint">'+esc(ftime(it.ts))+'</span>':'')+
    '<button type="button" class="msg-cp" onclick="copyMsgIdx('+arrIdx+')">复制</button></div>'+
    '<div class="msg-txt">'+renderRichMsgText(tx,dwMsgQuery)+'</div>'+
    (it.truncated?'<div class="msg-trunc">单块共 '+num(it.full_chars)+' 字符，已安全截取前 '+num(tx.length)+' 字符展示</div>':'')+
    '</div>';
}
async function loadDrawerMessages(reset){
  if(!dwCurSid)return;
  const my=++dwMsgSeq;
  const pane=$("#dw-msg-pane");
  if(reset){
    dwMsgItems=[];
    pane.dataset.sid=dwCurSid;
    pane.innerHTML='<div class="skel" style="width:65%;margin-top:12px"></div><div class="skel" style="width:85%"></div>';
  }
  const off=reset?0:dwMsgItems.length;
  const qs="id="+encodeURIComponent(dwCurSid)+"&scope="+encodeURIComponent(dwMsgScope)+
    "&offset="+off+"&limit=40"+(dwMsgQuery?"&q="+encodeURIComponent(dwMsgQuery):"");
  try{
    const r=await api("/messages?"+qs);
    if(my!==dwMsgSeq)return;
    const baseIdx=dwMsgItems.length;
    const added=r.items||[];
    dwMsgItems=dwMsgItems.concat(added);
    const cardsHtml=added.map((it,i)=>renderMsgItem(it,baseIdx+i)).join("");
    if(reset){
      pane.innerHTML=
        '<div class="msg-bar">'+
          '<div class="msg-seg" role="group" aria-label="上下文范围">'+
            '<button type="button" class="'+(dwMsgScope==="live"?"on":"")+'" onclick="dwMsgScope=\'live\';loadDrawerMessages(true)">活上下文 '+num(r.total_live||0)+'</button>'+
            '<button type="button" class="'+(dwMsgScope==="all"?"on":"")+'" onclick="dwMsgScope=\'all\';loadDrawerMessages(true)">全部历史 '+num(r.total_all||0)+'</button>'+
          '</div>'+
          '<div class="msg-search">'+
            '<input type="search" id="dw-msg-q" value="'+esc(dwMsgQuery)+'" placeholder="筛选本会话内容（回车）…" aria-label="筛选本会话内容" '+
              'onkeydown="if(event.key===\'Enter\'){event.preventDefault();dwMsgQuery=this.value.trim();loadDrawerMessages(true)}">'+
            (dwMsgQuery?'<button type="button" class="btn sm" onclick="dwMsgQuery=\'\';loadDrawerMessages(true)">清除</button>':'')+
          '</div>'+
          '<div class="msg-meta" id="dw-msg-meta">显示 <b class="num">'+num(dwMsgItems.length)+'</b> / <b class="num">'+num(r.total||0)+'</b> 块'+
            (dwMsgQuery?'（筛选「'+esc(dwMsgQuery)+'」）':'')+'</div>'+
        '</div>'+
        (dwMsgItems.length?'<div class="msg-list" id="dw-msg-list">'+cardsHtml+'</div>'
          :'<div class="empty"><div>当前范围内没有匹配的消息块</div></div>')+
        '<div class="msg-more" id="dw-msg-more">'+
          (r.has_more?'<button type="button" class="btn" onclick="loadDrawerMessages(false)">加载更多（剩余 '+num((r.total||0)-dwMsgItems.length)+' 块）</button>':'')+
        '</div>';
    }else{
      const lst=$("#dw-msg-list");if(lst)lst.insertAdjacentHTML("beforeend",cardsHtml);
      const mt=$("#dw-msg-meta");if(mt)mt.innerHTML='显示 <b class="num">'+num(dwMsgItems.length)+'</b> / <b class="num">'+num(r.total||0)+'</b> 块'+(dwMsgQuery?'（筛选「'+esc(dwMsgQuery)+'」）':'');
      const mr=$("#dw-msg-more");if(mr)mr.innerHTML=r.has_more?'<button type="button" class="btn" onclick="loadDrawerMessages(false)">加载更多（剩余 '+num((r.total||0)-dwMsgItems.length)+' 块）</button>':'';
    }
  }catch(e){
    if(my!==dwMsgSeq)return;
    pane.innerHTML=errEmpty("对话正文加载失败："+e.message,"loadDrawerMessages(true)");
  }
}
async function toMig(sid){
  closeDrawer();setTab("mig");
  const ms=$("#mig-search");if(ms)ms.value="";
  const clr=$("#mig-filter-clr");if(clr)clr.hidden=true;
  const sel=$("#mig-sid");
  const pick=()=>{const o=[...sel.options].find(x=>x.value===sid);if(o){sel.value=sid;onMigSource();return true}return false};
  if(sel.dataset.filled==="1"&&pick())return;
  if(!sel.dataset.filled){delete sel.dataset.filled;await fillMig().catch(()=>{});if(pick())return}
  /* 老快照不在最近 200 条里: 直接拉 /snapshot 注入选项并立即预览, 不再失败阻断 */
  try{
    const d=await api("/snapshot?id="+encodeURIComponent(sid));
    const c=d.capture||{};
    const lbl=esc(shortSid(sid))+" · "+esc(c.agent||"?")+" · "+esc(truncPath(c.cwd||"",24))+"（详情带入）";
    const optHtml='<option value="'+esc(sid)+'">'+lbl+'</option>';
    sel.innerHTML=optHtml+(sel.innerHTML||"");
    sel.value=sid;
    onMigSource();
  }catch(e){toast("未能载入该快照: "+String(e&&e.message||e),1)}
}
async function forkCurrentSnap(sid){
  const btn=$("#dw-fork-btn"),old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="分叉中…"}
  try{
    const r=await api("/fork",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:sid})});
    await checkHealth();
    loadOv();refreshSnaps();
    toast((r.existed?"该分叉已存在：":"已创建不可变分叉：")+r.fork_sid);
    if(r.fork_sid)snapDetail(r.fork_sid);
  }catch(e){
    toast("分叉失败："+e.message,1);
    if(btn){btn.disabled=false;btn.textContent=old}
  }
}
async function snapDetail(sid,ts){
  const my=++dwSeq;
  dwCurSid=sid;
  $("#dw-msg-pane").dataset.sid="";
  openDrawer();
  $("#dw-sub").textContent=sid;
  if(dwView==="msg")loadDrawerMessages(true);
  $("#dw-body").innerHTML='<div class="skel" style="width:55%"></div><div class="skel" style="width:82%"></div><div class="skel" style="width:64%"></div>';
  try{
    const [d,p]=await Promise.all([api("/snapshot?id="+sid),api("/preview?id="+sid).catch(()=>null)]);
    if(my!==dwSeq)return;
    const c=d.capture||{},s=d.stats||{},lin=d.lineage||{};
    const imm=[];
    if(p){
      if(p.no_assistant)imm.push("assistant 文本缺失（源转录不含）— 目标会话只有 user / tool 视角");
      if(p.thinking)imm.push("thinking ×"+p.thinking+" — 仅明文摘要可迁移，加密推理不可移植");
      if(p.agent_messages)imm.push("agent_message ×"+p.agent_messages+" — 降级为带标注的文本");
      if(p.dropped_source)imm.push("源文件被丢弃行 ×"+p.dropped_source+"（developer 注入 / 无移植语义）");
    }
    const dist=Object.entries(d.block_dist||{}).map(([k,v])=>"<span class='chip'>"+esc(k)+" ×"+num(v)+"</span>").join(" ");
    $("#dw-body").innerHTML=
      "<div class='dw-chips'>"+
      (c.agent?"<span class='chip'>"+esc(c.agent)+"</span>":"")+
      (c.source_format?"<span class='chip'>"+esc(c.source_format)+"</span>":"")+
      (c.completeness&&c.completeness!=="full"?"<span class='chip warn'>"+esc(c.completeness)+"</span>":"<span class='chip ok'>完整</span>")+
      (lin.fork_of?"<span class='chip ok'>分叉节点</span>":"")+
      "<span class='hint'>"+ftime(ts||"")+"</span></div>"+
      "<div class='kv' style='margin-top:12px'>"+
      kv("项目目录",esc(c.cwd||"—"))+
      (lin.fork_of?kv("分叉来源",sidBtn(lin.fork_of)+(c.fork_note?(" · "+esc(c.fork_note)):"")):"")+
      (p?kv("活上下文",num(p.live_blocks)+" 块 · ~"+num(p.live_tokens_est)+" tokens"+(p.compactions?"（"+p.compactions+" 次压缩边界之后）":"")):"")+
      (p?kv("历史累计输入",num(p.history_tokens_in)+" tokens（源口径"+(c.source_format&&c.source_format!=="codex-rollout"?"，未含 cache 读":"，含 cache 读")+"）"):"")+
      kv("块总数",num(s.blocks_total)+" 块"+(s.blocks_dropped?" · 丢弃 "+num(s.blocks_dropped):""))+
      "</div>"+
      "<h3 class='sec'>块构成</h3><div class='dw-chips'>"+(dist||"<span class='hint'>—</span>")+"</div>"+
      (p?"<h3 class='sec'>可迁移性限制</h3><ul class='immlist'>"+
        (imm.length?imm.map(i=>"<li>"+WARN+"<span>"+i+"</span></li>").join(""):"<li class='good'>"+GOOD+"无可迁移性损失</li>")+"</ul>":"")+
      "<h3 class='sec'>会话时间线</h3><div id='dw-tl'><div class='skel' style='width:70%'></div></div>"+
      "<div class='dw-actions'>"+
      "<button class='btn' onclick=\"setDrawerView('msg')\">阅读对话正文</button>"+
      "<button class='btn primary' onclick=\"toMig('"+sid+"')\">迁移此快照 →</button>"+
      "<button class='btn' id='dw-fork-btn' onclick=\"forkCurrentSnap('"+sid+"')\" title='基于此不可变快照创建新分支节点'>分叉节点</button>"+
      "<button class='btn' onclick=\"copySid('"+sid+"')\">复制 SID</button></div>";
    (async()=>{
      try{
        const tl=await api("/timeline?session="+encodeURIComponent(c.session_ref||"")+"&agent="+encodeURIComponent(c.agent||""));
        if(my!==dwSeq)return;
        const rows=(tl.days||[]).map(day=>"<div class='tl-day'><span class='tl-d'>"+esc(day.day)+"</span>"+
          day.versions.map(v=>"<div class='tl-row"+(v.sid===sid?" cur":"")+"'>"+
            "<span class='mono'>"+v.time+"</span> · "+num(v.blocks)+" 块 · 入 "+num(v.tokens_in)+
            (v.leaf?" <span class='chip ok'>最新</span>":"")+
            " <button class='linklike' onclick=\"snapDetail('"+v.sid+"','"+esc(day.day+"T"+v.time)+"')\">"+esc(shortSid(v.sid))+"</button></div>").join("")+
          "</div>").join("");
        $("#dw-tl").innerHTML=rows||"<span class='hint'>—</span>";
      }catch(e){if(my!==dwSeq)return;$("#dw-tl").innerHTML="<span class='hint'>时间线加载失败: "+esc(String(e&&e.message||e))+"</span>"}
    })();
  }catch(e){if(my!==dwSeq)return;$("#dw-body").innerHTML=errEmpty("详情加载失败："+e.message,"snapDetail('"+sid+"')")}
}
const kv=(k,v)=>"<b>"+k+"</b><span>"+(v==null?"—":v)+"</span>";

