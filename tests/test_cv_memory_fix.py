"""cv_memory 四项 medium 缺陷修复回归 (2026-11-19):
1) memory_dedup 盲取 MIN(id) 可误删 active 副本 → 组内优先 status='active'
   (多个取 id 最大), 组内无 active 才回退 MIN(id); 去重后幂等重放覆盖关系;
2) 无文件名中文决策句把决策值当锚点 (决定：调度策略采用 daily → proj|daily,
   同槽推翻永不触发) → 剥决策值: 只认首个决策值引入词之前的主语, 取不到就空键;
3) 英文句首动词塌缩 (We agreed to use SQLite / …drop the compat layer 同得
   proj|agreed, 跨主题误推翻) → 剥决策短语, 动词/值词不入键;
4) 文件名正则误吞 IP/版本号 (20.11 → proj|2011, 127.0.0.1 → proj|1270,
   真库 goai|1270 槽实证挂 4 条无关记忆) → 纯数字段不构成文件锚点。

运行: python -m pytest tests/test_cv_memory_fix.py -v
铁律: 全部构造在 tmp_path 临时 vault, 绝不触碰生产 vault/。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import cv  # noqa: E402
import cv_memory  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "snapshots").mkdir(parents=True)
    return v


def _write_snap(vault: Path, sid: str, blocks: list, cwd: str = "D:\\proj"):
    env = cv.build_envelope({"capture": {"agent": "codex", "cwd": cwd, "session_ref": "s1"},
                             "conversation": blocks, "stats": cv.zero_stats()})
    (vault / "snapshots" / f"{sid}.json").write_text(json.dumps(env, ensure_ascii=False),
                                                     encoding="utf-8")


def _extract(vault: Path, force: bool = True) -> dict:
    return cv.extract_memories_incremental(vault, engine_name="v2", force=force)


def _rows(vault: Path, sql: str, args=()) -> list:
    conn = sqlite3.connect(vault / "vault.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------------- 修复2: 中文决策句剥离决策值锚点 ----------------

def test_topic_key_zh_strips_decision_value_anchor():
    """无文件名时, 采用/改为 之后的值绝不作锚点; 取不到可靠主语返回空键。"""
    tk = cv_memory._topic_key
    assert tk("proj", "决定：调度策略采用 daily 批处理") == "", "决策值 daily 不得入键"
    assert tk("proj", "决定：调度策略改为 weekly 批处理") == "", "决策值 weekly 不得入键"
    assert tk("proj", "改为 weekly 批处理") == ""
    assert tk("proj", "采用 Redis 做会话缓存") == ""
    # 决策值引入词之前的名词性主语照常入键 (覆盖不漏收)
    assert tk("proj", "调度策略 sched_policy 采用 daily 批处理") == "proj|sched_policy"
    # 有文件名的路径完全不变 (原有全绿用例的语义)
    assert tk("proj", "决定：deploy.yaml 的调度策略改为 weekly 批处理") == "proj|deployyaml"
    # 无决策结构的普通叙述不受影响
    assert tk("proj", "FTS5 虚表做全文索引效果很好") == "proj|fts5"


def test_zh_value_anchor_no_bogus_slot(tmp_path):
    """旧缺陷同槽推翻永不触发的根因 (proj|daily vs proj|weekly 两个伪槽) 必须消失:
    提取后库内不得出现以决策值为 topic_key 的槽位。"""
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_zhA00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：调度策略采用 daily 批处理", "ts": "2026-01-01T00:00:00"}])
    _write_snap(vault, "cv_zhB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：调度策略改为 weekly 批处理", "ts": "2026-02-01T00:00:00"}])
    r = _extract(vault)
    assert r["added"] >= 2
    bad = _rows(vault, "SELECT id FROM memory_items WHERE topic_key IN ('daily','weekly')")
    assert bad == [], "决策值不得占据 topic_key 槽位 (旧缺陷: proj|daily / proj|weekly)"
    sup = _rows(vault, "SELECT status, superseded_by FROM memory_items")
    assert all(r2["superseded_by"] is None for r2 in sup), "空键条目不参与跨句覆盖"


# ---------------- 修复3: 英文句首动词塌缩 ----------------

def test_topic_key_en_strips_decision_phrase_and_verbs():
    tk = cv_memory._topic_key
    # 决策短语剥离后首词是动词/值词 → 空键 (旧缺陷: 两句同得 proj|agreed)
    assert tk("proj", "We agreed to use SQLite for the metadata store") == ""
    assert tk("proj", "We agreed to drop the compat layer instead of keeping both") == ""
    assert tk("proj", "We decided to migrate the session store to Postgres") == ""
    # 剥离后是名词性主语 → 照常入键 (任务书语义: agreed on 之后的名词作主语)
    assert tk("proj", "We decided on SQLite for the metadata store") == "proj|sqlite"
    assert tk("proj", "Chose to keep retry_backoff configurable") == "", "keep 为动词不入键"
    # 普通英文叙述 (无决策短语) 不受影响
    assert tk("proj", "The session_tokens design uses HMAC signatures") == "proj|session_tokens"


def test_english_cross_topic_never_supersede(tmp_path):
    """两句不同主题 (旧缺陷: 同塌缩到 proj|agreed) 不得互相推翻。"""
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_enA00000000001", [
        {"type": "text", "role": "assistant",
         "content": "We agreed to use SQLite for the metadata store",
         "ts": "2026-01-01T00:00:00"}])
    _write_snap(vault, "cv_enB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "We agreed to drop the compat layer instead of keeping both",
         "ts": "2026-02-01T00:00:00"}])
    r = _extract(vault)
    assert r["added"] >= 2
    assert r["superseded"] == 0, "不同主题 (值词/动词锚已剥离为空键) 绝不互相推翻"
    st = {row["status"] for row in _rows(vault, "SELECT status FROM memory_items")}
    assert st == {"active"}


def test_english_same_topic_supersede_chain(tmp_path):
    """同主题 A→B 正常推翻 (英文语料): 文件锚路径不受剥离影响。"""
    vault = _vault(tmp_path)
    _write_snap(vault, "cv_esA00000000001", [
        {"type": "text", "role": "assistant",
         "content": "We agreed to use SQLite per storage.yaml for the metadata store",
         "ts": "2026-01-01T00:00:00"}])
    _write_snap(vault, "cv_esB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "We agreed to switch storage.yaml to Postgres instead of SQLite",
         "ts": "2026-02-01T00:00:00"}])
    r = _extract(vault)
    assert r["superseded"] >= 1
    rows = {row["content"]: row for row in
            _rows(vault, "SELECT * FROM memory_items WHERE kind='decision'")}
    a = next(v for c, v in rows.items() if "SQLite" in c)
    b = next(v for c, v in rows.items() if "Postgres" in c)
    assert a["status"] == "superseded" and a["superseded_by"] == b["id"]
    assert b["status"] == "active"
    assert a["topic_key"] == b["topic_key"] == "proj|storageyaml"


# ---------------- 修复4: 文件名正则不吞 IP/版本号 ----------------

def test_file_anchor_ignores_pure_numeric_segments():
    tk = cv_memory._topic_key
    # 纯数字段 (名与扩展名均无字母) 不构成文件锚点
    assert tk("proj", "Node 版本固定在 20.11 LTS") != "proj|2011"
    assert tk("proj", "升级到 Python 3.9 后行为变化") != "proj|39"
    assert tk("proj", "服务只绑定 127.0.0.1 调试") == "", "IP 不得成锚, 亦无主语 → 空键"
    assert tk("proj", "监听 127.0.0.1:8080 端口") == ""
    # 含字母的合法文件名必须继续命中
    assert tk("proj", "配置在 config.yaml 里") == "proj|configyaml"
    assert tk("proj", "入口是 run.py") == "proj|runpy"
    assert tk("proj", "v2.py 与 test_1.py 都要通过") == "proj|v2py"


# ---------------- 修复1: dedup 保留 active 副本 ----------------

def test_dedup_keeps_active_copy_not_superseded_min_id(tmp_path):
    """已证现场景: A(daily) 被 B(改为weekly) 推翻 → 新快照重申 daily 产生新 active 行
    (dup 检查只认 active) → 旧 dedup 删掉新 active 留下 MIN(id) 的 superseded,
    daily 从默认检索消失。修复后默认检索仍含 daily (active 版)。"""
    vault = _vault(tmp_path)
    daily_blk = [{"type": "text", "role": "assistant",
                  "content": "决定：deploy.yaml 的调度策略采用 daily 批处理",
                  "ts": "2026-01-01T00:00:00"}]
    _write_snap(vault, "cv_ddA00000000001", daily_blk)
    _extract(vault)  # 第 1 次增量运行: A 入库 active
    _write_snap(vault, "cv_ddB00000000001", [
        {"type": "text", "role": "assistant",
         "content": "决定：deploy.yaml 的调度策略改为 weekly 批处理",
         "ts": "2026-02-01T00:00:00"}])
    _extract(vault)  # 第 2 次: B 推翻 A (运行末 recompute → A=superseded)
    _write_snap(vault, "cv_ddC00000000001", [
        {"type": "text", "role": "assistant",
         "content": daily_blk[0]["content"],  # 与 A 同 kind+content, 但重申发生在 B 之后
         "ts": "2026-03-01T00:00:00"}])
    cv.extract_memories_incremental(vault, engine_name="v2")  # 第 3 次增量: dup 检查只认 active → 产生新行
    rows = _rows(vault, "SELECT id, content, status FROM memory_items"
                        " WHERE content LIKE '%daily 批处理%' ORDER BY id")
    assert len(rows) == 2, "前置: 同内容一 superseded (A) 一 active (重申行 C)"
    assert rows[0]["status"] == "superseded" and rows[1]["status"] == "active"
    active_id = rows[1]["id"]

    res = cv_memory.memory_dedup(vault)
    assert res["removed"] == 1
    left = _rows(vault, "SELECT id, content, status FROM memory_items"
                        " WHERE content LIKE '%daily 批处理%'")
    assert len(left) == 1 and left[0]["id"] == active_id, "必须保留 active 副本 (非 MIN(id))"
    assert left[0]["status"] == "active"

    # 默认检索 (active-only) 仍含 daily; B(weekly) 关系不受牵动
    hits = cv.memory_search_api(vault, "deploy 调度", 10)
    contents = [h["content"] for h in hits]
    assert any("daily" in c for c in contents), "去重后默认检索必须仍含 daily (active 版)"
    weekly = _rows(vault, "SELECT status FROM memory_items WHERE content LIKE '%weekly%'")
    assert weekly and weekly[0]["status"] == "active"
    # 覆盖链幂等重放无残留悬挂指针
    dangling = _rows(vault, "SELECT id FROM memory_items m WHERE superseded_by IS NOT NULL"
                            " AND NOT EXISTS (SELECT 1 FROM memory_items r"
                            " WHERE r.id = m.superseded_by)")
    assert dangling == []


def test_dedup_group_without_active_falls_back_to_min_id(tmp_path):
    """组内全为 superseded (人工注入的边态) → 回退保留最早一条; 组内多个 active →
    保留 id 最大 (最新) 一条。"""
    vault = _vault(tmp_path)
    _extract(vault)  # 建库建表
    conn = sqlite3.connect(vault / "vault.db")
    conn.executescript("""
INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,topic_key,status)
  VALUES('t','s1','decision','决定：cache_a.yaml 走 LRU','proj','proj|cachea','superseded'),
         ('t','s2','decision','决定：cache_a.yaml 走 LRU','proj','proj|cachea','superseded'),
         ('t','s3','decision','决定：cache_b.yaml 走 FIFO','proj','proj|cacheb','active'),
         ('t','s4','decision','决定：cache_b.yaml 走 FIFO','proj','proj|cacheb','active');
""")
    conn.commit()
    conn.close()
    res = cv_memory.memory_dedup(vault)
    assert res["removed"] == 2
    left = {row["content"]: row["id"] for row in
            _rows(vault, "SELECT id, content FROM memory_items ORDER BY id")}
    assert len(left) == 2
    min_a = _rows(vault, "SELECT MIN(id) AS i FROM memory_items"
                         " WHERE content LIKE '%cache_a.yaml%'")[0]["i"]
    assert left["决定：cache_a.yaml 走 LRU"] == min_a, "无 active 组回退 MIN(id)"
    max_b = _rows(vault, "SELECT MAX(id) AS i FROM memory_items"
                         " WHERE content LIKE '%cache_b.yaml%'")[0]["i"]
    assert left["决定：cache_b.yaml 走 FIFO"] == max_b, "多 active 组保留最新一条"
