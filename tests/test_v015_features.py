"""v0.15.0 新功能与检索质量回归测试：
1. 有界/分化时间衰减 (DEFAULT_DECAY_FLOOR / query_decay_floor) 与 no_decay 三路等价；
2. 用量账本 CSV / Markdown 报表导出 (API + CLI + HTTP 下载/报错)；
3. 备份清单、CRC+SQLite 完整性校验、损坏识别、路径穿越拦截、下载免 gzip 及审计过滤；
4. Windows 托盘自更新辅助工具 (SHA256SUMS 解析、Release 资产提取、回环下载校验与批处理生成)。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_core  # noqa: E402
import cv_ledger  # noqa: E402
import cv_ops  # noqa: E402
import cv_rank  # noqa: E402
import cv_semantic  # noqa: E402
import mcp_server  # noqa: E402


def _write_snapshot(
    vault: Path,
    *,
    agent: str,
    cwd: str,
    created_at: str,
    blocks: list[dict],
    session_ref: str = "s1",
    parent_snapshot: str | None = None,
) -> tuple[str, Path]:
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    env = {
        "format": "context-snapshot/0.1",
        "created_at": created_at,
        "capture": {
            "agent": agent,
            "source_format": f"{agent}-native",
            "completeness": "full",
            "cwd": cwd,
            "session_ref": session_ref,
        },
        "lineage": {"parent_snapshot": parent_snapshot, "fork_of": None},
        "stats": {**cv.zero_stats(), "blocks_total": len(blocks)},
        "conversation": blocks,
    }
    sid = cv.snapshot_id(env)
    p = vault / "snapshots" / f"{sid}.json"
    cv.atomic_write_text(p, json.dumps(env, ensure_ascii=False))
    return sid, p


def _index_vault(vault: Path) -> None:
    conn = cv.db(vault)
    try:
        conn.executescript(cv.SCHEMA + cv.MEMORY_TABLE)
        for p in sorted((vault / "snapshots").glob("cv_*.json")):
            env = json.loads(p.read_text(encoding="utf-8"))
            cv.register_snapshot(conn, env, p.stem, p)
        conn.commit()
    finally:
        conn.close()
    idx = cv.VaultIndex(vault, cv.block_text)
    idx.rebuild(force=True)
    idx._close()


def _http_raw(url: str, *, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req_hdr = {"User-Agent": cv_core.user_agent(), **(headers or {})}
    req = urllib.request.Request(url, headers=req_hdr, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _http_json(url: str) -> tuple[int, dict]:
    st, _, body = _http_raw(url)
    return st, json.loads(body.decode("utf-8"))


def test_bounded_and_differentiated_decay_floor(monkeypatch):
    """老内容有界保底 (0.65 / 标识符 0.80), 支持 CV_DECAY_FLOOR 与 half_life=0。"""
    now = dt.datetime(2026, 11, 26, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    ten_years_ago = "2016-11-26T12:00:00+00:00"

    f_nl = cv_rank.query_decay_floor(["数据库", "策略"])
    f_tech = cv_rank.query_decay_floor(["CV_DECAY_FLOOR"])
    f_err = cv_rank.query_decay_floor(["http_502", "超时"])
    assert f_nl == pytest.approx(0.65)
    assert f_tech == pytest.approx(0.80)
    assert f_err == pytest.approx(0.80)

    d_nl = cv_rank.decay_factor(ten_years_ago, now, 90.0, floor=f_nl)
    d_tech = cv_rank.decay_factor(ten_years_ago, now, 90.0, floor=f_tech)
    assert 0.65 <= d_nl < 0.66
    assert 0.80 <= d_tech < 0.81
    assert cv_rank.decay_factor(ten_years_ago, now, 0.0) == 1.0

    monkeypatch.setenv("CV_DECAY_FLOOR", "0.72")
    assert cv_rank.default_decay_floor() == pytest.approx(0.72)
    assert cv_rank.query_decay_floor(["中文"]) == pytest.approx(0.72)
    assert cv_rank.query_decay_floor(["vault_token_pipeline"]) == pytest.approx(0.80)


def test_no_decay_three_way_equivalence_http_readonly_cold_and_semantic(tmp_path, monkeypatch):
    """no_decay 在 HTTP /search+/context、只读索引、ColdRanker 与 hybrid_search 严格等价。"""
    vault = tmp_path / "vault"
    now_local = dt.datetime.now().astimezone()
    old_ts = (now_local - dt.timedelta(days=600)).isoformat(timespec="seconds")
    fresh_ts = (now_local - dt.timedelta(days=1)).isoformat(timespec="seconds")

    # old_strong 长度更紧凑 (仅 2 词), fresh_weak 含较多稀释词, 让 BM25(old_strong) > BM25(fresh_weak),
    # 使默认开启时间衰减 (floor=0.80) 时 fresh_weak 排第一, 关闭衰减后 old_strong 反超。
    old_sid, _ = _write_snapshot(
        vault,
        agent="claude-code",
        cwd=r"E:\work\kernel",
        created_at=old_ts,
        session_ref="s-old",
        blocks=[
            {"type": "text", "role": "user", "content": "kernel_scheduler_latch kernel_scheduler_latch audit"},
            {"type": "text", "role": "assistant", "content": "kernel_scheduler_latch kernel_scheduler_latch verified"},
        ],
    )
    fresh_sid, _ = _write_snapshot(
        vault,
        agent="codex",
        cwd=r"E:\work\docs",
        created_at=fresh_ts,
        session_ref="s-fresh",
        blocks=[
            {"type": "text", "role": "user", "content": "weekly standup meeting schedule planning lunch notes"},
            {"type": "text", "role": "assistant", "content": "kernel_scheduler_latch kernel_scheduler_latch mentioned alongside sprint planning grocery errands docs travel agenda review notes"},
        ],
    )
    # 加 2 份无关背景快照保证 N=4 > df=2, 让 BM25 IDF > 0
    for i in range(2):
        _write_snapshot(
            vault,
            agent="zcode",
            cwd=r"E:\work\other",
            created_at=fresh_ts,
            session_ref=f"s-bg-{i}",
            blocks=[{"type": "text", "role": "user", "content": f"unrelated frontend styling button margin update {i}"}],
        )
    _index_vault(vault)

    # 1) 热服务 HTTP + MCP 热路径
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st_dec, d_dec = _http_json(f"http://127.0.0.1:{port}/search?q=kernel_scheduler_latch&limit=5")
        st_nd, d_nd = _http_json(f"http://127.0.0.1:{port}/search?q=kernel_scheduler_latch&limit=5&no_decay=1")
        assert st_dec == 200 and st_nd == 200
        scores_dec = {h["sid"]: h["score"] for h in d_dec["hits"]}
        scores_nd = {h["sid"]: h["score"] for h in d_nd["hits"]}
        assert d_nd["hits"][0]["sid"] == old_sid
        assert scores_nd[old_sid] > scores_dec[old_sid]
        assert scores_dec[old_sid] >= scores_nd[old_sid] * 0.79

        st_cdec, c_dec = _http_json(f"http://127.0.0.1:{port}/context?q=kernel_scheduler_latch&limit=5")
        st_cnd, c_nd = _http_json(f"http://127.0.0.1:{port}/context?q=kernel_scheduler_latch&limit=5&no_decay=1")
        assert st_cdec == 200 and st_cnd == 200
        cscores_dec = {r["sid"]: r["score"] for r in c_dec["results"]}
        cscores_nd = {r["sid"]: r["score"] for r in c_nd["results"]}
        assert c_nd["results"][0]["sid"] == old_sid
        assert cscores_nd[old_sid] > cscores_dec[old_sid]

        monkeypatch.setattr(mcp_server, "SVC_BASE", f"http://127.0.0.1:{port}")
        mcp_server._svc_clear_neg()
        hot_s = mcp_server.dispatch("vault_search", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)
        hot_c = mcp_server.dispatch("vault_context", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)

        # cv_semantic.hybrid_search 透传 decay=False (关闭 Dense 请求直接走 BM25 基线)
        prov = cv_semantic.DenseProvider(vault=vault)
        prov.url = ""
        hyb_nd = cv_semantic.hybrid_search(
            idx, vault, "kernel_scheduler_latch", 5,
            provider=prov,
            decay=False,
        )
        assert hyb_nd["hits"][0]["sid"] == old_sid
        assert {h["sid"]: h["score"] for h in hyb_nd["hits"]} == scores_nd
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()

    # 2) 只读 search_index.db 直查
    monkeypatch.setattr(mcp_server, "SVC_BASE", "http://127.0.0.1:1")
    mcp_server._svc_clear_neg()
    ro_s = mcp_server.dispatch("vault_search", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)
    ro_c = mcp_server.dispatch("vault_context", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)

    # 3) 移除 search_index.db → ColdRanker 冷扫描
    (vault / "search_index.db").unlink()
    cold_s = mcp_server.dispatch("vault_search", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)
    cold_c = mcp_server.dispatch("vault_context", {"query": "kernel_scheduler_latch", "no_decay": True}, vault)

    assert "(via 热索引)" in hot_s and "(via 本地索引)" in ro_s and "via " not in cold_s
    assert "via 热索引" in hot_c and "via " not in ro_c and "via " not in cold_c
    assert hot_s.splitlines()[1:] == ro_s.splitlines()[1:] == cold_s.splitlines()[1:]
    assert hot_c.splitlines()[1:] == ro_c.splitlines()[1:] == cold_c.splitlines()[1:]
    assert old_sid in hot_s and fresh_sid in hot_s
    assert hot_s.index(old_sid) < hot_s.index(fresh_sid)


def _seed_usage_events(vault: Path, now: float) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    conn = cv.db(vault)
    try:
        conn.executescript(cv.SCHEMA)
        cv_ledger.ensure_schema(conn)
        rows = [
            ("k1", "claude", "claude-code", "acct-main", "claude-sonnet-4-5", r"E:\work\alpha-app",
             "s1", now - 3600, 1000, 500, 0, 2000, 100, 0.015, 0, 1, "{}"),
            ("k2", "codex", "codex", None, "gpt-5.6", r"E:\work\beta-svc",
             "s2", now - 2 * 86400, 2500, 800, 50, 500, 0, 0.025, 0, 1, "{}"),
            ("k3", "zcode", "zcode", "acct-main", "glm-5", r"E:\work\alpha-app",
             "s3", now - 40 * 86400, 4000, 1200, 0, 0, 0, 0.040, 1, 1, "{}"),
        ]
        conn.executemany(
            "INSERT INTO usage_events("
            "event_key,source,client,account,model,project,session_ref,occurred_at,"
            "tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,"
            "cost_est_usd,is_estimated,price_known,raw"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_ledger_export_csv_markdown_cli_and_http(tmp_path, capsys):
    """账本导出 API/CLI/HTTP 支持 30d/all、account 过滤与格式校验。"""
    vault = tmp_path / "vault"
    now = time.time()
    _seed_usage_events(vault, now)

    res_30d = cv_ledger.export_ledger_report(vault, fmt="csv", window="30d", now=now)
    res_all = cv_ledger.export_ledger_report(vault, fmt="csv", window="all", now=now)
    assert res_30d["events"] == 2
    assert res_all["events"] == 3
    parsed = list(csv.DictReader(io.StringIO(res_all["content"])))
    summary = [r for r in parsed if r["section"] == "summary"]
    assert len(summary) == 1 and int(summary[0]["events"]) == 3
    models = {r["name"]: int(r["events"]) for r in parsed if r["section"] == "by_model"}
    assert models["claude-sonnet-4-5"] == 1 and models["glm-5"] == 1

    res_acct = cv_ledger.export_ledger_report(vault, fmt="md", window="all", account="acct-main", now=now)
    assert res_acct["format"] == "markdown"
    assert res_acct["events"] == 2
    assert "# Context Vault 用量账本报表 (all · 账号 `acct-main`)" in res_acct["content"]
    assert "## 按项目分布 (by_project)" in res_acct["content"]

    with pytest.raises(ValueError):
        cv_ledger.export_ledger_report(vault, fmt="xml", window="30d")
    with pytest.raises(ValueError):
        cv_ledger.export_ledger_report(vault, fmt="csv", window="999d")

    out_csv = tmp_path / "reports" / "ledger.csv"
    cv.main(["--vault", str(vault), "ledger", "export", "--format", "csv", "--window", "all", "--out", str(out_csv)])
    assert out_csv.is_file() and "by_project" in out_csv.read_text(encoding="utf-8")
    _ = capsys.readouterr()

    cv.main(["--vault", str(vault), "ledger", "export", "--format", "md", "--window", "all"])
    md_stdout = capsys.readouterr().out
    assert "# Context Vault 用量账本报表 (all)" in md_stdout

    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st_csv, hdrs, raw_csv = _http_raw(f"http://127.0.0.1:{port}/ledger/export?format=csv&window=all")
        assert st_csv == 200
        assert "text/csv" in hdrs.get("content-type", "")
        assert "attachment;" in hdrs.get("content-disposition", "")
        assert len(list(csv.DictReader(io.StringIO(raw_csv.decode("utf-8"))))) >= 5

        st_j, payload_md = _http_json(f"http://127.0.0.1:{port}/ledger/export?format=markdown&window=all&json=1")
        assert st_j == 200 and payload_md["format"] == "markdown" and payload_md["events"] == 3

        st_bad, err_bad = _http_json(f"http://127.0.0.1:{port}/ledger/export?format=xml")
        assert st_bad == 400 and "error" in err_bad
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()


def test_backup_list_verify_tamper_traversal_download_and_audit(tmp_path, capsys):
    """备份列表、CRC+SQLite 校验、损坏识别、路径穿越拦截、下载无 gzip 与审计 kind 过滤。"""
    vault = tmp_path / "vault"
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    _write_snapshot(
        vault,
        agent="codex",
        cwd=r"E:\work\proj-ops",
        created_at=now_iso,
        blocks=[{"type": "text", "role": "user", "content": "backup check baseline"}],
    )
    _index_vault(vault)

    # 写入 probe_events: 新插入 8 条 ok 压过旧 error / missing_models, 验证 SQL 预过滤不漏检
    conn = cv.db(vault)
    try:
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-11-26T09:00:00+08:00", "p-err", "http://x", "http_502", 502, 120, "[]", "[]"),
        )
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-11-26T09:05:00+08:00", "p-miss", "http://x", "ok", 200, 90, "[]", '["claude-opus"]'),
        )
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-11-26T09:10:00+08:00", "p-sw", "http://x", "switch", 200, 80, '["glm-5"]', "[]"),
        )
        for i in range(8):
            conn.execute(
                "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (f"2026-11-26T10:0{i}:00+08:00", "p-ok", "http://x", "ok", 200, 30, '["glm-5"]', "[]"),
            )
        conn.commit()
    finally:
        conn.close()

    # 审计 kind 过滤 (limit=5 小于新 ok 条数 8, 必须仍命中老 error / abnormal / switch)
    au_err = cv.audit_summary_and_timeline(vault, "", "error", limit=5)
    err_statuses = [ev["status"] for d in au_err["timeline"] for ev in d["events"]]
    assert "http_502" in err_statuses and len(err_statuses) == 2

    au_abn = cv.audit_summary_and_timeline(vault, "", "abnormal", limit=5)
    assert [ev["profile"] for d in au_abn["timeline"] for ev in d["events"]] == ["p-miss", "p-err"]

    # 创建并列出/校验备份
    bres = cv.backup_vault_api(vault, keep=5)
    assert Path(bres["path"]).is_file()
    b_name = bres["name"]

    blist = cv.list_backups_api(vault)
    assert blist["count"] == 1
    assert blist["backups"][0]["name"] == b_name
    assert blist["backups"][0]["valid_zip"] is True and blist["backups"][0]["has_db"] is True

    v_ok = cv.verify_backup_api(vault, b_name)
    assert v_ok["ok"] is True and v_ok["crc_ok"] is True and v_ok["db_ok"] is True
    assert v_ok["db_check"] == "ok" and len(v_ok["sha256"]) == 64

    # 构造内部 vault.db 被破坏的 zip
    tampered = vault / "backups" / "cv-backup-99999999-tampered.zip"
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("vault.db", b"not-a-valid-sqlite-database-header")
    v_bad = cv.verify_backup_api(vault, tampered.name)
    assert v_bad["ok"] is False and v_bad["crc_ok"] is True and v_bad["db_ok"] is False and v_bad["error"]

    # 路径穿越防护
    for bad_name in ("../vault.db", "a/b.zip", r"..\evil.zip", "notzip.txt"):
        with pytest.raises(ValueError):
            cv.resolve_backup_file(vault, bad_name)

    # CLI --list / --verify
    cv.main(["--vault", str(vault), "backup", "--list"])
    out_list = capsys.readouterr().out
    assert b_name in out_list
    cv.main(["--vault", str(vault), "backup", "--verify", b_name])
    assert "校验 [OK]" in capsys.readouterr().out

    # HTTP 路由验证
    srv, idx = cv.build_vault_server(vault, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st_l, j_l = _http_json(f"http://127.0.0.1:{port}/ops/backups")
        assert st_l == 200 and j_l["count"] == 2

        st_v, j_v = _http_json(f"http://127.0.0.1:{port}/ops/backup/verify?name={b_name}")
        assert st_v == 200 and j_v["ok"] is True

        st_trav, _ = _http_json(f"http://127.0.0.1:{port}/ops/backup/verify?name=../vault.db")
        assert st_trav == 400

        st_miss, _ = _http_json(f"http://127.0.0.1:{port}/ops/backup/verify?name=cv-backup-missing.zip")
        assert st_miss == 404

        st_dl, hdrs_dl, body_dl = _http_raw(
            f"http://127.0.0.1:{port}/ops/backup/download?name={b_name}",
            headers={"Accept-Encoding": "gzip"},
        )
        assert st_dl == 200
        assert hdrs_dl.get("content-type") == "application/zip"
        assert hdrs_dl.get("content-encoding") is None
        assert body_dl == Path(bres["path"]).read_bytes()

        # _get_audit 缓存键必须包含 kind 与 limit (连续不同参数不能串缓存)
        st_a1, a_err = _http_json(f"http://127.0.0.1:{port}/audit?kind=error&limit=5")
        st_a2, a_sw = _http_json(f"http://127.0.0.1:{port}/audit?kind=switch&limit=5")
        assert st_a1 == 200 and st_a2 == 200
        assert any(ev["profile"] == "p-err" for d in a_err["timeline"] for ev in d["events"])
        assert [ev["profile"] for d in a_sw["timeline"] for ev in d["events"]] == ["p-sw"]
    finally:
        srv.shutdown()
        srv.server_close()
        idx._close()


def test_tray_self_updater_helpers_and_loopback_sha256(tmp_path):
    """解析 SHA256SUMS、选择 Release 资产、回环流式下载校验与批处理生成。"""
    sample_sums = (
        "# comment line\n"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  ContextVault.exe\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb *dist/other.exe\n"
        "SHA256 (foo.tar.gz) = cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n"
    )
    parsed = cv_ops.parse_sha256sums(sample_sums)
    assert parsed["ContextVault.exe"] == "a" * 64
    assert parsed["other.exe"] == "b" * 64
    assert parsed["foo.tar.gz"] == "c" * 64

    rel = {
        "tag_name": "v0.15.0",
        "html_url": "https://github.com/nghqqa/context-vault/releases/tag/v0.15.0",
        "assets": [
            {
                "name": "ContextVault.exe",
                "browser_download_url": "https://github.com/nghqqa/context-vault/releases/download/v0.15.0/ContextVault.exe",
                "digest": "sha256:" + ("d" * 64),
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://github.com/nghqqa/context-vault/releases/download/v0.15.0/SHA256SUMS.txt",
            },
        ],
    }
    assets = cv_ops.select_release_assets(rel)
    assert assets["version"] == "0.15.0"
    assert assets["exe_digest"] == "d" * 64
    assert assets["checksum_name"] == "SHA256SUMS.txt"

    fake_exe = b"MZ-fake-context-vault-exe-payload-" + (b"x" * 2048)
    real_sha = hashlib.sha256(fake_exe).hexdigest()
    good_sums = f"{real_sha}  ContextVault.exe\n".encode("utf-8")
    bad_sums = f"{'0' * 64}  ContextVault.exe\n".encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            payload_map = {
                "/ContextVault.exe": fake_exe,
                "/SHA256SUMS.txt": good_sums,
                "/BADSUMS.txt": bad_sums,
            }
            data = payload_map.get(self.path)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        dl_dir = tmp_path / "dl"
        ok_res = cv_ops.download_and_verify_exe(
            f"http://127.0.0.1:{port}/ContextVault.exe",
            dl_dir,
            checksum_url=f"http://127.0.0.1:{port}/SHA256SUMS.txt",
            filename="ContextVault-0.15.0.exe.new",
        )
        assert ok_res["ok"] is True and ok_res["sha256"] == real_sha
        assert Path(ok_res["path"]).read_bytes() == fake_exe

        bad_dir = tmp_path / "dl_bad"
        with pytest.raises(ValueError, match="SHA-256"):
            cv_ops.download_and_verify_exe(
                f"http://127.0.0.1:{port}/ContextVault.exe",
                bad_dir,
                checksum_url=f"http://127.0.0.1:{port}/BADSUMS.txt",
                filename="ContextVault-bad.exe.new",
            )
        assert list(bad_dir.glob("*")) == []

        with pytest.raises(ValueError):
            cv_ops.download_and_verify_exe(f"http://127.0.0.1:{port}/ContextVault.exe", dl_dir)

        with pytest.raises(ValueError):
            cv_ops.download_and_verify_exe("http://example.com/ContextVault.exe", dl_dir, expected_sha256=real_sha)
    finally:
        srv.shutdown()
        srv.server_close()

    bat = cv_ops.write_windows_update_script(
        tmp_path / "ContextVault.exe",
        tmp_path / "ContextVault-0.15.0.exe.new",
        pid=4321,
        relaunch=True,
    )
    txt = bat.read_text(encoding="utf-8")
    assert 'set "PID=4321"' in txt
    assert 'move /Y "%STAGED%" "%TARGET%"' in txt
    assert 'start "" "%TARGET%"' in txt
    assert 'del "%~f0"' in txt

    with pytest.raises(ValueError):
        cv_ops.write_windows_update_script(Path(r"E:\bad&path\ContextVault.exe"), tmp_path / "a.exe", pid=1)
