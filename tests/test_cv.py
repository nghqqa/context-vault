"""Context Vault 核心路径测试套件.

运行: python -m pytest tests/ -v
被测代码保持零第三方依赖; pytest 仅是开发依赖。
全部用例用 tmp_path 隔离, 不触碰真实 vault/会话目录。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402


# ---------------- 测试样本构造 ----------------

def w(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                    encoding="utf-8")
    return path


def mini_rollout(tmp: Path) -> Path:
    return w(tmp / "rollout-2026-10-01T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
        {"timestamp": "2026-10-01T02:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     "timestamp": "2026-10-01T02:00:00.000Z", "cwd": "D:\\proj",
                     "cli_version": "0.153.4", "source": "exec"}},
        {"timestamp": "2026-10-01T02:00:01.000Z", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "id": "msg_1", "role": "user",
                     "content": [{"type": "input_text", "text": "hello vault"}]}},
        {"timestamp": "2026-10-01T02:00:01.000Z", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "id": "msg_2", "role": "developer",
                     "content": [{"type": "input_text", "text": "<env>injected</env>"}]}},
        {"timestamp": "2026-10-01T02:00:02.000Z", "ordinal": 3, "type": "response_item",
         "payload": {"type": "reasoning", "id": "rs_1",
                     "summary": [{"type": "summary_text", "text": "thinking hard"}],
                     "encrypted_content": "gAAAADROP"}},
        {"timestamp": "2026-10-01T02:00:03.000Z", "ordinal": 4, "type": "response_item",
         "payload": {"type": "function_call", "id": "fc_1", "name": "exec",
                     "arguments": "{\"cmd\":\"ls\"}", "call_id": "call_1"}},
        {"timestamp": "2026-10-01T02:00:04.000Z", "ordinal": 5, "type": "response_item",
         "payload": {"type": "function_call_output", "id": "fco_1", "call_id": "call_1",
                     "output": "file_a file_b"}},
        {"timestamp": "2026-10-01T02:00:05.000Z", "ordinal": 6, "type": "response_item",
         "payload": {"type": "message", "id": "msg_3", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-10-01T02:00:06.000Z", "ordinal": 7, "type": "token_usage_record",
         "payload": {"turn_id": "t1",
                     "turn_token_usage": {"input_tokens": "100", "output_tokens": "20",
                                          "cached_input_tokens": "60",
                                          "cache_write_input_tokens": "10",
                                          "reasoning_output_tokens": "5", "total_tokens": "120"}}},
    ])


def mini_transcript(tmp: Path) -> Path:
    return w(tmp / "ses_TEST0001.jsonl", [
        {"type": "user", "timestamp": "2026-06-01T00:00:00.000Z", "content": "q1"},
        {"type": "tool_use", "timestamp": "2026-06-01T00:00:01.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}},
        {"type": "tool_result", "timestamp": "2026-06-01T00:00:02.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}, "tool_output": {"output": "hit"}},
        {"type": "tool_result", "timestamp": "2026-06-01T00:00:03.000Z",
         "tool_name": "grep", "tool_input": {"pat": "x"}, "tool_output": {"output": "orphan"}},
    ])


def mini_projects(tmp: Path) -> Path:
    return w(tmp / "11111111-2222-3333-4444-555555555555.jsonl", [
        {"parentUuid": None, "type": "user", "uuid": "u1", "timestamp": "2026-10-01T00:00:00.000Z",
         "sessionId": "11111111-2222-3333-4444-555555555555", "cwd": "D:\\proj",
         "message": {"role": "user", "content": "read the file"}},
        {"parentUuid": "u1", "type": "assistant", "uuid": "a1",
         "timestamp": "2026-10-01T00:00:02.000Z", "cwd": "D:\\proj",
         "message": {"role": "assistant", "model": "glm-5.2",
                     "content": [{"type": "thinking", "thinking": "plan",
                                  "signature": "sig"},
                                 {"type": "tool_use", "id": "call_9", "name": "Read",
                                  "input": {"path": "x.py"}}],
                     "usage": {"input_tokens": 50, "output_tokens": 10,
                               "cache_read_input_tokens": 30,
                               "cache_creation_input_tokens": 5}}},
        {"parentUuid": "a1", "type": "user", "uuid": "u2",
         "timestamp": "2026-10-01T00:00:03.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call_9", "content": "file body"}]}},
    ])


# ---------------- 内容寻址与格式 ----------------

def test_snapshot_id_idempotent_and_sensitive():
    env = cv.build_envelope({"capture": {"agent": "x"}, "conversation": [{"type": "text", "role": "user", "content": "a"}], "stats": cv.zero_stats()})
    s1 = cv.snapshot_id(env)
    assert cv.snapshot_id(json.loads(json.dumps(env))) == s1          # 幂等
    env2 = json.loads(json.dumps(env)); env2["created_at"] = "1999-01-01"
    assert cv.snapshot_id(env2) == s1                                  # created_at 不参与
    env3 = json.loads(json.dumps(env)); env3["lineage"]["fork_of"] = "cv_x"
    assert cv.snapshot_id(env3) == s1                                  # lineage 不参与
    env4 = json.loads(json.dumps(env)); env4["conversation"][0]["content"] = "b"
    assert cv.snapshot_id(env4) != s1                                  # 内容敏感


def test_detect_format_three_way(tmp_path):
    assert cv.detect_format(cv.first_jsonl(mini_rollout(tmp_path)), tmp_path) == "codex-rollout"
    assert cv.detect_format(cv.first_jsonl(mini_transcript(tmp_path / "t")), tmp_path / "t") == "claude-transcript"
    assert cv.detect_format(cv.first_jsonl(mini_projects(tmp_path / "p")), tmp_path / "p") == "claude-projects"


# ---------------- 三源导出 ----------------

def test_export_codex_roundtrip(tmp_path):
    r = cv.export_codex(mini_rollout(tmp_path), drop_developer=False)
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "text", "thinking", "tool_call", "tool_result", "text"]
    assert r["capture"]["cwd"] == "D:\\proj"
    assert r["stats"]["tokens_in"] == 100 and r["stats"]["cache_read"] == 60
    assert r["stats"]["cache_write"] == 10 and r["stats"]["usage_events"] == 1
    # drop_developer 丢弃注入
    r2 = cv.export_codex(mini_rollout(tmp_path), drop_developer=True)
    assert not any(b.get("role") == "developer" for b in r2["conversation"])


def test_export_claude_transcript_fifo_pairing(tmp_path):
    r = cv.export_claude(mini_transcript(tmp_path / "t"), False)
    calls = [b for b in r["conversation"] if b["type"] == "tool_call"]
    results = [b for b in r["conversation"] if b["type"] == "tool_result"]
    assert len(results) == 1                                   # 孤儿 result 被丢弃
    assert results[0]["call_id"] == calls[0]["call_id"]        # FIFO 配对
    assert r["capture"]["completeness"] == "no_assistant_text"


def test_export_claude_code_projects(tmp_path):
    r = cv.export_claude_code(mini_projects(tmp_path / "p"), False)
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "thinking", "tool_call", "tool_result"]
    tc = [b for b in r["conversation"] if b["type"] == "tool_call"][0]
    tr = [b for b in r["conversation"] if b["type"] == "tool_result"][0]
    assert tc["call_id"] == "call_9" and tr["call_id"] == "call_9"
    assert r["capture"]["model"] == "glm-5.2" and r["capture"]["cwd"] == "D:\\proj"
    assert r["stats"]["tokens_in"] == 50 and r["stats"]["cache_read"] == 30
    assert r["capture"]["completeness"] == "full"


# ---------------- 迁移生成 ----------------

def make_env(tmp_path, kind="codex-rollout"):
    exp = {"codex-rollout": (cv.export_codex, mini_rollout),
           "claude-transcript": (cv.export_claude, lambda t: mini_transcript(t / "t")),
           "claude-projects": (cv.export_claude_code, lambda t: mini_projects(t / "p"))}[kind]
    exported = exp[0](exp[1](tmp_path), False)
    return cv.build_envelope(exported)


def test_blocks_to_codex_structure(tmp_path):
    env = make_env(tmp_path)
    lines, dropped, sid, fname = cv.blocks_to_codex(env, False)
    assert sid in fname and fname.startswith("rollout-")        # 文件名含 UUID
    assert all(isinstance(l["ordinal"], int) for l in lines)    # ordinal 必须 int (serde)
    assert lines[0]["type"] == "session_meta"
    kinds = {l["type"] for l in lines}
    assert "event_msg" in kinds and "turn_context" in kinds      # 生命周期 + turn 锚点
    fc = {p["call_id"] for l in lines if l["type"] == "response_item"
          and (p := l["payload"]).get("type") == "function_call"}
    fo = {p["call_id"] for l in lines if l["type"] == "response_item"
          and (p := l["payload"]).get("type") == "function_call_output"}
    assert fc == fo and "call_1" in fc                           # 完美配对
    assert dropped == 1                                          # developer 注入被迁移过滤 (§3.1 裁决)


def test_blocks_to_codex_developer_filtered_and_compaction(tmp_path):
    env = make_env(tmp_path)
    env["conversation"].insert(0, {"type": "compaction", "summary": "earlier stuff",
                                   "replaced_blocks": [0, 0], "ts": "2026-10-01T00:00:00.000Z"})
    env["conversation"].append({"type": "text", "role": "developer", "content": "dev", "ts": ""})
    lines, dropped, sid, fname = cv.blocks_to_codex(env, False)
    roles = [p.get("role") for l in lines if l["type"] == "response_item"
             and (p := l["payload"]).get("type") == "message"]
    assert "developer" not in roles                              # 迁移过滤 developer
    assert dropped == 2                                          # 样本1个 + 追加1个 developer 均被过滤
    user_texts = [p["content"][0]["text"] for l in lines if l["type"] == "response_item"
                  and (p := l["payload"]).get("type") == "message" and p.get("role") == "user"]
    assert "迁移摘要" in user_texts[0]                             # stateless replay: 摘要为首条 user
    assert any("hello vault" in t for t in user_texts[1:])        # 活上下文紧随其后


def test_blocks_to_claude_chain(tmp_path):
    env = make_env(tmp_path, "claude-projects")
    lines, dropped = cv.blocks_to_claude(env, False)
    assert lines[0]["parentUuid"] is None
    assert all(lines[i]["parentUuid"] == lines[i - 1]["uuid"] for i in range(1, len(lines)))
    assert all(str(lines[i]["timestamp"]).endswith("Z") for i in range(len(lines)))
    tu = [c for l in lines if l["type"] == "assistant"
          for c in l["message"]["content"] if c["type"] == "tool_use"]
    tr = [c for l in lines if l["type"] == "user"
          and isinstance(l["message"]["content"], list)
          for c in l["message"]["content"] if c["type"] == "tool_result"]
    assert {t["id"] for t in tu} == {t["tool_use_id"] for t in tr}


# ---------------- sync 增量与 lineage ----------------

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把 cv 的全局路径指向 tmp, 隔离真实会话目录。"""
    home = tmp_path / "home"
    (home / ".codex" / "sessions" / "2026" / "10" / "01").mkdir(parents=True)
    (home / ".claude" / "transcripts").mkdir(parents=True)
    (home / ".claude" / "projects" / "D--proj").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")  # 隔离真实 zcode 库
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", home / ".zcode" / "absent_tasks.sqlite")
    # antigravity 回灌指向 tmp (模块常量在 import 时已绑定真 HOME, 不 patch 会吸入真实会话库)
    monkeypatch.setattr(cv, "ANTIGRAVITY_CONV_DIR",
                        home / ".gemini" / "antigravity" / "conversations")
    import cv as _cv
    # scan_sources 用模块常量, 已随 monkeypatch 生效
    vault = tmp_path / "vault"
    vault.mkdir()
    return home, vault


def test_sync_incremental_lineage(isolated, monkeypatch):
    home, vault = isolated
    src = mini_rollout(home / ".codex" / "sessions" / "2026" / "10" / "01")
    import time as _t
    import os as _os
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)

    from types import SimpleNamespace
    A = lambda: SimpleNamespace(vault=str(vault), root=None)
    cv.cmd_sync(A())
    n1 = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n1 == 1
    first_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]

    cv.cmd_sync(A())                                             # 二次全跳过
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1

    w(src, [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
      + [{"timestamp": "2026-10-01T03:00:00.000Z", "ordinal": 8, "type": "response_item",
          "payload": {"type": "message", "id": "msg_9", "role": "user",
                      "content": [{"type": "input_text", "text": "follow up"}]}}])
    _os.utime(src)
    cv.cmd_sync(A())
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    new_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]
    assert new_sid != first_sid
    env = json.loads((vault / "snapshots" / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert env["lineage"]["parent_snapshot"] == first_sid        # lineage 自动成链


# ---------------- 热索引 ----------------

def test_tokenize_and_index(tmp_path):
    toks = cv.tokenize("MergePilot 优化 dataset1 时序")
    assert "mergepilot" in toks and "dataset1" in toks
    assert any(len(t) == 2 and all("\u4e00" <= c <= "\u9fff" for c in t) for t in toks)  # CJK bigram

    def bt(b):
        return b.get("content", "") or ""
    idx = cv.VaultIndex(tmp_path, bt)
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    env = make_env(tmp_path)
    (tmp_path / "snapshots" / "cv_test1.json").write_text(
        json.dumps(env, ensure_ascii=False), encoding="utf-8")
    idx.rebuild(force=True)
    r = idx.search("hello vault", 5)
    assert r["hits"], "索引可检索"


# ---------------- 账本 ----------------

def test_ledger_headroom_import(isolated, monkeypatch):
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir()
    w(head / "savings_events.jsonl", [
        {"v": 1, "ts": "2026-08-19T10:30:27Z", "before": 100, "after": 40, "saved": 60,
         "cost_usd": 0.03, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"}])
    (head / "proxy_savings.json").write_text(json.dumps({
        "schema_version": "6", "generated_at": "2026-10-28T00:00:00Z",
        "lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5,
                     "cache_read_tokens": 9000, "cache_savings_usd": 0.7,
                     "total_input_tokens": 100000}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)

    from types import SimpleNamespace
    cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    assert n == 2                                                 # 1 明细 + 1 汇总
    row = conn.execute("SELECT tokens_saved, cost_usd FROM ledger_events "
                       "WHERE kind='compression'").fetchone()
    assert row["tokens_saved"] == 60 and abs(row["cost_usd"] - 0.03) < 1e-9


# ---------------- 自有账本 (协作者零依赖体验) ----------------

def test_owned_stats_zero_external(isolated):
    _, vault = isolated
    # 无 headroom 时自有账本仍完整: 快照 stats 聚合
    exp = cv.export_codex(mini_rollout(Path(str(vault)) and (isolated[0] / ".codex" / "sessions" / "2026" / "10" / "01")), False)
    env = cv.build_envelope(exp)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    import uuid as _u
    sid = "cv_t" + _u.uuid4().hex[:12]
    out = vault / "snapshots" / f"{sid}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(env), encoding="utf-8")
    cv.register_snapshot(conn, env, sid, out)
    conn.commit()
    own = cv.owned_stats(vault)
    assert own["snapshots"] == 1
    assert own["sessions"] == 1
    assert own["tokens_in"] == 100 and own["cache_read"] == 60
    assert own["cache_hit_rate"] == 0.6
    assert own["by_agent"][0]["agent"] == "codex"
    assert own["by_agent"][0]["sessions"] == 1


def _register_env(vault: Path, capture: dict, stats: dict, created_at: str) -> str:
    """注册一个手工 stats 的快照 (账本口径测试用), 返回 sid。"""
    import uuid as _u
    env = cv.build_envelope({"capture": capture, "conversation":
                             [{"type": "text", "role": "user", "content": "x"}],
                             "stats": {**cv.zero_stats(), **stats}})
    env["created_at"] = created_at
    sid = "cv_t" + _u.uuid4().hex[:12]
    out = vault / "snapshots" / f"{sid}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(env), encoding="utf-8")
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    cv.register_snapshot(conn, env, sid, out)
    conn.commit()
    return sid


def test_owned_stats_cache_hit_normalized(isolated):
    """回归 (2026-11-02 审查 #2): Claude 的 tokens_in 只是未缓存输入, 缓存读/写单独计数,
    旧公式 cache_read/tokens_in 在 10+90 时得出 900%。按来源语义归一分母后:
    claude-projects 分母 = 未缓存+读+写; codex 的 input 已含缓存部分, 分母 = input。"""
    _, vault = isolated
    _register_env(vault, {"agent": "claude-code", "source_format": "claude-projects"},
                  {"tokens_in": 10, "cache_read": 90, "cache_write": 0,
                   "total_tokens": 110, "usage_events": 1}, "2026-11-01T00:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == 0.9                      # 90/100, 不再是 9.0
    # 混源: 加一个 codex 叶子 (input=100 已含 cached 60, 写 10) → 加权 150/(100 + 100) = 0.75
    _register_env(vault, {"agent": "codex", "source_format": "codex-rollout"},
                  {"tokens_in": 100, "cache_read": 60, "cache_write": 10,
                   "total_tokens": 120, "usage_events": 1}, "2026-11-01T01:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == round(150 / 200, 3)
    assert own["cache_hit_rate"] <= 1.0


def test_owned_stats_dedup_cumulative(isolated, monkeypatch):
    """回归 (2026-11-02 审查 #1): sync 对同一会话保存多个累计快照, owned_stats 曾对
    全版本求和 → 同一批 usage 逐版本重复累计 (100 变 200)。现在只累计 lineage 叶子。"""
    home, vault = isolated
    src = mini_rollout(home / ".codex" / "sessions" / "2026" / "10" / "01")
    import os as _os
    from types import SimpleNamespace
    A = lambda: SimpleNamespace(vault=str(vault), root=None)
    cv.cmd_sync(A())                                    # v1: input=100, cache_read=60
    w(src, [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
      + [{"timestamp": "2026-10-01T03:00:00.000Z", "ordinal": 8, "type": "response_item",
          "payload": {"type": "message", "id": "msg_9", "role": "user",
                      "content": [{"type": "input_text", "text": "follow up"}]}}])
    _os.utime(src)
    cv.cmd_sync(A())                                    # v2 累计含 v1 全部 usage, parent=v1
    conn = cv.db(vault)
    leaf_sid = conn.execute("SELECT snapshot_id FROM sync_sources").fetchone()[0]
    own = cv.owned_stats(vault)
    assert own["snapshots"] == 2                        # 版本总数: 诚实包含被替代的历史版本
    assert own["sessions"] == 1                         # 独立会话 = 1 条演进链
    assert own["tokens_in"] == 100 and own["cache_read"] == 60   # 只数叶子, 不再 200/120
    assert own["by_agent"][0]["snapshots"] == 1
    # fork 是用户主动创建的独立资产副本: 计自己的叶子 (会话 +1, 用量按副本重复)
    cv.cmd_fork(SimpleNamespace(vault=str(vault), snapshot_id=leaf_sid, message=None))
    own2 = cv.owned_stats(vault)
    assert own2["snapshots"] == 3 and own2["sessions"] == 2 and own2["tokens_in"] == 200


# ---------------- M3 记忆层 ----------------

def test_memory_extract_and_search(isolated, monkeypatch, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path, "codex-rollout")
    env["conversation"].append({"type": "text", "role": "assistant",
                                "content": "结论：我们决定采用单实例自托管方案", "ts": ""})
    env["conversation"].insert(0, {"type": "compaction", "summary": "早前讨论了缓存策略并确定 TTL=300",
                                   "replaced_blocks": [0, 0], "ts": ""})
    env["conversation"].append({"type": "text", "role": "user",
                                "content": "记住：以后所有提交都必须带签名", "ts": ""})
    sid = "cv_memtest1"
    (vault / "snapshots").mkdir(exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    import cv as _cv
    items = _cv.extract_memories({**env, "_sid": sid, "capture": {"cwd": r"D:\proj"}})
    kinds = {i["kind"] for i in items}
    assert kinds == {"summary", "decision", "preference"}
    dec = [i for i in items if i["kind"] == "decision"]
    assert any("单实例自托管" in i["content"] for i in dec)
    assert all(i["project"] == "proj" for i in items)

    conn = _cv.db(vault)
    conn.executescript(_cv.SCHEMA + _cv.MEMORY_TABLE)
    for it in items:
        conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)"
                     " VALUES(?,?,?,?,?)", ("t", sid, it["kind"], it["content"], it["project"]))
    conn.commit()
    hits = _cv.memory_search_api(vault, "自托管 方案", 5)
    assert hits and any("单实例" in h["content"] for h in hits)
    assert all(h["snapshot"] == sid for h in hits)


def test_memory_noise_gates_and_dedup(isolated, tmp_path):
    """rule 引擎 v0.1 提精度闸门: 围栏内/列表项/问句/标记词出句首窗口的行不入库;
    memory_dedup 合并 (kind, content) 跨快照重复; counts 带 unique。"""
    _, vault = isolated
    sid = "cv_noisegt1"
    env = make_env(tmp_path, "codex-rollout")
    env["conversation"] += [
        {"type": "text", "role": "assistant",
         "content": "```python\n# 采用递归下降解析\nresult = parse(src)\n```", "ts": ""},
        {"type": "text", "role": "assistant", "content": "- 采用缓存方案减少重复计算的开销", "ts": ""},
        {"type": "text", "role": "assistant", "content": "你觉得这里应该采用哪种方案更好？", "ts": ""},
        {"type": "text", "role": "assistant",
         "content": "在这个问题上我们经过了相当多轮的对比与讨论，最终才在很靠后的位置提到采用增量水位方案", "ts": ""},
        {"type": "text", "role": "assistant", "content": "结论：采用增量水位做记忆提取", "ts": ""},
    ]
    (vault / "snapshots").mkdir(exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    import cv as _cv
    items = _cv.extract_memories({**env, "_sid": sid, "capture": {"cwd": "D:\\proj"}})
    contents = "\n".join(i["content"] for i in items)
    assert "递归下降" not in contents     # ``` 围栏内是代码
    assert "缓存方案减少" not in contents  # 列表项
    assert "哪种方案更好" not in contents  # 问句
    assert "靠后的位置" not in contents   # 标记词在句首 32 字窗口外
    assert any("增量水位做记忆提取" in i["content"] for i in items)  # 句首窗口内正常提取

    conn = _cv.db(vault)
    conn.executescript(_cv.SCHEMA + _cv.MEMORY_TABLE)
    for c in ("结论：采用增量水位做记忆提取", "结论：采用增量水位做记忆提取", "另一条独立记忆的完整内容样例"):
        conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)"
                     " VALUES(?,?,?,?,?)", ("t", sid, "decision", c, "proj"))
    conn.commit()
    conn.close()
    res = _cv.memory_dedup(vault)
    assert res["removed"] == 1 and res["total"] == 2
    cnts = _cv.memory_counts_api(vault)
    assert cnts["unique"] == 2 and cnts["total"] == 2


# ---------------- M3 收尾: fork / audit / 引擎接口 ----------------

def test_fork_lineage(isolated, monkeypatch, capsys, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path)
    sid = "cv_forksrc1"
    (vault / "snapshots").mkdir(exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    from types import SimpleNamespace
    cv.cmd_fork(SimpleNamespace(vault=str(vault), snapshot_id=sid, message="test fork"))
    new_sid = [f.stem for f in (vault / "snapshots").glob("cv_*.json") if f.stem != sid][0]
    forked = json.loads((vault / "snapshots" / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert forked["lineage"]["fork_of"] == sid
    assert forked["capture"]["fork_note"] == "test fork"
    # 原快照不可变: 内容未被改动
    orig = json.loads((vault / "snapshots" / f"{sid}.json").read_text(encoding="utf-8"))
    assert orig["lineage"]["fork_of"] is None


def test_audit_summary(isolated):
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    import sqlite3 as _s
    for st, miss in (("ok", None), ("ok", None), ("switch", None),
                     ("http_error", '["gpt-x"]')):
        conn.execute("INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (f"2026-10-28T0{st.__len__()}:" + "00:00+08:00", "p1", "u", st, 200, 10, "[]", miss))
    conn.commit()
    r = cv.audit_summary_and_timeline(vault, "", "all")
    assert r["summary"]["ok_probes"] == 2
    assert r["summary"]["switches"] == 1
    assert r["summary"]["model_missing_events"] == 1
    assert r["timeline"][0]["day"] == "2026-10-28"


def test_memory_engine_protocol():
    eng = cv.load_memory_engine("rule")
    assert eng.name == "rule"
    assert hasattr(eng, "extract") and hasattr(eng, "search")
    # 动态加载路径: 用本模块内伪造引擎验证 importlib 机制
    import sys as _s
    _s.path.insert(0, "tools")
    fake = "tests_fake_engine:FakeEngine"
    import types as _t
    mod = _t.ModuleType("tests_fake_engine")
    class FakeEngine:
        name = "fake"
        def extract(self, env): return [{"kind": "decision", "content": "x",
                                         "project": "p", "snapshot_id": "s"}]
        def search(self, vault, q, lim): return []
    mod.FakeEngine = FakeEngine
    _s.modules["tests_fake_engine"] = mod
    e2 = cv.load_memory_engine(fake)
    assert e2.name == "fake" and e2.extract({})[0]["content"] == "x"


# ---------------- zcode sqlite 适配器 ----------------

def test_export_zcode_session(isolated, tmp_path):
    import sqlite3 as _s
    db = _s.connect(":memory:")
    db.row_factory = _s.Row
    db.executescript("""CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_updated INTEGER,
        time_deleted INTEGER, time_created INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, sequence INTEGER);""")
    db.execute(r"INSERT INTO session VALUES('sess_t','D:\proj','测试会话',1793000000000,NULL,1793000000000)")
    db.execute("INSERT INTO message VALUES('m1','sess_t',?,1000,1)",
               (json.dumps({"role": "user"}),))
    db.execute("INSERT INTO message VALUES('m2','sess_t',?,2000,2)",
               (json.dumps({"role": "assistant", "modelId": "GLM-5.3"}),))
    db.execute("INSERT INTO part VALUES('p1','m1','sess_t',?,1)",
               (json.dumps({"type": "text", "text": "帮我看看"}),))
    db.execute("INSERT INTO part VALUES('p2','m1','sess_t',?,2)",
               (json.dumps({"type": "text", "text": "[系统提醒]", "synthetic": True}),))
    db.execute("INSERT INTO part VALUES('p3','m2','sess_t',?,1)",
               (json.dumps({"type": "reasoning", "text": "先查库",
                            "metadata": {"anthropic": {"signature": "sig"}}}),))
    db.execute("INSERT INTO part VALUES('p4','m2','sess_t',?,2)",
               (json.dumps({"type": "tool", "callID": "call_1", "tool": "Bash",
                            "state": {"status": "completed", "input": {"command": "ls"},
                                      "output": "a b c"}}),))
    r = cv.export_zcode_session(db, db.execute("SELECT * FROM session").fetchone())
    types = [b["type"] for b in r["conversation"]]
    assert types == ["text", "thinking", "tool_call", "tool_result"]  # synthetic 被过滤
    assert r["capture"]["agent"] == "zcode" and r["capture"]["model"] == "GLM-5.3"
    assert r["capture"]["cwd"] == r"D:\proj"
    tc = r["conversation"][2]; tr = r["conversation"][3]
    assert tc["call_id"] == "call_1" and tr["call_id"] == "call_1"
    assert tr["content"] == "a b c"
    assert "signature" not in json.dumps(r)  # spec §3.2: 签名不可移植即丢弃


# ---------------- serve 本地访问防护 + 账本回归 (2026-10-29 修复) ----------------

import http.client  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _req(port, method, path, body=None, headers=None):
    """直接对 127.0.0.1:port 发请求, 返回 (status, body_json); 头部完全可控。"""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    raw = r.read().decode("utf-8", errors="replace")
    c.close()
    return r.status, (json.loads(raw) if raw.startswith("{") else {})


def test_serve_local_guard_and_stats(isolated, tmp_path):
    """Host/Origin 回环白名单 + POST 强制 application/json; /stats 曾因漏导入 500。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _idx = cv.build_vault_server(vault, 0)  # 0 = 随机空闲端口
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 本地进程 (MCP 热索引 / 托盘 / 控制台同源页面) 正常访问
        code, h = _req(port, "GET", "/healthz")
        assert code == 200 and h["status"] == "ok"
        code, s = _req(port, "GET", "/stats")  # 回归: ledger_summary 漏导入曾 500
        assert code == 200 and "owned" in s and "stats" in s
        # DNS rebinding: Host 非回环 → 403
        code, _ = _req(port, "GET", "/healthz", headers={"Host": "evil.example:1"})
        assert code == 403
        # 恶意网页跨站: Origin 非回环 → 403
        code, _ = _req(port, "GET", "/healthz", headers={"Origin": "http://evil.example"})
        assert code == 403
        # 跨站简单请求 POST (text/plain 不触发预检) → 403, 不再触达 do_import
        code, _ = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": "x", "apply": True}),
                       headers={"Content-Type": "text/plain"})
        assert code == 403
        # 合法 POST: application/json + 回环 Origin → 200 (真实快照 dry-run 迁移)
        env = make_env(tmp_path)
        sid = cv.snapshot_id(env)
        (vault / "snapshots").mkdir(exist_ok=True)
        cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": sid, "target": "codex", "apply": False}),
                       headers={"Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{port}"})
        assert code == 200 and r["lines"] >= 1 and r["applied"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_ledger_relay_billing_single_entry(isolated, monkeypatch, capsys):
    """回归: billing 模式重复代码块曾致每次导入双计入 relay_usage。"""
    _, vault = isolated
    monkeypatch.delenv("CV_RELAY_KEY", raising=False)
    monkeypatch.setattr(cv, "fetch_relay_usage", lambda url, key, s, e: {"total_usage": 12.34})
    cv.cmd_ledger_import_relay(SimpleNamespace(
        vault=str(vault), mode="billing", url="https://relay.example",
        days=7, start=None, end=None, key="k", token=None, user_id=None, log_path=None))
    conn = cv.db(vault)
    rows = conn.execute("SELECT cost_usd FROM ledger_events WHERE kind='relay_usage'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 12.34


# ---------------- backup: 一致性备份与轮换 ----------------

def test_backup_content_and_rotation(isolated, tmp_path):
    import sqlite3 as _s
    import zipfile
    _, vault = isolated
    env = make_env(tmp_path)
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.execute("INSERT INTO ledger_events(source,occurred_at,kind) VALUES('t','t','x')")
    conn.commit()

    bdir = vault / "backups"
    cv.cmd_backup(SimpleNamespace(vault=str(vault), out=str(bdir), keep=2))
    zips = sorted(bdir.glob("cv-backup-*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as z:
        names = z.namelist()
        assert "vault.db" in names and f"snapshots/{sid}.json" in names
        z.extract("vault.db", bdir / "restore-test")
    rc = _s.connect(bdir / "restore-test" / "vault.db")  # 备份库可开且数据完整
    assert rc.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 1
    rc.close()
    assert not list(bdir.glob(".backup-tmp-*"))          # 无临时残留

    # 轮换: 伪造两份旧备份, keep=1 时应只剩最新一份
    for name in ("cv-backup-20200101-000000.zip", "cv-backup-20250101-000000.zip"):
        (bdir / name).write_bytes(b"old")
    cv.cmd_backup(SimpleNamespace(vault=str(vault), out=str(bdir), keep=1))
    left = [p.name for p in bdir.glob("cv-backup-*.zip")]
    assert len(left) == 1                                        # keep=1: 只剩最新一份
    assert not (bdir / "cv-backup-20200101-000000.zip").exists()  # 两份伪造旧备份被轮换掉
    assert not (bdir / "cv-backup-20250101-000000.zip").exists()


# ---------------- Markdown 交接简报 (可携降级档) ----------------

def test_brief_generation(isolated, tmp_path):
    _, vault = isolated
    env = make_env(tmp_path)
    env["conversation"].append({"type": "text", "role": "assistant",
                                "content": "已完成 RBAC 方案初稿, 待评审", "ts": ""})
    env["conversation"].append({"type": "tool_call", "call_id": "c2", "name": "edit",
                                "arguments": {"path": "src/auth.py"}, "ts": ""})
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    r = cv.do_import(vault, sid, "markdown", apply=False)
    brief = Path(r["dest"]).read_text(encoding="utf-8")
    assert len(brief) <= 48 * 1024 + 200                         # 48KB 上限
    for section in ("它停在哪儿", "最近动作", "活上下文尾部", "RBAC 方案初稿", "edit", "src/auth.py"):
        assert section in brief
    assert "precise" not in brief and r["applied"] is False      # 简报不写真实会话目录


def test_brief_stop_point_prefers_last_assistant_output(isolated, tmp_path):
    """回归 (2026-11-02 审查 #5): 原生 Codex/Claude exporter 的普通 assistant 消息是
    text 块 (role=assistant), 停点检索曾只认 agent_message → 明明有进展文本,
    简报却写"会话中无 assistant 文本输出"。"""
    _, vault = isolated
    env = cv.build_envelope({"capture": {"agent": "claude-code", "cwd": "D:\\proj",
                                         "completeness": "full"},
                             "conversation": [
                                 {"type": "text", "role": "user", "content": "继续修 bug", "ts": ""},
                                 {"type": "text", "role": "assistant",
                                  "content": "已定位空指针, 修复已提交", "ts": ""}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(exist_ok=True)
    cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
    r = cv.do_import(vault, sid, "markdown", apply=False)
    brief = Path(r["dest"]).read_text(encoding="utf-8")
    stop = brief.split("## 它停在哪儿", 1)[1].split("## 最近动作", 1)[0]
    assert "已定位空指针, 修复已提交" in stop                      # 停点 = 最后一条 assistant 文本
    assert "会话中无 assistant 文本输出" not in stop
    # agent_message 仍是合法停点 (多 agent 场景)
    env["conversation"].append({"type": "agent_message", "author": "a",
                                "recipient": "b", "content": "移交给 b 处理", "ts": ""})
    sid2 = cv.snapshot_id(env)
    cv.atomic_write_text(vault / "snapshots" / f"{sid2}.json", json.dumps(env, ensure_ascii=False))
    stop2 = Path(cv.do_import(vault, sid2, "markdown", apply=False)["dest"]).read_text("utf-8")
    stop2 = stop2.split("## 它停在哪儿", 1)[1].split("## 最近动作", 1)[0]
    assert "移交给 b 处理" in stop2


def test_do_import_catchable_errors(isolated):
    """回归 (2026-11-02 审查 #8): 领域层抛可捕获异常 (SnapshotNotFound/BadMigrateTarget),
    不再是 SystemExit (BaseException) — HTTP 层才能映射 404/400, CLI 在 main() 统一转退出。"""
    _, vault = isolated
    with pytest.raises(cv.SnapshotNotFound):
        cv.do_import(vault, "cv_not0000000000000000000000000000000", "codex", False)
    with pytest.raises(cv.SnapshotNotFound):
        cv.load_snapshot(vault, "cv_not0000000000000000000000000000000")
    with pytest.raises(cv.BadMigrateTarget):
        cv.do_import(vault, "cv_x", "nosuchtarget", False)
    try:
        cv.do_import(vault, "cv_not0000000000000000000000000000000", "codex", False)
    except Exception as e:  # SystemExit 不是 Exception 子类 — 这正是当年 HTTP 断连的根因
        assert not isinstance(e, SystemExit)


def test_serve_token_auth(isolated, tmp_path):
    """鉴权档: 无凭据 401; ?token= 首访 200+Set-Cookie; cookie 复访 200; 错 token 401。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/healthz")
        assert code == 401                                       # 无凭据
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/healthz?token=sekret")
        r = c.getresponse(); body = r.read(); setc = r.getheader("Set-Cookie"); c.close()
        assert r.status == 200 and "vault_token=sekret" in (setc or "")
        code, _ = _req(port, "GET", "/healthz", headers={"Cookie": "vault_token=sekret"})
        assert code == 200                                       # cookie 复访
        code, _ = _req(port, "GET", "/healthz", headers={"X-Vault-Token": "sekret"})
        assert code == 200                                       # 头形态 (MCP/脚本)
        code, _ = _req(port, "GET", "/healthz?token=wrong")
        assert code == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_token_root_seeds_cookie(isolated):
    """回归 (2026-11-02 审查 #3): 首访 /?token= 的 HTML 响应曾不发 Set-Cookie,
    控制台页面加载后的所有 fetch 全部 401。根路径必须与 /healthz 同样种 cookie。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/?token=sekret")
        r = c.getresponse(); body = r.read(); setc = r.getheader("Set-Cookie"); c.close()
        assert r.status == 200 and b"<!doctype html>" in body[:40].lower()
        assert "vault_token=sekret" in (setc or "")
        # 带首访种下的 cookie 复访根路径与数据接口: 不再 401
        code, _ = _req(port, "GET", "/", headers={"Cookie": "vault_token=sekret"})
        assert code == 200
        code, h = _req(port, "GET", "/healthz", headers={"Cookie": "vault_token=sekret"})
        assert code == 200 and h["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_unknown_sid_stable_json(isolated, tmp_path):
    """回归 (2026-11-02 审查 #8): 不存在的迁移 sid 曾让 load_snapshot 抛 SystemExit
    (BaseException), 逃过 HTTP 的 except Exception, 客户端收到断连而非 JSON 错误。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        post = lambda sid: _req(port, "POST", "/migrate",
                                body=json.dumps({"id": sid, "target": "codex", "apply": False}),
                                headers={"Content-Type": "application/json"})
        # 形态非法 (含路径穿越) → 400 稳定 JSON
        code, r = post("../../etc/passwd")
        assert code == 400 and "invalid id" in r["error"]
        # 形态合法但不存在 → 404 稳定 JSON, 不再断连
        code, r = post("cv_0000000000000000")
        assert code == 404 and "not found" in r["error"]
        # GET /snapshot 与 /preview 同样受 id 形态校验保护
        code, _ = _req(port, "GET", "/snapshot?id=../vault.db%00")
        assert code == 400
        code, _ = _req(port, "GET", "/preview?id=cv_zzz")
        assert code == 400
        code, _ = _req(port, "GET", "/snapshot?id=cv_0000000000000000")
        assert code == 404
        # 未知迁移目标 → 400 (BadMigrateTarget), 不落通用 500
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": "cv_0000000000000000", "target": "nosuch",
                                        "apply": False}),
                       headers={"Content-Type": "application/json"})
        assert code == 404  # 先撞存在性 404; 换真实快照验证 target 分支
        env = make_env(tmp_path)
        sid = cv.snapshot_id(env)
        (vault / "snapshots").mkdir(exist_ok=True)
        cv.atomic_write_text(vault / "snapshots" / f"{sid}.json", json.dumps(env, ensure_ascii=False))
        code, r = _req(port, "POST", "/migrate",
                       body=json.dumps({"id": sid, "target": "nosuch", "apply": False}),
                       headers={"Content-Type": "application/json"})
        assert code == 400 and "未知迁移目标" in r["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_log_redacts_token(isolated):
    """回归 (2026-11-02 审查 #3 附带): ?token= 随 requestline 落 serve_http.log,
    鉴权档下必须脱敏为 token=***, 明文不得进日志。"""
    import re as _re
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="sekret-value")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/healthz?token=sekret-value")
        assert code == 200
    finally:
        srv.shutdown()
        srv.server_close()
    log = (vault / "serve_http.log").read_text(encoding="utf-8")
    assert "token=***" in log
    assert "sekret-value" not in log


def _mk_snap(vault: Path, sid: str, first_user: str, blocks: int = 3):
    """写一个 VaultIndex 可解析的最小快照文件, 返回其 mtime 前的文件对象。"""
    p = vault / "snapshots" / f"{sid}.json"
    p.parent.mkdir(exist_ok=True)
    env = {"capture": {"agent": "zcode", "completeness": "full", "cwd": r"D:\proj"},
           "created_at": "2026-11-01T00:00:00",
           "stats": {"blocks_total": blocks, "tokens_in": 100 * blocks},
           "conversation": ([{"type": "user", "role": "user", "content": first_user}] +
                            [{"type": "assistant", "role": "assistant", "content": f"回复{i}"}
                             for i in range(blocks - 1)])}
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    return p


def test_index_incremental_cache_and_removal(isolated):
    """索引三态: 追加走增量 (不全量), 重启加载缓存后增量追赶一致, 删除触发全量。"""
    _, vault = isolated
    bt = lambda b: b.get("content") or ""
    _mk_snap(vault, "cv_aaaa", "第一个会话 关键词甲")
    _mk_snap(vault, "cv_bbbb", "第二个会话 关键词乙")
    idx = cv.VaultIndex(vault, bt)
    assert len(idx.meta) == 2 and idx.stats == {"full": 1, "incr": 0, "cache_load": 0}

    # 增量: 纯新增 -> incr 计数, 不动全量
    _mk_snap(vault, "cv_cccc", "第三个会话 关键词甲")
    idx.rebuild()
    assert len(idx.meta) == 3 and idx.stats["incr"] == 1 and idx.stats["full"] == 1
    assert "cv_cccc" in idx.meta
    assert idx.search("关键词甲", 5)["hits"][0]["sid"] in ("cv_aaaa", "cv_cccc")

    # 缓存: 新实例直接加载, 追加的 cv_dddd 走增量追赶, 搜索/上下文跨加载一致
    _mk_snap(vault, "cv_dddd", "第四个会话 关键词乙")
    idx2 = cv.VaultIndex(vault, bt)
    assert idx2.stats["cache_load"] == 1 and idx2.stats["full"] == 0
    assert len(idx2.meta) == 4
    assert idx2.search("关键词乙", 5)["hits"][0]["sid"] in ("cv_bbbb", "cv_dddd")
    assert idx2.context("关键词甲", 3)["results"]
    assert idx2.load_env("cv_aaaa") is not None

    # 删除: 差分发现移除 -> 全量重建, 计数与搜索立刻一致
    (vault / "snapshots" / "cv_aaaa.json").unlink()
    idx2.rebuild()
    assert len(idx2.meta) == 3 and "cv_aaaa" not in idx2.meta
    assert idx2.stats["full"] == 1 and idx2.stats["cache_load"] == 1  # 全量恰好 +1
    assert (vault / "search_index.db").exists()  # 落盘缓存已生成 (v2: SQLite 倒排取代 pickle)


def test_index_regression_guards(isolated):
    """复测三高危的回归门: LRU 溢出、损坏缓存回退、缺键快照跳过。"""
    import collections
    _, vault = isolated
    bt = lambda b: b.get("content") or ""
    _mk_snap(vault, "cv_good1", "正常快照 回归门甲")
    # H3: 缺 capture/stats 的合法 JSON 必须被跳过, 不炸重建/启动
    bad = vault / "snapshots" / "cv_bad1.json"
    bad.write_text(json.dumps({"created_at": "2026-11-01T00:00:00"}), encoding="utf-8")
    idx = cv.VaultIndex(vault, bt)
    assert "cv_good1" in idx.meta and "cv_bad1" not in idx.meta
    assert idx.search("回归门", 5)["hits"], "好快照应可被检索"
    # H1: LRU 溢出走 OrderedDict 逐出, 不抛 TypeError; 缓存上限被尊重
    for i in range(70):
        idx._env_cache[f"cv_pad{i}"] = {"_sid": f"cv_pad{i}"}
    assert idx.load_env("cv_good1") is not None
    assert isinstance(idx._env_cache, collections.OrderedDict)
    assert len(idx._env_cache) <= cv.VaultIndex.ENV_CACHE_MAX
    # H2: 损坏的 search_index.db 必须静默回退全量, 不让 DatabaseError 逃出启动
    (vault / "search_index.db").write_bytes(b"definitely not a sqlite db \x00garbage")
    idx2 = cv.VaultIndex(vault, bt)
    assert idx2.stats["cache_load"] == 0 and idx2.stats["full"] == 1
    assert len(idx2.meta) == 1  # bad1 被跳过, good1 在


# ---------------- 全面测试回归 (dwfrun-bdea7af6 发现) ----------------

def _mk_chain_rollout(tmp: Path, sid: str) -> Path:
    """带 token_usage_record 的最小 codex rollout (账本/同步回归用)。"""
    return w(tmp / f"rollout-2026-11-01T00-00-00-{sid}.jsonl", [
        {"timestamp": "2026-11-01T00:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": sid, "session_id": sid, "timestamp": "2026-11-01T00:00:00.000Z",
                     "cwd": "D:\\proj", "cli_version": "0.153.4", "source": "exec"}},
        {"timestamp": "2026-11-01T00:00:01.000Z", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "id": "m1", "role": "user",
                     "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-11-01T00:00:02.000Z", "ordinal": 2, "type": "token_usage_record",
         "payload": {"turn_id": "t1", "turn_token_usage": {"input_tokens": "100", "output_tokens": "20",
                                                           "cached_input_tokens": "60",
                                                           "cache_write_input_tokens": "10",
                                                           "reasoning_output_tokens": "5",
                                                           "total_tokens": "120"}}},
    ])


def test_export_idempotent_preserves_chain(isolated):
    """H1 回归: sync 已成链后 cv export 幂等跳过, 不得用 parent=NULL 的 envelope 覆盖链
    (旧代码让同一会话 usage 双计, 重新引入 09f90a6 修掉的缺陷)。"""
    from types import SimpleNamespace
    import os as _os
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "11111111-1111-1111-1111-111111111111")
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "follow up"}]}})
    w(src, rows)
    sync()                                             # 建链 v1→v2
    own0 = cv.owned_stats(vault)
    assert own0["sessions"] == 1 and own0["tokens_in"] == 100
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    own1 = cv.owned_stats(vault)
    assert own1["sessions"] == 1 and own1["tokens_in"] == 100   # 不再 2/200
    conn = cv.db(vault)
    linked = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE parent_snapshot IS NOT NULL").fetchone()["n"]
    assert linked == 1                                 # 链完好


def test_sync_content_unchanged_no_self_parent(isolated):
    """M2 回归: 仅 mtime 变化 (内容未变) 的二次 sync 不得产生 parent==id 自环
    (自环会被叶子过滤把会话从账本清零)。"""
    from types import SimpleNamespace
    import os as _os
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "22222222-2222-2222-2222-222222222222")
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    _os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 5))
    sync()
    conn = cv.db(vault)
    rows = conn.execute("SELECT id, parent_snapshot FROM snapshots").fetchall()
    assert all(r["parent_snapshot"] != r["id"] for r in rows)     # 无自环
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100       # 会话未从账本消失


def test_headroom_detail_import_idempotent(isolated, monkeypatch):
    """H2 回归: 重跑 import-headroom, compression 明细与 lifetime 一样幂等, 不再翻倍。"""
    from types import SimpleNamespace
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir(parents=True, exist_ok=True)
    w(head / "savings_events.jsonl", [
        {"v": 1, "ts": "2026-11-01T10:30:27Z", "before": 100, "after": 40, "saved": 60,
         "cost_usd": 0.03, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"},
        {"v": 1, "ts": "2026-11-01T11:00:00Z", "before": 50, "after": 10, "saved": 40,
         "cost_usd": 0.02, "model": "gpt-5.6-sol", "client": "codex", "source": "proxy"}])
    (head / "proxy_savings.json").write_text(json.dumps(
        {"schema_version": "6", "generated_at": "2026-11-01T00:00:00Z",
         "lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)
    imp = lambda: cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))  # noqa: E731
    imp()
    imp()
    imp()                                              # 连跑三次
    conn = cv.db(vault)
    n = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(tokens_saved),0) t FROM ledger_events"
                     " WHERE kind='compression'").fetchone()
    assert n["n"] == 2 and n["t"] == 100               # 旧代码 6/300


def test_ledger_stats_minimal_lifetime(isolated, monkeypatch):
    """ledger-7 防御: lifetime 缺 cache_read_tokens/total_input_tokens 键 (写入端存 null)
    时 cmd_ledger_stats 不再 int(None) TypeError。"""
    from types import SimpleNamespace
    home, vault = isolated
    head = home / ".headroom"
    head.mkdir(parents=True, exist_ok=True)
    (head / "proxy_savings.json").write_text(json.dumps(
        {"lifetime": {"tokens_saved": 1000, "compression_savings_usd": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(cv, "HEADROOM_DIR", head)
    cv.cmd_ledger_import_headroom(SimpleNamespace(vault=str(vault)))
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault)))       # 旧代码在此崩溃


def test_fork_of_fork_not_swallowed(isolated, capsys):
    """M4 回归: fork 的 fork 必须产生 fork_of=F1 的新实体, 不被祖先的"已存在"吞掉。"""
    from types import SimpleNamespace
    _, vault = isolated
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                             "conversation": [{"type": "text", "role": "user", "content": "base"}],
                             "stats": cv.zero_stats()})
    a_sid = cv.snapshot_id(env)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "snapshots" / f"{a_sid}.json").write_text(json.dumps(env), encoding="utf-8")
    fk = lambda sid: cv.cmd_fork(  # noqa: E731
        SimpleNamespace(vault=str(vault), snapshot_id=sid, message=None))
    fk(a_sid)
    f1_sid = capsys.readouterr().out.split("→ ")[1].split("\n")[0].strip()
    n0 = len(list((vault / "snapshots").glob("cv_*.json")))
    fk(f1_sid)                                         # 旧代码在此报"已存在"指向 A
    f2 = [p for p in (vault / "snapshots").glob("cv_*.json")
          if json.loads(p.read_text(encoding="utf-8")).get("lineage", {}).get("fork_of") == f1_sid]
    assert len(list((vault / "snapshots").glob("cv_*.json"))) == n0 + 1
    assert len(f2) == 1


def test_export_codex_compaction_replaced_range():
    """migrate-1 回归: replaced_blocks = 上次压缩边界到当前最后一个会话块 (含端点)。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = w(d / "rollout-c.jsonl", [
        {"timestamp": "t", "ordinal": 0, "type": "session_meta",
         "payload": {"session_id": "s", "cwd": "D:\\p"}},
        {"timestamp": "t", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "q1"}]}},
        {"timestamp": "t", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "a1"}]}},
        {"timestamp": "t", "ordinal": 3, "type": "compacted", "payload": {"message": "sum1"}},
        {"timestamp": "t", "ordinal": 4, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "q2"}]}},
        {"timestamp": "t", "ordinal": 5, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "a2"}]}},
        {"timestamp": "t", "ordinal": 6, "type": "compacted", "payload": {"message": "sum2"}}])
    rb = [b["replaced_blocks"] for b in cv.export_codex(p, False)["conversation"]
          if b["type"] == "compaction"]
    assert rb == [[0, 1], [3, 4]]                      # 旧代码 [[0, 0], [0, 2]]


def test_blocks_to_codex_last_agent_message():
    """migrate-2 回归: agent_message 结尾的会话, task_complete.last_agent_message 不再为空。"""
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "C:\\w", "completeness": "full"},
                             "conversation": [{"type": "agent_message", "author": "a", "recipient": "b",
                                               "content": "final agent message", "ts": ""}],
                             "stats": cv.zero_stats()})
    lines, _, _, _ = cv.blocks_to_codex(env, False)
    tc = [l for l in lines if l.get("type") == "event_msg"
          and l.get("payload", {}).get("type") == "task_complete"][0]
    assert tc["payload"]["last_agent_message"] == "final agent message"


def test_owned_stats_ingest_denominator(isolated):
    """ledger-3 回归: via-smigrate 产物是 Claude 语义 usage, 命中率分母按 Claude 归一。"""
    _, vault = isolated
    _register_env(vault, {"agent": "cursor", "source_format": "via-smigrate:cursor"},
                  {"tokens_in": 10, "cache_read": 90, "cache_write": 0,
                   "total_tokens": 110, "usage_events": 1}, "2026-11-01T00:00:00")
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] == 0.9                # 旧代码 9.0 (900%)


def test_guard_rejection_drains_body(isolated):
    """M1 回归: 守卫拒绝的 POST 已先读净 body — 同一 keep-alive 连接的下一条请求完好。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/migrate", body=json.dumps({"id": "cv_bad", "target": "codex"}),
                  headers={"Content-Type": "text/plain"})          # 跨站简单请求 → 403
        r1 = c.getresponse()
        r1.read()
        assert r1.status == 403
        c.request("GET", "/healthz")                               # 同一 keep-alive 连接
        r2 = c.getresponse()
        body2 = r2.read()
        assert r2.status == 200                                    # 旧代码收到 501/400 垃圾
        assert json.loads(body2)["status"] == "ok"
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_errors_are_400_not_500(isolated):
    """serve-2 回归: 数值参数坏 / JSON body 坏 → 400 稳定 JSON, 不落 500 兜底回显异常文本。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/recent?limit=abc")
        assert code == 400 and "limit" in r["error"]
        code, r = _req(port, "POST", "/migrate", body="{not json",
                       headers={"Content-Type": "application/json"})
        assert code == 400 and "error" in r
    finally:
        srv.shutdown()
        srv.server_close()


def test_stats_dbless_no_poison(isolated, monkeypatch):
    """M5 回归: 无 vault.db 时 /stats 200 且不留零表空库; 事后冷路径 vault_stats 仍诚实。"""
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                             "conversation": [{"type": "text", "role": "user", "content": "hi"}],
                             "stats": cv.zero_stats()})
    sid = cv.snapshot_id(env)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env), encoding="utf-8")
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/stats")
        assert code == 200                                          # 旧代码 500 + 留零表空库
    finally:
        srv.shutdown()
        srv.server_close()
    import mcp_server  # noqa: E402
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    out = mcp_server.dispatch("vault_stats", {}, vault)
    # 修复后 /stats 会把库建齐 (不再留零表毒化), 冷路径两种诚实空态都合法:
    # 库不存在 → "账本为空"回退; 库为空表 → 全 0 数字。唯独不能再 isError
    assert "isError" not in out and "no such table" not in out
    assert "账本为空" in out or "快照数: 0" in out


def test_cold_search_skips_malformed_snapshot(isolated, monkeypatch):
    """M6 回归: 畸形快照文件不废掉冷路径 search/recent (与热索引一致地跳过)。"""
    import mcp_server  # noqa: E402
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    good = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                              "conversation": [{"type": "text", "role": "user",
                                                "content": "phantom 迁移方案"}],
                              "stats": cv.zero_stats()})
    sid = cv.snapshot_id(good)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(good), encoding="utf-8")
    (vault / "snapshots" / "cv_malformed000001.json").write_text(
        json.dumps({"conversation": [{"type": "text", "role": "user", "content": "phantom 迁移"}]}),
        encoding="utf-8")
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    out = mcp_server.dispatch("vault_search", {"query": "phantom"}, vault)
    assert sid in out and "isError" not in out
    out2 = mcp_server.dispatch("vault_recent", {"limit": 5}, vault)
    assert sid in out2


def test_search_tie_hot_cold_same_order(isolated, monkeypatch):
    """M7 回归: 得分并列时冷热返回同一快照 (limit 截断边界命中集一致, 不随哈希种子漂移)。"""
    import mcp_server  # noqa: E402
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    for fname, cat, text in (("cv_aaaa111111111111.json", "2026-01-01T00:00:00", "alpha here"),
                             ("cv_bbbb222222222222.json", "2026-02-02T00:00:00", "beta here")):
        env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                                 "conversation": [{"type": "text", "role": "user", "content": text}],
                                 "stats": cv.zero_stats()})
        env["created_at"] = cat
        (vault / "snapshots" / fname).write_text(json.dumps(env), encoding="utf-8")
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, hot = _req(port, "GET", "/search?q=alpha%20beta&limit=1")
        assert code == 200
        hot_sid = hot["hits"][0]["sid"]
    finally:
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    cold = mcp_server.dispatch("vault_search", {"query": "alpha beta", "limit": 1}, vault)
    assert hot_sid in cold


def test_zcode_subsecond_update_syncs(isolated, monkeypatch, tmp_path):
    """ledger-8 回归: zcode 水位严格前进 — 秒内的新消息增量不再被 <1s 容差吞掉。"""
    import sqlite3 as _s
    from types import SimpleNamespace
    _, vault = isolated
    zdb_p = tmp_path / "zcode.sqlite"
    db = _s.connect(zdb_p)
    db.executescript("""CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_updated INTEGER,
        time_deleted INTEGER, time_created INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, sequence INTEGER);""")
    db.execute("INSERT INTO session VALUES('s1','D:\\p',NULL,1000,NULL,1000)")
    db.execute("INSERT INTO message VALUES('m1','s1',?,900,1)", (json.dumps({"role": "user"}),))
    db.execute("INSERT INTO part VALUES('p1','m1','s1',?,1)",
               (json.dumps({"type": "text", "text": "第一句"}),))
    db.commit()
    monkeypatch.setattr(cv, "ZCODE_DB", zdb_p)
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    db.execute("INSERT INTO part VALUES('p2','m1','s1',?,2)",
               (json.dumps({"type": "text", "text": "秒回"}),))
    db.execute("UPDATE session SET time_updated=1500 WHERE id='s1'")   # 仅 +500ms
    db.commit()
    sync()                                             # 旧代码: 0.5s < 1s 容差 → "秒回"永久丢失
    snaps = sorted((vault / "snapshots").glob("cv_*.json"))
    assert any("秒回" in p.read_text(encoding="utf-8") for p in snaps)


# ---------------- 存量修复回归 (2026-11-07) ----------------

def _write_env(vault: Path, env: dict) -> str:
    sid = cv.snapshot_id(env)
    p = vault / "snapshots" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    return sid


def test_backfill_lineage_restores_chain(isolated):
    """T0 回归: parent_snapshot 列后加且 ALTER 未回填 → 历史版本在库里全是孤立叶子 (真库 921
    个幻影叶子)。backfill_lineage 从文件尾 lineage 回填后, 演进链恢复为一个会话; 自环防御。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    mk = lambda text: cv.build_envelope({"capture": {"agent": "codex", "session_ref": "s1"},  # noqa: E731
                                         "conversation": [{"type": "text", "role": "user", "content": text}],
                                         "stats": {**cv.zero_stats(), "tokens_in": 100, "usage_events": 1}})
    v1 = mk("hello")
    s1 = _write_env(vault, v1)
    v2 = mk("hello again")
    v2["lineage"]["parent_snapshot"] = s1
    s2 = _write_env(vault, v2)
    cv.register_snapshot(conn, v1, s1, vault / "snapshots" / f"{s1}.json")
    cv.register_snapshot(conn, v2, s2, vault / "snapshots" / f"{s2}.json")
    conn.execute("UPDATE snapshots SET parent_snapshot=NULL")    # 模拟 11-02 前登记的行
    conn.commit()
    assert cv.owned_stats(vault)["sessions"] == 2                # 断链: 两个"会话", 用量双计
    assert cv.read_lineage_tail(vault / "snapshots" / f"{s2}.json").get("parent_snapshot") == s1
    assert cv.backfill_lineage(conn) == 1
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100
    assert cv.backfill_lineage(conn) == 0                        # 标记后不重复扫描


def test_export_zcode_session_usage():
    """T1 回归: zcode 适配器从 model_usage 按 session_id 聚合用量 (OpenAI 语义, 输入含缓存)。"""
    import sqlite3 as _s
    db = _s.connect(":memory:")
    db.row_factory = _s.Row
    db.executescript("""CREATE TABLE session(id TEXT, directory TEXT, title TEXT, time_updated INTEGER,
        time_deleted INTEGER, time_created INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, data TEXT, sequence INTEGER);
        CREATE TABLE model_usage(session_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
        reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
        started_at INTEGER, completed_at INTEGER);""")
    db.execute("INSERT INTO session VALUES('s1','D:\\\\p','t',1000,NULL,1000)")
    db.execute("INSERT INTO message VALUES('m1','s1',?,900,1)", (json.dumps({"role": "user"}),))
    db.execute("INSERT INTO part VALUES('p1','m1','s1',?,1)", (json.dumps({"type": "text", "text": "hi"}),))
    db.execute("INSERT INTO model_usage VALUES('s1',100,10,2,80,5,1,2)")
    db.execute("INSERT INTO model_usage VALUES('s1',200,20,0,150,0,3,4)")
    db.execute("INSERT INTO model_usage VALUES('other',999,9,0,0,0,1,2)")
    srow = db.execute("SELECT * FROM session WHERE id='s1'").fetchone()
    s = cv.export_zcode_session(db, srow)["stats"]
    assert (s["tokens_in"], s["tokens_out"], s["cache_read"], s["usage_events"]) == (300, 30, 230, 2)
    assert s["total_tokens"] == 330                              # 输入已含缓存, 不再加 cache_read


def test_backfill_zcode_usage_as_of_version(isolated, monkeypatch, tmp_path):
    """T1 回归: 历史 zcode 版本按各自 created_at 回填"截至当时"的累计用量 (索引层, 不重写文件)。"""
    import sqlite3 as _s
    import datetime as _dt
    _, vault = isolated
    ms = lambda iso: int(_dt.datetime.fromisoformat(iso).timestamp() * 1000)  # noqa: E731
    zp = tmp_path / "z.sqlite"
    z = _s.connect(zp)
    z.execute("CREATE TABLE model_usage(session_id TEXT, input_tokens INTEGER, output_tokens INTEGER,"
              " reasoning_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,"
              " started_at INTEGER, completed_at INTEGER)")
    z.execute("INSERT INTO model_usage VALUES('s1',100,10,0,80,0,?,?)",
              (ms("2026-11-01T00:00:00+08:00"), ms("2026-11-01T00:00:01+08:00")))
    z.execute("INSERT INTO model_usage VALUES('s1',200,20,0,150,0,?,?)",
              (ms("2026-11-03T00:00:00+08:00"), ms("2026-11-03T00:00:01+08:00")))
    z.commit()
    z.close()
    monkeypatch.setattr(cv, "ZCODE_DB", zp)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    ids = []
    for text, cat in (("v1", "2026-11-02T00:00:00+08:00"), ("v2", "2026-11-04T00:00:00+08:00")):
        env = cv.build_envelope({"capture": {"agent": "zcode", "source_format": "zcode-sqlite",
                                             "session_ref": "s1"},
                                 "conversation": [{"type": "text", "role": "user", "content": text}],
                                 "stats": cv.zero_stats()})
        env["created_at"] = cat
        sid = _write_env(vault, env)
        cv.register_snapshot(conn, env, sid, vault / "snapshots" / f"{sid}.json")
        ids.append(sid)
    conn.commit()
    assert cv.backfill_zcode_usage(conn) == 2
    got = {r["id"]: r["tokens_in"] for r in conn.execute("SELECT id, tokens_in FROM snapshots")}
    assert got[ids[0]] == 100 and got[ids[1]] == 300             # 各自截至当时, 不是统一的终值


def test_migration_copy_baseline_not_double_counted(isolated):
    """T2 回归: --apply 迁移副本经 sync 入库时打上 migrated_from + 首版用量基线, owned_stats
    扣基线 — 源会话用量不再被副本重复计一次 (评审遗留项, fork_of 落库后收敛)。"""
    from types import SimpleNamespace
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "33333333-3333-3333-3333-333333333333")
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()
    conn = cv.db(vault)
    src_sid = conn.execute("SELECT id FROM snapshots").fetchone()[0]
    r = cv.do_import(vault, src_sid, "codex", apply=True)      # 副本写进 ~/.codex/sessions (隔离 HOME)
    assert Path(r["dest"]).exists()
    sync()                                                      # 副本被扫入: 首版 usage = 源用量
    row = conn.execute("SELECT migrated_from, migration_baseline FROM snapshots WHERE id != ?",
                       (src_sid,)).fetchone()
    assert row["migrated_from"] == src_sid and json.loads(row["migration_baseline"])["tokens_in"] == 100
    own = cv.owned_stats(vault)
    assert own["sessions"] == 2 and own["tokens_in"] == 100     # 旧口径 200


def test_sync_debounce_active_vs_idle(isolated):
    """T7 回归: 周期同步 (debounce) 对活跃会话限频, 空闲后立即捕获; 手动 sync 不受影响。"""
    from types import SimpleNamespace
    import os as _os
    import time as _t
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "44444444-4444-4444-4444-444444444444")
    sync = lambda deb: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None, debounce=deb))  # noqa: E731
    count = lambda: cv.db(vault).execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]  # noqa: E731
    sync(True)
    assert count() == 1                                         # 首次入库不去抖
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 9, "type": "response_item",
                 "payload": {"type": "message", "id": "m9", "role": "user",
                             "content": [{"type": "input_text", "text": "more"}]}})
    w(src, rows)                                                # 源刚更新 = 活跃
    sync(True)
    assert count() == 1                                         # 活跃且距上版 <30 分钟: 不出版本
    old = _t.time() - 700
    _os.utime(src, (old, old))                                  # 源已空闲 >10 分钟
    sync(True)
    assert count() == 2                                         # 空闲: 立即捕获最终态
    rows.append({"timestamp": "2026-11-01T02:00:00.000Z", "ordinal": 10, "type": "response_item",
                 "payload": {"type": "message", "id": "m10", "role": "user",
                             "content": [{"type": "input_text", "text": "even more"}]}})
    w(src, rows)
    sync(False)
    assert count() == 3                                         # 手动 sync 立即捕获


def test_recent_leaves_collapses_versions(isolated):
    """T3 回归: /recent?leaves=1 每会话只列演进链最新版本并附 ×N 版本数; 默认仍列全部版本。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    mk = lambda ref, text: cv.build_envelope({"capture": {"agent": "codex", "session_ref": ref},  # noqa: E731
                                              "conversation": [{"type": "text", "role": "user",
                                                                "content": text}],
                                              "stats": cv.zero_stats()})
    a1 = mk("sa", "alpha one")
    sa1 = _write_env(vault, a1)
    a2 = mk("sa", "alpha two")
    a2["lineage"]["parent_snapshot"] = sa1
    sa2 = _write_env(vault, a2)
    b1 = mk("sb", "beta")
    sb1 = _write_env(vault, b1)
    for env, sid in ((a1, sa1), (a2, sa2), (b1, sb1)):
        cv.register_snapshot(conn, env, sid, vault / "snapshots" / f"{sid}.json")
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, all_ = _req(port, "GET", "/recent?limit=50")
        assert code == 200 and all_["total"] == 3
        code, lv = _req(port, "GET", "/recent?limit=50&leaves=1")
        assert code == 200 and lv["total"] == 2 and lv["versions_total"] == 3
        by = {r["sid"]: r for r in lv["rows"]}
        assert sa1 not in by and by[sa2]["versions"] == 2 and by[sb1]["versions"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_owned_stats_input_effective(isolated):
    """KPI 口径: input_effective = 各来源语义归一后的总输入 (Claude 补回 cache 读/写, OpenAI 原值)。"""
    _, vault = isolated
    _register_env(vault, {"agent": "claude-code", "source_format": "claude-projects"},
                  {"tokens_in": 10, "cache_read": 90, "cache_write": 5, "usage_events": 1},
                  "2026-11-01T00:00:00")
    _register_env(vault, {"agent": "zcode", "source_format": "zcode-sqlite"},
                  {"tokens_in": 1000, "cache_read": 950, "usage_events": 1}, "2026-11-01T01:00:00")
    own = cv.owned_stats(vault)
    assert own["input_effective"] == 105 + 1000                 # 不是 10+1000, 也不是 +950 重计


# ---------------- 二轮全面测试回归 (dwfrun-7597d968 发现) ----------------

def test_index_malformed_typed_snapshot(isolated):
    """二轮 serve-1/mcp-1: capture/conv 类型不对的快照, 热索引启动不崩、/search 200
    (与冷路径一致跳过)。旧代码 AttributeError 让服务起不来 + /search 500 + 索引停更。"""
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    good = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                              "conversation": [{"type": "text", "role": "user", "content": "markerok"}],
                              "stats": cv.zero_stats()})
    sid = cv.snapshot_id(good)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(good), encoding="utf-8")
    (vault / "snapshots" / "cv_bad0000000000001.json").write_text(
        json.dumps({"capture": "str", "conversation": [], "stats": {}}), encoding="utf-8")
    (vault / "snapshots" / "cv_bad0000000000002.json").write_text(
        json.dumps({"capture": {"agent": "zcode"}, "conversation": ["str"], "stats": {"blocks_total": 1}}),
        encoding="utf-8")
    srv, _ = cv.build_vault_server(vault, 0)                    # 旧代码此处 AttributeError 崩溃
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/search?q=markerok")
        assert code == 200 and r["total"] == 1 and r["hits"][0]["sid"] == sid
        code, r = _req(port, "GET", "/healthz")
        assert code == 200 and r["snapshots"] == 1              # 畸形文件被跳过, 不进索引
    finally:
        srv.shutdown()
        srv.server_close()


def test_timeline_leaf_and_agent_default(isolated):
    """二轮 serve-2/3: /timeline leaf 用 parent_snapshot 列 (曾误用 blocks_total 列,
    真叶子恒 False); agent 缺省时按 session_ref 全 agent 匹配 (曾用 '='+'%' 恒空)。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    prev = None
    ids = {}
    for name, cat, blocks in (("p", "2026-11-05T09:00:00", 0), ("q", "2026-11-06T09:00:00", 4),
                              ("r", "2026-11-07T09:00:00", 6)):
        env = cv.build_envelope({"capture": {"agent": "zcode", "session_ref": "S"},
                                 "conversation": [{"type": "text", "role": "user", "content": name}],
                                 "stats": {**cv.zero_stats(), "blocks_total": blocks}})
        env["created_at"] = cat
        sid = cv.snapshot_id(env)
        ids[name] = sid
        p = vault / "snapshots" / f"{sid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        conn.execute("INSERT INTO snapshots(id,created_at,agent,session_ref,blocks_total,parent_snapshot)"
                     " VALUES(?,?,?,?,?,?)", (sid, cat, "zcode", "S", blocks, prev))
        prev = sid
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, r = _req(port, "GET", "/timeline?session=S&agent=zcode")
        assert code == 200
        leaf = {v["sid"]: v["leaf"] for day in r["days"] for v in day["versions"]}
        assert leaf == {ids["p"]: False, ids["q"]: False, ids["r"]: True}
        code, r2 = _req(port, "GET", "/timeline?session=S")     # agent 缺省
        assert code == 200 and r2["days"] == r["days"]
        code, r3 = _req(port, "GET", "/timeline?session=S&agent=")  # 空 agent 同义
        assert code == 200 and r3["days"] == r["days"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_duplicate_instance_raises(isolated):
    """二轮 serve-4: allow_reuse_address 恒 False 后, 首选端口上已有本服务实例 → 抛 OSError
    (单实例语义), 不再静默双绑收不到流量还写 serve.json 误导 MCP。"""
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError):
            cv.build_vault_server(vault, port)                  # 旧代码静默双绑
    finally:
        srv.shutdown()
        srv.server_close()


def test_guard_stale_cookie_rotation(isolated):
    """二轮 serve-5: 旧 cookie 存在时正确的 ?token=/header 仍放行, 且 cookie 被轮换刷新
    (旧代码 cookie 先占位 → 首访自动登录永久 401, HttpOnly 前端清不掉)。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="NEW")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/?token=NEW", headers={"Cookie": "vault_token=OLD"})
        r = c.getresponse(); body = r.read(); setc = r.getheader("Set-Cookie")
        assert r.status == 200 and "vault_token=NEW" in (setc or "")
        c.request("GET", "/healthz", headers={"Cookie": "vault_token=OLD"})
        r2 = c.getresponse(); r2.read()
        assert r2.status == 401                                 # 旧 cookie 本身不再放行
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_nonobject_body_400(isolated):
    """二轮 serve-6: 合法 JSON 但非对象 ([1,2]/null/"s"/42) → 400, 不落 500 回显异常。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for body in ("[1,2]", "null", '"cv_abc"', "42"):
            code, r = _req(port, "POST", "/migrate", body=body,
                           headers={"Content-Type": "application/json"})
            assert code == 400 and "error" in r, (body, code, r)
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_events_and_audit_dbless(isolated):
    """二轮 concurrency-1/serve-7: 零表空库上 /probe_events 与 /audit 返回诚实空态, 不落 500。"""
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    import sqlite3 as _s
    _s.connect(vault / "vault.db").close()                      # 造零表空库 (M5 同族场景)
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/probe_events?limit=5")
        assert code == 200
        code, r = _req(port, "GET", "/audit")
        assert code == 200 and r["total_events"] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_log_redaction_percent_encoded(isolated):
    """二轮 serve-8: %74oken= (parse_qs 会解码为 token) 也是有效凭据 — 日志必须脱敏。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0, token="S3CR3T-tok-777")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _req(port, "GET", "/healthz?%74oken=S3CR3T-tok-777")
        assert code == 200                                      # 凭据有效
    finally:
        srv.shutdown()
        srv.server_close()
    log = (vault / "serve_http.log").read_text(encoding="utf-8")
    assert "S3CR3T-tok-777" not in log and "token=***" in log


def test_prune_unlink_failure_keeps_row(isolated):
    """二轮 concurrency-2: 文件删除失败 (Windows 共享冲突) 时该行保留, 盘库同进退,
    不产生索引/账本口径分裂的孤儿文件。"""
    from types import SimpleNamespace
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)
    src = vault / "src.jsonl"
    src.write_text("{}", encoding="utf-8")
    prev = None
    paths = []
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(("v1", "v2", "v3")):
        env = cv.build_envelope({"capture": {"agent": "codex", "session_ref": "sx"},
                                 "conversation": [{"type": "text", "role": "user", "content": text}],
                                 "stats": cv.zero_stats()})
        env["created_at"] = f"2026-11-07T0{i}:00:00+08:00"
        if prev:
            env["lineage"]["parent_snapshot"] = prev
        sid = cv.snapshot_id(env)
        prev = sid
        if i == 1:  # 中间版本 v2: 把"快照文件"换成目录 → unlink 抛 IsADirectoryError
            d = vault / "snapshots" / f"{sid}.json"
            d.mkdir()
            paths.append(d)
        else:
            p = vault / "snapshots" / f"{sid}.json"
            p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
            paths.append(p)
        cv.register_snapshot(conn, env, sid, p)
    conn.execute("INSERT OR REPLACE INTO sync_sources VALUES(?,?,?,?,?)",
                 (str(src.resolve()), 0, 0, prev, "2026-11-07T02:00:00+08:00"))
    conn.commit()
    cv.cmd_prune(SimpleNamespace(vault=str(vault), apply=True))
    left = {r[0] for r in conn.execute("SELECT id FROM snapshots")}
    assert paths[1].stem in left and paths[1].is_dir()          # 删失败 → 行保留 (下次再试)
    assert paths[0].stem not in left                           # 正常回收
    assert paths[1].stem in left and paths[1].is_dir()          # unlink 失败 → 行保留 (下次再试), 无孤儿
    assert paths[2].stem in left                               # 叶子恒留
    assert len(left) == 2
    tl = cv.audit_summary_and_timeline(vault, "", "all")
    assert any(e.get("status") == "prune" for day in tl["timeline"] for e in day["events"])


def test_prices_boolean_cost_none(isolated):
    """二轮 regression-1: prices 值为 bool 时 (float(True)=1.0 不抛错) 不编造假成本 → cost=None。"""
    _, vault = isolated
    import sqlite3 as _s
    (vault / "prices.json").write_text(json.dumps(
        {"bool-priced": {"input": True, "output": False}}), encoding="utf-8")
    zp = vault / "zcode.sqlite"
    z = _s.connect(zp)
    z.execute("CREATE TABLE model_usage(model_id TEXT, input_tokens INTEGER, output_tokens INTEGER,"
              " cache_read_input_tokens INTEGER)")
    z.execute("INSERT INTO model_usage VALUES('bool-priced', 5000000, 1000000, 0)")
    z.commit()
    z.close()
    monkey_like = vault
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)
    # /ledger 直读 cv.ZCODE_DB: 指向合成库
    old = cv.ZCODE_DB
    cv.ZCODE_DB = zp
    try:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, r = _req(port, "GET", "/ledger")
            assert code == 200
            assert r["zcode_usage"]["bool-priced"]["cost"] is None
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        cv.ZCODE_DB = old


def test_export_fresh_vault_and_sibling_chain(isolated):
    """二轮 migrate-1 + ledger-1/2: 全新 vault 直接 export 不崩 (sync_sources 表已建);
    export/sync 水位键一致 (单行); 源演进后 sync 正常挂链; 库行删除后 export 重挂兄弟链。"""
    from types import SimpleNamespace
    import os as _os
    home, vault = isolated
    src = _mk_chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01",
                            "55555555-5555-5555-5555-555555555555")
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))  # 全新 vault 不崩
    conn = cv.db(vault)
    assert conn.execute("SELECT COUNT(*) FROM sync_sources").fetchone()[0] == 1
    own0 = cv.owned_stats(vault)
    assert own0["sessions"] == 1 and own0["tokens_in"] == 100
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "more"}]}})
    w(src, rows)
    sync = lambda: cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))  # noqa: E731
    sync()                                                      # 水位键一致: 命中行挂链, 不再双行/双计
    assert conn.execute("SELECT COUNT(*) FROM sync_sources").fetchone()[0] == 1
    own1 = cv.owned_stats(vault)
    assert own1["sessions"] == 1 and own1["tokens_in"] == 100
    conn.execute("DELETE FROM snapshots WHERE parent_snapshot IS NOT NULL")  # 模拟库行丢失
    conn.commit()
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    own2 = cv.owned_stats(vault)
    assert own2["sessions"] == 1 and own2["tokens_in"] == 100   # 兄弟链重挂, 不再永久双计


def test_prune_daily_keep_relink_and_guards(isolated, monkeypatch, tmp_path, capsys):
    """GC 回归: 每会话每天留最后一版 + 叶子; fork 与源已消失会话整链保留; 保留集重链后
    账本 sessions/tokens 不变; --apply 先备份; GC 事件写入审计时间线。"""
    from types import SimpleNamespace
    import datetime as _dt
    _, vault = isolated
    ms = lambda d, h: int(_dt.datetime.fromisoformat(f"2026-11-0{d}T{h}:00:00+08:00").timestamp())  # noqa: E731
    # 造 zcode 库: s1 存活, sdead 不存在 (源已删 → vault 是唯一副本)
    zp = tmp_path / "z.sqlite"
    z = __import__("sqlite3").connect(zp)
    z.execute("CREATE TABLE session(id TEXT, time_deleted INTEGER)")
    z.execute("INSERT INTO session VALUES('s1', NULL)")
    z.commit()
    z.close()
    monkeypatch.setattr(cv, "ZCODE_DB", zp)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    mk = lambda ref, d, h, text, parent=None: (lambda env: (env["lineage"].update(  # noqa: E731
        {"parent_snapshot": parent} if parent else {}), env)[1])
    reg = lambda env, sid: (_write_env(vault, env), cv.register_snapshot(conn, env, sid,  # noqa: E731
        vault / "snapshots" / f"{sid}.json"))[0]
    # 会话 A (codex 文件源不存在 → 视为死源? path 造出来才活; 这里不建源文件 → 整链保留)
    a1 = cv.build_envelope({"capture": {"agent": "codex", "session_ref": "sa"},
                            "conversation": [{"type": "text", "role": "user", "content": "a1"}],
                            "stats": {**cv.zero_stats(), "tokens_in": 100, "usage_events": 1}})
    a1["created_at"] = "2026-11-05T10:00:00+08:00"
    sa1 = _write_env(vault, a1)
    a2 = cv.build_envelope({"capture": {"agent": "codex", "session_ref": "sa"},
                            "conversation": [{"type": "text", "role": "user", "content": "a2"}],
                            "stats": {**cv.zero_stats(), "tokens_in": 100, "usage_events": 1}})
    a2["created_at"] = "2026-11-06T10:00:00+08:00"
    a2["lineage"]["parent_snapshot"] = sa1
    sa2 = _write_env(vault, a2)
    cv.register_snapshot(conn, a1, sa1, vault / "snapshots" / f"{sa1}.json")
    cv.register_snapshot(conn, a2, sa2, vault / "snapshots" / f"{sa2}.json")
    # 会话 B (codex, 两天的演进链 + 中间版本)
    envs = []
    prev = None
    for d, h, text in ((5, "09", "b-day5"), (5, "18", "b-day5-late"), (6, "09", "b-day6"), (6, "20", "b-day6-late")):
        e = cv.build_envelope({"capture": {"agent": "codex", "session_ref": "sb"},
                               "conversation": [{"type": "text", "role": "user", "content": text}],
                               "stats": {**cv.zero_stats(), "tokens_in": 100, "usage_events": 1}})
        e["created_at"] = f"2026-11-0{d}T{h}:00:00+08:00"
        if prev:
            e["lineage"]["parent_snapshot"] = prev
        prev = cv.snapshot_id(e)
        envs.append((e, prev))
    sb_ids = [reg(e, cv.snapshot_id(e)) for e, _ in envs]
    src_b = vault / "src-b.jsonl"                                # B 的源文件存在 → 活会话, 按天规则生效
    src_b.write_text("{}", encoding="utf-8")
    conn.execute("INSERT OR REPLACE INTO sync_sources VALUES(?,?,?,?,?)",
                 (str(src_b), 0, 0, sb_ids[3], "2026-11-06T20:00:00+08:00"))
    # 会话 C (zcode, 源已删): 整链应保留
    c1 = cv.build_envelope({"capture": {"agent": "zcode", "source_format": "zcode-sqlite",
                                        "session_ref": "sdead"},
                            "conversation": [{"type": "text", "role": "user", "content": "c1"}],
                            "stats": cv.zero_stats()})
    c1["created_at"] = "2026-11-05T08:00:00+08:00"
    sc1 = _write_env(vault, c1)
    cv.register_snapshot(conn, c1, sc1, vault / "snapshots" / f"{sc1}.json")
    conn.commit()
    own0 = cv.owned_stats(vault)
    assert own0["sessions"] == 3 and own0["tokens_in"] == 200    # A/B/C 各一叶; a1⊂a2 只计 a2
    # dry-run: 不动数据
    cv.cmd_prune(SimpleNamespace(vault=str(vault), apply=False))
    assert cv.db(vault).execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 7
    # apply: 删前备份; A 是死源整链保留 (2 版都留); B 留 day5-late/day6-late/leaf=b-day6-late
    cv.cmd_prune(SimpleNamespace(vault=str(vault), apply=True))
    left = {r[0] for r in conn.execute("SELECT id FROM snapshots")}
    # A 死源整链保留; B 留 day5-late (每日记录) + day6-late (叶子); b-day5-09/b-day6-09 回收
    assert left == {sa1, sa2, sb_ids[1], sb_ids[3], sc1}
    # 重链: leaf (b-day6-late) 的 parent = day5-late (中间被删的 b-day5-09/b-day6-09 已绕开)
    assert conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?",
                        (sb_ids[3],)).fetchone()[0] == sb_ids[1]
    assert conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?",
                        (sb_ids[1],)).fetchone()[0] is None      # 链头
    # 账本口径: sessions 不因 prune 膨胀 (每链一个叶子), tokens 不变
    own1 = cv.owned_stats(vault)
    assert own1["sessions"] == 3 and own1["tokens_in"] == 200
    # GC 事件进了审计时间线
    tl = cv.audit_summary_and_timeline(vault, "", "all")
    assert any(e.get("status") == "prune" for day in tl["timeline"] for e in day["events"])
    # backfill 不得让悬空 parent 复活 (b-day6-09 已删), 也不得覆盖重链结果
    cv.backfill_lineage(conn, force=True)
    assert conn.execute("SELECT parent_snapshot FROM snapshots WHERE id=?",
                        (sb_ids[3],)).fetchone()[0] == sb_ids[1]
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE parent_snapshot=?",
                        (sb_ids[2],)).fetchone()[0] == 0


def test_serve_port_fallback_and_duplicate_instance(isolated, monkeypatch):
    """回归 (2026-11-07 托盘停摆): 首选端口落进 Windows 保留区段 (绑定 WinError 10013) 时
    服务退到备用端口并写 serve.json; 首选端口上真有 Context Vault 实例时仍按单实例语义抛出。"""
    import socket
    from http.server import ThreadingHTTPServer
    # 托盘生产路径先置 False (True 时 Windows 允许对活动监听端口静默双绑, 单实例检测失效)
    monkeypatch.setattr(ThreadingHTTPServer, "allow_reuse_address", False)
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    # 1) 首选端口被非本服务占用 (模拟系统保留/他用): 退到备用端口
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    monkeypatch.setattr(cv, "SERVE_FALLBACK_PORTS", [])          # 只剩 0 = 随机空闲端口
    try:
        srv, _ = cv.build_vault_server(vault, busy)
        try:
            actual = srv.server_address[1]
            assert actual != busy
            sj = json.loads((vault / "serve.json").read_text(encoding="utf-8"))
            assert sj["port"] == actual and sj["url"] == f"http://127.0.0.1:{actual}"
            # 2) 首选端口上已有 Context Vault 实例: 不回退, 抛 OSError (托盘单实例退出)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            with pytest.raises(OSError):
                cv.build_vault_server(vault, actual)
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        blocker.close()


def test_mcp_discovers_service_via_serve_json(isolated, monkeypatch):
    """MCP 热路径经 vault/serve.json 找到退到备用端口的服务 (默认 8791 不可用时不再静默冷回退)。"""
    import mcp_server  # noqa: E402
    _, vault = isolated
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": "D:\\x"},
                             "conversation": [{"type": "text", "role": "user", "content": "hello vault"}],
                             "stats": cv.zero_stats()})
    sid = _write_env(vault, env)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    conn.commit()
    srv, _ = cv.build_vault_server(vault, 0)                     # 随机端口 + 写 serve.json
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_server, "SVC_BASE", mcp_server._DEFAULT_SVC)  # 未显式指定地址
        out = mcp_server.dispatch("vault_search", {"query": "hello vault"}, vault)
        assert sid in out and "via 热索引" in out
    finally:
        srv.shutdown()
        srv.server_close()


def test_resolve_top_conversations_and_batch_import(isolated, monkeypatch, tmp_path):
    """过滤 ZCode 子代理与历史演进版本，仅保留各项目最新顶层主对话，并支持 --project 批量导入。"""
    import sqlite3 as _s
    _, vault = isolated
    zdb = tmp_path / "zcode_test.sqlite"
    z = _s.connect(zdb)
    z.execute("CREATE TABLE session(id TEXT, parent_id TEXT, time_deleted INTEGER, time_archived INTEGER)")
    z.execute("INSERT INTO session VALUES('z_top', NULL, NULL, NULL)")
    z.execute("INSERT INTO session VALUES('z_sub', 'z_top', NULL, NULL)")
    z.execute("INSERT INTO session VALUES('z_arch', NULL, NULL, 1000)")
    z.commit()
    z.close()
    monkeypatch.setattr(cv, "ZCODE_DB", zdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def add_snap(agent, sref, cwd, title, text, created_at, parent=None):
        env = cv.build_envelope({
            "capture": {"agent": agent, "session_ref": sref, "cwd": cwd, "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": text}],
            "stats": cv.zero_stats(),
        })
        env["created_at"] = created_at
        if parent:
            env["lineage"]["parent_snapshot"] = parent
        sid = cv.snapshot_id(env)
        p = vault / "snapshots" / f"{sid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, p)
        return sid

    sid_v1 = add_snap("zcode", "z_top", "D:\\projA", "主对话A", "v1", "2026-11-10T10:00:00Z")
    sid_v2 = add_snap("zcode", "z_top", "D:\\projA", "主对话A", "v2", "2026-11-11T10:00:00Z", parent=sid_v1)
    sid_sub = add_snap("zcode", "z_sub", "D:\\projA", "子代理", "sub", "2026-11-11T11:00:00Z")
    sid_arch = add_snap("zcode", "z_arch", "D:\\projA", "归档会话", "arch", "2026-11-09T10:00:00Z")
    sid_b = add_snap("zcode", "z_other", "D:\\projB", "项目B对话", "other", "2026-11-12T10:00:00Z")
    conn.commit()
    conn.close()

    res_a = cv.resolve_top_conversations(vault, project="projA")
    assert [r["id"] for r in res_a] == [sid_v2]

    res_all = cv.resolve_top_conversations(vault, include_subagents=False)
    assert {r["id"] for r in res_all} == {sid_v2, sid_b}

    res_inc = cv.resolve_top_conversations(vault, project="projA", include_subagents=True)
    assert {r["id"] for r in res_inc} == {sid_v2, sid_sub, sid_arch}

    cv.main(["--vault", str(vault), "import", "--project", "projA", "--target", "markdown"])
    assert (vault / "outbox" / f"brief-{sid_v2}.md").exists()
    assert not (vault / "outbox" / f"brief-{sid_sub}.md").exists()
    assert not (vault / "outbox" / f"brief-{sid_b}.md").exists()


def test_projects_endpoint_and_migrate_project(isolated, monkeypatch, tmp_path):
    """验证 /projects 项目树归组接口（含未分组任务）与 POST /migrate_project 批量导出接口。"""
    _, vault = isolated
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def add_snap(sref, cwd, title, text, created_at):
        env = cv.build_envelope({
            "capture": {"agent": "zcode", "session_ref": sref, "cwd": cwd, "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": text}],
            "stats": cv.zero_stats(),
        })
        env["created_at"] = created_at
        sid = cv.snapshot_id(env)
        p = vault / "snapshots" / f"{sid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, p)
        return sid

    sid_a = add_snap("s_a", "E:\\paper-studio", "论文平台", "hello paper", "2026-11-10T10:00:00Z")
    sid_at = add_snap("s_at", "E:\\paper-studio-team", "团队版", "hello team", "2026-11-11T10:00:00Z")
    sid_u = add_snap("s_u", "C:\\Users\\ngh\\.zcode\\workspace\\draft-1", "草稿问答", "draft", "2026-11-12T10:00:00Z")
    conn.commit()
    conn.close()

    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, data = _req(port, "GET", "/projects")
        assert code == 200
        assert data["total_projects"] == 3
        assert data["total_conversations"] == 3
        names = [p["name"] for p in data["projects"]]
        assert names == ["paper-studio-team", "paper-studio", "未分组任务"]
        assert data["projects"][-1]["is_ungrouped"] is True

        # 精确路径与目录名均不应误导出 paper-studio-team
        assert [it["id"] for it in cv._resolve_project_items(vault, "paper-studio")] == [sid_a]
        code, res = _req(port, "POST", "/migrate_project",
                         body=json.dumps({"project": "E:\\paper-studio", "target": "markdown", "apply": False}),
                         headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        assert code == 200 and res["count"] == 1
        assert len(res["files"]) == 1 and sid_a in res["files"][0]
        assert not any(sid_at in f for f in res["files"])
        assert not any(sid_u in f for f in res["files"])

        # 未匹配项目返回 404
        code, _ = _req(port, "POST", "/migrate_project",
                         body=json.dumps({"project": "no-such-proj", "target": "markdown"}),
                         headers={"Content-Type": "application/json"})
        assert code == 404

        # 缺 smigrate 时 antigravity 目标稳定返回 400（不崩溃）
        monkeypatch.setattr(cv, "find_smigrate", lambda: None)
        code, err = _req(port, "POST", "/migrate",
                         body=json.dumps({"id": sid_a, "target": "antigravity"}),
                         headers={"Content-Type": "application/json"})
        assert code == 400 and "smigrate" in err["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_antigravity_set_workspace_protobuf_and_project_id(tmp_path, monkeypatch):
    """回归: 多字节 protobuf tag (field 18) 无损解析/序列化, 修正 file:/// URI 编码并写入 project_id。"""
    import sqlite3 as _s
    monkeypatch.setattr(cv, "HOME", tmp_path)
    proj_id = "27bddbf9-3f69-4072-b5f5-7c10d7fd25dc"
    pdir = tmp_path / ".gemini" / "config" / "projects"
    pdir.mkdir(parents=True)
    (pdir / f"{proj_id}.json").write_text(json.dumps({
        "id": proj_id,
        "name": "goai",
        "projectResources": {"resources": [{"folderUri": "file:///d%3A/goai"}]},
    }), encoding="utf-8")

    dbp = tmp_path / "conv.db"
    conn = _s.connect(dbp)
    conn.execute("CREATE TABLE trajectory_metadata_blob(id TEXT PRIMARY KEY, data BLOB)")
    # 模拟 smigrate 初始 blob: field 6 (session) + field 18 (outside-of-project, tag=\x92\x01)
    init_blob = cv._pb_len(6, b"sess-uuid") + cv._pb_len(18, b"outside-of-project")
    conn.execute("INSERT INTO trajectory_metadata_blob VALUES('main', ?)", (init_blob,))
    conn.commit()
    conn.close()

    assert cv._antigravity_set_workspace(dbp, "D:\\goai", 1794794528) is True

    conn = _s.connect(dbp)
    out = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
    conn.close()

    # 完整 protobuf 重解析不得截断或抛越界
    parsed = {}
    i = 0
    while i < len(out):
        k, i = cv._rd_varint(out, i)
        w, kn = k & 7, k >> 3
        assert w == 2
        ln, i = cv._rd_varint(out, i)
        parsed[kn] = out[i:i + ln]
        i += ln
    assert i == len(out)
    assert parsed[3] != parsed[6] and len(parsed[3]) == 36
    assert parsed[6] == b"sess-uuid"
    assert parsed[7] == b"file:///d%3A/goai"
    assert parsed[18] == proj_id.encode("ascii")


def test_antigravity_fill_steps_payload_and_apply(isolated, tmp_path, monkeypatch):
    """验证 step_payload Field 5 与 steps.metadata 对齐原生格式，source=1 与 --apply 落盘。"""
    import sqlite3 as _s
    _, vault = isolated
    monkeypatch.setattr(cv, "HOME", tmp_path)
    pdir = tmp_path / ".gemini" / "config" / "projects"
    pdir.mkdir(parents=True)
    proj_id = "27bddbf9-3f69-4072-b5f5-7c10d7fd25dc"
    (pdir / f"{proj_id}.json").write_text(json.dumps({
        "id": proj_id, "name": "goai",
        "projectResources": {"resources": [{"folderUri": "file:///d%3A/goai"}]},
    }), encoding="utf-8")

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    env = cv.build_envelope({
        "capture": {"agent": "zcode", "session_ref": "s_agy", "cwd": "D:\\goai", "originator": "测试导出"},
        "conversation": [
            {"type": "text", "role": "user", "content": "hi", "ts": "2026-11-10T10:00:00Z"},
            {"type": "text", "role": "assistant", "content": "hello", "ts": "2026-11-10T10:05:00Z"},
        ],
        "stats": cv.zero_stats(),
    })
    sid = cv.snapshot_id(env)
    sp = vault / "snapshots" / f"{sid}.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    cv.register_snapshot(conn, env, sid, sp)
    conn.commit()
    conn.close()

    traj_id = "aaaaaaaa-1111-4111-8111-111111111111"
    casc_id = "bbbbbbbb-2222-4222-8222-222222222222"

    # 桩 smigrate：直接写出最小 smigrate SQLite 结构（source=17, 无 step_payload.f5）
    monkeypatch.setattr(cv, "find_smigrate", lambda: "fake-smigrate")

    def fake_run(cmd, **kw):
        out_path = Path(cmd[cmd.index("--output") + 1])
        c = _s.connect(out_path)
        c.execute("CREATE TABLE trajectory_meta(trajectory_id text, cascade_id text, trajectory_type integer, source integer, PRIMARY KEY (trajectory_id))")
        c.execute("CREATE TABLE steps(idx integer PRIMARY KEY, step_type integer, status integer, has_subtrajectory numeric DEFAULT 0, metadata blob, step_payload blob)")
        c.execute("CREATE TABLE trajectory_metadata_blob(id text PRIMARY KEY, data blob)")
        c.execute("INSERT INTO trajectory_meta VALUES(?, ?, 4, 17)", (traj_id, casc_id))
        c.execute("INSERT INTO trajectory_metadata_blob VALUES('main', ?)", (cv._pb_len(6, casc_id.encode()),))
        p0 = cv._pb_var(1, 14) + cv._pb_var(4, 3) + cv._pb_len(19, b"\x12\x02hi")
        p1 = cv._pb_var(1, 15) + cv._pb_var(4, 3) + cv._pb_len(20, b"\n\x05hello")
        c.execute("INSERT INTO steps(idx, step_type, status, metadata, step_payload) VALUES(0, 14, 3, NULL, ?)", (p0,))
        c.execute("INSERT INTO steps(idx, step_type, status, metadata, step_payload) VALUES(1, 15, 3, NULL, ?)", (p1,))
        c.commit()
        c.close()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cv.subprocess, "run", fake_run)

    res = cv.do_import(vault, sid, "antigravity", apply=True)
    assert res["applied"] is True and res["new_sid"] == casc_id
    target_db = tmp_path / ".gemini" / "antigravity" / "conversations" / f"{casc_id}.db"
    assert Path(res["dest"]) == target_db and target_db.exists()

    c = _s.connect(target_db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert c.execute("SELECT source FROM trajectory_meta").fetchone()[0] == 1
    blob_fields, tail = cv._pb_decode(c.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0])
    assert tail == b"" and (3, 2) in blob_fields and blob_fields[(18, 2)][0] == proj_id.encode("ascii")
    rows = c.execute("SELECT idx, metadata, step_payload FROM steps ORDER BY idx").fetchall()
    c.close()

    for idx, meta_b, payload_b in rows:
        pf, ptail = cv._pb_decode(payload_b)
        assert ptail == b""
        # step_payload Field 5 与 steps.metadata 完全一致，且原始 content 字段未丢
        assert pf[(5, 2)][0] == meta_b
        assert ((19, 2) if idx == 0 else (20, 2)) in pf
        mf, mtail = cv._pb_decode(meta_b)
        assert mtail == b""
        assert (1, 2) in mf and (12, 2) in mf and (20, 2) in mf and (26, 2) in mf
        if idx == 1:
            assert (6, 2) in mf  # 终步带 completed_at，供 last_modified_time 提取
            assert (2, 0) in cv._pb_decode(mf[(1, 2)][0])[0]
            assert (2, 0) in cv._pb_decode(mf[(6, 2)][0])[0]
            assert mf.get((7, 2)) == mf[(6, 2)] and mf.get((8, 2)) == mf[(6, 2)]
            pr, ptail_r = cv._pb_decode(pf[(20, 2)][0])
            assert ptail_r == b""
            assert (3, 2) in pr and (11, 2) in pr and pr.get((12, 0)) == [2]
            assert (1, 2) not in pr


def test_zcode_tasks_index_archived_filtering(isolated, monkeypatch, tmp_path):
    """桌面端 v2 tasks-index.sqlite 的 archived/deleted/cli_deleted 与 --include-archived 隔离行为。"""
    import sqlite3 as _s
    _, vault = isolated
    zdb = tmp_path / "cli_db.sqlite"
    tdb = tmp_path / "tasks-index.sqlite"
    zc = _s.connect(zdb)
    zc.execute("CREATE TABLE session(id TEXT, parent_id TEXT, time_deleted INTEGER, time_archived INTEGER)")
    zc.execute("INSERT INTO session VALUES('z_act', NULL, NULL, NULL)")
    zc.execute("INSERT INTO session VALUES('z_sub', 'z_act', NULL, NULL)")
    zc.execute("INSERT INTO session VALUES('z_v2_arch', NULL, NULL, NULL)")
    zc.execute("INSERT INTO session VALUES('z_v2_del', NULL, NULL, NULL)")
    zc.execute("INSERT INTO session VALUES('z_v2_cdel', NULL, NULL, NULL)")
    zc.commit()
    zc.close()

    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE tasks(task_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO tasks VALUES('z_act', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_v2_arch', 1, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_v2_del', 0, 1, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_v2_cdel', 0, 0, 1)")
    tc.commit()
    tc.close()

    monkeypatch.setattr(cv, "ZCODE_DB", zdb)
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def reg(sref, title, ts):
        env = cv.build_envelope({
            "capture": {"agent": "zcode", "session_ref": sref, "cwd": "E:\\paper-studio", "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": title}],
            "stats": cv.zero_stats(),
        })
        env["created_at"] = ts
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return sid

    s_act = reg("z_act", "活跃会话", "2026-11-10T10:00:00Z")
    s_sub = reg("z_sub", "子代理", "2026-11-10T10:01:00Z")
    s_arch = reg("z_v2_arch", "v2归档", "2026-11-10T10:02:00Z")
    s_del = reg("z_v2_del", "v2删除", "2026-11-10T10:03:00Z")
    s_cdel = reg("z_v2_cdel", "cli删除", "2026-11-10T10:04:00Z")
    conn.commit()
    conn.close()

    assert [r["id"] for r in cv.resolve_top_conversations(vault)] == [s_act]
    assert {r["id"] for r in cv.resolve_top_conversations(vault, include_archived=True)} == {
        s_act, s_arch, s_del, s_cdel
    }
    assert {r["id"] for r in cv.resolve_top_conversations(vault, include_subagents=True)} == {
        s_act, s_sub, s_arch, s_del, s_cdel
    }


def test_sync_antigravity_archived_dry_run_apply_and_lock_guard(isolated, monkeypatch, tmp_path):
    """验证 sync-antigravity 对账 migrations.jsonl + outbox、project 消歧、锁保护与幂等清理。"""
    import sqlite3 as _s
    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    tdb = tmp_path / "tasks-index.sqlite"

    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE tasks(task_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO tasks VALUES('z_act', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_arch_ps', 1, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_arch_pst', 1, 0, 0)")
    tc.commit()
    tc.close()
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def reg(sref, cwd, title):
        env = cv.build_envelope({
            "capture": {"agent": "zcode", "session_ref": sref, "cwd": cwd, "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": title}],
            "stats": cv.zero_stats(),
        })
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return sid, sp

    sid_act, sp_act = reg("z_act", "E:\\paper-studio", "活跃主对话")
    sid_ps, sp_ps = reg("z_arch_ps", "E:\\paper-studio", "归档paper-studio")
    sid_pst, sp_pst = reg("z_arch_pst", "E:\\paper-studio-team", "归档team")
    conn.commit()
    conn.close()

    cid_act = "11111111-1111-4111-8111-111111111111"
    cid_act_dup = "55555555-5555-4555-8555-555555555555"
    cid_ps = "22222222-2222-4222-8222-222222222222"
    cid_pst = "33333333-3333-4333-8333-333333333333"
    cid_nat = "44444444-4444-4444-8444-444444444444"
    for cid in (cid_act, cid_act_dup, cid_ps, cid_nat):  # 故意不给 cid_pst 建 .db，模拟只剩摘要/proto 幽灵
        (conv_dir / f"{cid}.db").write_bytes(b"SQLite format 3\x00")
    (conv_dir / f"{cid_ps}.db-wal").write_bytes(b"wal")

    # 两条通过 migrations.jsonl，另外通过 outbox/*.session-migrate.json 注册重复副本与 cid_pst
    cv._record_migration(vault, conv_dir / f"{cid_act}.db", sid_act)
    cv._record_migration(vault, conv_dir / f"{cid_ps}.db", sid_ps)
    outbox = vault / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"harness-antigravity-{sid_act[:14]}-900.json.session-migrate.json").write_text(
        json.dumps({"target": {"format": "antigravity", "session_id": cid_act_dup, "cwd": "E:\\paper-studio"}}),
        encoding="utf-8",
    )
    (outbox / f"harness-antigravity-{sid_pst[:14]}-1000.json.session-migrate.json").write_text(
        json.dumps({"target": {"format": "antigravity", "session_id": cid_pst, "cwd": "E:\\paper-studio-team"}}),
        encoding="utf-8",
    )

    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, title TEXT)")
    sc.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, archived UNINDEXED, title)")
    for cid, ttl in ((cid_act, "act"), (cid_act_dup, "act"), (cid_ps, "ps"), (cid_pst, "pst"), (cid_nat, "nat")):
        sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid, ttl))
        sc.execute("INSERT INTO conversations_fts VALUES(?, 0, ?)", (cid, ttl))
    sc.commit()
    sc.close()

    # 同步造 agyhub_summaries_proto.pb，验证清理 .db 之外也必须清 proto（否则重启复活幽灵）
    proto_pb = agy_root / "agyhub_summaries_proto.pb"
    proto_pb.write_bytes(b"".join(
        cv._pb_len(1, cv._pb_len(1, c.encode("ascii")) + cv._pb_len(2, b"\n\x03tag"))
        for c in (cid_act, cid_act_dup, cid_ps, cid_pst, cid_nat)
    ))

    # 1. dry-run 不删任何文件与摘要；且 --project paper-studio 精确消歧，不误伤 paper-studio-team
    dry = cv.sync_antigravity_archived(vault, project="paper-studio", apply=False)
    assert [t["cid"] for t in dry["targets"]] == [cid_ps]
    assert (conv_dir / f"{cid_ps}.db").exists()

    # 2. 摘要库被占用时，--apply 在删除 .db / proto 之前即中止并抛 BadMigrateTarget
    lock_conn = _s.connect(sum_db, timeout=0.1)
    lock_conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(cv.BadMigrateTarget):
            cv.sync_antigravity_archived(vault, project="paper-studio", apply=True)
        assert (conv_dir / f"{cid_ps}.db").exists()
        assert cid_ps.encode() in proto_pb.read_bytes()
    finally:
        lock_conn.rollback()
        lock_conn.close()

    # 3. 释放锁后全量执行 --apply --dedup，清理已归档(.db+幽灵摘要+proto)与同快照旧重复副本
    cv.main(["--vault", str(vault), "sync-antigravity", "--dedup", "--apply"])
    assert not (conv_dir / f"{cid_ps}.db").exists()
    assert not (conv_dir / f"{cid_ps}.db-wal").exists()
    assert not (conv_dir / f"{cid_act_dup}.db").exists()
    assert (conv_dir / f"{cid_act}.db").exists() and (conv_dir / f"{cid_nat}.db").exists()
    assert sp_act.exists() and sp_ps.exists() and sp_pst.exists()  # Vault 快照完全保留

    sc = _s.connect(sum_db)
    assert {r[0] for r in sc.execute("SELECT conversation_id FROM conversation_summaries")} == {cid_act, cid_nat}
    assert {r[0] for r in sc.execute("SELECT conversation_id FROM conversations_fts")} == {cid_act, cid_nat}
    sc.close()

    ptop, _ = cv._pb_decode(proto_pb.read_bytes())
    kept_pb_cids = {cv._pb_decode(e)[0][(1, 2)][0].decode() for e in ptop.get((1, 2), [])}
    assert kept_pb_cids == {cid_act, cid_nat}

    # 4. 幂等：再跑一次无残留可删
    again = cv.sync_antigravity_archived(vault, apply=True, dedup=True)
    assert again["targets"] == [] and again["removed_dbs"] == 0 and again["cleaned_proto"] == 0

    # 5. 默认 --vault="vault" 且不在仓库目录运行非 init 命令时自动回落到脚本同级仓库 vault
    from types import SimpleNamespace
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    fake_file = tmp_path / "repo" / "tools" / "cv.py"
    fake_file.parent.mkdir(parents=True)
    fake_repo_vault = tmp_path / "repo" / "vault"
    fake_repo_vault.mkdir(parents=True)
    (fake_repo_vault / "vault.db").write_bytes(b"")
    monkeypatch.setattr(cv, "__file__", str(fake_file))
    assert cv.vault_dir(SimpleNamespace(vault="vault", cmd="sync-antigravity")) == fake_repo_vault
    assert cv.vault_dir(SimpleNamespace(vault="vault", cmd="init")) == Path("vault")
    monkeypatch.chdir(tmp_path)  # cwd 下已有 tmp_path/vault/vault.db 时不回落
    assert cv.vault_dir(SimpleNamespace(vault="vault", cmd="sync-antigravity")) == Path("vault")


def test_strict_agent_filter_import_and_antigravity_only_agent(isolated, monkeypatch):
    """验证严格按 --agent 过滤列表/批量导入/HTTP API 与 sync-antigravity --only-agent 清理非目标来源。"""
    import sqlite3 as _s
    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    proto_pb = agy_root / "agyhub_summaries_proto.pb"

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def add_snap(ag, sref, cwd, title, ts):
        env = cv.build_envelope({
            "capture": {"agent": ag, "session_ref": sref, "cwd": cwd, "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": title}],
            "stats": cv.zero_stats(),
        })
        env["created_at"] = ts
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return sid

    s_zc = add_snap("zcode", "z_1", "D:\\goai", "ZCode对话", "2026-11-10T10:00:00Z")
    s_cx = add_snap("codex", "c_1", "D:\\goai", "Codex对话", "2026-11-11T10:00:00Z")
    s_cc = add_snap("claude-code", "cl_1", "D:\\goai", "Claude对话", "2026-11-12T10:00:00Z")
    conn.commit()
    conn.close()

    # 1. CLI --agent zcode 严格排除 codex / claude-code；单条错 agent 直接拒绝
    assert [it["id"] for it in cv._resolve_project_items(vault, "goai", agent="zcode")] == [s_zc]
    assert {it["id"] for it in cv._resolve_project_items(vault, "goai", agent="zcode,claude-code")} == {s_zc, s_cc}

    cv.main(["--vault", str(vault), "import", "--project", "goai", "--agent", "zcode", "--target", "markdown"])
    assert (vault / "outbox" / f"brief-{s_zc}.md").exists()
    assert not (vault / "outbox" / f"brief-{s_cx}.md").exists()
    assert not (vault / "outbox" / f"brief-{s_cc}.md").exists()

    with pytest.raises(SystemExit):
        cv.main(["--vault", str(vault), "import", s_cx, "--agent", "zcode", "--target", "markdown"])

    # 2. HTTP /projects?agent=zcode 与 /migrate_project {agent:"zcode"}
    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, pdata = _req(port, "GET", "/projects?project=goai&agent=zcode")
        assert code == 200 and pdata["total_conversations"] == 1
        assert pdata["projects"][0]["conversations"][0]["id"] == s_zc

        out_dir_before = set((vault / "outbox").iterdir())
        for f in out_dir_before:
            f.unlink()
        code, mres = _req(
            port, "POST", "/migrate_project",
            body=json.dumps({"project": "D:\\goai", "target": "markdown", "agent": "zcode"}),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )
        assert code == 200 and mres["count"] == 1
        assert s_zc in mres["files"][0]
    finally:
        srv.shutdown()
        srv.server_close()

    # 3. sync-antigravity --only-agent zcode 清理已迁入的 codex/claude-code，不动 zcode 与原生会话
    cid_zc = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    cid_cx = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    cid_cc = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    cid_nat = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    for cid in (cid_zc, cid_cx, cid_cc, cid_nat):
        (conv_dir / f"{cid}.db").write_bytes(b"SQLite format 3\x00")
    cv._record_migration(vault, conv_dir / f"{cid_zc}.db", s_zc)
    cv._record_migration(vault, conv_dir / f"{cid_cx}.db", s_cx)
    cv._record_migration(vault, conv_dir / f"{cid_cc}.db", s_cc)

    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, title TEXT)")
    sc.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, archived UNINDEXED, title)")
    for cid in (cid_zc, cid_cx, cid_cc, cid_nat):
        sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid, cid))
        sc.execute("INSERT INTO conversations_fts VALUES(?, 0, ?)", (cid, cid))
    sc.commit()
    sc.close()

    proto_pb.write_bytes(b"".join(
        cv._pb_len(1, cv._pb_len(1, c.encode("ascii")) + cv._pb_len(2, b"\n\x01x"))
        for c in (cid_zc, cid_cx, cid_cc, cid_nat)
    ))

    cv.main(["--vault", str(vault), "sync-antigravity", "--only-agent", "zcode", "--apply"])
    assert (conv_dir / f"{cid_zc}.db").exists()
    assert (conv_dir / f"{cid_nat}.db").exists()
    assert not (conv_dir / f"{cid_cx}.db").exists()
    assert not (conv_dir / f"{cid_cc}.db").exists()

    sc = _s.connect(sum_db)
    assert {r[0] for r in sc.execute("SELECT conversation_id FROM conversation_summaries")} == {cid_zc, cid_nat}
    sc.close()
    ptop, _ = cv._pb_decode(proto_pb.read_bytes())
    assert {cv._pb_decode(e)[0][(1, 2)][0].decode() for e in ptop.get((1, 2), [])} == {cid_zc, cid_nat}



# ---------------- 确定性导出 id · antigravity 索引行 · zcode 写回 (2026-11-16) ----------------

def test_deterministic_session_id_stable(tmp_path):
    """同一源会话恒定派生 uuid4 格式 id: 重导出不再产生副本 (uuid5 → 版本位改写)。"""
    env = make_env(tmp_path)
    a = cv._deterministic_session_id(env)
    assert a == cv._deterministic_session_id(env)
    import uuid as _u
    u = _u.UUID(a)
    assert u.version == 4 and str(u) == a          # smigrate/_require_uuid4 强校验 uuid4
    env2 = make_env(tmp_path)
    env2["capture"]["session_ref"] = (env["capture"].get("session_ref") or "x") + "-other"
    assert cv._deterministic_session_id(env2) != a


def test_do_import_codex_claude_deterministic(isolated):
    """重复 do_import 同一快照: codex/claude 目标 session id 与文件名恒定 (原位更新语义)。"""
    _, vault = isolated
    sid = _register_env(vault, {"agent": "codex", "source_format": "codex-rollout",
                                "cwd": "D:\\proj", "session_ref": "sess_det-1"},
                        {"tokens_in": 1, "total_tokens": 2}, "2026-11-16T00:00:00")
    r1 = cv.do_import(vault, sid, "codex", apply=False)
    r2 = cv.do_import(vault, sid, "codex", apply=False)
    assert r1["new_sid"] == r2["new_sid"]
    assert Path(r1["dest"]).name == Path(r2["dest"]).name      # 文件名时间戳也确定
    c1 = cv.do_import(vault, sid, "claude", apply=False)
    c2 = cv.do_import(vault, sid, "claude", apply=False)
    assert c1["new_sid"] == c2["new_sid"]
    assert Path(c1["dest"]).name.endswith(c1["new_sid"] + ".jsonl")


def test_antigravity_register_summaries(isolated, tmp_path, monkeypatch):
    """apply 导入必须写 conversation_summaries 索引行 (只落 .db = 永不可见);
    标题取 originator (zcode 短标题), 行含 workspace/project_id/raw_summary。"""
    home, vault = isolated
    agy_home = tmp_path / "agyhome"
    (agy_home / ".gemini" / "antigravity" / "conversations").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", agy_home)
    sum_db = agy_home / ".gemini" / "antigravity" / "conversation_summaries.db"
    import sqlite3 as _s
    conn = _s.connect(sum_db)
    conn.executescript("""
      CREATE TABLE conversation_summaries (
        conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER,
        last_modified_time TEXT, workspace_uris TEXT, status TEXT, source TEXT,
        project_id TEXT, agent_name TEXT, parent_conversation_id TEXT,
        nesting_depth INTEGER, battle_id TEXT, winning_conversation_id TEXT,
        not_fully_idle INTEGER, killed INTEGER, last_user_input_time TEXT,
        last_user_input_step_index INTEGER, app_data_dir TEXT,
        raw_summary BLOB, group_id TEXT);
    """)
    conn.commit()
    conn.close()
    # 伪已安装库: steps/trajectory_meta/blob
    tdb = tmp_path / "convs" / "cas-1.db"
    tdb.parent.mkdir(exist_ok=True)
    conn = _s.connect(tdb)
    conn.executescript("""
      CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, metadata BLOB,
                          step_payload BLOB);
      INSERT INTO steps VALUES (0, 14, 2, NULL, NULL);
      INSERT INTO steps VALUES (1, 15, 2, NULL, NULL);
      CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER,
                                    source INTEGER);
      INSERT INTO trajectory_meta VALUES ('tr-1', 'cas-1', 4, 1);
      CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);
    """)
    conn.commit()
    conn.close()
    env = cv.build_envelope({"capture": {"agent": "zcode", "source_format": "zcode-sqlite",
                                         "cwd": "D:\\GBM-DRACI", "originator": "短标题回归",
                                         "session_ref": "sess_x"},
                             "conversation": [{"type": "text", "role": "user",
                                               "content": "首问内容", "ts": "2026-11-16T01:00:00Z"}],
                             "stats": cv.zero_stats()})
    assert cv._antigravity_register_summaries(tdb, env, "cas-1")
    conn = _s.connect(sum_db)
    row = conn.execute("SELECT title, preview, project_id, workspace_uris, step_count, raw_summary"
                       " FROM conversation_summaries WHERE conversation_id='cas-1'").fetchone()
    conn.close()
    assert row[0] == "短标题回归"                       # originator 短标题优先生效
    assert row[1] == "首问内容"
    assert row[3] == '["file:///d%3A/GBM-DRACI"]'
    assert row[4] == 2
    fields, _ = cv._pb_decode(bytes(row[5]))            # raw_summary 是合法 protobuf 且含标题
    assert fields.get((1, 2))[0].decode() == "短标题回归"


def _mk_zcode_db(path):
    """按真库列建 mini zcode 主库 (session/message/part)。"""
    import sqlite3 as _s
    conn = _s.connect(path)
    conn.executescript("""
      CREATE TABLE session (id TEXT, project_id TEXT NOT NULL, workspace_id TEXT,
        parent_id TEXT, slug TEXT NOT NULL, directory TEXT NOT NULL, path TEXT NOT NULL,
        title TEXT NOT NULL, version TEXT NOT NULL, share_url TEXT,
        summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
        summary_diffs TEXT, revert TEXT, permission TEXT, time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL, time_compacting INTEGER, time_archived INTEGER,
        task_type TEXT NOT NULL DEFAULT 'interactive',
        title_source TEXT NOT NULL DEFAULT 'first_input',
        title_message_id TEXT, time_title_updated INTEGER,
        trace_id TEXT, time_deleted INTEGER, last_outcome TEXT);
      CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
        time_updated INTEGER, data TEXT, sequence INTEGER);
      CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER,
        time_updated INTEGER, data TEXT, sequence INTEGER);
    """)
    ms = 1760000000000
    conn.execute(
        "INSERT INTO session (id, project_id, slug, directory, path, title, version,"
        " time_created, time_updated, time_deleted) VALUES"
        "('sess_src', 'proj_d-goai', 'sess_src', 'D:\\goai', 'D:\\goai', '源会话', '0.17.1',"
        " ?, ?, NULL)", (ms, ms + 1000))
    conn.execute("INSERT INTO message VALUES ('msg_src_1', 'sess_src', ?, ?, ?, 0)",
                 (ms, ms, json.dumps({"role": "user", "time": {"created": ms}})))
    conn.execute("INSERT INTO message VALUES ('msg_src_2', 'sess_src', ?, ?, ?, 1)",
                 (ms + 10, ms + 10, json.dumps({"role": "assistant",
                                                "time": {"created": ms + 10, "completed": ms + 10},
                                                "modelId": "GLM-5.3-Flash"})))
    conn.execute("INSERT INTO part VALUES ('p1', 'msg_src_1', 'sess_src', ?, ?, ?, 0)",
                 (ms, ms, json.dumps({"type": "text", "text": "你好 zcode 回写"})))
    conn.execute("INSERT INTO part VALUES ('p2', 'msg_src_2', 'sess_src', ?, ?, ?, 0)",
                 (ms + 10, ms + 10, json.dumps({"type": "tool", "callID": "call_1",
                                                "tool": "Bash",
                                                "state": {"status": "error",
                                                          "input": {"command": "ls"},
                                                          "error": "boom"}})))
    conn.commit()
    conn.close()
    return path


def test_zcode_writeback_roundtrip(isolated, tmp_path):
    """写回 ZCode 往返: export → blocks_to_zcode → --apply (备份+事务) → 再 export 块一致;
    is_error 贯通 (state.status=error → block.is_error → 回写 state.status=error)。"""
    _, vault = isolated
    src_db = tmp_path / "zsrc.sqlite"
    _mk_zcode_db(src_db)
    conn = cv.sqlite3.connect(src_db)
    conn.row_factory = cv.sqlite3.Row
    srow = conn.execute("SELECT * FROM session WHERE id='sess_src'").fetchone()
    exported = cv.export_zcode_session(conn, srow)
    conn.close()
    assert exported["stats"]["blocks_total"] == 3
    tr = [b for b in exported["conversation"] if b["type"] == "tool_result"]
    assert tr and tr[0]["is_error"] is True                      # 采集侧 is_error 贯通
    env = cv.build_envelope(exported)
    dst_db = tmp_path / "zdst.sqlite"
    _mk_zcode_db(dst_db)
    r = cv.do_import_zcode(vault, env, "cv_tdst", apply=True,
                           session_id=cv._deterministic_session_id(env), db_path=dst_db)
    assert r["applied"]
    backups = list((vault / "backups").glob("zcode-import-backup-*.sqlite"))
    assert len(backups) == 1                                     # apply 先备份
    conn = cv.sqlite3.connect(dst_db)
    conn.row_factory = cv.sqlite3.Row
    srow = conn.execute("SELECT * FROM session WHERE id=?", (r["new_sid"],)).fetchone()
    rt = cv.export_zcode_session(conn, srow)
    conn.close()
    rt_blocks = rt["conversation"]
    assert [b["type"] for b in rt_blocks] == [b["type"] for b in exported["conversation"]]
    tr = [b for b in rt_blocks if b["type"] == "tool_result"]
    assert tr and tr[0]["is_error"] is True                      # 回写后再读仍为 error
    assert any("你好 zcode 回写" in b["content"] for b in rt_blocks if b["type"] == "text")


def test_zcode_writeback_update_in_place(isolated, tmp_path):
    """同 session_id 重导 = 原位更新 (messages/parts 重写), 不产生第二会话。"""
    _, vault = isolated
    src_db = tmp_path / "zsrc.sqlite"
    _mk_zcode_db(src_db)
    conn = cv.sqlite3.connect(src_db)
    conn.row_factory = cv.sqlite3.Row
    srow = conn.execute("SELECT * FROM session WHERE id='sess_src'").fetchone()
    env = cv.build_envelope(cv.export_zcode_session(conn, srow))
    conn.close()
    dst = tmp_path / "zdst.sqlite"
    _mk_zcode_db(dst)
    sess_id = "sess_cvimp_fixed"
    cv.do_import_zcode(vault, env, "cv_t1", apply=True, session_id=sess_id, db_path=dst)
    cv.do_import_zcode(vault, env, "cv_t2", apply=True, session_id=sess_id, db_path=dst)
    conn = cv.sqlite3.connect(dst)
    n_sess = conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (sess_id,)).fetchone()[0]
    n_msg = conn.execute("SELECT COUNT(*) FROM message WHERE session_id=?",
                         (sess_id,)).fetchone()[0]
    conn.close()
    assert n_sess == 1 and n_msg == 2


def test_sync_antigravity_guard_paths(isolated, tmp_path, monkeypatch):
    """回灌守卫: 无 smigrate → (0,0,0); 坏 .db 单文件失败不断链。"""
    _, vault = isolated
    monkeypatch.setattr(cv, "find_smigrate", lambda: None)
    assert cv._sync_antigravity_dbs(cv.db(vault), vault) == (0, 0, 0)
    exe = __import__("shutil").which("smigrate")
    if not exe:
        return  # CI 无 smigrate: 守卫路径已覆盖
    monkeypatch.setattr(cv, "find_smigrate", lambda: exe)
    monkeypatch.setattr(cv, "ANTIGRAVITY_CONV_DIR", tmp_path / "convs")
    (tmp_path / "convs").mkdir(exist_ok=True)
    (tmp_path / "convs" / "bad.db").write_bytes(b"not a sqlite file")
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)
    assert cv._sync_antigravity_dbs(conn, vault) == (0, 0, 0)    # 坏文件被跳过不炸


def test_antigravity_proto_upsert(isolated, tmp_path, monkeypatch):
    """proto.pb 是 app 真源 (启动按它重建索引表): register 必须同步 upsert 条目
    {f12: cascade, f22: raw_summary}; 幂等 (重注册不产生重复条目)。"""
    agy_home = tmp_path / "agyhome2"
    (agy_home / ".gemini" / "antigravity").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", agy_home)
    pb = agy_home / ".gemini" / "antigravity" / "agyhub_summaries_proto.pb"
    # app 原生条目格式: f1=cid, f2=raw_summary (字段1/字段2, 非线型号)
    pb.write_bytes(cv._pb_len(1, cv._pb_len(1, b"native-1") + cv._pb_len(2, b"raw-native")))
    assert cv._antigravity_proto_upsert("cas-9", b"raw-9")
    top, _ = cv._pb_decode(pb.read_bytes())
    entries = [cv._pb_decode(e)[0] for e in top.get((1, 2), [])]
    assert len(entries) == 2                                       # 原生保留 + 新增
    assert entries[0].get((1, 2))[0] == b"native-1"
    assert entries[1].get((1, 2))[0] == b"cas-9"
    assert entries[1].get((2, 2))[0] == b"raw-9"
    cv._antigravity_proto_upsert("cas-9", b"raw-9-v2")             # 同 cid 重写 = 替换
    top, _ = cv._pb_decode(pb.read_bytes())
    entries = [cv._pb_decode(e)[0] for e in top.get((1, 2), [])]
    assert len(entries) == 2 and entries[1].get((2, 2))[0] == b"raw-9-v2"


def test_sync_antigravity_dedup_by_session_ref_and_self_loop(isolated, monkeypatch, tmp_path):
    """验证 --dedup 按 session_ref 对不同 sid 演化快照去重（保留确定性 UUID），并清理 antigravity 自环迁入库。"""
    import sqlite3 as _s
    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    proto_pb = agy_root / "agyhub_summaries_proto.pb"

    tdb = tmp_path / "tasks-index.sqlite"
    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE tasks(task_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO tasks VALUES('z_evo', 0, 0, 0)")
    tc.commit()
    tc.close()
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def reg(ag, sref, text):
        env = cv.build_envelope({
            "capture": {"agent": ag, "session_ref": sref, "cwd": "D:\\goai", "originator": text},
            "conversation": [{"type": "text", "role": "user", "content": text}],
            "stats": cv.zero_stats(),
        })
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return env, sid

    env_old, sid_old = reg("zcode", "z_evo", "初始提问")
    env_new, sid_new = reg("zcode", "z_evo", "演化追问")
    _, sid_self = reg("antigravity", "native_self_1", "自环回流")
    conn.commit()
    conn.close()
    assert sid_old != sid_new

    cid_det = cv._deterministic_session_id(env_new)
    cid_rand = "99999999-9999-4999-8999-999999999999"
    cid_self = "88888888-8888-4888-8888-888888888888"
    cid_nat = "77777777-7777-4777-8777-777777777777"
    for cid in (cid_det, cid_rand, cid_self, cid_nat):
        dc = _s.connect(conv_dir / f"{cid}.db")
        dc.execute("CREATE TABLE steps(step_index INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)")
        dc.execute("INSERT INTO steps VALUES(0, 14, X'')")
        if cid == cid_det:
            dc.execute("INSERT INTO steps VALUES(1, 15, X'')")
        dc.commit()
        dc.close()

    cv._record_migration(vault, conv_dir / f"{cid_rand}.db", sid_old)
    cv._record_migration(vault, conv_dir / f"{cid_det}.db", sid_new)
    cv._record_migration(vault, conv_dir / f"{cid_self}.db", sid_self)

    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, title TEXT)")
    sc.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, archived UNINDEXED, title)")
    for cid in (cid_det, cid_rand, cid_self, cid_nat):
        sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid, cid))
        sc.execute("INSERT INTO conversations_fts VALUES(?, 0, ?)", (cid, cid))
    sc.commit()
    sc.close()

    proto_pb.write_bytes(b"".join(
        cv._pb_len(1, cv._pb_len(1, c.encode("ascii")) + cv._pb_len(2, b"\n\x03tag"))
        for c in (cid_det, cid_rand, cid_self, cid_nat)
    ))

    res = cv.sync_antigravity_archived(vault, dedup=True, apply=True)
    reasons = {t["cid"]: t["reason"] for t in res["targets"]}
    assert reasons[cid_self] == "self_loop"
    assert reasons[cid_rand] in ("duplicate", "dup")
    assert cid_det not in reasons and cid_nat not in reasons
    assert (conv_dir / f"{cid_det}.db").exists() and (conv_dir / f"{cid_nat}.db").exists()
    assert not (conv_dir / f"{cid_rand}.db").exists() and not (conv_dir / f"{cid_self}.db").exists()


def test_antigravity_batch_strict_agent_and_recover_filters(isolated, monkeypatch, tmp_path, capsys):
    """验证 antigravity_batch 默认只导未归档 zcode，且 --recover 也绝不复活归档、codex 或 antigravity 自环。"""
    import sqlite3 as _s
    import antigravity_batch as ab

    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    monkeypatch.setattr(ab, "HOME", home)
    monkeypatch.setattr(ab, "CONV_DIR", conv_dir)
    monkeypatch.setattr(ab, "SUMMARY_DB", sum_db)

    tdb = tmp_path / "tasks-index.sqlite"
    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE tasks(task_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO tasks VALUES('z_act_missing', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_act_evo', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('z_arch', 1, 0, 0)")
    tc.commit()
    tc.close()
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def reg(ag, sref, title, ts):
        env = cv.build_envelope({
            "capture": {"agent": ag, "session_ref": sref, "cwd": "D:\\goai", "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": title}],
            "stats": cv.zero_stats(),
        })
        env["created_at"] = ts
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return env, sid

    env_miss, sid_miss = reg("zcode", "z_act_missing", "ACTIVE_KEEP_MISSING", "2026-11-10T10:00:00Z")
    env_evo_old, sid_evo_old = reg("zcode", "z_act_evo", "EVO_OLD", "2026-11-10T11:00:00Z")
    env_evo_new, sid_evo_new = reg("zcode", "z_act_evo", "EVO_NEW_ALREADY_PRESENT", "2026-11-10T12:00:00Z")
    env_arch, sid_arch = reg("zcode", "z_arch", "ARCHIVED_SKIP", "2026-11-10T13:00:00Z")
    env_cx, sid_cx = reg("codex", "c_1", "CODEX_SKIP", "2026-11-10T14:00:00Z")
    env_ag, sid_ag = reg("antigravity", "a_1", "SELF_LOOP_SKIP", "2026-11-10T15:00:00Z")
    conn.commit()
    conn.close()

    # z_act_evo 旧版已在摘要库 + .db 存在；其余 4 条曾迁入记录在 migrations.jsonl 但目标端 .db 已删
    cid_evo_old = "12121212-1212-4121-8121-121212121212"
    (conv_dir / f"{cid_evo_old}.db").write_bytes(b"SQLite format 3\x00")
    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, title TEXT)")
    sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid_evo_old, "EVO_OLD"))
    sc.commit()
    sc.close()

    cv._record_migration(vault, conv_dir / f"{cid_evo_old}.db", sid_evo_old)
    cv._record_migration(vault, conv_dir / f"{cv._deterministic_session_id(env_miss)}.db", sid_miss)
    cv._record_migration(vault, conv_dir / f"{cv._deterministic_session_id(env_arch)}.db", sid_arch)
    cv._record_migration(vault, conv_dir / f"{cv._deterministic_session_id(env_cx)}.db", sid_cx)
    cv._record_migration(vault, conv_dir / f"{cv._deterministic_session_id(env_ag)}.db", sid_ag)

    capsys.readouterr()
    # 1. --recover --dry-run 默认只认未归档 zcode：仅 sid_miss 缺失待恢复，z_arch / codex / antigravity 均跳过
    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--recover", "--dry-run"])
    ab.main()
    out_r1 = capsys.readouterr().out
    assert "待导入 1 条" in out_r1

    # 2. 补齐 sid_miss 后再跑：z_arch / codex / antigravity 仍在 migrations.jsonl 中，但绝不复活
    cid_miss = cv._deterministic_session_id(env_miss)
    (conv_dir / f"{cid_miss}.db").write_bytes(b"SQLite format 3\x00")
    sc = _s.connect(sum_db)
    sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid_miss, "ACTIVE_KEEP_MISSING"))
    sc.commit()
    sc.close()

    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--recover", "--dry-run"])
    ab.main()
    assert "待导入 0 条" in capsys.readouterr().out

    # 3. 常规与 --recover 加 --agent all：仅释放 codex (1 条)，仍跳过 z_arch / antigravity / 已存在 z_act_evo 新快照
    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--dry-run"])
    ab.main()
    assert "待导入 0 条" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--agent", "all", "--dry-run"])
    ab.main()
    assert "待导入 1 条" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--recover", "--agent", "all", "--dry-run"])
    ab.main()
    assert "待导入 1 条" in capsys.readouterr().out

    # 4. 补齐 codex 并把 sid_evo_new 也写入 migrations.jsonl，--recover --agent all 仍必须为 0
    cid_cx = cv._deterministic_session_id(env_cx)
    (conv_dir / f"{cid_cx}.db").write_bytes(b"SQLite format 3\x00")
    sc = _s.connect(sum_db)
    sc.execute("INSERT INTO conversation_summaries VALUES(?, ?)", (cid_cx, "CODEX_SKIP"))
    sc.commit()
    sc.close()
    cv._record_migration(vault, conv_dir / f"{cv._deterministic_session_id(env_evo_new)}.db", sid_evo_new)

    monkeypatch.setattr(sys, "argv", ["antigravity_batch.py", "--vault", str(vault), "--recover", "--agent", "all", "--dry-run"])
    ab.main()
    assert "待导入 0 条" in capsys.readouterr().out


def test_sync_antigravity_deleted_projects_and_empty_project_json_cleanup(isolated, monkeypatch, tmp_path):
    """验证 ZCode projects.deleted=1 联动清理，及空壳 ~/.gemini/config/projects/*.json 清理与安全守卫。"""
    import sqlite3 as _s
    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    proto_pb = agy_root / "agyhub_summaries_proto.pb"
    pdir = home / ".gemini" / "config" / "projects"
    pdir.mkdir(parents=True, exist_ok=True)

    tdb = tmp_path / "tasks-index.sqlite"
    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE projects(project_id TEXT PRIMARY KEY, name TEXT, deleted INTEGER)")
    tc.execute("CREATE TABLE project_roots(project_id TEXT, root_path TEXT)")
    tc.execute("CREATE TABLE tasks(task_id TEXT, workspace_path TEXT, project_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO projects VALUES('p-act', 'goai', 0)")
    tc.execute("INSERT INTO projects VALUES('p-del', 'changecheck', 1)")
    tc.execute("INSERT INTO projects VALUES('p-readd-old', 'goai', 1)")
    tc.execute("INSERT INTO project_roots VALUES('p-act', 'D:\\goai')")
    tc.execute("INSERT INTO project_roots VALUES('p-del', 'E:\\changecheck')")
    tc.execute("INSERT INTO project_roots VALUES('p-readd-old', 'D:\\goai')")
    # t_del 与 t_del_legacy 的 task.archived 均为 0，仅因所属 ZCode 项目已删除而应被清理
    tc.execute("INSERT INTO tasks VALUES('t_act', 'D:\\goai', 'p-act', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('t_readd_legacy', 'D:\\goai', NULL, 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('t_del', 'E:\\changecheck', 'p-del', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('t_del_legacy', 'E:\\changecheck', NULL, 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('t_huawei', 'D:\\华为杯', NULL, 1, 0, 0)")
    tc.commit()
    tc.close()
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)

    def reg(sref, cwd, title):
        env = cv.build_envelope({
            "capture": {"agent": "zcode", "session_ref": sref, "cwd": cwd, "originator": title},
            "conversation": [{"type": "text", "role": "user", "content": title}],
            "stats": cv.zero_stats(),
        })
        sid = cv.snapshot_id(env)
        sp = vault / "snapshots" / f"{sid}.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, sp)
        return sid

    s_act = reg("t_act", "D:\\goai", "活跃goai")
    s_readd = reg("t_readd_legacy", "D:\\goai", "重添后旧任务保留")
    s_del = reg("t_del", "E:\\changecheck", "已删项目changecheck")
    s_del_leg = reg("t_del_legacy", "E:\\changecheck", "已删项目无pid旧任务")
    s_hw = reg("t_huawei", "D:\\华为杯", "华为杯已清对话")
    conn.commit()
    conn.close()

    # resolve_top_conversations 默认排除已删项目下任务
    vis = {it["id"] for it in cv.resolve_top_conversations(vault)}
    assert vis == {s_act, s_readd}

    cid_act = "11111111-0000-4000-8000-111111111111"
    cid_del = "22222222-0000-4000-8000-222222222222"
    cid_del_leg = "33333333-0000-4000-8000-333333333333"
    cid_hw = "44444444-0000-4000-8000-444444444444"
    for c_id in (cid_act, cid_del, cid_del_leg):
        (conv_dir / f"{c_id}.db").write_bytes(b"SQLite format 3\x00")
    cv._record_migration(vault, conv_dir / f"{cid_act}.db", s_act)
    cv._record_migration(vault, conv_dir / f"{cid_del}.db", s_del)
    cv._record_migration(vault, conv_dir / f"{cid_del_leg}.db", s_del_leg)
    cv._record_migration(vault, conv_dir / f"{cid_hw}.db", s_hw)  # 华为杯 .db 早已删除，只剩空项目壳子

    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, project_id TEXT, workspace_uris TEXT)")
    sc.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, archived UNINDEXED, title)")
    sc.execute("INSERT INTO conversation_summaries VALUES(?, 'pj-goai', '[\"file:///d%3A/goai\"]')", (cid_act,))
    sc.execute("INSERT INTO conversation_summaries VALUES(?, 'pj-cc', '[\"file:///e%3A/changecheck\"]')", (cid_del,))
    sc.execute("INSERT INTO conversation_summaries VALUES(?, 'pj-cc', '[\"file:///e%3A/changecheck\"]')", (cid_del_leg,))
    for cid in (cid_act, cid_del, cid_del_leg):
        sc.execute("INSERT INTO conversations_fts VALUES(?, 0, ?)", (cid, cid))
    sc.commit()
    sc.close()
    proto_pb.write_bytes(b"".join(
        cv._pb_len(1, cv._pb_len(1, c.encode("ascii")) + cv._pb_len(2, b"\n\x01x"))
        for c in (cid_act, cid_del, cid_del_leg)
    ))

    def mk_pj(pid, name, res=None, **extra):
        data = {"id": pid, "name": name, "projectResources": {"resources": res or []}, **extra}
        (pdir / f"{pid}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    mk_pj("pj-goai", "goai", [{"folderUri": "file:///d%3A/goai"}])
    mk_pj("pj-cc", "changecheck", [{"folderUri": "file:///e%3A/changecheck"}])
    mk_pj("pj-hw", "华为杯", [{"folderUri": "file:///d%3A/%E5%8D%8E%E4%B8%BA%E6%9D%AF"}])
    mk_pj("pj-native-empty", "manual-open", [{"folderUri": "file:///e%3A/manual-open"}])
    mk_pj("pj-sidecar", "swarm/swarm_board", [], sidecar=True)

    # 1. --project changecheck 仅清 changecheck 对话与空壳，不误碰华为杯/goai/原生空壳/sidecar
    dry = cv.sync_antigravity_archived(vault, project="changecheck", apply=False)
    assert {t["cid"] for t in dry["targets"]} == {cid_del, cid_del_leg}
    assert {e["id"] for e in dry["empty_projects"]} == {"pj-cc"}

    res1 = cv.sync_antigravity_archived(vault, project="changecheck", apply=True)
    assert res1["removed_dbs"] == 2 and res1["removed_projects"] == 1
    assert not (pdir / "pj-cc.json").exists()
    assert (pdir / "pj-hw.json").exists() and (pdir / "pj-goai.json").exists()
    assert (pdir / "pj-native-empty.json").exists() and (pdir / "pj-sidecar.json").exists()

    # 2. 即使此时已无残留对话 (targets==[])，摘要库被锁时仍中止且不删 pj-hw.json
    lock = _s.connect(sum_db, timeout=0.1)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(cv.BadMigrateTarget):
            cv.sync_antigravity_archived(vault, apply=True)
        assert (pdir / "pj-hw.json").exists()
    finally:
        lock.rollback()
        lock.close()

    # 3. 释放锁后全量执行：targets==[] 时仍清掉早先留下的空壳 华为杯，同时保留 goai / native / sidecar
    res2 = cv.sync_antigravity_archived(vault, apply=True)
    assert res2["targets"] == [] and res2["removed_projects"] == 1
    assert not (pdir / "pj-hw.json").exists()
    assert (pdir / "pj-goai.json").exists()
    assert (pdir / "pj-native-empty.json").exists()
    assert (pdir / "pj-sidecar.json").exists()


def test_http_sync_antigravity_and_zcode_migrate_project(isolated, monkeypatch, tmp_path):
    """验证 /sync 刷新热索引、/migrate_project 支持 zcode apply、/antigravity/sync 与 /antigravity/batch 完整链路。"""
    import sqlite3 as _s
    import threading
    home, vault = isolated
    conv_dir = home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    agy_root = conv_dir.parent
    sum_db = agy_root / "conversation_summaries.db"
    pdir = home / ".gemini" / "config" / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "p1.json").write_text(json.dumps({
        "id": "p1", "name": "goai",
        "projectResources": {"resources": [{"folderUri": "file:///d%3A/goai"}]},
    }), encoding="utf-8")

    zdb = _mk_zcode_db(tmp_path / "zcode.sqlite")
    monkeypatch.setattr(cv, "ZCODE_DB", zdb)

    tdb = tmp_path / "tasks-index.sqlite"
    tc = _s.connect(tdb)
    tc.execute("CREATE TABLE projects(project_id TEXT PRIMARY KEY, name TEXT, deleted INTEGER)")
    tc.execute("CREATE TABLE tasks(task_id TEXT, project_id TEXT, archived INTEGER, deleted INTEGER, cli_deleted INTEGER)")
    tc.execute("INSERT INTO projects VALUES('p-act', 'goai', 0)")
    tc.execute("INSERT INTO tasks VALUES('sess_src', 'p-act', 0, 0, 0)")
    tc.execute("INSERT INTO tasks VALUES('sess_arch', 'p-act', 1, 0, 0)")
    tc.commit()
    tc.close()
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", tdb)

    # 先写一个已归档 zcode 快照与其残留在 Antigravity 的 .db + summary
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    env_arch = cv.build_envelope({
        "capture": {"agent": "zcode", "session_ref": "sess_arch", "cwd": "D:\\goai", "originator": "归档旧对话"},
        "conversation": [{"type": "text", "role": "user", "content": "arch old"}],
        "stats": cv.zero_stats(),
    })
    sid_arch = cv.snapshot_id(env_arch)
    sp_arch = vault / "snapshots" / f"{sid_arch}.json"
    sp_arch.parent.mkdir(parents=True, exist_ok=True)
    sp_arch.write_text(json.dumps(env_arch, ensure_ascii=False), encoding="utf-8")
    cv.register_snapshot(conn, env_arch, sid_arch, sp_arch)
    conn.commit()
    conn.close()

    cid_arch = "aaaaaaaa-0000-4000-8000-000000000001"
    (conv_dir / f"{cid_arch}.db").write_bytes(b"SQLite format 3\x00")
    cv._record_migration(vault, conv_dir / f"{cid_arch}.db", sid_arch)
    sc = _s.connect(sum_db)
    sc.execute("CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT, "
               "step_count INTEGER, last_modified_time TEXT, workspace_uris TEXT, status TEXT, source TEXT, "
               "project_id TEXT, agent_name TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, "
               "battle_id TEXT, winning_conversation_id TEXT, not_fully_idle INTEGER, killed INTEGER, "
               "last_user_input_time TEXT, last_user_input_step_index INTEGER, app_data_dir TEXT, "
               "raw_summary BLOB, group_id TEXT)")
    sc.execute("INSERT INTO conversation_summaries(conversation_id, title, project_id, workspace_uris) "
               "VALUES(?, '归档旧对话', 'p1', '[\"file:///d%3A/goai\"]')", (cid_arch,))
    sc.commit()
    sc.close()

    # 桩 smigrate 给 /antigravity/batch apply 用
    monkeypatch.setattr(cv, "find_smigrate", lambda: "fake-smigrate")

    def fake_run(cmd, **kw):
        out_path = Path(cmd[cmd.index("--output") + 1])
        casc = cmd[cmd.index("--session-id") + 1] if "--session-id" in cmd else "bbbbbbbb-1111-4111-8111-111111111111"
        c = _s.connect(out_path)
        c.execute("CREATE TABLE trajectory_meta(trajectory_id text, cascade_id text, trajectory_type integer, source integer, PRIMARY KEY (trajectory_id))")
        c.execute("CREATE TABLE steps(idx integer PRIMARY KEY, step_type integer, status integer, has_subtrajectory numeric DEFAULT 0, metadata blob, step_payload blob)")
        c.execute("CREATE TABLE trajectory_metadata_blob(id text PRIMARY KEY, data blob)")
        c.execute("INSERT INTO trajectory_meta VALUES(?, ?, 4, 17)", ("traj-1", casc))
        c.execute("INSERT INTO trajectory_metadata_blob VALUES('main', ?)", (cv._pb_len(6, casc.encode()),))
        c.execute("INSERT INTO steps(idx, step_type, status, metadata, step_payload) VALUES(0, 14, 3, NULL, ?)",
                  (cv._pb_var(1, 14) + cv._pb_var(4, 3) + cv._pb_len(19, b"\x12\x02hi"),))
        c.commit()
        c.close()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cv.subprocess, "run", fake_run)

    srv, _ = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hdrs = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    try:
        # 1. POST /sync 扫入 zcode sess_src 并立即刷新热索引
        code, sres = _req(port, "POST", "/sync", body="{}", headers=hdrs)
        assert code == 200 and sres["new"] >= 1
        code, rec = _req(port, "GET", "/recent?limit=10")
        assert code == 200 and rec["total"] >= 2

        # 2. POST /migrate_project target=zcode + apply=True 真写入 ZCODE_DB（不再静默降级）
        code, mz = _req(port, "POST", "/migrate_project",
                        body=json.dumps({"project": "goai", "target": "zcode", "agent": "zcode", "apply": True}),
                        headers=hdrs)
        assert code == 200 and mz["applied"] is True and mz["count"] == 1

        # 3. GET /antigravity/sync 预览待清归档 -> 锁库 400 -> 解锁 POST apply 成功删除
        code, sprev = _req(port, "GET", "/antigravity/sync?dedup=1&only_agent=zcode")
        assert code == 200 and sprev["targets_count"] == 1 and sprev["targets"][0]["cid"] == cid_arch

        lck = _s.connect(sum_db, timeout=0.1)
        lck.execute("BEGIN IMMEDIATE")
        try:
            code, serr = _req(port, "POST", "/antigravity/sync", body=json.dumps({"apply": True}), headers=hdrs)
            assert code == 400 and "Antigravity" in serr["error"]
        finally:
            lck.rollback()
            lck.close()

        code, sapply = _req(port, "POST", "/antigravity/sync", body=json.dumps({"apply": True}), headers=hdrs)
        assert code == 200 and sapply["removed_dbs"] == 1 and not (conv_dir / f"{cid_arch}.db").exists()

        # 4. GET /antigravity/batch 仅计划活跃 sess_src -> POST apply 导入 -> 二次预览归零
        code, bprev = _req(port, "GET", "/antigravity/batch?agent=zcode")
        assert code == 200 and bprev["planned"] == 1
        code, bapply = _req(port, "POST", "/antigravity/batch", body=json.dumps({"agent": "zcode", "apply": True}), headers=hdrs)
        assert code == 200 and bapply["imported"] == 1 and bapply["failed"] == 0
        code, bprev2 = _req(port, "GET", "/antigravity/batch?agent=zcode")
        assert code == 200 and bprev2["planned"] == 0 and bprev2["skipped_done"] >= 1
    finally:
        srv.shutdown()
        srv.server_close()
