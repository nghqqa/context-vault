"""运维与可视化子系统 — 上游哨兵 (probe)、缓存预热 (warm)、切换守望 (watch) 与静态面板 (dashboard)。"""
from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from cv_ui import load_dashboard_html as _load_dashboard_html


from cv_core import cv_mod as _cv  # noqa: E402


# ------------------------------------------------- probe: 上游可用性探测

def load_profiles() -> list:
    env = os.environ.get("CV_RELAY_PROFILES")
    if env:
        # 显式设置的环境变量优先于 ~/.headroom 文件 (覆盖语义): 测试/CI 传隔离配置时
        # 不得被真实机器的 profiles 文件反超, 误触真实上游 (复核员曾实际误发请求)
        return json.loads(env)
    p = _cv().HEADROOM_DIR / "switcher-profiles.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    print("提示: 未找到 relay profiles (~/.headroom/switcher-profiles.json);"
          "可用 CV_RELAY_PROFILES='[{\"id\":\"myrelay\",\"openai_api_url\":\"https://host\",\"default_model\":\"m\"}]' 配置",
          file=sys.stderr)
    return []


def models_url(base: str) -> str:
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/models"


def fetch_models(url: str, key: str | None, timeout: int = 10) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": "ContextVault/0.1"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ids = sorted(m.get("id", "") for m in body.get("data", []))
            return ("ok", resp.status, ids, int((time.time() - t0) * 1000))
    except urllib.error.HTTPError as e:
        return ("http_error", e.code, [], int((time.time() - t0) * 1000))
    except Exception as e:
        return ("error", None, [type(e).__name__ + ": " + str(e)[:80]], int((time.time() - t0) * 1000))


def notify(title: str, msg: str):
    """零依赖告警: 控制台高亮 + BEL; Windows toast 交给 Switcher exe (M2 托盘)。"""
    print(f"\a⚠ {title}: {msg}", file=sys.stderr)


def resolve_key(args) -> str | None:
    """key 解析顺序: CV_RELAY_KEY 环境变量 → --key 参数 → headroom auth-token 脚本。"""
    if os.environ.get("CV_RELAY_KEY"):
        return os.environ["CV_RELAY_KEY"]
    if args.key:
        return args.key
    script = _cv().HOME / ".local" / "bin" / "headroom-auth-token.ps1"
    if script.exists():
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=8, creationflags=_cv().NO_WINDOW)
            token = (r.stdout or "").strip().splitlines()
            return token[-1].strip() if token else None
        except Exception:
            return None
    return None


def probe_once(vault: Path, profiles: list, key, baseline: dict, verbose=True) -> list:
    """单轮全 profile 探测; 更新 baseline(dict, 调用方负责持久化)并落库, 返回告警结果。"""
    conn = _cv().db(vault)
    conn.executescript(_cv().SCHEMA)
    alerts = []
    global _PROBE_ALERT_STATE
    try:
        _PROBE_ALERT_STATE
    except NameError:
        _PROBE_ALERT_STATE = set()  # (profile,kind,签名): 持续同样异常只首弹, 防通知轰炸
    for p in profiles:
        pid, url = p["id"], models_url(p["openai_api_url"])
        status, code, ids, ms = fetch_models(url, key)
        watched, missing = p.get("default_model"), []
        if status == "ok":
            if watched and watched not in ids:
                missing.append(watched)
            # 仅告警基线中消失的模型; 新增模型不打扰
            if pid in baseline and baseline[pid]:
                missing += [m for m in baseline[pid] if m not in ids]
            missing = list(dict.fromkeys(missing))
            baseline[pid] = ids
            if verbose:
                flag = "✗" if missing else "✓"
                print(f"{flag} [{pid}] ok http={code} {ms}ms  models={len(ids)}"
                      + (f"  消失: {missing}" if missing else ""))
            if missing:
                alerts.append({"profile": pid, "kind": "model_missing", "detail": missing})
                if (pid, "missing", tuple(missing)) not in _PROBE_ALERT_STATE:
                    _PROBE_ALERT_STATE.add((pid, "missing", tuple(missing)))
                    notify("上游模型变动", f"[{pid}] 消失: {', '.join(missing)}")
        else:
            # 端点异常(网络/鉴权) ≠ 模型下架, 不进入模型消失判定, 防止狼来了效应
            detail = ids[0] if ids else ""
            if verbose:
                print(f"✗ [{pid}] {status} http={code} {ms}ms  端点异常: {detail}")
            alerts.append({"profile": pid, "kind": "endpoint", "detail": detail})
            if (pid, "endpoint", status, code) not in _PROBE_ALERT_STATE:
                _PROBE_ALERT_STATE.add((pid, "endpoint", status, code))
                notify("上游端点异常", f"[{pid}] {status} http={code} — 无法判定模型状态")
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (_cv().now_iso(), pid, url, status, code, ms, json.dumps(ids), json.dumps(missing)))
    conn.commit()
    return alerts


def cmd_probe(args):
    vault = _cv().vault_dir(args)
    baseline_p = vault / "probe_baseline.json"
    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
    key = resolve_key(args)
    profiles = load_profiles()
    if args.profile:
        # 对齐 cmd_warm: 不存在的 profile 报错退出, 不许静默兜底探测全部
        # (曾 `or profiles` 把 --profile nope 变成全量探测, 误发真实上游请求)
        matched = [p for p in profiles if p["id"] == args.profile]
        if not matched:
            raise SystemExit(f"profile 不存在: {args.profile} (可用: {[p['id'] for p in profiles]})")
        profiles = matched
    while True:
        _cv().probe_once(vault, profiles, key, baseline)
        _cv().atomic_write_text(baseline_p, json.dumps(baseline, ensure_ascii=False, indent=1))
        if not args.watch:
            break
        time.sleep(args.watch)



# ------------------------------------------------- warm: 缓存预热 (M2, Pro 核心能力)

def chat_url(base: str) -> str:
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/chat/completions"


def warm_messages(env: dict, cap_chars: int) -> list:
    """活上下文 → chat messages (近似前缀; 精确前缀预热用 --prefix-file)。"""
    live, comp_summary = _cv().live_context(env["conversation"])
    msgs = []
    if comp_summary:
        msgs.append({"role": "user", "content": "[context-vault 迁移摘要] " + comp_summary[:4000]})
    total = 0
    for b in live:
        if b["type"] == "text" and b.get("role") in ("user", "assistant"):
            content = b["content"][:cap_chars]
            total += len(content)
            msgs.append({"role": b["role"], "content": content})
    return msgs


def post_chat(url: str, key: str, model: str, messages: list, timeout: int = 120) -> dict:
    body = json.dumps({"model": model, "messages": messages, "max_tokens": 1,
                       "temperature": 0}).encode("utf-8")
    # 自定义 UA 必需: Python-urllib 默认 UA 会被部分 relay 的 CDN 防护拦截 (实测 502)
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}",
                                          "User-Agent": "ContextVault/0.1"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), int((time.time() - t0) * 1000)


def cmd_warm(args):
    vault = _cv().vault_dir(args)
    env = _cv().load_snapshot(vault if isinstance(vault, Path) else args, args.snapshot)
    profiles = load_profiles()
    prof = next((p for p in profiles if p["id"] == args.profile), None)
    if not prof:
        raise SystemExit(f"profile 不存在: {args.profile} (可用: {[p['id'] for p in profiles]})")
    key = resolve_key(args)
    if not key:
        raise SystemExit("无 API key (设 CV_RELAY_KEY / --key, 或安装 headroom-auth-token.ps1)")
    model = args.model or prof.get("default_model") or "gpt-5.6-sol"
    if args.prefix_file:
        messages = [{"role": "user", "content": Path(args.prefix_file).read_text(encoding="utf-8")[:args.cap]}]
        mode = f"prefix-file {args.prefix_file}"
    else:
        messages = warm_messages(env, args.cap)
        mode = f"snapshot {args.snapshot} 活上下文"
    nchars = sum(len(m["content"]) for m in messages)
    print(f"预热目标: [{prof['id']}] {model}  模式: {mode}  消息={len(messages)} 字符={nchars:,}")
    url = chat_url(prof["openai_api_url"])
    # 第一次: 填充缓存
    r1, ms1 = post_chat(url, key, model, messages)
    u1 = r1.get("usage", {})
    print(f"  ① 预热请求: {ms1}ms  usage={json.dumps({k: u1.get(k) for k in ('prompt_tokens','completion_tokens')}, ensure_ascii=False)}")
    # 第二次: 验证缓存命中
    r2, ms2 = post_chat(url, key, model, messages)
    u2 = r2.get("usage", {})
    cached = (u2.get("prompt_tokens_details") or {}).get("cached_tokens") or u2.get("cached_tokens") or 0
    print(f"  ② 复验请求: {ms2}ms  prompt={u2.get('prompt_tokens')}  cached={cached}")
    conn = _cv().db(vault)
    conn.executescript(_cv().SCHEMA)
    conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (_cv().now_iso(), prof["id"], url, "warm", 200, ms1, json.dumps(
                     {"model": model, "messages": len(messages), "chars": nchars,
                      "prompt_tokens": u2.get("prompt_tokens"), "cached_tokens": cached}), None))
    conn.commit()
    if cached:
        print(f"✓ 缓存已生效: 第二次请求 {cached}/{u2.get('prompt_tokens')} tokens 命中缓存 — 切换后真实会话将直接受益")
    else:
        print("⚠ 该上游未返回缓存命中间接信号 (cached_tokens); 前缀是否入缓存取决于供应商实现, 延迟对比可作旁证")


# ==== M2 IMPL (dashboard/watch/serve) ====
# ------------------------------------------------- dashboard: 账本可视化 (M2)

DASH_TMPL = _load_dashboard_html()


def cmd_dashboard(args):
    vault = _cv().vault_dir(args)
    conn = _cv().db(vault)
    cards, bymodel, months, probews, snaps = [], [], [], [], []
    row = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events "
                       "WHERE source='headroom-lifetime'").fetchone()
    raw = json.loads(row["raw"]) if row else {}
    usd = lambda v: f"${v:,.2f}" if isinstance(v, (int, float)) else "-"
    # 自有账本卡 (零外部依赖, 人人开箱即有) — headroom 未装时仍是主视觉
    own = _cv().owned_stats(vault)
    if own["snapshots"]:
        cards.append(f'<div class="card"><div class="v">{own["tokens_in"]/(10**6):,.0f}M</div>'
                     f'<div class="k">会话累计消耗 tokens ({own["snapshots"]} 快照 · 自有)</div></div>')
        cards.append(f'<div class="card alt"><div class="v">{own["cache_hit_rate"]*100:.0f}%</div>'
                     f'<div class="k">缓存命中率 (cache读 {own["cache_read"]/(10**6):,.0f}M)</div></div>')
        top = own["by_agent"][0] if own["by_agent"] else None
        if top:
            cards.append(f'<div class="card alt"><div class="v">{top["snapshots"]}</div>'
                         f'<div class="k">{html.escape(str(top["agent"]))} 会话 · '
                         f'{(top["tokens_in"] or 0)/(10**6):,.0f}M tok</div></div>')
    if row:  # headroom 增强 (装了才有)
        total_saved = ((row["cost_usd"] or 0) + (raw.get("cache_savings_usd") or 0)) if row else 0
        cards.append(f'<div class="card"><div class="v">{usd(total_saved)}</div><div class="k">累计节省 (headroom 通路层)</div></div>')
        cards.append(f'<div class="card alt"><div class="v">{usd(raw.get("cache_savings_usd"))}</div><div class="k">缓存节省</div></div>')
    else:
        cards.append('<div class="card alt"><div class="v">—</div>'
                     '<div class="k">节省金额: 接入 headroom 解锁 (cv ledger import-headroom)</div></div>')
    rows = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c "
                        "FROM ledger_events WHERE kind='compression' GROUP BY model ORDER BY c DESC").fetchall()
    mx = max((r["c"] or 0) for r in rows) if rows else 1
    for r in rows:
        w = int(100 * (r["c"] or 0) / mx) if mx else 0
        bymodel.append(f'<div class="bar"><div class="name">{html.escape(str(r["model"] or ""))}</div>'
                       f'<div class="track"><div class="fill" style="width:{w}%"></div></div>'
                       f'<div class="val">{usd(r["c"] or 0)} · {(r["t"] or 0):,} tok</div></div>')
    rows = conn.execute("SELECT substr(occurred_at,1,7) m, SUM(cost_usd) c FROM ledger_events "
                        "WHERE kind='compression' GROUP BY m ORDER BY m").fetchall()
    mx = max((r["c"] or 0) for r in rows) if rows else 1
    for r in rows:
        h = int(100 * (r["c"] or 0) / mx) if mx else 0
        m_txt = html.escape(str((r["m"] or "")[2:]))
        months.append(f'<div class="mcol"><div class="stick" style="height:{max(h,3)}%" title="{usd(r["c"] or 0)}"></div>'
                      f'<div class="lb">{m_txt}</div></div>')
    for r in conn.execute("SELECT * FROM probe_events ORDER BY id DESC LIMIT 15"):
        cls = {"ok": "ok", "warm": "warm"}.get(r["status"], "err")
        detail = html.escape(str((r["missing_models"] or "") or (r["models"] or "")[:60]))
        probews.append(f'<tr><td><span class="dot {cls}"></span></td><td>{html.escape(str(r["ts"] or "")[:19])}</td>'
                       f'<td>{html.escape(str(r["profile"] or ""))}</td><td>{html.escape(str(r["status"] or ""))}</td><td>{detail}</td></tr>')
    for r in conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 20"):
        snaps.append(f'<tr><td>{html.escape(str(r["id"] or ""))}</td><td>{html.escape(str(r["agent"] or ""))}</td><td>{r["blocks_total"]}</td>'
                     f'<td>{r["tokens_in"]:,}</td><td>{r["cache_read"]:,}</td>'
                     f'<td>{html.escape(str(r["completeness"] or ""))}</td><td>{html.escape(str(r["created_at"])[:19])}</td></tr>')
    doc_html = (DASH_TMPL.replace("__GENERATED__", _cv().now_iso())
                .replace("__CARDS__", "".join(cards))
                .replace("__BYMODEL__", "".join(bymodel) or '<div class="sub">明细为空</div>')
                .replace("__MONTHS__", str(len(months)))
                .replace("__TIMELINE__", "".join(months))
                .replace("__PROBE__", "".join(probews))
                .replace("__NSNAP__", str(len(snaps)))
                .replace("__SNAPS__", "".join(snaps)))
    out = Path(args.out) if args.out else vault / "dashboard.html"
    out.write_text(doc_html, encoding="utf-8")
    print(f"面板已生成: {out.resolve()}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


# ------------------------------------------------- watch: 切换携带会话 (M2)

def active_sessions(hours: float = 8) -> list:
    import cv_dsh
    cutoff = time.time() - hours * 3600
    found = []
    for f in _cv().CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("codex", str(f)))
    for f in (_cv().HOME / ".claude" / "projects").glob("*/*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("claude-code", str(f)))
    for f in (_cv().HOME / ".claude" / "transcripts").glob("*.jsonl"):
        if f.stat().st_mtime > cutoff:
            found.append(("claude-transcript", str(f)))
    for f in cv_dsh.discover_dsh_session_files():
        try:
            if f.stat().st_mtime > cutoff:
                found.append(("dsh", str(f)))
        except OSError:
            continue
    return sorted(found, key=lambda x: -Path(x[1]).stat().st_mtime)


def toast(title: str, msg: str):
    t_esc = html.escape(str(title), quote=True).replace("'", "''")
    m_esc = html.escape(str(msg), quote=True).replace("'", "''")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
          "ContentType=WindowsRuntime] | Out-Null; "
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument; "
          "$x.LoadXml('<toast><visual><binding template=\"ToastText02\">"
          f"<text id=\"1\">{t_esc}</text><text id=\"2\">{m_esc}</text>"
          "</binding></visual></toast>'); "
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Context Vault')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10, creationflags=_cv().NO_WINDOW)
    except Exception:
        pass
    print(f"\a⚠ {title}: {msg}", file=sys.stderr)


def cmd_watch(args):
    vault = _cv().vault_dir(args)
    prof_p = Path(args.file) if args.file else _cv().HEADROOM_DIR / "upstream-profile.json"
    if not prof_p.exists():
        raise SystemExit(f"未找到 {prof_p} (Switcher 未安装?)")
    conn = _cv().db(vault)
    conn.executescript(_cv().SCHEMA)
    last = prof_p.read_text(encoding="utf-8")
    print(f"守望中: 监听 {prof_p} (当前 "
          f"{json.loads(last).get('profile', '?')}), 每 {args.interval}s 检查; Ctrl+C 退出")
    while True:
        time.sleep(args.interval)
        try:
            cur = prof_p.read_text(encoding="utf-8")
        except OSError:
            continue
        if cur == last:
            continue
        last = cur
        try:
            prof = json.loads(cur)
        except ValueError:
            continue
        pid = prof.get("profile", "?")
        acts = active_sessions(args.hours)
        by_agent = {}
        for a, _ in acts:
            by_agent[a] = by_agent.get(a, 0) + 1
        acts_desc = ", ".join(f"{k}×{v}" for k, v in by_agent.items()) or "无"
        msg = (f"已切换到 [{pid}]。最近 {args.hours:.0f}h 活跃会话: {acts_desc}。"
               "携带迁移: cv migrate <会话文件> --target codex|claude")
        toast("Context Vault · 上游已切换", msg)
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (_cv().now_iso(), pid, "switch-detected", "switch", None, None,
                      json.dumps({"active_sessions": len(acts), "by_agent": by_agent},
                                 ensure_ascii=False), None))
        conn.commit()
        for agent, f in acts[:5]:
            print(f"  · [{agent}] {f}")

