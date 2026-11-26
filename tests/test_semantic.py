"""语义增强层与 cv.py 模块化拆分回归测试: Ollama 配置、LLM 记忆精炼、自动会话标题与 HTTP /semantic/*。"""
from __future__ import annotations

from http.client import HTTPConnection
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
import cv_memory  # noqa: E402
import cv_semantic  # noqa: E402


class _FakeLLM(cv_semantic.LocalLLMClient):
    def __init__(self, reply: str):
        super().__init__(url="http://127.0.0.1:11434/v1/chat/completions", model="fake-qwen")
        self._reply = reply
        self.calls = 0

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
        self.calls += 1
        return self._reply


class TestSemanticAndModularization(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv_sem_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ai_config_persistence_and_dense_provider_fallback(self):
        # 初始未配置时, DenseProvider 与 LocalLLMClient 均关闭且不发网络请求
        self.assertFalse(cv_semantic.DenseProvider(vault=self.tmp).available)
        self.assertFalse(cv_semantic.LocalLLMClient(vault=self.tmp).available)

        # 保存配置到 vault/ai_config.json
        cv_semantic.save_ai_config(self.tmp, {
            "embeddings_url": "http://127.0.0.1:11434/v1/embeddings",
            "embeddings_model": "nomic-embed-text",
            "llm_url": "http://127.0.0.1:11434/v1/chat/completions",
            "llm_model": "qwen2.5:7b",
            "llm_key": "secret-should-not-leak",
        })
        dp = cv_semantic.DenseProvider(vault=self.tmp)
        self.assertTrue(dp.available)
        self.assertEqual(dp.model, "nomic-embed-text")

        st = cv_semantic.semantic_status(self.tmp, probe=False)
        self.assertTrue(st["embeddings"]["enabled"])
        self.assertTrue(st["llm"]["enabled"])
        self.assertTrue(st["llm"]["has_key"])
        # 确保密钥不明文泄露在状态输出中
        self.assertNotIn("secret-should-not-leak", json.dumps(st))

    def test_llm_memory_refinement_and_idempotency(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            r = ad.add(
                [
                    {"role": "user", "content": "我们排查了半天发现 Windows 下 SQLite 锁是因为没关句柄，以后记得用 finally 释放。"},
                    {"role": "assistant", "content": "好的，另外咱们把 API 限流窗口统一改成 5 小时滚动窗口吧。"},
                ],
                session_id="ses_implicit",
                project="vault-core",
                extract_memory=False,
            )
        finally:
            ad.close()

        fake_reply = (
            '```json\n'
            '[{"kind":"pitfall","content":"Windows 下未在 finally 关闭 SQLite 连接会导致临时目录清理锁文件"},'
            ' {"kind":"decision","content":"API 限流窗口统一采用 5 小时滚动窗口"},'
            ' {"kind":"invalid_kind","content":"应被过滤"}]\n'
            '```'
        )
        llm = _FakeLLM(fake_reply)
        res1 = cv_semantic.refine_memories_with_llm(self.tmp, limit=5, client=llm)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["scanned"], 1)
        self.assertEqual(res1["added"], 2)

        # 再次运行应命中 llm-refine 水位, 不重复调用 LLM
        res2 = cv_semantic.refine_memories_with_llm(self.tmp, limit=5, client=llm)
        self.assertEqual(res2["scanned"], 0)
        self.assertEqual(llm.calls, 1)

        mems = cv_memory.memory_search_api(self.tmp, "滚动窗口", limit=5)
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0]["snapshot"], r["snapshot_id"])

    def test_session_title_heuristic_and_llm_generation(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            r = ad.add(
                [
                    {"role": "user", "content": "看一下这个项目"},
                    {"role": "assistant", "content": "已分析仓库。"},
                    {"role": "user", "content": "请帮我重构 tools/cv.py 并优化 SQLite 检索排序逻辑"},
                ],
                session_id="ses_low_title",
                project="context-vault",
                extract_memory=False,
            )
        finally:
            ad.close()

        # 1) 零依赖启发式清洗低信息量标题
        self.assertTrue(cv_semantic.is_low_info_title("看一下这个项目"))
        res_h = cv_semantic.generate_session_titles(self.tmp, limit=10, use_llm=False)
        self.assertEqual(res_h["updated"], 1)
        self.assertEqual(res_h["heuristic_used"], 1)

        conn = cv.db(self.tmp)
        try:
            title_h = conn.execute("SELECT summary_title FROM snapshots WHERE id=?", (r["snapshot_id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("tools/cv.py", title_h)

        # 2) LLM 标题生成 (--force 覆盖)
        llm = _FakeLLM("重构 cv.py 与 SQLite 检索排序")
        res_l = cv_semantic.generate_session_titles(self.tmp, limit=10, force=True, client=llm)
        self.assertEqual(res_l["llm_used"], 1)
        conn = cv.db(self.tmp)
        try:
            title_l = conn.execute("SELECT summary_title FROM snapshots WHERE id=?", (r["snapshot_id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(title_l, "重构 cv.py 与 SQLite 检索排序")

    def test_http_semantic_endpoints_and_modular_reexports(self):
        # 确认拆分后的全部子模块与 cv.py re-export 一致
        for sym in ("build_vault_server", "cmd_serve", "cmd_probe", "cmd_warm", "cmd_dashboard",
                    "cmd_import", "cmd_migrate", "compress_brief", "cmd_ingest",
                    "sync_antigravity_archived", "cmd_semantic"):
            self.assertTrue(hasattr(cv, sym), f"cv.{sym} 缺失")

        srv, idx = cv.build_vault_server(self.tmp, 0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            def call(method: str, path: str, body: dict | None = None):
                c = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    if body is not None:
                        c.request(method, path, body=json.dumps(body).encode("utf-8"),
                                  headers={"Content-Type": "application/json"})
                    else:
                        c.request(method, path)
                    resp = c.getresponse()
                    return resp.status, json.loads(resp.read().decode("utf-8"))
                finally:
                    c.close()

            st, status_data = call("GET", "/semantic/status")
            self.assertEqual(st, 200)
            self.assertFalse(status_data["embeddings"]["enabled"])

            st, cfg_data = call("POST", "/semantic/config", {
                "embeddings_url": "http://127.0.0.1:11434/v1/embeddings",
                "embeddings_model": "nomic-embed-text",
            })
            self.assertEqual(st, 200)
            self.assertTrue(cfg_data["embeddings"]["enabled"])

            st, titles_res = call("POST", "/semantic/titles", {"limit": 5, "use_llm": False})
            self.assertEqual(st, 200)
            self.assertIn("updated", titles_res)
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()
            th.join(timeout=3)

    def test_native_antigravity_db_export_handles_empty_tool_result_and_wal(self):
        import sqlite3
        import cv_antigravity as ag

        dbp = self.tmp / "11112222-3333-4444-5555-666677778888.db"
        conn = sqlite3.connect(dbp)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE trajectory_meta(trajectory_id TEXT, cascade_id TEXT)")
            conn.execute("CREATE TABLE trajectory_metadata_blob(id TEXT, data BLOB)")
            conn.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB, step_payload BLOB)")
            conn.execute("INSERT INTO trajectory_meta VALUES('traj1', '11112222-3333-4444-5555-666677778888')")
            # trajectory_metadata_blob: f1={f1:'file:///e:/demo-repo'}, f2={f1:1795300000}
            meta_blob = ag._pb_len(1, ag._pb_len(1, b"file:///e:/demo-repo")) + ag._pb_len(2, ag._pb_var(1, 1795300000))
            conn.execute("INSERT INTO trajectory_metadata_blob VALUES('main', ?)", (meta_blob,))
            # step 0: user input (type 14)
            ts_md = ag._pb_len(1, ag._pb_var(1, 1795300010))
            u_payload = ag._pb_len(19, ag._pb_len(2, "帮我检查原生 Antigravity 同步".encode("utf-8")))
            conn.execute("INSERT INTO steps VALUES(0, 14, 3, ?, ?)", (ts_md, u_payload))
            # step 1: planner reply + tool_call (type 15)
            tc = ag._pb_len(1, b"call_1") + ag._pb_len(2, b"read_file") + ag._pb_len(3, b'{"Path":"a.py"}')
            p_payload = ag._pb_len(20, ag._pb_len(3, b"thinking...") + ag._pb_len(1, "已定位 WAL 同步问题".encode("utf-8")) + ag._pb_len(7, tc))
            conn.execute("INSERT INTO steps VALUES(1, 15, 3, ?, ?)", (ts_md, p_payload))
            # step 2: generic tool result with MISSING text (smigrate 曾在此报错崩溃)
            t_md = ts_md + ag._pb_len(4, ag._pb_len(1, b"call_1") + ag._pb_len(2, b"read_file"))
            t_payload = ag._pb_len(140, ag._pb_len(2, b""))
            conn.execute("INSERT INTO steps VALUES(2, 132, 3, ?, ?)", (t_md, t_payload))
            conn.commit()
        finally:
            conn.close()

        exp = ag.export_antigravity_db(dbp)
        self.assertEqual(exp["capture"]["agent"], "antigravity")
        self.assertEqual(exp["capture"]["cwd"], r"E:\demo-repo")
        self.assertEqual(exp["capture"]["completeness"], "full")
        types = [b["type"] for b in exp["conversation"]]
        self.assertEqual(types, ["text", "thinking", "text", "tool_call", "tool_result"])
        self.assertIn("Antigravity", exp["conversation"][0]["content"])
        self.assertIn("empty/binary", exp["conversation"][4]["output"])


if __name__ == "__main__":
    unittest.main()
