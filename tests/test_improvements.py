"""综合优化回归测试: 记忆 CRUD/置顶、统一 /aml/* 挂载、敏感信息脱敏、cv setup、BM25 冷热等价与打包入口。"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import aml_adapter  # noqa: E402
import cv  # noqa: E402
import cv_index  # noqa: E402
import cv_memory  # noqa: E402


class TestProjectImprovements(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv_imp_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_memory_crud_and_pin_priority(self):
        m1 = cv_memory.memory_add_manual(
            self.tmp, "核心决策：数据库索引统一采用 SQLite WAL", kind="decision", project="core", pinned=False
        )
        m2 = cv_memory.memory_add_manual(
            self.tmp, "架构红线：绝不修改原始会话文件", kind="constraint", project="core", pinned=True
        )
        self.assertFalse(m1["existed"])
        self.assertTrue(m2["pinned"])

        # 幂等去重
        dup = cv_memory.memory_add_manual(
            self.tmp, "核心决策：数据库索引统一采用 SQLite WAL", kind="decision", project="core", pinned=False
        )
        self.assertTrue(dup["existed"])
        self.assertEqual(dup["id"], m1["id"])

        # 速览模式置顶条目排最前
        top = cv_memory.memory_search_api(self.tmp, "", limit=5)
        self.assertEqual(top[0]["id"], m2["id"])
        self.assertTrue(top[0]["pinned"])

        # 切换置顶状态
        cv_memory.memory_set_pin(self.tmp, m2["id"], pinned=False)
        cv_memory.memory_set_pin(self.tmp, m1["id"], pinned=True)
        top2 = cv_memory.memory_search_api(self.tmp, "", limit=5)
        self.assertEqual(top2[0]["id"], m1["id"])

        # 删除单条记忆
        dres = cv_memory.memory_delete(self.tmp, m2["id"])
        self.assertEqual(dres["deleted"], 1)
        left = cv_memory.memory_search_api(self.tmp, "", limit=10)
        self.assertEqual([x["id"] for x in left], [m1["id"]])

        # 非法入参
        with self.assertRaises(ValueError):
            cv_memory.memory_add_manual(self.tmp, "   ")
        with self.assertRaises(ValueError):
            cv_memory.memory_add_manual(self.tmp, "test", kind="unknown_kind")
        with self.assertRaises(ValueError):
            cv_memory.memory_delete(self.tmp, True)

    def test_secret_redaction_and_aml_redact_option(self):
        sample = (
            "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456\n"
            "GH=ghp_1234567890abcdefghij1234567890abcd\n"
            "AWS=AKIAIOSFODNN7EXAMPLE\n"
            "JWT=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAn\n-----END RSA PRIVATE KEY-----\n"
            "正常代码路径: tools/cv.py"
        )
        cleaned, hits = cv.redact_secrets(sample)
        self.assertEqual(hits, 5)
        self.assertIn("[REDACTED:API_KEY]", cleaned)
        self.assertIn("[REDACTED:GITHUB_TOKEN]", cleaned)
        self.assertIn("[REDACTED:AWS_KEY]", cleaned)
        self.assertIn("[REDACTED:JWT]", cleaned)
        self.assertIn("[REDACTED:PRIVATE_KEY]", cleaned)
        self.assertIn("tools/cv.py", cleaned)
        self.assertNotIn("sk-proj-", cleaned)
        self.assertNotIn("ghp_", cleaned)

        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            r = ad.add(
                [{"role": "user", "content": "我的密钥是 sk-proj-abcdefghijklmnopqrstuvwxyz123456 记得脱敏"}],
                session_id="ses_red",
                redact=True,
                extract_memory=False,
            )
            raw = (self.tmp / "snapshots" / f"{r['snapshot_id']}.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-proj-", raw)
            self.assertIn("[REDACTED:API_KEY]", raw)
        finally:
            ad.close()

    def test_unified_serve_memory_crud_and_aml_routes_without_global_mutation(self):
        orig_reuse = ThreadingHTTPServer.allow_reuse_address
        srv, idx = cv.build_vault_server(self.tmp, 0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            self.assertEqual(ThreadingHTTPServer.allow_reuse_address, orig_reuse)

            def post_json(path: str, payload: dict, extra_headers: dict | None = None):
                c = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    hdrs = {"Content-Type": "application/json"}
                    if extra_headers:
                        hdrs.update(extra_headers)
                    c.request("POST", path, body=json.dumps(payload).encode("utf-8"), headers=hdrs)
                    resp = c.getresponse()
                    return resp.status, json.loads(resp.read().decode("utf-8"))
                finally:
                    c.close()

            def get_json(path: str):
                c = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    c.request("GET", path)
                    resp = c.getresponse()
                    return resp.status, json.loads(resp.read().decode("utf-8"))
                finally:
                    c.close()

            # 1) HTTP 记忆 CRUD
            st, created = post_json("/memory/add", {
                "kind": "constraint",
                "project": "demo",
                "content": "严禁跳过单测直接合并主干",
                "pinned": True,
            })
            self.assertEqual(st, 200)
            mid = created["id"]
            st, pin_res = post_json("/memory/pin", {"id": mid, "pinned": False})
            self.assertEqual(st, 200)
            self.assertFalse(pin_res["pinned"])
            st, del_res = post_json("/memory/delete", {"id": mid})
            self.assertEqual(st, 200)
            self.assertEqual(del_res["deleted"], 1)

            # 2) 统一挂载的 /aml/add 与 /aml/search (主库实时同步至 /search + 409 冲突 + 租户隔离)
            st, a1 = post_json("/aml/add", {
                "request_id": "srv_req_1",
                "messages": [{"role": "assistant", "content": "决定：统一服务挂载 AML 路由 sync_marker_9981"}],
            })
            self.assertEqual(st, 200)
            self.assertFalse(a1["idempotent_replay"])

            st, sr_main = get_json("/search?q=sync_marker_9981&limit=5")
            self.assertEqual(st, 200)
            self.assertGreaterEqual(sr_main["total"], 1)

            st, a_conflict = post_json("/aml/add", {
                "request_id": "srv_req_1",
                "messages": [{"role": "assistant", "content": "不同内容触发 409"}],
            })
            self.assertEqual(st, 409)

            # 租户隔离
            st, _ = post_json("/aml/add", {
                "user_id": "tenant_x",
                "messages": [{"role": "assistant", "content": "租户独享密钥 tenant_secret_771"}],
            })
            self.assertEqual(st, 200)
            st, sr_other = post_json("/aml/search", {"user_id": "tenant_y", "query": "tenant_secret_771"})
            self.assertEqual(st, 200)
            self.assertEqual(sr_other["total"], 0)
            st, sr_self = post_json("/aml/search", {"user_id": "tenant_x", "query": "tenant_secret_771"})
            self.assertEqual(st, 200)
            self.assertGreaterEqual(sr_self["total"], 1)
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()
            th.join(timeout=3)

    def test_bm25_smoothing_cold_hot_parity(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            ad.add(
                [{"role": "user", "content": "排查 phoenix-api retry 超时告警，决定采用 exponential_jitter 策略。 " + ("日志分析细节 " * 25)}],
                session_id="ses_real",
                extract_memory=False,
            )
            ad.add(
                [{"role": "user", "content": "phoenix-api retry 巡检"}],
                session_id="ses_short",
                extract_memory=False,
            )
        finally:
            ad.close()

        idx = cv_index.VaultIndex(self.tmp, cv.block_text)
        try:
            hot = idx.search("phoenix-api retry exponential_jitter 什么", 5, now=1800000000.0)
            snaps = []
            for p in sorted((self.tmp / "snapshots").glob("cv_*.json")):
                env = json.loads(p.read_text(encoding="utf-8"))
                env["_sid"] = p.stem
                snaps.append(env)
            cold = cv_index.ColdRanker(snaps, "phoenix-api retry exponential_jitter 什么",
                                       now=1800000000.0).search(5)
            self.assertEqual([(h["sid"], h["score"]) for h in hot["hits"]],
                             [(h["sid"], h["score"]) for h in cold["hits"]])
        finally:
            idx._close()

    def test_pyproject_metadata_and_setup_command(self):
        pyp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dependencies = []', pyp)
        self.assertIn('cv = "cv:main"', pyp)
        self.assertIn('cv-mcp = "mcp_server:main"', pyp)
        self.assertIn('cv-aml = "aml_adapter:main"', pyp)

        # 验证 cv setup 干跑建库路径 (不扫描外部目录时也安全完成)
        orig_sync = cv.sync_vault
        cv.sync_vault = lambda vault, **kw: {"new": 0, "updated": 0, "total": 0}
        try:
            cv.main(["--vault", str(self.tmp), "setup", "--no-ledger"])
            self.assertTrue((self.tmp / "vault.db").exists())
            self.assertTrue((self.tmp / "snapshots").is_dir())
        finally:
            cv.sync_vault = orig_sync

    def test_cli_help_and_docs_hygiene(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(buf):
                cv.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        help_txt = buf.getvalue()
        self.assertIn("cv setup --mcp", help_txt)
        self.assertIn("cv serve --open", help_txt)

        # 文档卫生: 公开文档不得残留本机 file:/// 绝对路径死链
        for rel in ("docs/USAGE.md", "docs/USAGE.en.md", "README.md", "README.zh.md", "llms.txt"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("file:///", txt, f"{rel} 不应包含本机 file:/// 死链")

    def test_serve_fast_path_swr_audit_summary_and_cache_invalidation(self):
        """验证首屏加速路径: /audit?summary_only=1、/ledger/usage?fast=1 非阻塞 SWR、写入即时清缓存。"""
        import os
        import time
        from unittest import mock
        import cv_ledger as cl

        vault = self.tmp / "fast_vault"
        (vault / "snapshots").mkdir(parents=True)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-11-24T10:00:00+08:00", "p1", "https://example.invalid", "ok", 200, 120, "[]", "[]"),
        )
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("2026-11-24T10:05:00+08:00", "p1", "https://example.invalid", "http_error", 502, 50, "[]", '["m1"]'),
        )
        conn.commit()
        conn.close()

        release = threading.Event()

        def slow_remote(token, url=None, timeout=None):
            release.wait(2.0)
            return {"ok": True, "plan": "PRO", "limits": []}

        def slow_agv(timeout=2.0, creds=None):
            release.wait(2.0)
            return {"ok": True, "plan": "AGV", "limits": []}

        def _req(port, method, path, body=None):
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"} if body is not None else {}
            data = json.dumps(body).encode("utf-8") if body is not None else None
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            c.close()
            return r.status, json.loads(raw)

        with mock.patch.dict(os.environ, {"CV_PLAN_TOKEN": "slow-tok"}), \
             mock.patch.object(cl, "_REMOTE_Q", {"at": 0.0, "data": None}), \
             mock.patch.object(cl, "_AGV_Q", {"at": 0.0, "data": None}), \
             mock.patch.object(cl, "_REMOTE_Q_INFLIGHT", False), \
             mock.patch.object(cl, "_AGV_Q_INFLIGHT", False), \
             mock.patch.object(cl, "fetch_remote_quota", side_effect=slow_remote), \
             mock.patch.object(cl, "fetch_agv_quota", side_effect=slow_agv):
            srv, idx = cv.build_vault_server(vault, 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                # 1) /audit?summary_only=1 单 SQL 汇总且不回明细
                code, au = _req(port, "GET", "/audit?summary_only=1")
                self.assertEqual(code, 200)
                self.assertEqual(au["total_events"], 2)
                self.assertEqual(au["summary"]["ok_probes"], 1)
                self.assertEqual(au["summary"]["abnormal"], 1)
                self.assertEqual(au["summary"]["model_missing_events"], 1)
                self.assertEqual(au["timeline"], [])

                # 2) /ledger/usage?window=5h&fast=1 远端慢时立刻返回 quota_pending=True
                t0 = time.perf_counter()
                code, u = _req(port, "GET", "/ledger/usage?window=5h&fast=1")
                dt_ms = (time.perf_counter() - t0) * 1000
                self.assertEqual(code, 200)
                self.assertLess(dt_ms, 500.0)
                self.assertTrue(u.get("quota_pending"))

                # 放行后台刷新后缓存就绪, 再次请求 0ms 拿实时额度
                release.set()
                deadline = time.time() + 2.0
                while time.time() < deadline and (cl._REMOTE_Q["data"] is None or cl._AGV_Q["data"] is None):
                    time.sleep(0.02)
                code, u2 = _req(port, "GET", "/ledger/usage?window=5h&fast=1")
                self.assertEqual(code, 200)
                self.assertFalse(u2.get("quota_pending"))
                self.assertEqual(u2.get("remote_quota", {}).get("plan"), "PRO")
                self.assertEqual(u2.get("remote_quota_agv", {}).get("plan"), "AGV")

                # 3) /ledger 缓存在写入记忆后立即失效
                code, l1 = _req(port, "GET", "/ledger")
                self.assertEqual(code, 200)
                self.assertEqual(l1["memories"], 0)
                code, _ = _req(port, "POST", "/memory/add", {"content": "测试缓存失效", "kind": "decision"})
                self.assertEqual(code, 200)
                code, l2 = _req(port, "GET", "/ledger")
                self.assertEqual(code, 200)
                self.assertEqual(l2["memories"], 1)
            finally:
                release.set()
                srv.shutdown()
                srv.server_close()
                idx._close()

    def test_v090_hardening_gzip_ops_agent_filter_and_log_rotation(self):
        """验证 v0.9.0 加固: POST 413 限流、gzip 响应压缩、日志轮换、/ops/* 运维端点与 /recent?agent= 筛选。"""
        import gzip as _gzip

        vault = self.tmp / "ops_vault"
        (vault / "snapshots").mkdir(parents=True)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        sids = []
        for idx_i, ag in enumerate(("codex", "antigravity")):
            env = cv.build_envelope({
                "capture": {"agent": ag, "source_format": f"{ag}-test", "completeness": "full",
                            "session_ref": f"ses_{idx_i}", "cwd": f"E:/proj_{idx_i}"},
                "conversation": [
                    {"type": "text", "role": "user", "content": f"hello from {ag} session {idx_i}"},
                    {"type": "text", "role": "assistant", "content": f"reply from {ag}"},
                ],
                "stats": cv.zero_stats(),
            })
            sid = cv.snapshot_id(env)
            p = vault / "snapshots" / f"{sid}.json"
            p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
            cv.register_snapshot(conn, env, sid, p)
            sids.append(sid)
        conn.commit()
        conn.close()

        # 日志轮换单元测试
        lf = vault / "rot.log"
        lf.write_bytes(b"x" * 2048)
        cv.rotate_log_if_needed(lf, max_bytes=1024)
        self.assertFalse(lf.exists())
        self.assertTrue((vault / "rot.log.1").is_file())

        srv, idx = cv.build_vault_server(vault, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # 1) gzip 自动压缩 (HTML > 4KB)
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/", headers={"Accept-Encoding": "gzip"})
            r = c.getresponse()
            raw = r.read()
            c.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Encoding"), "gzip")
            self.assertIn("Context Vault", _gzip.decompress(raw).decode("utf-8"))

            # 2) POST 超限返回 413
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/sync", body=b"",
                      headers={"Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)})
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 413)

            # 3) /recent?agent= 精确来源筛选
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/recent?limit=10&agent=antigravity")
            r = c.getresponse()
            d = json.loads(r.read().decode("utf-8"))
            c.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(d["total"], 1)
            self.assertEqual(d["rows"][0]["agent"], "antigravity")

            # 4) /ops/status + /ops/mcp + /ops/backup + /ops/prune + /ops/plan_token
            def _j(method, path, body=None):
                cc = HTTPConnection("127.0.0.1", port, timeout=5)
                hdr = {"Content-Type": "application/json"} if body is not None else {}
                cc.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=hdr)
                rr = cc.getresponse()
                txt = rr.read().decode("utf-8")
                cc.close()
                return rr.status, json.loads(txt)

            code, st = _j("GET", "/ops/status")
            self.assertEqual(code, 200)
            self.assertEqual(st["version"], cv.VERSION)
            code, mcp = _j("GET", "/ops/mcp")
            self.assertEqual(code, 200)
            self.assertEqual(len(mcp["clients"]), 5)
            code, bk = _j("POST", "/ops/backup", {"keep": 5})
            self.assertEqual(code, 200)
            self.assertTrue(bk["ok"] and bk["name"].endswith(".zip"))
            code, pr = _j("POST", "/ops/prune", {"apply": False})
            self.assertEqual(code, 200)
            self.assertEqual(pr["applied"], False)
            code, pt = _j("POST", "/ops/plan_token", {"token": "demo-token-123456", "verify": False})
            self.assertEqual(code, 200)
            self.assertTrue(pt["plan_token_set"])
            code, pt2 = _j("POST", "/ops/plan_token", {"clear": True})
            self.assertEqual(code, 200)
            self.assertFalse(pt2["plan_token_set"])

            # 5) /migrate target=markdown 返回 content 供一键复制
            code, mg = _j("POST", "/migrate", {"id": sids[0], "target": "markdown", "apply": False, "budget_tokens": 2000})
            self.assertEqual(code, 200)
            self.assertIn("content", mg)
            self.assertIn("hello from codex", mg["content"])

            # 6) /stats 两次连续命中缓存 (owned_stats 只计算 1 次), 写入清缓存后再重算
            from unittest import mock
            orig_owned = cv.owned_stats
            calls = [0]

            def counted_owned(v):
                calls[0] += 1
                return orig_owned(v)

            with mock.patch.object(cv, "owned_stats", side_effect=counted_owned):
                code1, s1 = _j("GET", "/stats")
                code2, s2 = _j("GET", "/stats")
                self.assertEqual((code1, code2), (200, 200))
                self.assertEqual(s1, s2)
                self.assertLessEqual(calls[0], 1)

            # 7) cv_core.db() 对已建表库命中 _MIGRATED_DBS 缓存; 无表空库不误标已迁移
            import cv_core
            cv_core.reset_db_cache(vault)
            c1 = cv_core.db(vault)
            c1.close()
            import os as _os
            vdb_key = _os.path.normcase(_os.path.abspath(str(vault / "vault.db")))
            self.assertTrue(any(k[0] == vdb_key for k in cv_core._MIGRATED_DBS))
            empty_v = self.tmp / "empty_db_cache_test"
            empty_v.mkdir()
            c_empty = cv_core.db(empty_v)
            c_empty.close()
            ev_key = _os.path.normcase(_os.path.abspath(str(empty_v / "vault.db")))
            self.assertFalse(any(k[0] == ev_key for k in cv_core._MIGRATED_DBS))
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()

    def test_mcp_selective_install_residual_and_uninstall(self):
        """验证真实主程序探测、卸载残留跳过、自选接入与 5 端安全移除 (含 HTTP API)。"""
        from unittest import mock

        fake_home = self.tmp / "home"
        fake_home.mkdir()
        vault = self.tmp / "vault"
        vault.mkdir()

        # Cursor 仅有卸载残留目录 (~/.cursor/mcp.json), 无 Cursor 主程序; Codex 有真实 CLI, 尚无 ~/.codex
        cur_cfg = fake_home / ".cursor" / "mcp.json"
        cur_cfg.parent.mkdir(parents=True)
        cur_cfg.write_text(json.dumps({
            "mcpServers": {
                "other-mcp": {"command": "npx", "args": ["other"]},
                "context-vault": {"command": "python", "args": ["old.py"]},
            }
        }, indent=2), encoding="utf-8")

        codex_bin = fake_home / ".local" / "bin" / ("codex.exe" if sys.platform.startswith("win") else "codex")
        codex_bin.parent.mkdir(parents=True)
        codex_bin.write_text("", encoding="utf-8")

        with mock.patch.object(cv, "HOME", fake_home):
            st = cv.mcp_status_api(vault)
            by_id = {c["id"]: c for c in st["clients"]}
            self.assertTrue(by_id["cursor"]["detected"])
            self.assertFalse(by_id["cursor"]["app_detected"])
            self.assertTrue(by_id["cursor"]["residual"])
            self.assertTrue(by_id["cursor"]["installed"])
            self.assertTrue(by_id["codex"]["detected"])
            self.assertTrue(by_id["codex"]["app_detected"])
            self.assertFalse(by_id["codex"]["residual"])
            self.assertFalse(by_id["claude"]["detected"])

            # client='all' 自动跳过残留目录, 只为真实检测到的 codex 创建 config.toml 并写入
            res_all = cv.mcp_install_api(vault, client="all")
            msg_map = {x["client"]: x["message"] for x in res_all["results"]}
            self.assertIn("已跳过", msg_map["cursor"])
            self.assertIn("~/.codex/config.toml", msg_map["codex"])
            self.assertTrue((fake_home / ".codex" / "config.toml").is_file())

            # 安全移除 cursor 中的 context-vault, 保留 other-mcp 并生成 .json.bak-cv
            res_un = cv.mcp_uninstall_api(vault, client="cursor")
            self.assertIn("移除 context-vault", res_un["results"][0]["message"])
            cur_after = json.loads(cur_cfg.read_text(encoding="utf-8"))
            self.assertIn("other-mcp", cur_after["mcpServers"])
            self.assertNotIn("context-vault", cur_after["mcpServers"])
            self.assertTrue(cur_cfg.with_suffix(".json.bak-cv").is_file())

            # Codex / Antigravity / Claude 卸载往返 (保留同文件其他服务器与表段)
            codex_cfg = fake_home / ".codex" / "config.toml"
            codex_cfg.write_text(
                'model = "gpt-5"\n\n'
                '[mcp_servers.other]\ncommand = "npx"\nargs = ["x"]\n\n'
                '[mcp_servers.context-vault]\ncommand = "python"\nargs = ["mcp_server.py"]\n\n'
                '[mcp_servers.context-vault.env]\nFOO = "1"\n\n'
                '[projects."D:\\\\proj"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            cv.mcp_uninstall_api(vault, client="codex")
            codex_after = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.other]", codex_after)
            self.assertIn('[projects."D:\\\\proj"]', codex_after)
            self.assertNotIn("context-vault", codex_after)

            for sub in ("config", "antigravity"):
                ap = fake_home / ".gemini" / sub / "mcp_config.json"
                ap.parent.mkdir(parents=True, exist_ok=True)
                ap.write_text(json.dumps({
                    "mcpServers": {"keep": {"command": "k"}, "context-vault": {"command": "py"}}
                }), encoding="utf-8")
            cv.mcp_uninstall_api(vault, client="antigravity")
            for sub in ("config", "antigravity"):
                ap = fake_home / ".gemini" / sub / "mcp_config.json"
                d = json.loads(ap.read_text(encoding="utf-8"))
                self.assertIn("keep", d["mcpServers"])
                self.assertNotIn("context-vault", d["mcpServers"])

            cl_cfg = fake_home / ".claude.json"
            cl_cfg.write_text(json.dumps({"mcpServers": {"k": {}, "context-vault": {}}}), encoding="utf-8")
            cv.mcp_uninstall_api(vault, client="claude")
            self.assertNotIn("context-vault", json.loads(cl_cfg.read_text(encoding="utf-8"))["mcpServers"])
            self.assertIn("k", json.loads(cl_cfg.read_text(encoding="utf-8"))["mcpServers"])

            # 验证 cv_skills.source_summary 同步返回 app_detected, 供前端隐藏 0 Skill 未安装源
            import cv_skills
            with mock.patch.dict("os.environ", {"DSH_HOME": str(fake_home / ".dsh")}):
                sk_sum = {x["agent"]: x for x in cv_skills.source_summary(use_cache=False)}
            self.assertIs(sk_sum["cursor"]["app_detected"], False)
            self.assertIs(sk_sum["codex"]["app_detected"], True)

            # HTTP 路由 /ops/mcp/install (clients 数组 + 空数组 400) 与 /ops/mcp/uninstall (缺参 400 + 正常移除)
            (vault / "snapshots").mkdir(exist_ok=True)
            srv, idx = cv.build_vault_server(vault, 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def _post(path, body):
                    cc = HTTPConnection("127.0.0.1", port, timeout=5)
                    cc.request("POST", path, body=json.dumps(body).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
                    rr = cc.getresponse()
                    txt = rr.read().decode("utf-8")
                    cc.close()
                    return rr.status, json.loads(txt)

                c_err, _ = _post("/ops/mcp/install", {"clients": []})
                self.assertEqual(c_err, 400)
                c_ok, r_ok = _post("/ops/mcp/install", {"clients": ["cursor"]})
                self.assertEqual(c_ok, 200)
                self.assertTrue(next(x for x in r_ok["status"]["clients"] if x["id"] == "cursor")["installed"])

                u_err, _ = _post("/ops/mcp/uninstall", {})
                self.assertEqual(u_err, 400)
                u_ok, r_un = _post("/ops/mcp/uninstall", {"client": "cursor"})
                self.assertEqual(u_ok, 200)
                self.assertFalse(next(x for x in r_un["status"]["clients"] if x["id"] == "cursor")["installed"])

                # Skill 盘点/迁移与已接入 MCP 后端对齐 (connected_only=1 过滤未接入与 0 Skill 源)
                def _get(path):
                    cc = HTTPConnection("127.0.0.1", port, timeout=5)
                    cc.request("GET", path)
                    rr = cc.getresponse()
                    txt = rr.read().decode("utf-8")
                    cc.close()
                    return rr.status, json.loads(txt)

                (fake_home / ".cursor" / "skills" / "cur-sk").mkdir(parents=True)
                (fake_home / ".cursor" / "skills" / "cur-sk" / "SKILL.md").write_text(
                    "---\nname: cur-sk\ndescription: c\n---\nhi\n", encoding="utf-8"
                )
                dsh_sk = fake_home / ".dsh" / "skills" / "dsh-sk" / "SKILL.md"
                dsh_sk.parent.mkdir(parents=True)
                dsh_sk.write_text("---\nname: dsh-sk\ndescription: d\n---\nhi\n", encoding="utf-8")
                with mock.patch.dict("os.environ", {"DSH_HOME": str(fake_home / ".dsh")}):
                    # codex 在上面卸载测试中被摘除, 此处重新接入 codex (0 Skill) 与 dsh (1 Skill), cursor 保持未接入
                    _post("/ops/mcp/install", {"clients": ["codex", "dsh"]})
                    cv_skills.clear_cache()
                    sc, skl = _get("/skills/list?source=cursor&target=cursor&connected_only=1&refresh=1")
                    self.assertEqual(sc, 200)
                    self.assertIn("dsh", skl["connected_agents"])
                    self.assertIn("codex", skl["connected_agents"])
                    self.assertNotIn("cursor", skl["connected_agents"])
                    src_ids = [x["agent"] for x in skl["sources"]]
                    self.assertEqual(src_ids, ["dsh"])  # codex 0 Skill 不显示, cursor 未接入不显示
                    self.assertEqual(skl["source"], "dsh")
                    self.assertNotEqual(skl["target"], "cursor")

                    bad_src, _ = _post("/skills/migrate", {"source": "cursor", "target": "dsh", "apply": False, "connected_only": True})
                    self.assertEqual(bad_src, 400)
                    bad_tgt, _ = _post("/skills/migrate", {"source": "dsh", "target": "cursor", "apply": False, "connected_only": True})
                    self.assertEqual(bad_tgt, 400)
                    ok_mig, r_mig = _post("/skills/migrate", {"source": "dsh", "target": "codex", "apply": False, "connected_only": True})
                    self.assertEqual(ok_mig, 200)
                    self.assertEqual(r_mig["counts"]["selected"], 1)

                    _post("/ops/mcp/uninstall", {"client": "dsh"})
                    sc2, skl2 = _get("/skills/list?source=dsh&target=codex&connected_only=1&refresh=1")
                    self.assertEqual(sc2, 200)
                    self.assertNotIn("dsh", skl2["connected_agents"])
                    self.assertEqual(skl2["sources"], [])
                    self.assertEqual(skl2["total"], 0)
            finally:
                srv.shutdown()
                srv.server_close()
                idx._close()

    def test_http_guard_ipv6_security_headers_and_post_reject_close(self):
        vault = self.tmp / "vault_sec"
        (vault / "snapshots").mkdir(parents=True)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        conn.commit()
        conn.close()

        srv, idx = cv.build_vault_server(vault, 0, token="sec-tok")
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # 1) IPv6 [::1] Host 允许通过，响应包含 3 项安全头
            c1 = HTTPConnection("127.0.0.1", port, timeout=5)
            c1.putrequest("GET", "/healthz", skip_host=True)
            c1.putheader("Host", f"[::1]:{port}")
            c1.putheader("X-Vault-Token", "sec-tok")
            c1.endheaders()
            r1 = c1.getresponse()
            r1.read()
            self.assertEqual(r1.status, 200)
            self.assertEqual(r1.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r1.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(r1.getheader("Referrer-Policy"), "no-referrer")
            c1.close()

            # 2) 未携带有效 token 的 POST 必须在读 body 前拒绝并带 Connection: close
            c2 = HTTPConnection("127.0.0.1", port, timeout=5)
            c2.request("POST", "/sync", body=b'{"debounce":true}',
                       headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"})
            r2 = c2.getresponse()
            r2.read()
            self.assertEqual(r2.status, 401)
            self.assertEqual((r2.getheader("Connection") or "").lower(), "close")
            c2.close()
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()

    def test_backup_vault_api_quiet_archives_configs_and_keeps_limit(self):
        import io
        import time
        import zipfile
        vault = self.tmp / "vault_bk"
        (vault / "snapshots").mkdir(parents=True)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        conn.commit()
        conn.close()
        for fname, txt in (
            ("accounts.json", '{"accounts":[]}\n'),
            ("migrations.jsonl", '{"dest":"x"}\n'),
            ("pricing.json", '{"custom":{}}\n'),
        ):
            (vault / fname).write_text(txt, encoding="utf-8")

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            for idx in range(3):
                if idx:
                    time.sleep(0.01)
                res = cv.backup_vault_api(vault, keep=2)
                self.assertTrue(res["ok"])
        self.assertEqual(buf.getvalue(), "")
        zips = sorted((vault / "backups").glob("cv-backup-*.zip"))
        self.assertEqual(len(zips), 2)
        with zipfile.ZipFile(zips[-1], "r") as zf:
            names = set(zf.namelist())
        self.assertIn("vault.db", names)
        self.assertIn("accounts.json", names)
        self.assertIn("migrations.jsonl", names)
        self.assertIn("pricing.json", names)

    def test_dashboard_and_toast_html_escaping_and_reexports(self):
        import importlib.util
        from types import SimpleNamespace
        import aml_adapter
        import cv_antigravity
        import cv_antigravity_pb
        import cv_core
        import cv_dsh
        import cv_dsh_codec
        import cv_export
        import cv_index
        import cv_ledger
        import cv_ledger_cli
        import cv_ledger_collect
        import cv_mcp
        import cv_ops
        import cv_projects
        import cv_rank
        import cv_skills
        import cv_skills_ops
        import cv_sync
        import cv_ui

        self.assertIs(cv.mcp_install_api, cv_mcp.mcp_install_api)
        self.assertIs(cv.sync_vault, cv_sync.sync_vault)
        self.assertIs(cv_ledger.sync_usage_events, cv_ledger_collect.sync_usage_events)
        self.assertIs(cv.export_claude_code, cv_export.export_claude_code)
        self.assertIs(cv.resolve_top_conversations, cv_projects.resolve_top_conversations)
        self.assertIs(cv.cmd_ledger_stats, cv_ledger_cli.cmd_ledger_stats)
        self.assertIs(cv.cmd_fork, cv_ops.cmd_fork)
        self.assertIs(cv.ColdRanker, cv_rank.ColdRanker)
        self.assertIs(cv_index.ColdRanker, cv_rank.ColdRanker)
        self.assertIs(cv_skills.migrate_skills, cv_skills_ops.migrate_skills)
        self.assertIs(cv_antigravity.export_antigravity_db, cv_antigravity_pb.export_antigravity_db)
        self.assertIs(cv.export_antigravity_db, cv_antigravity_pb.export_antigravity_db)
        self.assertIs(cv_dsh.zstd_backend, cv_dsh_codec.zstd_backend)
        self.assertIsNone(importlib.util.find_spec("cv_dense"))
        self.assertTrue(issubclass(aml_adapter._AMLHTTPServer, cv_core.LocalThreadingHTTPServer))
        self.assertEqual(cv_core.user_agent(), f"ContextVault/{cv.VERSION}")

        console_html = cv_ui.load_console_html()
        self.assertNotIn("<!--__MIG_", console_html)
        for token in ('id="tgt-cards"', 'id="mig-budget"', 'id="mb-tgt-cards"', 'id="mb-budget"'):
            self.assertIn(token, console_html)

        vault = self.tmp / "vault_xss"
        (vault / "snapshots").mkdir(parents=True)
        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        conn.execute(
            "INSERT INTO probe_events(ts,profile,endpoint,status,http_code,latency_ms,models,missing_models)"
            " VALUES('2026-11-24T00:00:00Z','<script>alert(1)</script>','http://x','err',500,10,'[]','<img src=x>')"
        )
        conn.commit()
        conn.close()
        out_html = self.tmp / "dash.html"
        with mock.patch("cv_ops.webbrowser.open") as m_open, mock.patch("sys.stdout"):
            cv_ops.cmd_dashboard(SimpleNamespace(vault=str(vault), out=str(out_html), open=True))
        m_open.assert_called_once()
        self.assertTrue(m_open.call_args[0][0].startswith("file:///"))
        html_txt = out_html.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html_txt)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_txt)

        with mock.patch("cv_ops.subprocess.run") as m_run:
            cv_ops.toast("T'&<x>", "M'&<y>")
        ps_cmd = m_run.call_args[0][0][-1]
        self.assertIn("T&#x27;&amp;&lt;x&gt;", ps_cmd)
        self.assertIn("M&#x27;&amp;&lt;y&gt;", ps_cmd)

    def test_batch_session_migration_plan_run_http_and_cli(self):
        """验证整端全量 / 批量会话迁移: plan 盘点、Claude 多子类型聚合、去重、单条容错、HTTP 与 CLI --agent 批量迁移。"""
        import io
        from types import SimpleNamespace

        vault = self.tmp / "batch_vault"
        (vault / "snapshots").mkdir(parents=True)
        fake_home = self.tmp / "batch_home"
        fake_codex = fake_home / ".codex" / "sessions"
        fake_claude = fake_home / ".claude" / "projects"
        fake_zdb = fake_home / ".zcode" / "db.sqlite"
        fake_ztdb = fake_home / ".zcode" / "tasks.sqlite"
        fake_codex.mkdir(parents=True)
        fake_claude.mkdir(parents=True)
        fake_zdb.parent.mkdir(parents=True)

        conn = cv.db(vault)
        conn.executescript(cv.SCHEMA)
        samples = [
            ("z1", "zcode", "E:/proj_alpha", "zcode alpha task", "2026-11-20T10:00:00"),
            ("z_bad", "zcode", "E:/proj_beta", "zcode broken task", "2026-11-20T09:00:00"),
            ("cc1", "claude-code", "E:/proj_alpha", "claude code fix", "2026-11-20T11:00:00"),
            ("ct1", "claude-transcript", "E:/proj_beta", "claude transcript note", "2026-11-20T08:00:00"),
            ("cx1", "codex", "E:/proj_alpha", "codex existing session", "2026-11-20T07:00:00"),
            ("dh1", "dsh", "E:/proj_alpha", "dsh test session", "2026-11-20T06:00:00"),
        ]
        sids = {}
        for key, ag, cwd, prompt, ts in samples:
            env = cv.build_envelope({
                "capture": {
                    "agent": ag,
                    "source_format": f"{ag}-test",
                    "completeness": "full",
                    "session_ref": f"ses_{key}",
                    "cwd": cwd,
                },
                "conversation": [
                    {"type": "text", "role": "user", "content": prompt, "ts": ts},
                    {"type": "text", "role": "assistant", "content": f"done {prompt}", "ts": ts},
                ],
                "stats": cv.zero_stats(),
                "created_at": ts,
            })
            sid = cv.snapshot_id(env)
            p = vault / "snapshots" / f"{sid}.json"
            p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
            cv.register_snapshot(conn, env, sid, p)
            sids[key] = sid
        conn.commit()
        conn.close()

        with (
            mock.patch.object(cv, "HOME", fake_home),
            mock.patch.object(cv, "CODEX_SESSIONS", fake_codex),
            mock.patch.object(cv, "CLAUDE_PROJECTS", fake_claude),
            mock.patch.object(cv, "ZCODE_DB", fake_zdb),
            mock.patch.object(cv, "ZCODE_TASKS_DB", fake_ztdb),
        ):
            # 1) plan_batch_migration: claude 自动聚合 claude-code + claude-transcript, codex 标为同源跳过
            plan_cl = cv.plan_batch_migration(vault, source="claude", target="codex")
            self.assertEqual(plan_cl["source"], "claude")
            self.assertEqual(plan_cl["counts"]["total"], 2)
            self.assertEqual(plan_cl["counts"]["ready"], 2)
            self.assertEqual({it["id"] for it in plan_cl["items"]}, {sids["cc1"], sids["ct1"]})
            src_agents = {x["agent"] for x in plan_cl["sources"]}
            self.assertEqual(src_agents, {"zcode", "claude", "codex", "dsh"})

            plan_all = cv.plan_batch_migration(vault, source="all", target="codex")
            by_id = {it["id"]: it for it in plan_all["items"]}
            self.assertTrue(by_id[sids["cx1"]]["same_origin"])
            self.assertEqual(by_id[sids["cx1"]]["status"], "skip_same_origin")

            plan_proj = cv.plan_batch_migration(vault, source="all", target="codex", project="proj_alpha", query="dsh")
            self.assertEqual([it["id"] for it in plan_proj["items"]], [sids["dh1"]])

            # 2) dry-run outbox vs apply 写入 + migrations.jsonl 去重 + --no-skip-migrated 重跑
            dry = cv.run_batch_migration(vault, source="claude", target="codex", apply=False)
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["counts"]["migrated"], 2)
            self.assertTrue(all(it["action"] == "outbox" and Path(it["dest"]).is_file() for it in dry["items"]))
            self.assertFalse((vault / "migrations.jsonl").exists())

            applied = cv.run_batch_migration(vault, source="claude", target="codex", apply=True)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["counts"]["migrated"], 2)
            for it in applied["items"]:
                self.assertEqual(it["action"], "migrated")
                self.assertTrue(Path(it["dest"]).is_file())
                self.assertTrue(str(it["dest"]).startswith(str(fake_codex)))

            mig_lines = [json.loads(x) for x in (vault / "migrations.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(mig_lines), 2)
            self.assertTrue(all(m.get("target") == "codex" for m in mig_lines))

            dedup = cv.run_batch_migration(vault, source="claude", target="codex", skip_migrated=True, apply=False)
            self.assertEqual(dedup["counts"]["migrated"], 0)
            self.assertEqual(dedup["counts"]["skipped_migrated"], 2)

            rerun = cv.run_batch_migration(vault, source="claude", target="codex", skip_migrated=False, apply=False)
            self.assertEqual(rerun["counts"]["migrated"], 2)

            # 3) markdown 目标强制禁止 apply 语义 + selected 指定子集混合同源与非同源
            md_res = cv.run_batch_migration(vault, source="all", target="markdown", apply=True, limit=2)
            self.assertFalse(md_res["applied"])
            self.assertEqual(md_res["counts"]["migrated"], 2)
            self.assertTrue(all(Path(it["dest"]).name.startswith("brief-") for it in md_res["items"]))

            sel_res = cv.run_batch_migration(vault, source="all", target="codex", selected=[sids["z1"], sids["cx1"]], apply=False)
            act_by_id = {it["id"]: it["action"] for it in sel_res["items"]}
            self.assertEqual(act_by_id[sids["z1"]], "outbox")
            self.assertEqual(act_by_id[sids["cx1"]], "skipped_same_origin")

            # 4) 单条损坏容错: z_bad 损坏不影响 z1 完成
            bad_p = vault / "snapshots" / f"{sids['z_bad']}.json"
            bad_orig = bad_p.read_text(encoding="utf-8")
            bad_p.write_text("{corrupted-json", encoding="utf-8")
            try:
                err_res = cv.run_batch_migration(vault, source="zcode", target="codex", apply=False)
                self.assertEqual(err_res["counts"]["migrated"], 1)
                self.assertEqual(err_res["counts"]["failed"], 1)
                bad_row = next(it for it in err_res["items"] if it["id"] == sids["z_bad"])
                self.assertEqual(bad_row["action"], "failed")
                self.assertTrue(bad_row.get("error"))
            finally:
                bad_p.write_text(bad_orig, encoding="utf-8")

            # 5) HTTP GET/POST /migrate/batch + 400 参数校验 + CLI cmd_import --agent 无 --project
            srv, idx = cv.build_vault_server(vault, 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def _get(path):
                    cc = HTTPConnection("127.0.0.1", port, timeout=5)
                    cc.request("GET", path)
                    rr = cc.getresponse()
                    txt = rr.read().decode("utf-8")
                    cc.close()
                    return rr.status, json.loads(txt)

                def _post(path, body):
                    cc = HTTPConnection("127.0.0.1", port, timeout=5)
                    cc.request("POST", path, body=json.dumps(body).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
                    rr = cc.getresponse()
                    txt = rr.read().decode("utf-8")
                    cc.close()
                    return rr.status, json.loads(txt)

                st_g, body_g = _get("/migrate/batch?source=claude&target=codex")
                self.assertEqual(st_g, 200)
                self.assertEqual(body_g["counts"]["skip_migrated"], 2)

                st_gb, _ = _get("/migrate/batch?target=no_such_target")
                self.assertEqual(st_gb, 400)

                st_p, body_p = _post("/migrate/batch", {"source": "dsh", "target": "codex", "apply": False})
                self.assertEqual(st_p, 200)
                self.assertEqual(body_p["counts"]["migrated"], 1)

                st_pb1, _ = _post("/migrate/batch", {"target": "no_such_target"})
                self.assertEqual(st_pb1, 400)
                st_pb2, _ = _post("/migrate/batch", {"selected": ["bad_sid"]})
                self.assertEqual(st_pb2, 400)
                st_pb3, _ = _post("/migrate/batch", {"selected": []})
                self.assertEqual(st_pb3, 400)
            finally:
                srv.shutdown()
                srv.server_close()
                idx._close()

            buf = io.StringIO()
            cli_args = SimpleNamespace(
                vault=str(vault),
                snapshot_id=None,
                project=None,
                agent="claude",
                target="markdown",
                apply=False,
                strip_thinking=False,
                budget_tokens=None,
                include_archived=False,
                include_subagents=False,
                no_skip_migrated=False,
                limit=0,
            )
            with mock.patch("sys.stdout", buf):
                cv.cmd_import(cli_args)
            self.assertIn("整端批量迁移 [claude -> markdown]", buf.getvalue())
            self.assertIn("成功 2", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
