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
import os
import sqlite3
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from pathlib import Path

from cv_rank import (  # noqa: F401
    _I,
    _H,
    _CJK_LO,
    _CJK_HI,
    _RUN_RE,
    _SEG_RE,
    MAX_COMPOSITE_LEN,
    MAX_RUN_LEN,
    MIN_IDENT_LEN,
    _camel_parts,
    _is_cjk,
    tokenize,
    ident_set,
    block_fingerprint,
    chain_walk_order,
    _DedupSet,
    _DedupSql,
    block_text,
    block_role_of,
    DEFAULT_WEIGHTS,
    UNIFORM,
    IDENT_BOOST,
    DEFAULT_HALF_LIFE_DAYS,
    _weight_key,
    block_weight,
    BM25_K1,
    BM25_B,
    SCORE_DP,
    LEN_CAP,
    TOOL_CJK_CAP,
    NARR_WORDS_CAP,
    NARR_CJK_CAP,
    STOP_DF_FRAC,
    STOP_DF_MIN_CORPUS,
    _EN_STOPWORDS,
    _ZH_STOP_BIGRAMS,
    block_index_terms,
    bm25_idf,
    bm25_tf_norm,
    default_half_life,
    decay_factor,
    sort_hits,
    chain_root,
    fold_chains,
    Corpus,
    rank_blocks,
    SOFT_TAIL,
    snapshot_scores,
    context_results,
    ColdRanker,
    score_blocks,
)

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
CREATE INDEX IF NOT EXISTS idx_blockhash_h ON blockhash(h);
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
                "ca": bool(code_aware), "cid": config_id or "", "sv": 2,
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
