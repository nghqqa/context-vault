/* ================= 快照详情抽屉与对话正文阅读器 ================= */
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
