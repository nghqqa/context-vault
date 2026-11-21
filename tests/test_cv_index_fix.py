"""cv_index 四项已证实缺陷的回归验收:
fix1 增量去重首见序 = 全量/冷的全局链走序 (三级命中分歧);
fix2 单文件瞬时读失败不得计入指纹 (否则该文件从热索引永久消失);
fix3 换装窗口并发 — 读者单引用原子换装 (IndexError/空命中窗/closed database/段错误);
fix4 他进程持锁时构造 VaultIndex 绝不销毁健康索引库文件。

运行: python -m pytest tests/test_cv_index_fix.py -v
铁律: 全部构造在 tmp_path 临时 vault, 绝不触碰生产 vault/。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from cv_index import ColdRanker, VaultIndex  # noqa: E402

NOW = 1796000000.0  # 固定 now 消除时间衰减浮动 (created_at 2026-11, age 为正, 各代同衰减)


# ---------------- 构造工具 ----------------

def _env(blocks: list, parent: str | None = None,
         created_at: str = "2026-11-01T00:00:00") -> dict:
    """最小可解析 envelope (capture/conversation/stats/lineage)。"""
    return {"capture": {"agent": "codex", "cwd": "D:\\proj", "session_ref": "s1",
                        "completeness": "full"},
            "created_at": created_at,
            "stats": {"blocks_total": len(blocks), "tokens_in": 100 * len(blocks)},
            "conversation": blocks,
            "lineage": {"parent_snapshot": parent}}


def _write(vault: Path, env: dict, sid: str) -> str:
    (vault / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                     encoding="utf-8")
    return sid


def _load_envs(vault: Path) -> list:
    out = []
    for f in sorted((vault / "snapshots").glob("cv_*.json")):
        env = json.loads(f.read_text(encoding="utf-8"))
        env["_sid"] = f.stem
        out.append(env)
    return out


# ---------------- fix1: 增量去重首见序 = 全量/冷的全局链走序 ----------------

def test_incremental_first_seen_matches_global_chain_order(tmp_path):
    """链根 p、q 各挂一版旧快照; 批量增量同批加入 z9(父p)、a1(父q), 两版含相同新块 S。
    文件名序 a1<z9 与根序 p<q 相反: 修复前增量只按本批新增文件名排序, 把 S 首见记给
    a1, 全量/冷按根序记给 z9 → 三级命中分歧; 修复后四级 (增量热索引 / readonly 装载 /
    全量重建 / ColdRanker) 必须命中同一 sid。"""
    p, q = "cv_pchainroot0001", "cv_qchainroot0001"
    a1, z9 = "cv_a1newleaf000001", "cv_z9newleaf000001"
    assert a1 < z9 and p < q  # 复现前提: 文件名序与链根序相反
    _write(tmp_path, _env([{"type": "text", "role": "user",
                            "content": "根链P 旧版内容"}]), p)
    _write(tmp_path, _env([{"type": "text", "role": "user",
                            "content": "根链Q 旧版内容"}]), q)
    idx = VaultIndex(tmp_path)
    assert idx.stats["full"] == 1
    block_s = {"type": "text", "role": "user",
               "content": "孤语独标 uniquetok7 相同新块S"}
    _write(tmp_path, _env([dict(block_s)], parent=p), z9)
    _write(tmp_path, _env([dict(block_s)], parent=q), a1)
    idx.rebuild()
    assert idx.stats["incr"] == 1  # 同批新增走增量

    def hit_sid(ranker_result):
        assert ranker_result["hits"], "相同新块 S 必须可命中"
        return ranker_result["hits"][0]["sid"]

    hot = hit_sid(idx.search("uniquetok7", 5, now=NOW))
    assert hot == z9, f"增量首见必须按全局链序记给 z9 (链根 p<q), 实得 {hot}"

    # readonly 装载 (增量落盘库直查) 同命中
    ro = VaultIndex(tmp_path, readonly=True)
    assert not ro._broken and hit_sid(ro.search("uniquetok7", 5, now=NOW)) == z9
    ro._close()

    # 全量重建同命中 (先释放增量实例句柄, Windows 才允许删库)
    idx._close()
    (tmp_path / "search_index.db").unlink()
    full = VaultIndex(tmp_path)
    assert full.stats["full"] == 1 and hit_sid(full.search("uniquetok7", 5, now=NOW)) == z9
    full._close()

    # 冷回退 (ColdRanker 全量扫描) 同命中
    cold = ColdRanker(_load_envs(tmp_path), "uniquetok7", now=NOW).search(5)
    assert hit_sid(cold) == z9


# ---------------- fix2: 单文件瞬时读失败 → 不计入指纹, 下次自动追赶 ----------------

def test_transient_read_failure_auto_catchup(tmp_path, monkeypatch):
    """修复前: 失败文件也计入指纹 → 之后任何 rebuild 因指纹相同早退, 该文件永远
    从热索引消失 (冷路径却能查到)。修复后: 指纹 = 成功索引文件集合的指纹 →
    下次 rebuild 自动追赶, 追赶成功后再 rebuild 幂等早退。"""
    good, bad = "cv_g00d0000000001", "cv_b4d0000000001"
    _write(tmp_path, _env([{"type": "text", "role": "user",
                            "content": "恢复词 recover7 好快照"}]), good)
    _write(tmp_path, _env([{"type": "text", "role": "user",
                            "content": "瞬时词 transient7 坏快照"}]), bad)
    calls = {"fail_left": 1}
    orig = VaultIndex._index_one

    def flaky(self, f, staging, dedup=None):
        if f.stem == bad and calls["fail_left"] > 0:
            calls["fail_left"] -= 1
            return False  # 模拟单文件瞬时读失败 (部分失败不炸整体)
        return orig(self, f, staging, dedup=dedup)

    monkeypatch.setattr(VaultIndex, "_index_one", flaky)
    idx = VaultIndex(tmp_path)
    assert good in idx.meta and bad not in idx.meta
    fp_failed = idx.fingerprint
    assert idx.search("recover7", 5)["hits"], "部分失败时好文件必须仍可检索"

    monkeypatch.undo()
    idx.rebuild()  # 指纹不含失败文件 → 自动追赶
    assert calls["fail_left"] == 0, "追赶轮必须重试过失败文件"
    assert bad in idx.meta
    r = idx.search("transient7", 5)
    assert r["hits"] and r["hits"][0]["sid"] == bad
    assert idx.fingerprint != fp_failed

    stats_before = dict(idx.stats)
    fp_ok = idx.fingerprint
    idx.rebuild()  # 幂等: 指纹已含成功文件 → 早退
    assert idx.stats == stats_before and idx.fingerprint == fp_ok


# ---------------- fix3: 换装窗口并发 (读者原子换装回归, 3 轮精简版) ----------------

def test_rebuild_reader_concurrency_atomic_swap(tmp_path):
    """写线程连续 全量+增量 重建 × 多读者循环 search/context (自然调度):
    零异常 / 零空命中窗 / 零段错误 (进程存活本身即断言)。
    修复前: DB 先提交内存后扩 → IndexError; 全量重建先清共享态 → 静默空命中;
    _close 不持锁关共享连接 → ProgrammingError / 段错误。"""
    anchor = "锚点共存词 anchor7"
    for i in range(4):
        _write(tmp_path, _env([{"type": "text", "role": "user",
                                "content": f"基线快照{i} {anchor}"}]),
               f"cv_base{i:02d}0000000001")
    idx = VaultIndex(tmp_path)
    assert idx.search("anchor7", 5)["hits"]

    errors: list = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                if not idx.search("anchor7", 5)["hits"]:
                    errors.append(AssertionError("空命中窗: 锚点词在任何完整代都应命中"))
                if not idx.context("anchor7", 3)["results"]:
                    errors.append(AssertionError("context 空命中窗"))
            except Exception as exc:  # 读者侧任何异常都判失败 (含 IndexError/closed db)
                errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in readers:
        t.start()

    def writer():
        try:
            for r in range(3):
                _write(tmp_path, _env([{"type": "text", "role": "user",
                                        "content": f"增量轮次{r} {anchor} 轮次词{r}"}]),
                       f"cv_inc{r:02d}0000000001")
                idx.rebuild()            # 增量 (拼接出新数组, 单引用换装)
                idx.rebuild(force=True)  # 全量 (同事务覆盖, 不清共享态)
                time.sleep(0.01)
        except Exception as exc:
            errors.append(exc)

    w = threading.Thread(target=writer)
    w.start()
    w.join(120)
    stop.set()
    for t in readers:
        t.join(30)
    assert not errors, f"并发窗口出现 {len(errors)} 个异常, 首个: {errors[0]!r}"
    assert idx.stats["full"] >= 4 and idx.stats["incr"] >= 3
    assert idx.search("anchor7", 5)["hits"]


# ---------------- fix4: 他进程持锁时构造绝不销毁健康索引 ----------------

def test_locked_db_construction_preserves_file(tmp_path):
    """外部连接 BEGIN EXCLUSIVE 持锁时构造 VaultIndex: 不抛异常 (内存兜底)、
    库文件字节级完好、锁释放后 rebuild 追赶落盘且功能正常。
    修复前: _load_db 失败一律 purge + unlink 失败降级 truncate(0) →
    健康库在约 42.9s 后被截成 0B 且构造抛 OperationalError。"""
    sid = "cv_l0cksafe0000001"
    _write(tmp_path, _env([{"type": "text", "role": "user",
                            "content": "持锁词 locksafe7 健康库"}]), sid)
    idx = VaultIndex(tmp_path)
    assert idx.search("locksafe7", 5)["hits"]
    idx._close()  # 释放本进程句柄 (被锁场景只含外部连接)

    db = tmp_path / "search_index.db"
    expected = db.read_bytes()
    ext = sqlite3.connect(db)
    ext.execute("BEGIN EXCLUSIVE")  # 模拟他进程独占持锁
    t0 = time.monotonic()
    try:
        idx2 = VaultIndex(tmp_path)  # 构造不得抛异常, 不得动库文件
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"持锁构造必须干净回退, 实际抛出: {exc!r}")
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, f"持锁构造回退不应阻塞数十秒, 实耗时 {elapsed:.2f}s"
    assert db.read_bytes() == expected, "持锁期间库文件必须字节级完好"
    assert idx2.search("locksafe7", 5)["hits"], "内存兜底代必须功能正常"

    ext.rollback()
    ext.close()      # 释放锁
    idx2.rebuild()   # 指纹未落账 → 自动追赶落盘
    assert idx2.stats["full"] >= 1
    assert idx2.search("locksafe7", 5)["hits"]
    idx3 = VaultIndex(tmp_path)  # 锁释放后新实例正常装载
    assert idx3.stats["cache_load"] == 1
    assert idx3.search("locksafe7", 5)["hits"]
