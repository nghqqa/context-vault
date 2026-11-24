"""AML 借鉴特性回归测试: AMLMemoryAdapter (Add/Search) + HTTP 边界 + MCP budget_tokens 预算裁剪。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import aml_adapter  # noqa: E402
import cv  # noqa: E402
import mcp_server  # noqa: E402


class TestAMLFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv_aml_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aml_adapter_add_search_id_guard_and_budget(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            r1 = ad.add(
                [
                    {"role": "user", "content": "我们需要把检索缓存从 pickle 迁移到 SQLite 倒排索引。"},
                    {"role": "assistant", "content": "决定采用 FTS5 与 zlib 压缩倒排索引 (`search_index.db`)，禁止使用巨大 pickle 缓存。"},
                ],
                session_id="ses_1",
                project="demo-proj",
            )
            # 快照 ID 必须严格满足全链路纯 hex 守卫 ^cv_[0-9a-f]{8,64}$
            self.assertRegex(r1["snapshot_id"], r"^cv_[0-9a-f]{8,64}$")
            self.assertEqual(r1["blocks"], 2)

            # 全链路详情查看: MCP vault_snapshot 可直接读取 AML 快照
            snap_detail = mcp_server.t_snapshot(self.tmp, r1["snapshot_id"])
            self.assertIn(r1["snapshot_id"], snap_detail)
            self.assertNotIn("无效 id", snap_detail)

            r2 = ad.add(
                [
                    {"role": "user", "content": "补充 webhook 重试策略与幂等键设计。"},
                    {"role": "assistant", "content": "结论：webhook 重试采用指数退避，幂等键使用 X-Idempotency-Key。"},
                ],
                session_id="ses_2",
                project="demo-proj",
            )
            self.assertEqual(r2["blocks"], 2)

            full = ad.search("SQLite 倒排索引 FTS5", top_k=20)
            self.assertGreaterEqual(full["total"], 1)
            self.assertFalse(full["trimmed"])
            self.assertTrue(any("search_index.db" in item["content"] or "FTS5" in item["content"]
                                for item in full["results"]))

            # 紧凑预算应触发尾部裁剪
            tight = ad.search("SQLite 倒排索引 webhook 幂等键", top_k=20, budget_tokens=64)
            self.assertGreaterEqual(tight["total"], 1)
            self.assertLessEqual(tight["total"], full["total"] + 1)
        finally:
            ad.close()

    def test_aml_add_metadata_in_hash_and_legacy_db_migration(self):
        # 模拟缺少 v2 列的旧版 memory_items 表
        conn = cv.db(self.tmp)
        conn.executescript(
            cv.SCHEMA
            + """
            CREATE TABLE IF NOT EXISTS memory_items(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
              kind TEXT, content TEXT, project TEXT, hits INTEGER DEFAULT 0, last_used TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            msgs = [
                {"role": "user", "content": "统一配置中心切换为 etcd。"},
                {"role": "assistant", "content": "决定：配置中心使用 etcd 集群存储。"},
            ]
            a = ad.add(msgs, session_id="ses_A", project="proj_a")
            b = ad.add(msgs, session_id="ses_B", project="proj_b")
            # 相同 messages 但不同元数据应生成不同快照 ID, 不会造成磁盘与 DB 分裂
            self.assertNotEqual(a["snapshot_id"], b["snapshot_id"])
            # 相同 messages + 相同元数据应幂等
            a2 = ad.add(msgs, session_id="ses_A", project="proj_a")
            self.assertEqual(a["snapshot_id"], a2["snapshot_id"])

            # 核对磁盘 JSON 与 DB 记录一致
            env_a = json.loads((self.tmp / "snapshots" / f"{a['snapshot_id']}.json").read_text(encoding="utf-8"))
            env_b = json.loads((self.tmp / "snapshots" / f"{b['snapshot_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(env_a["capture"]["project"], "proj_a")
            self.assertEqual(env_b["capture"]["project"], "proj_b")
        finally:
            ad.close()

    def test_aml_search_single_item_over_budget_truncates_and_marks_trimmed(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            long_text = "超长架构说明：" + ("数据库分片与一致性哈希细节说明" * 40)
            ad.add(
                [{"role": "assistant", "content": long_text}],
                session_id="ses_long",
                project="long-proj",
                extract_memory=False,
            )
            res = ad.search("一致性哈希", top_k=5, budget_tokens=64, include_memories=False)
            self.assertEqual(res["total"], 1)
            self.assertTrue(res["trimmed"])
            self.assertLessEqual(cv.estimate_text_tokens(res["results"][0]["content"]), 64)
        finally:
            ad.close()

    def test_aml_concurrent_add_and_search_thread_safe(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            def worker(idx: int):
                res = ad.add(
                    [
                        {"role": "user", "content": f"并发任务 #{idx}: 验证线程安全写入 topic_{idx}"},
                        {"role": "assistant", "content": f"决定：并发任务 #{idx} 使用互斥锁保护 topic_{idx}"},
                    ],
                    session_id=f"ses_conc_{idx}",
                    project="conc-proj",
                )
                sr = ad.search(f"topic_{idx}", top_k=5)
                return res["snapshot_id"], sr["total"]

            with ThreadPoolExecutor(max_workers=6) as pool:
                outs = list(pool.map(worker, range(12)))
            self.assertEqual(len(outs), 12)
            self.assertTrue(all(re.fullmatch(r"cv_[0-9a-f]{8,64}", sid) for sid, _ in outs))
            self.assertTrue(all(total >= 1 for _, total in outs))
        finally:
            ad.close()

    def test_tenant_router_isolation_and_lru_eviction(self):
        router = aml_adapter.TenantRouter(self.tmp, max_adapters=2)
        try:
            alice = router.for_user("alice@example.com")
            bob = router.for_user("bob@example.com")
            alice.add([
                {"role": "user", "content": "Alice 秘密架构：采用 ClickHouse 存储指标。"},
                {"role": "assistant", "content": "决定：Alice 服务使用 ClickHouse。"},
            ])
            bob.add([
                {"role": "user", "content": "Bob 架构：采用 DuckDB 本地分析。"},
                {"role": "assistant", "content": "决定：Bob 服务使用 DuckDB。"},
            ])
            # Alice 只能查到 ClickHouse，查不到 Bob 的 DuckDB
            self.assertEqual(alice.search("DuckDB", top_k=10)["total"], 0)
            self.assertEqual(bob.search("ClickHouse", top_k=10)["total"], 0)
            self.assertGreaterEqual(alice.search("ClickHouse", top_k=10)["total"], 1)

            # 第 3 个租户应触发 LRU 淘汰, 句柄池大小不超过 max_adapters=2
            carol = router.for_user("carol@example.com")
            carol.add([{"role": "user", "content": "Carol 使用 Postgres。"}])
            self.assertLessEqual(len(router._adapters), 2)
            # 重新获取被淘汰的租户仍能正常查询已有数据
            bob_again = router.for_user("bob@example.com")
            self.assertGreaterEqual(bob_again.search("DuckDB", top_k=10)["total"], 1)
        finally:
            router.close_all()

    def test_http_server_route_validation_and_body_guards(self):
        # 启动隔离的 HTTP 测试服务器 (验证不篡改全局 ThreadingHTTPServer.allow_reuse_address)
        orig_reuse = ThreadingHTTPServer.allow_reuse_address
        router = aml_adapter.TenantRouter(self.tmp)

        class _TestSrv(aml_adapter._AMLHTTPServer):
            pass

        # 启动前用子线程运行 _AMLHTTPServer 以便优雅 shutdown (port=0 消除 TOCTOU 竞态)
        srv_holder = {}
        orig_cls = aml_adapter._AMLHTTPServer

        class _CaptureSrv(orig_cls):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                srv_holder["srv"] = self

        aml_adapter._AMLHTTPServer = _CaptureSrv
        th = threading.Thread(
            target=aml_adapter.serve_aml,
            args=(self.tmp, "127.0.0.1", 0, None),
            daemon=True,
        )
        th.start()
        try:
            for _ in range(250):
                if "srv" in srv_holder:
                    break
                time.sleep(0.02)
            self.assertIn("srv", srv_holder)
            port = srv_holder["srv"].server_address[1]
            self.assertEqual(ThreadingHTTPServer.allow_reuse_address, orig_reuse)

            def req(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
                c = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    c.request(method, path, body=body, headers=headers or {})
                    resp = c.getresponse()
                    data = json.loads(resp.read().decode("utf-8"))
                    return resp.status, data
                finally:
                    c.close()

            # 1) POST 到不存在路径不得创建租户目录
            st, _ = req("POST", "/not-found", json.dumps({"user_id": "ghost_user"}).encode("utf-8"))
            self.assertEqual(st, 404)
            self.assertFalse((self.tmp / "tenants").exists())

            # 2) 非对象 JSON 请求体应返回 400
            st, data = req("POST", "/add", b"[]")
            self.assertEqual(st, 400)
            self.assertIn("JSON object", data["error"])

            # 3) 超大 Content-Length 返回 413
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                c.putrequest("POST", "/add")
                c.putheader("Content-Length", str(aml_adapter.MAX_HTTP_BODY_BYTES + 1))
                c.endheaders()
                resp = c.getresponse()
                self.assertEqual(resp.status, 413)
            finally:
                c.close()

            # 4) 正常 /add + /search
            st, add_res = req(
                "POST",
                "/add",
                json.dumps({
                    "user_id": "u_http",
                    "messages": [{"role": "assistant", "content": "决定：HTTP 接口使用限流保护。"}],
                }).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            self.assertRegex(add_res["snapshot_id"], r"^cv_[0-9a-f]{8,64}$")

            st, sr_res = req(
                "POST",
                "/search",
                json.dumps({"user_id": "u_http", "query": "限流保护", "top_k": 5}).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            self.assertGreaterEqual(sr_res["total"], 1)

            # 5) HTTP 层 request_id 幂等重放 (200) 与 payload 冲突 (409 Conflict)
            idem_payload = json.dumps({
                "user_id": "u_http",
                "request_id": "http_req_001",
                "messages": [{"role": "assistant", "content": "决定：HTTP 层启用幂等重放。"}],
            }).encode("utf-8")
            st1, r_first = req("POST", "/add", idem_payload)
            self.assertEqual(st1, 200)
            self.assertFalse(r_first["idempotent_replay"])

            st2, r_replay = req("POST", "/add", idem_payload)
            self.assertEqual(st2, 200)
            self.assertTrue(r_replay["idempotent_replay"])
            self.assertEqual(r_replay["snapshot_id"], r_first["snapshot_id"])

            st3, r_conflict = req(
                "POST",
                "/add",
                json.dumps({
                    "user_id": "u_http",
                    "request_id": "http_req_001",
                    "messages": [{"role": "assistant", "content": "完全不同的冲突内容。"}],
                }).encode("utf-8"),
            )
            self.assertEqual(st3, 409)
            self.assertIn("冲突", r_conflict["error"])

            # 6) 兼容 Idempotency-Key 请求头
            hdr_body = json.dumps({
                "user_id": "u_http",
                "messages": [{"role": "user", "content": "通过 Header 传递幂等键。"}],
            }).encode("utf-8")
            st4, h1 = req("POST", "/add", hdr_body, headers={"Idempotency-Key": "hdr_req_001"})
            self.assertEqual(st4, 200)
            self.assertFalse(h1["idempotent_replay"])
            st5, h2 = req("POST", "/add", hdr_body, headers={"Idempotency-Key": "hdr_req_001"})
            self.assertEqual(st5, 200)
            self.assertTrue(h2["idempotent_replay"])
            st6, _ = req(
                "POST",
                "/add",
                json.dumps({
                    "user_id": "u_http",
                    "messages": [{"role": "user", "content": "Header 幂等键冲突内容。"}],
                }).encode("utf-8"),
                headers={"Idempotency-Key": "hdr_req_001"},
            )
            self.assertEqual(st6, 409)

            # 7) 非法 request_id (纯空白/非字符串) 返回 400
            st7, _ = req(
                "POST",
                "/add",
                json.dumps({
                    "user_id": "u_http",
                    "request_id": "   ",
                    "messages": [{"role": "user", "content": "空白 request_id 测试"}],
                }).encode("utf-8"),
            )
            self.assertEqual(st7, 400)
        finally:
            aml_adapter._AMLHTTPServer = orig_cls
            if "srv" in srv_holder:
                srv_holder["srv"].shutdown()
                srv_holder["srv"].server_close()
            th.join(timeout=3)
            router.close_all()

    def test_mcp_budget_tokens_trimming_edge_cases_and_parity(self):
        # 1) token 估算口径与 cv.estimate_text_tokens 完全一致
        for sample in ("", "hello world", "中文测试文本", "混合 SQLite 倒排索引 test 123"):
            self.assertEqual(mcp_server._estimate_tokens(sample), cv.estimate_text_tokens(sample))

        # 2) 单条超长纯中文触发首条截断时: 必须保留裁剪提示且总 token 贴合预算
        long_cn = "架构决策记录：" + ("索引分片与连接池复用核心细节" * 40)
        out = mcp_server._trim_by_budget("头部:", [long_cn], "尾部", budget_tokens=64)
        self.assertIn("budget_tokens=64", out)
        self.assertLessEqual(mcp_server._estimate_tokens(out), 72)

        # 3) 异常 budget_tokens 类型应直接抛出 ValueError (含 bool / 非数字字符串)
        with self.assertRaises(ValueError):
            mcp_server.dispatch("vault_search", {"query": "SQLite", "budget_tokens": True}, self.tmp)
        with self.assertRaises(ValueError):
            mcp_server.dispatch("vault_search", {"query": "SQLite", "budget_tokens": "not_a_num"}, self.tmp)

        # 4) 端到端冷路径裁剪
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            for i in range(4):
                ad.add(
                    [
                        {"role": "user", "content": f"讨论第 {i} 个微服务模块的 SQLite 索引优化方案。"},
                        {"role": "assistant", "content": f"决定：模块 {i} 采用 SQLite 索引分片与连接池复用，" + ("细节说明 " * 30)},
                    ],
                    session_id=f"ses_mcp_{i}",
                    project="mcp-proj",
                )
        finally:
            ad.close()

        mcp_server._svc_mark_down()
        full_ctx = mcp_server.dispatch("vault_context", {"query": "SQLite 索引", "limit": 4}, self.tmp)
        trimmed_ctx = mcp_server.dispatch(
            "vault_context", {"query": "SQLite 索引", "limit": 4, "budget_tokens": 90}, self.tmp
        )
        self.assertIn("budget_tokens=90", trimmed_ctx)
        self.assertLess(len(trimmed_ctx), len(full_ctx))

        trimmed_search = mcp_server.dispatch(
            "vault_search", {"query": "SQLite 索引", "limit": 4, "budget_tokens": 64}, self.tmp
        )
        self.assertIn("budget_tokens=64", trimmed_search)

    def test_request_id_idempotency_and_conflict(self):
        ad = aml_adapter.AMLMemoryAdapter(self.tmp)
        try:
            msgs = [
                {"role": "user", "content": "确认幂等键设计。"},
                {"role": "assistant", "content": "决定：统一使用 request_id 做幂等写入。"},
            ]
            first = ad.add(msgs, session_id="ses_idem", request_id="req_001")
            self.assertFalse(first["idempotent_replay"])

            replay = ad.add(msgs, session_id="ses_idem", request_id="req_001")
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["snapshot_id"], first["snapshot_id"])

            # 同 request_id 不同消息内容 → RequestConflictError
            with self.assertRaises(aml_adapter.RequestConflictError):
                ad.add(
                    [{"role": "user", "content": "完全不同的 payload"}],
                    session_id="ses_idem",
                    request_id="req_001",
                )

            # 同 request_id 切换 extract_memory 行为参数 → RequestConflictError
            with self.assertRaises(aml_adapter.RequestConflictError):
                ad.add(msgs, session_id="ses_idem", request_id="req_001", extract_memory=False)

            # 非法 request_id (空白 / 非字符串) → ValueError
            for bad_id in ("   ", "", 123, True, ["a"]):
                with self.assertRaises(ValueError):
                    ad.add(msgs, session_id="ses_idem", request_id=bad_id)

            # 并发同 request_id 不同 payload: 必须恰好 1 个成功, 其余全抛 RequestConflictError
            def race(i: int):
                try:
                    ad.add(
                        [{"role": "user", "content": f"并发竞争内容 #{i}"}],
                        session_id=f"ses_race_{i}",
                        request_id="req_race_same",
                        extract_memory=False,
                    )
                    return "ok"
                except aml_adapter.RequestConflictError:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=6) as pool:
                outcomes = list(pool.map(race, range(8)))
            self.assertEqual(outcomes.count("ok"), 1)
            self.assertEqual(outcomes.count("conflict"), 7)
        finally:
            ad.close()

    def test_cli_env_defaults_handle_empty_strings(self):
        saved = {k: os.environ.get(k) for k in ("HOST", "PORT", "CV_VAULT_DIR")}
        try:
            os.environ["HOST"] = ""
            os.environ["PORT"] = ""
            os.environ["CV_VAULT_DIR"] = ""
            args = aml_adapter._build_arg_parser().parse_args([])
            self.assertEqual(args.host, "127.0.0.1")
            self.assertEqual(args.port, 8792)
            self.assertEqual(args.vault, "vault")

            os.environ["HOST"] = "0.0.0.0"
            os.environ["PORT"] = "8000"
            os.environ["CV_VAULT_DIR"] = "/data/vault"
            args2 = aml_adapter._build_arg_parser().parse_args([])
            self.assertEqual(args2.host, "0.0.0.0")
            self.assertEqual(args2.port, 8000)
            self.assertEqual(args2.vault, "/data/vault")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
