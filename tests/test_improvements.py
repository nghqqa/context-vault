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
        for rel in ("docs/USAGE.md", "README.md", "README.zh.md", "llms.txt"):
            txt = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("file:///", txt, f"{rel} 不应包含本机 file:/// 死链")


if __name__ == "__main__":
    unittest.main()
