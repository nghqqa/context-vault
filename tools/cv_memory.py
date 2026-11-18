"""记忆域纯逻辑层 — M3 跨会话记忆的提取/去重/检索引擎 (零第三方依赖)。

与 cv_index / cv_ui 同模式: cv.py 只留 CLI/HTTP 薄壳, 本模块可独立演进。
cv 依赖 (db/SCHEMA/now_iso) 一律函数内惰性获取 — 无论 cv 先导入还是本模块
先导入都不会触发循环导入; tokenize 来自自包含的 cv_index。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from cv_index import tokenize

MEMORY_TABLE = """
CREATE TABLE IF NOT EXISTS memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
  kind TEXT, content TEXT, project TEXT,
  hits INTEGER DEFAULT 0, last_used TEXT);
CREATE INDEX IF NOT EXISTS idx_memory_snap ON memory_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind);
CREATE TABLE IF NOT EXISTS memory_extracted(
  snapshot_id TEXT, engine TEXT, extracted_at TEXT,
  PRIMARY KEY (snapshot_id, engine));
"""

# 提取规则 v0: 诚实规则式, 不假装语义理解; 外部引擎 (Supermemory/Graphiti) 走
# MemoryEngine 接口位 (高级语义记忆 = Pro 空间), 内置引擎为零依赖默认实现.
DECISION_MARKERS = ("决定", "决策", "采用", "方案是", "结论是", "结论：", "选型",
                    "我们选", "定了", "agreed", "we decide", "let's go with",
                    "chose", "final answer", "the plan is")
PREFERENCE_MARKERS = ("不要", "必须", "总是", "记住", "以后都", "每次都",
                      "never ", "always ", "remember to", "from now on")
MAX_MEMORY_PER_SNAPSHOT = 40


def _memory_line_noise(s: str) -> bool:
    """行级噪音闸门 (rule 引擎 v0.1 提精度): 列表项/标题/引用行、重行内代码行与问句
    不构成可长期复用的决策/偏好叙述; summary (压缩摘要) 不适用此闸门。"""
    if s.endswith(("?", "？")):
        return True
    if s[0] in "-*+#>" or (len(s) > 1 and s[0].isdigit() and s[1] in ".、)）"):
        return True
    return s.count("`") >= 2  # 行内代码密度高 (命令/标识符罗列), 非叙述句


def extract_memories(env: dict) -> list:
    """从单个快照提取记忆条目: decision (assistant 决策句) / summary (压缩摘要)
    / preference (user 指令模式)。每条带块索引溯源 (raw 里 idx)。"""
    items, sid = [], env.get("_sid") or "?"
    project = None
    cwd = env["capture"].get("cwd")
    if cwd:
        project = str(cwd).rstrip("\\").rsplit("\\", 1)[-1] or str(cwd)
    seen = set()
    for i, b in enumerate(env["conversation"]):
        kind, text = None, ""
        if b["type"] == "compaction":
            kind, text = "summary", b.get("summary", "")
        elif b["type"] == "text" and b.get("role") == "assistant":
            text = b.get("content", "")
            kind = "decision" if any(m in text.lower() for m in DECISION_MARKERS) else None
        elif b["type"] == "text" and b.get("role") == "user":
            text = b.get("content", "")
            kind = "preference" if any(m in text.lower() for m in PREFERENCE_MARKERS) else None
        if not kind or not text:
            continue
        # 句级切分: 只取命中标记的句子, 控制颗粒度; ``` 围栏内是代码不是叙述
        in_fence = False
        for sent in text.replace("\r", "").split("\n"):
            s = sent.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not (12 <= len(s) <= 400):
                continue
            if kind != "summary" and _memory_line_noise(s):
                continue
            low = s.lower()
            # decision 标记词须落在句首 32 字窗口: 排除"如果采用…"条件句与长段落顺带命中
            if kind == "summary" \
               or (kind == "decision" and any(m in low[:32] for m in DECISION_MARKERS)) \
               or (kind == "preference" and any(m in low[:24] for m in PREFERENCE_MARKERS)):
                key = s[:120]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"snapshot_id": sid, "kind": kind, "content": s,
                              "project": project, "block_idx": i})
        if len(items) >= MAX_MEMORY_PER_SNAPSHOT:
            break
    return items


# ---------------- MemoryEngine 协议 (外部引擎接口位) ----------------
# 内置 rule 引擎为零依赖默认; 外部引擎 (Supermemory/Graphiti/自定义 embedding 引擎)
# 通过 CV_MEMORY_ENGINE="path.to.module:ClassName" 或 --engine 加载, 实现同名协议即可
# 替换提取与检索。高级语义记忆 (embedding) 属 Pro 层规划。

ENGINE_PROTOCOL_DOC = ("MemoryEngine 协议: extract(env: dict) -> list[dict] "
                       "(kind/content/project/snapshot_id); "
                       "search(vault, query, limit) -> list[dict] "
                       "(kind/content/project/snapshot/score)")


class RuleMemoryEngine:
    """内置规则引擎 v0 — extract_memories/memory_search_api 的协议化封装。"""
    name = "rule"

    def extract(self, env: dict) -> list:
        return extract_memories(env)

    def search(self, vault: Path, query: str, limit: int) -> list:
        return memory_search_api(vault, query, limit)


def load_memory_engine(name: str):
    if name == "rule" or not name:
        return RuleMemoryEngine()
    mod_path, _, cls_name = name.partition(":")
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name or "Engine")()


def extract_memories_incremental(
    vault: Path,
    engine_name: str | None = None,
    force: bool = False,
    snapshot_id: str | None = None,
) -> dict:
    """增量记忆提取核心函数 (CLI cmd_memory / HTTP POST /memory/extract / POST /sync 共用)。"""
    from cv import db, now_iso, SCHEMA
    eng_name = engine_name or os.environ.get("CV_MEMORY_ENGINE", "rule")
    engine = load_memory_engine(eng_name)
    conn = db(vault)
    try:
        conn.executescript(SCHEMA + MEMORY_TABLE)
        done_sids = set()
        if not force and not snapshot_id:
            done_sids = {r[0] for r in conn.execute(
                "SELECT snapshot_id FROM memory_extracted WHERE engine=?", (engine.name,))}
        snap_dir = vault / "snapshots"
        files = ([snap_dir / f"{snapshot_id}.json"] if snapshot_id
                 else sorted(snap_dir.glob("cv_*.json")) if snap_dir.exists() else [])
        added = 0
        scanned = 0
        for f in files:
            if not f.exists():
                continue
            sid = f.stem
            if sid in done_sids:
                continue
            try:
                env = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(env, dict) or not isinstance(env.get("capture"), dict) \
                    or not isinstance(env.get("conversation"), list):
                continue
            env["_sid"] = sid
            scanned += 1
            conn.execute("DELETE FROM memory_items WHERE snapshot_id=?", (sid,))
            for it in engine.extract(env):
                conn.execute("INSERT INTO memory_items(created_at,snapshot_id,kind,content,project)"
                             " VALUES(?,?,?,?,?)",
                             (now_iso(), it["snapshot_id"], it["kind"], it["content"], it["project"]))
                added += 1
            conn.execute("INSERT OR REPLACE INTO memory_extracted(snapshot_id,engine,extracted_at)"
                         " VALUES(?,?,?)", (sid, engine.name, now_iso()))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
    finally:
        conn.close()
    return {"engine": engine.name, "scanned": scanned, "added": added, "total": total, "by_kind": by}


def memory_dedup(vault: Path) -> dict:
    """合并 (kind, content) 完全相同的跨快照重复摘录: 保留最早一条 (引用最早快照),
    其余删除。快照不可变, memory_items 是纯派生数据, 随时可 --force 重建。
    (CLI memory dedup / 控制台"去重合并"/ POST /memory/dedup 共用。)"""
    from cv import db, SCHEMA
    conn = db(vault)
    try:
        conn.executescript(SCHEMA + MEMORY_TABLE)
        cur = conn.execute(
            "DELETE FROM memory_items WHERE id NOT IN"
            " (SELECT MIN(id) FROM memory_items GROUP BY kind, content)")
        removed = max(0, cur.rowcount)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
    finally:
        conn.close()
    return {"removed": removed, "total": total, "by_kind": by}


def memory_counts_api(vault: Path) -> dict:
    """统计各类型跨会话记忆数量 (无库时不建库)。unique = 去重后条数。"""
    from cv import db
    if not (vault / "vault.db").exists():
        return {"total": 0, "unique": 0, "by_kind": {}}
    conn = db(vault)
    try:
        try:
            by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
            unique = conn.execute("SELECT COUNT(*) FROM"
                                  " (SELECT DISTINCT kind, content FROM memory_items)").fetchone()[0]
        except sqlite3.OperationalError:
            by, unique = {}, 0
    finally:
        conn.close()
    return {"total": sum(by.values()), "unique": unique, "by_kind": by}


def memory_search_api(vault: Path, query: str, limit: int = 8, kind: str | None = None) -> list:
    """serve /memory 与 MCP vault_remember 共用。query 为空 = 返回最新 N 条 (去重速览)。"""
    from cv import db, now_iso, SCHEMA
    conn = db(vault)
    try:
        conn.executescript(SCHEMA + MEMORY_TABLE)
        terms = tokenize(query)
        k_norm = (kind or "").strip().lower()
        if k_norm and k_norm != "all":
            rows = conn.execute("SELECT * FROM memory_items WHERE kind=?", (k_norm,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory_items").fetchall()
        if not terms:  # 速览模式: 按 id 倒序取最新且按 (kind, content) 去重, 消除多版本快照重复刷屏
            rows.sort(key=lambda r: r["id"], reverse=True)
            out, seen = [], set()
            for r in rows:
                key = (r["kind"], (r["content"] or "").strip())
                if key in seen:
                    continue
                seen.add(key)
                out.append({"kind": r["kind"], "content": r["content"], "project": r["project"],
                            "snapshot": r["snapshot_id"], "hits": r["hits"] or 0, "score": 0})
                if len(out) >= limit:
                    break
            return out
        scored = []
        for r in rows:
            low = r["content"].lower()
            n = sum(low.count(t) for t in terms)
            if n:
                scored.append((n + (3 if r["kind"] == "summary" else 0), r["id"], r))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out, seen = [], set()
        for n, _, r in scored:
            key = (r["kind"], (r["content"] or "").strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": r["kind"], "content": r["content"], "project": r["project"],
                        "snapshot": r["snapshot_id"], "hits": (r["hits"] or 0) + 1, "score": n})
            conn.execute("UPDATE memory_items SET hits=hits+1, last_used=? WHERE id=?",
                         (now_iso(), r["id"]))
            if len(out) >= limit:
                break
        conn.commit()
        return out
    finally:
        conn.close()
