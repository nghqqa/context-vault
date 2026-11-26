/* ================= Skill 技能跨工具迁移 (与「设置与运维」已接入 MCP 后端强一致) ================= */
let migMode="session",skSource="zcode",skTarget="antigravity",skAgyMode="skills";
let skData=null,skSelected=new Set(),skPluginEdited=false,skBackups=null,skManageOpen=false,skManageTab="target";
const skSeq=seqGuard();
function isSkillSameOrigin(){
  return !!(skSource&&skTarget&&skTarget===skSource&&!(skTarget==="antigravity"&&skAgyMode==="plugin"));
}
const SK_TARGET_META={
  antigravity:{title:"Antigravity",desc:"全局目录或独立插件包"},
  claude:{title:"Claude Code",desc:"~/.claude/skills/"},
  codex:{title:"Codex",desc:"~/.codex/skills/"},
  cursor:{title:"Cursor",desc:"~/.cursor/skills/"},
  zcode:{title:"ZCode",desc:"~/.zcode/skills/（双向互迁）"},
  dsh:{title:"DeepSeek Harness",desc:"~/.dsh/skills/（双向互迁）"},
};
function setMigMode(m){
  migMode=(m==="skills")?"skills":"session";
  const isSk=(migMode==="skills");
  const sp=$("#mig-session-pane"),kp=$("#mig-skills-pane");
  if(sp)sp.hidden=isSk;
  if(kp)kp.hidden=!isSk;
  const isBatch=(typeof migScope!=="undefined"&&migScope==="batch");
  const b1=$("#mig-scope-single"),b2=$("#mig-scope-batch"),bk=$("#mig-tab-skills");
  if(b1){const on=!isSk&&!isBatch;b1.classList.toggle("on",on);b1.setAttribute("aria-selected",on?"true":"false")}
  if(b2){const on=!isSk&&isBatch;b2.classList.toggle("on",on);b2.setAttribute("aria-selected",on?"true":"false")}
  if(bk){bk.classList.toggle("on",isSk);bk.setAttribute("aria-selected",isSk?"true":"false")}
  const mh=$("#mig-mode-hint");
  if(mh)mh.textContent=isSk
    ?"批量搬运已接入后端的本地 SKILL.md 技能包（支持目录展平、Frontmatter 规范化与 Antigravity 双模式）"
    :(isBatch
      ?"按整个 Harness 批量盘点并迁出主对话（支持按项目/已迁移/同源自动去重，一键落 outbox 或写入真实目录）"
      :"单条精调可预览活上下文、逐项失真与精确恢复命令");
  if(isSk&&!skData)loadSkills({refresh:true});
}
function skVisibleList(){
  const all=(skData&&skData.skills)||[];
  const q=(($("#sk-q")&&$("#sk-q").value)||"").trim().toLowerCase();
  if(!q)return all;
  return all.filter(s=>(s.slug||"").toLowerCase().includes(q)
    ||(s.raw_name||"").toLowerCase().includes(q)
    ||(s.description||"").toLowerCase().includes(q)
    ||(s.group||"").toLowerCase().includes(q));
}
async function loadSkills(opts){
  const o=opts||{};
  const listEl=$("#sk-list");
  if(listEl){
    if(o.soft&&skData)listEl.style.opacity="0.65";
    else listEl.innerHTML='<div style="padding:12px"><div class="skel" style="width:60%"></div><div class="skel" style="width:78%"></div></div>';
  }
  const inc=!!($("#sk-inc-plug")&&$("#sk-inc-plug").checked);
  const pnEl=$("#sk-plugin-name");
  if(pnEl&&!skPluginEdited&&skSource)pnEl.value="migrated-from-"+skSource;
  const pname=(pnEl&&pnEl.value||"").trim();
  const qs=["source="+encodeURIComponent(skSource||"zcode"),"target="+encodeURIComponent(skTarget||"antigravity"),
    "include_plugins="+(inc?1:0),"agy_mode="+encodeURIComponent(skAgyMode),"connected_only=1"];
  if(skTarget==="antigravity"&&skAgyMode==="plugin"&&pname)qs.push("plugin_name="+encodeURIComponent(pname));
  if(o.refresh)qs.push("refresh=1");
  try{
    const d=await skSeq(()=>api("/skills/list?"+qs.join("&")));
    if(listEl)listEl.style.opacity="";
    skData=d;
    const srcs=(d&&d.sources)||[];
    if(d&&d.source)skSource=d.source;
    else if(!srcs.length)skSource="";
    if(d&&d.target)skTarget=d.target;
    if(pnEl&&!skPluginEdited&&skSource)pnEl.value="migrated-from-"+skSource;
    const validIds=new Set(((d&&d.skills)||[]).map(s=>s.id));
    if(skSelected.size){
      skSelected=new Set([...skSelected].filter(id=>validIds.has(id)));
    }else if(!inc){
      ((d&&d.skills)||[]).filter(s=>s.target_status==="new").forEach(s=>skSelected.add(s.id));
    }
    renderSkillSources();
    renderSkillTargets();
    renderSkillList();
    updateSkillBudget();
    renderSkillManage();
  }catch(e){
    if(listEl)listEl.style.opacity="";
    if(e&&e.stale)return;
    if(listEl)listEl.innerHTML=errEmpty("Skill 扫描失败："+e.message,"loadSkills({refresh:true})");
  }
}
function renderSkillSources(){
  const bar=$("#sk-src-bar");if(!bar)return;
  const srcs=((skData&&skData.sources)||[]).filter(s=>{
    const total=(s.total!=null)?Number(s.total):((s.standalone||0)+(s.plugins||0));
    return (s.mcp_connected!==false)&&total>0;
  });
  const names={zcode:"ZCode",claude:"Claude Code",codex:"Codex",antigravity:"Antigravity",cursor:"Cursor",dsh:"DeepSeek Harness"};
  bar.innerHTML=srcs.map(s=>{
    const on=(s.agent===skSource);
    const cnt=s.standalone+(s.plugins?("+"+s.plugins):"");
    return '<button type="button" class="pill-btn'+(on?' on':'')+'" onclick="setSkillSource(\''+esc(s.agent)+'\')">'+
      esc(names[s.agent]||s.agent)+' <span class="mono" style="opacity:.78">'+esc(String(cnt))+'</span></button>';
  }).join("");
}
function renderSkillTargets(){
  const wrap=$("#sk-tgt-cards");if(!wrap)return;
  const order=["antigravity","claude","codex","cursor","zcode","dsh"];
  const conns=new Set((skData&&skData.connected_agents)||[]);
  const visible=order.filter(id=>conns.has(id));
  if(!visible.length){
    wrap.innerHTML='<div class="hint" style="grid-column:1 / -1;padding:6px 0">暂无已接入 MCP 后端的目标客户端 · <button type="button" class="ch-link" onclick="setTab(\'ops\')">前往设置与运维接入 →</button></div>';
    const ab=$("#sk-agy-opts");if(ab)ab.style.display="none";
    return;
  }
  if(!conns.has(skTarget))skTarget=visible[0];
  wrap.innerHTML=visible.map(id=>{
    const m=SK_TARGET_META[id]||{title:id,desc:""};
    const on=(id===skTarget);
    const same=(id===skSource&&!(id==="antigravity"&&skAgyMode==="plugin"));
    return '<label class="tgt-card'+(on?" on":"")+'"><input type="radio" name="sk-tgt" value="'+esc(id)+'"'+(on?" checked":"")+
      ' onchange="onSkillTargetChange(\''+esc(id)+'\')">'+
      '<span><span class="tt">'+esc(m.title)+(same?' <span class="pill muted">同源</span>':"")+'</span><br><span class="td">'+esc(m.desc)+'</span></span></label>';
  }).join("");
  const ab=$("#sk-agy-opts");
  if(ab)ab.style.display=(skTarget==="antigravity")?"":"none";
}
function setSkillSource(ag){
  skSource=ag;skSelected.clear();skPluginEdited=false;
  const conns=(skData&&skData.connected_agents)||[];
  if(skTarget===ag&&!(ag==="antigravity"&&skAgyMode==="plugin")){
    const alt=conns.find(a=>a!==ag);
    if(alt)skTarget=alt;
  }
  $("#sk-result")&&($("#sk-result").innerHTML="");
  loadSkills();
}
function onSkillTargetChange(val){
  if(val){skTarget=val;}
  else{
    const r=document.querySelector('input[name="sk-tgt"]:checked');
    if(r&&r.value)skTarget=r.value;
  }
  $$("#sk-tgt-cards .tgt-card").forEach(c=>{
    const inp=c.querySelector("input");
    c.classList.toggle("on",!!(inp&&inp.value===skTarget));
  });
  const ab=$("#sk-agy-opts");
  if(ab)ab.style.display=(skTarget==="antigravity")?"":"none";
  $("#sk-result")&&($("#sk-result").innerHTML="");
  loadSkills({soft:true});
}
function onSkillAgyModeChange(){
  const r=document.querySelector('input[name="sk-agy-mode"]:checked');
  skAgyMode=r?r.value:"skills";
  $$("#sk-agy-opts .sk-mode-card").forEach(c=>c.classList.toggle("on",c.querySelector("input").value===skAgyMode));
  const pr=$("#sk-plugin-row");
  if(pr)pr.style.display=(skAgyMode==="plugin")?"flex":"none";
  const pn=$("#sk-plugin-name");
  if(pn){pn.oninput=()=>{skPluginEdited=true};if(!pn.value)pn.value="migrated-from-"+skSource}
  $("#sk-result")&&($("#sk-result").innerHTML="");
  loadSkills({soft:true});
}
function toggleSkillItem(id,checked){
  if(checked)skSelected.add(id);else skSelected.delete(id);
  updateSkillBudget();
}
function skSelectBatch(mode){
  const vis=skVisibleList();
  if(mode==="none"){skSelected.clear();}
  else if(mode==="visible"){vis.forEach(s=>skSelected.add(s.id));}
  else if(mode==="new"){skSelected.clear();vis.filter(s=>s.target_status==="new").forEach(s=>skSelected.add(s.id));}
  renderSkillList();
  updateSkillBudget();
}
function renderSkillList(){
  const listEl=$("#sk-list");if(!listEl)return;
  const conns=(skData&&skData.connected_agents)||[];
  const srcs=(skData&&skData.sources)||[];
  if(!conns.length){
    listEl.innerHTML='<div class="empty"><span>尚未接入任何 MCP 记忆后端，暂不展示或迁移 Skill</span>'+
      '<button type="button" class="btn sm primary" onclick="setTab(\'ops\')">前往设置与运维接入 →</button></div>';
    updateSkillBudget();return;
  }
  if(!srcs.length){
    listEl.innerHTML='<div class="empty"><span>当前已接入的客户端均未发现本地 Skill（0 个）</span></div>';
    updateSkillBudget();return;
  }
  const vis=skVisibleList();
  if(!vis.length){
    listEl.innerHTML='<div class="hint" style="padding:18px;text-align:center">当前筛选条件下没有匹配的 Skill</div>';
    updateSkillBudget();return;
  }
  const stMeta={new:["ok","新增"],identical:["","已一致"],conflict:["warn","同名冲突"]};
  listEl.innerHTML=vis.map(s=>{
    const ck=skSelected.has(s.id)?" checked":"";
    const sm=stMeta[s.target_status]||["",s.target_status||""];
    const wTip=(s.warnings&&s.warnings.length)?(' <span class="pill warn" title="'+esc(s.warnings.join("；"))+'">⚠ '+s.warnings.length+'</span>'):"";
    const gTag=s.scope==="plugin"?(' <span class="chip">'+esc(s.group||"plugin")+'</span>'):"";
      return '<label class="sk-row"><input type="checkbox"'+ck+' onchange="toggleSkillItem(\''+esc(s.id)+'\',this.checked)">'+
      '<div class="sk-row-b"><div class="sk-row-top">'+
      '<span class="sk-slug">'+esc(s.slug)+'</span>'+
      '<span class="pill '+sm[0]+'">'+sm[1]+'</span>'+gTag+wTip+
      '<span class="sk-tok">~'+num(s.prompt_tokens)+' tok · '+num(s.files)+' 文件</span>'+
      '<button type="button" class="btn sm sk-inspect-btn'+(s.target_status==="conflict"?" warn":"")+
      '" onclick="event.preventDefault();event.stopPropagation();inspectSkill(\''+esc(s.id)+'\')">'+
      (s.target_status==="conflict"?"对比差异":"预览")+'</button></div>'+
      '<div class="sk-desc">'+esc(s.description||"")+'</div></div></label>';
  }).join("");
  updateSkillBudget();
}
function updateSkillBudget(){
  const all=(skData&&skData.skills)||[];
  const conns=(skData&&skData.connected_agents)||[];
  const srcs=(skData&&skData.sources)||[];
  const sameOrigin=isSkillSameOrigin();
  const noCan=!conns.length||!srcs.length||!all.length||sameOrigin;
  ["#sk-dry-btn","#sk-apply-btn"].forEach(s=>{const b=$(s);if(b)b.disabled=noCan;});
  const picked=all.filter(s=>skSelected.has(s.id));
  const tok=picked.reduce((a,s)=>a+(s.prompt_tokens||0),0);
  const stEl=$("#sk-sel-stat");
  if(stEl)stEl.textContent="已选 "+num(picked.length)+" / "+num(all.length)+" 个";
  const box=$("#sk-budget-box");if(!box)return;
  if(!conns.length){
    box.className="sk-budget";
    box.innerHTML='<div class="hint">请先在「设置与运维 → MCP 记忆后端」接入至少一个客户端后再迁移 Skill。</div>';
    return;
  }
  if(!srcs.length){
    box.className="sk-budget";
    box.innerHTML='<div class="hint">当前已接入客户端暂无可迁移的本地 Skill。</div>';
    return;
  }
  const heavy=(picked.length>30||tok>4000);
  box.className="sk-budget"+((heavy||sameOrigin)?" warn":"");
  const modeTxt=(skTarget==="antigravity")
    ?(skAgyMode==="plugin"?"Antigravity 独立插件包（可一键开关）":"Antigravity 全局目录")
    :(SK_TARGET_META[skTarget]?SK_TARGET_META[skTarget].title:skTarget);
  box.innerHTML=
    '<div><b>目标：</b><span class="mono">'+esc(modeTxt)+'</span> → <span class="mono" style="word-break:break-all">'+esc((skData&&skData.dest_root)||"—")+'</span></div>'+
    '<div><b>已选：</b>'+num(picked.length)+' 个 Skill · 预估常驻 System Prompt <b>~'+num(tok)+' tokens</b></div>'+
    (sameOrigin?'<div style="margin-top:4px">⚠ 当前目标与源端目录相同，无法原地覆盖（请切换其他目标，或选 Antigravity 打包为独立插件）。</div>':"")+
    (heavy?'<div style="margin-top:4px">⚠ 本次勾选项较多，可能增加每轮系统提示词开销；建议按需挑选常用 Skill，或切换为 Antigravity 独立插件模式便于随时整体关闭。</div>':"");
}
async function skRunMigrate(apply){
  const ids=[...skSelected];
  if(!ids.length)return toast("请先勾选至少一个要迁移的 Skill",1);
  if(apply){
    const modeDesc=(skTarget==="antigravity"&&skAgyMode==="plugin")
      ?("Antigravity 独立插件 ("+(($("#sk-plugin-name")&&$("#sk-plugin-name").value)||("migrated-from-"+skSource))+")")
      :((skData&&skData.dest_root)||skTarget);
    if(!await askConfirm("将把 "+ids.length+" 个 Skill 迁移到:\n"+modeDesc+"\n\n源端文件严格只读，不会被修改。确认写入？"))return;
  }
  const btns=[$("#sk-dry-btn"),$("#sk-apply-btn")];
  btns.forEach(b=>b&&(b.disabled=true));
  $("#sk-result").innerHTML='<div class="skel" style="width:60%"></div>';
  try{
    const r=await api("/skills/migrate",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        source:skSource,target:skTarget,selected:ids,
        include_plugins:!!($("#sk-inc-plug")&&$("#sk-inc-plug").checked),
        agy_mode:skAgyMode,
        plugin_name:(($("#sk-plugin-name")&&$("#sk-plugin-name").value)||"").trim(),
        conflict:($("#sk-conflict")&&$("#sk-conflict").value)||"skip",
        normalize:!!($("#sk-norm")&&$("#sk-norm").checked),
        apply:!!apply,
        connected_only:true,
      })});
    const c=r.counts||{};
    const actLbl=r.applied
      ?{copy:"已写入",identical:"已一致跳过",conflict_skip:"冲突跳过",overwrite:"已覆盖",rename:"已重命名写入"}
      :{copy:"待复制",identical:"已一致跳过",conflict_skip:"冲突跳过",overwrite:"待覆盖",rename:"待重命名"};
    const rowsHtml=(r.items||[]).slice(0,80).map(it=>{
      const cls=(it.action==="conflict_skip"||it.warnings.length)?"warn":(it.action==="identical"?"":"ok");
      const notes=[...(it.rewrites||[]),...(it.warnings||[]).map(w=>"⚠ "+w)].join(" · ");
      return '<div class="sk-res-row"><span class="pill '+cls+'">'+esc(actLbl[it.action]||it.action)+'</span>'+
        '<span class="mono">'+esc(it.slug)+(it.target_slug!==it.slug?(" → "+esc(it.target_slug)):"")+'</span>'+
        (notes?'<span class="hint">'+esc(notes)+'</span>':"")+'</div>';
    }).join("");
    $("#sk-result").innerHTML=
      '<div class="result-card"><div class="rt">'+GOOD+(r.applied?"Skill 迁移已写入目标目录":"Skill 迁移预演报告（dry-run，未改动磁盘）")+'</div>'+
      '<div class="path-row"><span class="hint">目标目录:</span><span class="mono" style="word-break:break-all">'+esc(r.dest_root||"")+'</span>'+
      '<button class="btn sm" data-v="'+esc(r.dest_root||"")+'" onclick="copyData(this,\'已复制目标目录\')">复制目录</button></div>'+
      (r.plugin_manifest?'<div class="hint">插件清单：<span class="mono">'+esc(r.plugin_manifest)+'</span>（在 Antigravity 插件面板可一键启用/停用）</div>':"")+
      '<div class="hint">选中 '+num(c.selected)+' · '+(r.applied?"已写入 ":"计划写入 ")+num(r.applied?c.migrated:c.planned_copy)+
      ' · 已一致跳过 '+num(c.skipped_identical)+' · 冲突跳过 '+num(c.skipped_conflict)+
      ' · 规范化 '+num(c.rewrites)+' · 源路径告警 '+num(c.warnings)+' · 预计 ~'+num(r.prompt_tokens_est)+' tokens</div>'+
      (r.budget_warning?'<div class="hint" style="color:var(--warn);margin-top:4px">⚠ '+esc(r.budget_warning)+'</div>':"")+
      (rowsHtml?'<div class="sk-res-tbl">'+rowsHtml+'</div>':"")+'</div>';
    toast(r.applied?("已迁移 "+num(c.migrated)+" 个 Skill"):"预演完成：计划写入 "+num(c.planned_copy)+" 个 Skill");
    if(r.applied){
      if(r.backup_id)skBackups=null;
      await loadSkills({refresh:true});
      if(skManageOpen)await loadSkillBackups();
    }
  }catch(e){
    $("#sk-result").innerHTML='<div class="result-card err"><div class="rt">'+ERRIC+'Skill 迁移失败</div><div class="hint">'+esc(e.message)+'</div></div>';
    toast("Skill 迁移失败："+e.message,1);
  }
  btns.forEach(b=>b&&(b.disabled=false));
  updateSkillBudget();
}
function openSkillDrawer(){
  const d=$("#sk-drawer");
  if(d&&!d.open)d.showModal();
}
function closeSkillDrawer(){
  const d=$("#sk-drawer");
  if(d&&d.open)d.close();
}
function _renderDiffHtml(d){
  if(!d.target_exists)return '<div class="hint">目标目录尚无同名 Skill，本次迁入为全新复制。</div>';
  if(d.target_status==="identical")return '<div class="hint">✅ 目标目录内容与待迁入版本完全一致，无需变更。</div>';
  const chips=[];
  (d.files_added||[]).forEach(f=>chips.push('<span class="pill ok">+ '+esc(f)+'</span>'));
  (d.files_changed||[]).forEach(f=>chips.push('<span class="pill warn">~ '+esc(f)+'</span>'));
  (d.files_removed||[]).forEach(f=>chips.push('<span class="pill err">- '+esc(f)+'</span>'));
  const lines=(d.diff_lines||[]).map(ln=>{
    let cls="meta";
    if(ln.startsWith("+++")||ln.startsWith("---"))cls="meta";
    else if(ln.startsWith("@@"))cls="hunk";
    else if(ln.startsWith("+"))cls="add";
    else if(ln.startsWith("-"))cls="del";
    return '<div class="sk-diff-ln '+cls+'">'+esc(ln)+'</div>';
  }).join("");
  return (chips.length?'<div class="dw-chips">'+chips.join("")+'</div>':"")+
    (lines?'<div class="sk-diff-pre" style="margin-top:8px">'+lines+
      (d.diff_truncated?'<div class="sk-diff-ln meta">…（Diff 已截断至 400 行）</div>':"")+'</div>'
      :'<div class="hint">SKILL.md 文本一致，差异来自附属文件增删或改动。</div>');
}
async function inspectSkill(skillId){
  openSkillDrawer();
  $("#sk-dw-title").textContent="正在读取 Skill 详情…";
  $("#sk-dw-sub").textContent=String(skillId||"");
  $("#sk-dw-body").innerHTML='<div class="skel" style="width:65%"></div><div class="skel" style="width:85%"></div>';
  const inc=!!($("#sk-inc-plug")&&$("#sk-inc-plug").checked);
  const norm=!!($("#sk-norm")&&$("#sk-norm").checked);
  const pname=(($("#sk-plugin-name")&&$("#sk-plugin-name").value)||"").trim();
  const qs=[
    "source="+encodeURIComponent(skSource||"zcode"),
    "id="+encodeURIComponent(skillId||""),
    "include_plugins="+(inc?1:0),
    "normalize="+(norm?1:0),
    "connected_only=1",
  ];
  if(skTarget){
    qs.push("target="+encodeURIComponent(skTarget));
    qs.push("agy_mode="+encodeURIComponent(skAgyMode));
    if(skTarget==="antigravity"&&skAgyMode==="plugin"&&pname)qs.push("plugin_name="+encodeURIComponent(pname));
  }
  try{
    const d=await api("/skills/inspect?"+qs.join("&"));
    const sk=d.skill||{};
    $("#sk-dw-title").textContent=(sk.slug||"Skill")+" · 预览与差异";
    $("#sk-dw-sub").textContent=(sk.path||"")+(d.dest_path?("  ->  "+d.dest_path):"");
    const rwHtml=(d.rewrites&&d.rewrites.length)?('<div class="hint" style="margin-top:6px">Frontmatter 规范化：'+esc(d.rewrites.join(" · "))+'</div>'):"";
    const warnHtml=(sk.warnings&&sk.warnings.length)?('<div class="hint" style="color:var(--warn);margin-top:4px">⚠ '+esc(sk.warnings.join("；"))+'</div>'):"";
    const filesHtml=(d.source_files||[]).map(f=>'<span class="chip">'+esc(f.rel)+' ('+num(f.size)+'B)</span>').join(" ");
    $("#sk-dw-body").innerHTML=
      '<div class="dw-chips">'+
      '<span class="pill">'+esc((sk.source||"")+" / "+(sk.scope||""))+'</span>'+
      '<span class="pill">~'+num(sk.prompt_tokens||0)+' tok</span>'+
      '<span class="pill">'+num(sk.files||0)+' 文件 · '+esc(String(sk.size_kb||0))+' KB</span>'+
      (d.target?'<span class="pill '+(d.target_status==="conflict"?"warn":(d.target_status==="new"?"ok":""))+'">'+esc(d.target_status||"")+'</span>':"")+
      '</div>'+rwHtml+warnHtml+
      (d.target?('<h3 class="sec">与目标端差异（'+esc(d.target)+'）</h3>'+_renderDiffHtml(d)):"")+
      '<h3 class="sec" style="display:flex;align-items:center;justify-content:space-between"><span>迁入版 SKILL.md 预览</span>'+
      '<button type="button" class="btn sm" data-v="'+esc(d.planned_skill_md||"")+'" onclick="copyData(this,\'已复制 SKILL.md\')">复制全文</button></h3>'+
      '<pre class="sk-md-pre">'+esc(d.planned_skill_md||"")+'</pre>'+
      (filesHtml?('<h3 class="sec">目录文件清单</h3><div class="dw-chips">'+filesHtml+'</div>'):"");
  }catch(e){
    $("#sk-dw-body").innerHTML=errEmpty("Skill 详情读取失败："+e.message,"");
  }
}
function onSkillManageToggle(open){
  skManageOpen=!!open;
  renderSkillManage();
  if(skManageOpen&&skBackups===null)loadSkillBackups();
}
function setSkillManageTab(tab){
  skManageTab=(tab==="backups")?"backups":"target";
  const bt=$("#sk-mg-tab-tgt"),bb=$("#sk-mg-tab-bak");
  const lt=$("#sk-tgt-list"),lb=$("#sk-bak-list");
  if(bt){bt.classList.toggle("on",skManageTab==="target");bt.setAttribute("aria-selected",skManageTab==="target"?"true":"false");}
  if(bb){bb.classList.toggle("on",skManageTab==="backups");bb.setAttribute("aria-selected",skManageTab==="backups"?"true":"false");}
  if(lt)lt.hidden=(skManageTab!=="target");
  if(lb)lb.hidden=(skManageTab!=="backups");
  if(skManageTab==="backups"&&skBackups===null)loadSkillBackups();
}
function renderSkillManage(){
  const tList=(skData&&skData.target_skills)||[];
  const tc=$("#sk-tgt-cnt");if(tc)tc.textContent=num(tList.length);
  const tgtEl=$("#sk-tgt-list");
  if(tgtEl){
    tgtEl.innerHTML=tList.length?tList.map(it=>
      '<div class="sk-mg-row"><div class="sk-mg-main">'+
      '<div class="sk-mg-title">'+esc(it.slug)+' <span class="hint mono">'+num(it.files||0)+' 文件 · '+esc(String(it.size_kb||0))+' KB</span></div>'+
      '<div class="sk-mg-sub">'+esc(it.description||"")+'</div></div>'+
      '<button type="button" class="btn sm danger" onclick="removeSkillTarget(\''+esc(it.slug)+'\')">安全移除</button></div>'
    ).join(""):'<div class="hint" style="padding:10px 12px">当前目标目录暂无已安装的 Skill。</div>';
  }
  const bks=skBackups||[];
  const bc=$("#sk-bak-cnt");if(bc)bc.textContent=num(bks.length);
  const bakEl=$("#sk-bak-list");
  if(bakEl){
    if(skBackups===null){
      bakEl.innerHTML='<div class="hint" style="padding:10px 12px">正在加载 Skill 历史备份…</div>';
    }else if(!bks.length){
      bakEl.innerHTML='<div class="hint" style="padding:10px 12px">暂无备份记录（执行覆盖或安全移除时会自动备份至 vault/backups/）。</div>';
    }else{
      bakEl.innerHTML=bks.map(b=>{
        const kindTxt=b.kind==="remove"?"移除前备份":(b.kind==="overwrite"?"覆盖前备份":"早期目录备份");
        const act=b.restorable
          ?'<button type="button" class="btn sm" onclick="rollbackSkillBackup(\''+esc(b.id)+'\')">恢复备份</button>'
          :'<span class="pill muted">仅目录</span>';
        return '<div class="sk-mg-row"><div class="sk-mg-main">'+
          '<div class="sk-mg-title">'+esc(b.id)+' <span class="pill">'+esc(kindTxt)+'</span></div>'+
          '<div class="sk-mg-sub">'+esc((b.target||"-")+" · "+num(b.count||0)+" 项 · "+String(b.created_at||"").slice(0,19))+'</div></div>'+
          act+'</div>';
      }).join("");
    }
  }
}
async function loadSkillBackups(){
  try{
    const d=await api("/skills/backups");
    skBackups=(d&&d.backups)||[];
  }catch(e){
    skBackups=[];
  }
  renderSkillManage();
}
async function removeSkillTarget(slug){
  const modeDesc=(skTarget==="antigravity"&&skAgyMode==="plugin")
    ?("Antigravity 插件 ("+(($("#sk-plugin-name")&&$("#sk-plugin-name").value)||("migrated-from-"+skSource))+")")
    :((skData&&skData.dest_root)||skTarget);
  if(!await askConfirm("确认从 "+modeDesc+" 安全移除「"+slug+"」？\n\n移除前会自动完整备份到 vault/backups/，可在下方「历史备份」随时还原。","确认安全移除 Skill","备份并移除"))return;
  const pname=(($("#sk-plugin-name")&&$("#sk-plugin-name").value)||"").trim();
  try{
    const r=await api("/skills/remove",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        target:skTarget,slugs:[slug],agy_mode:skAgyMode,plugin_name:pname,
        apply:true,connected_only:true,
      })});
    toast("已移除 "+slug+(r.backup_id?("（备份 "+r.backup_id+"）"):""));
    skBackups=null;
    await loadSkills({refresh:true});
    if(skManageOpen)await loadSkillBackups();
  }catch(e){
    toast("移除失败："+e.message,1);
  }
}
async function rollbackSkillBackup(backupId){
  if(!await askConfirm("确认从备份 "+backupId+" 恢复对应的 Skill 到原目标目录？","确认恢复 Skill 备份","确认恢复"))return;
  try{
    const r=await api("/skills/rollback",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({backup_id:backupId,apply:true})});
    toast("已恢复 "+num(r.restored||0)+" 个 Skill（"+backupId+"）");
    await loadSkills({refresh:true});
    await loadSkillBackups();
  }catch(e){
    toast("恢复失败："+e.message,1);
  }
}

