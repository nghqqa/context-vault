/* ================= 主题与基础工具 ================= */
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
function applyThemeMeta(v){
  const val=(v==="light"||v==="dark")?v:"light dark";
  try{const m=document.querySelector('meta[name="color-scheme"]');if(m)m.content=val}catch(e){}
  try{if(document.documentElement&&document.documentElement.dataset){val==="light dark"?delete document.documentElement.dataset.theme:(document.documentElement.dataset.theme=val)}}catch(e){}
  $$(".theme-sw button").forEach(b=>b.setAttribute("aria-pressed",(b.dataset.theme===val)?"true":"false"));
}
function setTheme(v){
  const val=(v==="light"||v==="dark")?v:"light dark";
  try{if(typeof localStorage!=="undefined"){val==="light dark"?localStorage.removeItem("cv-theme"):localStorage.setItem("cv-theme",val)}}catch(e){}
  applyThemeMeta(val);
}
(()=>{
  let saved="light dark";
  try{if(typeof localStorage!=="undefined"){const s=localStorage.getItem("cv-theme");if(s==="light"||s==="dark")saved=s}}catch(e){}
  applyThemeMeta(saved);
  try{window.addEventListener("storage",e=>{if(e&&e.key==="cv-theme")applyThemeMeta(e.newValue||"light dark")})}catch(e){}
})();

const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=v=>(+v||0).toLocaleString("en-US");
const bn=v=>{v=+v||0;return v>=1e9?(v/1e9).toFixed(2)+"B":v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e4?(v/1e3).toFixed(1)+"k":num(v)};
const numT=v=>'<span class="num" title="'+num(v)+'">'+bn(v)+"</span>";
const usd=v=>v==null?"—":"$"+(+v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const ftime=s=>{if(!s)return"—";const d=new Date(s);return isNaN(d)?String(s).slice(0,19)
  :d.toLocaleString("zh-CN",{year:"2-digit",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})};
const frel=s=>{if(!s)return"";const d=new Date(s);if(isNaN(d))return String(s).slice(0,19);
  const m=Math.round((Date.now()-d.getTime())/6e4);
  return m<1?"刚刚":m<60?m+" 分钟前":m<1440?Math.round(m/60)+" 小时前":Math.round(m/1440)+" 天前"};
const shortSid=s=>{s=String(s||"");return s.length>22?s.slice(0,14)+"…"+s.slice(-5):s};
const truncPath=(p,n)=>{
  p=String(p||"—");
  const lim=n||32;
  if(p.length<=lim)return p;
  const parts=p.split(/[\\/]/).filter(Boolean);
  if(parts.length>=2){
    const last=parts[parts.length-1],head=parts[0];
    if(head.length+last.length+3<=lim)return head+"\\…\\"+last;
    if(last.length+2<=lim)return "…\\"+last;
    return "…\\"+last.slice(-(lim-2));
  }
  return p.slice(0,lim-1)+"…";
};

let toastTimer;
function toast(m,err){const t=$("#toast");t.textContent=m;t.classList.toggle("err",!!err);
  if(!t.matches(":popover-open"))t.showPopover();
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>{if(t.matches(":popover-open"))t.hidePopover()},5200)}

class ApiError extends Error{constructor(msg,code){super(msg);this.code=code}}
let bootRetry=null;
function scheduleBoot(){if(bootRetry)return;bootRetry=setTimeout(()=>{bootRetry=null;boot()},4000)}
function markSvcBad(){
  const s=$("#status");
  if(!s.classList.contains("err")){s.classList.add("err");
    s.innerHTML="<span class='pulse'></span>服务异常 · 4s 后重试";}
  scheduleBoot();
}
async function api(p,opt){
  let r;
  try{r=await fetch(p,opt)}catch(e){markSvcBad();throw e}
  if(!r.ok){
    const j=await r.json().catch(()=>({}));
    throw new ApiError(j.error||("HTTP "+r.status),r.status);
  }
  return r.json();
}
function seqGuard(){
  const box={seq:0};
  return async fn=>{
    const my=++box.seq;
    const r=await fn();
    if(my!==box.seq)throw{stale:true};
    return r;
  };
}

const WARN='<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M8 2L15 14H1L8 2Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" style="color:var(--warn)"/><path d="M8 6.5v3.5M8 12v.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" style="color:var(--warn)"/></svg>';
const GOOD='<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.3" stroke="currentColor" stroke-width="1.5" style="color:var(--ok)"/><path d="M5.4 8.2l1.8 1.8 3.4-3.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" style="color:var(--ok)"/></svg>';
const ERRIC='<svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.3" stroke="currentColor" stroke-width="1.5" style="color:var(--err)"/><path d="M8 4.8v4M8 11.2v.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" style="color:var(--err)"/></svg>';
const errEmpty=(msg,retry)=>'<div class="empty">'+ERRIC+'<div>'+esc(msg)+"</div>"+
  (retry?'<button class="btn sm" onclick="'+retry+'">重试</button>':"")+"</div>";

const TBL_COLS={
  "tbl-ov":'<colgroup><col class="c-src"><col><col class="c-dir"><col class="c-time"></colgroup>',
  "tbl-snap":'<colgroup><col class="c-sid"><col><col class="c-src"><col class="c-num"><col class="c-time"><col class="c-dir"><col class="c-act"></colgroup>'
};
const cell=c=>(c&&typeof c==="object")?c:{t:c};
const table=(heads,rows,cls)=>{if(!rows||!rows.length)return"";
  const lbls=heads.map(h=>String(h).replace(/<[^>]*>/g,"").trim());
  const hs=heads.map((h,i)=>{const c=cell(rows[0][i]);return '<th scope="col"'+(c.num?' class="num"':"")+">"+h+"</th>"}).join("");
  const bs=rows.map(r=>{const open="<tr"+(r.__tr?" "+r.__tr:"")+">";
    return open+r.map((c0,i)=>{const c=cell(c0);return "<td data-label=\""+esc(lbls[i]||"")+"\""+(c.num?' class="num"':"")+">"+c.t+"</td>"}).join("")+"</tr>"}).join("");
  const ckey=(cls||"").split(/\s+/).find(k=>k in TBL_COLS);
  const cg=ckey?TBL_COLS[ckey]:"";
  return '<div class="tbl-wrap"><table'+(cls?' class="'+cls+'"':"")+">"+cg+
    "<thead><tr>"+hs+"</tr></thead><tbody>"+bs+"</tbody></table></div>"};

/* 清洗会话摘要 & 记忆正文的 Markdown/URL/路径残留 */
const stripMd=s=>String(s||"")
  .replace(/```[\s\S]*?```/g," ")
  .replace(/`([^`\n]+)`/g,"$1")
  .replace(/\*\*([^*\n]+)\*\*/g,"$1")
  .replace(/\*\*/g,"")
  .replace(/__([^_\n]+)__/g,"$1")
  .replace(/^[#>*\s]+/gm,"");
const cleanSummary=s=>{let t=stripMd(s);const tags=[];
  t=t.replace(/https?:\/\/\S+/g," ");
  t=t.replace(/\[([^\]\n]{1,24})\]\([^)\n]{0,60}\)?/g,(m,g)=>{tags.push(g);return " "});
  t=t.replace(/\(([A-Za-z]:\\?[^)\n]{0,36})\)?/g,(m,g)=>{tags.push(g);return " "});
  t=t.replace(/\b[A-Za-z]:\\[^\s",;)]+/g,(m)=>{tags.push(m);return " "});
  t=t.replace(/\s+/g," ").trim();
  return {text:t||"(无文本)",tags:[...new Set(tags)].slice(0,2)}};
const sumCell=s=>{const c=cleanSummary(s);
  return '<span class="sum-line" title="'+esc(stripMd(s)||"")+'"><span class="sum-main">'+esc(c.text)+"</span>"+
    c.tags.map(t=>'<span class="mini-tag">'+esc(truncPath(t,18))+"</span>").join("")+"</span>"};
const dirCell=(p,max)=>'<span class="dir" title="'+esc(p||"—")+'"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/></svg><span>'+esc(truncPath(p||"—",max||26))+"</span></span>";
const agentCell=a=>"<span class='chip'>"+esc(a||"?")+"</span>";
const memKindMeta=k=>{
  if(k==="preference")return{t:"偏好",cls:"ok"};
  if(k==="decision")return{t:"决策",cls:"warn"};
  if(k==="pitfall")return{t:"踩坑",cls:"err"};
  if(k==="constraint")return{t:"红线",cls:"warn"};
  if(k==="summary")return{t:"摘要",cls:""};
  if(k==="fact")return{t:"事实",cls:""};
  return{t:k||"记忆",cls:""};
};
const copyText=async(t,msg)=>{
  try{await navigator.clipboard.writeText(t)}
  catch(e){toast("复制失败: "+String(e&&e.message||e),1);return}
  toast(msg||"已复制")};

/* ================= 分区导航 + 快捷键 ================= */
const TABS=["ov","snap","mig","ops"];
const TAB_TITLE={ov:"概览",snap:"快照资产",mig:"迁移工作台",ops:"设置与运维"};
let curTab="ov",booted=false,snapsLoaded=false;
function setTab(t){
  if(!TABS.includes(t))t="ov";
  curTab=t;
  $$("nav.side-nav button").forEach(x=>{const on=x.dataset.t===t;
    x.classList.toggle("on",on);on?x.setAttribute("aria-current","page"):x.removeAttribute("aria-current")});
  TABS.forEach(k=>{$("#t-"+k).style.display=k===t?"":"none"});
  $("#sec-title").textContent=TAB_TITLE[t];
  const sc=$(".content");if(sc)sc.scrollTop=0;
  if(window.scrollTo)window.scrollTo(0,0);
  if(t==="snap"&&booted&&!snapsLoaded){snapsLoaded=true;refreshSnaps();}
  if(t==="mig"&&!$("#mig-sid").dataset.filled)fillMig();
  if(t==="mig"&&typeof migMode!=="undefined"&&migMode==="skills"&&!skData)loadSkills({refresh:true});
  if(t==="ops"&&!$("#ops-mcp-box").dataset.loaded)loadOps(false);
  if(window.history)history.replaceState(null,"","#"+t);
}
$$("nav.side-nav button").forEach(b=>b.onclick=()=>setTab(b.dataset.t));
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&String(e.key||"").toLowerCase()==="k"){
    e.preventDefault();openCmdk();return;
  }
  const t=e.target,tag=t&&t.tagName;
  if(tag&&/^(INPUT|SELECT|TEXTAREA)$/.test(tag)){
    if(e.key==="Escape"&&t.blur)t.blur();
    return;
  }
  if(drawerIsOpen()||cmdkIsOpen())return;
  if(e.key==="/"){e.preventDefault();const g=$("#g-q");if(g&&g.focus)g.focus();return}
  if(e.key==="?"){e.preventDefault();openHelp();return}
  const n=+e.key;
  if(n>=1&&n<=TABS.length){setTab(TABS[n-1]);e.preventDefault()}
});
function openHelp(){
  const d=$("#help-dlg");if(!d)return;
  if(d.open){closeHelp();return}
  try{if(d.showModal)d.showModal()}catch(e){}
}
function closeHelp(){const d=$("#help-dlg");if(d&&d.open&&d.close)d.close()}
let cmdkTimer=null,cmdkSeq=0,cmdkItems=[],cmdkIdx=0;
const CMDK_QUICK=[
  {label:"立即扫描同步全端会话",sub:"Sync",fn:()=>syncVaultNow()},
  {label:"查看全部快照与项目树",sub:"快捷键 2",fn:()=>{setTab("snap");snapTreeToggle(true)}},
  {label:"打开跨会话长期记忆库",sub:"Memory",fn:()=>openMemoryView()},
  {label:"前往整端批量会话迁移",sub:"快捷键 3",fn:()=>{setTab("mig");if(typeof setMigScope==="function")setMigScope("batch")}},
  {label:"前往 Skill 技能跨端迁移",sub:"Skills",fn:()=>{setTab("mig");if(typeof setMigMode==="function")setMigMode("skills")}},
  {label:"管理 MCP 接入与备份体检",sub:"快捷键 4",fn:()=>setTab("ops")},
  {label:"打开独立 Token 经济面板",sub:"/dashboard",fn:()=>{try{window.open("/dashboard","_blank")}catch(e){}}},
  {label:"查看快捷键与术语说明",sub:"?",fn:()=>openHelp()}
];
function cmdkIsOpen(){const d=$("#cmdk");return !!(d&&d.open)}
function openCmdk(){
  const d=$("#cmdk");if(!d)return;
  try{if(!d.open&&d.showModal)d.showModal()}catch(e){}
  const inp=$("#cmdk-q");
  if(inp){inp.value=($("#g-q")&&$("#g-q").value)||"";if(inp.focus)inp.focus();if(inp.select)inp.select()}
  runCmdkSearch();
}
function closeCmdk(){const d=$("#cmdk");if(d&&d.open&&d.close)d.close()}
function onCmdkInput(){
  clearTimeout(cmdkTimer);
  cmdkTimer=setTimeout(()=>runCmdkSearch(),150);
}
function _syncCmdkActive(){
  const inp=$("#cmdk-q");
  $$(".cmdk-item").forEach((el,i)=>{
    const on=(i===cmdkIdx);
    el.classList.toggle("on",on);
    el.setAttribute("aria-selected",on?"true":"false");
    if(on){
      if(inp)inp.setAttribute("aria-activedescendant",el.id||("cmdk-opt-"+i));
      if(el.scrollIntoView)el.scrollIntoView({block:"nearest"});
    }
  });
  if(!cmdkItems.length&&inp)inp.setAttribute("aria-activedescendant","");
}
async function runCmdkSearch(){
  const my=++cmdkSeq,body=$("#cmdk-body"),q=($("#cmdk-q")&&$("#cmdk-q").value||"").trim();
  if(!body)return;
  if(!q){
    cmdkItems=CMDK_QUICK.map(x=>({type:"act",fn:x.fn}));cmdkIdx=0;
    body.innerHTML='<div class="cmdk-sec">快捷操作 (↑↓ 选择 · Enter 执行)</div>'+
      CMDK_QUICK.map((x,i)=>'<button type="button" id="cmdk-opt-'+i+'" role="option" aria-selected="'+(i===0?"true":"false")+'" class="cmdk-item'+(i===0?' on':'')+'" data-ci="'+i+'" onclick="execCmdkItem('+i+')">'+
        '<span class="ci-main"><span class="chip">操作</span><span>'+esc(x.label)+'</span></span>'+
        '<span class="ci-sub">'+esc(x.sub)+'</span></button>').join("");
    _syncCmdkActive();
    return;
  }
  body.innerHTML='<div class="cmdk-empty">检索中…</div>';
  try{
    const [mr,sr]=await Promise.all([
      api("/memory?limit=5&q="+encodeURIComponent(q)).catch(()=>({memories:[]})),
      api("/search?limit=8&q="+encodeURIComponent(q)).catch(()=>({hits:[]}))
    ]);
    if(my!==cmdkSeq)return;
    const mems=(mr&&mr.memories)||[],hits=(sr&&sr.hits)||[];
    const ql=q.toLowerCase();
    const acts=CMDK_QUICK.filter(x=>x.label.toLowerCase().includes(ql)||x.sub.toLowerCase().includes(ql));
    cmdkItems=[];cmdkIdx=0;
    let html="";
    if(acts.length){
      html+='<div class="cmdk-sec">快捷操作 ('+acts.length+')</div>';
      acts.forEach(x=>{
        const idx=cmdkItems.length;
        cmdkItems.push({type:"act",fn:x.fn});
        html+='<button type="button" id="cmdk-opt-'+idx+'" role="option" aria-selected="'+(idx===0?"true":"false")+'" class="cmdk-item'+(idx===0?' on':'')+'" data-ci="'+idx+'" onclick="execCmdkItem('+idx+')">'+
          '<span class="ci-main"><span class="chip">操作</span><span>'+esc(x.label)+'</span></span>'+
          '<span class="ci-sub">'+esc(x.sub)+'</span></button>';
      });
    }
    if(mems.length){
      html+='<div class="cmdk-sec">跨会话记忆 ('+mems.length+')</div>';
      mems.forEach(m=>{
        const idx=cmdkItems.length,km=memKindMeta(m.kind),txt=stripMd(m.content).replace(/\s+/g," ").trim();
        const sid=m.snapshot||"";
        cmdkItems.push({type:"mem",sid:sid,q:q});
        html+='<button type="button" id="cmdk-opt-'+idx+'" role="option" aria-selected="'+(idx===0?"true":"false")+'" class="cmdk-item'+(idx===0?' on':'')+'" data-ci="'+idx+'" onclick="execCmdkItem('+idx+')">'+
          '<span class="ci-main"><span class="chip '+km.cls+'">'+esc(km.t)+'</span><span>'+highlightPlain(esc(txt.slice(0,84)),q)+'</span></span>'+
          '<span class="ci-sub">'+esc(m.project||"全局")+'</span></button>';
      });
    }
    if(hits.length){
      html+='<div class="cmdk-sec">历史快照 ('+hits.length+')</div>';
      hits.forEach(h=>{
        const idx=cmdkItems.length,c=cleanSummary(h.first||"");
        cmdkItems.push({type:"snap",sid:h.sid,ts:h.created_at||""});
        html+='<button type="button" id="cmdk-opt-'+idx+'" role="option" aria-selected="'+(idx===0?"true":"false")+'" class="cmdk-item'+(idx===0?' on':'')+'" data-ci="'+idx+'" onclick="execCmdkItem('+idx+')">'+
          '<span class="ci-main"><span class="chip">'+esc(h.agent||"?")+'</span><span>'+highlightPlain(esc(c.text.slice(0,72)),q)+'</span></span>'+
          '<span class="ci-sub">'+esc(shortSid(h.sid))+'</span></button>';
      });
    }
    if(!cmdkItems.length){
      body.innerHTML='<div class="cmdk-empty">未找到匹配「'+esc(q)+'」的记忆或快照</div>';
      _syncCmdkActive();
      return;
    }
    body.innerHTML=html;
    _syncCmdkActive();
  }catch(e){
    if(my!==cmdkSeq)return;
    body.innerHTML='<div class="cmdk-empty">检索失败：'+esc(e.message)+'</div>';
  }
}
function onCmdkKey(e){
  if(e.key==="Escape"){closeCmdk();return}
  if(!cmdkItems.length){
    if(e.key==="Enter"){
      const q=($("#cmdk-q")&&$("#cmdk-q").value||"").trim();
      closeCmdk();setTab("snap");$("#snap-q").value=q;q?snapSearch():loadSnaps();
    }
    return;
  }
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    e.preventDefault();
    cmdkIdx=(cmdkIdx+(e.key==="ArrowDown"?1:-1)+cmdkItems.length)%cmdkItems.length;
    _syncCmdkActive();
    return;
  }
  if(e.key==="Enter"){e.preventDefault();execCmdkItem(cmdkIdx)}
}
function execCmdkItem(idx){
  const it=cmdkItems[idx];if(!it)return;
  closeCmdk();
  if(it.type==="act"&&it.fn){it.fn();return}
  if(it.sid&&/^cv_[0-9a-f]{8,64}$/i.test(it.sid)){snapDetail(it.sid,it.ts||"");return}
  openMemoryView();
  if($("#snap-q"))$("#snap-q").value=it.q||"";
  loadMemoryView();
}
$("#g-q").addEventListener("keydown",e=>{
  if(e.key!=="Enter")return;
  const q=e.target.value.trim();
  snapTreeMode=false;snapMemMode=false;updateSnapTreeBtn();
  setTab("snap");
  $("#snap-q").value=q;
  q?snapSearch():loadSnaps();
});

/* ================= 服务健康与窗口焦点自愈 ================= */
let lastSnapTotal=null,lastHealthAt=0;
function stripToken(){
  if(/[?&]token=/.test(location.search||"")&&window.history)
    history.replaceState(null,"",location.pathname+(location.hash||""));
}
async function checkHealth(){
  const h=await api("/healthz");
  lastHealthAt=Date.now();
  const s=$("#status");s.classList.remove("err","auth");
  s.title="数据已同步于 "+new Date().toLocaleTimeString("zh-CN",{hour12:false});
  s.innerHTML='<span class="pulse"></span><span>服务正常 · '+num(h.snapshots)+" 快照 · "+num(h.terms)+" 索引词项</span>";
  return h;
}
let healthSeq=seqGuard();
async function boot(){
  booted=true;
  // 并发启动健康探活与概览首屏, 消除首屏串行等待; 快照列表按需在进入 snap 分区时再加载
  const ovP=loadOv();
  if(curTab==="snap"){snapsLoaded=true;refreshSnaps();}
  try{
    const h=await healthSeq(async()=>checkHealth());
    lastSnapTotal=h?h.snapshots:null;
    stripToken();
  }catch(e){
    const s=$("#status");
    if(e&&e.code===401){s.classList.remove("err");s.classList.add("auth");
      s.innerHTML='<span class="pulse"></span><span>需要鉴权 — 请用带 token 的链接重新打开 (凭据不入页面)</span>';}
    else{s.classList.add("err");
      s.innerHTML="<span class='pulse'></span><span>服务异常 · 4s 后重试</span>";}
    scheduleBoot();
  }
  await ovP;
}
document.addEventListener("visibilitychange",async()=>{
  if(document.visibilityState!=="visible"||(Date.now()-lastHealthAt)<25000)return;
  try{
    const h=await checkHealth();
    if(h&&lastSnapTotal!==null&&h.snapshots!==lastSnapTotal){
      lastSnapTotal=h.snapshots;
      loadOv();
      if(curTab==="snap"){snapsLoaded=true;refreshSnaps();}else{snapsLoaded=false;}
    }
  }catch(e){}
});

