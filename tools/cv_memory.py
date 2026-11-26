"""记忆域纯逻辑层 — 跨会话记忆的提取/去重/检索引擎 (零第三方依赖)。

与 cv_index / cv_ui 同模式: cv.py 只留 CLI/HTTP 薄壳, 本模块可独立演进。
cv 依赖 (db/SCHEMA/now_iso) 一律函数内惰性获取 — 无论 cv 先导入还是本模块
先导入都不会触发循环导入; tokenize/ident_set 来自自包含的 cv_index。

引擎代际 (W4 消融对照, 全部保留可复现):
- rule (v0/v0.1): 宽松标记 → v0.1 加硬噪音闸门 — 高降噪但误杀含行内代码实体的
  决策句与 `- **结论**：…` 结构化裁决行 (真实缺陷, 见 _memory_line_noise_v01);
- v2 (TemporalMemoryEngine, 默认): 双时态记忆 — 出处 (block_idx/快照真实 event_ts)、
  主题签名 topic_key、pitfall/constraint 新类别、跨会话冲突自动覆盖 (superseded_by)、
  同内容跨快照去重改记信心; 行级闸门区分「代码罗列刷屏」与「含技术实体的叙述」。

快照只读铁律: 本模块对 snapshots/ 永远只读; memory_items 是纯派生数据, 可 --force 重建。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from pathlib import Path

from cv_core import atomic_write_text, estimate_text_tokens, truncate_to_tokens
from cv_index import ident_set, match_project_scope, normalize_since, project_match_rank, tokenize
from cv_rank import _ts_ge

# 老库迁移顺序: 表 (IF NOT EXISTS 幂等) → 补列 → 索引 (索引引用新列, 必须最后建)
_MEMORY_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, snapshot_id TEXT,
  kind TEXT, content TEXT, project TEXT,
  hits INTEGER DEFAULT 0, last_used TEXT,
  block_idx INTEGER, event_ts TEXT, lineage_root TEXT, topic_key TEXT,
  status TEXT DEFAULT 'active', superseded_by INTEGER, confidence REAL DEFAULT 1.0,
  pinned INTEGER DEFAULT 0)
"""
_MEMORY_TABLE_BASE = _MEMORY_ITEMS_DDL + """;
CREATE TABLE IF NOT EXISTS memory_extracted(
  snapshot_id TEXT, engine TEXT, extracted_at TEXT,
  PRIMARY KEY (snapshot_id, engine));
"""
_MEMORY_TABLE_INDEXES = ("CREATE INDEX IF NOT EXISTS idx_memory_snap ON memory_items(snapshot_id);\n"
                         "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind);\n"
                         "CREATE INDEX IF NOT EXISTS idx_memory_slot ON memory_items(project, topic_key);\n")
MEMORY_TABLE = _MEMORY_TABLE_BASE + _MEMORY_TABLE_INDEXES

# 老库增量迁移 (ALTER ... ADD COLUMN): 幂等, 已存在即跳过
_V2_COLUMNS = (
    ("block_idx", "INTEGER"),
    ("event_ts", "TEXT"),
    ("lineage_root", "TEXT"),
    ("topic_key", "TEXT"),
    ("status", "TEXT DEFAULT 'active'"),
    ("superseded_by", "INTEGER"),
    ("confidence", "REAL DEFAULT 1.0"),
    ("pinned", "INTEGER DEFAULT 0"),
)


def ensure_memory_schema(conn) -> None:
    """建表 + 老库补列 + 建索引 (memory_items 双时态字段)。老行 status 经 DEFAULT 回填
    'active', block_idx/event_ts 为 NULL = 诚实缺失 (v0.1 时代未记录), 不伪造出处。"""
    conn.executescript(_MEMORY_TABLE_BASE)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    for col, ddl in _V2_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE memory_items ADD COLUMN {col} {ddl}")
    conn.executescript(_MEMORY_TABLE_INDEXES)
    conn.commit()


# ---------------- 标记词库 (引擎 v0.1 原样保留 + v2 扩充) ----------------

DECISION_MARKERS = ("决定", "决策", "采用", "方案是", "结论是", "结论：", "选型",
                    "我们选", "定了", "agreed", "we decide", "let's go with",
                    "chose", "final answer", "the plan is")
PREFERENCE_MARKERS = ("不要", "必须", "总是", "记住", "以后都", "每次都",
                      "never ", "always ", "remember to", "from now on")
PITFALL_MARKERS = ("踩坑", "踩了坑", "根因", "死路", "行不通", "无效", "不奏效",
                   "失败原因", "排雷", "报错原因", "坑：", "坑:",
                   "root cause", "dead end", "does not work", "doesn't work",
                   "didn't work", "gotcha", "pitfall", "failed because",
                   "broken because", "wasted time")
CONSTRAINT_MARKERS = ("铁律", "红线", "绝不", "永远不要", "不可违背", "一律", "禁止",
                      "只能", "不允许",
                      "hard rule", "must never", "never modify", "read-only",
                      "readonly", "invariant", "constraint")
SUPERSEDE_MARKERS = ("改为", "换成", "替换为", "改用", "弃用", "废弃", "不再使用",
                     "不再采用", "推翻", "作废", "以...为准", "以此为准",
                     "switch from", "switch to", "replaced by", "replace with",
                     "deprecate", "deprecated", "instead of", "no longer",
                     "supersede", "revert to", "obsolete")
MAX_MEMORY_PER_SNAPSHOT = 40

# topic_key 停用词 (通用功能词, 非领域词 — 与评测集零关联)
_TOPIC_STOP = frozenset(
    "the and for with this that from into not are was were will can our you your "
    "has have had its their using use used new set get run file line code test "
    "help also just like make made when then than them they been being over "
    "under out down more most some such only same very much many each other "
    "which whose what when where how why who whom whose does doing done".split())

_LIST_PREFIX_RE = re.compile(r"^([-*+>]+|#+|\d+[.、)）])\s*")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _memory_line_noise_v01(s: str) -> bool:
    """v0.1 硬噪音闸门 (保留为消融对照 — 已知缺陷: 单对反引号技术实体与 Markdown
    结构化裁决行被一票误杀)。"""
    if s.endswith(("?", "？")):
        return True
    if s[0] in "-*+#>" or (len(s) > 1 and s[0].isdigit() and s[1] in ".、)）"):
        return True
    return s.count("`") >= 2  # 行内代码密度高 (命令/标识符罗列), 非叙述句


def _memory_line_noise(s: str) -> bool:
    """v2 行级噪音闸门: 区分「纯代码罗列刷屏」与「含行内技术实体的技术裁决叙述」。
    - 问句仍杀; 围栏残留/水平线杀;
    - 列表/标题/引用前缀不再一票杀 (`- **结论**：…` 正是高价值结构化决策行),
      标记词窗口检查在剥前缀后的核心句上照常执行 (见 extract_memories_v2);
    - 单对行内代码标识符 (`FTS5`) 是 topic_key 高价值锚点, 必须保留;
    - ≥2 对反引号 (≥4 个反引号字符) = 标识符罗列刷屏, 杀。"""
    if s.endswith(("?", "？")):
        return True
    if s.count("`") >= 4:
        return True
    if s[:3] in ("---", "===") or s.startswith("```"):
        return True
    return False


def _norm_ts(ts, fallback: str = "") -> str:
    """块内真实时间戳规范化 (event_ts 取会话内真实时间, 绝不用入库时的 now)。"""
    s = str(ts or "").strip()
    if _TS_RE.match(s):
        return s[:19].replace(" ", "T")
    fb = str(fallback or "").strip()
    return fb[:19].replace(" ", "T") if _TS_RE.match(fb) else ""


_FILE_RE = re.compile(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]{1,6}")
_SUBJ_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_LETTER_RE = re.compile(r"[A-Za-z]")

# 决策值引入词: 其后的标识符是「被选中的值」(daily/weekly/mtime/SQLite…) 而非主语,
# 绝不入键 — 否则 A→B→C 槽位永不匹配 (决定：调度策略采用 daily → proj|daily 缺陷),
# 或不同主题的值互相串槽。英文形态按词匹配 (左右各垫一空格后再 find)。
_VALUE_MARKERS = ("采用", "改为", "改用", "换成", "换用", "改成", "替换为", "选择", "选定",
                  "选型", "定为", "定了", "弃用", "废弃", "使用", "方案是",
                  " use ", " using ", " used ", " uses ", " chose ", " chosen ",
                  " picked ", " adopted ", " switch to ", " switched to ",
                  " switch from ", " replaced by ", " replace with ", " instead of ",
                  " go with ", " went with ", " moved to ", " migrated to ",
                  " deprecate ", " deprecated ", " drop ", " dropped ",
                  " prefer ", " settled on ", " opted for ", " opt for ", " no longer ")

# 句首决策引导语 (可安全剥离): 剥掉之后其后的名词性成分才是主语。
# 中文头标记 (决定：/结论：) 只剥前缀; 英文剥离 we agreed to / decided on / let's go with…
# (修复3: 不剥则动词 agreed 成锚 → proj|agreed 跨主题误推翻)。
_HEAD_STRIP_RE = re.compile(
    r"^(?:(?:决定|决策|结论|定案)\s*[：:，,、]?\s*"
    r"|(?:we|they|i|team)\s+(?:have\s+|had\s+)?"
    r"(?:agreed|decided|chose|chosen|opted|settled)\s+(?:to|on|upon|for)\b[,:]?\s*"
    r"|(?:agreed|decided|chose|chosen|opted|settled)\s+(?:to|on|upon|for)\b[,:]?\s*"
    r"|(?:lets|let's)\s+go\s+with\s+"
    r"|final\s+answer\s*[：:,]?\s*"
    r"|the\s+plan\s+is\s*[：:,]?\s*)",
    re.IGNORECASE)

# 英文动词不作主语锚点 (句首动词塌缩 → proj|agreed 类伪槽位; 宁可空键不误覆盖)。
_ANCHOR_VERBS = frozenset(
    "agree agreed decide decided chose choose chosen opt opted picked pick "
    "went go going switched switch replaced replace revert reverted adopted "
    "adopt moved move migrated migrate dropped drop kept keep prefer preferred "
    "removed remove added add changed change tried try let plan planned want "
    "wanted need needed propose proposed suggest suggested settled settle "
    "finalize finalized".split())


def _file_anchor_match(core: str):
    """首个合法文件名锚点 (修复4): 名与扩展名都必须含字母 — 纯数字段 (20.11 /
    3.9 / 127.0.0.1) 是版本号或 IP, 不构成锚点 (真库 goai|1270 槽实证挂 4 条
    互不相关记忆); config.yaml / run.py / v2.py / test_1.py 照常命中。"""
    for m in _FILE_RE.finditer(core):
        stem, _, ext = m.group(0).partition(".")
        if _LETTER_RE.search(stem) and _LETTER_RE.search(ext):
            return m
    return None


def _topic_key(project, content: str) -> str:
    """主题签名 (槽位键): project + 单一主语锚点。
    锚点优先级: ① 文件名模式 (`deploy.yaml` → deployyaml, 工程裁决最经典的主语);
    ② 剥离句首决策引导语 (决定：/we agreed to…) 后、首个决策值引入词之前的
    标识符主语 (camel/snake 归一)。
    决策值 (采用/改为/use 之后的 daily/weekly/SQLite…) 与句首动词 (agreed/decided…)
    绝不入键 — 否则 A→B→C 槽位永不匹配, 或不同主题塌缩到同一伪槽互相误覆盖。
    取不到可靠主语返回空串 (该条只参与精确去重, 不参与跨句覆盖 — 宁可漏覆盖,
    不可误覆盖)。有文件名的路径完全不受影响。"""
    core = str(content or "").strip()
    m = _file_anchor_match(core)
    if m:
        anchor = re.sub(r"[^A-Za-z0-9]", "", m.group(0)).lower()
        return f"{project or ''}|{anchor}"
    core = _HEAD_STRIP_RE.sub("", core, count=1)  # 剥句首引导语, 其后才是主语区
    low = core.lower()
    # 主语区 = 首个决策值引入词之前; 其后全是值 (绝不入键)。
    # 英文标记按词匹配: 左垫空格使句首 "Use x" 与句中 "we use x" 同样命中。
    cut = len(core)
    padded = " " + low
    for mk in _VALUE_MARKERS:
        i = padded.find(mk)
        if i >= 0:
            cut = min(cut, max(0, i - 1))  # 回映射 padded→core 下标 (左垫 1 空格)
    for m2 in _SUBJ_IDENT_RE.finditer(core[:24]):
        if m2.start() >= cut:
            break  # 主语区之后全是决策值, 不再找锚
        tok = m2.group(0).lower()
        if tok in _TOPIC_STOP:
            continue
        if tok in _ANCHOR_VERBS:
            return ""  # 句首动词塌缩 (we agreed to…): 不造 proj|agreed 伪槽
        return f"{project or ''}|{tok}"
    return ""


def _is_superseding(content: str) -> bool:
    low = str(content or "").lower()
    return any(m in low for m in SUPERSEDE_MARKERS)


# ---------------- v0/v0.1 规则引擎 (消融对照, 原样保留) ----------------

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
            if kind != "summary" and _memory_line_noise_v01(s):
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


def extract_memories_v2(env: dict) -> list:
    """双时态提取 (v2): 五类别 (summary/decision/preference/pitfall/constraint) + 出处
    (block_idx / 块内真实 event_ts / lineage 链) + topic_key 实体签名。"""
    items, sid = [], env.get("_sid") or "?"
    capture = env.get("capture") or {}
    project = None
    cwd = capture.get("cwd")
    if cwd:
        project = str(cwd).rstrip("\\").rsplit("\\", 1)[-1] or str(cwd)
    lin = env.get("lineage") or {}
    lineage_root = str(lin.get("parent_snapshot") or sid)
    env_ts = env.get("created_at") or ""
    seen = set()
    for i, b in enumerate(env.get("conversation") or []):
        if not isinstance(b, dict):
            continue
        kind, text = None, ""
        if b.get("type") == "compaction":
            kind, text = "summary", b.get("summary", "")
        elif b.get("type") == "text" and b.get("role") == "assistant":
            text = b.get("content", "")
            low_all = text.lower()
            if any(m in low_all for m in CONSTRAINT_MARKERS):
                kind = "constraint"
            elif any(m in low_all for m in PITFALL_MARKERS):
                kind = "pitfall"
            elif any(m in low_all for m in DECISION_MARKERS):
                kind = "decision"
        elif b.get("type") == "text" and b.get("role") == "user":
            text = b.get("content", "")
            low_all = text.lower()
            if any(m in low_all for m in CONSTRAINT_MARKERS):
                kind = "constraint"
            elif any(m in low_all for m in PITFALL_MARKERS):
                kind = "pitfall"
            elif any(m in low_all for m in PREFERENCE_MARKERS):
                kind = "preference"
        if not kind or not text:
            continue
        block_ts = _norm_ts(b.get("ts"), env_ts)
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
            core = _LIST_PREFIX_RE.sub("", s).strip()
            low = core.lower()
            ok = (kind == "summary"
                  or (kind == "decision" and any(m in low[:32] for m in DECISION_MARKERS))
                  or (kind == "preference" and any(m in low[:24] for m in PREFERENCE_MARKERS))
                  or (kind in ("pitfall", "constraint") and any(m in low[:48] for m in
                      (PITFALL_MARKERS if kind == "pitfall" else CONSTRAINT_MARKERS))))
            if not ok:
                continue
            key = (kind, core[:120])
            if key in seen:
                continue
            seen.add(key)
            items.append({"snapshot_id": sid, "kind": kind, "content": core,
                          "project": project, "block_idx": i,
                          "event_ts": block_ts, "lineage_root": lineage_root,
                          "topic_key": _topic_key(project, core)})
        if len(items) >= MAX_MEMORY_PER_SNAPSHOT:
            break
    return items


# ---------------- MemoryEngine 协议 (外部引擎接口位) ----------------
# 内置 v2 引擎为零依赖默认; 外部引擎 (Supermemory/Graphiti/自定义 embedding 引擎)
# 通过 CV_MEMORY_ENGINE="path.to.module:ClassName" 或 --engine 加载, 实现同名协议即可
# 替换提取与检索。高级语义记忆 (embedding) 属 Pro 层规划。

ENGINE_PROTOCOL_DOC = ("MemoryEngine 协议: extract(env: dict) -> list[dict] "
                       "(kind/content/project/snapshot_id[, block_idx/event_ts/topic_key]); "
                       "search(vault, query, limit[, include_history]) -> list[dict] "
                       "(kind/content/project/snapshot/score/status)")


class RuleMemoryEngine:
    """内置规则引擎 v0/v0.1 — 语义原样保留, 作为 W4 消融的 Baseline 对照。"""
    name = "rule"

    def extract(self, env: dict) -> list:
        return extract_memories(env)

    def search(self, vault: Path, query: str, limit: int) -> list:
        return memory_search_api(vault, query, limit)


class TemporalMemoryEngine:
    """内置双时态记忆引擎 v2 (默认): 出处 + 主题签名 + 冲突覆盖 + 去重记信心。"""
    name = "v2"

    def extract(self, env: dict) -> list:
        return extract_memories_v2(env)

    def search(self, vault: Path, query: str, limit: int, include_history: bool = False) -> list:
        return memory_search_api(vault, query, limit, include_history=include_history)


def load_memory_engine(name: str):
    if name == "rule":
        return RuleMemoryEngine()
    if name in ("v2", "temporal", "temporal-v2", ""):
        return TemporalMemoryEngine()
    mod_path, _, cls_name = name.partition(":")
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name or "Engine")()


def supersede_pass(conn, items: list) -> int:
    """对一批新入库条目执行槽位冲突覆盖: 同 (project, topic_key) 且带推翻语义的新记忆,
    将更早的 active 记忆标 superseded 并记 superseded_by。items: [(id, project,
    topic_key, content, event_ts)]。返回覆盖条数。"""
    n = 0
    for iid, project, tk, content, ets in items:
        if not tk or not _is_superseding(content):
            continue
        rows = conn.execute(
            "SELECT id FROM memory_items WHERE project IS ? AND topic_key=? AND status='active'"
            " AND id<>? AND COALESCE(event_ts,'')<=COALESCE(?, '')"
            " ORDER BY COALESCE(event_ts,''), id",
            (project, tk, iid, ets or "")).fetchall()
        for (oid,) in rows:
            conn.execute("UPDATE memory_items SET status='superseded', superseded_by=?"
                         " WHERE id=?", (iid, oid))
            n += 1
    return n


def recompute_supersession(conn) -> int:
    """全量重算覆盖关系 (幂等): 按会话真实时间序重放, 显式推翻语义将同槽位更早的
    active 记忆标 superseded。快照只读, memory_items 纯派生 — 任何时刻可重放。"""
    conn.execute("UPDATE memory_items SET status='active', superseded_by=NULL"
                 " WHERE status='superseded'")
    rows = conn.execute(
        "SELECT id, project, topic_key, content, COALESCE(event_ts,'') AS ets"
        " FROM memory_items WHERE topic_key IS NOT NULL AND topic_key<>''"
        " ORDER BY COALESCE(event_ts,''), id").fetchall()
    active_by_slot: dict = {}
    n = 0
    for r in rows:
        slot = (r["project"] or "", r["topic_key"])
        if _is_superseding(r["content"]):
            for oid in active_by_slot.get(slot, ()):
                conn.execute("UPDATE memory_items SET status='superseded', superseded_by=?"
                             " WHERE id=?", (r["id"], oid))
                n += 1
            active_by_slot[slot] = [r["id"]]  # 推翻后槽位以新记忆为准
        else:
            active_by_slot.setdefault(slot, []).append(r["id"])
    conn.commit()
    return n


def extract_memories_incremental(
    vault: Path,
    engine_name: str | None = None,
    force: bool = False,
    snapshot_id: str | None = None,
) -> dict:
    """增量记忆提取核心函数 (CLI cmd_memory / HTTP POST /memory/extract / POST /sync 共用)。
    v2: 同 (kind, content) 的 active 记忆跨快照去重 — 改记 confidence (+0.1, 上限 5) 不重复
    入库 (消灭 v0 时代 5.5 万条重复刷屏); 提取完成后全量重放覆盖关系 (幂等)。"""
    from cv import db, now_iso, SCHEMA
    eng_name = engine_name or os.environ.get("CV_MEMORY_ENGINE", "v2")
    engine = load_memory_engine(eng_name)
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        ensure_memory_schema(conn)
        done_sids = set()
        if not force and not snapshot_id:
            done_sids = {r[0] for r in conn.execute(
                "SELECT snapshot_id FROM memory_extracted WHERE engine=?", (engine.name,))}
        snap_dir = vault / "snapshots"
        files = ([snap_dir / f"{snapshot_id}.json"] if snapshot_id
                 else sorted(snap_dir.glob("cv_*.json")) if snap_dir.exists() else [])
        added = 0
        reaffirmed = 0
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
                dup = conn.execute(
                    "SELECT id FROM memory_items WHERE kind=? AND content=? AND status='active'"
                    " LIMIT 1", (it["kind"], it["content"])).fetchone()
                if dup:
                    conn.execute("UPDATE memory_items SET confidence=MIN(COALESCE(confidence,1.0)"
                                 "+0.1, 5.0) WHERE id=?", (dup["id"],))
                    reaffirmed += 1
                    continue
                ets = _norm_ts(it.get("event_ts"), env.get("created_at") or "")
                conn.execute(
                    "INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,"
                    "block_idx,event_ts,lineage_root,topic_key,status,confidence)"
                    " VALUES(?,?,?,?,?,?,?,?,?,'active',1.0)",
                    (now_iso(), it["snapshot_id"], it["kind"], it["content"], it["project"],
                     it.get("block_idx"), ets or None, it.get("lineage_root"),
                     it.get("topic_key") or None))
                added += 1
            conn.execute("INSERT OR REPLACE INTO memory_extracted(snapshot_id,engine,extracted_at)"
                         " VALUES(?,?,?)", (sid, engine.name, now_iso()))
        conn.commit()
        superseded = recompute_supersession(conn)
        total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memory_items WHERE status='active'").fetchone()[0]
        by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
    finally:
        conn.close()
    return {"engine": engine.name, "scanned": scanned, "added": added,
            "reaffirmed": reaffirmed, "superseded": superseded,
            "total": total, "active": active, "by_kind": by}


def memory_dedup(vault: Path) -> dict:
    """合并 (kind, content) 完全相同的跨快照重复摘录: 组内优先保留 status='active'
    的最新一条 (多个 active 取 id 最大); 组内无 active 才回退最早一条 MIN(id)。
    旧规则盲取 MIN(id) 会删掉 active 副本、留下 superseded 旧行 — 该内容从默认
    检索消失 (真实缺陷: A(daily) 被 B 推翻后新快照重申 daily 产生新 active 行,
    去重删掉的恰是新 active)。去重后全量重放 recompute_supersession (幂等),
    修复被删行牵动的覆盖链。快照不可变, memory_items 是纯派生数据, 随时可
    --force 重建。(CLI memory dedup / 控制台"去重合并"/ POST /memory/dedup 共用。)"""
    from cv import db, SCHEMA
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        ensure_memory_schema(conn)
        cur = conn.execute(
            "DELETE FROM memory_items WHERE id NOT IN"
            " (SELECT COALESCE(MAX(CASE WHEN status='active' THEN id END), MIN(id))"
            "  FROM memory_items GROUP BY kind, content)")
        removed = max(0, cur.rowcount)
        conn.commit()
        resuperseded = recompute_supersession(conn)  # 幂等重放, 顺带清悬挂 superseded_by
        total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
    finally:
        conn.close()
    return {"removed": removed, "total": total, "by_kind": by, "resuperseded": resuperseded}


def memory_counts_api(vault: Path) -> dict:
    """统计各类型跨会话记忆数量 (无库时不建库)。unique = 去重后条数; active = 当前有效。"""
    from cv import db
    if not (vault / "vault.db").exists():
        return {"total": 0, "unique": 0, "active": 0, "by_kind": {}, "by_status": {}}
    conn = db(vault)
    try:
        try:
            by = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")}
            unique = conn.execute("SELECT COUNT(*) FROM"
                                  " (SELECT DISTINCT kind, content FROM memory_items)").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM memory_items WHERE status='active'").fetchone()[0]
            by_status = {r[0]: r[1] for r in
                         conn.execute("SELECT status, COUNT(*) FROM memory_items GROUP BY status")}
        except sqlite3.OperationalError:
            by, unique, active, by_status = {}, 0, 0, {}
    finally:
        conn.close()
    return {"total": sum(by.values()), "unique": unique, "active": active,
            "by_kind": by, "by_status": by_status}


def memory_search_api(vault: Path, query: str, limit: int = 8, kind: str | None = None,
                      include_history: bool = False, project: str | None = None,
                      since: str | None = None) -> list:
    """serve /memory 与 MCP vault_remember 共用。默认只返回 status='active' 的当前有效
    记忆 (过期记忆不污染上下文); include_history=True 追溯决策演变链 (含 superseded,
    带指向新记忆的 superseded_by)。query 为空 = 返回最新 N 条 (去重速览)。"""
    from cv import db, now_iso, SCHEMA
    since_iso = normalize_since(since)
    proj_filter = (project or "").strip()
    conn = db(vault)
    try:
        try:
            conn.executescript(SCHEMA)
            ensure_memory_schema(conn)
        except sqlite3.OperationalError:
            pass  # MCP 修复6: 只读挂载 vault.db 时建表/补列必失败 — 不致命, 表已在则照常检索
        limit = max(1, int(limit))
        terms = tokenize(query)
        k_norm = (kind or "").strip().lower()
        if k_norm and k_norm != "all" and k_norm not in VALID_MEMORY_KINDS:
            raise ValueError(f"不支持的记忆类型: {k_norm} (可选: {', '.join(VALID_MEMORY_KINDS)})")
        status_clause = "" if include_history else " AND status='active'"
        try:
            if k_norm and k_norm != "all":
                rows = conn.execute("SELECT * FROM memory_items WHERE kind=?" + status_clause,
                                    (k_norm,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM memory_items WHERE 1=1" + status_clause).fetchall()
        except sqlite3.OperationalError:
            return []  # 只读且零表: 无记忆可检索, 与空库同口径 (修复6)
        if proj_filter or since_iso:
            filtered = []
            for r in rows:
                if proj_filter and not match_project_scope(r["project"] or "", proj_filter):
                    continue
                if since_iso:
                    keys = r.keys()
                    ev = r["event_ts"] if ("event_ts" in keys and r["event_ts"]) else r["created_at"]
                    if not _ts_ge(str(ev or ""), since_iso):
                        continue
                filtered.append(r)
            rows = filtered
        if not terms:  # 速览模式: 置顶优先, 再按 id 倒序取最新且按 (kind, content) 去重
            rows.sort(
                key=lambda r: (
                    1 if ("pinned" in r.keys() and r["pinned"]) else 0,
                    r["id"],
                ),
                reverse=True,
            )
            out, seen = [], set()
            for r in rows:
                key = (r["kind"], (r["content"] or "").strip())
                if key in seen:
                    continue
                seen.add(key)
                out.append(_memory_row_api(r, 0))
                if len(out) >= limit:
                    break
            return out
        scored = []
        for r in rows:
            low = (r["content"] or "").lower()
            n = sum(low.count(t) for t in terms)
            if n:
                is_pin = 1 if ("pinned" in r.keys() and r["pinned"]) else 0
                scored.append((n + (3 if r["kind"] == "summary" else 0) + (5 if is_pin else 0), is_pin, r["id"], r))
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        out, seen = [], set()
        for n, _, _, r in scored:
            key = (r["kind"], (r["content"] or "").strip())
            if key in seen:
                continue
            seen.add(key)
            out.append(_memory_row_api(r, n))
            try:  # MCP 修复6: hits/last_used 是纯遥测, 只读挂载下写失败静默放弃, 检索结果照常返回
                conn.execute("UPDATE memory_items SET hits=hits+1, last_used=? WHERE id=?",
                             (now_iso(), r["id"]))
                conn.commit()
            except sqlite3.OperationalError:
                pass
            if len(out) >= limit:
                break
        try:
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 同修复6: 只读挂载下无写入可提交
        return out
    finally:
        conn.close()


VALID_MEMORY_KINDS = ("decision", "preference", "pitfall", "constraint", "summary")


def memory_projects_api(vault: Path) -> list:
    """只读统计当前有效记忆涉及的项目清单 (按条数降序), 供规则编译器自动补全。"""
    from cv import db
    if not (vault / "vault.db").exists():
        return []
    conn = db(vault)
    try:
        has_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items'"
        ).fetchone()
        if not has_tbl:
            return []
        try:
            ensure_memory_schema(conn)
        except sqlite3.OperationalError:
            pass
        try:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(project), ''), 'manual') AS proj, COUNT(*) AS n "
                "FROM memory_items WHERE status='active' GROUP BY proj ORDER BY n DESC, proj ASC"
            ).fetchall()
            return [{"project": r["proj"], "count": r["n"]} for r in rows]
        except sqlite3.OperationalError:
            try:
                rows = conn.execute(
                    "SELECT COALESCE(NULLIF(TRIM(project), ''), 'manual') AS proj, COUNT(*) AS n "
                    "FROM memory_items GROUP BY proj ORDER BY n DESC, proj ASC"
                ).fetchall()
                return [{"project": r["proj"], "count": r["n"]} for r in rows]
            except sqlite3.OperationalError:
                return []
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def memory_add_manual(
    vault: Path,
    content: str,
    kind: str = "decision",
    project: str = "manual",
    pinned: bool = False,
    supersedes: int | None = None,
    source: str = "manual",
) -> dict:
    """手动/Agent 写入一条长期记忆 (高置信度, 支持置顶、显式推翻旧记忆与幂等去重)。"""
    from cv import db, now_iso, SCHEMA
    txt = str(content or "").strip()
    if not txt:
        raise ValueError("记忆内容不能为空")
    if len(txt) > 2000:
        txt = txt[:2000]
    k = str(kind or "decision").strip().lower()
    if k not in VALID_MEMORY_KINDS:
        raise ValueError(f"不支持的记忆类型: {k} (可选: {', '.join(VALID_MEMORY_KINDS)})")
    proj_raw = str(project or "manual").strip() or "manual"
    if len(proj_raw) <= 512:
        proj = proj_raw
    else:
        base = proj_raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        proj = (base[:120] if base else proj_raw[-512:])
    src_id = (str(source or "manual").strip() or "manual")[:64]
    pin_i = 1 if pinned else 0
    sup_id: int | None = None
    if supersedes is not None:
        if isinstance(supersedes, bool):
            raise ValueError("supersedes 必须为正整数")
        try:
            sup_id = int(supersedes)
        except (TypeError, ValueError):
            raise ValueError("supersedes 必须为正整数")
        if sup_id <= 0:
            raise ValueError("supersedes 必须为正整数")
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        ensure_memory_schema(conn)
        if sup_id is not None:
            target_row = conn.execute(
                "SELECT id, status FROM memory_items WHERE id=?", (sup_id,)
            ).fetchone()
            if target_row is None:
                raise ValueError(f"supersedes 目标记忆不存在: #{sup_id}")
        dup = conn.execute(
            "SELECT id, pinned, snapshot_id FROM memory_items "
            "WHERE kind=? AND content=? "
            "AND lower(COALESCE(NULLIF(TRIM(project), ''), 'manual'))=lower(?) "
            "AND status='active' LIMIT 1",
            (k, txt, proj),
        ).fetchone()
        if dup:
            mid = dup["id"]
            if sup_id is not None and sup_id == mid:
                raise ValueError("不能用记忆自身推翻自己")
            new_pin = 1 if (pin_i or (dup["pinned"] or 0)) else 0
            conn.execute(
                "UPDATE memory_items SET confidence=5.0, pinned=?, project=? WHERE id=?",
                (new_pin, proj, mid),
            )
            superseded_id = None
            if sup_id is not None:
                scur = conn.execute(
                    "UPDATE memory_items SET status='superseded', superseded_by=? "
                    "WHERE id=? AND status='active'",
                    (mid, sup_id),
                )
                superseded_id = sup_id if scur.rowcount else None
            conn.commit()
            return {"id": mid, "kind": k, "project": proj, "content": txt,
                    "pinned": bool(new_pin), "existed": True,
                    "source": dup["snapshot_id"] or src_id, "superseded_id": superseded_id}
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,"
            "event_ts,status,confidence,pinned) VALUES(?,?,?,?,?,?,'active',5.0,?)",
            (now, src_id, k, txt, proj, now, pin_i),
        )
        mid = cur.lastrowid
        superseded_id = None
        if sup_id is not None:
            scur = conn.execute(
                "UPDATE memory_items SET status='superseded', superseded_by=? "
                "WHERE id=? AND status='active'",
                (mid, sup_id),
            )
            superseded_id = sup_id if scur.rowcount else None
        conn.commit()
        return {"id": mid, "kind": k, "project": proj, "content": txt,
                "pinned": bool(pin_i), "existed": False,
                "source": src_id, "superseded_id": superseded_id}
    finally:
        conn.close()


def memory_delete(vault: Path, mem_id: int) -> dict:
    """按 id 删除单条记忆 (仅删派生索引表, 永不修改快照文件)。"""
    from cv import db, SCHEMA
    if isinstance(mem_id, bool):
        raise ValueError("id 必须为正整数")
    mid = int(mem_id)
    if mid <= 0:
        raise ValueError("id 必须为正整数")
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        ensure_memory_schema(conn)
        cur = conn.execute("DELETE FROM memory_items WHERE id=?", (mid,))
        conn.commit()
        return {"id": mid, "deleted": cur.rowcount}
    finally:
        conn.close()


def memory_set_pin(vault: Path, mem_id: int, pinned: bool = True) -> dict:
    """切换单条记忆的置顶状态。"""
    from cv import db, SCHEMA
    if isinstance(mem_id, bool):
        raise ValueError("id 必须为正整数")
    mid = int(mem_id)
    if mid <= 0:
        raise ValueError("id 必须为正整数")
    pin_i = 1 if pinned else 0
    conn = db(vault)
    try:
        conn.executescript(SCHEMA)
        ensure_memory_schema(conn)
        cur = conn.execute("UPDATE memory_items SET pinned=? WHERE id=?", (pin_i, mid))
        conn.commit()
        return {"id": mid, "pinned": bool(pin_i), "updated": cur.rowcount}
    finally:
        conn.close()


def _memory_row_api(r, score: int) -> dict:
    """检索行 → API 字典 (v0 键全保留, v2 出处/双时态键 + id/pinned 增量附加)。"""
    keys = r.keys()
    return {"id": r["id"], "kind": r["kind"], "content": r["content"], "project": r["project"],
            "snapshot": r["snapshot_id"], "hits": (r["hits"] or 0) + (1 if score else 0),
            "score": score,
            "block_idx": r["block_idx"], "event_ts": r["event_ts"],
            "topic_key": r["topic_key"], "status": r["status"] or "active",
            "superseded_by": r["superseded_by"], "confidence": r["confidence"],
            "pinned": bool(r["pinned"]) if "pinned" in keys else False}


RULE_TARGETS = {
    "agents": "AGENTS.md",
    "claude": "CLAUDE.md",
    "cursor": ".cursorrules",
}
_RULE_KIND_ORDER = ("constraint", "decision", "pitfall", "preference", "summary")
_RULE_KIND_HEADINGS = {
    "constraint": "### 架构红线与硬约束 (Constraints)",
    "decision": "### 核心技术决策 (Decisions)",
    "pitfall": "### 已知踩坑与禁区 (Pitfalls)",
    "preference": "### 工程与编码偏好 (Preferences)",
    "summary": "### 阶段摘要 (Summaries)",
}
_RULES_SENTINEL_RE = re.compile(
    r"<!--\s*context-vault:rules:start\b[^>]*-->.*?<!--\s*context-vault:rules:end\s*-->",
    re.DOTALL,
)


def _resolve_rule_out_file(conn: sqlite3.Connection | None, proj: str, tgt: str,
                           out_path: str | Path | None) -> Path | None:
    if out_path is not None and str(out_path).strip():
        return Path(out_path).expanduser().resolve()
    cand_dir = Path(proj).expanduser()
    if cand_dir.is_dir():
        return cand_dir.resolve() / RULE_TARGETS[tgt]
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT cwd, MAX(created_at) AS ts FROM snapshots "
                "WHERE cwd IS NOT NULL AND cwd != '' GROUP BY cwd ORDER BY ts DESC"
            ).fetchall()
            best_key = None
            best_dir: Path | None = None
            for r in rows:
                cwd = str(r["cwd"] or "")
                rank = project_match_rank(cwd, proj)
                if rank <= 0:
                    continue
                p = Path(cwd).expanduser()
                if not p.is_dir():
                    continue
                cand_key = (rank, str(r["ts"] or ""))
                if best_key is None or cand_key > best_key:
                    best_key = cand_key
                    best_dir = p.resolve()
            if best_dir is not None:
                return best_dir / RULE_TARGETS[tgt]
        except sqlite3.OperationalError:
            pass
    return None


def _rule_provenance(r) -> str:
    keys = r.keys()
    sid = str(r["snapshot_id"] or "")
    bidx = r["block_idx"] if "block_idx" in keys else None
    ev = (r["event_ts"] if "event_ts" in keys and r["event_ts"] else r["created_at"]) or ""
    day = str(ev)[:10]
    if sid.startswith("cv_"):
        ref = sid[:11] + (f"#b{bidx}" if bidx is not None else "")
    else:
        ref = sid or "manual"
    return f"{ref} · {day}" if day else ref


def _safe_attr(val: str) -> str:
    return str(val or "").replace('"', "").replace("-->", "").strip()


def compile_project_rules(
    vault: Path,
    project: str,
    *,
    target: str = "agents",
    out_path: str | Path | None = None,
    budget_tokens: int = 2000,
    include_summary: bool = False,
    apply: bool = False,
) -> dict:
    """将项目活跃记忆按优先级与 token 预算编译为受哨兵保护的 AGENTS.md / CLAUDE.md / .cursorrules。
    默认 dry-run (不触盘); apply=True 时保留哨兵外手写内容并在覆盖前生成 .bak-cv 备份。"""
    from cv import db

    proj = str(project or "").strip()
    if not proj:
        raise ValueError("project 不能为空")
    tgt = str(target or "agents").strip().lower()
    if tgt not in RULE_TARGETS:
        raise ValueError(f"不支持的规则目标: {tgt} (可选: {', '.join(RULE_TARGETS)})")
    if isinstance(budget_tokens, bool):
        raise ValueError("budget_tokens 必须为整数")
    try:
        bt = int(budget_tokens)
    except (TypeError, ValueError):
        raise ValueError("budget_tokens 必须为整数")
    if bt < 200 or bt > 32000:
        raise ValueError("budget_tokens 必须在 200..32000 之间")

    proj_label = Path(proj).name if ("/" in proj or "\\" in proj) and Path(proj).name else proj
    rows = []
    out_file: Path | None = None
    db_file = vault / "vault.db"
    conn = db(vault) if db_file.exists() else None
    try:
        if conn is not None:
            raw_rows = []
            try:
                has_tbl = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items'"
                ).fetchone()
            except sqlite3.OperationalError:
                has_tbl = None
            if has_tbl:
                try:
                    ensure_memory_schema(conn)
                except sqlite3.OperationalError:
                    pass
                try:
                    raw_rows = conn.execute(
                        "SELECT * FROM memory_items WHERE status='active'"
                    ).fetchall()
                except sqlite3.OperationalError:
                    try:
                        raw_rows = conn.execute("SELECT * FROM memory_items").fetchall()
                    except sqlite3.OperationalError:
                        raw_rows = []
            for r in raw_rows:
                if not include_summary and r["kind"] == "summary":
                    continue
                if match_project_scope(r["project"] or "", proj):
                    rows.append(r)
        out_file = _resolve_rule_out_file(conn, proj, tgt, out_path)
    finally:
        if conn is not None:
            conn.close()

    if out_file is not None and out_file.exists() and out_file.is_dir():
        raise ValueError(f"目标路径是目录而非文件: {out_file}")
    if apply and out_file is None:
        raise ValueError(f"无法定位项目 {proj!r} 的本地目录，请通过 out_path / --out 指定目标文件")

    # 同 (kind, content) 去重并按 (pinned, confidence, hits, id) 选最高优一条
    rank_by_kind = {k: i for i, k in enumerate(_RULE_KIND_ORDER)}
    best: dict = {}
    for r in rows:
        keys = r.keys()
        k = str(r["kind"] or "decision")
        txt = re.sub(r"\s+", " ", str(r["content"] or "")).strip()
        if not txt or k not in rank_by_kind:
            continue
        pin = 1 if ("pinned" in keys and r["pinned"]) else 0
        conf = float(r["confidence"] or 0.0) if "confidence" in keys else 1.0
        hits = int(r["hits"] or 0) if "hits" in keys else 0
        score_key = (pin, conf, hits, int(r["id"] or 0))
        dedup_k = (k, txt)
        if dedup_k not in best or score_key > best[dedup_k][0]:
            best[dedup_k] = (score_key, r, txt)

    # 装填优先级: 红线 constraint > 其余类型置顶条目 > decision > pitfall > preference > summary
    ordered = sorted(
        best.values(),
        key=lambda item: (
            0 if item[1]["kind"] == "constraint" else (1 if item[0][0] else 2),
            rank_by_kind[item[1]["kind"]],
            -item[0][0],
            -item[0][1],
            -item[0][2],
            -item[0][3],
        ),
    )

    start_tag = f'<!-- context-vault:rules:start project="{_safe_attr(proj_label)}" target="{tgt}" -->'
    end_tag = "<!-- context-vault:rules:end -->"

    def _render_block(selected_by_kind: dict, total_n: int, kept_n: int) -> str:
        hdr_note = (
            f"> Auto-compiled from active project memories "
            f"(budget: {bt} tok, {kept_n}/{total_n} items). "
            f"Do not edit inside markers manually; re-run `cv memory compile-rules` to update."
        )
        lines = [
            start_tag,
            f"## Project Rules — {proj_label} (Compiled by Context Vault)",
            "",
            hdr_note,
        ]
        if not total_n:
            lines += ["", f"*(当前项目 `{proj_label}` 暂无有效长期记忆，可在控制台手动录入或通过 MCP `vault_memorize` 写入。)*"]
        elif not kept_n:
            lines += ["", f"*(已匹配 {total_n} 条记忆，但当前预算 `{bt}` tokens 过窄，请调大 `--budget`。)*"]
        else:
            for k in _RULE_KIND_ORDER:
                sec_items = selected_by_kind.get(k)
                if not sec_items:
                    continue
                lines += ["", _RULE_KIND_HEADINGS[k], ""]
                lines.extend(sec_items)
        lines.append("")
        lines.append(end_tag)
        return "\n".join(lines)

    total_matched = len(ordered)
    selected_by_kind: dict[str, list[str]] = {}
    by_kind_counts: dict[str, int] = {}
    included = 0
    dropped = 0

    for score_key, r, txt in ordered:
        k = str(r["kind"])
        safe_txt = txt.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
        pin_mark = "**[置顶]** " if score_key[0] else ""
        prov = _rule_provenance(r)
        bullet = f"- {pin_mark}{safe_txt} *(来源: {prov})*"
        cand_map = {ck: list(cv_lst) for ck, cv_lst in selected_by_kind.items()}
        cand_map.setdefault(k, []).append(bullet)
        cand_md = _render_block(cand_map, total_matched, included + 1)
        if estimate_text_tokens(cand_md) <= bt:
            selected_by_kind = cand_map
            by_kind_counts[k] = by_kind_counts.get(k, 0) + 1
            included += 1
        elif included == 0:
            # 首条就超出严格预算时按真实剩余 token 预算截断单行文本, 保证合法非空输出
            overhead_md = _render_block({k: [f"- {pin_mark} *(来源: {prov})*"]}, total_matched, 1)
            avail = bt - estimate_text_tokens(overhead_md)
            added_trim = False
            while avail >= 6:
                trimmed = truncate_to_tokens(safe_txt, avail)
                b_trim = f"- {pin_mark}{trimmed} *(来源: {prov})*"
                cand_trim = {k: [b_trim]}
                if estimate_text_tokens(_render_block(cand_trim, total_matched, 1)) <= bt:
                    selected_by_kind = cand_trim
                    by_kind_counts[k] = 1
                    included = 1
                    added_trim = True
                    break
                avail -= 4
            if not added_trim:
                dropped += 1
        else:
            dropped += 1

    block_md = _render_block(selected_by_kind, total_matched, included)
    tokens_est = estimate_text_tokens(block_md)

    old_text = ""
    exists = bool(out_file is not None and out_file.exists())
    if exists and out_file is not None:
        old_text = out_file.read_text(encoding="utf-8", errors="replace")

    if apply and total_matched == 0 and (not exists or not _RULES_SENTINEL_RE.search(old_text)):
        raise ValueError(f"项目 {proj!r} 暂无有效长期记忆，已取消写入 (dry-run 仍可预览)")

    if exists:
        if _RULES_SENTINEL_RE.search(old_text):
            new_text = _RULES_SENTINEL_RE.sub(lambda _m: block_md, old_text, count=1)
        else:
            sep = "" if not old_text or old_text.endswith("\n\n") else ("\n" if old_text.endswith("\n") else "\n\n")
            new_text = old_text + sep + block_md + "\n"
    else:
        new_text = block_md + "\n"

    changed = old_text != new_text
    diff_lines = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{out_file.name}" if out_file else "a/" + RULE_TARGETS[tgt],
        tofile=f"b/{out_file.name}" if out_file else "b/" + RULE_TARGETS[tgt],
        lineterm="",
        n=2,
    ))[:200]

    backup_path = None
    applied = False
    if apply and out_file is not None and changed:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        if exists:
            bak = out_file.with_name(out_file.name + ".bak-cv")
            atomic_write_text(bak, old_text)
            backup_path = str(bak)
        atomic_write_text(out_file, new_text)
        applied = True

    return {
        "project": proj,
        "project_label": proj_label,
        "target": tgt,
        "out_path": str(out_file) if out_file else None,
        "exists": exists,
        "applied": applied,
        "changed": changed,
        "backup_path": backup_path,
        "budget_tokens": bt,
        "tokens_est": tokens_est,
        "total_matched": total_matched,
        "included": included,
        "dropped": dropped,
        "by_kind": by_kind_counts,
        "block_markdown": block_md,
        "merged_markdown": new_text,
        "diff_lines": diff_lines,
    }
