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
            self.assertEqual(len(mcp["clients"]), 4)
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


if __name__ == "__main__":
    unittest.main()
