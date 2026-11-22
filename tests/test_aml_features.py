"""AML 借鉴特性回归测试: AMLMemoryAdapter (Add/Search) + MCP budget_tokens 预算裁剪。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import tempfile
import shutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import aml_adapter  # noqa: E402
import mcp_server  # noqa: E402


class TestAMLFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv_aml_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aml_adapter_add_and_search_with_budget(self):
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
            self.assertTrue(r1["snapshot_id"].startswith("cv_aml_"))
            self.assertEqual(r1["blocks"], 2)

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

    def test_mcp_budget_tokens_trimming(self):
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

        # 模拟热服务不可用，直接走本地索引路径
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

    def test_tenant_router_isolation(self):
        router = aml_adapter.TenantRouter(self.tmp)
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
            res_a = alice.search("DuckDB", top_k=10)
            self.assertEqual(res_a["total"], 0)
            res_b = bob.search("ClickHouse", top_k=10)
            self.assertEqual(res_b["total"], 0)
            res_a_ok = alice.search("ClickHouse", top_k=10)
            self.assertGreaterEqual(res_a_ok["total"], 1)
        finally:
            router.close_all()


if __name__ == "__main__":
    unittest.main()
