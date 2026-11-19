"""CLI 命令面修复回归套件 (2026-11-19, 1 high + 7 medium).

覆盖:
  修复1 (high) — cmd_export 同会话链挂接缺新旧守卫 → 旧内容重导链环化; 附 repair 环自愈
  修复2 — cmd_ingest 未初始化 vault 崩 (缺 vault/snapshots)
  修复3 — ingest 不幂等: 无确定性 --session-id/session_ref + register 无 H1 守卫
  修复4 — probe --profile 不存在静默兜底探测全部
  修复5 — load_profiles 真实 ~/.headroom 文件反超显式 CV_RELAY_PROFILES
  修复6 — ledger stats 空目录裸崩且凭空建零表 vault.db
  修复7 — GBK 保护仅覆盖 doctor, list --top 管道崩 UnicodeEncodeError
  修复8 — migrate 非交互 stdin EOFError 裸栈

全部用例 tmp_path 合成 vault, 不触碰真实 vault/会话目录; probe 不发任何网络请求。
运行: python -m pytest tests/test_cv_cli_fix.py -v
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------- 公共构造 ----------------

def w(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                    encoding="utf-8")
    return path


def chain_rollout(tmp: Path, extra=None) -> Path:
    """带 token_usage_record 的最小 codex rollout (extra 追加块用于演进 V2)。"""
    sid = "11111111-1111-1111-1111-111111111111"
    lines = [
        {"timestamp": "2026-11-01T00:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"id": sid, "session_id": sid, "timestamp": "2026-11-01T00:00:00.000Z",
                     "cwd": "D:\\proj", "cli_version": "0.153.4", "source": "exec"}},
        {"timestamp": "2026-11-01T00:00:01.000Z", "ordinal": 1, "type": "response_item",
         "payload": {"type": "message", "id": "m1", "role": "user",
                     "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-11-01T00:00:02.000Z", "ordinal": 2, "type": "token_usage_record",
         "payload": {"turn_id": "t1",
                     "turn_token_usage": {"input_tokens": "100", "output_tokens": "20",
                                          "cached_input_tokens": "60",
                                          "cache_write_input_tokens": "10",
                                          "reasoning_output_tokens": "5", "total_tokens": "120"}}},
    ] + (extra or [])
    return w(tmp / f"rollout-2026-11-01T00-00-00-{sid}.jsonl", lines)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """与 tests/test_cv.py 同款隔离: cv 的全局源路径全部指向 tmp, 真实 vault 只读不触。"""
    home = tmp_path / "home"
    (home / ".codex" / "sessions" / "2026" / "11" / "01").mkdir(parents=True)
    (home / ".claude" / "projects" / "D--proj").mkdir(parents=True)
    monkeypatch.setattr(cv, "HOME", home)
    monkeypatch.setattr(cv, "CODEX_SESSIONS", home / ".codex" / "sessions")
    monkeypatch.setattr(cv, "ZCODE_DB", home / ".zcode" / "absent.sqlite")
    monkeypatch.setattr(cv, "ZCODE_TASKS_DB", home / ".zcode" / "absent_tasks.sqlite")
    monkeypatch.setattr(cv, "ANTIGRAVITY_CONV_DIR",
                        home / ".gemini" / "antigravity" / "conversations")
    monkeypatch.setattr(cv, "HEADROOM_DIR", home / ".headroom")
    # now_iso 换成确定性的秒级步进时钟: export created_at 只有秒精度, 真实时钟两次调用
    # 可能同秒 (平局按 sid), 时序守卫的测试需要确定的先后序
    clock = {"n": 0}

    def fake_now():
        clock["n"] += 1
        return f"2026-11-19T10:{clock['n'] // 60:02d}:{clock['n'] % 60:02d}+08:00"

    monkeypatch.setattr(cv, "now_iso", fake_now)
    vault = tmp_path / "vault"
    vault.mkdir()
    return home, vault


def assert_acyclic(conn) -> dict:
    """校验全库 parent 链无环, 返回 {id: parent} 映射供进一步断言。"""
    pmap = {r["id"]: r["parent_snapshot"]
            for r in conn.execute("SELECT id, parent_snapshot FROM snapshots")}
    for k, p in pmap.items():
        seen, node = set(), p
        while node:
            assert node not in seen, f"双向环: {k} 的祖先链回到已访问节点"
            seen.add(node)
            node = pmap.get(node)
    return pmap


# ---------------- 修复1 (high): export 旧内容重导不产生环; repair 能自愈 ----------------

def test_export_stale_content_keeps_root_no_cycle(isolated, capsys):
    """旧内容重导 (源截回 V1) 不得把旧快照挂到更新的兄弟下 — 双向环曾让账本叶子去重清零。"""
    home, vault = isolated
    src = chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01")
    v1 = cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "more"}]}})
    w(src, rows)
    os.utime(src)
    cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))   # 源演进 → V2 挂到 V1 下
    conn = cv.db(vault)
    pmap = assert_acyclic(conn)
    v2 = next(s for s, p in pmap.items() if s != v1)
    assert pmap[v2] == v1 and pmap[v1] is None                  # 正向链: V1 根 ← V2

    w(src, [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()][:3])
    os.utime(src)
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    pmap = assert_acyclic(conn)                                 # 旧内容重导: 不得成环
    assert pmap[v1] is None                                     # 保持 root
    assert pmap[v2] == v1                                       # 更新版本仍是叶子
    assert "不挂链" in capsys.readouterr().out                  # 给出可读提示
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100     # 叶子去重不再清零


def test_export_newer_content_still_reattaches_chain(isolated):
    """时序守卫不得误伤正向场景: 同会话更新内容经 cmd_export 仍挂为最新版本 (兄弟严格旧)。"""
    home, vault = isolated
    src = chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01")
    v1 = cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "more"}]}})
    w(src, rows)
    os.utime(src)
    v2 = cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    conn = cv.db(vault)
    pmap = assert_acyclic(conn)
    assert pmap[v2] == v1 and pmap[v1] is None                  # 挂链仍正常
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100


def test_repair_selfheals_bidirectional_cycle(isolated):
    """环库自愈: 造出 V1↔V2 双向环后 cv repair 按 mtime 重链断环, 账本口径恢复。"""
    home, vault = isolated
    src = chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01")
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines()]
    rows.append({"timestamp": "2026-11-01T01:00:00.000Z", "ordinal": 3, "type": "response_item",
                 "payload": {"type": "message", "id": "m2", "role": "user",
                             "content": [{"type": "input_text", "text": "more"}]}})
    w(src, rows)
    os.utime(src)
    cv.cmd_sync(SimpleNamespace(vault=str(vault), root=None))
    conn = cv.db(vault)
    ids = [r["id"] for r in conn.execute("SELECT id FROM snapshots ORDER BY created_at")]
    root, leaf = ids[0], ids[1]
    conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (leaf, root))   # 人为造环
    conn.execute("UPDATE snapshots SET parent_snapshot=? WHERE id=?", (root, leaf))
    conn.commit()
    assert cv.owned_stats(vault)["sessions"] == 0               # 环时叶子清零 (缺陷效应)
    cv.cmd_repair(SimpleNamespace(vault=str(vault)))
    pmap = assert_acyclic(conn)
    assert pmap[root] is None and pmap[leaf] == root            # 最旧者置根, 环解开
    own = cv.owned_stats(vault)
    assert own["sessions"] == 1 and own["tokens_in"] == 100


# ---------------- 修复2+3: ingest 全新 vault 不崩 + 幂等 ----------------

def _fake_smigrate(monkeypatch, captured: list):
    """伪造 smigrate convert: 未给 --session-id 时随机生成 sessionId (复刻真实行为)。"""
    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        out = Path(cmd[cmd.index("--output") + 1])
        det = cmd[cmd.index("--session-id") + 1] if "--session-id" in cmd else uuid.uuid4().hex
        w(out, [
            {"parentUuid": None, "type": "user", "uuid": "u1",
             "timestamp": "2026-10-01T00:00:00.000Z", "sessionId": det, "cwd": "D:\\proj",
             "message": {"role": "user", "content": "hello ingest"}},
            {"parentUuid": "u1", "type": "assistant", "uuid": "a1",
             "timestamp": "2026-10-01T00:00:02.000Z", "sessionId": det, "cwd": "D:\\proj",
             "message": {"role": "assistant", "model": "glm-5.2",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
        ])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(cv, "find_smigrate", lambda: "fake-smigrate")
    monkeypatch.setattr(cv, "subprocess", SimpleNamespace(run=fake_run))


def _ingest_args(vault: Path, src: Path) -> SimpleNamespace:
    return SimpleNamespace(vault=str(vault), source=str(src), format=None, cwd=None,
                           drop_developer=False, keep_temp=False)


def test_ingest_fresh_vault_creates_snapshot_dirs(isolated, monkeypatch):
    """全新 vault 直接 ingest: 曾只建 vault/ingest, 落快照时裸 FileNotFoundError。"""
    _, vault = isolated
    _fake_smigrate(monkeypatch, [])
    src = chain_rollout(isolated[0] / "stale")
    cv.cmd_ingest(_ingest_args(vault, src))                     # 旧代码在此 FileNotFoundError
    assert (vault / "ingest").is_dir() and (vault / "snapshots").is_dir()
    assert list((vault / "snapshots").glob("cv_*.json"))


def test_ingest_same_file_twice_same_sid_no_inflation(isolated, monkeypatch):
    """同文件两次 ingest: session_ref/ sid 确定性, 快照不堆积, 账本会话数不膨胀;
    smigrate 收到确定性 --session-id; 已在库不重注册 (H1 守卫)。"""
    home, vault = isolated
    captured: list = []
    _fake_smigrate(monkeypatch, captured)
    src = chain_rollout(home / "stale")
    cv.cmd_ingest(_ingest_args(vault, src))
    conn = cv.db(vault)
    sid1 = conn.execute("SELECT id FROM snapshots").fetchone()[0]
    n1 = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n1 == 1
    env = json.loads((vault / "snapshots" / f"{sid1}.json").read_text(encoding="utf-8"))
    assert "--session-id" in captured[0]                        # 确定性 id 确实传给 smigrate
    assert env["capture"]["session_ref"] == captured[0][-1]     # 且落到 session_ref
    cv.cmd_ingest(_ingest_args(vault, src))
    sid2 = conn.execute("SELECT id FROM snapshots ORDER BY id").fetchone()[0]
    assert sid1 == sid2                                         # 同文件重 ingest 同 sid
    n2 = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n2 == 1                                              # 快照不堆积
    assert cv.owned_stats(vault)["sessions"] == 1               # 账本不膨胀


# ---------------- 修复4: probe --profile 不存在即退出 ----------------

def test_probe_unknown_profile_exits_without_probing(isolated, monkeypatch, capsys):
    """--profile 指向不存在的 id: 可读报错退出, 不静默兜底探测全部 (曾误发真实上游请求)。"""
    _, vault = isolated
    monkeypatch.setenv("CV_RELAY_PROFILES", json.dumps(
        [{"id": "deadport", "openai_api_url": "http://127.0.0.1:1", "default_model": "m"}]))
    monkeypatch.setattr(cv, "probe_once",
                        lambda *a, **k: pytest.fail("不存在 profile 不应发起任何探测"))
    monkeypatch.setenv("CV_RELAY_KEY", "test-key")
    with pytest.raises(SystemExit, match="profile 不存在"):
        cv.cmd_probe(SimpleNamespace(vault=str(vault), profile="nope", watch=None, key=None))


# ---------------- 修复5: CV_RELAY_PROFILES 显式优先于 ~/.headroom 文件 ----------------

def test_load_profiles_env_overrides_headroom_file(isolated, monkeypatch):
    """两来源并存时 env 必须赢 (测试/CI 的隔离配置不得被真实机器 profiles 反超)。"""
    home, _ = isolated
    head = home / ".headroom"
    head.mkdir()
    (head / "switcher-profiles.json").write_text(json.dumps(
        [{"id": "file-relay", "openai_api_url": "http://127.0.0.1:1"}]), encoding="utf-8")
    monkeypatch.setenv("CV_RELAY_PROFILES", json.dumps(
        [{"id": "env-relay", "openai_api_url": "http://127.0.0.1:1"}]))
    assert [p["id"] for p in cv.load_profiles()] == ["env-relay"]   # env 显式覆盖文件
    monkeypatch.delenv("CV_RELAY_PROFILES")
    assert [p["id"] for p in cv.load_profiles()] == ["file-relay"]  # 无 env 时回落文件


# ---------------- 修复6: ledger stats 空目录不建库不崩 ----------------

def test_ledger_stats_empty_dir_readable_empty_state(isolated, capsys):
    """空目录 ledger stats: 可读空态 + 引导, 退出干净, 零文件残留 (曾裸崩且凭空建库)。"""
    _, vault = isolated
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault)))
    out = capsys.readouterr().out
    assert "先运行" in out and "init" in out
    assert not (vault / "vault.db").exists()                    # 不凭空建库
    assert not list(vault.iterdir())                            # 零残留


def test_ledger_stats_zero_table_db_no_crash(isolated):
    """库文件在但零表 (中断创建残留): 兜底建表后干净输出, 不抛 no such table。"""
    _, vault = isolated
    (vault / "vault.db").write_bytes(b"")
    cv.cmd_ledger_stats(SimpleNamespace(vault=str(vault)))      # 旧代码 OperationalError


# ---------------- 修复7: main 入口统一 UTF-8, list --top 管道不崩 ----------------

def test_list_top_pipe_survives_gbk_console(isolated):
    """env 无 PYTHONUTF8/PYTHONIOENCODING 时子进程跑 list --top 且 stdout 为管道:
    📁 曾在 GBK 管道崩 UnicodeEncodeError (全文件只有 doctor 一处 reconfigure)。"""
    home, vault = isolated
    src = chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01")
    cv.cmd_export(SimpleNamespace(vault=str(vault), source=str(src), drop_developer=False))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "cv.py"),
                        "--vault", str(vault), "list", "--top"],
                       cwd=str(REPO_ROOT), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    assert "UnicodeEncodeError" not in err
    assert "📁" in out and "D:\\proj" in out                    # 中文/emoji 输出完好


def test_doctor_direct_call_still_utf8_safe(isolated, capsys):
    """doctor 的 reconfigure 去重后仍保留兜底 (直调不经过 main 的路径)。"""
    _, vault = isolated
    cv.cmd_doctor(SimpleNamespace(vault=str(vault)))            # 不抛即通过
    assert "环境诊断" in capsys.readouterr().out


# ---------------- 修复8: migrate 非交互 EOF 优雅取消 ----------------

def test_migrate_eof_stdin_cancels_cleanly(isolated):
    """stdin 关闭 (< /dev/null 形态) 时 migrate 确认环节按取消处理:
    干净退出无栈, 已导出快照保留。"""
    home, vault = isolated
    src = chain_rollout(home / ".codex" / "sessions" / "2026" / "11" / "01")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "cv.py"),
                        "--vault", str(vault), "migrate", str(src), "--target", "markdown"],
                       cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    assert "EOFError" not in err and "Traceback" not in err     # 不抛裸栈
    assert "已取消" in out
    snaps = list((vault / "snapshots").glob("cv_*.json"))
    assert len(snaps) == 1                                      # 已导出快照留存
