/* ================= 设置与运维 (MCP / Ollama 语义层 / 额度 Token / 备份与裁剪) ================= */
const opsSeq=seqGuard();
let opsMcpSel=new Set(),opsMcpBooted=false,opsMcpShowMore=false;
function opsSyncMcpBtn(){
  const btn=$("#ops-mcp-apply-btn");if(!btn)return;
  const n=opsMcpSel.size;
  btn.textContent="接入已选 ("+n+")";
  btn.disabled=(n===0);
}
function opsMcpToggle(id,checked){
  if(!id)return;
  if(checked)opsMcpSel.add(id);else opsMcpSel.delete(id);
  opsSyncMcpBtn();
}
function renderMcpClients(mcp){
  const mb=$("#ops-mcp-box");if(!mb)return;
  mb.dataset.loaded="1";
  const rows=(mcp&&mcp.clients)||[];
  const validIds=new Set();
  if(!opsMcpBooted){
    opsMcpSel.clear();
    rows.forEach(c=>{
      const appDet=(c.app_detected!==undefined)?!!c.app_detected:(!!c.detected&&!c.residual);
      if(!c.installed&&!c.residual&&appDet)opsMcpSel.add(c.id);
    });
    opsMcpBooted=true;
  }else{
    rows.forEach(c=>{if(!c.installed&&c.detected)validIds.add(c.id);});
    opsMcpSel=new Set([...opsMcpSel].filter(id=>validIds.has(id)));
  }
  const renderRow=c=>{
    const appDet=(c.app_detected!==undefined)?!!c.app_detected:(!!c.detected&&!c.residual);
    const residual=!!(c.residual!==undefined?c.residual:(c.detected&&!appDet));
    let dotCls="off",pillCls="muted",statusTxt="未检测到",statusTip="";
    if(c.installed&&residual){
      dotCls="warn";pillCls="warn";statusTxt="已接入 · 未检测到程序";
      statusTip="未检测到主程序，仅存在配置目录或残留 MCP 配置；如已卸载可点击右侧「移除」清理 context-vault 条目";
    }else if(c.installed){
      dotCls="ok";pillCls="ok";statusTxt="已接入";
    }else if(residual){
      dotCls="off";pillCls="muted";statusTxt="仅残留目录";
      statusTip="仅检测到配置目录而未检测到主程序（如已卸载），默认不勾选；如需仍可手动勾选接入";
    }else if(c.detected){
      dotCls="warn";pillCls="warn";statusTxt="已检测到 · 未接入";
    }
    const chkChecked=c.installed||opsMcpSel.has(c.id);
    const chkDisabled=c.installed||!c.detected;
    const actBtn=c.installed
      ?'<button class="btn sm'+(residual?" danger":"")+'" onclick="opsUninstallMcp(\''+esc(c.id)+'\')">移除</button>'
      :'<button class="btn sm" onclick="opsInstallMcp(\''+esc(c.id)+'\')"'+(c.detected?"":" disabled")+'>接入</button>';
    return '<div class="ops-row"><div class="ops-row-l">'+
      '<input type="checkbox" class="ops-mcp-chk" aria-label="选择 '+esc(c.name)+'"'+
      (chkChecked?" checked":"")+(chkDisabled?" disabled":"")+
      ' onchange="opsMcpToggle(\''+esc(c.id)+'\',this.checked)">'+
      '<span class="dot '+dotCls+'"></span>'+
      '<span class="ops-row-name">'+esc(c.name)+'</span>'+
      '<span class="pill '+pillCls+'"'+(statusTip?' title="'+esc(statusTip)+'"':"")+'>'+statusTxt+'</span>'+
      '<span class="ops-row-sub" title="'+esc(c.config||"")+'">'+esc(truncPath(c.config||"",56))+'</span></div>'+
      actBtn+'</div>';
  };
  const isPrimary=c=>{
    const appDet=(c.app_detected!==undefined)?!!c.app_detected:(!!c.detected&&!c.residual);
    return !!(c.installed||appDet);
  };
  let primaryRows=rows.filter(isPrimary);
  let secondaryRows=rows.filter(c=>!isPrimary(c));
  if(!primaryRows.length){primaryRows=rows;secondaryRows=[];}
  const moreHtml=secondaryRows.length
    ?('<details class="ops-mcp-more"'+(opsMcpShowMore?" open":"")+' ontoggle="opsMcpShowMore=this.open">'+
        '<summary>其他受支持的客户端（未检测到程序 · '+secondaryRows.length+'）</summary>'+
        '<div class="ops-mcp-more-body">'+secondaryRows.map(renderRow).join("")+'</div></details>')
    :"";
  mb.innerHTML=
    '<div class="ops-mcp-note">勾选需要接入记忆库的本机客户端（未检测到或仅残留的客户端已默认折叠，已接入项可随时单独移除）。</div>'+
    primaryRows.map(renderRow).join("")+
    moreHtml+
    '<div class="hint" style="margin-top:8px;word-break:break-all">MCP 启动命令：<code class="mono">'+esc((mcp&&mcp.server_cmd)||"")+'</code></div>'+
    '<div id="ops-mcp-out"></div>';
  opsSyncMcpBtn();
}
let opsAuditKind="all";
function renderOpsBackupsList(bk){
  const items=(bk&&Array.isArray(bk.backups))?bk.backups:[];
  if(!items.length)return '<div class="hint" style="margin-top:8px">暂无历史 zip 备份</div>';
  return '<details class="ops-mcp-more ops-bk-more"><summary>历史备份清单（'+num(items.length)+' 份 · 校验与下载）</summary>'+
    '<div class="ops-mcp-more-body">'+
    items.slice(0,20).map(it=>{
      const ok=it.valid_zip&&it.has_db;
      const st=ok?'<span class="pill ok">完整</span>':'<span class="pill err">异常</span>';
      const qn=encodeURIComponent(it.name);
      return '<div class="ops-bk-row">'+
        '<div class="ops-bk-meta">'+st+' <code>'+esc(it.name)+'</code> · '+(it.size_mb||0)+' MB · '+num(it.snapshots_count||0)+' 快照</div>'+
        '<div class="ops-bk-acts">'+
          '<button class="btn xs" type="button" data-bk="'+esc(it.name)+'" onclick="opsVerifyBackup(this.dataset.bk)">校验</button>'+
          '<button class="btn xs" type="button" data-bk="'+esc(it.name)+'" onclick="opsDownloadBackup(this.dataset.bk)" data-dl="'+qn+'">下载</button>'+
        '</div></div>';
    }).join("")+
    '</div></details>';
}
function renderOpsAudit(audit){
  const box=$("#ops-audit-box");if(!box)return;
  $$("#ops-audit-filters button").forEach(b=>b.classList.toggle("on",(b.dataset.k||"all")===opsAuditKind));
  if(!audit){box.innerHTML='<div class="hint">审计数据暂不可用</div>';return}
  const s=audit.summary||{};
  const tl=Array.isArray(audit.timeline)?audit.timeline:[];
  const stPill=st=>{
    const v=String(st||"").toLowerCase();
    if(v==="ok"||v==="prune")return '<span class="pill ok">'+esc(st)+'</span>';
    if(v==="switch"||v==="warm")return '<span class="pill warn">'+esc(st)+'</span>';
    return '<span class="pill err">'+esc(st||"error")+'</span>';
  };
  let html='<div class="ops-audit-sum">'+
    '<span class="pill muted">总事件 '+num(audit.total_events||0)+'</span>'+
    '<span class="pill ok">正常 '+num(s.ok_probes||0)+'</span>'+
    '<span class="pill '+((s.abnormal||0)>0?"err":"muted")+'">异常 '+num(s.abnormal||0)+'</span>'+
    '<span class="pill '+((s.switches||0)>0?"warn":"muted")+'">切换 '+num(s.switches||0)+'</span>'+
    '<span class="pill '+((s.model_missing_events||0)>0?"warn":"muted")+'">模型缺失 '+num(s.model_missing_events||0)+'</span>'+
    '</div>';
  if(!tl.length){
    box.innerHTML=html+'<div class="hint">当前筛选条件暂无审计记录</div>';
    return;
  }
  html+=tl.slice(0,7).map(day=>{
    const evs=(day.events||[]).slice(0,12).map(ev=>{
      const detail=(ev.detail||(ev.missing_models&&ev.missing_models.length?("缺失模型: "+ev.missing_models.join(", ")):""))||"";
      return '<div class="ops-audit-ev">'+
        '<span class="ops-audit-ts">'+esc(ev.ts||"")+'</span>'+
        stPill(ev.status)+
        (ev.profile?'<code>'+esc(ev.profile)+'</code>':"")+
        (detail?'<span class="ops-audit-detail">'+esc(detail)+'</span>':"")+
        '</div>';
    }).join("");
    return '<div class="ops-audit-day"><div class="ops-audit-day-h">'+esc(day.day||"")+
      ' ('+num((day.events||[]).length)+' 条)</div>'+evs+'</div>';
  }).join("");
  box.innerHTML=html;
}
async function loadOpsAudit(kind){
  if(kind)opsAuditKind=kind;
  $$("#ops-audit-filters button").forEach(b=>b.classList.toggle("on",(b.dataset.k||"all")===opsAuditKind));
  try{
    const audit=await api("/audit?limit=80&kind="+encodeURIComponent(opsAuditKind));
    renderOpsAudit(audit);
  }catch(e){
    const box=$("#ops-audit-box");
    if(box)box.innerHTML='<div class="hint">审计时间线加载失败：'+esc((e&&e.message)||e)+'</div>';
  }
}
async function loadOps(probeSem){
  try{
    const [mcp,st,bk,audit]=await opsSeq(()=>Promise.all([
      api("/ops/mcp"),
      api("/ops/status"+(probeSem?"?probe=1":"")),
      api("/ops/backups").catch(()=>null),
      api("/audit?limit=80&kind="+encodeURIComponent(opsAuditKind)).catch(()=>null),
    ]));
    renderMcpClients(mcp);
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
          '<button class="btn sm" onclick="opsConfigureOllama()">保存并启用 Ollama</button>'+
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
          '<button class="btn sm" onclick="opsSavePlanToken(false)">保存并校验</button>'+
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
          '<button class="btn sm" id="ops-bk-btn" onclick="opsBackupNow()">立即备份 (zip)</button>'+
          '<button class="btn sm" onclick="opsPrune(false)">预览可裁剪版本</button>'+
          '<button class="btn sm danger" onclick="opsPrune(true)">执行裁剪 (自动先备份)</button>'+
        '</div>'+renderOpsBackupsList(bk)+'<div id="ops-maint-out"></div>';
    }
    renderOpsAudit(audit);
  }catch(e){
    if(e&&e.stale)return;
    const mb=$("#ops-mcp-box");
    if(mb)mb.innerHTML=errEmpty("设置面板加载失败："+e.message,"loadOps(false)");
  }
}
async function _postMcpOp(url,payload,okToast,errPrefix){
  const out=$("#ops-mcp-out");
  try{
    const r=await api(url,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)});
    const lines=((r&&r.results)||[]).map(x=>"["+x.client+"] "+x.message).join("\n");
    if(typeof skData!=="undefined"){skData=null;skSelected.clear();}
    toast(okToast);
    await loadOps(false);
    const out2=$("#ops-mcp-out");
    if(out2&&lines)out2.innerHTML='<div class="ops-out">'+esc(lines)+'</div>';
  }catch(e){
    toast(errPrefix+"："+e.message,1);
    if(out)out.innerHTML='<div class="ops-out">'+esc(e.message)+'</div>';
  }
}
function opsInstallMcp(client){
  if(Array.isArray(client)){
    if(!client.length)return toast("请先勾选要接入的客户端",1);
    return _postMcpOp("/ops/mcp/install",{clients:client},"MCP 已接入所选客户端","MCP 接入失败");
  }
  return _postMcpOp("/ops/mcp/install",{client:client||"all"},"MCP 配置已执行","MCP 接入失败");
}
function opsInstallSelectedMcp(){
  const ids=[...opsMcpSel];
  if(!ids.length)return toast("请先勾选要接入的客户端",1);
  return opsInstallMcp(ids);
}
async function opsUninstallMcp(client){
  if(!client)return;
  if(!await askConfirm("将从该客户端配置中安全移除 context-vault 条目（会自动备份原配置文件为 .bak-cv，不会删除其他 MCP 服务或配置目录）。确认继续？"))return;
  return _postMcpOp("/ops/mcp/uninstall",{client:client},"已移除 context-vault MCP","MCP 移除失败");
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
    const sum=kind==="titles"
      ?("生成会话标题：处理 "+num((r&&(r.processed||r.updated))||0)+" 条"+(r&&r.skipped!=null?" · 跳过 "+num(r.skipped):"")+(r&&r.errors?" · 失败 "+num(r.errors):""))
      :("语义记忆精炼：处理 "+num((r&&(r.refined||r.updated||r.processed||r.memories))||0)+" 条"+(r&&r.errors?" · 失败 "+num(r.errors):""));
    const raw=JSON.stringify(r,null,2);
    if(out)out.innerHTML='<div class="ops-out"><div>'+esc(sum)+'</div><details style="margin-top:6px"><summary style="cursor:pointer;color:var(--ink-3)">查看详细返回 JSON</summary><pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word">'+esc(raw)+'</pre></details></div>';
    toast(kind==="titles"?"会话标题生成完成":"语义记忆精炼完成");
    loadOv();
  }catch(e){
    if(out)out.innerHTML='<div class="ops-out">'+esc(e.message)+'</div>';
    toast("执行失败："+e.message,1);
  }
}
async function opsSavePlanToken(clear){
  if(clear&&!await askConfirm("确认清除本机保存的智谱 Coding Plan Token？（不影响本地用量统计，仅停用实时额度探测）"))return;
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
async function opsVerifyBackup(name){
  const out=$("#ops-maint-out");
  if(out)out.innerHTML='<div class="ops-out">正在校验 '+esc(name)+'…</div>';
  try{
    const r=await api("/ops/backup/verify?name="+encodeURIComponent(name));
    const msg=(r.ok?"校验通过: ":"校验失败: ")+esc(r.name)+
      " | CRC="+(r.crc_ok?"OK":"BAD")+
      " | vault.db="+esc(r.db_check||"unknown")+
      " | 快照="+num(r.zip_snapshots||0)+
      (r.sha256?"\nSHA-256: "+esc(r.sha256):"")+
      (r.error?"\n错误: "+esc(r.error):"");
    if(out)out.innerHTML='<div class="ops-out">'+msg+'</div>';
    toast(r.ok?("备份完整: "+r.name):("备份异常: "+(r.error||r.name)),!r.ok);
  }catch(e){
    if(out)out.innerHTML='<div class="ops-out">'+esc((e&&e.message)||e)+'</div>';
    toast("校验失败："+((e&&e.message)||e),1);
  }
}
async function opsDownloadBackup(name){
  const url="/ops/backup/download?name="+encodeURIComponent(name);
  try{
    if(typeof URL!=="undefined"&&typeof URL.createObjectURL==="function"&&typeof Blob!=="undefined"&&typeof document!=="undefined"&&typeof document.createElement==="function"){
      const r=await fetch(url);
      if(!r.ok){
        const j=await r.json().catch(()=>({}));
        throw new ApiError((j&&j.error)||("HTTP "+r.status),r.status);
      }
      const blob=await r.blob();
      const a=document.createElement("a");
      const objUrl=URL.createObjectURL(blob);
      a.href=objUrl;a.download=name;
      if(document.body&&document.body.appendChild)document.body.appendChild(a);
      a.click();
      if(a.remove)a.remove();
      setTimeout(()=>{try{URL.revokeObjectURL(objUrl)}catch(e){}},1000);
      toast("开始下载备份："+name);
      return;
    }
    toast("下载就绪："+url);
  }catch(e){toast("下载失败："+((e&&e.message)||e),1)}
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
