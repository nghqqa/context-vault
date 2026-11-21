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
    """隔离: cv 全局路径指向 tmp + 干净 vault.db (SCHEMA 全套)。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
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
    assert len(events) == 4  # conv1 逐步 + conv2/conv3 会话级
    per_step = [e for e in events if e["session_ref"] == "conv1"]
    assert len(per_step) == 2 and all(e["is_estimated"] == 1 for e in per_step)
    assert per_step[0]["tokens_in"] == 100  # 400 bytes / 4
    assert per_step[0]["event_key"] == cl._sha("agv|conv1|1")
    conv2_ev = [e for e in events if e["session_ref"] == "conv2"][0]
    assert conv2_ev["event_key"] == cl._sha("agv|conv2|conv")
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


def test_pricing_and_auto_recompute(env):
    home, vault = env
    zcode_sample(home)
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    row = conn.execute("SELECT cost_est_usd, price_known FROM usage_events"
                       " WHERE model='GLM-5.3-Flash'").fetchone()
    assert row["cost_est_usd"] is None and row["price_known"] == 0  # 未知单价诚实 NULL
    conn.close()
    (vault / "pricing.json").write_text(json.dumps(
        {"glm-5.3-flash": {"input": 1.0, "output": 3.0, "cache_read": 0.1}}), encoding="utf-8")
    cl.cmd_ledger_sync(SimpleNamespace(vault=str(vault)))  # 指纹变更 → 自动重算 (修正3)
    conn = cv.db(vault)
    row = conn.execute("SELECT cost_est_usd, price_known FROM usage_events"
                       " WHERE model='GLM-5.3-Flash'").fetchone()
    expect = (966 * 1.0 + 1683 * 3.0 + 82240 * 0.1) / 1e6
    assert row["price_known"] == 1
    assert abs(row["cost_est_usd"] - expect) < 1e-6
    conn.close()


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
    vault = tmp_path / "vault"
    vault.mkdir()
    seed_events(vault, [ev("e1", 600, 100, cr=50)])
    p = cl.usage_payload(vault, now=NOW)
    a = p["fiveh"]["accounts"][0]
    assert a["account"] == "client:claude-code"  # 未配置 → client 伪账号
    assert a["limit"] is None and a["pct"] is None and a["eta"]["50%"] is None
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
    assert "5 小时滚动窗口" in out and "client:zcode" in out and "未配置上限" in out
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


def test_antigravity_active_conversation_upsert(env):
    """审查#2 回归: 活跃会话增长 → 同 event_key 的会话级估算值被 upsert 刷新, 行数不增。"""
    home, vault = env
    d = home / ".gemini" / "antigravity" / "conversations"
    d.mkdir(parents=True)
    f = d / "convG.db"
    agv_db(f, real_schema=True)  # step_payload 1600B → 估算 400
    args = SimpleNamespace(vault=str(vault))
    cl.cmd_ledger_sync(args)
    conn = cv.db(vault)
    row = conn.execute("SELECT event_key, tokens_in FROM usage_events"
                       " WHERE source='antigravity'").fetchone()
    assert row["tokens_in"] == 400
    key = row["event_key"]
    conn.close()
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
    assert len(rows) == 1  # 仍是单条会话级事件, 不翻倍
    assert rows[0]["event_key"] == key
    assert rows[0]["tokens_in"] == (1600 + 6000) // 4  # 1900: 估算值已刷新 (修复前停在 400)


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
