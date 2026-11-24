/* ================= 跨会话长期记忆库 ================= */
let memKind="all",memComposeOpen=false;
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
