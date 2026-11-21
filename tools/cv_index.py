"""Context Vault 检索引擎 v2 (零第三方依赖, Python 3.9+ stdlib)。

承载:
- tokenize: 代码感知分词 — 中英混合 + camelCase/PascalCase/snake_case/kebab-case/文件路径/
  错误码的「复合保留 + 子词拆分」双索引 (搜 VaultIndex 命中 vault_index, 反之亦然);
- 结构化块角色加权 BM25: 全局块级 IDF + 文档长度归一 (k1=1.2, b=0.75) × 块语义角色先验权重,
  消除 v0 词集交集中超长 tool_result 刷分偏差; tool 块内标识符精确命中保留加分;
- VaultIndex: SQLite 紧凑倒排持久层 (vault/search_index.db, 派生层可 100% 重建) + 增量追赶 +
  Lineage 折叠 (同一演进链多快照命中折叠到链叶子) + 时间衰减 + RRF 融合接口。
  存储选型裁决: 不用 FTS5 虚表做主索引 — CPython 的 sqlite3 不暴露 fts5 extension API,
  无法注册自定义分词器, 中文 bigram 与代码子词语义无法与冷路径对齐, 会破坏冷热等价铁律;
  故以紧凑二进制倒排 (postings blob) 为唯一规范存储, 任何 sqlite3 构建皆可用
  (fts5_available() 仅作能力探测记录)。
- ColdRanker: 无索引文件时的冷回退, 与热索引共用 rank_blocks/fold_chains/decay 数学,
  corpus 统计按同一枚举规则产出 (冷热等价是铁律)。
- format_snapshot_messages: 会话正文阅读器分页/过滤/安全截断格式化。

快照只读铁律: 本引擎对 snapshots/cv_*.json 永远只读; 全部派生数据住 search_index.db,
删除后可从快照 100% 幂等重建。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from pathlib import Path

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
    本批新见指纹同时记入内存集合 (同批链后继免查库即可判重); 落库统一延迟到
    _commit_staging 的同一事务 — 崩溃整批回滚, 账本与索引永不跨代分叉
    (旧实现逐文件 commit, 中途崩溃会留下指向未索引快照的孤儿账本)。"""
    __slots__ = ("conn", "lock", "rows", "_batch")

    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock
        self.rows = []
        self._batch = set()

    def dup(self, fp: int) -> bool:
        if fp in self._batch:
            return True
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM blockhash WHERE h=? LIMIT 1", (fp,)).fetchone() is not None

    def add(self, fp: int, ord_: int, doc: int):
        self._batch.add(fp)
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

def block_index_terms(text: str, bt: str, tokenizer=tokenize, code_aware: bool = True) -> list:
    """块索引词集 (热索引与冷回退共用同一实现 — 索引体积与冷热等价的关键闸口)。
    tokenizer/code_aware 必须从索引/排序器实例贯穿传入 (消融 V1 vanilla 才能真实生效):
    - 叙述块 (text/thinking/compaction/agent_message): 分词结果剔除 CJK 单字 (bigram
      承载中文召回; 单字 df 巨大 — 曾占总 postings 近半且贡献趋零);
    - tool 块 (tool_call/tool_result): 标识符锚点 + CJK bigram 头部截断 (报错摘要头)。
      code_aware=False (vanilla) 时 tool 块退化为纯词切分 (无 ident_set/复合词)。"""
    toks = tokenizer(text)
    cjk = [t for t in toks if _is_cjk(t[0])]
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
    return (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * d / max(avg_len, 1.0)))


def default_half_life() -> float:
    try:
        v = float(os.environ.get("CV_DECAY_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS))
        return v if v > 0 else DEFAULT_HALF_LIFE_DAYS
    except ValueError:
        return DEFAULT_HALF_LIFE_DAYS


def decay_factor(created_at: str, now: float | None, half_life: float) -> float:
    """时间衰减: 0.5 ** (age_days / half_life); 时间解析失败 = 1.0 (不惩罚)。"""
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
    age_days = max((now if now is not None else time.time()) - ts, 0.0) / 86400.0
    return 0.5 ** (age_days / half_life)


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


def fold_chains(scored: dict, all_sids: list, parents: dict, meta: dict) -> list:
    """Lineage 折叠: 演进链 (parent_snapshot 连通分量) 只出一命中 —
    链记分 = 链内最高分命中成员; 代表 = 链叶子 (沿 parent 正向走到无子, 多叉取最新版,
    环安全; 叶子无命中时仍作代表 — 折叠语义 = 指到该链最新快照); folded = 链全库成员数
    - 1 (去重后命中可能集中于首个出现的祖先版, 折叠数按链规模计)。返回 [(score, rep_sid, folded_n)],
    按 (score 降序, created_at 降序, sid 升序) 确定性排序。"""
    if not scored:
        return []
    all_set = set(all_sids)
    child: dict = {}
    for s, p in (parents or {}).items():
        if p and p in all_set and s in all_set:
            child.setdefault(p, []).append(s)
    memo: dict = {}
    root_of = lambda s: chain_root(s, parents, all_set, memo)  # noqa: E731
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
        leaf = leaf_of(best_sid)
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
                    meta_row, text_of, terms: list, now, half_life: float) -> list:
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
    if out and half_life and half_life > 0:  # 摘录链时间衰减 (与 search 同半衰期)
        for r in out:
            r["score"] = round(r["score"] * decay_factor(r["_ts"], now, half_life), SCORE_DP)
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

    def search(self, limit: int) -> dict:
        self._scan()
        empty = {"query": self.query, "terms": self.terms, "total": 0, "hits": []}
        if not self._ranked:
            return empty
        scored = snapshot_scores(self._ranked, lambda d: d[0], self._sid_of)
        triples = (fold_chains(scored, self._sids, self._parents, self._rows) if self.fold
                   else self._sort3([(sc, s, 0) for s, sc in scored.items()]))
        hits = []
        for sc, sid, folded in triples:
            row = dict(self._rows.get(sid) or {})
            if self.decay:
                sc = round(sc * decay_factor(row.get("created_at") or "", self.now,
                                             self.half_life), SCORE_DP)
            hits.append({"sid": sid, "score": sc, "folded": folded, **row})
        order = sort_hits([(h["score"], h["sid"], h.get("folded", 0)) for h in hits],
                          self._rows)
        pos = {t[1]: i for i, t in enumerate(order)}
        hits.sort(key=lambda h: pos[h["sid"]])
        return {"query": self.query, "terms": self.terms, "total": len(hits),
                "hits": hits[:limit]}

    def context(self, limit: int) -> dict:
        self._scan()
        empty = {"query": self.query, "terms": self.terms, "total": 0, "results": []}
        if not self._ranked:
            return empty
        results = context_results(self._ranked, limit, lambda d: d[0], self._sid_of,
                                  lambda d: d[1], self._root_of, self._rows.get,
                                  self._text_of, self.terms, self.now, self.half_life)
        for r in results:
            r["role"] = self._block_role(r["sid"], r["block"])
            if not r["versions"]:
                r["versions"] = self._chain_sizes.get(self._root_of(r["sid"]), 1)
        return {"query": self.query, "terms": self.terms, "total": len(self._ranked),
                "results": results}


def score_blocks(env: dict, terms: list, **kw) -> list:
    """单快照块级评分 (兼容签名)。注: 语料统计仅取该快照自身; 全库口径请用 ColdRanker。
    返回 [(score, bidx, text)] 按 (score 降序, bidx 升序)。"""
    r = ColdRanker([env], "", tokenizer=lambda _q: list(terms), fold=False, decay=False, **kw)
    r._scan()
    return [(sc, doc[1], r._text_of(doc)) for sc, doc in r._ranked]


# ================================================================ SQLite 持久层

INDEX_VER = 5  # 4→5: cfg 配置指纹 + tokenizer 贯穿 (旧索引一律重建)
INDEX_NAME = "search_index.db"
LEGACY_PKL = "index_cache.pkl"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sids(
  ord INTEGER PRIMARY KEY, sid TEXT UNIQUE, created_at TEXT, parent TEXT);
CREATE TABLE IF NOT EXISTS sid_rows(sid TEXT PRIMARY KEY, row_json BLOB);
CREATE TABLE IF NOT EXISTS sid_mtimes(sid TEXT PRIMARY KEY, mtime INTEGER);
CREATE TABLE IF NOT EXISTS terms(term TEXT PRIMARY KEY, df INTEGER, docs BLOB, tfs BLOB);
CREATE TABLE IF NOT EXISTS blockhash(h INTEGER, sid_ord INTEGER, doc INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value BLOB);
"""


def _lineage_parent(path: Path) -> str:
    """从快照文件尾部解析 lineage.parent_snapshot (envelope 尾键约定, 免解析数 MB 全文)。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            n = fh.tell()
            fh.seek(max(0, n - 4096))
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    i = tail.rfind('"lineage"')
    j = tail.find("{", i) if i >= 0 else -1
    if j < 0:
        return ""
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail, j)
    except ValueError:
        return ""
    p = obj.get("parent_snapshot") if isinstance(obj, dict) else None
    return str(p) if p else ""


def _z(bytes_: bytes) -> bytes:
    return zlib.compress(bytes_, 1)


def _unz(blob) -> bytes:
    return zlib.decompress(blob)


def fts5_available() -> bool:
    """运行时 FTS5 探测 (记录于索引 meta; 主存储不依赖它, 见模块 docstring 裁决)。"""
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        c.close()
        return True
    except (sqlite3.Error, AttributeError):
        return False


def _fp_from(mtimes: dict) -> str:
    """成功索引文件集合 {sid: mtime} → 指纹 (与 _scan 同一公式)。
    指纹语义 = 「已成功索引的文件集合」而非「盘上全部文件」: 单文件瞬时读失败
    不得计入, 否则此后任何 rebuild 都会因指纹相同而早退, 该文件从热索引消失
    (冷路径却能查到), 直到其 mtime 变化才自愈。"""
    return hashlib.sha256(
        "|".join(f"{sid}:{m}" for sid, m in sorted(mtimes.items())).encode()).hexdigest()


def _is_transient_db_error(exc: BaseException) -> bool:
    """瞬时错误 (他进程 BEGIN EXCLUSIVE 持锁 / busy / OOM): 库文件可能完全健康,
    绝不允许 purge/truncate; 仅结构性损坏 (DatabaseError malformed / not-a-database、
    blob 解码失败等) 才允许删库重建。"""
    return isinstance(exc, (sqlite3.OperationalError, MemoryError))


# ================================================================ 热索引

class VaultIndex:
    """SQLite 倒排热索引: postings 走磁盘 (不再有 800MB pickle / 常驻大对象),
    块元数据走紧凑数组 (全库 ~数 MB); 指纹失效自动重建, 纯新增走增量。
    并发模型 (ThreadingHTTPServer 多线程共用一实例, check_same_thread=False 设计内):
    读者可见态打成单引用 _state 元组整体换装, DB 连接同代打包; 写侧事务提交与换装
    收拢在同一把 _store_lock 内 → 读者永远拿到同一代完整状态 (无 IndexError/空窗),
    也不会踩到被并发关闭的连接。"""

    ENV_CACHE_MAX = 64

    def __init__(self, vault: Path, block_text_fn=block_text, tokenizer=tokenize,
                 weights=DEFAULT_WEIGHTS, ident_boost: float = IDENT_BOOST,
                 half_life: float | None = None, code_aware: bool = True,
                 config_id: str | None = None, readonly: bool = False):
        self.vault = Path(vault)
        self.block_text = block_text_fn
        self.tokenizer = tokenizer
        self.code_aware = code_aware
        self.weights = weights
        self.ident_boost = IDENT_BOOST if ident_boost is None else ident_boost
        self.half_life = default_half_life() if half_life is None else half_life
        self._readonly = bool(readonly)
        self._broken = False
        # 读者可见状态: 单引用元组 (布局见 _empty_state), 换装 = 整体一次赋值
        self._state = self._empty_state()
        self._store = None     # 当前代连接镜像 (mcp_server/_close 等外部探测兼容)
        # 配置指纹: 检索参数 (权重/加成/衰减/分词模式) 变化后旧索引一律判失效重建,
        # 防止消融/多配置场景误复用上一配置落盘的 postings (_load_db 严格比对)
        try:
            cfg_src = json.dumps({
                "w": weights if isinstance(weights, str) else (weights or DEFAULT_WEIGHTS),
                "ib": round(float(self.ident_boost), 3),
                "hl": round(float(self.half_life), 3),
                "ca": bool(code_aware), "cid": config_id or "",
            }, sort_keys=True, default=str)
            self.config_fp = hashlib.sha256(cfg_src.encode("utf-8")).hexdigest()[:16]
        except (TypeError, ValueError):
            self.config_fp = "custom"
        self.fingerprint = None
        self._mtimes = {}       # 写侧账本: 成功索引文件 → mtime (读者不读)
        self._next_doc = 0      # 写侧游标 (读者不读)
        self._store_lock = threading.RLock()  # 多线程 serve 共用一条 sqlite 连接 (写临界区内可嵌套)
        self._env_lock = threading.Lock()     # 快照 LRU 缓存专用锁 (读者并发)
        self._mem_only = False  # 当前代是否为 :memory: 兜底 (锁释放后需换回文件连接)
        self._init_locked = False  # 构造期 _load_db 命中外部锁: 本轮重建直接回退内存, 不再重复撞锁
        self._env_cache = OrderedDict()
        self._rebuild_lock = threading.Lock()
        self._rebuild_pending = False
        self._last_rebuild = 0.0
        self.stats = {"full": 0, "incr": 0, "cache_load": 0}
        if self._readonly:
            # 只读模式 (MCP 冷路径直查 search_index.db): 装载失败不重建/不落盘/不删文件
            if not self._load_db(readonly=True):
                self._broken = True
        elif self._load_db():
            self._last_rebuild = time.monotonic()
            self.rebuild()  # 追赶: 缓存之后新增走增量 (毫秒级), 删除/变更自动转全量
        else:
            self.rebuild(force=True)

    # ---------------- 读者可见状态 (单引用原子换装) ----------------
    # _state 布局: (meta, sids, parents, doc_sid, doc_bidx, doc_len, doc_w,
    #               total_len, term_count, store连接)。
    # 换装纪律: 写侧构建完整新代后对 _state 一次赋值; 已换出的旧代对象绝不原地修改
    # (增量并入 = 拼接出全新数组/字典, 旧代读者仍在引用)。连接同代打包: 读者只在
    # _store_lock 临界区内一次取 (整代状态, postings), 写侧关/换连接也在同一锁内,
    # 故任何时刻读者拿到的 meta/sids/数组/postings 同代自洽。

    @staticmethod
    def _empty_state() -> tuple:
        return ({}, [], {}, array(_I), array(_I), array(_H), array("d"), 0, 0, None)

    @property
    def meta(self) -> dict:
        return self._state[0]

    @property
    def sids(self) -> list:
        return self._state[1]

    def recent_pair(self) -> tuple:
        """meta+sids 同代一次读取 (锁内取整代引用)。/recent 等外部消费方若分开调
        .meta 与 .sids 两次属性读, 两次之间恰逢换装会拿到跨代错配对 (KeyError 500) —
        单个属性读自身原子, 但"成对自洽"必须一次取。"""
        with self._store_lock:
            st = self._state
            return st[0], st[1]

    @property
    def parents(self) -> dict:
        return self._state[2]

    @property
    def _doc_sid(self) -> array:
        return self._state[3]

    @property
    def _doc_bidx(self) -> array:
        return self._state[4]

    @property
    def _doc_len(self) -> array:
        return self._state[5]

    @property
    def _doc_w(self) -> array:
        return self._state[6]

    @property
    def term_count(self) -> int:
        return self._state[8]

    # ---------------- 快照全文按需加载 ----------------

    def load_env(self, sid):
        with self._env_lock:
            env = self._env_cache.pop(sid, None)
        if env is None:
            try:
                env = json.loads((self.vault / "snapshots" / f"{sid}.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            env["_sid"] = sid
        with self._env_lock:
            self._env_cache[sid] = env
            while len(self._env_cache) > self.ENV_CACHE_MAX:
                self._env_cache.popitem(last=False)
        return env

    def _scan(self):
        """(指纹, {stem: file}, {stem: mtime}) — 一次 glob 供指纹与差分共用。"""
        files = {f.stem: f for f in sorted((self.vault / "snapshots").glob("cv_*.json"))}
        mtimes = {}
        for sid, f in files.items():
            try:
                mtimes[sid] = int(f.stat().st_mtime)
            except OSError:
                continue
        return _fp_from(mtimes), files, mtimes

    # ---------------- 装载 / 持久化 ----------------

    def _db_path(self) -> Path:
        return self.vault / INDEX_NAME

    @staticmethod
    def _busy_timeout() -> float:
        """派生索引 SQLite 锁等待上限 (秒): 派生层有完备的旧代保留/内存兜底机制,
        短超时避免他进程持锁时构造或重建阻塞数十秒 (支持 CV_INDEX_LOCK_TIMEOUT 覆盖)。"""
        try:
            v = float(os.environ.get("CV_INDEX_LOCK_TIMEOUT", "1.5"))
            return max(0.1, min(v, 30.0))
        except ValueError:
            return 1.5

    def _open_conn(self):
        """新建库文件连接 (不读 _state; 须持 _store_lock)。"""
        new_db = not self._db_path().exists()
        conn = sqlite3.connect(self._db_path(), timeout=self._busy_timeout(), check_same_thread=False)
        if new_db:
            try:  # postings blob 偏大: 8K 页减少页开销 (仅新库首建生效)
                conn.execute("PRAGMA page_size=8192")
            except sqlite3.Error:
                pass
        try:
            conn.execute("PRAGMA synchronous=OFF")  # 派生索引: 损坏即重建, 不值 fsync
        except sqlite3.Error:
            pass
        return conn

    def _conn(self):
        """当前代共享连接 (须持 _store_lock; RLock 允许写侧临界区内嵌套调用)。
        连接属构造线程, 放开检查并用专用锁串行化; 连接随代打包进 _state,
        换装后旧代连接由 GC 回收 (或 _close 锁内显式关闭) — 读者不会踩到已关闭连接。"""
        conn = self._state[9]
        if conn is None:
            conn = self._open_conn()
            self._state = self._state[:9] + (conn,)
            self._store = conn
        return conn

    def _close(self):
        """关闭当前代共享连接 (mcp_server 只读路径查询后调用, 释放 Windows 句柄)。
        读者只在 _store_lock 临界区内使用连接, 本方法也在锁内换出并关闭 →
        不存在读者正在使用时被关闭的窗口 (历史 ProgrammingError 根因)。"""
        with self._store_lock:
            conn = self._state[9]
            self._state = self._state[:9] + (None,)
            self._store = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _arrays_blob(doc_sid, doc_bidx, doc_len, doc_w) -> bytes:
        return b"|S|".join((doc_sid.tobytes(), doc_bidx.tobytes(),
                            doc_len.tobytes(), doc_w.tobytes()))

    @staticmethod
    def _arrays_from_blob(blob: bytes):
        """blob → (doc_sid, doc_bidx, doc_len, doc_w); 校验失败返回 None。"""
        parts = blob.split(b"|S|")
        if len(parts) != 4:
            return None
        a_sid, a_bidx, a_len, a_w = array(_I), array(_I), array(_H), array("d")
        try:
            a_sid.frombytes(parts[0])
            a_bidx.frombytes(parts[1])
            a_len.frombytes(parts[2])
            a_w.frombytes(parts[3])
        except ValueError:
            return None
        n = len(a_sid)
        if not (n == len(a_bidx) == len(a_len) == len(a_w)):
            return None
        return a_sid, a_bidx, a_len, a_w

    def _reset_arrays(self):
        """读者可见态整体复位为空代 (构造失败回退用; 单引用换装, 不清读者正在用的对象)。"""
        self._state = self._empty_state()
        self._next_doc = 0

    def _purge_db_file(self):
        """删除派生索引文件 — 仅限 _load_db 判定的结构性损坏 (瞬时错误绝不走到这里);
        Windows 上他实例仍持句柄时 unlink 会失败 — SQLite 以共享写打开,
        截断清零同样可行 (0 字节 = 全新空库, schema 随后重建)。"""
        p = self._db_path()
        try:
            os.unlink(p)
            return
        except OSError:
            pass
        try:
            with open(p, "wb") as fh:
                fh.truncate(0)
        except OSError:
            pass

    def _load_db(self, readonly: bool = False) -> bool:
        p = self._db_path()
        if not p.exists():
            return False
        conn = None
        try:
            tmo = self._busy_timeout()
            conn = (sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=tmo,
                                    check_same_thread=False)
                    if readonly else
                    # 装载路径同样要放开跨线程 (serve 工作线程共用此连接; 与 _conn 一致)
                    sqlite3.connect(p, timeout=tmo, check_same_thread=False))
            row = conn.execute("SELECT value FROM meta WHERE key='hdr'").fetchone()
            if not row:
                conn.close()
                return False
            hdr = json.loads(bytes(row[0]).decode("utf-8"))
            if hdr.get("ver") != INDEX_VER or hdr.get("cfg") != self.config_fp:
                # 版本/检索配置 (权重/分词模式/衰减) 与落盘索引不一致: 判失效走重建,
                # 绝不复用上一配置的 postings (消融多配置同目录场景的防串档闸);
                # 此处不删文件, 重建时 _commit_staging 在同一事务内整体覆盖
                conn.close()
                return False
            blob = conn.execute("SELECT value FROM meta WHERE key='arrays'").fetchone()
            arrays = None if not blob else self._arrays_from_blob(_unz(bytes(blob[0])))
            if arrays is None:
                conn.close()
                return False
            meta_rows = {r[0]: json.loads(r[1]) for r in
                         conn.execute("SELECT sid, row_json FROM sid_rows")}
            sids = [r[0] for r in conn.execute("SELECT sid FROM sids ORDER BY ord")]
            parents = {r[0]: r[1] for r in
                       conn.execute("SELECT sid, parent FROM sids WHERE parent IS NOT NULL")}
            mtimes = {r[0]: r[1] for r in
                      conn.execute("SELECT sid, mtime FROM sid_mtimes")}
            if len(sids) != len(meta_rows) or len(mtimes) != len(sids):
                conn.close()
                return False
            term_count = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            # 全部校验通过: 整代单引用换装 (读者要么旧代要么完整新代, 无中间态)
            self._state = (meta_rows, sids, parents) + arrays + \
                          (sum(arrays[2]), term_count, conn)
            self._store = conn
            self._next_doc = len(arrays[0])
            self._mtimes = mtimes
            self.fingerprint = hdr.get("fp")
            self.stats["cache_load"] += 1
            return True
        except (sqlite3.Error, ValueError, KeyError, EOFError, UnicodeDecodeError,
                MemoryError, zlib.error) as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if not self._readonly:
                if _is_transient_db_error(exc):
                    self._init_locked = True
                else:
                    # 仅结构性损坏 (malformed / not-a-database / blob 失配) 才删派生文件;
                    # 瞬时错误 (他进程 BEGIN EXCLUSIVE 持锁 / busy / OOM) 下库文件可能完全
                    # 健康 — 绝不 purge/truncate, 放弃装载走内存重建, 指纹不落账下次追赶
                    self._purge_db_file()
            self._reset_arrays()
            return False

    # ---------------- 索引构建 ----------------

    def _fresh_staging(self, full: bool) -> dict:
        return {"full": full,
                "base_ord": 0 if full else len(self.sids),
                "base_doc": 0 if full else self._next_doc,
                "doc_sid": array(_I), "doc_bidx": array(_I), "doc_len": array(_H),
                "doc_w": array("d"), "terms": {},
                "sids": {}, "sid_list": [], "sid_rows": {}, "parents": {}, "mtimes": {},
                "next_doc": 0 if full else self._next_doc}

    def _index_one(self, f: Path, staging: dict, dedup=None) -> bool:
        """解析并索引单个快照 → staging。dedup 为全局块指纹去重后端 (_DedupSet/_DedupSql),
        演进链累积副本不入索引 (冷路径同规则 → 冷热等价)。失败返回 False 且不留残迹。"""
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
            capture, conv, stats = env["capture"], env["conversation"], env["stats"]
            if not isinstance(capture, dict) or not isinstance(conv, list) or not isinstance(stats, dict):
                return False
            meta_row = {"agent": capture.get("agent"),
                        "first": next(((b.get("content") or b.get("summary") or "")[:96]
                                       for b in conv if isinstance(b, dict)
                                       and b.get("role") == "user"
                                       and (b.get("content") or b.get("summary"))), ""),
                        "created_at": env.get("created_at"),
                        "cwd": capture.get("cwd"),
                        "blocks": stats["blocks_total"],
                        "tokens_in": stats["tokens_in"],
                        "completeness": capture.get("completeness")}
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            return False
        sid = f.stem
        parent = _lineage_parent(f)
        ord_ = staging["base_ord"] + len(staging["sid_list"])
        for i, b in enumerate(conv):
            if not isinstance(b, dict):
                continue
            try:
                text = self.block_text(b)
            except (AttributeError, TypeError, KeyError, ValueError):
                continue
            if not text:
                continue
            fp = block_fingerprint(text)
            if dedup is not None and dedup.dup(fp):
                continue  # 全局重复块: 演进链累积副本不入索引 (冷路径同规则)
            bt, role = block_role_of(b)
            doc = staging["next_doc"]
            staging["next_doc"] = doc + 1
            if dedup is not None:
                dedup.add(fp, ord_, doc)
            staging["doc_sid"].append(ord_)
            staging["doc_bidx"].append(i)
            staging["doc_len"].append(min(len(text), LEN_CAP))
            staging["doc_w"].append(block_weight(bt, role, self.weights))
            counts: dict = {}
            for t in block_index_terms(text, bt, self.tokenizer, self.code_aware):
                counts[t] = counts.get(t, 0) + 1
            for t, tf in counts.items():
                docs, tfs = staging["terms"].setdefault(t, (array(_I), array(_H)))
                docs.append(doc)
                tfs.append(min(tf, LEN_CAP))
        staging["sids"][sid] = {"ord": ord_,
                                "created_at": meta_row.get("created_at") or "",
                                "parent": parent}
        staging["sid_list"].append(sid)
        staging["sid_rows"][sid] = meta_row
        if parent:
            staging["parents"][sid] = parent
        try:
            staging["mtimes"][sid] = int(f.stat().st_mtime)
        except OSError:
            staging["mtimes"][sid] = 0
        return True

    def _drop_stop_terms(self, conn, n_docs: int):
        """构建期停用词项丢弃: df > STOP_DF_FRAC·N 的词项不入库 (纯存储优化 —
        查询期本就按同规则跳过, 分数与命中集不变; 冷路径同规则 → 冷热等价)。
        仅大语料启用; 须在写事务内调用 (与内容同事务提交)。"""
        if n_docs < STOP_DF_MIN_CORPUS:
            return
        conn.execute("DELETE FROM terms WHERE df > ?", (n_docs * STOP_DF_FRAC,))

    def _term_upsert(self, conn, term: str, docs: array, tfs: array):
        """增量合并: 新 doc 恒大于已有 doc (只追加不回填), 拼接即保持有序。
        tfs=None 表示全 1 tf; 合并后任一 tf>1 则物化完整 tfs 数组。"""
        row = conn.execute("SELECT docs, tfs FROM terms WHERE term=?", (term,)).fetchone()
        new_uniform = max(tfs, default=0) <= 1
        if row:
            od, ot = array(_I), array(_H)
            od.frombytes(_unz(row[0]))
            if row[1] is None:
                ot.extend([1] * len(od))  # 隐式全 1 显式化
            else:
                ot.frombytes(_unz(row[1]))
            merged_d = array(_I, od + docs)
            merged_t = array(_H, (ot + tfs) if not new_uniform
                             else array(_H, ot + array(_H, [1] * len(docs))))
            uniform = max(merged_t, default=0) <= 1
            conn.execute("UPDATE terms SET df=?, docs=?, tfs=? WHERE term=?",
                         (len(merged_d), _z(merged_d.tobytes()),
                          None if uniform else _z(merged_t.tobytes()), term))
        else:
            conn.execute("INSERT INTO terms(term,df,docs,tfs) VALUES(?,?,?,?)",
                         (term, len(docs), _z(docs.tobytes()),
                          None if new_uniform else _z(tfs.tobytes())))

    def _write_staging_txn(self, conn, staging: dict, arrays: tuple, hashes, fingerprint: str):
        """staging 一次性落库 (单写事务; 调用方须持 _store_lock):
        全量 = 清表重装; 增量 = 只写新 sid/blocks/terms + blockhash 追加。
        arrays 快照 blob + hdr 与内容同事务落盘 — 崩溃后 fp / 数组快照 / postings
        不会跨代错配 (旧实现 install 与 persist 分两段提交, 中间崩溃即错配)。"""
        conn.executescript(_SCHEMA)
        if staging["full"]:
            conn.execute("DELETE FROM sids")
            conn.execute("DELETE FROM sid_rows")
            conn.execute("DELETE FROM sid_mtimes")
            conn.execute("DELETE FROM terms")
            conn.execute("DELETE FROM blockhash")
            conn.executemany("INSERT OR REPLACE INTO terms(term,df,docs,tfs) VALUES(?,?,?,?)",
                             [(t, len(d[0]), _z(d[0].tobytes()),
                               None if max(d[1], default=0) <= 1 else _z(d[1].tobytes()))
                              for t, d in staging["terms"].items()])
        else:
            for t, (docs, tfs) in staging["terms"].items():
                self._term_upsert(conn, t, docs, tfs)
        conn.executemany("INSERT OR REPLACE INTO sids(ord,sid,created_at,parent) VALUES(?,?,?,?)",
                         [(v["ord"], s, v["created_at"], v["parent"] or None)
                          for s, v in staging["sids"].items()])
        conn.executemany("INSERT OR REPLACE INTO sid_rows(sid,row_json) VALUES(?,?)",
                         [(s, json.dumps(v, ensure_ascii=False).encode("utf-8"))
                          for s, v in staging["sid_rows"].items()])
        conn.executemany("INSERT OR REPLACE INTO sid_mtimes(sid,mtime) VALUES(?,?)",
                         list(staging["mtimes"].items()))
        if hashes:
            conn.executemany("INSERT INTO blockhash(h,sid_ord,doc) VALUES(?,?,?)", hashes)
        hdr = json.dumps({"ver": INDEX_VER, "fp": fingerprint, "cfg": self.config_fp,
                          "fts5": fts5_available()}).encode("utf-8")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('arrays',?)",
                     (sqlite3.Binary(_z(self._arrays_blob(*arrays))),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('hdr',?)",
                     (sqlite3.Binary(hdr),))
        self._drop_stop_terms(conn, staging["next_doc"])
        conn.commit()

    def _swap_to(self, arrays: tuple, meta_rows: dict, sids: list, parents: dict,
                 total_len: float, staging: dict, conn, fingerprint: str):
        """内存态单引用原子换装 (须持 _store_lock; 紧跟同一事务的 commit 之后)。"""
        term_count = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        self._state = (meta_rows, sids, parents) + tuple(arrays) + \
                      (total_len, term_count, conn)
        self._store = conn
        self._mtimes = dict(staging["mtimes"]) if staging["full"] \
            else {**self._mtimes, **staging["mtimes"]}
        self._next_doc = staging["next_doc"]
        self.fingerprint = fingerprint

    def _commit_staging(self, staging: dict, hashes, fingerprint: str):
        """写侧原子换装: DB 事务提交与内存换装收拢在同一 _store_lock 临界区 —
        读者在锁内一次取 (整代状态, postings), 因此要么看到完整旧代 (事务未提交),
        要么看到完整新代 (postings 与内存数组同批生效); 不存在历史上
        「DB 先提交、内存数组后扩」的 IndexError 窗口, 也不再清空共享态制造空命中窗。
        sqlite3.OperationalError (他进程持锁等) 原样上抛, 由调用方决定回退方式。"""
        with self._store_lock:
            if staging["full"]:
                arrays = (staging["doc_sid"], staging["doc_bidx"],
                          staging["doc_len"], staging["doc_w"])
                meta_rows, sids, parents = (staging["sid_rows"], staging["sid_list"],
                                            staging["parents"])
                total_len = sum(staging["doc_len"])
            else:
                st = self._state
                # 增量并入 = 拼接出全新数组/字典 (绝不原地 extend: 旧代读者仍在引用,
                # 原地扩容曾致读者读到重分配中的缓冲 → 段错误)
                arrays = (st[3] + staging["doc_sid"], st[4] + staging["doc_bidx"],
                          st[5] + staging["doc_len"], st[6] + staging["doc_w"])
                meta_rows = {**st[0], **staging["sid_rows"]}
                sids = st[1] + staging["sid_list"]
                parents = {**st[2], **staging["parents"]}
                total_len = st[7] + sum(staging["doc_len"])
            if self._mem_only:
                # 内存兜底代追赶落盘: 为库文件新建连接试写, 读者仍用内存代连接
                # (零窗口); 成功换装后旧内存连接随旧代由 GC 回收
                conn = self._open_conn()
            else:
                conn = self._conn()
            try:
                self._write_staging_txn(conn, staging, arrays, hashes, fingerprint)
            except sqlite3.OperationalError:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                if self._mem_only and conn is not self._state[9]:
                    try:  # 未被采纳的试写连接: 关闭句柄, 不留 Windows 文件锁
                        conn.close()
                    except sqlite3.Error:
                        pass
                raise  # 状态未换装、指纹未更新 → 旧完整代原样保留, 下次 rebuild 自动追赶
            if staging["full"]:
                self._vacuum(conn)
            self._swap_to(arrays, meta_rows, sids, parents, total_len, staging,
                          conn, fingerprint)
            self._mem_only = False  # 换装成功: 当前代已回到库文件连接

    def _vacuum(self, conn):
        """全量重建提交后 VACUUM 回收 blob 删除空间 (锁内执行 — 读者在临界区外排队,
        不会踩 busy; 派生库一次性压缩)。"""
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass

    def _fallback_memory_install(self, staging: dict, hashes, fingerprint: str):
        """库文件被外部进程锁死时的构造期兜底: 派生索引整代换装到独立 :memory: 连接
        (库文件一个字节都不动; 指纹不落账 → 锁释放后下次 rebuild 自动追赶落盘)。"""
        with self._store_lock:
            old_conn = self._state[9]
            arrays = (staging["doc_sid"], staging["doc_bidx"],
                      staging["doc_len"], staging["doc_w"])
            mem = sqlite3.connect(":memory:", timeout=10, check_same_thread=False)
            self._write_staging_txn(mem, staging, arrays, hashes, fingerprint)
            term_count = mem.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            self._state = ((staging["sid_rows"], staging["sid_list"], staging["parents"])
                           + arrays + (sum(staging["doc_len"]), term_count, mem))
            self._store = mem
            self._mtimes = dict(staging["mtimes"])
            self._next_doc = staging["next_doc"]
            self._mem_only = True
            # 指纹刻意不设: 下次 rebuild 必走全量, _commit_staging 将为库文件
            # 新建连接试写, 把派生数据追赶落盘到健康库文件
            if old_conn is not None:
                try:
                    old_conn.close()
                except sqlite3.Error:
                    pass

    def _rm_legacy_pkl(self):
        """v2 落库成功后移除 v0 的 ~800MB pickle 缓存 (纯派生数据, 已由本库取代)。"""
        try:
            (self.vault / LEGACY_PKL).unlink()
        except OSError:
            pass

    def rebuild(self, force=False, stale_after=0.0):
        if self._readonly:
            return  # 只读模式: 绝不重建/落盘
        now = time.monotonic()
        if not force and stale_after and now - self._last_rebuild < stale_after:
            return
        if not self._rebuild_lock.acquire(blocking=False):
            return
        try:
            fp, files, mtimes = self._scan()
            if not force and fp == self.fingerprint:
                return
            added_ids = {s for s in files if s not in self._mtimes}
            dirty = (set(self._mtimes) - files.keys()) or \
                    any(mtimes[s] != self._mtimes[s] for s in self._mtimes if s in mtimes)
            if not force and self.fingerprint is not None and not dirty and added_ids:
                self._rebuild_incremental(files, added_ids)
                return
            self._rebuild_full(files)
        finally:
            self._rebuild_lock.release()

    def _rebuild_incremental(self, files, added_ids):
        """增量追赶 (纯新增): 只解析新文件, 但索引/去重顺序必须与全量重建的全局链
        走序一致 — 用全体文件 (旧+新) 的链根序决定处理顺序 (旧文件 parent 取自库内
        账本, 新文件只读尾部 4KB, 不解析全文, 增量性能语义不变)。历史缺陷: 只按本批
        新增文件名排序 — 两条链同批各进一版含相同新块的快照时 (文件名序与根序相反),
        增量把「首见」记给一条链, 全量/冷按根序记给另一条, 三级命中分歧。
        失败文件不计入指纹 (指纹 = 成功索引文件集合的指纹) → 下次 rebuild 自动追赶;
        部分失败不影响本批其他文件与既有代 (部分失败不炸整体)。"""
        staging = self._fresh_staging(full=False)
        parents_all = dict(self._state[2])  # 旧文件 parent: 库内账本 (与全量同源同值)
        for sid in added_ids:
            parents_all[sid] = _lineage_parent(files[sid])
        conn = self._conn()
        dedup = _DedupSql(conn, self._store_lock)
        for sid in chain_walk_order(sorted(files), parents_all):
            if sid not in added_ids:
                continue
            self._index_one(files[sid], staging, dedup=dedup)
        ok_mtimes = dict(self._mtimes)
        ok_mtimes.update(staging["mtimes"])  # staging 只含成功索引的文件
        try:
            self._commit_staging(staging, dedup.rows, _fp_from(ok_mtimes))
        except sqlite3.OperationalError:
            return  # 他进程持锁: 旧完整代原样保留 (指纹未动), 下次 rebuild 自动重试
        self.stats["incr"] += 1
        self._last_rebuild = time.monotonic()
        self._rm_legacy_pkl()

    def _rebuild_full(self, files):
        """全量重建: 不再关共享连接/清共享态/删库文件 (历史空命中窗与并发段错误根因);
        全部内容在同一写事务内覆盖旧表, 提交后单引用原子换装。库文件本身只在
        _load_db 判定结构性损坏时才被删除, 此处对健康文件是纯内容覆盖。
        失败文件不计入指纹 (与增量同语义), 下次 rebuild 自动追赶。"""
        staging = self._fresh_staging(full=True)
        parents_all = {s: _lineage_parent(f) for s, f in files.items()}
        dedup = _DedupSet()
        for sid in chain_walk_order(sorted(files), parents_all):
            self._index_one(files[sid], staging, dedup=dedup)
        fp_ok = _fp_from(staging["mtimes"])
        hashes = dedup.rows
        if self._init_locked and not self._state[1]:
            # 构造期 _load_db 刚撞到外部独占锁: 直接落内存兜底, 避免紧接着再撞一次锁超时
            self._init_locked = False
            self._fallback_memory_install(staging, hashes, fp_ok)
        else:
            self._init_locked = False
            try:
                self._commit_staging(staging, hashes, fp_ok)
            except sqlite3.OperationalError:
                if self._state[1]:
                    return  # 运行期重建撞外部锁: 已有健康旧代, 保留 (指纹未动, 下次再试)
                # 构造期撞外部锁: 派生索引换装到 :memory: 兜底, 构造不失败、库文件不动
                self._fallback_memory_install(staging, hashes, fp_ok)
        self.stats["full"] += 1
        self._last_rebuild = time.monotonic()
        self._rm_legacy_pkl()

    def request_rebuild(self, stale_after=60.0):
        now = time.monotonic()
        if now - self._last_rebuild < stale_after:
            return
        self._last_rebuild = now
        if self._rebuild_pending:
            return
        self._rebuild_pending = True

        def _bg():
            try:
                self.rebuild()
            finally:
                self._rebuild_pending = False

        threading.Thread(target=_bg, daemon=True).start()

    # ---------------- 查询 ----------------

    def _postings_for(self, terms: list) -> dict:
        """{term: (docs 数组, tfs 数组或 None)} — tfs=None = 全 1 tf (省解码与 2B/posting)。
        term IN (...) 批量读取 (逐词 SELECT 曾是查询 P95 大头之一)。
        须持 _store_lock 调用 (读者请走 _lookup: 状态与连接同锁同代)。"""
        uniq = sorted(set(terms))
        if not uniq:
            return {}
        conn = self._state[9]
        if conn is None:
            return {}
        out: dict = {}
        for i in range(0, len(uniq), 200):
            chunk = uniq[i:i + 200]
            q = "SELECT term, docs, tfs FROM terms WHERE term IN (%s)" % ",".join("?" * len(chunk))
            for term, docs_b, tfs_b in conn.execute(q, chunk):
                docs, tfs = array(_I), array(_H)
                docs.frombytes(_unz(docs_b))
                if tfs_b is not None:
                    tfs.frombytes(_unz(tfs_b))
                else:
                    tfs = None
                out[term] = (docs, tfs)
        return out

    def _lookup(self, terms: list) -> tuple:
        """读者入口: 在同一 _store_lock 临界区内完成「整代状态快照 + postings 读取」—
        两者必然同代 (写侧的换装/连接更换也在同一锁内完成, 不会插入中间态)。"""
        with self._store_lock:
            st = self._state
            if st[9] is None:
                return st, {}
            return st, self._postings_for(terms)

    def _corpus_stats(self, st: tuple) -> Corpus:
        """语料统计出自同一代状态快照 (N / avg_len 与 postings 同源)。"""
        c = Corpus()
        c.n_docs = len(st[3])
        c.total_len = st[7]  # 换装时维护的缓存值, 查询路径零遍历
        return c

    def _text_of(self, doc: int, st: tuple | None = None) -> str:
        if st is None:
            st = self._state
        ord_ = st[3][doc]
        if ord_ >= len(st[1]):
            return ""
        env = self.load_env(st[1][ord_])
        if env is None:
            return ""
        try:
            return self.block_text(env["conversation"][st[4][doc]])
        except (IndexError, KeyError, TypeError):
            return ""

    def search(self, query: str, limit: int, decay: bool = True, fold: bool = True,
               now: float | None = None) -> dict:
        if getattr(self, "_broken", False):
            return {"query": query, "terms": [], "total": 0, "hits": []}
        terms = block_index_terms(query, "text", self.tokenizer, self.code_aware)
        empty = {"query": query, "terms": terms, "total": 0, "hits": []}
        if not terms:
            return empty
        st, postings = self._lookup(terms)
        if not postings:
            return empty
        meta, sids, parents, doc_sid, doc_bidx, doc_len, doc_w, total_len, _tc, _conn = st
        ranked = rank_blocks(postings, self._corpus_stats(st), lambda d: doc_sid[d],
                             doc_len.__getitem__, doc_w.__getitem__)
        if not ranked:
            return empty
        scored = snapshot_scores(ranked, lambda d: doc_sid[d], lambda o: sids[o])
        now = now if now is not None else time.time()
        triples = (fold_chains(scored, sids, parents, meta) if fold
                   else sort_hits([(sc, s, 0) for s, sc in scored.items()], meta))
        hits = []
        for sc, sid, folded in triples:
            row = dict(meta.get(sid) or {})
            if decay:
                sc = round(sc * decay_factor(row.get("created_at") or "", now, self.half_life),
                           SCORE_DP)
            hits.append({"sid": sid, "score": sc, "folded": folded, **row})
        order = sort_hits([(h["score"], h["sid"], h.get("folded", 0)) for h in hits], meta)
        pos = {t[1]: i for i, t in enumerate(order)}
        hits.sort(key=lambda h: pos[h["sid"]])
        return {"query": query, "terms": terms, "total": len(hits), "hits": hits[:limit]}

    def context(self, query: str, limit: int, decay: bool = True, fold: bool = True,
                now: float | None = None) -> dict:
        if getattr(self, "_broken", False):
            return {"query": query, "terms": [], "total": 0, "results": []}
        terms = block_index_terms(query, "text", self.tokenizer, self.code_aware)
        empty = {"query": query, "terms": terms, "total": 0, "results": []}
        if not terms:
            return empty
        st, postings = self._lookup(terms)
        if not postings:
            return empty
        meta, sids, parents, doc_sid, doc_bidx, doc_len, doc_w, total_len, _tc, _conn = st
        ranked = rank_blocks(postings, self._corpus_stats(st), lambda d: doc_sid[d],
                             doc_len.__getitem__, doc_w.__getitem__)
        if not ranked:
            return empty
        now = now if now is not None else time.time()
        all_set = set(sids)
        memo: dict = {}

        def root_of(s):
            return chain_root(s, parents, all_set, memo)

        def meta_row(s):
            return meta.get(s) or {}

        def block_role(sid, bidx):
            env = self.load_env(sid)
            if env is None:
                return "text"
            try:
                b = env["conversation"][bidx]
                return b.get("role") or b["type"]
            except (IndexError, KeyError, TypeError):
                return "text"

        results = context_results(ranked, limit, lambda d: doc_sid[d],
                                  lambda o: sids[o], doc_bidx.__getitem__,
                                  root_of, meta_row,
                                  lambda d: self._text_of(d, st), terms, now, self.half_life)
        for r in results:
            r["role"] = block_role(r["sid"], r["block"])
            if fold and not r["versions"]:
                root = root_of(r["sid"])
                r["versions"] = sum(1 for s in sids if root_of(s) == root)
        return {"query": query, "terms": terms, "total": len(ranked), "results": results}


# ================================================================ RRF 融合

def rrf_fuse(rankings: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion: 多路排序 → 融合分排序。rankings: [[key, ...], ...]。"""
    fused: dict = {}
    for ranking in rankings:
        for r, key in enumerate(ranking):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + r + 1)
    return sorted(fused.items(), key=lambda kv: (-kv[1], str(kv[0])))


# ================================================================ 阅读器格式化 (v0 原样)

def _block_to_reader_item(idx: int, b: dict, call_names: dict, max_chars: int) -> dict:
    bt = str(b.get("type") or "text")
    ts = str(b.get("ts") or "")
    role = str(b.get("role") or "")
    name = ""
    is_error = False
    if bt == "thinking":
        role = role or "assistant"
        raw = str(b.get("summary") or b.get("content") or "")
    elif bt == "compaction":
        role = "system"
        raw = str(b.get("summary") or "")
    elif bt == "tool_call":
        role = "assistant"
        name = str(b.get("name") or "call")
        args = b.get("arguments")
        if isinstance(args, (dict, list)):
            raw = json.dumps(args, ensure_ascii=False, indent=2)
        else:
            raw = str(args if args is not None else "")
    elif bt == "tool_result":
        role = "tool"
        cid = str(b.get("call_id") or "")
        name = call_names.get(cid, "")
        is_error = bool(b.get("is_error"))
        raw = str(b.get("content") or "")
    elif bt == "agent_message":
        role = str(b.get("author") or role or "agent")
        raw = str(b.get("content") or "")
    else:
        role = role or "assistant"
        raw = str(b.get("content") or "")
    full_chars = len(raw)
    truncated = full_chars > max_chars
    return {
        "idx": idx,
        "type": bt,
        "role": role,
        "name": name,
        "is_error": is_error,
        "ts": ts,
        "text": raw[:max_chars] if truncated else raw,
        "truncated": truncated,
        "full_chars": full_chars,
    }


def format_snapshot_messages(
    env: dict,
    scope: str = "live",
    offset: int = 0,
    limit: int = 40,
    query: str = "",
    max_chars: int = 16000,
) -> dict:
    """将快照会话格式化为分页阅读器列表 (支持 live/all 范围切换与块内关键词筛选)。"""
    blocks = [b for b in (env.get("conversation") or []) if isinstance(b, dict)]
    call_names: dict = {}
    for b in blocks:
        if b.get("type") == "tool_call" and b.get("call_id") and b.get("name"):
            call_names[str(b["call_id"])] = str(b["name"])

    last_comp = -1
    for i, b in enumerate(blocks):
        if b.get("type") == "compaction":
            last_comp = i

    total_all = len(blocks)
    has_comp_summary = last_comp >= 0 and bool((blocks[last_comp].get("summary") or "").strip())
    total_live = ((len(blocks) - last_comp - 1) + (1 if has_comp_summary else 0)
                  if last_comp >= 0 else total_all)

    if scope == "all":
        pairs = list(enumerate(blocks))
    else:
        pairs = []
        if last_comp >= 0:
            if has_comp_summary:
                pairs.append((-1, blocks[last_comp]))
            pairs.extend((i, blocks[i]) for i in range(last_comp + 1, len(blocks)))
        else:
            pairs = list(enumerate(blocks))

    q_low = (query or "").strip().lower()
    filtered_items = []
    for idx, b in pairs:
        it = _block_to_reader_item(idx, b, call_names, max_chars)
        if q_low:
            hay = f"{it['name']} {it['role']} {it['type']} {it['text']}".lower()
            if q_low not in hay:
                continue
        filtered_items.append(it)

    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or 40), 100))
    page = filtered_items[off:off + lim]
    return {
        "scope": "all" if scope == "all" else "live",
        "query": query or "",
        "total": len(filtered_items),
        "total_live": total_live,
        "total_all": total_all,
        "offset": off,
        "limit": lim,
        "has_more": (off + len(page)) < len(filtered_items),
        "items": page,
    }
