"""账本汇总、自有用量统计、Headroom/ZCode 导入与 Relay CLI 接线。"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from cv_core import cv_mod as _cv, user_agent

QUOTA_PER_USD = 500000  # new-api 默认换算; 各站倍率不同, 入账标"估算"


def cmd_ledger_import_headroom(args):
    c = _cv()
    vault = c.vault_dir(args)
    conn = c.db(vault)
    conn.executescript(c.SCHEMA)
    n1 = 0
    se = c.HEADROOM_DIR / "savings_events.jsonl"
    if se.exists():
        for obj in c.iter_jsonl(se):
            if obj.get("v") != 1:
                continue
            raw = json.dumps(obj, ensure_ascii=False)
            dup = conn.execute(
                "SELECT 1 FROM ledger_events WHERE source='headroom-savings' AND raw=?",
                (raw,),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "headroom-savings", obj.get("ts"), "compression",
                    obj.get("model"), obj.get("source"), obj.get("client"),
                    int(obj.get("saved", 0)), float(obj.get("cost_usd", 0)), None, raw,
                ),
            )
            n1 += 1
    ps = c.HEADROOM_DIR / "proxy_savings.json"
    if ps.exists():
        conn.execute("DELETE FROM ledger_events WHERE kind='summary' AND source='headroom-lifetime'")
        d = json.loads(ps.read_text(encoding="utf-8"))
        lt = d.get("lifetime", {})
        conn.execute(
            "INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
            "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "headroom-lifetime", d.get("generated_at", c.now_iso()), "summary", None,
                "aggregate", None, int(lt.get("tokens_saved", 0)),
                float(lt.get("compression_savings_usd", 0)), None,
                json.dumps({
                    "cache_read_tokens": lt.get("cache_read_tokens"),
                    "cache_savings_usd": lt.get("cache_savings_usd"),
                    "requests": lt.get("requests"),
                    "total_input_tokens": lt.get("total_input_tokens"),
                    "total_input_cost_usd": lt.get("total_input_cost_usd"),
                }, ensure_ascii=False),
            ),
        )
    conn.commit()
    print(f"账本导入完成: 明细 {n1} 条 (savings_events) + 汇总 1 条 (lifetime)")


def owned_stats(vault: Path) -> dict:
    """Vault 自有账本: 快照内嵌 usage 聚合 (按 lineage 叶子去重)。"""
    c = _cv()
    if not (vault / "vault.db").exists():
        return {
            "snapshots": 0, "disk_snapshots": 0, "sessions": 0, "tokens_in": 0, "tokens_out": 0,
            "cache_read": 0, "cache_write": 0, "total_tokens": 0, "input_effective": 0,
            "cache_hit_rate": 0.0, "by_agent": [],
            "source": "vault-owned (no vault.db yet)",
        }
    snap_dir = vault / "snapshots"
    try:
        disk_snaps = sum(
            1 for e in os.scandir(snap_dir)
            if e.is_file() and e.name.startswith("cv_") and e.name.endswith(".json")
        ) if snap_dir.is_dir() else 0
    except OSError:
        disk_snaps = 0
    conn = c.db(vault)
    try:
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone() is None:
                conn.executescript(c.SCHEMA)
                conn.commit()
        except sqlite3.OperationalError:
            pass
        leaf = "id NOT IN (SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL)"
        versions = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        rows = conn.execute(
            f"SELECT agent, source_format, tokens_in, tokens_out, cache_read, cache_write,"
            f" total_tokens, migration_baseline FROM snapshots WHERE {leaf}"
        ).fetchall()
    finally:
        conn.close()
    ti = to = cr = cw = tt = denom = 0
    agg = {}
    for r in rows:
        i, o, ch, w, t = (
            int(r[k] or 0)
            for k in ("tokens_in", "tokens_out", "cache_read", "cache_write", "total_tokens")
        )
        if r["migration_baseline"]:
            try:
                b = json.loads(r["migration_baseline"]) or {}
            except ValueError:
                b = {}
            i = max(0, i - int(b.get("tokens_in", 0) or 0))
            o = max(0, o - int(b.get("tokens_out", 0) or 0))
            ch = max(0, ch - int(b.get("cache_read", 0) or 0))
            w = max(0, w - int(b.get("cache_write", 0) or 0))
            t = max(0, t - int(b.get("total_tokens", 0) or 0))
        ti += i
        to += o
        cr += ch
        cw += w
        tt += t
        sf = r["source_format"] or ""
        denom += (i + ch + w) if (sf == "claude-projects" or sf.startswith("via-smigrate") or sf.startswith("dsh-")) else i
        a = agg.setdefault(r["agent"], [0, 0])
        a[0] += 1
        a[1] += i
    hit = (cr / denom) if denom else 0.0
    return {
        "snapshots": versions, "disk_snapshots": disk_snaps, "sessions": len(rows),
        "tokens_in": ti, "tokens_out": to,
        "cache_read": cr, "cache_write": cw, "total_tokens": tt,
        "input_effective": denom,
        "cache_hit_rate": round(hit, 3),
        "by_agent": [
            {"agent": k, "snapshots": v[0], "sessions": v[0], "tokens_in": v[1]}
            for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])
        ],
        "source": "vault-owned (from agent session usage records)",
    }


def cv_ledger_sync_cmd(args):
    """薄接线 (延迟导入防循环: cv_ledger 顶层 import cv)。"""
    import cv_ledger
    cv_ledger.cmd_ledger_sync(args)


def cv_ledger_quota_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_quota(args)


def cv_ledger_limit_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_limit(args)


def cv_ledger_plan_token_cmd(args):
    import cv_ledger
    cv_ledger.cmd_ledger_plan_token(args)


def cmd_ledger_stats(args):
    c = _cv()
    vault = c.vault_dir(args)
    if not (vault / "vault.db").exists():
        print("vault 尚无数据库 (账本为空) — 先运行 cv init 或 cv export <会话文件> 建库入账")
        return
    legacy = bool(getattr(args, "legacy_headroom", False))
    conn = c.db(vault)
    try:
        conn.executescript(c.SCHEMA)
    except sqlite3.OperationalError:
        pass
    row = conn.execute("SELECT tokens_saved, cost_usd FROM ledger_events WHERE source='headroom-lifetime'").fetchone()
    if row and legacy:
        raw = json.loads(conn.execute("SELECT raw FROM ledger_events WHERE source='headroom-lifetime'").fetchone()[0])
        print("== lifetime (headroom 通路层累计) ==")
        print(f"  压缩节省: {row['tokens_saved']:,} tokens = {c.fmt_usd(row['cost_usd'])}")
        print(f"  缓存节省: {int(raw.get('cache_read_tokens') or 0):,} cache-read tokens = {c.fmt_usd(raw.get('cache_savings_usd'))}")
        print(f"  总输入:   {int(raw.get('total_input_tokens') or 0):,} tokens = {c.fmt_usd(raw.get('total_input_cost_usd'))}")
    rows = conn.execute(
        "SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c "
        "FROM ledger_events WHERE kind='compression' GROUP BY model ORDER BY c DESC LIMIT 10"
    ).fetchall() if legacy else []
    if rows:
        print("== 按模型 (明细) ==")
        for r in rows:
            print(f"  {r['model']:<24} 事件={r['n']:<6} 省={r['t'] or 0:>12,} tok  {c.fmt_usd(r['c'] or 0)}")
    zx = conn.execute(
        "SELECT COUNT(*) n, model FROM ledger_events WHERE kind='zcode_usage' GROUP BY model ORDER BY n DESC"
    ).fetchall() if legacy else []
    if zx:
        tot = conn.execute(
            "SELECT COUNT(*) n, SUM(CAST(json_extract(raw,'$.input') AS INT)) i,"
            " SUM(CAST(json_extract(raw,'$.cache_read') AS INT)) cr"
            " FROM ledger_events WHERE kind='zcode_usage'"
        ).fetchone()
        print(f"== zcode 用量 ({tot['n']} 次调用, 审计直读) ==")
        print(f"  入 {tot['i'] or 0:,} · cache读 {tot['cr'] or 0:,} · 模型: {', '.join(r['model'] for r in zx[:3])}")
    try:
        import cv_ledger
        us = cv_ledger.usage_stats_summary(vault)
        if us.get("events_total"):
            print(f"== 本地多端账本 ({us['events_total']} 事件,"
                  f" 来源 {json.dumps(us.get('sources') or {}, ensure_ascii=False)}) ==")
            print(f"  近 5h:  总输入 {us['window_5h_total_input']:,} · 输出 {us['window_5h_tokens_out']:,}"
                  f" · 费用估算 {c.fmt_usd(us['window_5h_cost_est_usd'])}")
            print(f"  近 24h: 总输入 {us['window_24h_total_input']:,} · 输出 {us['window_24h_tokens_out']:,}"
                  f" · 费用估算 {c.fmt_usd(us['window_24h_cost_est_usd'])}")
            hit = us.get("cache_hit_rate")
            if hit is not None:
                print(f"  缓存命中率 {hit * 100:.1f}% · 缓存节省约 {c.fmt_usd(us['cache_save_est_usd'])}"
                      f" · 估算事件占比 {(us.get('estimated_share') or 0) * 100:.0f}%")
            print("  (cv ledger quota 看 5h 窗口恢复 ETA 与跑道; cv ledger sync 采集)")
    except sqlite3.OperationalError:
        pass
    own = c.owned_stats(c.vault_dir(args))
    if own["snapshots"]:
        print(f"== vault 自有账本 ({own['sessions']} 独立会话 / {own['snapshots']} 快照版本,"
              f" 零外部依赖, 用量按 lineage 叶子去重) ==")
        print(f"  会话累计消耗: 入 {own['tokens_in']:,} · 出 {own['tokens_out']:,} · "
              f"cache 读 {own['cache_read']:,} (命中率 {own['cache_hit_rate']*100:.0f}%)")
        for a in own["by_agent"][:5]:
            print(f"    {a['agent']:<16} {a['snapshots']:>3} 会话 · {a['tokens_in'] or 0:>13,} tok")


def fetch_relay_usage(base: str, key: str, start: str, end: str) -> dict:
    """OpenAI 兼容 dashboard/billing/usage (new-api/one-api 系普遍支持; 部分中转关闭)。"""
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = f"{base}/dashboard/billing/usage?start_date={start}&end_date={end}"
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent(),
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(
                "该端点未开放 billing API (404) — 实测 999555999 即此情况; 此 relay 的账本只能经 headroom 导入"
            )
        raise SystemExit(f"billing API {e.code}: {e.reason}")


def fetch_relay_web_logs(base: str, token: str, user_id: str | None,
                         path: str, start_ts: int, page_size: int = 100) -> list:
    """new-api / one-api 系 web 消费日志 (/api/log/self, 系统访问令牌认证)。"""
    base = base.rstrip("/")
    out, page = [], 0
    while True:
        url = f"{base}{path}?p={page}&page_size={page_size}&type=0&start_timestamp={start_ts}"
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent(),
            "Authorization": token,
            **({"New-Api-User": user_id} if user_id else {}),
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = (data.get("data") or {}).get("items") or data.get("data") or []
        if not isinstance(items, list) or not items:
            break
        out += items
        if len(items) < page_size:
            break
        page += 1
    return out


def cmd_ledger_import_zcode(args):
    """Zcode 用量入账: ~/.zcode/cli/rollout/model-io-*.jsonl。"""
    c = _cv()
    vault = c.vault_dir(args)
    io_dir = c.HOME / ".zcode" / "cli" / "rollout"
    files = sorted(io_dir.glob("model-io-*.jsonl")) if io_dir.exists() else []
    if not files:
        raise SystemExit("未找到 ~/.zcode/cli/rollout/model-io-*.jsonl (zcode 未使用?)")
    conn = c.db(vault)
    conn.executescript(c.SCHEMA)
    seen = set()
    for (rawj,) in conn.execute("SELECT raw FROM ledger_events WHERE kind='zcode_usage'"):
        try:
            seen.add(json.loads(rawj).get("requestId"))
        except (ValueError, TypeError):
            pass
    n = 0
    agg = {}
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for l in fh:
                try:
                    o = json.loads(l)
                except ValueError:
                    continue
                rid = o.get("requestId")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                resp = o.get("response") or {}
                u = resp.get("usage") or {}
                model = (o.get("model") or {}).get("modelId") or resp.get("modelId") or "zcode"
                tin = int(u.get("inputTokens") or 0)
                tout = int(u.get("outputTokens") or 0)
                tcr = int(u.get("cacheReadTokens") or 0)
                tcw = int(u.get("cacheWriteTokens") or 0)
                conn.execute(
                    "INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                    "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "zcode-modelio", o.get("completedAt") or c.now_iso(), "zcode_usage",
                        model, (o.get("model") or {}).get("providerId"), "zcode", 0, None, None,
                        json.dumps({
                            "requestId": rid, "input": tin, "output": tout,
                            "cache_read": tcr, "cache_write": tcw,
                            "duration_ms": o.get("durationMs"),
                        }, ensure_ascii=False),
                    ),
                )
                a = agg.setdefault(model, [0, 0, 0, 0])
                a[0] += tin
                a[1] += tout
                a[2] += tcr
                a[3] += tcw
                n += 1
    conn.commit()
    print(f"zcode 用量入账: {n} 条新记录 (requestId 幂等)")
    for m, (i, o_, cr, cw) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        print(f"  {m:<12} 入 {i:>12,} · 出 {o_:>9,} · cache读 {cr:>13,} · cache写 {cw:,}")


def cmd_ledger_import_relay(args):
    c = _cv()
    vault = c.vault_dir(args)
    conn = c.db(vault)
    conn.executescript(c.SCHEMA)
    key = c.resolve_key(args)
    if args.mode == "web":
        token = args.token or os.environ.get("CV_RELAY_WEB_TOKEN")
        if not token:
            raise SystemExit(
                "web 模式需要系统访问令牌: --token 或 CV_RELAY_WEB_TOKEN"
                "(中转站 web 个人设置里生成; 999555999 面板待提供令牌后对号入座)"
            )
        wm_p = vault / "relay_watermark.json"
        wm = json.loads(wm_p.read_text(encoding="utf-8")) if wm_p.exists() else {}
        last_ts = int(wm.get(args.url, 0))
        logs = c.fetch_relay_web_logs(args.url, token, args.user_id, args.log_path, last_ts)
        n = 0
        for item in logs:
            ts = int(item.get("created_at", 0))
            quota = int(item.get("quota", 0) or 0)
            conn.execute(
                "INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "relay-web",
                    dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else c.now_iso(),
                    "relay_log", item.get("model_name"), args.url,
                    item.get("channel") or None, 0, round(quota / QUOTA_PER_USD, 6), None,
                    json.dumps({
                        "prompt_tokens": item.get("prompt_tokens"),
                        "completion_tokens": item.get("completion_tokens"),
                        "quota": quota, "use_time": item.get("use_time"),
                        "cost_basis": "quota/500k 估算",
                    }, ensure_ascii=False),
                ),
            )
            n += 1
            last_ts = max(last_ts, ts)
        wm[args.url] = last_ts
        c.atomic_write_text(wm_p, json.dumps(wm))
        conn.commit()
        total = sum(int(i.get("quota", 0) or 0) for i in logs)
        print(f"[{args.url}] 拉取 {n} 条消费记录, ~${total / QUOTA_PER_USD:,.2f} (quota 估算) 已入账; "
              f"水位 {last_ts} (下次增量)")
        return
    end = args.end or dt.date.today().isoformat()
    start = args.start or (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    if not key:
        raise SystemExit("无 API key (CV_RELAY_KEY / --key / headroom-auth-token)")
    data = c.fetch_relay_usage(args.url, key, start, end)
    total = float(data.get("total_usage", 0))
    conn.execute(
        "INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
        "tokens_saved,cost_usd,snapshot_id,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("relay-billing", c.now_iso(), "relay_usage", None, args.url, None, 0,
         total, None, json.dumps({"start": start, "end": end, **data}, ensure_ascii=False)),
    )
    conn.commit()
    print(f"[{args.url}] {start} ~ {end}: ${total:,.2f} 已入账 (source=relay-billing)")
