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

/* ================= 设置与运维 (MCP / Ollama 语义层 / 额度 Token / 备份与裁剪) ================= */
const opsSeq=seqGuard();
async function loadOps(probeSem){
  try{
    const [mcp,st]=await opsSeq(()=>Promise.all([
      api("/ops/mcp"),
      api("/ops/status"+(probeSem?"?probe=1":"")),
    ]));
    const mb=$("#ops-mcp-box");
    if(mb){
      mb.dataset.loaded="1";
      const rows=(mcp&&mcp.clients)||[];
      mb.innerHTML=
        rows.map(c=>'<div class="ops-row"><div class="ops-row-l">'+
          '<span class="dot '+(c.installed?"ok":(c.detected?"warn":"off"))+'"></span>'+
          '<span class="ops-row-name">'+esc(c.name)+'</span>'+
          '<span class="pill '+(c.installed?"ok":(c.detected?"warn":""))+'">'+(c.installed?"已接入":(c.detected?"已检测到 · 未接入":"未检测到"))+'</span>'+
          '<span class="ops-row-sub" title="'+esc(c.config||"")+'">'+esc(truncPath(c.config||"",34))+'</span></div>'+
          '<button class="btn sm" onclick="opsInstallMcp(\''+esc(c.id)+'\')">'+(c.installed?"重检":"接入")+'</button></div>').join("")+
        '<div class="hint" style="margin-top:8px;word-break:break-all">MCP 启动命令：<code class="mono">'+esc((mcp&&mcp.server_cmd)||"")+'</code></div>'+
        '<div id="ops-mcp-out"></div>';
    }
    const sb=$("#ops-sem-box");
    if(sb){
      const sem=(st&&st.semantic)||{};
      const emb=sem.embeddings||{},llm=sem.llm||{};
      const hostVal=(emb.url||"").replace(/\/v1\/embeddings.*$/,"")||"http://127.0.0.1:11434";
      sb.innerHTML=
        '<div class="ops-kv">'+
        '<b>向量重排</b><span>'+(emb.configured?('<span class="pill ok">已配置</span> '+esc(emb.model||"")+(emb.reachable===true?" · 在线":(emb.reachable===false?" · 不可达":""))):'<span class="pill">未开启 (纯本地零外发)</span>')+'</span>'+
        '<b>本地 LLM</b><span>'+(llm.configured?('<span class="pill ok">已配置</span> '+esc(llm.model||"")+(llm.reachable===true?" · 在线":(llm.reachable===false?" · 不可达":""))):'<span class="pill">未开启</span>')+'</span>'+
        '</div>'+
        '<div class="ops-form">'+
        '<div class="ops-form-row"><input id="ops-ol-host" value="'+esc(hostVal)+'" placeholder="Ollama 地址 (默认 http://127.0.0.1:11434)" aria-label="Ollama 地址"></div>'+
        '<div class="ops-form-row">'+
          '<input id="ops-ol-emb" value="'+esc(emb.model||"nomic-embed-text")+'" placeholder="Embedding 模型" aria-label="Embedding 模型">'+
          '<input id="ops-ol-llm" value="'+esc(llm.model||"qwen2.5:7b")+'" placeholder="LLM 模型" aria-label="LLM 模型">'+
        '</div></div>'+
        '<div class="ops-acts">'+
          '<button class="btn sm primary" onclick="opsConfigureOllama()">保存并启用 Ollama</button>'+
          '<button class="btn sm" onclick="opsSemanticRun(\'refine\')">LLM 精炼记忆</button>'+
          '<button class="btn sm" onclick="opsSemanticRun(\'titles\')">生成会话标题</button>'+
        '</div><div id="ops-sem-out"></div>';
    }
    const qb=$("#ops-quota-box");
    if(qb){
      qb.innerHTML=
        '<div class="ops-kv">'+
        '<b>智谱实时额度</b><span>'+(st&&st.plan_token_set?('<span class="pill ok">已配置</span> '+esc(st.plan_token_hint||"***")):'<span class="pill">未配置</span>')+'</span>'+
        '<b>自定义额度池</b><span>'+(st&&st.accounts_configured?'<span class="pill ok">vault/accounts.json 已生效</span>':'<span class="hint">按 7d 峰值纯消耗模式展示</span>')+'</span>'+
        '</div>'+
        '<div class="ops-form"><div class="ops-form-row">'+
          '<input type="password" id="ops-plan-tok" placeholder="粘贴智谱 Coding Plan Token（仅存本机 vault/plan_token.json）" aria-label="智谱 Coding Plan Token">'+
          '<button class="btn sm primary" onclick="opsSavePlanToken(false)">保存并校验</button>'+
          (st&&st.plan_token_set?'<button class="btn sm" onclick="opsSavePlanToken(true)">清除</button>':"")+
        '</div></div><div id="ops-quota-out"></div>';
    }
    const mt=$("#ops-maint-box");
    if(mt){
      const lb=st&&st.latest_backup;
      mt.innerHTML=
        '<div class="ops-kv">'+
        '<b>当前版本</b><span>v'+esc((st&&st.version)||"?")+(st&&st.frozen?" (Windows 独立托盘版)":" (源码模式)")+'</span>'+
        '<b>数据目录</b><span>'+esc((st&&st.vault)||"")+'</span>'+
        '<b>存储占用</b><span>vault.db '+((st&&st.db_mb)||0)+' MB · 倒排索引 '+((st&&st.index_mb)||0)+' MB</span>'+
        '<b>本地备份</b><span>'+(lb?(esc(lb.name)+' ('+lb.size_mb+' MB · 共 '+num(st.backups_count||0)+' 份)'):'暂无备份')+'</span>'+
        '</div>'+
        '<div class="ops-acts">'+
          '<button class="btn sm primary" id="ops-bk-btn" onclick="opsBackupNow()">立即备份 (zip)</button>'+
          '<button class="btn sm" onclick="opsPrune(false)">预览可裁剪版本</button>'+
          '<button class="btn sm danger" onclick="opsPrune(true)">执行裁剪 (自动先备份)</button>'+
        '</div><div id="ops-maint-out"></div>';
    }
  }catch(e){
    if(e&&e.stale)return;
    const mb=$("#ops-mcp-box");
    if(mb)mb.innerHTML=errEmpty("设置面板加载失败："+e.message,"loadOps(false)");
  }
}
async function opsInstallMcp(client){
  const out=$("#ops-mcp-out");
  try{
    const r=await api("/ops/mcp/install",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({client:client||"all"})});
    const lines=((r&&r.results)||[]).map(x=>"["+x.client+"] "+x.message).join("\n");
    toast("MCP 配置已执行");
    await loadOps(false);
    const out2=$("#ops-mcp-out");
    if(out2&&lines)out2.innerHTML='<div class="ops-out">'+esc(lines)+'</div>';
  }catch(e){
    toast("MCP 接入失败："+e.message,1);
    if(out)out.innerHTML='<div class="ops-out">'+esc(e.message)+'</div>';
  }
}
async function opsConfigureOllama(){
  const host=($("#ops-ol-host")&&$("#ops-ol-host").value||"").trim()||"http://127.0.0.1:11434";
  const emb=($("#ops-ol-emb")&&$("#ops-ol-emb").value||"").trim()||"nomic-embed-text";
  const llm=($("#ops-ol-llm")&&$("#ops-ol-llm").value||"").trim()||"qwen2.5:7b";
  try{
    await api("/semantic/config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ollama:true,host:host,embed_model:emb,llm_model:llm,enable_llm:true})});
    toast("已保存本地 Ollama 语义配置");
    await loadOps(true);
  }catch(e){toast("配置保存失败："+e.message,1)}
}
async function opsSemanticRun(kind){
  const out=$("#ops-sem-out");
  if(out)out.innerHTML='<div class="ops-out">正在执行，请稍候…</div>';
  try{
    const url=kind==="titles"?"/semantic/titles":"/semantic/refine";
    const r=await api(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({limit:15})});
    if(out)out.innerHTML='<div class="ops-out">'+esc(JSON.stringify(r,null,2))+'</div>';
    toast(kind==="titles"?"会话标题生成完成":"语义记忆精炼完成");
    loadOv();
  }catch(e){
    if(out)out.innerHTML='<div class="ops-out">'+esc(e.message)+'</div>';
    toast("执行失败："+e.message,1);
  }
}
async function opsSavePlanToken(clear){
  const tok=clear?"":(($("#ops-plan-tok")&&$("#ops-plan-tok").value)||"").trim();
  if(!clear&&!tok)return toast("请先粘贴 Token",1);
  try{
    const r=await api("/ops/plan_token",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(clear?{clear:true}:{token:tok,verify:true})});
    toast(clear?"已清除 Token":(r.probe&&r.probe.ok?"Token 已保存并验证通过":"Token 已保存"));
    await loadOps(false);loadOv();
  }catch(e){toast("保存失败："+e.message,1)}
}
async function opsLedgerSync(jumpOv){
  const btn=$("#ops-ledger-btn");if(btn)btn.disabled=true;
  try{
    const r=await api("/ops/ledger_sync",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    toast((r&&r.message)||"多端账本同步完成");
    const out=$("#ops-quota-out");if(out&&r&&r.message)out.innerHTML='<div class="ops-out">'+esc(r.message)+'</div>';
    loadOv();
    if(jumpOv)setTab("ov");
  }catch(e){toast("账本同步失败："+e.message,1)}
  if(btn)btn.disabled=false;
}
async function opsBackupNow(){
  const btn=$("#ops-bk-btn");if(btn){btn.disabled=true;btn.textContent="备份中…"}
  try{
    const r=await api("/ops/backup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({keep:10})});
    toast("备份完成："+(r.name||"zip"));
    await loadOps(false);
    const out=$("#ops-maint-out");if(out)out.innerHTML='<div class="ops-out">'+esc(r.message||r.path||"")+'</div>';
  }catch(e){toast("备份失败："+e.message,1)}
  if(btn){btn.disabled=false;btn.textContent="立即备份 (zip)"}
}
async function opsPrune(apply){
  if(apply&&!await askConfirm("将清理每个会话同日的冗余中间版本，并保留每日最后一版、最新叶子、fork 与孤本。\n执行前会自动创建一份完整 zip 备份。确认继续？"))return;
  const out=$("#ops-maint-out");
  if(out)out.innerHTML='<div class="ops-out">'+(apply?"正在先备份并裁剪…":"正在扫描可回收版本…")+'</div>';
  try{
    const r=await api("/ops/prune",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({apply:!!apply})});
    if(out)out.innerHTML='<div class="ops-out">'+esc(r.message||"")+'</div>';
    toast(apply?"版本裁剪完成":"可回收 "+num(r.drop_count||0)+" 版 / "+(r.reclaim_mb||0)+" MB");
    if(apply){await checkHealth();loadOv();await loadOps(false);}
  }catch(e){
    if(out)out.innerHTML='<div class="ops-out">'+esc(e.message)+'</div>';
    toast("裁剪失败："+e.message,1);
  }
}

/* ================= 启动 ================= */
const h0=(location.hash||"").slice(1);
setTab(TABS.includes(h0)?h0:"ov");
boot();
setInterval(()=>{
  if($("#t-probe").style.display!=="none"&&document.visibilityState==="visible"){
    delete $("#probe-list").dataset.loaded;loadProbe()}
},30000);
