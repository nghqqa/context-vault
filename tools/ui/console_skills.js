/* ================= Skill 技能跨工具迁移 (支持 Antigravity 全局目录 / 独立插件包双模式) ================= */
let migMode="session",skSource="zcode",skTarget="antigravity",skAgyMode="skills";
let skData=null,skSelected=new Set(),skPluginEdited=false;
const skSeq=seqGuard();
function setMigMode(m){
  migMode=(m==="skills")?"skills":"session";
  const isSk=(migMode==="skills");
  const sp=$("#mig-session-pane"),kp=$("#mig-skills-pane");
  if(sp)sp.hidden=isSk;
  if(kp)kp.hidden=!isSk;
  const bs=$("#mig-tab-session"),bk=$("#mig-tab-skills");
  if(bs){bs.classList.toggle("on",!isSk);bs.setAttribute("aria-selected",!isSk?"true":"false")}
  if(bk){bk.classList.toggle("on",isSk);bk.setAttribute("aria-selected",isSk?"true":"false")}
  const mh=$("#mig-mode-hint");
  if(mh)mh.textContent=isSk
    ?"批量搬运本地 SKILL.md 技能包（支持目录展平、Frontmatter 规范化、冲突去重与 Antigravity 双模式）"
    :"跨工具搬运历史对话（支持保真度预演与 Markdown 预算简报）";
  if(isSk&&!skData)loadSkills();
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
  if(pnEl&&!skPluginEdited)pnEl.value="migrated-from-"+skSource;
  const pname=(pnEl&&pnEl.value||"").trim();
  const qs=["source="+encodeURIComponent(skSource),"target="+encodeURIComponent(skTarget),
    "include_plugins="+(inc?1:0),"agy_mode="+encodeURIComponent(skAgyMode)];
  if(skTarget==="antigravity"&&skAgyMode==="plugin"&&pname)qs.push("plugin_name="+encodeURIComponent(pname));
  if(o.refresh)qs.push("refresh=1");
  try{
    const d=await skSeq(()=>api("/skills/list?"+qs.join("&")));
    if(listEl)listEl.style.opacity="";
    skData=d;
    // 默认仅保留仍在当前列表中的勾选项; 首次加载默认勾选未同步的独立 Skill (最多全选, 方便一步搬家)
    const validIds=new Set((d.skills||[]).map(s=>s.id));
    if(skSelected.size){
      skSelected=new Set([...skSelected].filter(id=>validIds.has(id)));
    }else if(!inc){
      (d.skills||[]).filter(s=>s.target_status==="new").forEach(s=>skSelected.add(s.id));
    }
    renderSkillSources();
    renderSkillList();
    updateSkillBudget();
  }catch(e){
    if(listEl)listEl.style.opacity="";
    if(e&&e.stale)return;
    if(listEl)listEl.innerHTML=errEmpty("Skill 扫描失败："+e.message,"loadSkills({refresh:true})");
  }
}
function renderSkillSources(){
  const bar=$("#sk-src-bar");if(!bar)return;
  const srcs=(skData&&skData.sources)||[];
  const names={zcode:"ZCode",claude:"Claude Code",codex:"Codex",antigravity:"Antigravity",cursor:"Cursor"};
  bar.innerHTML=srcs.map(s=>{
    const on=(s.agent===skSource);
    const cnt=s.standalone+(s.plugins?("+"+s.plugins):"");
    return '<button type="button" class="pill-btn'+(on?' on':'')+'" onclick="setSkillSource(\''+esc(s.agent)+'\')">'+
      esc(names[s.agent]||s.agent)+' <span class="mono" style="opacity:.78">'+esc(String(cnt))+'</span></button>';
  }).join("");
}
function setSkillSource(ag){
  skSource=ag;skSelected.clear();skPluginEdited=false;
  $("#sk-result")&&($("#sk-result").innerHTML="");
  loadSkills();
}
function onSkillTargetChange(){
  const r=document.querySelector('input[name="sk-tgt"]:checked');
  skTarget=r?r.value:"antigravity";
  $$("#sk-tgt-cards .tgt-card").forEach(c=>c.classList.toggle("on",c.querySelector("input").value===skTarget));
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
  const vis=skVisibleList();
  if(!vis.length){
    listEl.innerHTML='<div class="hint" style="padding:18px;text-align:center">当前来源或筛选条件下没有匹配的 Skill</div>';
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
      '<span class="sk-tok">~'+num(s.prompt_tokens)+' tok · '+num(s.files)+' 文件</span></div>'+
      '<div class="sk-desc">'+esc(s.description||"")+'</div></div></label>';
  }).join("");
  updateSkillBudget();
}
function updateSkillBudget(){
  const all=(skData&&skData.skills)||[];
  const picked=all.filter(s=>skSelected.has(s.id));
  const tok=picked.reduce((a,s)=>a+(s.prompt_tokens||0),0);
  const stEl=$("#sk-sel-stat");
  if(stEl)stEl.textContent="已选 "+num(picked.length)+" / "+num(all.length)+" 个";
  const box=$("#sk-budget-box");if(!box)return;
  const heavy=(picked.length>30||tok>4000);
  box.className="sk-budget"+(heavy?" warn":"");
  const modeTxt=(skTarget==="antigravity")
    ?(skAgyMode==="plugin"?"Antigravity 独立插件包（可一键开关）":"Antigravity 全局目录")
    :skTarget;
  box.innerHTML=
    '<div><b>目标：</b><span class="mono">'+esc(modeTxt)+'</span> → <span class="mono" style="word-break:break-all">'+esc((skData&&skData.dest_root)||"—")+'</span></div>'+
    '<div><b>已选：</b>'+num(picked.length)+' 个 Skill · 预估常驻 System Prompt <b>~'+num(tok)+' tokens</b></div>'+
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
    if(r.applied)await loadSkills();
  }catch(e){
    $("#sk-result").innerHTML='<div class="result-card err"><div class="rt">'+ERRIC+'Skill 迁移失败</div><div class="hint">'+esc(e.message)+'</div></div>';
    toast("Skill 迁移失败："+e.message,1);
  }
  btns.forEach(b=>b&&(b.disabled=false));
}

