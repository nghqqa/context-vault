"""Context Vault 检索引擎无状态排序与分词原语 (零第三方依赖, Python 3.9+ stdlib)。

承载:
- tokenize / ident_set: 代码感知中英混合分词与标识符集合
- block_fingerprint / chain_walk_order / _DedupSet / _DedupSql: 链序遍历与块去重原语
- block_text / block_role_of / block_weight / block_index_terms / bm25_* / decay_factor: 评分原语
- chain_root / fold_chains / sort_hits / Corpus / rank_blocks / snapshot_scores / context_results
- ColdRanker / score_blocks: 无索引冷回退排序器
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from array import array

_I = "I" if array("I").itemsize >= 4 else "L"   # 无符号 4 字节 (doc refs / sid ord / bidx)
_H = "H"                                        # 无符号 2 字节 (blen / tf, 封顶)

# ================================================================ 代码感知分词

_CJK_LO, _CJK_HI = "\u4e00", "\u9fff"
_RUN_RE = re.compile(r"[A-Za-z0-9_]+(?:[.\-/\\][A-Za-z0-9_]+)*|[\u4e00-\u9fff]")
_SEG_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[0-9]+")

MAX_COMPOSITE_LEN = 40   # 复合词上限 (超长路径不产复合词, 防 postings 膨胀)
MAX_RUN_LEN = 64         # 超过此长度的 run 只产子词
MIN_IDENT_LEN = 3        # 标识符精确命中集合的最短子词


def _camel_parts(seg: str) -> list:
    """camelCase/PascalCase/数字边界切分: VaultIndex → vault,index; dataset1 → dataset,1。"""
    parts = _SEG_RE.findall(seg)
    return parts if parts else [seg]


def _is_cjk(ch: str) -> bool:
    return _CJK_LO <= ch <= _CJK_HI


def tokenize(text: str, code_aware: bool = True) -> list:
    """代码感知分词 (code_aware=False 退回纯词切分, 供消融 Variant 对照):
    - 英文词 + CJK 单字及 bigram (与 v0 兼容);
    - 标识符 run (snake/kebab/路径/点号/错误码) → 子词拆分 + 归一复合词双索引。"""
    words, cjk_chars, runs = [], [], []
    for m in _RUN_RE.finditer(text or ""):
        tok = m.group(0)
        if len(tok) == 1 and _is_cjk(tok):
            cjk_chars.append(tok)
            continue
        if cjk_chars:
            runs.append("".join(cjk_chars))
            cjk_chars = []
        runs.append(tok)
    if cjk_chars:
        runs.append("".join(cjk_chars))

    for run in runs:
        if _is_cjk(run[0]):
            words.extend(run)  # CJK 单字 (bigram 由相邻单字合成)
            continue
        low = run.lower()
        plain_segs = [s for s in re.split(r"[._\-/\\]+", run) if s]
        multi = len(plain_segs) > 1
        if code_aware:
            for seg in plain_segs:
                if len(seg) >= 2:
                    words.append(seg.lower())
                if any(c.isupper() for c in seg):
                    for p in _camel_parts(seg):
                        if len(p) >= 2:
                            words.append(p.lower())
            comp = re.sub(r"[^A-Za-z0-9]", "", low)
            if (multi and len(comp) >= 4 and len(comp) <= MAX_COMPOSITE_LEN
                    and len(run) <= MAX_RUN_LEN and not comp.isdigit()):
                words.append(comp)
            # 原始写法精确词项 (大小写/分隔符保留): 搜 VaultIndex 时精确原写文档比
            # 跨写法 (vault_index) 多命中一个词项 → 精确优先; 跨写法经复合词照常召回
            if len(run) <= MAX_RUN_LEN and len(run) >= 4 and (run != low or multi):
                words.append(run)
        elif len(low) >= 2:
            words.append(low)

    # CJK bigram: 相邻单字 (ASCII 词打断邻接, 与 v0 语义一致)
    cjk = [words[i] + words[i + 1] for i in range(len(words) - 1)
           if len(words[i]) == 1 and len(words[i + 1]) == 1]
    return [t for t in words if len(t) >= 2] + cjk


def ident_set(text: str) -> set:
    """块内标识符集合 (归一复合词 + ≥3 字符含字母子词): tool 块精确命中加分用,
    查询侧命中词与此集合精确比对。"""
    idents = set()
    for m in _RUN_RE.finditer(text or ""):
        tok = m.group(0)
        if _is_cjk(tok[0]):
            continue
        comp = re.sub(r"[^A-Za-z0-9]", "", tok.lower())
        if 4 <= len(comp) <= MAX_COMPOSITE_LEN and not comp.isdigit():
            idents.add(comp)
        for seg in re.split(r"[._\-/\\]+", tok):
            if len(seg) >= MIN_IDENT_LEN and any(c.isalpha() for c in seg):
                idents.add(seg.lower())
                if any(c.isupper() for c in seg):
                    for p in _camel_parts(seg):
                        if len(p) >= MIN_IDENT_LEN:
                            idents.add(p.lower())
    return idents


# ================================================================ 链序与块指纹 (冷热同构)

def block_fingerprint(text: str) -> int:
    """块内容指纹 (blake2b 63bit): 链内去重键 (63 位 = SQLite 有符号整数安全域)。
    热索引与冷回退共用同一实现。"""
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8", "ignore"),
                                          digest_size=8).digest(), "little") & 0x7FFFFFFFFFFFFFFF


def chain_walk_order(all_sids: list, parents: dict) -> list:
    """链内访问序 (root→叶; 多子防御按 sid 排序) — 去重语义 = 链内首个出现,
    结果只依赖 parent 图结构 (顺序无关), 热索引与冷回退产出完全一致的语料集。"""
    all_set = set(all_sids)
    child: dict = {}
    roots = []
    for sid in all_sids:
        p = parents.get(sid)
        if p and p in all_set:
            child.setdefault(p, []).append(sid)
        else:
            roots.append(sid)
    order = []
    for root in sorted(roots):
        cur, guard = root, 0
        while cur is not None and guard < 100000:
            order.append(cur)
            guard += 1
            kids = child.get(cur)
            cur = min(kids) if kids else None
    # 环/孤儿兜底: 未访问的 sid 追加在尾部 (保持确定性)
    if len(order) < len(all_sids):
        seen = set(order)
        order.extend(sid for sid in sorted(all_sids) if sid not in seen)
    return order


class _DedupSet:
    """内存指纹集 (全量重建 / 冷回退): 全局首个出现语义, 收集指纹行供 blockhash 表重建。"""
    __slots__ = ("seen", "rows")

    def __init__(self):
        self.seen = set()
        self.rows = []

    def dup(self, fp: int) -> bool:
        return fp in self.seen

    def add(self, fp: int, ord_: int, doc: int):
        self.seen.add(fp)
        self.rows.append((fp, ord_, doc))


class _DedupSql:
    """SQL 指纹查证 (增量): blockhash 表 = 当前已索引块的全局指纹账本。
    构造期一次性将已有指纹载入内存集合 (O(1) 判重, 避免无索引逐块全表扫 O(N*M) 卡死);
    落库统一延迟到 _commit_staging 的同一事务 — 崩溃整批回滚, 账本与索引永不跨代分叉。"""
    __slots__ = ("rows", "_seen")

    def __init__(self, conn, lock):
        self.rows = []
        with lock:
            try:
                self._seen = {r[0] for r in conn.execute("SELECT h FROM blockhash")}
            except sqlite3.Error:
                self._seen = set()

    def dup(self, fp: int) -> bool:
        return fp in self._seen

    def add(self, fp: int, ord_: int, doc: int):
        self._seen.add(fp)
        self.rows.append((fp, ord_, doc))

    def flush(self):
        pass  # 兼容保留 (无操作): 落库已并入 _commit_staging 的单事务


# ================================================================ 块文本 / 角色权重

def block_text(b: dict) -> str:
    """块的检索文本。热索引 (VaultIndex) 与冷回退 (ColdRanker/mcp_server) 共用同一实现,
    命中集必须一致, 不允许各自维护副本。"""
    if b["type"] in ("text", "agent_message"):
        return b.get("content", "") or ""
    if b["type"] == "thinking":
        return b.get("summary", "") or ""
    if b["type"] == "tool_call":
        return (b.get("name") or "") + " " + json.dumps(b.get("arguments", {}), ensure_ascii=False)[:200]
    if b["type"] == "tool_result":
        return (b.get("content", "") or "")[:500]
    return ""


def block_role_of(b: dict) -> tuple:
    """(规范化 btype, role) — 权重与展示共用。"""
    bt = str(b.get("type") or "text")
    role = str(b.get("role") or "")
    if bt == "thinking":
        role = role or "assistant"
    elif bt == "compaction":
        role = "system"
    elif bt == "tool_call":
        role = "assistant"
    elif bt == "tool_result":
        role = "tool"
    elif bt == "agent_message":
        role = str(b.get("author") or role or "agent")
    else:
        role = role or "assistant"
    return bt, role


# 块语义角色先验权重 (可配置, 参与消融): 压缩摘要/结论性对话高权。
# tool 块: 索引词集只含标识符锚点 + CJK 摘要头 (见 block_index_terms) — 能命中的都是
# 精确锚点, 权重即精确命中价值, 不再需要查询期 ident_boost (曾致按需 load_env 数 MB JSON)。
DEFAULT_WEIGHTS = {
    "compaction": 2.2,        # 压缩摘要 = 事后蒸馏的高密度结论
    "text:user": 1.4,         # 用户拍板/指令
    "text:assistant": 1.3,    # assistant 结论性输出
    "agent_message": 1.3,
    "thinking": 0.7,          # 推理过程, 中权
    "tool_call": 1.0,         # 参数 = 路径/函数名锚点
    "tool_result": 0.9,       # 回显只留锚点: 命中即精确证据
}
UNIFORM = "uniform"            # 消融开关: 权重全 1.0 (vanilla BM25)
IDENT_BOOST = 1.0              # 已废弃为 no-op (精确性移入 block_index_terms), 保留参数兼容
DEFAULT_HALF_LIFE_DAYS = 90.0  # 时间衰减半衰期 (CV_DECAY_HALF_LIFE_DAYS 可覆盖)
DEFAULT_DECAY_FLOOR = 0.65     # 有界衰减下界 (避免历史权威结论被衰减压死, CV_DECAY_FLOOR 可覆盖)


def _weight_key(bt: str, role: str) -> str:
    if bt == "text":
        return "text:user" if role == "user" else f"text:{role}"
    return bt


def block_weight(bt: str, role: str, weights=DEFAULT_WEIGHTS) -> float:
    if weights == UNIFORM:
        return 1.0
    w = DEFAULT_WEIGHTS if weights is None else weights
    return float(w.get(_weight_key(bt, role), w.get(bt, 1.0)))


# ================================================================ BM25 / 折叠 / 衰减 (冷热共用纯函数)

BM25_K1 = 1.2
BM25_B = 0.75
SCORE_DP = 6            # 聚合统一舍入: 消除冷热浮点求和顺序差, 并列确定性
LEN_CAP = 65000         # blen/tf 封顶 (H 数组), 冷热两侧同一常量
TOOL_CJK_CAP = 48       # tool 块 CJK bigram 截断数 (报错摘要头部, 防日志刷索引)
NARR_WORDS_CAP = 180    # 叙述块 ASCII 词上限 (长文只索头部; Lucene maxfieldlength 同思路)
NARR_CJK_CAP = 160      # 叙述块 CJK bigram 上限 (长中文文童只索头部)
STOP_DF_FRAC = 0.45     # df > 22%·N 的停用词项构建期丢弃+查询期跳过 (冷热同规则)
STOP_DF_MIN_CORPUS = 20000  # 仅大语料启用停用词跳过 (小语料高 df 是常态, 跳过会误杀)


_EN_STOPWORDS = frozenset(
    "the a an and or but if then else when while of to in on at by for with from "
    "as is are was were be been being am do does did doing have has had having i "
    "you he she it we they me him her them my your his its our their this that "
    "these those there here what which who whom whose how why not no so than too "
    "very can will just should now also about into over after before between out "
    "up down off again further once only own same s t d ll re ve m y".split())

# 极克制的中文纯疑问/礼貌虚词 bigram (仅在 code_aware 模式过滤, 防问句虚词被短噪声刷分)
_ZH_STOP_BIGRAMS = frozenset(
    "什么 怎么 如何 请问 一下 是否 哪些 为什 可以 这个 那个 我们 你们 他们 时候".split())

def block_index_terms(text: str, bt: str, tokenizer=tokenize, code_aware: bool = True) -> list:
    """块索引词集 (热索引与冷回退共用同一实现 — 索引体积与冷热等价的关键闸口)。
    tokenizer/code_aware 必须从索引/排序器实例贯穿传入 (消融 V1 vanilla 才能真实生效):
    - 叙述块 (text/thinking/compaction/agent_message): 分词结果剔除 CJK 单字 (bigram
      承载中文召回; 单字 df 巨大 — 曾占总 postings 近半且贡献趋零);
    - tool 块 (tool_call/tool_result): 标识符锚点 + CJK bigram 头部截断 (报错摘要头)。
      code_aware=False (vanilla) 时 tool 块退化为纯词切分 (无 ident_set/复合词)。"""
    toks = tokenizer(text)
    cjk = [t for t in toks if _is_cjk(t[0]) and (not code_aware or t not in _ZH_STOP_BIGRAMS)]
    if bt in ("tool_call", "tool_result"):
        if code_aware:
            return sorted(ident_set(text)) + cjk[:TOOL_CJK_CAP]
        return [t for t in toks if not _is_cjk(t[0])
                and t not in _EN_STOPWORDS][:TOOL_CJK_CAP * 2] + cjk[:TOOL_CJK_CAP]
    words = [t for t in toks if not _is_cjk(t[0]) and t not in _EN_STOPWORDS][:NARR_WORDS_CAP]
    return words + cjk[:NARR_CJK_CAP]


def bm25_idf(n_docs: int, df: int) -> float:
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def bm25_tf_norm(tf: int, blen: int, avg_len: float) -> float:
    d = max(float(min(blen, LEN_CAP)), 1.0)
    # 短块长度下界平滑: 避免极短草稿/巡检块凭借极小 d/avg_len 压制完整技术会话
    if avg_len > 12.0:
        d = max(d, 0.35 * avg_len)
    return (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * d / max(avg_len, 1.0)))


def default_half_life() -> float:
    try:
        v = float(os.environ.get("CV_DECAY_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS))
        return v if v > 0 else DEFAULT_HALF_LIFE_DAYS
    except ValueError:
        return DEFAULT_HALF_LIFE_DAYS


def default_decay_floor() -> float:
    try:
        raw = os.environ.get("CV_DECAY_FLOOR", "").strip()
        if not raw:
            return DEFAULT_DECAY_FLOOR
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return DEFAULT_DECAY_FLOOR


def query_decay_floor(query_terms: list, base_floor: float | None = None) -> float:
    """精确标识符/长代码词查询适当抬高时间衰减下界, 避免历史专有结论被时间压死."""
    fl = default_decay_floor() if base_floor is None else max(0.0, min(1.0, float(base_floor)))
    for t in (query_terms or ()):
        s = str(t or "")
        if not s:
            continue
        if any(ch in s for ch in ("_", "-", "/", "\\", ".")) or (len(s) >= 8 and s.isascii()) \
                or (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
            return max(fl, 0.80)
    return fl


def decay_factor(created_at: str, now: float | None, half_life: float,
                 floor: float | None = None) -> float:
    """有界时间衰减: floor + (1 - floor) * 0.5 ** (age_days / half_life); 解析失败或关闭 = 1.0."""
    if not created_at or not half_life or half_life <= 0:
        return 1.0
    ts = None
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            ts = time.mktime(time.strptime(created_at[:n], fmt))
            break
        except (ValueError, OverflowError):
            continue
    if ts is None:
        return 1.0
    fl = default_decay_floor() if floor is None else max(0.0, min(1.0, float(floor)))
    age_days = max((now if now is not None else time.time()) - ts, 0.0) / 86400.0
    return fl + (1.0 - fl) * (0.5 ** (age_days / half_life))


_REL_SINCE_RE = re.compile(r"^(\d+)([dh])$", re.IGNORECASE)
_YM_SINCE_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YMD_SINCE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_FULL_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|z|[+-]\d{2}:?\d{2})?$"
)
_AGENT_SCOPE_ALIASES = {
    "claude": frozenset({"claude", "claude-code", "claude-transcript"}),
    "dsh": frozenset({"dsh", "deepseek-harness"}),
    "antigravity": frozenset({"antigravity", "agy"}),
}


def _parse_iso_dt(val: str) -> dt.datetime | None:
    """严格解析 YYYY-MM / YYYY-MM-DD / ISO 时间戳 (兼容 Python 3.9+ 无冒号时区与任意小数位)."""
    s = str(val or "").strip()
    if not s:
        return None
    m = _YM_SINCE_RE.match(s)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return None
    m = _YMD_SINCE_RE.match(s)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _FULL_ISO_RE.match(s)
    if not m:
        return None
    y, mo, d, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    ss = int(m.group(6) or 0)
    frac = m.group(7) or ""
    us = int((frac + "000000")[:6]) if frac else 0
    tz_raw = m.group(8)
    tz = None
    if tz_raw:
        if tz_raw in ("Z", "z"):
            tz = dt.timezone.utc
        else:
            sign = 1 if tz_raw[0] == "+" else -1
            digits = tz_raw[1:].replace(":", "")
            th, tm = int(digits[:2]), int(digits[2:4])
            if th > 23 or tm > 59:
                return None
            tz = dt.timezone(sign * dt.timedelta(hours=th, minutes=tm))
    try:
        return dt.datetime(y, mo, d, hh, mm, ss, us, tzinfo=tz)
    except ValueError:
        return None


def normalize_since(since: str | None, now: float | None = None) -> str:
    """将时间下界规范化为 ISO 字符串 (支持 YYYY-MM / YYYY-MM-DD / ISO 时间戳 / 7d / 24h)。"""
    raw = str(since or "").strip()
    if not raw:
        return ""
    m = _REL_SINCE_RE.match(raw)
    if m:
        val, unit = int(m.group(1)), m.group(2).lower()
        if val <= 0:
            raise ValueError(f"invalid since: {since!r} (相对时间必须大于 0)")
        now_ts = time.time() if now is None else float(now)
        delta = dt.timedelta(days=val) if unit == "d" else dt.timedelta(hours=val)
        anchor = dt.datetime.fromtimestamp(now_ts).astimezone() - delta
        return anchor.isoformat(timespec="seconds")
    if _parse_iso_dt(raw) is not None:
        return raw[:10] + ("T" + raw[11:] if len(raw) > 10 and raw[10] == " " else raw[10:])
    raise ValueError(f"invalid since: {since!r} (支持 YYYY-MM-DD / ISO 时间戳 / 7d / 24h)")


def _ts_ge(ts_str: str, since_iso: str) -> bool:
    if not since_iso:
        return True
    ts = str(ts_str or "").strip()
    if not ts:
        return False
    if len(ts) > 10 and ts[10] == " ":
        ts = ts[:10] + "T" + ts[11:]
    d1, d2 = _parse_iso_dt(ts), _parse_iso_dt(since_iso)
    if d1 is not None and d2 is not None:
        if (d1.tzinfo is None) != (d2.tzinfo is None):
            local_tz = dt.datetime.now().astimezone().tzinfo
            if d1.tzinfo is None:
                d1 = d1.replace(tzinfo=local_tz)
            if d2.tzinfo is None:
                d2 = d2.replace(tzinfo=local_tz)
        return d1 >= d2
    return ts >= since_iso


def project_match_rank(stored: str | None, query: str | None) -> int:
    """按路径段边界匹配项目作用域 (3=全路径精确, 2=目录基名精确, 1=路径段边界前缀/后缀, 0=不匹配)."""
    nq = str(query or "").replace("\\", "/").rstrip("/").lower().strip()
    if not nq:
        return 3
    ns = str(stored or "").replace("\\", "/").rstrip("/").lower().strip()
    if not ns:
        return 0
    if ns == nq:
        return 3
    s_base = ns.rsplit("/", 1)[-1]
    q_base = nq.rsplit("/", 1)[-1]
    if "/" not in nq:
        return 2 if s_base == nq else 0
    if "/" not in ns and "/" in nq:
        return 2 if q_base == ns else 0
    if ns.endswith("/" + nq) or nq.endswith("/" + ns) or ns.startswith(nq + "/"):
        return 1
    return 0


def match_project_scope(stored: str | None, query: str | None) -> bool:
    """跨路径格式与段边界基名匹配项目作用域 (支持项目短名与完整路径双向命中, 避免宽泛子串误伤)."""
    return project_match_rank(stored, query) > 0


def match_agent_scope(row_agent: str | None, agent_filter: str | None) -> bool:
    """按客户端/agent 过滤 (支持逗号分隔与 claude/dsh/antigravity 标准别名)。"""
    raw = str(agent_filter or "").strip()
    if not raw:
        return True
    cur = str(row_agent or "").strip().lower()
    if not cur:
        return False
    accepted: set[str] = set()
    for part in raw.split(","):
        tok = part.strip().lower()
        if not tok:
            continue
        accepted.update(_AGENT_SCOPE_ALIASES.get(tok, (tok,)))
    return True if not accepted else (cur in accepted)


def match_snapshot_scope(
    row: dict | None,
    project: str | None = None,
    agent: str | None = None,
    since_iso: str = "",
) -> bool:
    """统一快照行作用域判断 (ColdRanker 与 VaultIndex 共用, 保障冷热等价)。"""
    r = row or {}
    if project and not match_project_scope(r.get("cwd"), project):
        return False
    if agent and not match_agent_scope(r.get("agent"), agent):
        return False
    if since_iso and not _ts_ge(str(r.get("created_at") or ""), since_iso):
        return False
    return True


def sort_hits(pairs: list, meta: dict) -> list:
    """(score, sid, folded) → score 降序, created_at 降序, sid 升序 (v0 同款三级稳定排序)。"""
    pairs.sort(key=lambda t: t[1])
    pairs.sort(key=lambda t: (meta.get(t[1]) or {}).get("created_at") or "", reverse=True)
    pairs.sort(key=lambda t: -t[0])
    return pairs


def chain_root(sid: str, parents: dict, all_sids: set, memo: dict) -> str:
    """sid → 演进链根 (context 折叠去重键; parent 缺库/环安全, 确定性)。"""
    if sid in memo:
        return memo[sid]
    chain, cur = [], sid
    while cur not in memo:
        chain.append(cur)
        memo[cur] = cur  # 先占位防环
        p = parents.get(cur)
        if not p or p not in all_sids or p in chain:
            break
        cur = p
    root = memo[cur]
    for s in chain:
        memo[s] = root
    return root


def chain_leaf_map(all_sids: list, parents: dict, meta: dict) -> dict[str, str]:
    """将全库每个 sid 映射到所属演进链的代表叶子 sid (环安全, 多叉取最新版)."""
    all_set = set(all_sids)
    child: dict = {}
    for s, p in (parents or {}).items():
        if p and p in all_set and s in all_set:
            child.setdefault(p, []).append(s)
    leaf_cache: dict = {}

    def leaf_of(s: str) -> str:
        if s in leaf_cache:
            return leaf_cache[s]
        cur, visited = s, set()
        while cur not in leaf_cache:
            visited.add(cur)
            leaf_cache[cur] = cur  # 占位防环
            kids = child.get(cur)
            if not kids:
                break
            nxt = max(kids, key=lambda x: ((meta.get(x) or {}).get("created_at") or "", x))
            if nxt in visited:
                break
            cur = nxt
        leaf = leaf_cache[cur]
        for v in visited:
            leaf_cache[v] = leaf
        return leaf

    return {s: leaf_of(s) for s in all_sids}


def fold_chains(scored: dict, all_sids: list, parents: dict, meta: dict) -> list:
    """Lineage 折叠: 演进链 (parent_snapshot 连通分量) 只出一命中 —
    链记分 = 链内最高分命中成员; 代表 = 链叶子 (沿 parent 正向走到无子, 多叉取最新版,
    环安全; 叶子无命中时仍作代表 — 折叠语义 = 指到该链最新快照); folded = 链全库成员数
    - 1 (去重后命中可能集中于首个出现的祖先版, 折叠数按链规模计)。返回 [(score, rep_sid, folded_n)],
    按 (score 降序, created_at 降序, sid 升序) 确定性排序。"""
    if not scored:
        return []
    all_set = set(all_sids)
    memo: dict = {}
    root_of = lambda s: chain_root(s, parents, all_set, memo)  # noqa: E731
    leaf_map = chain_leaf_map(all_sids, parents, meta)
    chains: dict = {}
    for sid, sc in scored.items():
        chains.setdefault(root_of(sid), []).append((sc, sid))
    sizes: dict = {}
    for s in all_sids:
        r = root_of(s)
        sizes[r] = sizes.get(r, 0) + 1
    out = []
    for root, members in chains.items():
        members.sort(key=lambda t: (-t[0], t[1]))
        best_sc, best_sid = members[0]
        leaf = leaf_map.get(best_sid, best_sid)
        out.append((round(best_sc, SCORE_DP), leaf, max(sizes.get(root, 1) - 1, 0)))
    return sort_hits(out, meta)


# ================================================================ 共享排序管线 (冷热同一实现)

class Corpus:
    """语料统计 (N / avg_len) — 热索引与冷回退各自按同一枚举规则产出, 数值必须一致。"""
    __slots__ = ("n_docs", "total_len")

    def __init__(self):
        self.n_docs = 0
        self.total_len = 0

    @property
    def avg_len(self) -> float:
        return (self.total_len / self.n_docs) if self.n_docs else 1.0


def rank_blocks(postings: dict, corpus: Corpus, doc_sid, doc_len, doc_w) -> list:
    """块级 BM25 评分 (冷热共用同一实现)。postings: {term: (docs_seq, tfs_seq|None)}
    — tfs=None 表示全 1 tf 词项 (存储省 2B/posting); docs_seq 支持数组/字典 items 等
    有 len 的序列。doc_*: doc → 元数据访问器。返回 [(score, doc)] 按 (score 降序,
    doc 升序) — doc 升序保证并列确定性。df > STOP_DF_FRAC·N 的停用词项整体跳过
    (构建期已同规则丢弃, 此处兜底; 冷热同规则 → 命中集一致)。"""
    if not postings or corpus.n_docs == 0:
        return []
    # 小语料 (含全部单测) 不启用停用词跳过: df_cap=None 视为不设上限
    df_cap = (corpus.n_docs * STOP_DF_FRAC) if corpus.n_docs >= STOP_DF_MIN_CORPUS else None
    scores = {}
    for term, (docs, tfs) in postings.items():
        df = len(docs)
        if df_cap is not None and df > df_cap:
            continue  # 停用词项: 冷热两侧同跳, 命中集一致
        w_idf = bm25_idf(corpus.n_docs, df)
        if tfs is None:
            for doc in docs:
                add = w_idf * bm25_tf_norm(1, doc_len(doc), corpus.avg_len) * doc_w(doc)
                scores[doc] = scores.get(doc, 0.0) + add
        else:
            for doc, tf in zip(docs, tfs):
                add = w_idf * bm25_tf_norm(tf, doc_len(doc), corpus.avg_len) * doc_w(doc)
                scores[doc] = scores.get(doc, 0.0) + add
    if not scores:
        return []
    ranked = sorted(((round(sc, SCORE_DP), doc) for doc, sc in scores.items()),
                    key=lambda kv: (-kv[0], kv[1]))
    return ranked


SOFT_TAIL = 0.1  # 快照聚合饱和系数: 多证据只带来对数级增益


def snapshot_scores(ranked: list, doc_sid, sid_of) -> dict:
    """块分 → 快照聚合 (防刷分饱和): score = best × (1 + SOFT_TAIL·ln(命中块数))。
    v0 求和聚合下 200 个弱 tool_result 命中可碾压 1 个关键决策块 (线性刷分);
    饱和聚合下单点高分 (决策/摘要块) 稳定胜出, 多块证据仍获得温和加成。
    doc_sid(doc) → sid 序号; sid_of(ord) → sid。统一舍入保证冷热并列确定性。"""
    per: dict = {}
    for sc, doc in ranked:
        per.setdefault(sid_of(doc_sid(doc)), []).append(sc)
    out = {}
    for sid, lst in per.items():
        best = max(lst)
        if len(lst) > 1:
            best *= 1.0 + SOFT_TAIL * math.log(len(lst))
        out[sid] = round(best, SCORE_DP)
    return out


def context_results(ranked: list, limit: int, doc_sid, sid_of, doc_bidx, root_of,
                    meta_row, text_of, terms: list, now, half_life: float,
                    decay: bool = True, decay_floor: float | None = None) -> list:
    """块级命中 → 每演进链一条摘录。契约字段: sid/block/role/score/excerpt/cwd (+versions)。"""
    out, seen_roots = [], set()
    first = terms[0].lower() if terms else ""
    for sc, doc in ranked:
        if len(out) >= limit:
            break
        sid = sid_of(doc_sid(doc))
        root = root_of(sid)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        text = text_of(doc) or ""
        pos = text.lower().find(first) if first else 0
        start = max(0, pos - 200)
        out.append({"sid": sid, "block": doc_bidx(doc), "role": "", "score": sc,
                    "excerpt": text[start:start + 400],
                    "versions": 0, "cwd": meta_row(sid).get("cwd"),
                    "_ts": meta_row(sid).get("created_at") or ""})
    if out and decay and half_life and half_life > 0:  # 摘录链时间衰减 (与 search 同半衰期与下界)
        for r in out:
            r["score"] = round(
                r["score"] * decay_factor(r["_ts"], now, half_life, floor=decay_floor),
                SCORE_DP,
            )
        out.sort(key=lambda r: (-r["score"], r["sid"], r["block"]))
    for r in out:
        r.pop("_ts", None)
    return out


# ================================================================ 冷回退排序器

class ColdRanker:
    """无索引文件的冷回退 (MCP/CLI): 全量扫描快照, 与热索引共用 rank_blocks/fold_chains/
    decay 数学。corpus 统计与热索引同规则枚举 (同一 block_text), N/df/avg_len 一致 →
    冷热命中集一致。doc = (env_i, bidx) — 与热索引的整数 doc 句柄同构。"""

    def __init__(self, envs: list, query: str, block_text_fn=block_text, tokenizer=tokenize,
                 weights=DEFAULT_WEIGHTS, ident_boost: float = IDENT_BOOST,
                 half_life: float | None = None, now: float | None = None,
                 fold: bool = True, decay: bool = True, code_aware: bool = True):
        self.envs = envs
        self.query = query
        self.block_text = block_text_fn
        self.tokenizer = tokenizer
        self.code_aware = code_aware
        self.weights = weights
        self.ident_boost = ident_boost  # 已废弃为 no-op, 保留参数兼容
        self.half_life = default_half_life() if half_life is None else half_life
        self.now = now if now is not None else time.time()
        self.fold = fold
        self.decay = decay
        self.terms = block_index_terms(query, "text", tokenizer, code_aware)
        self._done = False

    def _scan(self):
        if self._done:
            return
        corpus = Corpus()
        postings: dict = {}
        qset = set(self.terms)
        doc_len, doc_w = {}, {}
        sids, rows, parents = [], {}, {}
        env_i_of: dict = {}
        for env_i, env in enumerate(self.envs):
            capture = env.get("capture") or {}
            sid = env.get("_sid") or f"cv_{env_i:08d}"
            sids.append(sid)
            env_i_of[sid] = env_i
            rows[sid] = {"agent": capture.get("agent"),
                         "first": next(((b.get("content") or b.get("summary") or "")[:96]
                                        for b in env.get("conversation") or []
                                        if isinstance(b, dict) and b.get("role") == "user"
                                        and (b.get("content") or b.get("summary"))), ""),
                         "created_at": env.get("created_at"),
                         "cwd": capture.get("cwd"),
                         "blocks": (env.get("stats") or {}).get("blocks_total", 0),
                         "tokens_in": (env.get("stats") or {}).get("tokens_in", 0),
                         "completeness": capture.get("completeness")}
            lin = env.get("lineage") or {}
            if lin.get("parent_snapshot"):
                parents[sid] = lin["parent_snapshot"]
        # 链序 + 全局首见去重 (与热索引同一 chain_walk_order / block_fingerprint → 语料集一致)
        dedup = _DedupSet()
        for sid in chain_walk_order(sids, parents):
            env = self.envs[env_i_of[sid]]
            env_i = env_i_of[sid]
            for bidx, b in enumerate(env.get("conversation") or []):
                if not isinstance(b, dict):
                    continue
                try:
                    text = self.block_text(b)
                except (AttributeError, TypeError, KeyError, ValueError):
                    continue
                if not text:
                    continue
                fp = block_fingerprint(text)
                if dedup.dup(fp):
                    continue  # 演进链累积副本: 与热索引同规则跳过 (corpus 统计同步排除)
                bt, role = block_role_of(b)
                doc = (env_i, bidx)
                blen = min(len(text), LEN_CAP)
                corpus.n_docs += 1
                corpus.total_len += blen
                doc_len[doc] = blen
                doc_w[doc] = block_weight(bt, role, self.weights)
                if qset:
                    counts: dict = {}
                    for t in block_index_terms(text, bt, self.tokenizer, self.code_aware):
                        counts[t] = counts.get(t, 0) + 1
                    for t in qset:
                        if t in counts:
                            postings.setdefault(t, ([], []))
                            postings[t][0].append(doc)
                            postings[t][1].append(min(counts[t], LEN_CAP))
                dedup.add(fp, 0, 0)
        self._corpus = corpus
        self._postings = postings
        self._doc_len = doc_len
        self._doc_w = doc_w
        self._rows = rows
        self._sids = sids
        self._parents = parents
        all_set = set(sids)
        memo: dict = {}
        self._root_of = lambda s: chain_root(s, parents, all_set, memo)  # noqa: E731
        chain_sizes: dict = {}
        for s in sids:
            r = self._root_of(s)
            chain_sizes[r] = chain_sizes.get(r, 0) + 1
        self._chain_sizes = chain_sizes
        self._ranked = rank_blocks(postings, corpus, lambda d: d[0], doc_len.get, doc_w.get)
        self._done = True

    def _text_of(self, doc) -> str:
        try:
            b = self.envs[doc[0]]["conversation"][doc[1]]
            return self.block_text(b)
        except (IndexError, KeyError, AttributeError, TypeError):
            return ""

    def _sid_of(self, ord_: int) -> str:
        return self._sids[ord_]

    def _block_role(self, sid: str, bidx: int) -> str:
        for i, s in enumerate(self._sids):
            if s == sid:
                try:
                    b = self.envs[i]["conversation"][bidx]
                    return b.get("role") or b["type"]
                except (IndexError, KeyError, TypeError):
                    return "text"
        return "text"

    def _sort3(self, triples: list) -> list:
        triples.sort(key=lambda t: t[1])
        triples.sort(key=lambda t: (self._rows.get(t[1]) or {}).get("created_at") or "",
                     reverse=True)
        triples.sort(key=lambda t: -t[0])
        return triples

    def search(self, limit: int, project: str | None = None, agent: str | None = None,
               since: str | None = None, decay: bool | None = None) -> dict:
        since_iso = normalize_since(since, self.now)
        use_decay = self.decay if decay is None else bool(decay)
        self._scan()
        empty = {"query": self.query, "terms": self.terms, "total": 0, "hits": []}
        if not self._ranked:
            return empty
        scored = snapshot_scores(self._ranked, lambda d: d[0], self._sid_of)
        if self.fold:
            triples = fold_chains(scored, self._sids, self._parents, self._rows)
            if project or agent or since_iso:
                triples = [
                    (sc, sid, folded) for sc, sid, folded in triples
                    if match_snapshot_scope(self._rows.get(sid), project, agent, since_iso)
                ]
                if not triples:
                    return empty
        else:
            if project or agent or since_iso:
                scored = {s: sc for s, sc in scored.items()
                          if match_snapshot_scope(self._rows.get(s), project, agent, since_iso)}
                if not scored:
                    return empty
            triples = self._sort3([(sc, s, 0) for s, sc in scored.items()])
        d_floor = query_decay_floor(self.terms)
        hits = []
        for sc, sid, folded in triples:
            row = dict(self._rows.get(sid) or {})
            if use_decay:
                sc = round(sc * decay_factor(row.get("created_at") or "", self.now,
                                             self.half_life, floor=d_floor), SCORE_DP)
            hits.append({"sid": sid, "score": sc, "folded": folded, **row})
        order = sort_hits([(h["score"], h["sid"], h.get("folded", 0)) for h in hits],
                          self._rows)
        pos = {t[1]: i for i, t in enumerate(order)}
        hits.sort(key=lambda h: pos[h["sid"]])
        return {"query": self.query, "terms": self.terms, "total": len(hits),
                "hits": hits[:limit]}

    def context(self, limit: int, project: str | None = None, agent: str | None = None,
                since: str | None = None, decay: bool | None = None) -> dict:
        since_iso = normalize_since(since, self.now)
        use_decay = self.decay if decay is None else bool(decay)
        self._scan()
        empty = {"query": self.query, "terms": self.terms, "total": 0, "results": []}
        if not self._ranked:
            return empty
        ranked = self._ranked
        if project or agent or since_iso:
            leaf_map = chain_leaf_map(self._sids, self._parents, self._rows) if self.fold else {}
            ranked = [
                (sc, doc) for sc, doc in ranked
                if match_snapshot_scope(
                    self._rows.get(
                        leaf_map.get(self._sid_of(doc[0]), self._sid_of(doc[0]))
                        if self.fold else self._sid_of(doc[0])
                    ),
                    project, agent, since_iso,
                )
            ]
            if not ranked:
                return empty
        d_floor = query_decay_floor(self.terms)
        results = context_results(ranked, limit, lambda d: d[0], self._sid_of,
                                  lambda d: d[1], self._root_of, self._rows.get,
                                  self._text_of, self.terms, self.now,
                                  self.half_life if use_decay else 0.0,
                                  decay=use_decay, decay_floor=d_floor)
        for r in results:
            r["role"] = self._block_role(r["sid"], r["block"])
            if not r["versions"]:
                r["versions"] = self._chain_sizes.get(self._root_of(r["sid"]), 1)
        return {"query": self.query, "terms": self.terms, "total": len(ranked),
                "results": results}


def score_blocks(env: dict, terms: list, **kw) -> list:
    """单快照块级评分 (兼容签名)。注: 语料统计仅取该快照自身; 全库口径请用 ColdRanker。
    返回 [(score, bidx, text)] 按 (score 降序, bidx 升序)。"""
    r = ColdRanker([env], "", tokenizer=lambda _q: list(terms), fold=False, decay=False, **kw)
    r._scan()
    return [(sc, doc[1], r._text_of(doc)) for sc, doc in r._ranked]
