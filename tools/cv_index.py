"""Context Vault 检索与内存倒排索引引擎 (零第三方依赖, Python 3.9+ stdlib)。

承载:
- tokenize / block_text / score_blocks: 热索引与 MCP 冷回退共用的分词与评分规范实现;
- VaultIndex: 位编码内存倒排索引 + 增量追赶 + pickle 原子持久化 + LRU 快照缓存;
- format_snapshot_messages: 会话正文阅读器分页/过滤/安全截断格式化。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from array import array
from collections import Counter, OrderedDict
from pathlib import Path


def tokenize(text: str) -> list:
    """英文词 + CJK 单字及 bigram (中英混合检索够用)."""
    out, cur = [], []
    for ch in text.lower():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
            if "\u4e00" <= ch <= "\u9fff":
                out.append(ch)
    if cur:
        out.append("".join(cur))
    words = [t for t in out if len(t) >= 2]
    cjk = [out[i] + out[i + 1] for i in range(len(out) - 1)
           if len(out[i]) == 1 and len(out[i + 1]) == 1]
    return words + cjk


def block_text(b: dict) -> str:
    """块的检索文本。热索引 (VaultIndex) 与冷回退 (mcp_server) 共用同一实现 —
    两侧命中集必须一致, 不允许各自维护副本 (2026-11-02 审查 #7)。"""
    if b["type"] in ("text", "agent_message"):
        return b.get("content", "") or ""
    if b["type"] == "thinking":
        return b.get("summary", "") or ""
    if b["type"] == "tool_call":
        return (b.get("name") or "") + " " + json.dumps(b.get("arguments", {}), ensure_ascii=False)[:200]
    if b["type"] == "tool_result":
        return (b.get("content", "") or "")[:500]
    return ""


def score_blocks(env: dict, terms: list) -> list:
    """块级检索评分, 与热索引同语义: 词项 ∈ set(tokenize(块文本)) 计 1 分/词项/块
    (索引侧 postings 即按 set(tokenize) 落键)。冷回退必须经此函数, 不得自写匹配。"""
    tset = set(terms)
    if not tset:
        return []
    hits = []
    for i, b in enumerate(env["conversation"]):
        if not isinstance(b, dict):
            continue
        try:
            text = block_text(b)
        except (AttributeError, TypeError, KeyError, ValueError):
            continue
        if not text:
            continue
        n = len(tset & set(tokenize(text)))
        if n:
            hits.append((n, i, text))
    return hits


def _block_to_reader_item(idx: int, b: dict, call_names: dict[str, str], max_chars: int) -> dict:
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
    call_names: dict[str, str] = {}
    for b in blocks:
        if b.get("type") == "tool_call" and b.get("call_id") and b.get("name"):
            call_names[str(b["call_id"])] = str(b["name"])

    last_comp = -1
    for i, b in enumerate(blocks):
        if b.get("type") == "compaction":
            last_comp = i

    total_all = len(blocks)
    has_comp_summary = last_comp >= 0 and bool((blocks[last_comp].get("summary") or "").strip())
    total_live = (len(blocks) - last_comp - 1) + (1 if has_comp_summary else 0) if last_comp >= 0 else total_all

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


class VaultIndex:
    """内存倒排索引: term -> [(sid, block_idx)]; 指纹失效自动重建。"""

    def __init__(self, vault: Path, block_text_fn=block_text):
        self.vault = vault
        self.block_text = block_text_fn
        self.fingerprint = None
        # 唯一状态句柄: (index, meta, sids, mtimes) 整体换装, 并发读者一次读取永远自洽
        self._state = ({}, {}, [], {})
        self._env_cache = OrderedDict()  # sid -> env, 小 LRU (快照内容寻址不可变, 无失效问题)
        self._rebuild_lock = threading.Lock()  # 单飞: 重建期间其他请求拿现态秒回, 不排队不并发重建
        self._rebuild_pending = False          # 后台重建已在排队/执行中
        self._last_rebuild = 0.0               # monotonic; 节流基线 (库在增长时指纹永远追不上, 必须节流)
        self._last_persist = 0.0               # 缓存落盘节流 (0.4GB 写盘, 增量后 ≥PERSIST_MIN_SEC 才重写)
        self.stats = {"full": 0, "incr": 0, "cache_load": 0}  # 重建方式计数 (诊断 + 测试断言)
        if self._load_cache():
            self._last_rebuild = time.monotonic()
            self.rebuild()  # 追赶: 缓存之后的新增走增量 (毫秒级), 删除/变更自动转全量
        else:
            self.rebuild(force=True)

    @property
    def index(self): return self._state[0]
    @property
    def meta(self): return self._state[1]
    @property
    def _sids(self): return self._state[2]
    @property
    def _mtimes(self): return self._state[3]

    _BLOCK_BITS = 24
    _BLOCK_MASK = (1 << 24) - 1

    ENV_CACHE_MAX = 64
    PERSIST_MIN_SEC = 300

    def load_env(self, sid):
        """按需读快照全文 (磁盘 JSON -> 解析), LRU 缓存; 不存在/损坏返回 None。"""
        env = self._env_cache.pop(sid, None)
        if env is None:
            try:
                env = json.loads((self.vault / "snapshots" / f"{sid}.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            env["_sid"] = sid
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
        fp = hashlib.sha256("|".join(f"{sid}:{m}" for sid, m in sorted(mtimes.items())).encode()).hexdigest()
        return fp, files, mtimes

    def _index_one(self, f, meta, index, sids, mtimes):
        """解析并索引单个快照。可见顺序是硬约束: 先 meta, 再 sids, 最后 postings。"""
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
        meta[sid] = meta_row
        sid_code = len(sids) << self._BLOCK_BITS
        sids.append(sid)
        try:
            mtimes[sid] = int(f.stat().st_mtime)
        except OSError:
            mtimes[sid] = 0
        for i, b in enumerate(conv):
            if not isinstance(b, dict):
                continue
            try:
                text = self.block_text(b)
            except (AttributeError, TypeError, KeyError, ValueError):
                continue
            for t in set(tokenize(text)):
                arr = index.get(t)
                if arr is None:
                    arr = index[t] = array("q")
                arr.append(sid_code | i)
        return True

    _CACHE_VER = 1

    def _persist(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_persist < self.PERSIST_MIN_SEC:
            return
        tmp = None
        try:
            import pickle
            index, meta, sids, mtimes = self._state
            p = self.vault / "index_cache.pkl"
            tmp = p.parent / (p.name + f".{os.getpid()}.tmp")
            with open(tmp, "wb") as fh:
                pickle.dump({"ver": self._CACHE_VER, "fp": self.fingerprint,
                             "meta": meta, "index": index,
                             "sids": sids, "mtimes": mtimes}, fh, protocol=4)
            os.replace(tmp, p)
            self._last_persist = now
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._last_persist = now - self.PERSIST_MIN_SEC + 30

    def _load_cache(self):
        try:
            import pickle
            p = self.vault / "index_cache.pkl"
            if not p.exists():
                return False
            with open(p, "rb") as fh:
                d = pickle.load(fh)
            if d.get("ver") != self._CACHE_VER or not isinstance(d.get("index"), dict):
                return False
            if not isinstance(d.get("meta"), dict) or len(d.get("sids") or []) != len(d["meta"]):
                return False
            self._state = (d["index"], d["meta"], d["sids"], d["mtimes"])
            self.fingerprint = d["fp"]
            self.stats["cache_load"] += 1
            return True
        except Exception:
            return False

    def rebuild(self, force=False, stale_after=0.0):
        now = time.monotonic()
        if not force and stale_after and now - self._last_rebuild < stale_after:
            return
        if not self._rebuild_lock.acquire(blocking=False):
            return
        try:
            fp, files, mtimes = self._scan()
            if not force and fp == self.fingerprint:
                return
            index, meta, sids, mtimes_known = self._state
            added = [f for sid, f in files.items() if sid not in mtimes_known]
            dirty = (set(mtimes_known) - files.keys()) or \
                    any(mtimes[s] != mtimes_known[s] for s in mtimes_known if s in mtimes)
            if not force and self.fingerprint is not None and not dirty and added:
                for f in added:
                    self._index_one(f, meta, index, sids, mtimes_known)
                self.fingerprint = fp
                self.stats["incr"] += 1
                self._persist()
                return
            new_index, new_meta, new_sids, new_mtimes = {}, {}, [], {}
            for f in files.values():
                self._index_one(f, new_meta, new_index, new_sids, new_mtimes)
            self._state = (new_index, new_meta, new_sids, new_mtimes)
            self.fingerprint = fp
            self.stats["full"] += 1
            self._persist(force=True)
        finally:
            self._last_rebuild = time.monotonic()
            self._rebuild_lock.release()

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

    def search(self, query: str, limit: int) -> dict:
        terms = tokenize(query)
        index, meta, sids = self._state[:3]
        sc = Counter()
        for t in set(terms):
            for v in index.get(t, ()):
                sc[sids[v >> self._BLOCK_BITS]] += 1
        pairs = sorted(sc.items())
        pairs.sort(key=lambda kv: meta[kv[0]].get("created_at") or "", reverse=True)
        pairs.sort(key=lambda kv: -kv[1])
        return {"query": query, "terms": terms, "total": len(pairs),
                "hits": [{"sid": sid, "score": n, **meta[sid]}
                         for sid, n in pairs[:limit]]}

    def context(self, query: str, limit: int) -> dict:
        terms = tokenize(query)
        index, meta, sids = self._state[:3]
        pairs = Counter()
        for t in set(terms):
            for v in index.get(t, ()):
                pairs[(sids[v >> self._BLOCK_BITS], v & self._BLOCK_MASK)] += 1
        ordered = sorted(pairs.items(), key=lambda kv: kv[0])
        ordered.sort(key=lambda kv: meta[kv[0][0]].get("created_at") or "", reverse=True)
        ordered.sort(key=lambda kv: -kv[1])
        out, seen = [], set()
        for (sid, i), n in ordered:
            if len(out) >= limit:
                break
            if sid in seen:
                continue
            seen.add(sid)
            env = self.load_env(sid)
            if env is None:
                continue
            b = env["conversation"][i]
            text = self.block_text(b)
            pos = text.lower().find(terms[0]) if terms else 0
            start = max(0, pos - 200)
            out.append({"sid": sid, "block": i, "role": b.get("role") or b["type"],
                        "score": n, "excerpt": text[start:start + 400],
                        "cwd": meta[sid].get("cwd")})
        return {"query": query, "terms": terms, "total": len(pairs), "results": out}
