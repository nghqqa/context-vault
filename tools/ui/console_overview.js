/* ================= 概览 (全卡片并发渐进渲染, 任一慢接口绝不阻塞其余区块) ================= */
let ovGen=0,quotaPendingTimer=null;
function scheduleQuotaRefresh(gen,win){
  clearTimeout(quotaPendingTimer);
  quotaPendingTimer=setTimeout(async()=>{
    if(gen!==ovGen||win!==usageWindow)return;
    try{
      const u=await api("/ledger/usage?window="+encodeURIComponent(win));
      if(gen===ovGen&&win===usageWindow&&u)renderQuota(u,usageLedger);
    }catch(e){}
  },1200);
}
function openAgentSnaps(a){
  snapTreeMode=false;snapMemMode=false;updateSnapTreeBtn();
  snapsLoaded=true;
  setTab("snap");
  setSnapAgent(a);
}
async function loadOv(){
  const my=++ovGen;
  clearTimeout(quotaPendingTimer);
  $("#ov-band").innerHTML='<div class="kpi"><div class="skel" style="width:70%"></div></div>'.repeat(4);
  let ledgerCur=null,usageCur=null;

  const pLedger=api("/ledger").then(d=>{
    if(my!==ovGen)return;
    ledgerCur=d;usageLedger=d;
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
    const ob=$("#ov-onboard");
    if(ob){
      if(!(ow.snapshots||0)){
        ob.hidden=false;
        ob.innerHTML='<div class="onboard-card"><div class="onboard-hd"><b>欢迎使用 Context Vault — 3 步开启本机上下文资产库</b>'+
          '<span class="hint">全部数据保留在本机 · 零外发</span></div>'+
          '<div class="onboard-steps">'+
          '<div class="onboard-step"><div class="os-t">① 同步本机全部会话</div>'+
          '<div class="os-d">只读扫描 Claude Code / Codex / ZCode / Antigravity 历史会话与跨会话记忆。</div>'+
          '<button class="btn sm primary" onclick="syncVaultNow()">立即同步入库</button></div>'+
          '<div class="onboard-step"><div class="os-t">② 接入 AI 客户端 MCP</div>'+
          '<div class="os-d">让你的 AI 编程工具在任意新对话里直接检索本机历史决策与踩坑记录。</div>'+
          '<button class="btn sm" onclick="setTab(\'ops\')">一键接入 MCP →</button></div>'+
          '<div class="onboard-step"><div class="os-t">③ 同步多端 Token 账本</div>'+
          '<div class="os-d">离线统计 5h 滚动额度、冷却倒计时、缓存命中率与可选本地 Ollama 语义增强。</div>'+
          '<button class="btn sm" onclick="opsLedgerSync(true)">同步账本并查看 →</button></div>'+
          '</div></div>';
      }else{ob.hidden=true;ob.innerHTML="";}
    }
    const agEl=$("#ov-agents");
    if(agEl){
      const ags=(ow.by_agent||[]).slice().sort((a,b)=>{
        const sa=a.sessions!=null?a.sessions:(a.snapshots||0),sb=b.sessions!=null?b.sessions:(b.snapshots||0);
        return (sb-sa)||((b.tokens_in||0)-(a.tokens_in||0));
      }).slice(0,6);
      agEl.innerHTML=ags.length?ags.map(a=>{
        const sess=a.sessions!=null?a.sessions:(a.snapshots||0);
        const sub=(a.snapshots!=null&&a.snapshots!==sess)?(num(sess)+" 会话 · "+num(a.snapshots)+" 版本"):(num(sess)+" 独立会话");
        return '<div class="feed-row click" data-a="'+esc(String(a.agent||""))+'" onclick="openAgentSnaps(this.dataset.a)" title="查看该来源全部快照">'+
          '<span class="feed-main">'+agentCell(a.agent)+'<span class="feed-txt">'+sub+'</span></span>'+
          '<span class="feed-r">'+bn(a.tokens_in||0)+' tok</span></div>';
      }).join("")
        :'<div class="hint" style="padding:10px 14px">暂无多端会话 · 点击右上角「同步」采集本机会话</div>';
    }
    if(usageCur)renderQuota(usageCur,d);
  }).catch(e=>{
    if(my!==ovGen||(e&&e.stale))return;
    $("#ov-band").innerHTML=errEmpty("概览加载失败："+e.message,"loadOv()");
  });

  const pUsage=(async()=>{
    const w=usageWindow;
    let u5=null;
    try{u5=await api("/ledger/usage?window="+encodeURIComponent(w)+"&fast=1")}catch(e){u5=null}
    if(my!==ovGen||w!==usageWindow)return;
    usageCur=u5;
    renderQuota(u5,ledgerCur||usageLedger);
    if(u5&&u5.quota_pending)scheduleQuotaRefresh(my,w);
  })();

  const pRecent=(async()=>{
    let rc=null;try{rc=await api("/recent?limit=10&leaves=1")}catch(e){}
    if(my!==ovGen)return;
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
  })();

  const pMem=(async()=>{
    let mr=[];try{mr=await api("/memory?q=&limit=7");mr=(mr&&mr.memories)||[]}catch(e){mr=[]}
    if(my!==ovGen)return;
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
  })();

  await Promise.allSettled([pLedger,pUsage,pRecent,pMem]);
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
  try{const u=await usageSeq(()=>api("/ledger/usage?window="+encodeURIComponent(usageWindow)+"&fast=1"));
    if(u){renderQuota(u,usageLedger);if(u.quota_pending)scheduleQuotaRefresh(ovGen,usageWindow)}}catch(e){}
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
  const pre=u.precision||{};
  // 头部: 窗口切换 (5h/24h/7d/30d) + 窗口/全库/精确度精简摘要
  let h='<div class="q-head"><div class="q-head-l">'+
    '<div class="seg" id="ov-wseg" role="group" aria-label="统计窗口">'+
    USAGE_WINDOWS.map(([k,l])=>'<button class="btn'+(usageWindow===k?" on":"")+'" data-w="'+k+
      '" onclick="setUsageWindow(\''+k+'\')">'+l+"</button>").join("")+"</div>"+
    '<span class="hint">窗口 '+esc(usageWindow)+" 事件 "+num(t.events)+" ("+esc(wsrcs||"—")+
    ") · 全库 "+num(u.events_total)+"</span></div>"+
    '<div class="q-head-r"><span>精确 '+(pre.exact_events||0)+" / 估算 "+(pre.estimated_events||0)+
    " ("+((pre.estimated_share||0)*100).toFixed(0)+"%)</span>"+
    (u.accounts_configured?'<span class="mini-tag">accounts.json 已配</span>':'<span class="mini-tag" title="运行 cv ledger limit --list 配置额度上限">未配上限</span>')+
    "</div></div>";
  // 4 列紧凑 KPI 条 (一行排开, 避免和下方配额卡视觉混淆)
  h+='<div class="q-kpi-strip">'+
    aCell("窗口 "+esc(usageWindow)+" 事件",num(t.events)+' <span class="hint" style="font-size:11.5px;font-weight:400">'+esc(wsrcs||"—")+"</span>")+
    aCell("总输入",numT(t.total_input)+" <small>tokens</small>")+
    aCell("费用估算",(t.cost_est_usd?usd(t.cost_est_usd)+' <span class="mini-tag" title="按公开 API 挂牌价等效折算">等效折算</span>'
      :(costKnown?'<span class="hint">部分折算</span>':'<span class="mini-tag" title="未匹配到单价表 (vault/pricing.json); 订阅制模型默认不计成本">'+rqPlan+"</span>")))+
    aCell("缓存命中率",hit==null?"—":'<span>'+(hit*100).toFixed(1)+"%</span>"+
      ((u.cache_save_est_usd||0)>0?' <span class="hint" style="font-size:11.5px;font-weight:400">省 '+usd(u.cache_save_est_usd)+"</span>":'<span class="hint" style="font-size:11.5px;font-weight:400"> · 订阅</span>'))+
    "</div>";
  // 厂商实时额度: 紧凑高密度胶囊条 (奇数末项横跨整行, 彻底消除灰块空洞)
  const quotaDotCls=t=>{
    const s=String(t||"").toLowerCase();
    if(s.includes("argon"))return"arg";
    if(s.includes("claude")||s.includes("gpt"))return"cla";
    if(s.includes("gemini"))return"gem";
    return"zp";
  };
  const quotaRemoteBlock=(rq,title,tag,tip)=>{
    if(!rq||!rq.ok)return"";
    const LT={TOKENS_LIMIT:"5h Token 额度",TIME_LIMIT:"MCP 月额度",PROMPTS_LIMIT:"提示次数",REQUESTS_LIMIT:"请求数"};
    const lims=rq.limits||[];
    const odd=lims.length%2===1&&lims.length>1;
    return '<div class="q-group"><div class="q-group-hd">'+
      '<div class="q-group-title"><span>'+title+'</span><span class="q-plan-tag">'+esc(rq.plan||"?")+'</span></div>'+
      '<span class="q-live-tag" title="'+esc(tip)+'">'+tag+"</span></div>"+
      '<div class="q-pill-grid">'+lims.map((l,i)=>{
        const used=Math.max(0,Math.min(100,+(l.pct_used||0)));
        const rem=Math.max(0,100-used);
        const cls=used>=90?"q-hot":used>=70?"q-warn":"";
        const isArgon=String(l.type||"").toLowerCase().includes("argon");
        const spanCls=(odd&&i===lims.length-1)?" span-full":"";
        const argonCls=isArgon?" q-argon":"";
        const lbl=LT[l.type]||esc(l.type);
        const lTitle=l.desc?('<span class="q-pill-name" title="'+esc(l.desc)+'">'+lbl+"</span>"):('<span class="q-pill-name">'+lbl+"</span>");
        // 进度条按剩余额度填充 (越满越健康, 跌至低剩余自动转琥珀/警示红; 同时明示已用与剩余百分比)
        return '<div class="q-pill'+spanCls+argonCls+'">'+
          '<div class="q-pill-top">'+
            '<div class="q-pill-l"><span class="q-pill-dot '+quotaDotCls(l.type)+'"></span>'+lTitle+
              (l.reset_at?'<span class="q-pill-reset" title="下次额度重置时间">重置 '+etaRel(l.reset_at)+"</span>":"")+
            "</div>"+
            '<div class="q-pill-r">'+
              '<span class="q-pill-rem '+cls+'" title="剩余可用额度">余 '+rem.toFixed(1)+'%</span>'+
              '<span class="q-pill-used">已用 '+used.toFixed(1)+'%</span>'+
            "</div>"+
          "</div>"+
          '<span class="winbar'+(cls?" "+cls:"")+'"><i style="width:'+rem.toFixed(1)+'%"></i></span>'+
        "</div>";
      }).join("")+"</div></div>"};
  h+=quotaRemoteBlock(u.remote_quota,"套餐余额 (智谱实时)","实时",
    "CV_PLAN_TOKEN / vault/plan_token.json 配置后由智谱额度接口返回, 服务端 60s 缓存; Token 不出本机");
  h+=quotaRemoteBlock(u.remote_quota_agv,"Antigravity 额度 (本机 IDE)","LSP 实时",
    "经 LSP 官方发现文件 (daemon/hub_*.json) 直连本机 language_server 实时额度; IDE 登录态即凭据, 零配置");
  if((u.sources||{}).antigravity>0&&!(u.remote_quota_agv&&u.remote_quota_agv.ok)){
    h+='<div class="q-group" style="padding:8px 12px"><div class="hint"><b>Antigravity 额度 (本机 IDE)</b> — 暂不可用 ('+
      esc((u.remote_quota_agv||{}).error||"未发现")+') · IDE 未运行或未登录时仅显示下方本地估算消耗</div></div>'}
  // 5h 滚动窗口 (固定 5h 语义, 不随上方分析窗口切换): 放进独立轻边框卡片
  const accs=(u.fiveh&&u.fiveh.accounts)||[];
  const hasLimits=accs.some(a=>a.mode==="limit");
  const f5=(u.fiveh&&u.fiveh.totals)||{};
  h+='<div class="q-section"><div class="q-section-hd">'+
    '<span class="hint"><b>'+(hasLimits?"5 小时额度窗口 (本地额度池)":"5h 消耗构成 (按客户端归因)")+"</b> · "+
    (hasLimits?"上限与恢复 ETA 来自 accounts.json":"纯消耗模式: 进度按 7d 峰值/参考档位")+"</span>"+
    '<select id="ov-ref-tier" aria-label="参考档位" onchange="setRefTier(this.value)">'+
    REF_TIERS.map((v,i)=>'<option value="'+v+'"'+(usageRefTier===v?" selected":"")+">"+REF_TIERS_LBL[i]+"</option>").join("")+
    "</select></div>";
  h+=accs.length?accs.map(a=>{
    const isLim=a.mode==="limit"||a.limit;
    // 进度条真实填充: 有上限→上限占比; 无上限→参考档位占比 > 7d 单 5h 峰值比 > 窗口内占比
    let wPct,pctS;
    if(isLim){wPct=Math.min(100,(a.pct||0)*100);pctS=(a.pct*100).toFixed(1)+"%"}
    else if(usageRefTier){
      wPct=Math.min(100,(a.used_weighted||0)/usageRefTier*100);
      pctS=(a.used_weighted/usageRefTier*100).toFixed(1)+"%"}
    else if(a.pct_of_peak!=null){
      wPct=Math.min(100,a.pct_of_peak*100);pctS="峰值比 "+(a.pct_of_peak*100).toFixed(0)+"%"}
    else{wPct=Math.min(100,Math.round((a.share||0)*100));pctS="占比 "+Math.round((a.share||0)*100)+"%"}
    const barCls=wPct>=90?"winbar hot":wPct>=70?"winbar warn":"winbar";
    const usageS=isLim?(num(a.used_weighted)+" / "+num(a.limit)+" ("+esc(a.weight||"total_tokens")+")")
      :(bn(a.used_weighted)+" tok");
    let line='<div class="quota-row"><span class="qn" title="'+esc(a.account)+'">'+esc(a.account)+"</span>"+
      '<span class="'+barCls+'"><i style="width:'+wPct.toFixed(1)+'%"></i></span>'+
      '<span class="mono">'+pctS+"</span>"+
      '<span class="hint">'+usageS+"</span></div>";
    const subParts=[];
    if(a.eta&&(isLim?a.eta["50%"]!=null||a.eta["80%"]!=null||a.eta["100%"]!=null:true)){
      subParts.push('<span>'+(isLim?"恢复至 50%: ":"窗口回落 50%: ")+etaRel(a.eta["50%"])+
        " · 80%: "+etaRel(a.eta["80%"])+
        (isLim?" · 100%: ":" · 完全冷却: ")+etaRel(a.eta["100%"])+"</span>")}
    if(a.models&&a.models.length){
      subParts.push('<div class="q-model-chips">'+
        a.models.map(m=>'<span class="q-mchip">'+esc(m.name)+" "+Math.round((m.share||0)*100)+"% · "+bn(m.used)+"</span>").join("")+
        "</div>")}
    if(subParts.length)line+='<div class="q-sub-meta">'+subParts.join("")+"</div>";
    return line}).join("")
    :'<div class="hint" style="padding:2px 0">近 5 小时无消耗</div>';
  const rw=u.runway||[];
  if(rw.length){
    h+='<div class="hint" style="margin:8px 0 4px"><b>跑道</b> · 按近 24h 消耗速率外推距 7d/月上限剩余天数</div>';
    h+=rw.map(r=>{const cap=r.metric==="tokens"?num(r.cap)+" tok":usd(r.cap);
      const used=r.metric==="tokens"?num(r.used_tokens)+" tok":usd(r.used_usd);
      const dl=r.days_left!=null?r.days_left.toFixed(1)+" 天":"∞ (近 24h 零消耗)";
      return '<div class="quota-row"><span class="qn" title="'+esc(r.account)+'">'+esc(r.account)+"</span>"+
        '<span class="mini-tag">'+esc(r.kind)+"</span>"+
        '<span class="hint">上限 '+cap+" · 本期已用 "+used+" · 剩余跑道 <b>"+dl+"</b></span></div>"}).join("")}
  h+="</div>";
  // 消耗排行 (随窗口切换) + 30d 趋势: 标题与维度药丸同排对齐
  h+='<div class="q-section"><div class="q-section-hd">'+
    '<span class="hint"><b>消耗排行</b> · 窗口 '+esc(u.window||"5h")+
    ' <span class="mini-tag">含估算=来源无精确 usage</span></span>'+
    '<div id="ov-quota-rankseg" class="seg" role="group" aria-label="排行维度">'+
    USAGE_RANKS.map(([k,l])=>'<button class="btn'+(usageRankKey===k?" on":"")+'" data-k="'+k+
      '" onclick="setUsageRank(\''+k+'\')">'+l+"</button>").join("")+"</div></div>";
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
  h+="</div>";
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

