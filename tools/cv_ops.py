"""运维与可视化子系统 — 上游哨兵 (probe)、缓存预热 (warm)、切换守望 (watch) 与静态面板 (dashboard)。"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

from cv_ui import load_dashboard_html as _load_dashboard_html


from cv_core import cv_mod as _cv, user_agent  # noqa: E402


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
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
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
                                          "User-Agent": user_agent()})
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
    ops_rows = []
    ops_data = ops_log_summary_and_timeline(vault, kind="all", limit=15)
    kind_names = {"sync": "同步", "backup": "备份", "migrate": "迁移", "mcp": "MCP", "error": "失败"}
    for day_blk in ops_data.get("timeline") or []:
        day = str(day_blk.get("day") or "")
        for ev in day_blk.get("events") or []:
            st = str(ev.get("status") or "ok")
            k = str(ev.get("kind") or "ops")
            cls = "err" if st == "error" or k == "error" else "ok"
            k_lbl = kind_names.get(k, k)
            ops_rows.append(
                f'<tr><td><span class="dot {cls}"></span></td>'
                f'<td>{html.escape((day + " " + str(ev.get("ts") or "")).strip())}</td>'
                f'<td>{html.escape(k_lbl)}</td>'
                f'<td><code>{html.escape(str(ev.get("action") or ""))}</code></td>'
                f'<td>{html.escape(str(ev.get("detail") or ""))}</td></tr>'
            )
    if not ops_rows:
        ops_rows.append('<tr><td colspan="5" style="color:var(--ink-3)">暂无本地运维记录</td></tr>')
    ops_html = "".join(ops_rows)
    for r in conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 20"):
        snaps.append(f'<tr><td>{html.escape(str(r["id"] or ""))}</td><td>{html.escape(str(r["agent"] or ""))}</td><td>{r["blocks_total"]}</td>'
                     f'<td>{r["tokens_in"]:,}</td><td>{r["cache_read"]:,}</td>'
                     f'<td>{html.escape(str(r["completeness"] or ""))}</td><td>{html.escape(str(r["created_at"])[:19])}</td></tr>')
    doc_html = (DASH_TMPL.replace("__GENERATED__", _cv().now_iso())
                .replace("__CARDS__", "".join(cards))
                .replace("__BYMODEL__", "".join(bymodel) or '<div class="sub">明细为空</div>')
                .replace("__MONTHS__", str(len(months)))
                .replace("__TIMELINE__", "".join(months))
                .replace("__OPS__", ops_html)
                .replace("__PROBE__", ops_html)
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


# ------------------------------------------------- 运维、分支、审计与备份/裁剪

def storm_relink(conn) -> int:
    """断链重链 (2026-11-08 风暴修复): 按快照文件 mtime 恢复每会话链条。"""
    rows = conn.execute("SELECT id, agent, session_ref, parent_snapshot, fork_of, path FROM snapshots").fetchall()
    groups = {}
    for r in rows:
        if not r["session_ref"]:
            continue
        try:
            mt = os.path.getmtime(r["path"]) if r["path"] else 0
        except OSError:
            mt = 0
        groups.setdefault((r["agent"], r["session_ref"], r["fork_of"] or ""), []).append((mt, r))
    n = 0
    for lst in groups.values():
        if len(lst) < 2:
            continue
        roots = [x for x in lst if not x[1]["parent_snapshot"]]
        if roots and len(roots) < 2:
            continue
        if not roots:
            member_ids = {x[1]["id"] for x in lst}
            if not all(x[1]["parent_snapshot"] in member_ids for x in lst):
                continue
        lst = sorted(lst, key=lambda x: x[0])
        prev = None
        for _, r in lst:
            if r["parent_snapshot"] != prev:
                conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (prev, r["id"]))
                n += 1
            prev = r["id"]
    conn.commit()
    return n


def cmd_repair(args):
    """一次性数据修复的显式入口: 从快照文件回填 lineage 索引列, zcode 历史快照按版本时间回填用量。"""
    c = _cv()
    vault = c.vault_dir(args)
    conn = c.db(vault)
    n_lin = c.backfill_lineage(conn, force=True)
    n_zu = c.backfill_zcode_usage(conn, force=True)
    n_st = storm_relink(conn)
    own = c.owned_stats(vault)
    print(f"lineage 回填: {n_lin} 行 · zcode 用量回填: {n_zu} 行 · 断链重链: {n_st} 行")
    print(f"账本口径: {own['sessions']} 独立会话 / {own['snapshots']} 快照版本 · "
          f"规范化总输入 {own['input_effective']:,} tok · 命中率 {own['cache_hit_rate']*100:.1f}%")


def fork_snapshot(vault: Path, source_sid: str, message: str | None = None) -> dict:
    """在历史节点开分支: 复制快照为新内容寻址实体, lineage.fork_of 指向原快照 (CLI 与 POST /fork 共用)。"""
    c = _cv()
    env = c.load_snapshot(vault, source_sid)
    env["lineage"]["fork_of"] = source_sid
    if message:
        env["capture"]["fork_note"] = message
    sid = c.snapshot_id(env)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    out = vault / "snapshots" / f"{sid}.json"
    if out.exists():
        try:
            existing_fork_of = (json.loads(out.read_text(encoding="utf-8")).get("lineage") or {}).get("fork_of")
        except (ValueError, OSError):
            existing_fork_of = None
        if existing_fork_of != source_sid:
            sid = "cv_" + hashlib.sha256(
                c.canonical_bytes(env) + b"|fork_of:" + str(source_sid).encode()
            ).hexdigest()[:16]
            out = vault / "snapshots" / f"{sid}.json"
    existed = out.exists()
    if not existed:
        c.atomic_write_text(out, json.dumps(env, ensure_ascii=False, indent=1))
    conn = c.db(vault)
    try:
        conn.executescript(c.SCHEMA)
        c.register_snapshot(conn, env, sid, out)
        conn.commit()
    finally:
        conn.close()
    return {
        "fork_sid": sid, "source_sid": source_sid, "existed": existed,
        "message": message or "", "path": str(out),
    }


def cmd_fork(args):
    """在历史节点开分支: 复制快照为新内容寻址实体, lineage.fork_of 指向原快照。"""
    c = _cv()
    vault = c.vault_dir(args)
    res = fork_snapshot(vault, args.snapshot_id, getattr(args, "message", None))
    sid = res["fork_sid"]
    if res["existed"]:
        print(f"该分叉已存在 (幂等): {sid}.json")
    else:
        print(f"已分叉: {args.snapshot_id} → {sid}"
              + (f" · 备注: {args.message}" if getattr(args, "message", None) else ""))
    print(f"  续写: python tools/cv.py import {sid} --target codex|claude --apply")


def cmd_tree(args):
    """展示快照的 lineage 链: 向上追 parent (同源演进), 向下展 fork 子树。"""
    c = _cv()
    vault = c.vault_dir(args)
    env = c.load_snapshot(vault, args.snapshot_id)
    chain = [args.snapshot_id]
    cur = env
    while cur["lineage"].get("parent_snapshot"):
        p = cur["lineage"]["parent_snapshot"]
        chain.append(p)
        ppath = vault / "snapshots" / f"{p}.json"
        if not ppath.exists():
            break
        cur = json.loads(ppath.read_text(encoding="utf-8"))
    chain.reverse()
    children = {}
    for f in (vault / "snapshots").glob("cv_*.json"):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        fo = e.get("lineage", {}).get("fork_of")
        if fo:
            children.setdefault(fo, []).append(f.stem)
    print(f"快照 {args.snapshot_id} 的谱系:")
    for i, sid in enumerate(chain):
        mark = " → " if i < len(chain) - 1 else " ● "
        print(f"  {mark}{sid}" + ("  (本快照)" if sid == args.snapshot_id else ""))

    def show_forks(sid, depth=0):
        for ch in children.get(sid, []):
            print("  " + "    " * depth + f"↳ fork {ch}")
            show_forks(ch, depth + 1)

    show_forks(args.snapshot_id)


def audit_summary_and_timeline(vault: Path, since: str, kind: str, limit: int = 200,
                               summary_only: bool = False) -> dict:
    """统一事件流: probe/switch/warm + 汇总。"""
    c = _cv()
    conn = c.db(vault)
    rows = []
    try:
        agg = conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN status='switch' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN status='prune' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN missing_models IS NOT NULL AND missing_models != '[]' THEN 1 ELSE 0 END), 0)"
            " FROM probe_events"
        ).fetchone()
        total, ok_probes, switches, prune_n, model_missing = (int(x or 0) for x in agg)
        if not summary_only:
            q = "SELECT * FROM probe_events WHERE 1=1"
            params = []
            if since:
                q += " AND ts >= ?"
                params.append(since)
            k = str(kind or "all").strip().lower()
            if k in ("error", "abnormal"):
                q += (" AND (COALESCE(status,'') NOT IN ('ok','switch','prune')"
                      " OR (missing_models IS NOT NULL AND missing_models != '[]'))")
            elif k == "model_missing":
                q += (" AND (status = 'model_missing'"
                      " OR (missing_models IS NOT NULL AND missing_models != '[]'))")
            elif k and k != "all":
                q += " AND LOWER(COALESCE(status,'')) = ?"
                params.append(k)
            rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {
            "since": since or "(全部)", "total_events": 0,
            "summary": {"ok_probes": 0, "abnormal": 0, "switches": 0, "model_missing_events": 0},
            "timeline": [],
        }
    conn.close()
    err_probes = total - ok_probes - switches - prune_n
    by_day = {}
    for r in rows:
        day = str(r["ts"])[:10]
        by_day.setdefault(day, []).append(r)
    timeline = []
    for day in sorted(by_day, reverse=True):
        events = [
            {
                "ts": str(r["ts"])[11:19], "profile": r["profile"], "status": r["status"],
                "http": r["http_code"], "latency_ms": r["latency_ms"],
                "missing": r["missing_models"] if r["missing_models"] not in ("[]", None) else None,
                "detail": (r["models"] or "")[:80],
            }
            for r in by_day[day]
        ]
        timeline.append({"day": day, "events": events})
    return {
        "since": since or "(全部)", "total_events": total,
        "summary": {
            "ok_probes": ok_probes, "abnormal": err_probes,
            "switches": switches, "model_missing_events": model_missing,
        },
        "timeline": timeline,
    }


def record_ops_event(vault: Path, kind: str, action: str, detail: str = "", status: str = "ok") -> None:
    """Best-effort 写入纯本地运维事件到 vault.db/ops_events (不抛异常打断主流程)。"""
    try:
        c = _cv()
        conn = c.db(vault)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_events("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, action TEXT, status TEXT, detail TEXT)"
            )
            conn.execute(
                "INSERT INTO ops_events(ts,kind,action,status,detail) VALUES(?,?,?,?,?)",
                (
                    c.now_iso(),
                    str(kind or "ops").strip().lower(),
                    str(action or "").strip(),
                    str(status or "ok").strip().lower(),
                    str(detail or "")[:300],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _backup_zip_ts(path: Path) -> str:
    m = re.search(r"cv-backup-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", path.name)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


def ops_log_summary_and_timeline(vault: Path, kind: str = "all", limit: int = 80) -> dict:
    """汇总纯本地运维日志 (ops_events + 本地 backups/zip + migrations.jsonl)，完全不读取 probe_events。"""
    c = _cv()
    merged: list[dict] = []
    seen_backup_names: set[str] = set()
    seen_mig_sigs: set[tuple[str, str]] = set()
    db_path = vault / "vault.db"
    if db_path.exists():
        conn = c.db(vault)
        try:
            rows = conn.execute(
                "SELECT id, ts, kind, action, status, detail FROM ops_events ORDER BY id DESC LIMIT 1000"
            ).fetchall()
            for r in rows:
                ev = {
                    "ts_full": str(r["ts"] or ""),
                    "kind": str(r["kind"] or "ops").lower(),
                    "action": str(r["action"] or ""),
                    "status": str(r["status"] or "ok").lower(),
                    "detail": str(r["detail"] or ""),
                    "_seq": int(r["id"] or 0),
                }
                merged.append(ev)
                if ev["kind"] == "backup":
                    bm = re.search(r"(cv-backup-[^ ·\s]+\.zip)", ev["detail"])
                    if bm:
                        seen_backup_names.add(bm.group(1))
                elif ev["kind"] == "migrate":
                    sm = re.search(r"(cv_[0-9a-f]{8,64})", ev["detail"])
                    if sm:
                        seen_mig_sigs.add((ev["ts_full"][:16], sm.group(1)))
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    bk_dir = vault / "backups"
    if bk_dir.is_dir():
        for idx, bp in enumerate(sorted(bk_dir.glob("cv-backup-*.zip"), reverse=True)[:40]):
            if bp.name in seen_backup_names:
                continue
            try:
                sz_mb = bp.stat().st_size / 1048576.0
            except OSError:
                sz_mb = 0.0
            merged.append({
                "ts_full": _backup_zip_ts(bp),
                "kind": "backup",
                "action": "backup",
                "status": "ok",
                "detail": f"{bp.name} ({sz_mb:.1f} MB)",
                "_seq": -100 - idx,
            })

    mig_file = vault / "migrations.jsonl"
    if mig_file.is_file():
        try:
            lines = [ln for ln in mig_file.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            for idx, ln in enumerate(lines[-60:]):
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_val = str(rec.get("at") or rec.get("created_at") or "")
                src = str(rec.get("source") or rec.get("source_sid") or "")
                tgt = str(rec.get("target") or "")
                dest = str(rec.get("dest") or "")
                if not tgt and dest:
                    low = dest.lower()
                    if ".codex" in low:
                        tgt = "codex"
                    elif ".claude" in low:
                        tgt = "claude"
                    elif ".gemini" in low or "antigravity" in low:
                        tgt = "antigravity"
                    elif ".dsh" in low:
                        tgt = "dsh"
                    elif ".zcode" in low:
                        tgt = "zcode"
                    else:
                        tgt = Path(dest).name
                if src and (ts_val[:16], src) in seen_mig_sigs:
                    continue
                detail = f"{src} → {tgt}".strip(" →") if (src or tgt) else Path(dest).name
                merged.append({
                    "ts_full": ts_val,
                    "kind": "migrate",
                    "action": f"migrate→{tgt}" if tgt else "migrate",
                    "status": "ok",
                    "detail": detail[:300],
                    "_seq": -1000 - idx,
                })
        except OSError:
            pass

    merged.sort(key=lambda e: (e["ts_full"], e["_seq"]), reverse=True)
    summary = {"sync": 0, "backup": 0, "migrate": 0, "mcp": 0, "error": 0}
    for e in merged:
        k = e["kind"]
        if k in summary and k != "error":
            summary[k] += 1
        if e["status"] == "error" or k == "error":
            summary["error"] += 1

    qf = str(kind or "all").strip().lower() or "all"
    if qf == "error":
        filtered = [e for e in merged if e["status"] == "error" or e["kind"] == "error"]
    elif qf != "all":
        filtered = [e for e in merged if e["kind"] == qf]
    else:
        filtered = merged

    shown = filtered[:max(1, min(int(limit or 80), 500))]
    by_day: dict[str, list[dict]] = {}
    for e in shown:
        ts_val = e["ts_full"]
        day = ts_val[:10] if len(ts_val) >= 10 else "未知日期"
        hm = ts_val[11:19] if len(ts_val) >= 19 else ts_val[:8]
        by_day.setdefault(day, []).append({
            "ts": hm,
            "kind": e["kind"],
            "action": e["action"],
            "status": e["status"],
            "detail": e["detail"],
        })
    timeline = [{"day": d, "events": by_day[d]} for d in by_day]
    return {"total_events": len(merged), "summary": summary, "timeline": timeline}


def cmd_audit(args):
    c = _cv()
    vault = c.vault_dir(args)
    r = audit_summary_and_timeline(vault, args.since, args.kind)
    s = r["summary"]
    print(f"== 审计时间线 ({r['since']} 起, 库内共 {r['total_events']} 事件) ==")
    print(f"  探测正常 {s['ok_probes']} · 异常 {s['abnormal']} · 切换检测 {s['switches']}"
          f" · 模型消失事件 {s['model_missing_events']}")
    for day in r["timeline"][:args.days]:
        print(f"  {day['day']}  ({len(day['events'])} 事件)")
        for e in day["events"][:8]:
            mark = {"ok": "·", "switch": "⇄", "warm": "🔥"}.get(e["status"], "!")
            extra = f" 消失{e['missing']}" if e["missing"] else (f" {e['detail'][:40]}" if e["status"] == "switch" else "")
            print(f"    {mark} {e['ts']} [{e['profile']}] {e['status']}{extra}")
        if len(day["events"]) > 8:
            print(f"    … 其余 {len(day['events']) - 8} 条 (cv audit --days 更多)")


def backup_vault_api(vault: Path, keep: int = 10, out_dir: Path | None = None) -> dict:
    """结构化一致性备份接口: sqlite 在线快照 + snapshots + 配置文件打包 + 轮换保留 (不写 stdout)。"""
    import zipfile
    c = _cv()
    if not (vault / "vault.db").exists():
        raise ValueError(f"vault 未初始化: {vault} (先 cv init / cv export)")
    keep_n = max(1, min(int(keep or 10), 50))
    dest_dir = Path(out_dir).expanduser() if out_dir is not None else (vault / "backups")
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-f%03d" % (dt.datetime.now().microsecond // 1000))
    out = dest_dir / f"cv-backup-{ts}.zip"
    tmp_db = dest_dir / f".backup-tmp-{os.getpid()}-{threading.get_ident()}.db"
    n_snap = 0
    try:
        src = c.db(vault)
        try:
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "vault.db")
            snaps_dir = vault / "snapshots"
            if snaps_dir.is_dir():
                for f in sorted(snaps_dir.glob("cv_*.json")):
                    z.write(f, f"snapshots/{f.name}")
                    n_snap += 1
            for name in ("probe_baseline.json", "relay_watermark.json",
                         "migrations.jsonl", "accounts.json", "semantic.json",
                         "pricing.json", "prices.json"):
                p = vault / name
                if p.exists():
                    z.write(p, name)
    finally:
        tmp_db.unlink(missing_ok=True)
    backups = sorted(dest_dir.glob("cv-backup-*.zip"))
    for old in backups[:-keep_n] if len(backups) > keep_n else []:
        old.unlink(missing_ok=True)
    backups = sorted(dest_dir.glob("cv-backup-*.zip"))
    size_mb = out.stat().st_size / 1048576
    msg = f"备份完成: {out} ({n_snap} 快照, {size_mb:.1f} MB, 留存 {len(backups)}/{keep_n} 份)"
    record_ops_event(vault, "backup", "backup", f"{out.name} · {n_snap} 快照 · {size_mb:.1f} MB")
    logs = [msg, "  恢复: 解压到空目录, 以 --vault 指向它 (vault.db + snapshots/ 完整可用)"]
    return {
        "ok": True,
        "name": out.name,
        "path": str(out),
        "size_mb": round(size_mb, 2),
        "snapshots_count": n_snap,
        "backups_count": len(backups),
        "keep": keep_n,
        "message": msg,
        "logs": logs,
    }


def prune_snapshots_api(vault: Path, apply: bool = False) -> dict:
    """中间版本 GC 纯数据接口: 每会话每天保留最后一版 + 每会话最新版本 (叶子), 保留集按时间重链。"""
    c = _cv()
    conn = c.db(vault)
    logs: list[str] = []
    try:
        conn.executescript(c.SCHEMA + c.SYNC_TABLE)
        rows = conn.execute(
            "SELECT id, agent, session_ref, created_at, path, parent_snapshot, fork_of FROM snapshots"
        ).fetchall()
        parents = {r["parent_snapshot"] for r in rows if r["parent_snapshot"]}
        zalive = None
        if c.ZCODE_DB.exists():
            z = sqlite3.connect(f"file:{c.ZCODE_DB}?mode=ro", uri=True)
            try:
                zalive = {x[0] for x in z.execute("SELECT id FROM session WHERE time_deleted IS NULL")}
            finally:
                z.close()
        srcmap = {r[0]: r[1] for r in conn.execute("SELECT snapshot_id, path FROM sync_sources")}
        sess_rows = {}
        for r in rows:
            sess_rows.setdefault((r["agent"], r["session_ref"]), []).append(r)
        sess_alive = {}
        for key, lst in sess_rows.items():
            alive = False
            for r in lst:
                if key[0] == "zcode":
                    alive = zalive is not None and r["session_ref"] in zalive
                else:
                    sp = srcmap.get(r["id"])
                    alive = bool(sp) and Path(sp).exists()
                if alive:
                    break
            sess_alive[key] = alive
        dead_sessions = {k[1] for k, alive in sess_alive.items() if not alive}
        keep = set()
        for r in rows:
            if r["id"] not in parents:
                keep.add(r["id"])
            if r["fork_of"] or r["session_ref"] in dead_sessions:
                keep.add(r["id"])
        day_last = {}
        for r in rows:
            k = (r["agent"], r["session_ref"], (r["created_at"] or "")[:10])
            if k not in day_last or (r["created_at"] or "") > (day_last[k]["created_at"] or ""):
                day_last[k] = r
        for r in day_last.values():
            keep.add(r["id"])
        drop = [r for r in rows if r["id"] not in keep]
        reclaim = sum((os.path.getsize(r["path"]) if r["path"] and os.path.exists(r["path"]) else 0) for r in drop)
        kept_cnt = len(rows) - len(drop)
        summary = (
            f"保留 {kept_cnt} / {len(rows)} 版 (叶子 {sum(1 for r in rows if r['id'] in keep and r['id'] not in parents)} · "
            f"每日记录 {len(day_last)} · fork/孤本整链 {sum(1 for r in rows if r['fork_of'] or r['session_ref'] in dead_sessions)}) · "
            f"回收 {len(drop)} 版 / {reclaim/1e6:.0f} MB"
        )
        logs.append(summary)
        removed = 0
        if not drop:
            return {
                "applied": False, "kept": kept_cnt, "total": len(rows),
                "drop_count": 0, "reclaim_mb": 0.0, "removed": 0,
                "message": summary, "logs": logs,
            }
        if not apply:
            logs.append("dry-run — 加 --apply 执行 (执行前自动做一次 vault 备份)")
            return {
                "applied": False, "kept": kept_cnt, "total": len(rows),
                "drop_count": len(drop), "reclaim_mb": round(reclaim / 1e6, 1), "removed": 0,
                "message": "\n".join(logs), "logs": logs,
            }
        bres = backup_vault_api(vault, keep=10)
        logs.extend(bres.get("logs", []))
        kept = {}
        for r in rows:
            if r["id"] in keep:
                kept.setdefault((r["agent"], r["session_ref"]), []).append(r)
        for lst in kept.values():
            lst.sort(key=lambda r: (r["created_at"] or "", r["id"]))
            prev = None
            for r in lst:
                if r["parent_snapshot"] != prev:
                    conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (prev, r["id"]))
                prev = r["id"]
        for r in drop:
            try:
                os.unlink(r["path"])
            except OSError:
                continue
            conn.execute("DELETE FROM snapshots WHERE id=?", (r["id"],))
            removed += 1
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (c.now_iso(), "vault", "gc", "prune", None, None,
             json.dumps({"dropped": removed, "reclaimed_mb": round(reclaim / 1e6, 1),
                         "kept": kept_cnt, "backup": "vault/backups 最新一份"}, ensure_ascii=False), None),
        )
        conn.commit()
        if removed > 0:
            record_ops_event(vault, "backup", "prune", f"回收 {removed} 版 / {reclaim/1e6:.1f} MB · 保留 {kept_cnt} 版")
        done_msg = (
            f"已回收 {removed} 版 / {reclaim/1e6:.0f} MB"
            + (f" ({len(drop) - removed} 版因文件被占用跳过, 下次 prune 再试)" if removed != len(drop) else "")
            + " (GC 事件已写入审计时间线)"
        )
        logs.append(done_msg)
        return {
            "applied": True, "kept": kept_cnt, "total": len(rows),
            "drop_count": len(drop), "reclaim_mb": round(reclaim / 1e6, 1), "removed": removed,
            "message": "\n".join(logs), "logs": logs,
        }
    finally:
        conn.close()


def cmd_prune(args):
    """中间版本 GC CLI 包装。"""
    vault = _cv().vault_dir(args)
    res = prune_snapshots_api(vault, apply=bool(getattr(args, "apply", False)))
    for ln in res["logs"]:
        print(ln)


def _backup_dir(vault: Path, out_dir: Path | None = None) -> Path:
    return Path(out_dir).expanduser() if out_dir is not None else (vault / "backups")


def resolve_backup_file(vault: Path, name: str | None = None, out_dir: Path | None = None) -> Path:
    """安全解析备份 zip 路径 (拒绝路径穿越; name 为空时取最新一份)。"""
    dest_dir = _backup_dir(vault, out_dir)
    if not name or not str(name).strip():
        files = sorted(dest_dir.glob("cv-backup-*.zip")) if dest_dir.is_dir() else []
        if not files:
            raise FileNotFoundError(f"未找到任何备份文件: {dest_dir}")
        return files[-1]
    raw_name = str(name).strip()
    if (
        "\x00" in raw_name
        or "/" in raw_name
        or "\\" in raw_name
        or ".." in raw_name
        or raw_name.startswith(".")
        or Path(raw_name).name != raw_name
        or not raw_name.endswith(".zip")
    ):
        raise ValueError(f"非法备份文件名: {name}")
    base = dest_dir.resolve()
    target = (dest_dir / raw_name).resolve()
    if target.parent != base:
        raise ValueError(f"非法备份文件名: {name}")
    if not target.is_file():
        raise FileNotFoundError(f"备份文件不存在: {raw_name}")
    return target


def list_backups_api(vault: Path, out_dir: Path | None = None) -> dict:
    """列出全部本地备份压缩包 (按时间倒序, 附快照数与 zip 合法性预览)。"""
    dest_dir = _backup_dir(vault, out_dir)
    files = sorted(dest_dir.glob("cv-backup-*.zip"), reverse=True) if dest_dir.is_dir() else []
    items = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        valid_zip = False
        has_db = False
        snaps_cnt = 0
        try:
            with zipfile.ZipFile(p, "r") as z:
                names = z.namelist()
                valid_zip = True
                has_db = "vault.db" in names
                snaps_cnt = sum(1 for n in names if n.startswith("snapshots/") and n.endswith(".json"))
        except (zipfile.BadZipFile, OSError):
            valid_zip = False
        items.append({
            "name": p.name,
            "size_bytes": int(st.st_size),
            "size_mb": round(st.st_size / 1048576, 2),
            "mtime": dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            "has_db": has_db,
            "snapshots_count": snaps_cnt,
            "valid_zip": valid_zip,
        })
    return {
        "backup_dir": str(dest_dir),
        "count": len(items),
        "backups": items,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_backup_api(vault: Path, name: str | None = None, out_dir: Path | None = None) -> dict:
    """校验备份 zip 的 CRC-32 与内置 vault.db SQLite quick_check 一致性。"""
    target = resolve_backup_file(vault, name, out_dir=out_dir)
    st = target.stat()
    zip_sha = _sha256_file(target)
    crc_ok = False
    bad_member = None
    has_db = False
    db_ok = False
    db_check = "not_checked"
    db_snaps = None
    zip_snaps = 0
    files_cnt = 0
    err_msg = None

    try:
        with zipfile.ZipFile(target, "r") as z:
            names = z.namelist()
            files_cnt = len(names)
            has_db = "vault.db" in names
            zip_snaps = sum(1 for n in names if n.startswith("snapshots/") and n.endswith(".json"))
            bad_member = z.testzip()
            crc_ok = bad_member is None
            if not crc_ok:
                err_msg = f"ZIP CRC 损坏: {bad_member}"
            elif not has_db:
                err_msg = "备份缺少 vault.db"
            else:
                with tempfile.TemporaryDirectory(prefix="cv-verify-") as td:
                    tmp_db = Path(td) / "vault.db"
                    with z.open("vault.db", "r") as src, tmp_db.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    conn = None
                    try:
                        conn = sqlite3.connect(str(tmp_db))
                        row = conn.execute("PRAGMA quick_check(1)").fetchone()
                        db_check = str(row[0]) if row else "unknown"
                        db_ok = (db_check.lower() == "ok")
                        if db_ok:
                            try:
                                cnt_r = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
                                db_snaps = int(cnt_r[0]) if cnt_r else 0
                            except sqlite3.DatabaseError:
                                db_snaps = None
                        else:
                            err_msg = f"SQLite quick_check 异常: {db_check}"
                    except sqlite3.DatabaseError as e:
                        db_check = f"error: {e}"
                        db_ok = False
                        err_msg = f"SQLite 读取失败: {e}"
                    finally:
                        if conn is not None:
                            conn.close()
    except (zipfile.BadZipFile, OSError) as e:
        err_msg = f"ZIP 无法读取: {e}"

    return {
        "ok": bool(crc_ok and has_db and db_ok),
        "name": target.name,
        "path": str(target),
        "size_bytes": int(st.st_size),
        "size_mb": round(st.st_size / 1048576, 2),
        "sha256": zip_sha,
        "crc_ok": crc_ok,
        "bad_member": bad_member,
        "has_db": has_db,
        "db_ok": db_ok,
        "db_check": db_check,
        "db_snapshots": db_snaps,
        "zip_snapshots": zip_snaps,
        "files_count": files_cnt,
        "error": err_msg,
    }


def _validate_update_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"不支持的下载协议: {parsed.scheme or '(空)'}")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("非本机回环更新源必须使用 HTTPS")
    return url


def parse_sha256sums(text: str) -> dict[str, str]:
    """解析 SHA256SUMS.txt / BSD SHA256 输出为 {basename: hex_digest}。"""
    out = {}
    for raw_ln in str(text or "").splitlines():
        ln = raw_ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m_std = re.match(r"^([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", ln)
        if m_std:
            digest = m_std.group(1).lower()
            fname = Path(m_std.group(2).strip()).name
            if fname:
                out[fname] = digest
            continue
        m_bsd = re.match(r"^SHA256\s*\((.+?)\)\s*=\s*([0-9a-fA-F]{64})\s*$", ln, re.I)
        if m_bsd:
            fname = Path(m_bsd.group(1).strip()).name
            digest = m_bsd.group(2).lower()
            if fname:
                out[fname] = digest
    return out


def select_release_assets(
    release_data: dict,
    exe_name: str = "ContextVault.exe",
    checksum_names: tuple[str, ...] = ("SHA256SUMS.txt", "SHA256SUMS"),
) -> dict:
    """从 GitHub Release API 响应中提取版本、安装包和 SHA256SUMS 资产。"""
    assets = release_data.get("assets") if isinstance(release_data, dict) else None
    if not isinstance(assets, list):
        assets = []
    exe_asset = None
    exe_fallback = None
    chk_asset = None
    want_exe = str(exe_name or "ContextVault.exe").lower()
    want_chks = {n.lower() for n in checksum_names}

    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        low = name.lower()
        if low == want_exe and exe_asset is None:
            exe_asset = a
        elif low.endswith(".exe") and want_exe == "contextvault.exe" and exe_fallback is None:
            exe_fallback = a
        if (low in want_chks or low.endswith("sha256sums.txt")) and chk_asset is None:
            chk_asset = a

    chosen_exe = exe_asset or exe_fallback
    exe_digest = None
    if isinstance(chosen_exe, dict):
        raw_d = str(chosen_exe.get("digest") or "").strip()
        if raw_d.lower().startswith("sha256:"):
            cand = raw_d.split(":", 1)[1].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", cand):
                exe_digest = cand

    return {
        "version": str((release_data or {}).get("tag_name") or "").lstrip("vV"),
        "html_url": str((release_data or {}).get("html_url") or ""),
        "exe_name": str(chosen_exe.get("name") or exe_name) if chosen_exe else exe_name,
        "exe_url": str(chosen_exe.get("browser_download_url") or "") if chosen_exe else "",
        "exe_digest": exe_digest,
        "checksum_name": str(chk_asset.get("name") or "") if chk_asset else "",
        "checksum_url": str(chk_asset.get("browser_download_url") or "") if chk_asset else "",
    }


def download_and_verify_exe(
    exe_url: str,
    dest_dir: Path,
    expected_sha256: str | None = None,
    checksum_url: str | None = None,
    filename: str = "ContextVault.exe.new",
    match_name: str = "ContextVault.exe",
    timeout: float = 30.0,
) -> dict:
    """流式下载更新包并强制校验 SHA-256，校验通过才落盘目标文件名。"""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    _validate_update_url(exe_url)
    expected = str(expected_sha256 or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected_sha256 格式无效 (需 64 位十六进制)")

    if not expected:
        if not checksum_url:
            raise ValueError("缺少 SHA-256 校验值或 checksum_url，已拒绝无校验更新")
        _validate_update_url(checksum_url)
        req_c = urllib.request.Request(
            checksum_url,
            headers={"User-Agent": user_agent()},
        )
        with urllib.request.urlopen(req_c, timeout=timeout) as r:
            chk_text = r.read().decode("utf-8", errors="replace")
        sums = parse_sha256sums(chk_text)
        m_low = str(match_name or "ContextVault.exe").lower()
        for k, v in sums.items():
            if k.lower() == m_low:
                expected = v
                break
        if not expected:
            raise ValueError(f"校验文件中未找到 {match_name} 的 SHA-256 条目")

    safe_fn = Path(str(filename or "ContextVault.exe.new")).name
    part_path = dest / f".{safe_fn}.{os.getpid()}.part"
    target_path = dest / safe_fn
    h = hashlib.sha256()
    size_bytes = 0
    try:
        req_e = urllib.request.Request(
            exe_url,
            headers={"User-Agent": user_agent()},
        )
        with urllib.request.urlopen(req_e, timeout=timeout) as r, part_path.open("wb") as fh:
            while True:
                chunk = r.read(1024 * 128)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                size_bytes += len(chunk)
        actual = h.hexdigest().lower()
        if actual != expected:
            try:
                part_path.unlink()
            except OSError:
                pass
            raise ValueError(
                f"SHA-256 校验失败: 期望 {expected[:16]}..., 实际 {actual[:16]}..."
            )
        os.replace(part_path, target_path)
    except Exception:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        raise

    return {
        "ok": True,
        "path": str(target_path),
        "sha256": actual,
        "size_bytes": size_bytes,
    }


def _safe_cmd_path(p: Path) -> str:
    s = str(p)
    if any(ch in s for ch in ('"', "\r", "\n", "%", "&", "|", "<", ">", "^", "\x00")):
        raise ValueError(f"路径包含不安全字符，无法写入更新脚本: {s}")
    return s


def write_windows_update_script(
    current_exe: Path,
    new_exe: Path,
    pid: int,
    script_path: Path | None = None,
    relaunch: bool = True,
) -> Path:
    """生成 Windows 自替换批处理脚本 (等待进程退出后覆盖 exe 并可选重拉)。"""
    cur_s = _safe_cmd_path(Path(current_exe))
    new_s = _safe_cmd_path(Path(new_exe))
    pid_i = int(pid)
    if pid_i < 0:
        raise ValueError(f"非法 PID: {pid}")
    out_p = Path(script_path) if script_path is not None else (Path(new_exe).parent / f"cv-apply-update-{pid_i}.cmd")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    relaunch_line = f'if exist "%TARGET%" start "" "%TARGET%"\n' if relaunch else ""
    body = (
        "@echo off\n"
        "setlocal\n"
        f'set "TARGET={cur_s}"\n'
        f'set "STAGED={new_s}"\n'
        f'set "PID={pid_i}"\n'
        "for /L %%i in (1,1,30) do (\n"
        '  tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul\n'
        "  if errorlevel 1 goto apply\n"
        "  ping -n 2 127.0.0.1 >nul\n"
        ")\n"
        ":apply\n"
        "for /L %%r in (1,1,10) do (\n"
        '  move /Y "%STAGED%" "%TARGET%" >nul 2>nul\n'
        "  if not errorlevel 1 goto done\n"
        "  ping -n 2 127.0.0.1 >nul\n"
        ")\n"
        ":done\n"
        f"{relaunch_line}"
        '(goto) 2>nul & del "%~f0"\n'
    )
    _cv().atomic_write_text(out_p, body)
    return out_p


def cmd_backup(args):
    """vault 一致性备份 CLI 包装 (含列表/校验与创建)。"""
    vault = _cv().vault_dir(args)
    out_dir = Path(args.out).expanduser() if getattr(args, "out", None) else None
    if getattr(args, "list", False):
        res = list_backups_api(vault, out_dir=out_dir)
        print(f"备份目录: {res['backup_dir']} (共 {res['count']} 份)")
        for it in res["backups"]:
            mark = "OK" if it["valid_zip"] and it["has_db"] else "损坏"
            print(f"  [{mark}] {it['name']}  {it['size_mb']:.2f} MB  "
                  f"{it['snapshots_count']} 快照  {it['mtime']}")
        return
    verify_arg = getattr(args, "verify", None)
    if verify_arg is not None:
        try:
            res = verify_backup_api(vault, verify_arg or None, out_dir=out_dir)
        except (ValueError, FileNotFoundError) as e:
            raise SystemExit(str(e))
        mark = "OK" if res["ok"] else "FAILED"
        print(f"校验 [{mark}]: {res['name']} ({res['size_mb']:.2f} MB · sha256={res['sha256'][:12]}...)")
        print(f"  ZIP CRC={res['crc_ok']} · vault.db={res['db_check']} · 快照={res['zip_snapshots']}")
        if not res["ok"]:
            raise SystemExit(res.get("error") or "备份校验失败")
        return
    try:
        res = backup_vault_api(vault, keep=int(getattr(args, "keep", 10) or 10), out_dir=out_dir)
    except ValueError as e:
        raise SystemExit(str(e))
    for ln in res["logs"]:
        print(ln)

