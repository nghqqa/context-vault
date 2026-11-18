#!/usr/bin/env python3
"""本机大规模性能实测 — 指向本地大型 vault, 只读副本上实测千级快照下的
索引体积缩减比与查询延迟 (绝不改写原库: 快照以硬链接/拷贝进临时目录)。

产出脱敏数字 (无任何会话内容), 供 docs/BENCHMARK.md 与 README 引用:
  - 索引体积 (v2 search_index.db) vs v0 index_cache.pkl
  - 构建耗时 / 冷启动耗时 / 查询延迟 P50/P95
  - 唯一块数 (链内去重后) / 词项数

运行: python benchmarks/bench_scale_local.py --vault /path/to/vault [--queries "q1;q2"]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

from cv_index import VaultIndex  # noqa: E402

DEFAULT_QUERIES = [
    "数据库迁移 回滚", "迁移 方案", "cache 命中率", "token 账本", "部署 脚本",
    "索引 体积", "api 超时", "配置 环境变量", "测试 覆盖", "归档 会话",
    "同步 水位", "压缩 边界", "谱系 lineage", "备份 轮换", "MCP 工具",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, help="本地 vault 目录 (只读, 绝不改写)")
    ap.add_argument("--queries", default=";".join(DEFAULT_QUERIES), help="分号分隔的测试查询")
    ap.add_argument("--out", default=str(HERE / "results" / "scale_local.json"))
    args = ap.parse_args()

    src = Path(args.vault)
    if not (src / "snapshots").exists():
        raise SystemExit(f"vault 不存在或无 snapshots/: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="cv_scale_"))
    try:
        dst = tmp / "snapshots"
        dst.mkdir(parents=True)
        t0 = time.perf_counter()
        linked = copied = 0
        for f in (src / "snapshots").glob("cv_*.json"):
            try:
                os.link(f, dst / f.name)  # 同卷硬链接: 零拷贝 staging (只读)
                linked += 1
            except OSError:
                shutil.copy2(f, dst / f.name)
                copied += 1
        stage_sec = time.perf_counter() - t0

        t0 = time.perf_counter()
        idx = VaultIndex(tmp)
        build_sec = time.perf_counter() - t0
        db_mb = (tmp / "search_index.db").stat().st_size / 1048576
        pkl = Path(src / "index_cache.pkl")
        pkl_mb = pkl.stat().st_size / 1048576 if pkl.exists() else None

        t0 = time.perf_counter()
        idx2 = VaultIndex(tmp)
        idx2.search(DEFAULT_QUERIES[0], 5)
        cold_ms = (time.perf_counter() - t0) * 1000

        queries = [q for q in args.queries.split(";") if q.strip()]
        lats = []
        for q in queries:
            t0 = time.perf_counter()
            idx.search(q, 5)
            lats.append((time.perf_counter() - t0) * 1000)
        lats.sort()
        result = {
            "vault": str(src),
            "snapshots": len(idx.meta),
            "unique_blocks": len(idx._doc_sid),
            "terms": idx.term_count,
            "stage_sec": round(stage_sec, 1),
            "hardlinked": linked, "copied": copied,
            "build_sec": round(build_sec, 1),
            "index_v2_mb": round(db_mb, 1),
            "index_v0_pickle_mb": round(pkl_mb, 1) if pkl_mb else None,
            "reduction_vs_pickle_pct": round(100 * (1 - db_mb / pkl_mb), 1) if pkl_mb else None,
            "cold_start_ms": round(cold_ms, 1),
            "latency_p50_ms": round(lats[len(lats) // 2], 1),
            "latency_p95_ms": round(lats[min(int(len(lats) * 0.95) - 1, len(lats) - 1)], 1),
            "latency_max_ms": round(lats[-1], 1),
            "n_queries": len(queries),
            "note": "快照以硬链接/拷贝进临时目录实测, 原 vault 未改写; 查询词为通用工程词, 无会话内容外泄",
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
