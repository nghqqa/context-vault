"""DeepSeek Harness (dsh) 五合一原生适配完整回归测试。

隔离约束: 全部写入仅在 tmp_path 内进行 (monkeypatch cv.HOME / DSH_HOME), 绝不修改真实 ~/.dsh/。
真机冒烟测试仅以严格只读方式校验真实 ~/.dsh/sessions/ 样本 (若本机已安装)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import cv  # noqa: E402
import cv_dsh  # noqa: E402
import cv_ledger  # noqa: E402
import cv_skills  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    (vault / "snapshots").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(cv, "CLAUDE_PROJECTS", home / ".claude" / "projects")
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")
    monkeypatch.setattr(cv, "ANTIGRAVITY_CONV_DIR", home / ".gemini" / "antigravity" / "conversations")
    monkeypatch.delenv("DSH_HOME", raising=False)
    cv_dsh.reset_zstd_backend_cache()
    cv_skills.clear_cache()
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA + cv.SYNC_TABLE)
    conn.commit()
    conn.close()
    yield home, vault
    cv_dsh.reset_zstd_backend_cache()
    cv_skills.clear_cache()


def _write_dsh_file(path: Path, header: dict, events: list[dict], *, zstd: bool = False,
                    backend: str | None = None, trailing_garbage: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr_bytes = (json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    ev_bytes = ("\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events) + "\n").encode("utf-8")
    if zstd:
        payload = (
            cv_dsh.compress_zstd_frame(hdr_bytes, backend=backend)
            + cv_dsh.compress_zstd_frame(ev_bytes, backend=backend)
            + trailing_garbage
        )
        path.write_bytes(payload)
    else:
        path.write_bytes(hdr_bytes + ev_bytes)
    return path


def _v4_sample(cwd: str = "C:\\Work\\Demo", sid: str = "session-v4-demo") -> tuple[dict, list[dict]]:
    t0 = 1795521950087
    hdr = {
        "type": "session",
        "version": 4,
        "id": sid,
        "createdAt": t0,
        "cwd": cwd,
        "isSeeded": False,
        "delegationDepth": 0,
        "agentPreset": "standard",
    }
    evs = [
        {"type": "permission/preset", "seq": 0, "time": t0 + 1, "data": {"preset": "workspace-write"}},
        {"type": "system/message", "seq": 1, "time": t0 + 2,
         "data": {"turn": 1, "step": 1, "message": {
             "role": "system", "content": [{"type": "text", "text": "sys"}],
             "source": {"kind": "system-prompt"}, "id": "m-sys"}},
         "surfaceOp": "append"},
        {"type": "user/message", "seq": 2, "time": t0 + 10,
         "data": {"content": [{"type": "text", "text": "Inspect repo and run tests"}],
                  "source": {"kind": "user"}, "role": "user", "id": "m-u1"},
         "surfaceOp": "append"},
        {"type": "user/message", "seq": 3, "time": t0 + 11,
         "data": {"content": [{"type": "text", "text": "<system-reminder>runtime</system-reminder>"}],
                  "source": {"kind": "runtime-context"}, "role": "user", "id": "m-rt"},
         "surfaceOp": "append"},
        {"type": "user/message", "seq": 4, "time": t0 + 12,
         "data": {"content": [{"type": "text", "text": "<system-reminder>skills</system-reminder>"}],
                  "source": {"kind": "skill-catalog"}, "role": "user", "id": "m-sk"},
         "surfaceOp": "append"},
        {"type": "user/message", "seq": 5, "time": t0 + 13,
         "data": {"content": [{"type": "text", "text": "Time sampled: 2026-11-24"}],
                  "source": {"kind": "time-context"}, "role": "user", "id": "m-tc"},
         "surfaceOp": "append"},
        {"type": "session/title", "seq": 6, "time": t0 + 15,
         "data": {"title": "Inspect repo", "messageSeqs": [2], "source": {"kind": "fallback"}}},
        {"type": "request/header", "seq": 7, "time": t0 + 16,
         "data": {"header": {"config": {"provider": "deepseek-official", "model": "deepseek-flash"}}, "reason": "initial"}},
        {"type": "assistant/message", "seq": 8, "time": t0 + 500,
         "data": {
             "turn": 1,
             "step": 1,
             "message": {
                 "role": "assistant",
                 "content": [
                     {"type": "reasoning", "text": ""},
                     {"type": "reasoning", "text": "Let me run pytest."},
                     {"type": "text", "text": "Running tests now."},
                     {"type": "tool-call", "id": "call_1", "name": "bash", "arguments": "{\"command\":\"pytest\"}"},
                 ],
                 "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-flash"},
                 "id": "m-a1",
             },
             "usage": {"inputTokens": 200, "outputTokens": 50, "cacheReadTokens": 800,
                       "cacheWriteTokens": 0, "reasoningTokens": 20, "totalTokens": 1050},
             "stream": [],
         },
         "surfaceOp": "append"},
        # 独立 tool/call 日志事件应被读取端忽略, 避免重复生成 tool_call 块
        {"type": "tool/call", "seq": 9, "time": t0 + 501,
         "data": {"turn": 1, "step": 1, "callId": "call_1", "name": "bash", "arguments": "{\"command\":\"pytest\"}"}},
        {"type": "tool/result", "seq": 10, "time": t0 + 900,
         "data": {
             "turn": 1,
             "step": 1,
             "message": {
                 "role": "tool",
                 "source": {"kind": "tool", "callId": "call_1"},
                 "toolCallId": "call_1",
                 "content": [{"type": "text", "text": "42 passed\n"}],
                 "isError": False,
                 "id": "m-tr1",
             },
         },
         "sourceEventSeqs": [8],
         "surfaceOp": "append"},
        {"type": "compaction/summary", "seq": 11, "time": t0 + 1000,
         "data": {"summary": "Prior context summarized."}},
        {"type": "assistant/message", "seq": 12, "time": t0 + 1500,
         "data": {
             "turn": 1,
             "step": 2,
             "message": {
                 "role": "assistant",
                 "content": [{"type": "text", "text": "All tests passed."}],
                 "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-flash"},
                 "id": "m-a2",
             },
             "usage": {"inputTokens": 100, "outputTokens": 25, "cacheReadTokens": 900,
                       "cacheWriteTokens": 0, "reasoningTokens": 0, "totalTokens": 1025},
             "stream": [],
         },
         "surfaceOp": "append"},
        {"type": "session/title", "seq": 13, "time": t0 + 1600,
         "data": {"title": "LLM 精确标题: 仓储测试", "messageSeqs": [2], "source": {"kind": "llm"}}},
    ]
    return hdr, evs


# ------------------------------------------------------------------ 1. 路径编码与发现

def test_path_encoding_and_discovery(isolated, tmp_path):
    assert cv_dsh.encode_segment(".") == "~002E"
    assert cv_dsh.encode_segment("..") == "~002E~002E"
    assert cv_dsh.encode_segment("a/b:c~d\\e") == "a~002Fb~003Ac~007Ed~005Ce"
    assert cv_dsh.encode_segment("中文") == "~4E2D~6587"
    assert cv_dsh.encode_segment("🙂") == "~D83D~DE42"

    assert cv_dsh.project_key(r"C:\Users\ngh\Documents\deepseek-harness\default-workspace") == \
        "--C-Users-ngh-Documents-deepseek-harness-default-workspace--"
    assert cv_dsh.project_key("/home/alice/proj") == "--home-alice-proj--"
    long_cwd = "C:\\" + ("x" * 300)
    pk = cv_dsh.project_key(long_cwd)
    assert pk.startswith("--") and pk.endswith("--")
    assert len(pk) == 251 + 4

    root = tmp_path / "sessions"
    sdir = root / "--C-Work-Demo--" / "session-1"
    sdir.mkdir(parents=True)
    f3 = sdir / "session.v3.jsonl.zstd"
    f4 = sdir / "session.v4.jsonl.zstd"
    f3.write_bytes(b"old")
    f4.write_bytes(b"new")
    seg = root / ".segments" / "session-bad"
    seg.mkdir(parents=True)
    (seg / "session.v4.jsonl").write_text("{}\n", encoding="utf-8")
    empty_dir = root / "--C-Work-Demo--" / "session-empty"
    empty_dir.mkdir(parents=True)
    (empty_dir / "session.v4.jsonl").write_bytes(b"")

    found = cv_dsh.discover_dsh_session_files(root)
    assert found == [f4]


# ------------------------------------------------------------------ 2. Zstd 帧扫描与多后端往返

def test_zstd_scanner_and_backends_roundtrip():
    backends = []
    for bk in ("zstandard", "node"):
        if bk == "zstandard":
            try:
                import zstandard  # noqa: F401
                backends.append("zstandard")
            except ImportError:
                pass
        elif bk == "node":
            cv_dsh.reset_zstd_backend_cache()
            import shutil
            if shutil.which("node"):
                backends.append("node")

    assert backends, "至少应有一个 zstd 后端 (zstandard 或 node)"
    p1 = b'{"type":"session","version":4}\n'
    p2 = ("你好 Context Vault 🚀 " * 30 + "\n").encode("utf-8")

    for bk in backends:
        f1 = cv_dsh.compress_zstd_frame(p1, backend=bk)
        f2 = cv_dsh.compress_zstd_frame(p2, backend=bk)
        # 校验和位 (descriptor & 0x04) 必须开启
        assert (f1[4] & 0x04) == 0x04
        assert (f2[4] & 0x04) == 0x04
        combo = f1 + f2
        frames, torn = cv_dsh.scan_zstd_frames(combo)
        assert torn is None
        assert len(frames) == 2
        assert cv_dsh.decompress_zstd_frames(combo, max_frames=1, backend=bk) == p1
        assert cv_dsh.decompress_zstd_frames(combo, backend=bk) == p1 + p2

        # 崩溃撕裂尾帧恢复: 截断一半的第3帧不影响前两帧读取
        f3_torn = f1[:-3]
        frames_t, torn_at = cv_dsh.scan_zstd_frames(combo + f3_torn)
        assert len(frames_t) == 2
        assert torn_at == len(combo)
        assert cv_dsh.decompress_zstd_frames(combo + f3_torn, backend=bk) == p1 + p2

        with pytest.raises(ValueError, match="invalid frame magic"):
            cv_dsh.scan_zstd_frames(b"BAD!" + f1)


# ------------------------------------------------------------------ 3. 会话采集、V3/V4、子代理与 owned_stats

def test_export_dsh_v4_v3_subagent_and_owned_stats(isolated):
    home, vault = isolated
    hdr, evs = _v4_sample(cwd="C:\\Work\\Alpha", sid="session-v4-zstd")
    zpath = home / ".dsh" / "sessions" / cv_dsh.project_key("C:\\Work\\Alpha") / "session-v4-zstd" / "session.v4.jsonl.zstd"
    _write_dsh_file(zpath, hdr, evs, zstd=True, trailing_garbage=b"\x28\xb5\x2f\xfd\x00")

    res_keep = cv_dsh.export_dsh(zpath, drop_developer=False)
    assert res_keep["capture"]["agent"] == "dsh"
    assert res_keep["capture"]["source_format"] == "dsh-jsonl"
    assert res_keep["capture"]["originator"] == "LLM 精确标题: 仓储测试"
    assert res_keep["capture"]["model"] == "deepseek-flash"
    types_keep = [b["type"] for b in res_keep["conversation"]]
    roles_keep = [(b["type"], b.get("role")) for b in res_keep["conversation"]]
    assert ("text", "developer") in roles_keep
    assert types_keep.count("tool_call") == 1  # 未被 tool/call 重复计一次
    assert types_keep.count("thinking") == 1   # 空 reasoning 被跳过
    assert types_keep.count("compaction") == 1
    assert res_keep["stats"]["tokens_in"] == 300
    assert res_keep["stats"]["cache_read"] == 1700
    assert res_keep["stats"]["tokens_out"] == 75
    assert res_keep["stats"]["reasoning_tokens"] == 20
    assert res_keep["stats"]["usage_events"] == 2

    res_drop = cv_dsh.export_dsh(zpath, drop_developer=True)
    roles_drop = [(b["type"], b.get("role")) for b in res_drop["conversation"]]
    assert ("text", "developer") not in roles_drop
    # system/message (1) + 3 developer reminders (3) = 4
    assert res_drop["stats"]["blocks_dropped"] == 4

    # V3 tool/result 兼容 (role="user" + nested content=[{"type":"tool-result",...}])
    v3_path = home / ".dsh" / "sessions" / cv_dsh.project_key("C:\\Work\\Beta") / "session-v3-demo" / "session.v3.jsonl"
    _write_dsh_file(
        v3_path,
        {"type": "session", "version": 3, "id": "session-v3-demo", "createdAt": 1788240634286,
         "cwd": "C:\\Work\\Beta", "isSeeded": False, "delegationDepth": 0},
        [
            {"type": "user/message", "seq": 0, "time": 1788240634290,
             "data": {"role": "user", "source": {"kind": "user"}, "id": "u1",
                      "content": [{"type": "text", "text": "Run pwd"}]}},
            {"type": "assistant/message", "seq": 1, "time": 1788240634300,
             "data": {"turn": 1, "step": 1, "message": {
                 "role": "assistant", "id": "a1",
                 "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-v4-flash"},
                 "content": [{"type": "tool-call", "id": "c_v3", "name": "bash", "arguments": "{}"}],
             }, "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 900, "totalTokens": 1020}, "stream": []}},
            {"type": "tool/result", "seq": 2, "time": 1788240634350,
             "data": {"turn": 1, "step": 1, "message": {
                 "role": "user", "id": "tr_v3", "source": {"kind": "tool", "callId": "c_v3"},
                 "content": [{"type": "tool-result", "toolCallId": "c_v3",
                              "content": [{"type": "text", "text": "OK_V3"}], "isError": False}],
             }}},
        ],
        zstd=False,
    )
    res_v3 = cv_dsh.export_dsh(v3_path)
    trs = [b for b in res_v3["conversation"] if b["type"] == "tool_result"]
    assert len(trs) == 1 and trs[0]["call_id"] == "c_v3" and trs[0]["content"] == "OK_V3"

    # 子代理过滤标志
    sub_path = home / ".dsh" / "sessions" / cv_dsh.project_key("C:\\Work\\Alpha") / "session-sub-1" / "session.v4.jsonl"
    _write_dsh_file(
        sub_path,
        {"type": "session", "version": 4, "id": "session-sub-1", "createdAt": 1795521960000,
         "cwd": "C:\\Work\\Alpha", "isSeeded": False, "delegationDepth": 1,
         "origin": "subagent", "parentSession": "session-v4-zstd"},
        [{"type": "user/message", "seq": 0, "time": 1795521960010,
          "data": {"role": "user", "source": {"kind": "user"}, "id": "su1",
                   "content": [{"type": "text", "text": "Subagent task"}]},
          "surfaceOp": "append"}],
        zstd=False,
    )
    res_sub = cv_dsh.export_dsh(sub_path)
    assert res_sub["capture"].get("is_subagent") is True
    assert res_sub["capture"].get("parent_session_ref") == "session-v4-zstd"

    # cv sync 全量发现并入库 + owned_stats 命中率分母 <= 1.0
    sync_res = cv.sync_vault(vault, quiet=True)
    assert sync_res["new"] == 3
    own = cv.owned_stats(vault)
    assert own["cache_hit_rate"] <= 1.0
    assert any(a["agent"] == "dsh" and a["sessions"] == 3 for a in own["by_agent"])
    # 顶层主对话过滤掉子代理 session-sub-1 (剩余 V4 + V3 共 2 条)
    tops = cv.resolve_top_conversations(vault, agent="dsh")
    assert {t["session_ref"] for t in tops} == {"session-v4-zstd", "session-v3-demo"}


# ------------------------------------------------------------------ 4. V4 写回、平行工具、workspace.json 与安全降级

def test_blocks_to_dsh_and_do_import_apply_roundtrip(isolated, monkeypatch, tmp_path):
    home, vault = isolated
    ws_proj = tmp_path / "proj_alpha"
    ws_proj.mkdir()
    canon_proj = os.path.realpath(str(ws_proj))

    env = cv.build_envelope({
        "capture": {"agent": "claude-code", "source_format": "claude-projects",
                    "provider": "anthropic", "model": "claude-sonnet-4",
                    "session_ref": "cl-src-001", "cwd": str(ws_proj), "completeness": "full"},
        "conversation": [
            {"type": "text", "role": "user", "content": "Old msg", "ts": "2026-11-24T10:00:00Z"},
            {"type": "compaction", "summary": "Summarized earlier work", "ts": "2026-11-24T10:01:00Z"},
            {"type": "text", "role": "developer", "content": "Ignore me", "ts": "2026-11-24T10:02:00Z"},
            {"type": "text", "role": "user", "content": "Run two parallel checks", "ts": "2026-11-24T10:03:00Z"},
            {"type": "thinking", "summary": "Plan to call two tools", "ts": "2026-11-24T10:03:05Z"},
            {"type": "tool_call", "call_id": "c_p1", "name": "bash", "arguments": {"command": "echo 1"},
             "ts": "2026-11-24T10:03:06Z"},
            {"type": "tool_call", "call_id": "c_p2", "name": "read", "arguments": {"path": "a.py"},
             "ts": "2026-11-24T10:03:07Z"},
            {"type": "tool_result", "call_id": "c_p1", "content": "1\n", "is_error": False,
             "ts": "2026-11-24T10:03:08Z"},
            {"type": "tool_result", "call_id": "c_p2", "content": "print(1)", "is_error": False,
             "ts": "2026-11-24T10:03:09Z"},
            {"type": "tool_result", "call_id": "orphan_call", "content": "orphan", "is_error": True,
             "ts": "2026-11-24T10:03:10Z"},
            {"type": "text", "role": "assistant", "content": "Both checks succeeded.", "ts": "2026-11-24T10:03:12Z"},
        ],
        "stats": cv.zero_stats(),
    })
    sid = cv.snapshot_id(env)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    conn = cv.db(vault)
    cv.register_snapshot(conn, env, sid, vault / "snapshots" / f"{sid}.json")
    conn.commit()
    conn.close()

    # Dry-run 仅写纯文本 .v4.jsonl 到 vault/outbox/
    dry = cv.do_import(vault, sid, "dsh", apply=False)
    assert dry["applied"] is False
    assert dry["dropped"] == 2  # 1 developer + 1 orphan tool_result
    out_p = Path(dry["dest"])
    assert out_p.exists() and out_p.parent == (vault / "outbox") and out_p.name.endswith(".v4.jsonl")

    lines = [json.loads(ln) for ln in out_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    hdr, evs = lines[0], lines[1:]
    assert hdr["type"] == "session" and hdr["version"] == 4 and hdr["isSeeded"] is False
    assert hdr["delegationDepth"] == 0 and hdr["cwd"] == canon_proj
    assert [e["seq"] for e in evs] == list(range(len(evs)))
    assert all(isinstance(e["time"], int) and e["time"] >= 0 for e in evs)

    # 平行工具结果必须共享同一步 (turn=1, step=1), 而后续 assistant 文本推进到 step=2
    trs = [e for e in evs if e["type"] == "tool/result"]
    assert len(trs) == 2
    assert trs[0]["data"]["step"] == trs[1]["data"]["step"] == 1
    assert trs[0]["data"]["message"]["role"] == "tool"
    assert trs[0]["data"]["message"]["toolCallId"] == trs[0]["data"]["message"]["source"]["callId"] == "c_p1"
    assts = [e for e in evs if e["type"] == "assistant/message"]
    assert [a["data"]["step"] for a in assts] == [1, 2]

    # 准备已有 workspace.json (含已归档状态与过期 projcache, 校验清理 + 幂等迁移)
    ws_file = home / ".dsh" / "storages" / "workspace.json"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps({
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": True, "workspaceIds": ["ws-other"],
                   "archivedSessionIds": [dry["new_sid"]], "pinnedSessionIds": [],
                   "defaultWorkspaceId": "ws-other"},
        "tables": {"workspaces": {
            "ws-other": {"path": str(tmp_path / "other_ws"), "title": "other_ws",
                         "sessionIds": [dry["new_sid"]], "createdAt": "2026-11-24T00:00:00Z",
                         "updatedAt": "2026-11-24T00:00:00Z"},
        }},
    }), encoding="utf-8")
    stale_cache = home / ".dsh" / "storages" / "session_projcache" / "sessions" / f"{dry['new_sid']}.json"
    stale_cache.parent.mkdir(parents=True, exist_ok=True)
    stale_cache.write_text("{}", encoding="utf-8")

    # --apply: 写出双帧校验和 .v4.jsonl.zstd 并更新 workspace.json + 清理 projcache
    applied = cv.do_import(vault, sid, "dsh", apply=True)
    assert applied["applied"] is True
    assert applied["workspace_registered"] is True
    dest_z = Path(applied["dest"])
    assert dest_z.name == "session.v4.jsonl.zstd" and dest_z.exists()
    raw_z = dest_z.read_bytes()
    frames, torn = cv_dsh.scan_zstd_frames(raw_z)
    assert len(frames) == 2 and torn is None
    assert (raw_z[frames[0][0] + 4] & 0x04) == 0x04
    assert not stale_cache.exists()
    assert ws_file.with_suffix(".json.bak-cv").exists()

    ws_after = json.loads(ws_file.read_text(encoding="utf-8"))
    assert dry["new_sid"] not in ws_after["global"]["archivedSessionIds"]
    assert dry["new_sid"] not in ws_after["tables"]["workspaces"]["ws-other"]["sessionIds"]
    new_ws_ids = [w for w in ws_after["global"]["workspaceIds"] if w != "ws-other"]
    assert len(new_ws_ids) == 1
    assert dry["new_sid"] in ws_after["tables"]["workspaces"][new_ws_ids[0]]["sessionIds"]

    # 重导出幂等: sessionIds 不重复插入
    cv.do_import(vault, sid, "dsh", apply=True)
    ws_re = json.loads(ws_file.read_text(encoding="utf-8"))
    assert ws_re["tables"]["workspaces"][new_ws_ids[0]]["sessionIds"].count(dry["new_sid"]) == 1

    # 往返读回校验
    rt = cv_dsh.export_dsh(dest_z)
    assert any(b["type"] == "thinking" and "Plan to call" in b["summary"] for b in rt["conversation"])
    assert any(b["type"] == "tool_call" and b["call_id"] == "c_p2" for b in rt["conversation"])
    assert any(b["type"] == "tool_result" and b["call_id"] == "c_p1" for b in rt["conversation"])
    assert any(b["type"] == "text" and b["role"] == "assistant" and "Both checks" in b["content"]
               for b in rt["conversation"])

    # 无 zstd 编码器且目标为 zstd 时, 必须报错阻止混放并保留原目录完好
    monkeypatch.setattr(cv_dsh, "zstd_backend", lambda: None)
    with pytest.raises(cv.BadMigrateTarget, match="zstd"):
        cv.do_import(vault, sid, "dsh", apply=True)


# ------------------------------------------------------------------ 5. Token 账本直采

def test_ledger_collect_dsh_and_pricing(isolated):
    home, vault = isolated
    hdr, evs = _v4_sample(cwd="C:\\Work\\Alpha", sid="session-ledger-1")
    zpath = home / ".dsh" / "sessions" / cv_dsh.project_key("C:\\Work\\Alpha") / "session-ledger-1" / "session.v4.jsonl.zstd"
    _write_dsh_file(zpath, hdr, evs, zstd=True)

    cv_ledger.cmd_ledger_sync(SimpleNamespace(vault=str(vault), sources="dsh", from_agm=None, recompute=False))
    conn = cv.db(vault)
    rows = conn.execute(
        "SELECT source, client, model, tokens_in, tokens_out, cache_read, reasoning_tokens,"
        " price_known, cost_est_usd FROM usage_events WHERE source='dsh' ORDER BY tokens_in DESC"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0]["model"] == "deepseek-flash"
    assert rows[0]["tokens_in"] == 200 and rows[0]["cache_read"] == 800
    assert rows[0]["tokens_out"] == 50 and rows[0]["reasoning_tokens"] == 20
    assert rows[0]["price_known"] == 1 and rows[0]["cost_est_usd"] > 0

    # 再次增量同步: 文件未变则全部跳过 (0 新入账)
    cv_ledger.cmd_ledger_sync(SimpleNamespace(vault=str(vault), sources="dsh", from_agm=None, recompute=False))
    conn = cv.db(vault)
    cnt = conn.execute("SELECT COUNT(*) FROM usage_events WHERE source='dsh'").fetchone()[0]
    conn.close()
    assert cnt == 2


# ------------------------------------------------------------------ 6. Skill 双向迁移

def test_skills_bidirectional_dsh(isolated, monkeypatch, tmp_path):
    home, _ = isolated
    src_sk = home / ".zcode" / "skills" / "demo-skill"
    src_sk.mkdir(parents=True)
    (src_sk / "SKILL.md").write_text(
        "---\nname: Demo Skill\ndescription: Reuse ~/.dsh/notes carefully.\n---\n\n# Steps\nDo work.\n",
        encoding="utf-8",
    )

    r_dry = cv_skills.migrate_skills(source="zcode", target="dsh", apply=False)
    assert r_dry["counts"]["planned_copy"] == 1
    assert not (home / ".dsh" / "skills" / "demo-skill").exists()

    r_app = cv_skills.migrate_skills(source="zcode", target="dsh", apply=True)
    assert r_app["counts"]["migrated"] == 1
    assert (home / ".dsh" / "skills" / "demo-skill" / "SKILL.md").is_file()

    # DSH -> Antigravity
    r_back = cv_skills.migrate_skills(source="dsh", target="antigravity", apply=True)
    assert r_back["counts"]["migrated"] == 1
    assert (home / ".gemini" / "config" / "skills" / "demo-skill" / "SKILL.md").is_file()
    assert any(w for it in r_back["items"] for w in it["warnings"] if "~/.dsh" in w)

    # 验证 DSH_HOME 切换使 source_summary 缓存正确失效
    sums1 = {s["agent"]: s["standalone"] for s in cv_skills.source_summary()}
    assert sums1["dsh"] == 1
    custom_dsh = tmp_path / "custom_dsh_home"
    (custom_dsh / "skills").mkdir(parents=True)
    monkeypatch.setenv("DSH_HOME", str(custom_dsh))
    sums2 = {s["agent"]: s["standalone"] for s in cv_skills.source_summary()}
    assert sums2["dsh"] == 0


# ------------------------------------------------------------------ 7. MCP Cordis Patch 幂等接入

def test_mcp_dsh_install_variants_and_status(isolated):
    home, vault = isolated
    st0 = cv.mcp_status_api(vault)
    dsh0 = next(c for c in st0["clients"] if c["id"] == "dsh")
    assert dsh0["detected"] is False and dsh0["installed"] is False

    # Case A: ~/.dsh 存在但无 cordis.patch.yml → 新建
    (home / ".dsh").mkdir()
    msg1 = cv_dsh.install_mcp_dsh(vault)
    assert "已写入" in msg1
    cfg = home / ".dsh" / "cordis.patch.yml"
    txt1 = cfg.read_text(encoding="utf-8")
    assert "- insert:" in txt1 and "memory-context-vault" in txt1 and "context-vault" in txt1
    assert cv_dsh.install_mcp_dsh(vault) == "已安装 (跳过)"

    # Case B: 裸 `[]` → 替换而非在 [] 下直接拼 `- insert:`
    cfg.write_text("# comment header\n[]\n", encoding="utf-8")
    cv_dsh.install_mcp_dsh(vault)
    txt2 = cfg.read_text(encoding="utf-8")
    assert "# comment header" in txt2
    assert "\n[]" not in txt2 and not txt2.lstrip().startswith("[]")
    assert "- insert:" in txt2
    assert cfg.with_suffix(".yml.bak-cv").exists()

    # Case C: 已有其他用户 patch → 保留原内容并追加
    cfg.write_text("- id: ui-chat\n  name: '@deepseek-ai/dsh-client-ui-chat'\n  config:\n    transcriptView: standard\n",
                   encoding="utf-8")
    cv_dsh.install_mcp_dsh(vault)
    txt3 = cfg.read_text(encoding="utf-8")
    assert "ui-chat" in txt3 and "memory-context-vault" in txt3
    st1 = cv.mcp_status_api(vault)
    dsh1 = next(c for c in st1["clients"] if c["id"] == "dsh")
    assert dsh1["detected"] is True and dsh1["app_detected"] is False and dsh1["residual"] is True and dsh1["installed"] is True

    # Case D: 安装假 ~/.local/bin/dsh 可执行文件 → app_detected=True, residual=False
    fake_dsh = home / ".local" / "bin" / ("dsh.exe" if sys.platform.startswith("win") else "dsh")
    fake_dsh.parent.mkdir(parents=True, exist_ok=True)
    fake_dsh.write_text("", encoding="utf-8")
    st2 = cv.mcp_status_api(vault)
    dsh2 = next(c for c in st2["clients"] if c["id"] == "dsh")
    assert dsh2["app_detected"] is True and dsh2["residual"] is False


def test_mcp_dsh_uninstall_variants(isolated):
    home, vault = isolated
    assert cv_dsh.uninstall_mcp_dsh(vault) == "未安装 (无需移除)"

    dsh_dir = home / ".dsh"
    dsh_dir.mkdir()
    cfg = dsh_dir / "cordis.patch.yml"

    # Case A: 只有 memory-context-vault 的 - insert: → 移除后回写带注释 []\n
    cfg.write_text("# header comment\n[]\n", encoding="utf-8")
    cv_dsh.install_mcp_dsh(vault)
    msg1 = cv_dsh.uninstall_mcp_dsh(vault)
    assert "移除 context-vault" in msg1
    after1 = cfg.read_text(encoding="utf-8")
    assert "# header comment" in after1 and after1.rstrip().endswith("[]")
    assert cfg.with_suffix(".yml.bak-cv").exists()
    assert not next(c for c in cv.mcp_status_api(vault)["clients"] if c["id"] == "dsh")["installed"]

    # Case B: 同文件内含其他 patch + 同 - insert: 下有多子项 → 仅删 memory-context-vault
    cfg.write_text(
        "- id: ui-chat\n"
        "  name: '@deepseek-ai/dsh-client-ui-chat'\n\n"
        "- insert:\n"
        "    - id: other-mcp\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: other\n"
        "    - id: memory-context-vault\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: context-vault\n",
        encoding="utf-8",
    )
    msg2 = cv_dsh.uninstall_mcp_dsh(vault)
    assert "移除 context-vault" in msg2
    after2 = cfg.read_text(encoding="utf-8")
    assert "ui-chat" in after2 and "other-mcp" in after2
    assert "memory-context-vault" not in after2 and "context-vault" not in after2


# ------------------------------------------------------------------ 8. 真机只读冒烟测试 (若本机已安装真实 ~/.dsh/sessions)

def test_real_local_dsh_readonly_smoke():
    real_sessions = Path.home() / ".dsh" / "sessions"
    files = cv_dsh.discover_dsh_session_files(real_sessions)
    if not files:
        pytest.skip("本机无真实 ~/.dsh/sessions 数据")
    sample = files[0]
    before_stat = (sample.stat().st_mtime_ns, sample.stat().st_size)
    exp = cv_dsh.export_dsh(sample)
    assert exp["capture"]["agent"] == "dsh"
    assert exp["stats"]["blocks_total"] > 0
    evs, _ = cv_ledger.collect_dsh(base_dir=real_sessions)
    assert evs and sum(e["tokens_in"] for e in evs) > 0
    after_stat = (sample.stat().st_mtime_ns, sample.stat().st_size)
    assert after_stat == before_stat
