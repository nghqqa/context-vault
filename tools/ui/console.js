/* ================= 主题与基础工具 ================= */
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
function applyThemeMeta(v){
  const val=(v==="light"||v==="dark")?v:"light dark";
  try{const m=document.querySelector('meta[name="color-scheme"]');if(m)m.content=val}catch(e){}
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
  "tbl-snap":'<colgroup><col class="c-sid"><col><col class="c-src"><col class="c-num"><col class="c-time"><col class="c-dir"><col class="c-act"></colgroup>',
  "tbl-probe":'<colgroup><col class="c-time"><col class="c-node"><col class="c-http"><col class="c-lat"><col></colgroup>'
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
  if(k==="summary")return{t:"摘要",cls:""};
  if(k==="fact")return{t:"事实",cls:""};
  return{t:k||"记忆",cls:""};
};
const formatNodeId=p=>{
  const s=String(p||"").trim();
  if(!s)return"—";
  return /^\d+$/.test(s)?("节点 #"+s):s;
};
const relayBadge=p=>'<span class="relay-badge" title="上游节点 '+esc(p||"")+'">'+
  '<span class="rn">'+esc(formatNodeId(p))+"</span></span>";

const copyText=async(t,msg)=>{
  try{await navigator.clipboard.writeText(t)}
  catch(e){toast("复制失败: "+String(e&&e.message||e),1);return}
  toast(msg||"已复制")};

/* ================= 分区导航 + 快捷键 ================= */
const TABS=["ov","snap","mig","probe"];
const TAB_TITLE={ov:"概览",snap:"快照资产",mig:"迁移工作台",probe:"上游哨兵"};
function setTab(t){
  if(!TABS.includes(t))t="ov";
  $$("nav.side-nav button").forEach(x=>{const on=x.dataset.t===t;
    x.classList.toggle("on",on);on?x.setAttribute("aria-current","page"):x.removeAttribute("aria-current")});
  TABS.forEach(k=>{$("#t-"+k).style.display=k===t?"":"none"});
  $("#sec-title").textContent=TAB_TITLE[t];
  const sc=$(".content");if(sc)sc.scrollTop=0;
  if(window.scrollTo)window.scrollTo(0,0);
  if(t==="mig"&&!$("#mig-sid").dataset.filled)fillMig();
  if(t==="probe"&&!$("#probe-list").dataset.loaded)loadProbe();
  if(window.history)history.replaceState(null,"","#"+t);
}
$$("nav.side-nav button").forEach(b=>b.onclick=()=>setTab(b.dataset.t));
document.addEventListener("keydown",e=>{
  const t=e.target,tag=t&&t.tagName;
  if(tag&&/^(INPUT|SELECT|TEXTAREA)$/.test(tag)){
    if(e.key==="Escape"&&t.blur)t.blur();
    return;
  }
  if(drawerIsOpen())return;
  if(e.key==="/"){e.preventDefault();const g=$("#g-q");if(g&&g.focus)g.focus();return}
  const n=+e.key;
  if(n>=1&&n<=TABS.length){setTab(TABS[n-1]);e.preventDefault()}
});
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
  s.innerHTML='<span class="pulse"></span>服务正常 · '+num(h.snapshots)+" 快照 · "+num(h.terms)+" 索引词项";
  return h;
}
let healthSeq=seqGuard();
async function boot(){
  try{
    const h=await healthSeq(async()=>checkHealth());
    lastSnapTotal=h?h.snapshots:null;
    stripToken();
    loadOv();refreshSnaps();
  }catch(e){
    const s=$("#status");
    if(e&&e.code===401){s.classList.remove("err");s.classList.add("auth");
      s.innerHTML='<span class="pulse"></span>需要鉴权 — 请用带 token 的链接重新打开 (凭据不入页面)';}
    else{s.classList.add("err");
      s.innerHTML="<span class='pulse'></span>服务异常 · 4s 后重试";}
    scheduleBoot();
  }
}
document.addEventListener("visibilitychange",async()=>{
  if(document.visibilityState!=="visible"||(Date.now()-lastHealthAt)<25000)return;
  try{
    const h=await checkHealth();
    if(h&&lastSnapTotal!==null&&h.snapshots!==lastSnapTotal){
      lastSnapTotal=h.snapshots;
      loadOv();refreshSnaps();
    }
  }catch(e){}
});

/* ================= 概览 ================= */
const ovSeq=seqGuard();
async function loadOv(){
  $("#ov-band").innerHTML='<div class="kpi"><div class="skel" style="width:70%"></div></div>'.repeat(4);
  try{
    const d=await ovSeq(()=>api("/ledger"));
    const ow=d.owned||{};
    const zu=d.zcode_usage||{};
    const sessions=(ow.sessions!=null)?ow.sessions:null;
    const hit=(ow.cache_hit_rate!=null)?ow.cache_hit_rate:null;
    const hitSem=ow.semantics||"cache 读 / 全部输入 (按来源口径规范化的分母)";
    const inEff=(ow.input_effective!=null)?ow.input_effective:(ow.tokens_in||0);
    const ztIn=Object.values(zu).reduce((s,a)=>s+(a.tin||0),0);
    const zOwned=(((ow.by_agent||[]).find(a=>a.agent==="zcode"))||{}).tokens_in||0;
    const totalIn=(inEff||0)-(ztIn>0?zOwned:0)+ztIn;
    const snapSub=(ow.disk_snapshots!=null&&ow.disk_snapshots>0&&ow.disk_snapshots!==ow.snapshots)
      ?("在库文件 "+num(ow.disk_snapshots)+" · 台账记录 "+num(ow.snapshots))
      :"不可变内容寻址";
    const band=
      kpi(num(ow.snapshots||0),"快照版本","vault/snapshots 内容寻址快照 · 含被演进替代的历史版本",snapSub)+
      kpi(sessions==null?"—":num(sessions),"独立会话",sessions==null?"后端未提供独立会话口径":"按 lineage 演进链去重后的会话数","按演进链去重")+
      kpi('<span title="'+num(totalIn)+'">'+bn(totalIn)+'</span><small>tokens</small>',
        '累计输入 tokens',
        "口径: 快照内嵌 usage 规范化总输入 ("+num(inEff||0)+", Claude 语义补回 cache 读/写) · zcode 以 model_usage 全量调用 ("+num(ztIn)+") 替换快照内的 zcode 部分 · 均含 cache 读",
        '精确 '+num(totalIn)+(ztIn>0?' · 含 zcode 全量':''))+
      kpi(hit==null?"—":'<span class="'+(hit>1?"kpi-bad":"")+'">'+(hit*100).toFixed(1)+"%</span>",
        "缓存命中率",
        "口径: "+hitSem+" · 数据源: vault 快照内嵌 usage",
        hit==null?"尚无缓存样本":"近库加权命中");
    $("#ov-band").innerHTML=band;

    let u5=null;
    try{u5=await api("/ledger/usage?window="+encodeURIComponent(usageWindow))}catch(e){u5=null}
    renderQuota(u5,d);

    let rc=null;try{rc=await api("/recent?limit=10&leaves=1")}catch(e){}
    const total=(rc&&rc.total)||0;
    const arr=(rc&&rc.rows)||[];
    $("#ov-latest").innerHTML=(arr.length?table(["来源","会话摘要","位置","入库时间"],
      arr.map(s=>{const tr=[agentCell(s.agent),
        '<button class="linklike sumbtn" title="打开快照详情" onclick="event.stopPropagation();snapDetail(\''+s.sid+'\',\''+(s.created_at||"")+'\')">'+sumCell(s.first)+"</button>",
        dirCell(s.cwd,22),
        '<span class="nowr hint" title="'+esc(s.created_at||"")+'">'+frel(s.created_at)+"</span>"];
        tr.__tr='class="rowlink" onclick="snapDetail(\''+s.sid+'\',\''+(s.created_at||"")+'\')"';
        return tr}),"tbl-44 fixed-cols tbl-ov")
      :'<div class="empty">还没有会话入库<br>先点右上角 <b>↻ 立即同步</b> 或运行 <code>python tools/cv.py sync</code>'+
       '<br><button class="btn sm" onclick="loadOv()">重试</button></div>')+
      '<div class="foot"><span>显示最近 '+arr.length+" 个独立会话 · 共 "+num(total)+" 个会话 / "+
        num((rc&&rc.versions_total)||total)+" 个版本</span>"+
      '<button class="linklike" onclick="setTab(\'snap\')">浏览全部 →</button></div>';

    let pv=[];try{pv=(await api("/probe_events?limit=60")).filter(e=>e.status!=="prune")}catch(e){}
    if(Array.isArray(pv)&&pv.length){
      const byNode={},orderNodes=[];
      for(const e of pv){
        const k=e.profile||"_";
        if(!(k in byNode)){byNode[k]={head:e,errStreak:0,counting:true};orderNodes.push(k)}
        const g=byNode[k];
        if(g.counting){
          if(e.status==="http_error"||e.status==="error")g.errStreak++;
          else g.counting=false;
        }
      }
      $("#ov-probe").innerHTML=orderNodes.slice(0,5).map(k=>{
        const g=byNode[k],e=g.head,st=e.status||"?";
        const miss=e.missing_models&&e.missing_models!=="[]";
        const cls=miss?"err":st==="ok"?"ok":(st==="http_error"||st==="error")?"err":"warn";
        const lbl=miss?"模型缺失":st==="ok"?((e.http_code||200)+" 正常"):(st==="http_error"||st==="error")?(e.http_code?(e.http_code+" 异常"):"无响应"):st==="switch"?"节点切换":esc(st);
        const extra=(st==="ok"&&e.latency_ms)?('<span class="hint">'+e.latency_ms+'ms</span>')
          :(g.errStreak>=2?('<span class="hint">连续 '+g.errStreak+' 次异常</span>'):"");
        return '<div class="feed-row click" onclick="setTab(\'probe\')" title="查看哨兵节点详情"><span class="feed-main"><span class="dot '+cls+'"></span>'+
          '<span class="mono">'+esc(formatNodeId(e.profile))+'</span>'+
          '<span class="pill '+cls+'">'+esc(lbl)+'</span>'+extra+
          '</span><span class="feed-r">'+frel(e.ts)+"</span></div>"}).join("");
    }else{
      $("#ov-probe").innerHTML='<div class="hint" style="padding:10px 14px">暂无探测记录 · 到哨兵页点击「立即探测上游」</div>';
    }

    let mr=[];try{mr=await api("/memory?q=&limit=4");mr=(mr&&mr.memories)||[]}catch(e){mr=[]}
    $("#ov-mem").innerHTML=mr.length?mr.map(m=>{
      const km=memKindMeta(m.kind);
      const txt=stripMd(m.content).replace(/\s+/g," ").trim();
      const sid=m.snapshot||"";
      const clickAttr=(/^cv_[0-9a-f]{8,64}$/i.test(sid))?' class="feed-row click" onclick="snapDetail(\''+sid+'\')"':' class="feed-row"';
      return '<div'+clickAttr+' title="'+esc(txt+(sid?(" · 来源 "+sid):""))+'"><span class="feed-main">'+
        '<span class="chip '+km.cls+'">'+esc(km.t)+'</span>'+
        '<span class="feed-txt">'+esc(txt)+'</span></span>'+
        '<span class="feed-r"><span class="chip">'+esc(m.project||"全局")+"</span></span></div>";
    }).join("")
      :'<div class="hint" style="padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:8px"><span>暂无跨会话记忆</span><button class="btn sm" onclick="openMemoryView()">提取记忆 →</button></div>';

    let au=null;try{au=await api("/audit")}catch(e){}
    $("#ov-audit").innerHTML=au?
      '<div class="audit-grid">'+
      '<div class="audit-cell"><span class="al">累计事件</span><span class="av">'+num(au.total_events||0)+'</span></div>'+
      '<div class="audit-cell"><span class="al">探测正常</span><span class="av" style="color:var(--ok)">'+num((au.summary||{}).ok_probes||0)+'</span></div>'+
      '<div class="audit-cell"><span class="al">异常告警</span><span class="av" style="color:'+(((au.summary||{}).abnormal||0)>0?'var(--err)':'var(--ink)')+'">'+num((au.summary||{}).abnormal||0)+'</span></div>'+
      '<div class="audit-cell"><span class="al">切换 / 缺失</span><span class="av">'+num((au.summary||{}).switches||0)+' / '+num((au.summary||{}).model_missing_events||0)+'</span></div>'+
      '</div>'
      :'<div class="hint" style="padding:10px 14px">—</div>';

    renderUsageRank();
  }catch(e){
    if(e&&e.stale)return;
    $("#ov-band").innerHTML=errEmpty("概览加载失败："+e.message,"loadOv()");
    $("#ov-latest").innerHTML="";$("#ov-quota").innerHTML="";
  }
}
const kpi=(v,k,tip,sub)=>'<div class="kpi"'+(tip?' title="'+esc(tip)+'"':"")+'>'+
  '<div class="k"><span>'+k+'</span></div>'+
  '<div class="v">'+v+'</div>'+
  (sub?'<div class="exact">'+sub+'</div>':'')+'</div>';

/* ================= 用量与配额窗口 (本地多端账本, 无 headroom 依赖) ================= */
let usageWindow="5h",usageRankKey="by_model",usageRankData=null,usageLedger=null,usageRefTier=0;
const USAGE_WINDOWS=[["5h","5h"],["24h","24h"],["7d","7d"],["30d","30d"]];
const USAGE_RANKS=[["by_model","模型"],["by_project","项目"],["by_account","账号"],["by_client","客户端"]];
const REF_TIERS=[0,1e7,3e7,6e7,1.2e8];
const REF_TIERS_LBL=["无参考 (按 7d 峰值)","1千万 tok","3千万 tok","6千万 tok","1.2亿 tok"];
const usageSeq=seqGuard();
const aCell=(l,v)=>'<div class="audit-cell"><span class="al">'+l+'</span><span class="av">'+v+"</span></div>";
const estTag=n=>n>0?' <span class="mini-tag" title="该分组含估算事件 (无精确 usage 的来源)">含估算</span>':"";
const etaRel=iso=>{if(!iso)return"—";const d=(new Date(iso).getTime()-Date.now())/1e3;
  if(!(d>0))return"已恢复";const h=Math.floor(d/3600),m=Math.floor(d%3600/60);
  if(h>=48)return(h/24).toFixed(1)+" 天后";
  return h>0?h+"h"+String(m).padStart(2,"0")+"m 后":m+"m 后"};
function quotaRankRows(u,key){
  const rows=(u&&u[key])||[];
  return table(["名称","事件","总输入 tok","输出 tok","费用估算"],
    rows.map(x=>{const r=['<span class="mono" title="'+esc(x.name)+'">'+esc(x.name||"(未知)")+"</span>"+estTag(x.estimated_events),
      {t:num(x.events),num:1},
      {t:numT(x.total_input),num:1},
      {t:numT(x.total_output),num:1},
      {t:x.cost_est_usd?'<span title="按公开 API 挂牌价等效折算">≈'+usd(x.cost_est_usd)+'</span>':"订阅",num:1}];
      return r}));
}
function renderUsageRank(){
  const el=$("#ov-quota-ranks");if(!el)return;
  el.innerHTML=quotaRankRows(usageRankData,usageRankKey)
    ||'<div class="hint" style="padding:6px 0">窗口内暂无数据</div>';}
function setUsageRank(k){usageRankKey=k;
  $$("#ov-quota-rankseg button").forEach(b=>b.classList.toggle("on",b.dataset.k===k));
  renderUsageRank();}
async function setUsageWindow(w){
  usageWindow=USAGE_WINDOWS.some(x=>x[0]===w)?w:"5h";
  $$("#ov-wseg button").forEach(b=>b.classList.toggle("on",b.dataset.w===usageWindow));
  try{const u=await usageSeq(()=>api("/ledger/usage?window="+encodeURIComponent(usageWindow)));
    if(u)renderQuota(u,usageLedger)}catch(e){}
}
function renderQuota(u,ledger){
  const el=$("#ov-quota");if(!el)return;
  usageLedger=ledger||usageLedger;
  const sumEl=$("#ov-quota-sum");
  if(!u||!u.events_total){
    el.innerHTML='<div class="hint" style="padding:4px 0">usage_events 为空 — 运行 <code>cv ledger sync</code> 采集本地多端 (Claude/Codex/ZCode/Antigravity) 用量后, 这里展示 5 小时滚动窗口、冷却倒计时与多窗口排行。</div>';
    if(sumEl)sumEl.textContent="（未采集 · 运行 cv ledger sync 后展示）";
    usageRankData=null;renderUsageRank();return}
  const t=u.totals||{};
  const hit=u.cache_hit_rate;
  const wsrcs=Object.entries(u.window_sources||{}).map(([k,v])=>k+" "+v).join(" · ");
  const costKnown=(t.cost_known_events||0)>0;
  const rqPlan=(u.remote_quota&&u.remote_quota.ok)?("订阅 · "+esc(u.remote_quota.plan)+" 套餐"):"订阅模式 · 未配置单价";
  // 头部: 窗口切换 (5h/24h/7d/30d) + 窗口内来源分解 + 全库累计 (口径分离: 窗口 vs 全库)
  let h='<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">'+
    '<div class="seg" id="ov-wseg" role="group" aria-label="统计窗口">'+
    USAGE_WINDOWS.map(([k,l])=>'<button class="btn'+(usageWindow===k?" on":"")+'" data-w="'+k+
      '" onclick="setUsageWindow(\''+k+'\')">'+l+"</button>").join("")+"</div>"+
    '<span class="hint">窗口 '+esc(usageWindow)+" 事件 "+num(t.events)+" ("+esc(wsrcs||"—")+
    ") · 全库累计 "+num(u.events_total)+" 事件</span></div>";
  h+='<div class="audit-grid">'+
    aCell("窗口 "+esc(usageWindow)+" KPI · 窗口事件",num(t.events)+" <span class=\"hint\">"+esc(wsrcs||"—")+"</span>")+
    aCell("总输入",numT(t.total_input)+" <small>tokens</small>")+
    aCell("费用估算",(t.cost_est_usd?usd(t.cost_est_usd)+' <span class="mini-tag">公开 API 挂牌价等效折算</span>'
      :(costKnown?'<span class="hint">部分事件按列表价折算</span>':'<span class="mini-tag" title="未匹配到单价表 (vault/pricing.json); 订阅制模型默认不计成本">'+rqPlan+"</span>")))+
    aCell("缓存命中率",hit==null?"—":'<span>'+(hit*100).toFixed(1)+"%</span>"+
      ((u.cache_save_est_usd||0)>0?" · 节省约 "+usd(u.cache_save_est_usd):'<span class="hint"> · 订阅不计节省</span>'))+
    '</div>';
  const pre=u.precision||{};
  h+='<div class="hint" style="margin:4px 0 8px">精确事件 '+(pre.exact_events||0)+" · 估算事件 "+(pre.estimated_events||0)+
    " ("+((pre.estimated_share||0)*100).toFixed(0)+"% 估算) · 窗口 "+esc(u.window||"5h")+
    (u.accounts_configured?" · 额度池已配置 (accounts.json)":" · 未配置上限 (<code>cv ledger limit --list</code>)")+"</div>";
  // 厂商实时额度: 智谱 (CV_PLAN_TOKEN) + Antigravity IDE 本机 RPC, 共用渲染块
  const quotaRemoteBlock=(rq,title,tag,tip)=>{
    if(!rq||!rq.ok)return"";
    const LT={TOKENS_LIMIT:"5h Token 额度",TIME_LIMIT:"MCP 月额度",PROMPTS_LIMIT:"提示次数",REQUESTS_LIMIT:"请求数"};
    return '<div class="hint" style="margin:10px 0 2px"><b>'+title+"</b> — "+esc(rq.plan||"?")+
      ' <span class="mini-tag" title="'+esc(tip)+'">'+tag+"</span></div>"+
      '<div class="audit-grid">'+(rq.limits||[]).map(l=>{
        const pct=l.pct_used||0;
        const cls=pct>=90?"q-hot":pct>=70?"q-warn":"";
        const lTitle=l.desc?('<span title="'+esc(l.desc)+'">'+(LT[l.type]||esc(l.type))+"</span>"):(LT[l.type]||esc(l.type));
        return aCell(lTitle,
          '<span class="q-big '+cls+'">'+pct.toFixed(1)+'%</span><span class="hint"> 已用</span>'+
          '<span class="winbar'+(cls?" "+cls:"")+'" style="display:block;width:100%;margin:7px 0 4px"><i style="width:'+Math.min(100,pct)+'%"></i></span>'+
          (l.reset_at?'<div class="quota-eta">重置 '+etaRel(l.reset_at)+"</div>":""));
      }).join("")+"</div>"};
  h+=quotaRemoteBlock(u.remote_quota,"套餐余额 (智谱实时)","实时",
    "CV_PLAN_TOKEN / vault/plan_token.json 配置后由智谱额度接口返回, 服务端 60s 缓存; Token 不出本机");
  h+=quotaRemoteBlock(u.remote_quota_agv,"Antigravity 额度 (本机 IDE)","LSP 实时",
    "经 LSP 官方发现文件 (daemon/hub_*.json) 直连本机 language_server 实时额度; IDE 登录态即凭据, 零配置");
  if((u.sources||{}).antigravity>0&&!(u.remote_quota_agv&&u.remote_quota_agv.ok)){
    h+='<div class="hint" style="margin:6px 0 2px"><b>Antigravity 额度 (本机 IDE)</b> — 暂不可用 ('+
      esc((u.remote_quota_agv||{}).error||"未发现")+') · IDE 未运行或未登录时仅显示下方本地估算消耗</div>'}
  // 5h 滚动窗口 (固定 5h 语义, 不随上方分析窗口切换): 进度条真实填充 + 冷却倒计时常显
  const accs=(u.fiveh&&u.fiveh.accounts)||[];
  const hasLimits=accs.some(a=>a.mode==="limit");
  const f5=(u.fiveh&&u.fiveh.totals)||{};
  h+='<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 2px">'+
    '<span class="hint"><b>'+(hasLimits?"5 小时额度窗口 (本地额度池)":"5h 消耗构成 (按客户端归因)")+"</b> — "+
    (hasLimits?"上限/恢复 ETA 来自 accounts.json 额度池":"未配上限=纯消耗模式: 进度条按 7d 峰值/参考档位, ETA 为窗口回落倒计时")+"</span>"+
    '<select id="ov-ref-tier" aria-label="参考档位" onchange="setRefTier(this.value)">'+
    REF_TIERS.map((v,i)=>'<option value="'+v+'"'+(usageRefTier===v?" selected":"")+">"+REF_TIERS_LBL[i]+"</option>").join("")+
    "</select></div>";
  h+=accs.length?accs.map(a=>{
    const isLim=a.mode==="limit"||a.limit;
    const barCls=isLim&&a.pct!=null&&a.pct>=1?"winbar hot":"winbar";
    // 进度条真实填充: 有上限→上限占比; 无上限→参考档位占比 > 7d 单 5h 峰值比 > 窗口内占比
    let wPct,pctS;
    if(isLim){wPct=Math.min(100,(a.pct||0)*100);pctS=(a.pct*100).toFixed(1)+"%"}
    else if(usageRefTier){
      wPct=Math.min(100,(a.used_weighted||0)/usageRefTier*100);
      pctS=(a.used_weighted/usageRefTier*100).toFixed(1)+"%"}
    else if(a.pct_of_peak!=null){
      wPct=Math.min(100,a.pct_of_peak*100);pctS="峰值比 "+(a.pct_of_peak*100).toFixed(0)+"%"}
    else{wPct=Math.min(100,Math.round((a.share||0)*100));pctS="占比 "+Math.round((a.share||0)*100)+"%"}
    const usageS=isLim?(num(a.used_weighted)+" / "+num(a.limit)+" ("+esc(a.weight||"total_tokens")+")")
      :(bn(a.used_weighted)+" tok");
    let line='<div class="quota-row"><span class="qn" title="'+esc(a.account)+'">'+esc(a.account)+"</span>"+
      '<span class="'+barCls+'"><i style="width:'+wPct+'%"></i></span>'+
      '<span class="mono">'+pctS+"</span>"+
      '<span class="hint">'+usageS+"</span></div>";
    if(a.eta&&(isLim?a.eta["50%"]!=null||a.eta["80%"]!=null||a.eta["100%"]!=null:true)){
      line+='<div class="quota-eta" style="padding:0 0 3px 130px">'+
        (isLim?"恢复至 50%: ":"窗口回落 50%: ")+etaRel(a.eta["50%"])+
        " · 80%: "+etaRel(a.eta["80%"])+
        (isLim?" · 100%: ":" · 完全冷却: ")+etaRel(a.eta["100%"])+"</div>"}
    if(a.models&&a.models.length){
      line+='<div class="quota-eta" style="padding:0 0 6px 130px">模型拆分: '+
        a.models.map(m=>esc(m.name)+" "+Math.round((m.share||0)*100)+"% ("+bn(m.used)+")").join(" · ")+
        "</div>"}
    return line}).join("")
    :'<div class="hint" style="padding:2px 0">近 5 小时无消耗</div>';
  const rw=u.runway||[];
  if(rw.length){
    h+='<div class="hint" style="margin:8px 0 2px"><b>跑道</b> — 按近 24h 消耗速率外推距 7d/月上限的剩余天数</div>';
    h+=rw.map(r=>{const cap=r.metric==="tokens"?num(r.cap)+" tok":usd(r.cap);
      const used=r.metric==="tokens"?num(r.used_tokens)+" tok":usd(r.used_usd);
      const dl=r.days_left!=null?r.days_left.toFixed(1)+" 天":"∞ (近 24h 零消耗)";
      return '<div class="quota-row"><span class="qn" title="'+esc(r.account)+'">'+esc(r.account)+"</span>"+
        '<span class="mini-tag">'+esc(r.kind)+"</span>"+
        '<span class="hint">上限 '+cap+" · 本期已用 "+used+" · 剩余跑道 <b>"+dl+"</b></span></div>"}).join("")}
  // 消耗排行 (随窗口切换) + 30d 趋势
  h+='<div class="hint" style="margin:10px 0 6px"><b>消耗排行</b> — 窗口 '+esc(u.window||"5h")+
    ' · 精确/估算已标注 <span class="mini-tag">含估算=来源无精确 usage</span></div>';
  h+='<div id="ov-quota-rankseg" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'+
    USAGE_RANKS.map(([k,l])=>'<button class="btn sm'+(usageRankKey===k?" on":"")+'" data-k="'+k+
      '" onclick="setUsageRank(\''+k+'\')">'+l+"</button>").join("")+"</div>";
  h+='<div id="ov-quota-ranks"></div>';
  const days=(u.by_day)||[];
  if(usageWindow==="30d"&&days.length){
    const show=days.slice(-14);
    const mx=Math.max(...show.map(x=>x.total_input||0),1);
    const fmt=v=>v>=1e9?(v/1e9).toFixed(1)+"B":v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e3?(v/1e3).toFixed(0)+"k":String(Math.round(v));
    const mw=Math.min(820,show.length*54+100);
    h+='<div class="hint" style="margin:10px 0 4px"><b>近 14 天消耗趋势</b> (30d 窗口内逐日)</div>'+
      '<div class="months" style="max-width:'+mw+'px"><div class="yax"><span>'+fmt(mx)+'</span><span>'+fmt(mx/2)+
      '</span><span>0</span></div><div class="bars">'+show.map((x,i)=>{const hh=Math.max(100*(x.total_input||0)/mx,3);
      return '<div class="mcol"><div class="val" style="--h:'+hh.toFixed(1)+'">'+fmt(x.total_input||0)+
      '</div><div class="stick" style="height:'+hh.toFixed(2)+'%;--i:'+i+'" title="'+
      esc(x.day+" · 输入 "+num(x.total_input)+" · 输出 "+num(x.total_output||0))+
      '"></div><div class="lb">'+esc(String(x.day).slice(5))+"</div></div>"}).join("")+"</div></div>";
  }
  // 底部小字: ZCode 历史累计库 (model_usage 全量真相源, 与 usage_events 窗口口径分离)
  const zu=(usageLedger&&usageLedger.zcode_usage)||{};
  const zkeys=Object.keys(zu);
  if(zkeys.length){
    const zcalls=zkeys.reduce((s,k)=>s+(zu[k].n||0),0);
    const zline=zkeys.map(k=>esc(k)+" "+bn(zu[k].tin||0)).join(" · ");
    h+='<div class="section-note" style="margin-top:10px">ZCode 历史累计库 (~/.zcode/cli/db model_usage, 全量调用真相源): '+
      zline+" · 共 "+num(zcalls)+" 次调用 · 当前窗口实时消耗见上方 usage_events 账本</div>"}
  el.innerHTML=h;
  usageRankData=u;
  renderUsageRank();
  if(sumEl){
    sumEl.textContent="（5h 已用 "+bn((f5.total_input||0)+(f5.total_output||0))+
      (t.cost_est_usd?" · 窗口费用估 "+usd(t.cost_est_usd):" · 订阅模式")+
      " · 累计 "+num(u.events_total)+" 事件）"}
}
function setRefTier(v){usageRefTier=+v||0;
  if(usageRankData)renderQuota(usageRankData,usageLedger)}

/* ================= 快照资产 & 项目树 & 跨会话记忆库 ================= */
const snapsSeq=seqGuard();
let snapPage=0,snapAsc=false,snapMode="recent",snapQuery="",snapLeaves=true,snapTreeMode=false,snapMemMode=false,memKind="all",snapInputTimer=null,treeTarget="antigravity",treeApply=false,treeAgent="zcode",treeArchived=false;
const projOpenState={};

function updateSnapTreeBtn(){
  const b=$("#snap-tree-btn"),lbBtn=$("#snap-list-btn"),mbBtn=$("#snap-mem-btn"),qEl=$("#snap-q");
  const isList=!snapTreeMode&&!snapMemMode;
  if(b){b.setAttribute("aria-pressed",snapTreeMode?"true":"false");b.classList.toggle("on",snapTreeMode)}
  if(mbBtn){mbBtn.setAttribute("aria-pressed",snapMemMode?"true":"false");mbBtn.classList.toggle("on",snapMemMode)}
  if(lbBtn){lbBtn.setAttribute("aria-pressed",isList?"true":"false");lbBtn.classList.toggle("on",isList)}
  const sb=$("#snap-sort"),lb=$("#snap-leaves");
  if(sb)sb.style.display=isList?"":"none";
  if(lb)lb.style.display=isList?"":"none";
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
        pill("preference","偏好",cnts.preference||0)+
        pill("decision","决策",cnts.decision||0)+
        pill("summary","压缩摘要",cnts.summary||0)+
      '</div>'+
      '<div class="mem-acts">'+
        '<button class="btn sm primary" id="mem-ext-btn" onclick="extractMemoriesNow(false)">增量提取记忆</button>'+
        '<button class="btn sm" onclick="dedupMemoriesNow()" title="合并 (类型, 内容) 完全相同的跨快照重复摘录，保留最早一条">去重合并</button>'+
        '<button class="btn sm" onclick="extractMemoriesNow(true)" title="忽略已提取水位，重新扫描全库快照提取记忆">全量重提</button>'+
      '</div></div>';
    if(!arr.length){
      $("#snap-list").innerHTML=dock+'<div class="empty">'+WARN+
        '<div>'+(q?('没有匹配「'+esc(q)+'」的跨会话记忆'):'当前分类下暂无记忆条目')+'</div>'+
        '<div class="hint">记忆由规则引擎从历史对话中的「决策结论 / 用户偏好 / 压缩摘要」自动提取，不修改原快照。</div>'+
        '<button class="btn primary" onclick="extractMemoriesNow(false)">立即从全库快照提取</button></div>';
      return;
    }
    const listHtml=arr.map(m=>{
      const km=memKindMeta(m.kind);
      const cleanTxt=stripMd(m.content).replace(/\s+/g," ").trim();
      const sid=m.snapshot||"";
      const validSid=/^cv_[0-9a-f]{8,64}$/i.test(sid);
      return '<div class="mem-row">'+
        '<div class="mem-row-top">'+
          '<span class="chip '+km.cls+'">'+esc(km.t)+'</span>'+
          '<span class="chip">'+esc(m.project||"全局")+'</span>'+
          (m.hits>0?'<span class="mini-tag">引用 ×'+num(m.hits)+'</span>':'')+
          '<span class="mem-row-meta">'+
            (validSid?('来源 '+sidBtn(sid)):esc(sid))+
          '</span>'+
        '</div>'+
        '<div class="mem-row-txt">'+highlightPlain(esc(cleanTxt),q)+'</div>'+
      '</div>';
    }).join("");
    $("#snap-list").innerHTML=dock+'<div class="mem-list">'+listHtml+'</div>';
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
  const bar='<div class="proj-dock">'+
    '<div class="proj-toolbar">'+
      '<label for="tree-agent">来源筛选</label>'+
      '<select id="tree-agent" onchange="treeAgent=this.value;loadProjectTree()">'+
        '<option value="zcode"'+(treeAgent==="zcode"?" selected":"")+'>仅 ZCode（默认）</option>'+
        '<option value=""'+(treeAgent===""?" selected":"")+'>全部来源（含 Codex / Claude）</option>'+
        '<option value="codex"'+(treeAgent==="codex"?" selected":"")+'>仅 Codex</option>'+
        '<option value="claude-code"'+(treeAgent==="claude-code"?" selected":"")+'>仅 Claude Code</option>'+
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
      "&leaves="+(snapLeaves?1:0)));
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
  if(outOnly)$("#mig-md-note").textContent="Markdown 目标始终导出到 outbox，无写入会话目录语义。";
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
    const r=await api("/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:id,target:t,apply:apply})});
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
function renderMigResult(r,ctx){
  const fpath=String(r.dest||"");
  const fname=fpath.split(/[\\/]/).pop();
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
  }else{
    cmd="code \""+fpath+"\"";cmdNote="Markdown 简报 — 任意 agent / 编辑器可读";
  }
  $("#mig-result").innerHTML=
    '<div class="result-card"><div class="rt">'+GOOD+
    (r.applied?"已写入目标会话目录":"已生成到 vault/outbox/")+
    (ctx.target==="markdown"?"（交接简报）":"")+"</div>"+
    '<div class="path-row"><span class="hint">文件:</span><span class="mono" style="word-break:break-all">'+esc(fpath)+"</span>"+
    '<button class="btn sm" data-v="'+esc(fpath)+'" onclick="copyData(this,\'已复制文件路径\')">复制路径</button></div>'+
    '<div class="hint">'+num(r.lines)+" 行"+(r.dropped?" · 丢弃未配对 tool_result ×"+num(r.dropped):" · 无丢弃")+
    (r.new_sid?" · session id <span class='mono'>"+esc(r.new_sid)+"</span>":"")+"</div>"+
    '<div class="term"><span class="tp">'+esc(cmdNote)+"</span><code>$ "+esc(cmd)+
    '</code><br><button class="btn sm" style="margin-top:8px" data-v="'+esc(cmd)+'" onclick="copyData(this,\'已复制命令\')">复制命令</button></div></div>';
}

/* ================= 哨兵 ================= */
const probeSeq=seqGuard();
const shortModels=s=>{try{const m=JSON.parse(s);return m.by_agent?Object.entries(m.by_agent).map(([k,v])=>k+"×"+v).join(" · ")+" 活跃会话":""}catch(e){return""}};
function probeStateOf(rows){
  if(!rows.length)return {cls:"off",txt:"未配置 / 暂无探测记录"};
  const by={};
  rows.forEach(r=>{if(r.profile&&!(r.profile in by))by[r.profile]=r});
  const states=Object.values(by);
  const missing=states.some(r=>r.missing_models&&r.missing_models!=="[]");
  const err=states.some(r=>r.status==="http_error"||r.status==="error");
  if(missing)return {cls:"err",txt:"模型缺失 — 见事件详情"};
  if(err)return {cls:"err",txt:"存在异常节点 — 见事件详情"};
  const okAll=states.every(r=>r.status==="ok");
  if(okAll)return {cls:"ok",txt:states.length+" 个节点最近探测正常（基于最近样本）"};
  return {cls:"warn",txt:"部分节点待确认 — 见事件详情"};
}
async function loadProbe(){
  const el=$("#probe-list");
  el.innerHTML='<div class="skel" style="width:60%"></div><div class="skel" style="width:45%"></div>';
  try{
    const rows=(await probeSeq(()=>api("/probe_events?limit=200"))).filter(r=>r.status!=="prune");
    const st=probeStateOf(rows);
    $("#probe-state").className="probe-state "+st.cls;
    $("#probe-state").innerHTML='<span class="dot '+st.cls+'"></span>'+esc(st.txt);
    $("#probe-last").textContent=rows.length?("最新样本 "+frel(rows[0].ts)+" · 仅本页可见时每 30s 自动刷新"):"";
    const nodes=new Set(rows.map(r=>r.profile).filter(Boolean));
    const okN=rows.filter(r=>r.status==="ok").length;
    const lats=rows.filter(r=>r.status==="ok"&&r.latency_ms).map(r=>r.latency_ms);
    $("#sn-nodes").textContent=nodes.size||"—";
    $("#sn-avail").textContent=rows.length?((okN/rows.length)*100).toFixed(1)+"% ("+okN+"/"+rows.length+")":"—";
    $("#sn-avail-wrap").title="口径: 最近 "+rows.length+" 条事件内正常响应占比 — 非全天可用率";
    $("#sn-lat").textContent=lats.length?Math.round(lats.reduce((a,b)=>a+b,0)/lats.length)+" ms":"—";
    $("#sn-note").textContent=rows.length?("最近 "+rows.length+" 条事件（limit=200）"):"尚无样本";

    const by={};
    rows.forEach(r=>{if(r.profile&&!(r.profile in by))by[r.profile]=r});
    const profs=Object.entries(by);
    $("#probe-detail").innerHTML=profs.length?profs.map(([p,r])=>{
      const cls=r.status==="ok"?"ok":(r.status==="http_error"||r.status==="error")?"err":"warn";
      const txt=r.missing_models&&r.missing_models!=="[]"?("模型缺失 "+r.missing_models)
        :r.status==="ok"?("HTTP "+(r.http_code||200)+" · "+(r.latency_ms||"—")+" ms · 模型可用")
        :r.status==="switch"?("切换检测 · "+(shortModels(r.models)||"profile 变更"))
        :(r.status==="http_error"||r.status==="error")?("HTTP "+(r.http_code||"无响应")+" · 上游异常")
        :esc(r.status||"—");
      return '<span class="chip" style="margin:0 8px 8px 0"><span class="dot '+cls+'"></span><b class="mono">'+esc(formatNodeId(p))+'</b><span class="hint">· '+esc(txt)+'</span></span>';
    }).join(""):"";

    const httpCell=r=>{
      if(r.status==="ok")return '<span class="pill ok">'+(r.http_code||200)+" 正常</span>";
      if(r.status==="switch")return '<span class="pill warn">节点切换</span>';
      if(r.status==="http_error"||r.status==="error")return '<span class="pill err">'+(r.http_code?(r.http_code+" 异常"):"无响应")+"</span>";
      return '<span class="pill warn">'+esc(r.status||"—")+"</span>"};
    const latCell=r=>{
      if(!r.latency_ms)return '<span class="hint">—</span>';
      if(r.status!=="ok")return '<span class="lat muted">'+r.latency_ms+" ms</span>";
      const v=r.latency_ms;const cls=v<800?"good":v<=1500?"mid":"warm";
      const pct=Math.min(100,Math.round(v/2000*100));
      return '<span class="lat '+cls+'">'+v+" ms"+'<span class="latbar"><i style="width:'+pct+'%"></i></span></span>'};
    const detCell=r=>{
      if(r.missing_models&&r.missing_models!=="[]")return '<span style="color:var(--err)">模型消失 '+esc(r.missing_models)+"</span>";
      if(r.status==="ok")return '<span class="hint">模型列表校验通过</span> <span class="mini-tag">GET /v1/models</span>';
      if(r.status==="switch")return '<span class="hint">切换检测 · '+esc(shortModels(r.models)||"profile 变更")+"</span>";
      if(r.status==="http_error"||r.status==="error")return '<span class="hint">上游无响应 / 非 2xx 返回</span>';
      return '<span class="hint">—</span>'};

    el.innerHTML=rows.length?'<div class="cardbox">'+
      table(["时间","监控节点","状态","响应延迟","详情"],
        rows.map(r=>{const cls=r.status==="ok"?"ok":(r.status==="warm"||r.status==="probe")?"warn":(r.status==="switch")?"sw":"err";
          return ['<span class="mono nowr" title="'+esc(r.ts||"")+'" style="display:inline-flex;align-items:center;gap:6px"><span class="dot '+cls+'"></span>'+ftime(r.ts)+"</span>",
            '<span class="mono">'+esc(formatNodeId(r.profile))+"</span>",httpCell(r),latCell(r),detCell(r)]}),"fixed-cols tbl-probe")+"</div>"
      :'<div class="empty">'+WARN+'<div>暂无探测事件 — 还不能断言任何上游状态。</div>'+
       '<div class="hint" style="margin-top:6px;line-height:1.8">配置探测节点（二选一）：<br>'+
       '<code>~/.headroom/switcher-profiles.json</code> 或环境变量 <code>CV_RELAY_PROFILES=\'[{"id":"myrelay","openai_api_url":"https://host","default_model":"m"}]\'</code><br>'+
       '配置后点「立即探测上游」，或由托盘每 5 分钟自动探测。</div>'+
       '<button class="btn" onclick="probeNow()">立即探测上游</button></div>';
    el.dataset.loaded=1;
  }catch(e){
    if(e&&e.stale)return;
    el.innerHTML=errEmpty("哨兵事件加载失败："+e.message,"loadProbe()");
    $("#probe-state").className="probe-state err";
    $("#probe-state").innerHTML='<span class="dot err"></span>事件加载失败';
  }
}
let probing=false;
async function probeNow(){
  if(probing)return;probing=true;
  const b=$("#probe-btn");b&&(b.disabled=true);
  $("#probe-state").className="probe-state warn";
  $("#probe-state").innerHTML='<span class="dot warn"></span>探测中…';
  toast("探测中…");
  try{
    const r=await api("/probe",{method:"POST",headers:{"Content-Type":"application/json"}});
    const bad=r.alerts&&r.alerts.length;
    toast(bad?"发现异常："+r.alerts.map(a=>"["+a.profile+"] "+(a.kind==="model_missing"?"模型消失 "+JSON.stringify(a.detail):a.kind)).join("；")
      :"探测完成 — 本轮无新增异常",bad);
    delete $("#probe-list").dataset.loaded;loadProbe();
  }catch(e){
    toast("探测失败："+e.message,1);
    $("#probe-state").className="probe-state err";
    $("#probe-state").innerHTML='<span class="dot err"></span>探测请求失败';
  }
  b&&(b.disabled=false);probing=false;
}

/* ================= 启动 ================= */
const h0=(location.hash||"").slice(1);
setTab(TABS.includes(h0)?h0:"ov");
boot();
setInterval(()=>{
  if($("#t-probe").style.display!=="none"&&document.visibilityState==="visible"){
    delete $("#probe-list").dataset.loaded;loadProbe()}
},30000);