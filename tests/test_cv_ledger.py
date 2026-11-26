#!/usr/bin/env python3
"""tests/test_cv_ledger.py — 本地多端账本 (usage_events) 回归。

运行: python -m pytest tests/test_cv_ledger.py -v (全量: python -m pytest tests/ -v)
隔离策略: 全部样本在 tmp_path 构造 (真实四端日志结构的最小复刻), monkeypatch
cv.HOME / cv.CODEX_SESSIONS; 绝不触碰真实 vault/ 与真实客户端目录。
覆盖: 四端解析与 event_key 幂等 / claude message.id 去重 / codex 心跳不双计 /
双口径归一化与缓存命中率 / 5h 窗口与恢复 ETA 边界 / 跑道 / 水位含 -wal /
pricing·accounts 配置重算 / 老库迁移 / 无库不建库 / HTTP /ledger/usage / MCP 空库口径。
"""
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_ledger as cl  # noqa: E402

NOW = 1_800_000_000.0  # 固定时钟 (2027-01-15 附近), 窗口/ETA 计算可精确断言


# ---------------------------------------------------------------- 样本构造

def write_lines(path: Path, objs: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n",
                    encoding="utf-8")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离: cv 全局路径指向 tmp + 干净 vault.db (SCHEMA 全套)。ZCODE_DB 指向不存在路径 —
    否则 collect_zcode 的主库真相源路径会吸入真机 db.sqlite (55k 行)。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    conn.close()
    return home, vault


def claude_sample(home: Path):
    """同 msg_1 两个流式内容块 (out 100 → 250): 只保留最大, in 不含 cache (Claude 语义)。"""
    f = home / ".claude" / "projects" / "E--work--demo" / "s-1.jsonl"
    write_lines(f, [
        {"type": "user", "cwd": "E:\\work\\demo", "sessionId": "s-1", "uuid": "u0",
         "timestamp": "2026-11-20T01:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "sessionId": "s-1", "uuid": "u1", "requestId": "req_9",
         "isSidechain": False, "timestamp": "2026-11-20T01:00:05Z",
         "message": {"id": "msg_1", "model": "claude-sonnet-4-5",
                     "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                               "cache_read_input_tokens": 300, "output_tokens": 100}}},
        {"type": "assistant", "sessionId": "s-1", "uuid": "u2", "requestId": "req_9",
         "isSidechain": False, "timestamp": "2026-11-20T01:00:09Z",
         "message": {"id": "msg_1", "model": "claude-sonnet-4-5",
                     "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                               "cache_read_input_tokens": 300, "output_tokens": 250}}},
    ])
    return f


def codex_sample(home: Path):
    """心跳重复 (totals 不变) 不出事件; 第二次真增长出增量。input 含 cache (OpenAI 语义)。"""
    f = (home / ".codex" / "sessions" / "2026" / "11" / "20"
         / "rollout-2026-11-20T10-00-00-th_123.jsonl")
    tot1 = {"input_tokens": 1000, "cached_input_tokens": 300, "cache_write_input_tokens": 0,
            "output_tokens": 50, "reasoning_output_tokens": 10, "total_tokens": 1050}
    tot2 = {"input_tokens": 2000, "cached_input_tokens": 500, "cache_write_input_tokens": 40,
            "output_tokens": 120, "reasoning_output_tokens": 30, "total_tokens": 2120}
    write_lines(f, [
        {"timestamp": "2026-11-20T10:00:00Z", "type": "session_meta",
         "payload": {"id": "th_123", "cwd": "D:\\proj", "cli_version": "0.5"}},
        {"timestamp": "2026-11-20T10:00:01Z", "type": "turn_context",
         "payload": {"cwd": "D:\\proj", "model": "gpt-5.2"}},
        {"timestamp": "2026-11-20T10:01:00Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": tot1}}},
        {"timestamp": "2026-11-20T10:01:05Z", "type": "event_msg",  # 心跳: totals 未变
         "payload": {"type": "token_count", "info": {"total_token_usage": tot1}}},
        {"timestamp": "2026-11-20T10:05:00Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": tot2}}},
    ])
    return f


def iso_utc_ago(sec: float) -> str:
    return dt.datetime.fromtimestamp(time.time() - sec, tz=dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def zcode_sample(home: Path, ago=(600, 540)):
    """双格式: camelCase (input 含 cache) 与 snake_case (Claude 语义), 规范化后可相加。
    时间戳默认取"真机 now-10 分钟", 使 5h/24h 窗口测试可直接用真实时钟。"""
    d = home / ".zcode" / "cli" / "rollout"
    write_lines(d / "model-io-sess_a.jsonl", [
        {"requestId": "r1", "attempt": 1, "completedAt": iso_utc_ago(ago[0]),
         "durationMs": 3100, "model": {"modelId": "GLM-5.3-Flash",
                                       "providerId": "account:bigmodel"},
         "response": {"usage": {"inputTokens": 83206, "outputTokens": 1683,
                                "totalTokens": 84889, "cacheReadTokens": 82240,
                                "cacheWriteTokens": 0}}},
        {"requestId": "r2", "attempt": 1, "completedAt": iso_utc_ago(ago[1]),
         "model": {"modelId": "glm-5.2", "providerId": "x"},
         "response": {"usage": {"input_tokens": 500, "output_tokens": 80,
                                "cache_read_input_tokens": 4000,
                                "cache_creation_input_tokens": 10}}},
    ])
    return d


def agv_db(path: Path, with_ts: bool = True, real_schema: bool = False):
    db = sqlite3.connect(path)
    if real_schema:  # 2026-11 真机 schema: steps 无 data/ts 列, 内容在 step_payload
        db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER,"
                   " has_subtrajectory NUMERIC, metadata BLOB, error_details BLOB,"
                   " step_payload BLOB, step_format INTEGER)")
        db.execute("INSERT INTO steps VALUES(1,14,2,0,?,?,?,0)", (b"m" * 100, None, b"p" * 800))
        db.execute("INSERT INTO steps VALUES(2,15,2,0,?,?,?,0)", (b"m2" * 50, None, b"p2" * 400))
    elif with_ts:
        db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, data BLOB, ts TEXT)")
        db.execute("INSERT INTO steps VALUES(1,?,?)", (b"x" * 400, "2026-11-20T08:00:00Z"))
        db.execute("INSERT INTO steps VALUES(2,?,?)", (b"y" * 800, "2026-11-20T08:01:00Z"))
    else:
        db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, data BLOB)")
        db.execute("INSERT INTO steps VALUES(1,?)", (b"z" * 400,))
    db.commit()
    db.close()


# ---------------------------------------------------------------- 单采集器

def test_claude_message_id_dedup(env):
    home, _ = env
    f = claude_sample(home)
    events, marks = cl.collect_claude()
    assert len(marks) == 1 and len(events) == 1  # 同 msg_1 两块 → 1 事件
    e = events[0]
    assert e["event_key"] == cl._sha("claude|s-1|msg_1")
    assert e["tokens_in"] == 100 and e["tokens_out"] == 250  # 取 output 最大块
    assert e["cache_read"] == 300 and e["cache_write"] == 20  # Claude 语义原样
    assert e["project"] == "E:\\work\\demo" and e["session_ref"] == "s-1"
    assert e["is_estimated"] == 0
    assert abs(e["occurred_at"] - cl.ts_to_epoch("2026-11-20T01:00:09Z")) < 1


def test_claude_fallback_key_reqid_uuid(env, tmp_path):
    home, _ = env
    d = home / ".claude" / "projects" / "E--p"
    write_lines(d / "s-2.jsonl", [
        {"type": "assistant", "sessionId": "s-2", "uuid": "u9", "requestId": "rq1",
         "timestamp": "2026-11-20T02:00:00Z",
         "message": {"model": "m", "usage": {"input_tokens": 5, "output_tokens": 6}}},
    ])
    events, _ = cl.collect_claude()
    assert events[0]["event_key"] == cl._sha("claude|s-2|rq1")  # msg.id 缺省回退 requestId
    (d / "s-3.jsonl").write_text(json.dumps(
        {"type": "assistant", "sessionId": "s-3", "uuid": "u10",
         "timestamp": "2026-11-20T02:00:00Z",
         "message": {"usage": {"input_tokens": 1, "output_tokens": 2}}}), encoding="utf-8")
    events, _ = cl.collect_claude()
    key3 = [e for e in events if e["session_ref"] == "s-3"][0]["event_key"]
    assert key3 == cl._sha("claude|s-3|u10")  # 再回退 uuid


def test_codex_delta_and_heartbeat(env):
    home, _ = env
    codex_sample(home)
    events, _ = cl.collect_codex()
    assert len(events) == 2  # 心跳被跳过
    e1, e2 = events
    assert e1["event_key"] == cl._sha("codex|th_123|1000:50:300")
    assert e2["event_key"] == cl._sha("codex|th_123|2000:120:500")
    # 增量 1: in 1000 (含 cache 300) → 规范化 700; out 50; reason 10
    assert e1["tokens_in"] == 700 and e1["cache_read"] == 300
    assert e1["tokens_out"] == 50 and e1["reasoning_tokens"] == 10
    # 增量 2: in 1000 (cache 200, cw 40) → 规范化 760; out 70
    assert e2["tokens_in"] == 760 and e2["cache_read"] == 200
    assert e2["cache_write"] == 40 and e2["tokens_out"] == 70
    assert e2["model"] == "gpt-5.2" and e2["project"] == "D:\\proj"  # turn_context 归因
    assert e1["session_ref"] == "th_123"


def test_zcode_dual_format(env):
    home, _ = env
    zcode_sample(home)
    events, _ = cl.collect_zcode()
    by = {e["raw"] and json.loads(e["raw"])["requestId"]: e for e in events}
    r1, r2 = by["r1"], by["r2"]
    # camelCase: 83206 - 82240 = 966 (OpenAI 语义规范化)
    assert r1["tokens_in"] == 966 and r1["cache_read"] == 82240 and r1["tokens_out"] == 1683
    # snake_case: Claude 语义原样
    assert r2["tokens_in"] == 500 and r2["cache_read"] == 4000 and r2["cache_write"] == 10
    assert all(e["is_estimated"] == 0 for e in events)
    assert r1["model"] == "GLM-5.3-Flash" and r1["project"] is None


def test_antigravity_estimated_and_wal_watermark(env):
    home, vault = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    agv_db(d / "conv1.db", with_ts=True)
    agv_db(d / "conv2.db", with_ts=False)
    agv_db(d / "conv3.db", real_schema=True)  # 真机 schema: step_payload 无 ts
    events, marks = cl.collect_antigravity()
    assert len(events) == 4  # conv1 逐步 (ts 列) + conv2/conv3 存量基准 (base)
    per_step = [e for e in events if e["session_ref"] == "conv1"]
    assert len(per_step) == 2 and all(e["is_estimated"] == 1 for e in per_step)
    assert per_step[0]["tokens_in"] == 100  # 400 bytes / 4
    assert per_step[0]["event_key"] == cl._sha("agv|conv1|1")
    assert per_step[0]["occurred_at"] == cl.ts_to_epoch("2026-11-20T08:00:00Z")  # 步级真实时刻
    conv2_ev = [e for e in events if e["session_ref"] == "conv2"][0]
    assert conv2_ev["event_key"] == cl._sha("agv|conv2|base")  # 无时间戳老库 → 差量基准
    assert conv2_ev["model"] == "antigravity (估算)"
    conv3_ev = [e for e in events if e["session_ref"] == "conv3"][0]
    # 真机 schema 按 step_payload 体积估算: b"p"*800 (800B) + b"p2"*400 (800B) = 1600B
    assert conv3_ev["tokens_in"] == 1600 // 4
    assert json.loads(conv3_ev["raw"])["content_col"] == "step_payload"
    assert all(e["is_estimated"] == 1 for e in events)
    # 水位: sig 含 wal 字段 (无 -wal 文件时为 0)
    assert marks[0][1] == (marks[0][1][0], marks[0][1][1], 0.0, 0)
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    cl._wm_update(conn, "antigravity", d / "conv1.db", marks[0][1])
    assert not cl._wm_changed(conn, "antigravity", d / "conv1.db", marks[0][1])
    wal = d / "conv1.db-wal"
    wal.write_bytes(b"garbage")  # WAL 出现/增长 → 水位必须判变 (修正4)
    assert cl._wm_changed(conn, "antigravity", d / "conv1.db",
                          (marks[0][1][0], marks[0][1][1],
                           wal.stat().st_mtime, wal.stat().st_size))
    conn.close()


# ---------------------------------------------------------------- sync 幂等 + 归一化

def test_sync_idempotent_twice(env, capsys):
    home, vault = env
    claude_sample(home)
    codex_sample(home)
    zcode_sample(home)
    args = SimpleNamespace(vault=str(vault))
    cl.cmd_ledger_sync(args)
    conn = cv.db(vault)
    n1 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    conn.close()
    out1 = capsys.readouterr().out
    assert "新入账 5 条" in out1  # claude 1 + codex 2 + zcode 2
    cl.cmd_ledger_sync(args)  # 二次全幂等
    conn = cv.db(vault)
    n2 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    conn.close()
    assert n1 == n2 == 5
    assert "新入账 0 条" in capsys.readouterr().out


def test_normalization_cross_client_cache_hit(env):
    """Claude (in 不含 cache) 与 zcode camelCase (in 含 cache) 同 token 形态规范化后可相加。"""
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    # 手工补一条 claude 事件: in 966 (不含 cache) + cr 82240 → total_input 与 r1 相同形态
    conn = cv.db(vault)
    ev = {"event_key": "manual-claude-1", "source": "claude", "client": "claude-code",
          "model": "claude-sonnet-4-5", "project": None, "session_ref": "x",
          "occurred_at": cl.ts_to_epoch("2026-11-20T03:02:00Z"),
          "tokens_in": 966, "tokens_out": 1683, "reasoning_tokens": 0,
          "cache_read": 82240, "cache_write": 0, "is_estimated": 0, "raw": None}
    cl._insert_events(conn, [ev], cl.load_pricing(vault), cl.load_accounts(vault))
    conn.commit()
    conn.close()
    p = cl.usage_payload(vault, now=cl.ts_to_epoch("2026-11-20T04:00:00Z"))
    # r1 83206 + r2 4510 (500+4000+10) + claude 83206 → 三事件规范化总输入可跨端相加
    assert p["totals"]["total_input"] == 83206 + 4510 + 83206
    assert p["cache_hit_rate"] == pytest.approx((82240 + 4000 + 82240)
                                                / (83206 + 4510 + 83206), abs=1e-3)


def test_pricing_builtin_glm_and_override(env):
    """内置表含订阅模型参考价 (glm-5.3-flash, PRICING_REV=2 起收录);
    vault/pricing.json 逐条覆盖内置表 (指纹变更 → 自动重算)。"""
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    row = conn.execute("SELECT cost_est_usd, price_known FROM usage_events"
                       " WHERE model='GLM-5.3-Flash'").fetchone()
    assert row["price_known"] == 1  # 内置表已收录 glm-5.3-flash (公开 API 挂牌价等效折算)
    rule = cl.price_for(cl.load_pricing(vault), "GLM-5.3-Flash")[0]
    builtin_cost = (966 * rule["input"] + 1683 * rule["output"] + 82240 * rule["cache_read"]) / 1e6
    assert abs(row["cost_est_usd"] - builtin_cost) < 1e-6
    conn.close()
    (vault / "pricing.json").write_text(json.dumps(
        {"glm-5.3-flash": {"input": 1.0, "output": 3.0, "cache_read": 0.1}}), encoding="utf-8")
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))  # 指纹变更 → 自动重算 (修正3)
    conn = cv.db(vault)
    row = conn.execute("SELECT cost_est_usd, price_known FROM usage_events"
                       " WHERE model='GLM-5.3-Flash'").fetchone()
    expect = (966 * 1.0 + 1683 * 3.0 + 82240 * 0.1) / 1e6
    assert row["price_known"] == 1
    assert abs(row["cost_est_usd"] - expect) < 1e-6  # 覆盖价生效
    conn.close()
    # 真正未知的模型仍诚实 NULL
    assert cl.price_for(cl.load_pricing(vault), "totally-unknown-model")[0] is None


def test_pricing_sonnet_formula(env):
    home, vault = env
    claude_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    cost = conn.execute("SELECT cost_est_usd FROM usage_events"
                        " WHERE model='claude-sonnet-4-5'").fetchone()[0]
    # (100*3 + 250*15 + 300*0.30 + 20*3.75)/1e6 — in 不含 cache 的规范化口径
    expect = (100 * 3 + 250 * 15 + 300 * 0.30 + 20 * 3.75) / 1e6
    assert abs(cost - expect) < 1e-9
    conn.close()


def test_accounts_resolve_and_recompute(env):
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    (vault / "accounts.json").write_text(json.dumps({
        "profiles": [{"name": "sub-bigmodel",
                      "match": {"source": ["zcode"], "model_prefix": ["glm-*"]}}]}),
        encoding="utf-8")
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) FROM usage_events WHERE account='sub-bigmodel'"
                     ).fetchone()[0]
    conn.close()
    assert n == 2  # 两条 zcode 事件均 glm-* → 归入账号池 (配置变更自动重算)


def test_sync_sources_filter_and_bad(env):
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault), sources="zcode"))
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2
    conn.close()
    with pytest.raises(SystemExit):
        cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault), sources="nope"))


def test_sync_requires_db(tmp_path):
    vault = tmp_path / "empty"
    vault.mkdir()
    with pytest.raises(SystemExit):
        cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))


def test_legacy_db_migration(env):
    """老库 (仅 SCHEMA + headroom 行) 上 sync: usage_events 幂等建表, 旧数据原样。"""
    home, vault = env
    conn = cv.db(vault)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                 "tokens_saved,cost_usd,snapshot_id,raw) VALUES"
                 "('headroom-lifetime','2026-01-01','summary',NULL,'aggregate',NULL,999,1.5,NULL,'{}')")
    conn.commit()
    conn.close()
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2
    lt = conn.execute("SELECT tokens_saved FROM ledger_events"
                      " WHERE source='headroom-lifetime'").fetchone()
    conn.close()
    assert lt["tokens_saved"] == 999  # 历史兼容: headroom 数据不受影响


# ---------------------------------------------------------------- 窗口 / ETA / 跑道

def seed_events(vault, events):
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    # 派生列按 vault 当前 pricing/accounts 现算 (与真实 sync 路径一致)
    cl._insert_events(conn, events, cl.load_pricing(vault), cl.load_accounts(vault))
    conn.commit()
    conn.close()


def ev(key, t_off, tin, tout=0, cr=0, cw=0, acct=None, client="claude-code", model=None,
       source="claude", est=0):
    return {"event_key": key, "source": source, "client": client, "model": model,
            "project": None, "session_ref": key, "occurred_at": NOW - t_off,
            "tokens_in": tin, "tokens_out": tout, "reasoning_tokens": 0,
            "cache_read": cr, "cache_write": cw, "is_estimated": est, "raw": None}


def test_fiveh_window_and_eta_boundaries(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "main", "match": {"source": ["claude"]},
         "window_5h": {"tokens": 1000}}]}), encoding="utf-8")
    # 窗口内两笔: e1 (1h 前, 权重 600), e2 (30m 前, 权重 700); 窗口外一笔不入
    seed_events(vault, [
        ev("e1", 3600, 100, cr=500),   # weight 600
        ev("e2", 1800, 200, tout=500),  # weight 700
        ev("old", 6 * 3600, 99999),     # 6h 前: 窗口外
    ])
    p = cl.usage_payload(vault, now=NOW)
    accs = p["fiveh"]["accounts"]
    assert p["window_sources"] == {"claude": 2}  # 窗口内来源分解 (sources 为全量口径)
    assert len(accs) == 1 and accs[0]["account"] == "main"
    a = accs[0]
    assert a["used_weighted"] == 1300 and a["limit"] == 1000
    assert a["pct"] == pytest.approx(1.3, abs=1e-3)  # 已超限
    # ETA50: 需滑出 1300-500=800 → e1(600) 不足, e2(1300) 达标 → e2+5h
    assert a["eta"]["50%"] == cl.epoch_to_iso(NOW - 1800 + cl.WINDOW_5H)
    # ETA100: 需滑出 1300 → 同为 e2+5h
    assert a["eta"]["100%"] == cl.epoch_to_iso(NOW - 1800 + cl.WINDOW_5H)
    assert p["fiveh"]["totals"]["events"] == 2  # 窗口外事件不计
    assert p["events_total"] == 3


def test_eta_already_recovered(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "main", "match": {"source": ["claude"]}, "window_5h": {"tokens": 1000}}]}),
        encoding="utf-8")
    seed_events(vault, [ev("small", 3600, 50, cr=50)])  # used 100 < 50%×1000
    p = cl.usage_payload(vault, now=NOW)
    a = p["fiveh"]["accounts"][0]
    assert a["eta"]["50%"] == cl.epoch_to_iso(NOW)  # 已达标 → ETA=now
    assert a["pct"] == pytest.approx(0.1, abs=1e-3)


def test_eta_partial_slide_out(tmp_path):
    """部分滑出边界: e1 滑出即达标 (θ=50%) — 取 e1+5h 精确时刻。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "main", "match": {"source": ["claude"]}, "window_5h": {"tokens": 1000}}]}),
        encoding="utf-8")
    seed_events(vault, [ev("e1", 3600, 400), ev("e2", 1800, 500)])  # used 900
    p = cl.usage_payload(vault, now=NOW)
    a = p["fiveh"]["accounts"][0]
    assert a["eta"]["50%"] == cl.epoch_to_iso(NOW - 3600 + cl.WINDOW_5H)   # 400 ≥ 需求400
    assert a["eta"]["80%"] == cl.epoch_to_iso(NOW - 1800 + cl.WINDOW_5H)   # 700 需 e2
    assert a["eta"]["100%"] == cl.epoch_to_iso(NOW - 1800 + cl.WINDOW_5H)


def test_eta_slide_time_not_in_future_edge(tmp_path):
    """事件时间异常超前 (时钟漂移): slide 落在过去 → ETA 回落 now, 不回吐负倒计时。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "main", "match": {"source": ["claude"]}, "window_5h": {"tokens": 100}}]}),
        encoding="utf-8")
    seed_events(vault, [ev("future", -60, 1000)])  # occurred_at 在 now+60s (窗口内)
    p = cl.usage_payload(vault, now=NOW)
    a = p["fiveh"]["accounts"][0]
    assert a["eta"]["100%"] == cl.epoch_to_iso(NOW + 60 + cl.WINDOW_5H)


def test_fiveh_no_limit_pure_consumption(tmp_path):
    """纯消耗模式: 无上限仍有窗口回落 ETA (50%/80%/完全清零) + 7d 单 5h 峰值比 — 绝不隐藏。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    seed_events(vault, [ev("e1", 600, 100, cr=50),   # weight 150
                        ev("old", 7 * 86400 + 1, 999)])  # 7d 外: 不入峰值
    p = cl.usage_payload(vault, now=NOW)
    a = p["fiveh"]["accounts"][0]
    assert a["account"] == "client:claude-code"  # 未配置 → client 伪账号
    assert a["limit"] is None and a["pct"] is None
    assert a["mode"] == "consumption"
    # 回落 50%: 需滑出 75 → e1(150) 达标 → e1+5h; 100%: 完全冷却同点
    assert a["eta"]["50%"] == cl.epoch_to_iso(NOW - 600 + cl.WINDOW_5H)
    assert a["eta"]["80%"] == cl.epoch_to_iso(NOW - 600 + cl.WINDOW_5H)
    assert a["eta"]["100%"] == cl.epoch_to_iso(NOW - 600 + cl.WINDOW_5H)
    assert a["peak_5h"] == 150 and a["pct_of_peak"] == pytest.approx(1.0)
    assert a["share"] == 1.0
    assert p["runway"] == []


def test_empty_window_and_no_db(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    seed_events(vault, [ev("old", 6 * 3600, 100)])
    p = cl.usage_payload(vault, now=NOW)
    assert p["fiveh"]["accounts"] == [] and p["fiveh"]["totals"]["events"] == 0
    assert p["events_total"] == 1
    empty = tmp_path / "nodb"
    empty.mkdir()
    p2 = cl.usage_payload(empty, now=NOW)
    assert p2["events_total"] == 0 and not (empty / "vault.db").exists()  # 无库不建库


def test_runway_7d_and_monthly(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "main", "match": {"source": ["claude"]},
         "period": {"kind": "7d", "tokens": 10000}}]}), encoding="utf-8")
    seed_events(vault, [ev("e1", 3600, 100, cr=500),   # 600
                        ev("e2", 1800, 700),            # 700 → 24h 速率 1300
                        ev("old", 20 * 86400, 500000)])  # 20d 前: 在 7d 期外
    p = cl.usage_payload(vault, now=NOW)
    r = p["runway"][0]
    assert r["kind"] == "7d" and r["cap"] == 10000
    assert r["rate_tokens_per_day"] == 1300
    assert r["used_tokens"] == 1300  # 周期起点 = now-7d, 20d 前事件不计
    assert r["days_left"] == pytest.approx((10000 - 1300) / 1300, abs=0.01)


def test_runway_monthly_usd(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "paid", "match": {"source": ["claude"]},
         "period": {"kind": "monthly", "usd": 10}}]}), encoding="utf-8")
    # 用已知单价模型产生 cost: claude-sonnet-4-5 → cost 现算需要 pricing; seed 时按 vault 配置做账号归因
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    e1 = ev("e1", 3600, 1000, model="claude-sonnet-4-5")
    cl._insert_events(conn, [e1], cl.load_pricing(vault), cl.load_accounts(vault))
    conn.commit()
    conn.close()
    p = cl.usage_payload(vault, now=NOW)
    r = p["runway"][0]
    assert r["metric"] == "usd" and r["cap"] == 10
    cost_e1 = (1000 * 3) / 1e6  # in 1000 × $3/Mtok
    assert r["used_usd"] == pytest.approx(cost_e1, abs=1e-6)
    assert r["days_left"] == pytest.approx((10 - cost_e1) / cost_e1, abs=0.01)


def test_precision_estimated_share(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    seed_events(vault, [ev("exact", 600, 100, est=0), ev("est", 1200, 100, est=1)])
    p = cl.usage_payload(vault, now=NOW)
    assert p["precision"]["exact_events"] == 1 and p["precision"]["estimated_events"] == 1
    assert p["precision"]["estimated_share"] == 0.5
    by_src = p["by_client"]
    assert {x["name"] for x in by_src} == {"claude-code"}


def test_by_model_unknown_source_label(tmp_path):
    """antigravity 无模型事件的排行命名: 「antigravity · 估算」而非 (未知)。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    e = ev("a1", 600, 100, client="antigravity", source="antigravity", est=1)
    cl._insert_events(conn, [e], {}, {})
    conn.commit()
    conn.close()
    p = cl.usage_payload(vault, now=NOW)
    assert p["by_model"][0]["name"] == "antigravity · 估算"


# ---------------------------------------------------------------- 额度上限 (审查后续: 看得到限制)

def test_plan_presets_and_limit_cmd(env, capsys):
    """--list 打印预设; --preset 写入 accounts.json profile (同名覆盖)。"""
    home, vault = env
    cl.cmd_ledger_limit(SimpleNamespace(vault=str(vault), list=True, preset=None,
                                        name=None, tokens_5h=None, tokens_week=None,
                                        source=None, client=None, model_prefix=None))
    out = capsys.readouterr().out
    assert "glm-coding-pro" in out and "600" in out  # GLM Coding Plan Pro ~600 提示/5h
    cl.cmd_ledger_limit(SimpleNamespace(vault=str(vault), list=False, preset="glm-coding-pro",
                                        name="zcode-main", tokens_5h=30000000,
                                        tokens_week=150000000, source="zcode",
                                        client=None, model_prefix=None))
    accs = cl.load_accounts(vault)
    prof = [p for p in accs["profiles"] if p.get("name") == "zcode-main"][0]
    assert prof["window_5h"]["tokens"] == 30000000
    assert prof["period"] == {"kind": "7d", "tokens": 150000000}
    assert prof["match"]["source"] == ["zcode"]
    cl.cmd_ledger_limit(SimpleNamespace(vault=str(vault), list=False, preset="glm-coding-pro",
                                        name="zcode-main", tokens_5h=1, tokens_week=None,
                                        source=None, client=None, model_prefix=None))
    accs = cl.load_accounts(vault)
    assert len([p for p in accs["profiles"] if p.get("name") == "zcode-main"]) == 1  # 同名覆盖不堆积


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._p).encode("utf-8")


def test_fetch_remote_quota_shapes(monkeypatch):
    """厂商额度接口三形状: 成功 / success=false / 网络失败 — 均不抛异常。"""
    ok = {"success": True, "data": {"level": "PRO", "limits": [
        {"type": "TOKENS_LIMIT", "percentage": 42.5, "nextResetTime": "2026-11-21T16:00:00+08:00"}]}}
    seen_auth = []

    def fake_urlp(req, timeout=None):
        seen_auth.append(req.headers.get("Authorization"))
        return _FakeResp(ok)

    monkeypatch.setattr(cl, "urlopen", fake_urlp)
    r = cl.fetch_remote_quota("tok")
    assert r["ok"] and r["plan"] == "PRO"
    assert r["limits"][0]["pct_used"] == 42.5 and r["limits"][0]["reset_at"]
    assert seen_auth[0] == "Bearer tok"  # 先 Bearer, 命中即不再裸试

    monkeypatch.setattr(cl, "urlopen", lambda req, timeout=None: _FakeResp(
        {"success": False, "msg": "bad token"}))
    r2 = cl.fetch_remote_quota("tok")
    assert not r2["ok"] and "bad token" in r2["error"]

    def boom(req, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(cl, "urlopen", boom)
    r3 = cl.fetch_remote_quota("tok")
    assert not r3["ok"] and "refused" in r3["error"]


def test_remote_quota_cached_and_merge(env, monkeypatch):
    """无 token → None (不附字段); 有 token → TTL 缓存 + /ledger/usage 合并 remote_quota。"""
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    monkeypatch.delenv("CV_PLAN_TOKEN", raising=False)
    assert cl.remote_quota_cached(vault, now=1000.0) is None  # 未配置 → None
    monkeypatch.setenv("CV_PLAN_TOKEN", "tok")
    calls = {"n": 0}

    def fake_fetch(token, url=None, timeout=None):
        calls["n"] += 1
        return {"ok": True, "plan": "LITE", "limits": [
            {"type": "TOKENS_LIMIT", "pct_used": 10.0, "reset_at": None}]}

    monkeypatch.setattr(cl, "fetch_remote_quota", fake_fetch)
    monkeypatch.setattr(cl, "_REMOTE_Q", {"at": 0.0, "data": None})  # 清缓存
    r1 = cl.remote_quota_cached(vault, ttl=60.0, now=1000.0)
    assert r1["ok"] and calls["n"] == 1
    r2 = cl.remote_quota_cached(vault, ttl=60.0, now=1030.0)
    assert calls["n"] == 1 and r2 is r1  # TTL 内命中缓存
    r3 = cl.remote_quota_cached(vault, ttl=60.0, now=2000.0)
    assert calls["n"] == 2  # 过期重拉
    # /ledger/usage 处理路径合并 (cv.py 经 cv_ledger.remote_quota_cached)
    import threading, http.client
    srv, _idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/ledger/usage?window=5h")
        body = json.loads(c.getresponse().read().decode("utf-8"))
        assert body.get("remote_quota", {}).get("plan") == "LITE"
    finally:
        srv.shutdown()
        srv.server_close()


def test_agv_hub_discovery_and_path_a(env, monkeypatch):
    """官方发现文件路线: daemon/hub_*.json (pid/双端口/csrfToken), 陈旧 pid 跳过、
    活实例 GetUserStatus 探活; 路径 A 解析 (真机形状): userTier.name 作 plan,
    clientModelConfigs[].quotaInfo 按模型池去重 (档位后缀剥离), 无 quotaInfo 模型跳过。"""
    home, _ = env
    daemon = home / ".gemini" / "antigravity" / "daemon"
    daemon.mkdir(parents=True)
    (daemon / "hub_1111.json").write_text(json.dumps(
        {"pid": 999999, "httpsPort": 1110, "httpPort": 1111, "lsVersion": "2.33.0",
         "csrfToken": "stale-token-aaaaaaaa"}), encoding="utf-8")
    (daemon / "hub_13802.json").write_text(json.dumps(
        {"pid": 42424, "httpsPort": 13801, "httpPort": 13802, "lsVersion": "2.34.0",
         "csrfToken": "live-token-bbbbbbbb"}), encoding="utf-8")
    STATUS_RESP = {"userStatus": {
        "userTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
        "cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "Gemini 4 Neon (High)",
             "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-21T15:11:29Z"}},
            {"label": "Gemini 4 Neon (Medium)",
             "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-21T15:11:29Z"}},
            {"label": "Gemini 4 Argon (High)",
             "quotaInfo": {"remainingFraction": 0.8598132, "resetTime": "2026-11-28T08:55:05Z"}},
            {"label": "Claude 4.5 Sonnet",
             "quotaInfo": {"remainingFraction": 0.5, "resetTime": "2026-11-21T16:00:00Z"}},
            {"label": "Legacy Model", "modelOrAlias": {"model": "M_OLD"}},  # 无 quotaInfo
        ]}}}
    calls = []

    def fake_call(port, csrf, method, body=None, timeout=None, scheme=None):
        calls.append((port, csrf, method))
        assert csrf == "live-token-bbbbbbbb"  # 陈旧 hub 文件的 token 绝不出手
        if method == cl.AGV_LSP_STATUS:
            assert body == cl.AGV_STATUS_BODY  # GetUserStatus 带 metadata 标准体
            return STATUS_RESP, "http"
        return {"response": {"groups": []}}, "http"

    monkeypatch.setattr(cl, "_agv_call", fake_call)
    monkeypatch.setattr(cl, "_pid_alive", lambda pid: pid == 42424)
    creds = cl.discover_agv_lsp_credentials()
    assert creds == (42424, "live-token-bbbbbbbb", 13802, "http")
    r = cl.fetch_agv_quota(creds=creds)
    assert r["ok"] and r["plan"] == "Antigravity Google AI Pro"
    types = {l["type"]: l for l in r["limits"]}
    # 档位后缀剥离 + 同池去重: Neon (High/Medium) 同 (池,1.0,reset) 合一行
    assert "Gemini 4 Neon 5h" in types and types["Gemini 4 Neon 5h"]["pct_used"] == 0.0
    assert types["Gemini 4 Argon weekly"]["pct_used"] == pytest.approx(14.0, abs=0.01)
    assert types["claude/gpt 5h"]["pct_used"] == 50.0
    assert len(r["limits"]) == 3 and "Legacy Model" not in "".join(types)
    assert r["limits"][0]["pct_used"] >= r["limits"][-1]["pct_used"]  # 用得多在前
    assert calls and calls[0][2] == cl.AGV_LSP_STATUS


def test_agv_quota_path_b_groups(monkeypatch):
    """路径 B: RetrieveUserQuotaSummary groups/buckets — 顶层与 response 包装都兼容;
    proto3 零值省略: remainingFraction 键缺省 = 0.0 剩余 (已用 100%);
    路径 A 无 quotaInfo 时自动落 B; 双路径全空 → ok:false 带原因。"""
    STATUS_EMPTY = {"userStatus": {"userTier": {"name": "Free"}}}
    calls = {"quota": 0}

    def fake_call(port, csrf, method, body=None, timeout=None, scheme=None):
        if method == cl.AGV_LSP_STATUS:
            return STATUS_EMPTY, "http"
        calls["quota"] += 1
        if calls["quota"] == 1:  # 顶层 groups (无 response 包装)
            return {"groups": [{"displayName": "Claude & GPT (3p)", "buckets": [
                {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.5,
                 "resetTime": "2026-11-21T16:00:00Z"},
                {"bucketId": "3p-weekly", "window": "weekly"}]}]}, "http"
        if calls["quota"] == 2:  # response 包装 (真机形状)
            return {"response": {"groups": [{"displayName": "Gemini Models", "buckets": [
                {"bucketId": "gemini-5h", "remainingFraction": 1}]}]}}, "https"
        return {"success": False}, None

    monkeypatch.setattr(cl, "_agv_call", fake_call)
    creds = (55836, "tok-aaaa", 13802, "http")
    r1 = cl.fetch_agv_quota(creds=creds)
    assert r1["ok"] and r1["plan"] == "Antigravity Free"
    types = {l["type"]: l for l in r1["limits"]}
    assert types["claude/gpt 5h"]["pct_used"] == 50.0
    assert types["claude/gpt weekly"]["pct_used"] == 100.0  # proto3 缺省 = 剩余 0
    r2 = cl.fetch_agv_quota(creds=creds)
    assert r2["limits"][0]["type"] == "gemini 5h" and r2["limits"][0]["pct_used"] == 0.0
    r3 = cl.fetch_agv_quota(creds=creds)
    assert not r3["ok"] and "无 quota" in r3["error"]  # 双路径全空 → 诚实失败


def test_agv_legacy_cmdline_discovery(monkeypatch):
    """旧版 IDE 发现分支 (nt=PowerShell/netstat, posix=ps/lsof) 双平台可 mock 且语义一致;
    正则放宽 (含大小写字母/点/下划线 token) + 命令行端口补充 + --standalone 优先。"""
    import os as _os
    import tempfile
    monkeypatch.setattr(cv, "HOME", Path(tempfile.mkdtemp()))  # 隔离真机 hub 文件 (路线 1 不介入)
    native_name = _os.name
    PS_LINES = ("1234\tC:\\agv\\language_server.exe --csrf_token abcd.1234_x --standalone\n"
                "5678\tC:\\x\\language_server.exe --csrf_token deadbeef-1111\n")
    NETSTAT_LINES = ("  TCP    127.0.0.1:51024     0.0.0.0:0    LISTENING    1234\n"
                     "  TCP    127.0.0.1:51025     0.0.0.0:0    LISTENING    9999\n")

    def patch_discovery(monkeypatch, empty=False):
        ps = "" if empty else PS_LINES
        ns = "" if empty else NETSTAT_LINES

        def fake_lsof(pid):
            if not empty and pid == 1234:
                return f"cmd {pid} u TCP 127.0.0.1:51024 (LISTEN)\n"
            return ""

        if _os.name == "nt":
            monkeypatch.setattr(cl, "_powershell_out",
                                lambda s, timeout=None: ps if "language_server" in s else "")
            monkeypatch.setattr(cl, "_netstat_out", lambda: ns)
        else:
            monkeypatch.setattr(cl, "_ps_env_out", lambda: ps)
            monkeypatch.setattr(cl, "_lsof_out", fake_lsof)

    def fake_call(port, csrf, method, body=None, timeout=None, scheme=None):
        assert csrf == "abcd.1234_x"  # standalone 优先 + 放宽字符集 token
        if port == 51024 and method == cl.AGV_LSP_STATUS:
            return {"userStatus": {}}, "http"
        return None, None

    monkeypatch.setattr(cl, "_agv_call", fake_call)
    # 1) 当前平台真实分支
    patch_discovery(monkeypatch)
    creds = cl.discover_agv_lsp_credentials()
    assert creds[0] == 1234 and creds[1] == "abcd.1234_x" and creds[2] == 51024
    # 2) 模拟对侧平台分支 (跨平台互测, ubuntu CI 也全绿)
    monkeypatch.setattr(_os, "name", "posix" if native_name == "nt" else "nt")
    patch_discovery(monkeypatch)
    creds_b = cl.discover_agv_lsp_credentials()
    assert creds_b[0] == 1234 and creds_b[2] == 51024
    # 3) 未运行: 全空 → None
    monkeypatch.setattr(_os, "name", native_name)
    patch_discovery(monkeypatch, empty=True)
    assert cl.discover_agv_lsp_credentials() is None


def test_plan_token_cmd(env, monkeypatch, capsys):
    """plan-token 写入 + 验证 + 清除 + 脱敏状态。"""
    home, vault = env
    monkeypatch.delenv("CV_PLAN_TOKEN", raising=False)
    monkeypatch.setattr(cl, "fetch_remote_quota",
                        lambda tok, url=None, timeout=None: {
                            "ok": True, "plan": "LITE",
                            "limits": [{"type": "TOKENS_LIMIT", "pct_used": 7.0,
                                        "reset_at": "2026-11-21T16:00:00+08:00"}]})
    cl.cmd_ledger_plan_token(SimpleNamespace(vault=str(vault), token="  secret-token-99  ",
                                             clear=False))
    out = capsys.readouterr().out
    assert "已写入" in out and "验证成功" in out and "LITE" in out
    assert cl.load_plan_token(vault) == "secret-token-99"
    cl.cmd_ledger_plan_token(SimpleNamespace(vault=str(vault), token=None, clear=False))
    out2 = capsys.readouterr().out
    assert "****" in out2 and "secret-token-99" not in out2  # 脱敏
    cl.cmd_ledger_plan_token(SimpleNamespace(vault=str(vault), token=None, clear=True))
    assert cl.load_plan_token(vault) is None


def test_account_filter(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    e1 = ev("e1", 600, 100)
    e2 = {"event_key": "e2", "source": "zcode", "client": "zcode", "model": None,
          "project": None, "session_ref": "e2", "occurred_at": NOW - 600,
          "tokens_in": 300, "tokens_out": 0, "reasoning_tokens": 0,
          "cache_read": 0, "cache_write": 0, "is_estimated": 0, "raw": None}
    # e1 显式账号 main, e2 无账号 (client 伪账号)
    cl._insert_events(conn, [e1], {}, {"profiles": [{"name": "main",
                                                     "match": {"source": ["claude"]}}]})
    cl._insert_events(conn, [e2], {}, {})
    conn.commit()
    conn.close()
    p_main = cl.usage_payload(vault, window="24h", account="main", now=NOW)
    assert p_main["totals"]["total_input"] == 100
    p_zc = cl.usage_payload(vault, window="24h", account="client:zcode", now=NOW)
    assert p_zc["totals"]["total_input"] == 300
    p_all = cl.usage_payload(vault, window="24h", now=NOW)
    assert p_all["totals"]["total_input"] == 400


# ---------------------------------------------------------------- agm / HTTP / MCP

def test_agm_unreachable_skips(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    res = cl.sync_from_agm(vault, "http://127.0.0.1:1/agm")
    assert "skipped" in res  # 不可达静默跳过, 绝不抛


def test_http_ledger_usage_and_stats(env):
    """无 headroom 场景: /ledger/usage 与 /stats 均可用且含 usage 块。"""
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    import threading, http.client
    srv, _idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/ledger/usage?window=24h")
        r = json.loads(c.getresponse().read().decode("utf-8"))
        assert r["events_total"] == 2
        assert r["totals"]["total_input"] == 966 + 500 + 82240 + 4000 + 10
        assert r["fiveh"]["accounts"][0]["account"] == "client:zcode"
        c.request("GET", "/stats")
        s = json.loads(c.getresponse().read().decode("utf-8"))
        assert s["usage"]["events_total"] == 2
        assert "owned" in s and "stats" in s  # 原有形状保持
        c.request("GET", "/ledger/usage?window=bad")
        assert c.getresponse().status == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_mcp_t_stats_empty_and_usage(env, monkeypatch):
    """修正5: 全空仍「账本为空」; 有 usage_events 则无 headroom 也输出多端汇总。"""
    import mcp_server
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")  # 强制冷路径
    home, vault = env
    out = mcp_server.t_stats(vault)
    assert "账本为空" in out
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    out2 = mcp_server.t_stats(vault)
    assert "5 小时" in out2 and "来源 zcode 2" in out2
    # 24h 总输入 = (966+82240) + (500+4000+10) = 87,716
    assert "87,716" in out2


def test_quota_cli_output(env, capsys):
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    cl.cmd_ledger_quota(SimpleNamespace(vault=str(vault)))
    out = capsys.readouterr().out
    assert "5h 消耗构成 (按客户端归因" in out and "client:zcode" in out
    assert "未配置上限" not in out  # 无额度池时不再逐行喊未设上限
    cl.cmd_ledger_quota(SimpleNamespace(vault=str(vault), json=True))
    j = json.loads(capsys.readouterr().out)
    assert j["events_total"] == 2


def test_quota_empty_message(env, capsys):
    _, vault = env
    cl.cmd_ledger_quota(SimpleNamespace(vault=str(vault)))
    assert "usage_events 为空" in capsys.readouterr().out


# ---------------------------------------------------------------- 审查回归 (2026-11-21)

def test_runway_isolated_per_account(tmp_path):
    """审查#1 回归: _runway 按账号过滤 — 多账号速率/已用/剩余互不串账。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "accounts.json").write_text(json.dumps({"profiles": [
        {"name": "a1", "match": {"source": ["claude"]}, "period": {"kind": "7d", "tokens": 10000}},
        {"name": "a2", "match": {"source": ["zcode"]}, "period": {"kind": "7d", "tokens": 5000}}]}),
        encoding="utf-8")
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    accs = cl.load_accounts(vault)
    # a1 (claude): 24h 内权重 600+700=1300; a2 (zcode): 300
    cl._insert_events(conn, [ev("c1", 3600, 100, cr=500), ev("c2", 1800, 700)], {}, accs)
    cl._insert_events(conn, [ev("z1", 3600, 300, client="zcode", source="zcode")], {}, accs)
    conn.commit()
    conn.close()
    p = cl.usage_payload(vault, now=NOW)
    by = {r["account"]: r for r in p["runway"]}
    assert set(by) == {"a1", "a2"}
    assert by["a1"]["rate_tokens_per_day"] == 1300 and by["a1"]["used_tokens"] == 1300
    assert by["a1"]["days_left"] == pytest.approx((10000 - 1300) / 1300, abs=0.01)
    assert by["a2"]["rate_tokens_per_day"] == 300 and by["a2"]["used_tokens"] == 300
    assert by["a2"]["days_left"] == pytest.approx((5000 - 300) / 300, abs=0.01)
    # 修复前: a2 会把 a1 的 1300 速率算到自己头上 (rate=1600) — 此断言防回归


def test_antigravity_active_conversation_delta(env, monkeypatch):
    """审查#2 回归 (升级差量记账语义): 无步级时间戳的老库 — 存量全量锚在最早已知时间
    (base 事件), 文件增长只对滚动 delta 事件按 mtime 记账, 老存量不进当前窗口。"""
    home, vault = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    f = d / "convG.db"
    agv_db(f, real_schema=True)  # step_payload 1600B → 基准估算 400
    anchor = 1_600_000_000.0  # 老会话 (2020): 早于文件 st_ctime 等一切真实证据
    state = {"convG": {"base_bytes": 0, "anchor": anchor}}
    monkeypatch.setattr(cl, "_agv_base_state", lambda conn: state)
    args = SimpleNamespace(vault=str(vault))
    cl.cmd_ledger_sync(args)
    conn = cv.db(vault)
    rows = conn.execute("SELECT event_key, tokens_in, occurred_at FROM usage_events"
                        " WHERE source='antigravity'").fetchall()
    conn.close()
    assert len(rows) == 1  # 首次观测: 仅存量基准事件
    base = rows[0]
    assert base["event_key"] == cl._sha("agv|convG|base")
    assert base["tokens_in"] == 400
    assert base["occurred_at"] == anchor  # 锚在会话最早已知时间, 不污染当前窗口
    state["convG"]["base_bytes"] = 400 * 4  # 基准已入库 (sync 层下轮从 db 读出同值)
    db = sqlite3.connect(f)  # 模拟活跃会话继续: 追加 6000B 的更大 step
    db.execute("INSERT INTO steps VALUES(3,15,2,0,?,?,?,0)",
               (b"m3" * 10, None, b"big" * 2000))
    db.commit()
    db.close()
    cl.cmd_ledger_sync(args)
    conn = cv.db(vault)
    rows = conn.execute("SELECT event_key, tokens_in FROM usage_events"
                        " WHERE source='antigravity'").fetchall()
    conn.close()
    byk = {r["event_key"]: r["tokens_in"] for r in rows}
    assert len(rows) == 2  # base 保持不变 + 滚动 delta
    assert byk[cl._sha("agv|convG|base")] == 400  # 存量不变
    assert byk[cl._sha("agv|convG|delta")] == 6000 // 4  # 只记新增 1500


def test_antigravity_metadata_ts_per_step(env, monkeypatch):
    """步级时间戳 (metadata protobuf): 每步落真实时刻, 历史不再按 mtime 灌入近 5h;
    legacy 会话级 |conv 行被一次性迁移删除。"""
    home, vault = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    f = d / "convT.db"
    db = sqlite3.connect(f)
    db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER,"
               " has_subtrajectory NUMERIC, metadata BLOB, error_details BLOB,"
               " step_payload BLOB, step_format INTEGER)")
    def meta_pb(sec):
        # 复刻原生 metadata: 顶层 f1 (wiretype2) → Timestamp{f1: 秒 varint, f2: 纳秒 varint}
        def varint(v):
            v = int(v)
            out = b""
            while True:
                b7 = v & 0x7F
                v >>= 7
                out += bytes([b7 | (0x80 if v else 0)])
                if not v:
                    return out
        ts = b"\x08" + varint(sec) + b"\x10" + varint(500_000_000)
        return b"\x0a" + bytes([len(ts)]) + ts
    t_old = NOW - 9 * 3600   # 9h 前: 窗口外
    t_now = NOW - 600        # 10m 前: 窗口内
    db.execute("INSERT INTO steps VALUES(1,14,2,0,?,?,?,0)", (meta_pb(t_old), None, b"a" * 400))
    db.execute("INSERT INTO steps VALUES(2,15,2,0,?,?,?,0)", (meta_pb(t_now), None, b"b" * 800))
    db.commit()
    db.close()
    # legacy 会话级行 (升级前残留, raw 含 step_idx=conv): 必须被一次性迁移删除
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO usage_events(event_key,source,client,model,project,"
                 "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,"
                 "cache_write,cost_est_usd,is_estimated,price_known,raw) VALUES("
                 "'legacy-conv-key','antigravity','antigravity',NULL,NULL,'convT',?,9999,0,0,0,0,NULL,1,0,"
                 "'{\"step_idx\": \"conv\", \"content_col\": \"step_payload\"}')",
                 (NOW - 60,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(cl, "agv_workspaces", lambda: {"convT": "E:\\proj\\alpha"})
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE event_key='legacy-conv-key'"
                        ).fetchone()[0] == 0  # legacy 会话级全量行已迁移删除
    rows = conn.execute("SELECT occurred_at, tokens_in, project, model, session_ref"
                        " FROM usage_events WHERE source='antigravity'"
                        " ORDER BY occurred_at").fetchall()
    conn.close()
    assert len(rows) == 2  # 每步一事件 (900 窗口外 + 200 窗口内)
    assert rows[0]["occurred_at"] == t_old and rows[0]["tokens_in"] == 100
    assert rows[1]["occurred_at"] == t_now and rows[1]["tokens_in"] == 200
    assert rows[0]["project"] == "E:\\proj\\alpha"  # workspace_uris 回填
    assert rows[0]["model"] == "antigravity (估算)"
    # 近 5h 窗口只剩新步骤 (200 tok), 900 tok 的老步骤留在其真实时刻
    p = cl.usage_payload(vault, now=NOW)
    assert p["fiveh"]["totals"]["total_input"] == 200


def test_antigravity_leftover_base_and_model_label(tmp_path):
    """混合: 部分步骤无时间戳 → 无 ts 字节并一条 base 补遗 (锚在最早步); 模型命名估算标签。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    e1 = {"event_key": cl._sha("agv|c1|base"), "source": "antigravity", "client": "antigravity",
          "model": "antigravity (估算)", "project": None, "session_ref": "c1",
          "occurred_at": NOW - 3600, "tokens_in": 25, "tokens_out": 0, "reasoning_tokens": 0,
          "cache_read": 0, "cache_write": 0, "is_estimated": 1, "raw": None}
    e2 = {"event_key": cl._sha("agv|c2|1"), "source": "antigravity", "client": "antigravity",
          "model": None, "project": None, "session_ref": "c2",
          "occurred_at": NOW - 600, "tokens_in": 100, "tokens_out": 0, "reasoning_tokens": 0,
          "cache_read": 0, "cache_write": 0, "is_estimated": 1, "raw": None}
    cl._insert_events(conn, [e1, e2], {}, {})
    conn.commit()
    conn.close()
    p = cl.usage_payload(vault, now=NOW)
    names = {r["name"] for r in p["by_model"]}
    # 采集层标注 "antigravity (估算)"; model=None 的聚合兜底为 "antigravity · 估算" — 双路径并存
    assert "antigravity (估算)" in names and "antigravity · 估算" in names


def test_watermark_skips_unchanged_files(env, monkeypatch, capsys):
    """审查#2 回归: 二轮 sync 未变更文件零重读 (skip_fn 水位跳过)。"""
    home, vault = env
    zcode_sample(home)
    calls = {"n": 0}
    orig = cv.iter_jsonl

    def counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(cv, "iter_jsonl", counting)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    assert calls["n"] >= 1
    out1 = capsys.readouterr().out
    assert "变更 1 · 跳过 0" in out1  # 首轮: 唯一文件真变更
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    assert calls["n"] == 1  # 二轮: 未变更 → 不再读盘 (修复前会重读)
    assert "跳过 1" in capsys.readouterr().out


def test_collectors_skip_fn(env):
    """采集器级: skip_fn 为真 → 不产生事件, 但水位条目照记。"""
    home, _ = env
    zcode_sample(home)
    events, marks = cl.collect_zcode(skip_fn=lambda f, sig: True)
    assert events == [] and len(marks) == 1
    home2 = home  # 未跳过路径仍正常
    events2, _ = cl.collect_zcode(skip_fn=lambda f, sig: False)
    assert len(events2) == 2


def make_zcode_main_db(path: Path, rows: list, sessions: list = None):
    """复刻 ZCode 主库 model_usage 最小结构 (毫秒 epoch 字符串, input 含缓存);
    sessions 可选: [(id, project_id, directory, title)] — 项目归因主源的 schema 复刻。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE model_usage(id TEXT PRIMARY KEY, session_id TEXT, model_id TEXT,"
               " agent TEXT, started_at TEXT, completed_at TEXT, input_tokens INTEGER,"
               " output_tokens INTEGER, reasoning_tokens INTEGER,"
               " cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,"
               " provider_id TEXT, logical_request_id TEXT)")
    db.executemany("INSERT INTO model_usage VALUES(" + ",".join("?" * 13) + ")", rows)
    if sessions:
        db.execute("CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT,"
                   " directory TEXT, title TEXT)")
        db.executemany("INSERT INTO session VALUES(" + ",".join("?" * 4) + ")", sessions)
    db.commit()
    db.close()


def test_zcode_db_truth_source_and_migration(env, monkeypatch):
    """统一口径: 主库 model_usage 升为 zcode 真相源 —
    全量历史入账 (毫秒 ts + 含缓存规范化), 一次性移除 model-io 零头防双计, 无主库时回落。"""
    home, vault = env
    zcode_sample(home)  # 旧 model-io 零头: 先入账
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE source='zcode'"
                        ).fetchone()[0] == 2
    conn.close()
    # 主库出现 (真机结构复刻): 两条全量调用, input 含缓存
    db_path = home / ".zcode" / "cli" / "db" / "db.sqlite"
    make_zcode_main_db(db_path, [
        ("u1", "sess_a", "GLM-5.3-Flash", "zcode-agent", "1795248538256",
         "1795248551856", 437504, 594, 0, 768, 436736, "account:bigmodel",
         "msg_a"),
        ("u2", "sess_a", "GLM-5.3", "zcode-agent", "1795249797700",
         "1795249854685", 441350, 2698, 500, 0, 437504, "account:bigmodel",
         "msg_b"),
    ])
    monkeypatch.setattr(cv, "ZCODE_DB", db_path)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    rows = conn.execute("SELECT event_key, occurred_at, tokens_in, cache_read, raw"
                        " FROM usage_events WHERE source='zcode' ORDER BY event_key").fetchall()
    # model-io 零头 2 条已被一次性迁移移除 (防双计), 只剩主库 2 条
    assert len(rows) == 2
    u1 = [r for r in rows if r["event_key"] == cl._sha("zdb|u1")][0]
    assert u1["tokens_in"] == 437504 - 436736 - 768  # 规范化: input 含缓存 → 扣除 (恒 0)
    assert u1["cache_read"] == 436736
    assert abs(u1["occurred_at"] - 1795248551856 / 1000.0) < 1e-6  # 毫秒 epoch → 秒
    conn.close()
    # 二次 sync 幂等 (水位 + INSERT OR IGNORE)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM usage_events WHERE source='zcode'"
                        ).fetchone()[0] == 2
    conn.close()
    # 主库消失 (旧版 ZCode) → 回落 model-io, 不误删迁移标记
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")
    events, marks = cl.collect_zcode()
    assert len(events) == 2 and len(marks) == 1


# ---------------------------------------------------------------- 项目归因 + headroom 隐藏 (2026-11-21 反馈轮)

def test_zcode_project_from_session_table(env, monkeypatch):
    """项目归因主源: 主库 session.directory 同库 join → 事件 project (消除满屏 (未标注))。"""
    home, vault = env
    db_path = home / ".zcode" / "cli" / "db" / "db.sqlite"
    make_zcode_main_db(db_path, [
        ("u1", "sess_a", "GLM-5.3-Flash", "zcode-agent", "1795248538256",
         "1795248551856", 437504, 594, 0, 768, 436736, "account:bigmodel", "msg_a"),
    ], sessions=[("sess_a", "proj_x", r"D:\goai", "t")])
    monkeypatch.setattr(cv, "ZCODE_DB", db_path)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    r = conn.execute("SELECT project FROM usage_events WHERE event_key=?",
                     (cl._sha("zdb|u1"),)).fetchone()
    conn.close()
    assert r["project"] == r"D:\goai"


def test_zcode_project_backfill_existing(env, monkeypatch):
    """存量 zcode 事件 (project 空) 经一次性迁移回填: session.directory 主源 +
    vault snapshots 兜底; 旗标幂等, 二跑不再改写。"""
    home, vault = env
    db_path = home / ".zcode" / "cli" / "db" / "db.sqlite"
    make_zcode_main_db(db_path, [], sessions=[
        ("sess_db", None, r"D:\from-session", "t"),
        ("sess_snap", None, r"D:\stale", "older")])
    monkeypatch.setattr(cv, "ZCODE_DB", db_path)
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    # snapshots 兜底源: sess_snap 的库行 directory 是过时值, 快照 cwd 更新 → 应以快照兜底
    conn.execute("INSERT INTO snapshots(id, created_at, agent, session_ref, cwd) VALUES"
                 r"('cv_snap1', '2026-11-20T10:00:00+08:00', 'zcode', 'sess_snap', 'D:\fresh')")
    for key, ref in (("zdb1", "sess_db"), ("zdb2", "sess_snap")):
        e = {"event_key": key, "source": "zcode", "client": "zcode", "model": None,
             "project": None, "session_ref": ref, "occurred_at": NOW - 600,
             "tokens_in": 10, "tokens_out": 0, "reasoning_tokens": 0, "cache_read": 0,
             "cache_write": 0, "is_estimated": 0, "raw": None}
        cl._insert_events(conn, [e], {}, {})
    conn.commit()
    conn.close()
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    p1 = conn.execute("SELECT project FROM usage_events WHERE event_key='zdb1'").fetchone()["project"]
    p2 = conn.execute("SELECT project FROM usage_events WHERE event_key='zdb2'").fetchone()["project"]
    conn.close()
    assert p1 == r"D:\from-session"  # 主源
    assert p2 == r"D:\fresh"  # snapshots 兜底 (快照 cwd 覆盖同 session_ref)


def test_zcode_modelio_proj_map(env):
    """model-io 回落路径: proj_map (vault snapshots) 兜底项目归因。"""
    home, _ = env
    zcode_sample(home)
    events, _ = cl.collect_zcode(proj_map={"sess_a": r"D:\proj"})
    assert events and all(e["project"] == r"D:\proj" for e in events)


def test_ledger_stats_legacy_headroom_flag(env, capsys):
    """cv ledger stats 默认彻底隐藏 headroom 段落; --legacy-headroom 显式输出。"""
    home, vault = env
    conn = cv.db(vault)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                 "tokens_saved,cost_usd,snapshot_id,raw) VALUES"
                 "('headroom-lifetime','2026-01-01','summary',NULL,'aggregate',NULL,999,1.5,NULL,"
                 "'{\"cache_read_tokens\": 12345, \"cache_savings_usd\": 0.5}')")
    conn.commit()
    conn.close()
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault), legacy_headroom=False))
    out = capsys.readouterr().out
    assert "headroom" not in out and "lifetime" not in out  # 存有旧记录也彻底隐藏
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault), legacy_headroom=True))
    out2 = capsys.readouterr().out
    assert "lifetime (headroom 通路层累计)" in out2 and "999" in out2  # 显式旗标可见


def test_mcp_stats_no_headroom(env, monkeypatch):
    """vault_stats 即使库中存有旧 headroom-lifetime 记录也不再输出 headroom 段落。"""
    import mcp_server
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")  # 强制冷路径
    home, vault = env
    conn = cv.db(vault)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind,model,provider,client,"
                 "tokens_saved,cost_usd,snapshot_id,raw) VALUES"
                 "('headroom-lifetime','2026-01-01','summary',NULL,'aggregate',NULL,999,1.5,NULL,'{}')")
    conn.commit()
    conn.close()
    out = mcp_server.t_stats(vault)
    assert "headroom" not in out
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    out2 = mcp_server.t_stats(vault)
    assert "本地多端直采" in out2 and "headroom" not in out2


# ---------------------------------------------------------------- gen_metadata 精确记账 (2026-11-21 逆向)

def varint_b(v):
    out = b""
    while True:
        b7 = v & 0x7F
        v >>= 7
        out += bytes([b7 | (0x80 if v else 0)])
        if not v:
            return out


def gen_meta_row(tin, tout, cr, model_name, model_id, step_idx):
    """按真机逆向布局构造 gen_metadata.data:
    \x12<len> usage 子消息 (f2=tin 零值省略 / f3=tout 锚 / f5=cr 零值省略)
    + f7 'bot-<uuid>' (tag+len 恰为 ':​(bot-' 字节序列, 值 40 字节)
    + f21 可读模型名 + f19 模型 id + f20 KV (last_step_index)。"""
    usage = b""
    if tin:
        usage += b"\x10" + varint_b(tin)
    usage += b"\x18" + varint_b(tout)
    if cr:
        usage += b"\x28" + varint_b(cr)
    sub = b"\x08" + varint_b(1526) + usage + b"\x30" + varint_b(24)
    bot_value = b"bot-" + b"baa83c47-57a9-4331-9c47-1022ba833c47"  # 恰 40 字节
    kv = (b"\x0a" + varint_b(len(b"last_step_index")) + b"last_step_index"
          + b"\x12" + varint_b(len(str(step_idx))) + str(step_idx).encode())
    data = (b"\x12" + varint_b(len(sub)) + sub
            + b"\x3a" + varint_b(len(bot_value)) + bot_value
            + b"\xaa\x01" + varint_b(len(model_name)) + model_name.encode()
            + b"\x9a\x01" + varint_b(len(model_id)) + model_id.encode()
            + b"\xa2\x01" + varint_b(len(kv)) + kv)
    return data


def test_antigravity_gen_metadata_exact(env):
    """gen_metadata 精确主路径: 拆分具体模型名 + 精确 tin/tout/cache_read
    (is_estimated=0), 时间戳经 last_step_index 关联 steps; 无 gen_metadata 行的
    老库仍走字节估算兜底 (is_estimated=1), 两条路径不重叠计费。"""
    home, _ = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    f = d / "convGM.db"
    db = sqlite3.connect(f)
    db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER,"
               " has_subtrajectory NUMERIC, metadata BLOB, error_details BLOB,"
               " step_payload BLOB, step_format INTEGER)")
    t_old = NOW - 9 * 3600
    t_now = NOW - 600

    def meta_pb(sec):
        def vint(v):
            v = int(v)
            out = b""
            while True:
                b7 = v & 0x7F
                v >>= 7
                out += bytes([b7 | (0x80 if v else 0)])
                if not v:
                    return out
        ts = b"\x08" + vint(sec) + b"\x10" + vint(500_000_000)
        return b"\x0a" + bytes([len(ts)]) + ts

    db.execute("INSERT INTO steps VALUES(1,14,2,0,?,?,?,0)", (meta_pb(t_old), None, b"a" * 400))
    db.execute("INSERT INTO steps VALUES(2,15,2,0,?,?,?,0)", (meta_pb(t_now), None, b"b" * 800))
    db.execute("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    rows = [
        (0, gen_meta_row(14013, 171, 0, "Gemini 4 Neon (Medium)", "gemini-4-neon", 1)),
        (1, gen_meta_row(2445, 141, 11959, "Gemini 4 Argon (Medium)", "gemini-4-argon-trial", 2)),
    ]
    for gidx, gdata in rows:
        db.execute("INSERT INTO gen_metadata VALUES(?,?,?)", (gidx, gdata, len(gdata)))
    db.commit()
    db.close()
    events, marks = cl.collect_antigravity()
    gm = [e for e in events if e["is_estimated"] == 0]  # 精确事件 (event_key 是 sha, 无字面标记)
    assert len(gm) == 2  # 精确路径生效, 字节估算整体让位 (无 agv|convGM|1 估算事件)
    assert all(e["is_estimated"] == 0 for e in gm)
    bym = {e["model"]: e for e in gm}
    neon = bym["Gemini 4 Neon (Medium)"]
    assert neon["tokens_in"] == 14013 and neon["tokens_out"] == 171
    assert neon["cache_read"] == 0  # proto3 零值省略 → f5 缺省 = 0
    assert neon["event_key"] == cl._sha("agv|convGM|gm|0")
    assert neon["occurred_at"] == t_old  # last_step_index=1 → step1 时间戳
    argon = bym["Gemini 4 Argon (Medium)"]
    assert argon["tokens_in"] == 2445 and argon["cache_read"] == 11959
    assert argon["occurred_at"] == t_now
    assert not any(e["event_key"] == cl._sha("agv|convGM|1") for e in events)
    # 价目: 可读模型名 (lower 匹配) 命中 Gemini 4 档
    vault = home.parent / "vault"
    rule = cl.price_for(cl.load_pricing(vault), "Gemini 4 Neon (Medium)")[0]
    assert rule["input"] == 1.25
    assert cl.price_for(cl.load_pricing(vault), "gemini-4-argon-trial")[0]["input"] == 2.0
    assert cl.price_for(cl.load_pricing(vault), "Gemini 4 Hydrogen (Medium)")[0] is not None


def test_agv_gen_metadata_migration(env, monkeypatch, capsys):
    """一次性迁移 agv_gen_metadata_v1: 清旧估算事件 + 水位 → 重采为精确事件;
    旗标幂等 (二跑不再迁移), sync 全程无重复计费。"""
    home, vault = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    f = d / "convM.db"
    db = sqlite3.connect(f)
    db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_payload BLOB, metadata BLOB)")
    db.execute("INSERT INTO steps VALUES(1,?,?)", (b"x" * 400, None))
    db.execute("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    db.execute("INSERT INTO gen_metadata VALUES(?,?,?)",
               (0, gen_meta_row(900, 60, 8000, "Gemini 4 Neon (High)", "gemini-4-neon", 1),
                100))
    db.commit()
    db.close()
    # 预置旧估算事件 + 旧水位 (升级前残留)
    conn = cv.db(vault)
    cl.ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO usage_events(event_key,source,client,model,project,"
                 "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,"
                 "cache_write,cost_est_usd,is_estimated,price_known,raw) VALUES("
                 "'old-est-1','antigravity','antigravity','antigravity (估算)',NULL,'convM',?,100,"
                 "0,0,0,0,NULL,1,0,'{}')", (NOW - 3600,))
    cl._wm_update(conn, "antigravity", f, (1.0, 2, 0.0, 0))
    conn.commit()
    conn.close()
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    out = capsys.readouterr().out
    assert "gen_metadata 官方精确 token" in out
    conn = cv.db(vault)
    rows = conn.execute("SELECT event_key, model, is_estimated, tokens_in, cache_read"
                        " FROM usage_events WHERE source='antigravity'").fetchall()
    conn.close()
    assert len(rows) == 1  # 旧估算行已被迁移清除, 只剩精确行
    r = rows[0]
    assert r["model"] == "Gemini 4 Neon (High)" and r["is_estimated"] == 0
    assert r["tokens_in"] == 900 and r["cache_read"] == 8000
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    capsys.readouterr()
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) FROM usage_events WHERE source='antigravity'").fetchone()[0]
    conn.close()
    assert n == 1  # 二跑幂等 (旗标已置, 水位不再变更)
