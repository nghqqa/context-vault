/* ================= 跨会话长期记忆库 ================= */
let memKind="all",memComposeOpen=false,memRulesOpen=false,memRulesData=null,memProjects=[],memRulesProj="",memRulesTgt="agents",memRulesBudget=2000;
function setMemKind(k){
  memKind=k||"all";
  loadMemoryView();
}
function toggleMemRules(){
  memRulesOpen=!memRulesOpen;
  if(memRulesOpen)memComposeOpen=false;
  return loadMemoryView();
}
function _ruleTargetFile(t){
  if(t==="claude")return"CLAUDE.md";
  if(t==="cursor")return".cursorrules";
  return"AGENTS.md";
}
function _renderMemRulesOut(){
  const box=$("#mr-out");
  if(!box||!memRulesData)return;
  const d=memRulesData;
  const dest=d.out_path||_ruleTargetFile(d.target);
  const stChip=d.applied?'<span class="chip ok">已写入</span>':(d.changed?'<span class="chip warn">待写入预览</span>':'<span class="chip">已最新</span>');
  box.innerHTML=
    '<div class="mr-meta">'+stChip+
      '<span class="chip">项目 '+esc(d.project_label||d.project||"?")+'</span>'+
      '<span class="chip">纳入 '+num(d.included||0)+' / '+num(d.total_matched||0)+' 条</span>'+
      '<span class="chip mono">'+num(d.tokens_est||0)+' / '+num(d.budget_tokens||2000)+' tok</span>'+
      '<span class="mono" style="color:var(--ink-3);overflow-wrap:anywhere">'+esc(dest)+'</span>'+
    '</div>'+
    (d.backup_path?'<div class="hint" style="margin:4px 0">已保留原文件备份：<code>'+esc(d.backup_path)+'</code></div>':"")+
    '<pre class="mr-pre">'+esc(d.block_markdown||"")+'</pre>';
}
function _readMemRulesParams(){
  const pEl=$("#mr-proj");
  const p=pEl?(pEl.value||"").trim():(memRulesProj||(memProjects[0]&&memProjects[0].project)||"");
  const t=($("#mr-tgt")&&$("#mr-tgt").value)||memRulesTgt||"agents";
  const b=parseInt(($("#mr-budget")&&$("#mr-budget").value)||String(memRulesBudget||2000),10)||2000;
  memRulesProj=p;memRulesTgt=t;memRulesBudget=b;
  return{project:p,target:t,budget:b};
}
async function previewCompiledRules(){
  const cfg=_readMemRulesParams();
  if(!cfg.project){toast("请先填写或选择项目名称",1);return}
  const box=$("#mr-out");
  if(box)box.innerHTML='<div class="hint">正在编译项目规则…</div>';
  try{
    const qs="project="+encodeURIComponent(cfg.project)+"&target="+encodeURIComponent(cfg.target)+"&budget="+cfg.budget;
    memRulesData=await api("/memory/compile_rules?"+qs);
    _renderMemRulesOut();
  }catch(e){
    memRulesData=null;
    if(box)box.innerHTML='<div class="hint" style="color:var(--err)">编译失败：'+esc(e.message)+'</div>';
    toast("规则编译失败："+e.message,1);
  }
}
function copyCompiledRules(){
  if(!memRulesData||!memRulesData.block_markdown){toast("请先点击「预览规则」生成内容",1);return}
  copyText(memRulesData.block_markdown,"已复制受哨兵保护的规则 Markdown");
}
async function applyCompiledRules(){
  const cfg=_readMemRulesParams();
  if(!cfg.project){toast("请先填写或选择项目名称",1);return}
  try{
    const qs="project="+encodeURIComponent(cfg.project)+"&target="+encodeURIComponent(cfg.target)+"&budget="+cfg.budget;
    const pre=await api("/memory/compile_rules?"+qs);
    memRulesData=pre;
    _renderMemRulesOut();
    if(!pre.out_path){toast("未能从历史快照定位项目本地目录，请使用 CLI --out 指定目标路径或先复制 Markdown",1);return}
    if(!pre.changed){toast("目标规则文件已处于最新状态，无需重复写入");return}
    const bakMsg=pre.exists?"（修改前将自动生成 .bak-cv 备份，且保留哨兵外的手写规则）":"（将创建新规则文件）";
    const ok=await askConfirm("将把 "+num(pre.included||0)+" 条项目记忆写入：\n"+pre.out_path+"\n\n"+bakMsg,"确认写入项目规则文件","写入规则文件");
    if(!ok)return;
    const res=await api("/memory/compile_rules",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({project:cfg.project,target:cfg.target,budget_tokens:cfg.budget,apply:true})});
    memRulesData=res;
    _renderMemRulesOut();
    toast("已写入 "+_ruleTargetFile(res.target)+"（纳入 "+num(res.included||0)+" 条记忆）");
  }catch(e){toast("写入规则失败："+e.message,1)}
}
async function extractMemoriesNow(force){
  const btn=$("#mem-ext-btn"),old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="提取中…"}
  try{
    const r=await api("/memory/extract",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:!!force})});
    memRulesData=null;
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
    memRulesData=null;
    toast("已合并重复摘录 −"+num(r.removed||0)+" 条 · 剩余 "+num(r.total||0)+" 条");
    loadOv();
    if(snapMemMode)await loadMemoryView();
  }catch(e){
    toast("去重失败："+e.message,1);
  }
}
function toggleMemCompose(){
  memComposeOpen=!memComposeOpen;
  if(memComposeOpen)memRulesOpen=false;
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
    memComposeOpen=false;memRulesData=null;
    toast("已写入长期记忆库");
    loadOv();
    await loadMemoryView();
  }catch(e){toast("写入失败："+e.message,1)}
}
async function toggleMemPin(id,nextPin){
  try{
    await api("/memory/pin",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,pinned:!!nextPin})});
    memRulesData=null;
    toast(nextPin?"已置顶该记忆":"已取消置顶");
    loadOv();
    await loadMemoryView();
  }catch(e){toast("操作失败："+e.message,1)}
}
async function deleteMemItem(id){
  try{
    await api("/memory/delete",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id})});
    memRulesData=null;
    toast("已删除记忆条目 #"+id);
    loadOv();
    await loadMemoryView();
  }catch(e){toast("删除失败："+e.message,1)}
}
async function loadMemoryView(){
  const _hadMrProj=memRulesOpen&&!!$("#mr-proj");
  if(_hadMrProj)_readMemRulesParams();
  snapMemMode=true;snapTreeMode=false;updateSnapTreeBtn();
  const q=($("#snap-q")&&$("#snap-q").value||"").trim();
  $("#snap-list").innerHTML='<div class="skel" style="width:50%"></div><div class="skel" style="width:72%"></div>';
  try{
    const qs=["limit=80","q="+encodeURIComponent(q)];
    if(memKind&&memKind!=="all")qs.push("kind="+encodeURIComponent(memKind));
    const d=await snapsSeq(()=>api("/memory?"+qs.join("&")));
    const arr=(d&&d.memories)||[];
    memProjects=(d&&d.projects)||[];
    if(!_hadMrProj&&!memRulesProj&&memProjects.length)memRulesProj=memProjects[0].project||"";
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
        '<button class="btn sm'+(memRulesOpen?' on':'')+'" onclick="toggleMemRules()" title="把项目活跃记忆按预算编译为 AGENTS.md / CLAUDE.md / .cursorrules">编译项目规则</button>'+
        '<button class="btn sm primary" id="mem-ext-btn" onclick="extractMemoriesNow(false)">增量提取记忆</button>'+
        '<button class="btn sm" onclick="dedupMemoriesNow()" title="合并 (类型, 内容) 完全相同的跨快照重复摘录，保留最早一条">去重合并</button>'+
        '<button class="btn sm" onclick="extractMemoriesNow(true)" title="忽略已提取水位，重新扫描全库快照提取记忆">全量重提</button>'+
      '</div></div>';
    const dlOpts=memProjects.map(p=>'<option value="'+esc(p.project)+'">'+esc(p.project)+' ('+num(p.count)+')</option>').join("");
    const rulesHtml=memRulesOpen?(
      '<div class="mem-rules">'+
        '<div class="mem-rules-row">'+
          '<input type="text" id="mr-proj" list="mr-proj-list" value="'+esc(memRulesProj)+'" placeholder="输入或选择项目名 (如 context-vault)" aria-label="编译目标项目">'+
          '<datalist id="mr-proj-list">'+dlOpts+'</datalist>'+
          '<select id="mr-tgt" aria-label="目标规则文件">'+
            '<option value="agents"'+(memRulesTgt==="agents"?' selected':'')+'>AGENTS.md</option>'+
            '<option value="claude"'+(memRulesTgt==="claude"?' selected':'')+'>CLAUDE.md</option>'+
            '<option value="cursor"'+(memRulesTgt==="cursor"?' selected':'')+'>.cursorrules</option>'+
          '</select>'+
          '<select id="mr-budget" aria-label="Token 预算">'+
            '<option value="1000"'+(memRulesBudget===1000?' selected':'')+'>1,000 tok</option>'+
            '<option value="2000"'+(memRulesBudget===2000?' selected':'')+'>2,000 tok</option>'+
            '<option value="4000"'+(memRulesBudget===4000?' selected':'')+'>4,000 tok</option>'+
          '</select>'+
          '<button type="button" class="btn sm primary" onclick="previewCompiledRules()">预览规则</button>'+
          '<button type="button" class="btn sm" onclick="copyCompiledRules()">复制 Markdown</button>'+
          '<button type="button" class="btn sm" onclick="applyCompiledRules()">写入项目文件</button>'+
          '<button type="button" class="btn sm" onclick="toggleMemRules()">收起</button>'+
        '</div>'+
        '<div id="mr-out"><div class="hint">按红线(constraint) → 置顶 → 决策 → 踩坑 → 偏好优先级装填，只维护哨兵标记内区块，保留你手写的项目说明。</div></div>'+
      '</div>'
    ):"";
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
      $("#snap-list").innerHTML=dock+composeHtml+rulesHtml+'<div class="empty">'+WARN+
        '<div>'+(q?('没有匹配「'+esc(q)+'」的跨会话记忆'):'当前分类下暂无记忆条目')+'</div>'+
        '<div class="hint">记忆由规则引擎从历史对话自动提取，也可点击「+ 手动记一条」录入关键红线，永不修改原快照。</div>'+
        '<button class="btn primary" onclick="extractMemoriesNow(false)">立即从全库快照提取</button></div>';
      if(memRulesOpen&&memRulesData)_renderMemRulesOut();
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
            (validSid?('来源 '+sidBtn(sid)):(sid==="mcp"?"Agent 录入":(sid==="manual"?"手动录入":esc(sid))))+
            ops+
          '</span>'+
        '</div>'+
        '<div class="mem-row-txt">'+highlightPlain(esc(cleanTxt),q)+'</div>'+
      '</div>';
    }).join("");
    $("#snap-list").innerHTML=dock+composeHtml+rulesHtml+'<div class="mem-list">'+listHtml+'</div>';
    if(memRulesOpen&&memRulesData)_renderMemRulesOut();
  }catch(e){
    if(e&&e.stale)return;
    $("#snap-list").innerHTML=errEmpty("记忆库加载失败："+e.message,"loadMemoryView()");
    $("#snap-meta").textContent="";
  }
}
