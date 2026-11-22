"""可选稠密向量语义重排层 (零依赖默认关闭 — 未配置即零外发、零文件、零开销)。

定位: cv_index 的结构化 BM25 + Lineage 折叠是唯一默认检索路径; 本模块是可插拔增强层 —
配置 CV_EMBEDDINGS_URL 后, 用 OpenAI 兼容 /v1/embeddings 端点 (Ollama/vLLM/本地 sidecar)
做快照级语义重排 (RRF 融合), 向量缓存在 vault/embeddings.db (派生层, 可重建)。
未配置 URL 时 available()=False, hybrid_search 直接回退纯结构化结果, 绝不报错。
核心零第三方依赖铁律不受影响: 本模块仅在显式启用时才发网络请求。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import urllib.error
import urllib.request
from pathlib import Path

from cv_index import rrf_fuse

EMBED_URL_ENV = "CV_EMBEDDINGS_URL"       # 如 http://127.0.0.1:11434/v1/embeddings (Ollama)
EMBED_MODEL_ENV = "CV_EMBEDDINGS_MODEL"   # 如 nomic-embed-text
EMBED_KEY_ENV = "CV_EMBEDDINGS_KEY"       # 可选 Bearer
EMBED_TIMEOUT_ENV = "CV_EMBEDDINGS_TIMEOUT"   # 秒, 默认 8
HYBRID_K = 60                             # RRF 常数
RRF_CANDIDATES = 200                      # BM25 候选快照数 (送入融合的池深)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class DenseProvider:
    """OpenAI 兼容 embedding 客户端 (默认不存在 = 整层关闭)。"""

    def __init__(self, url: str | None = None, model: str | None = None,
                 key: str | None = None, vault: Path | None = None):
        cfg = {}
        if vault is not None:
            try:
                from cv_semantic import load_ai_config
                cfg = load_ai_config(vault)
            except Exception:
                cfg = {}
        self.url = (url or _env(EMBED_URL_ENV) or str(cfg.get("embeddings_url") or "").strip())
        self.model = (model or _env(EMBED_MODEL_ENV) or str(cfg.get("embeddings_model") or "").strip()
                      or "nomic-embed-text")
        self.key = (key or _env(EMBED_KEY_ENV) or str(cfg.get("embeddings_key") or "").strip())
        try:
            self.timeout = float(_env(EMBED_TIMEOUT_ENV, "8"))
        except ValueError:
            self.timeout = 8.0

    @property
    def available(self) -> bool:
        return bool(self.url)

    def embed(self, texts: list) -> list:
        """批量向量化; 失败抛异常 (调用方负责回退), 不吞错伪装成功。"""
        if not self.available:
            raise RuntimeError("dense provider 未配置 (CV_EMBEDDINGS_URL)")
        body = {"model": self.model, "input": texts}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        data = out.get("data") or []
        vecs = [d.get("embedding") for d in data]
        if len(vecs) != len(texts) or any(not isinstance(v, list) or not v for v in vecs):
            raise ValueError(f"embedding 端点返回形状不符: {len(vecs)} vs {len(texts)}")
        return vecs


class EmbeddingCache:
    """向量缓存 (vault/embeddings.db, 派生层可重建)。key = sha256(model + 文本)。"""

    def __init__(self, vault: Path):
        self.path = Path(vault) / "embeddings.db"
        self._conn = None

    def _db(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, timeout=10)
            self._conn.execute("CREATE TABLE IF NOT EXISTS vec_cache("
                               " key TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB)")
            self._conn.commit()
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    @staticmethod
    def _key(model: str, text: str) -> str:
        return hashlib.sha256((model + "\x00" + text).encode("utf-8")).hexdigest()

    @staticmethod
    def _pack(vec: list) -> bytes:
        return struct.pack(f"<{len(vec)}f", *vec)

    @staticmethod
    def _unpack(blob: bytes) -> list:
        n = len(blob) // 4
        return list(struct.unpack(f"<{n}f", blob[:n * 4]))

    def get_many(self, model: str, texts: list) -> tuple:
        """返回 (vecs, missing_texts) — 命中位填 None。"""
        conn = self._db()
        out, missing = [], []
        for t in texts:
            row = conn.execute("SELECT vec FROM vec_cache WHERE key=?",
                               (self._key(model, t),)).fetchone()
            if row:
                out.append(self._unpack(bytes(row[0])))
            else:
                out.append(None)
                missing.append(t)
        return out, missing

    def put_many(self, model: str, texts: list, vecs: list) -> None:
        conn = self._db()
        conn.executemany("INSERT OR REPLACE INTO vec_cache(key,model,dim,vec) VALUES(?,?,?,?)",
                         [(self._key(model, t), model, len(v), self._pack(v))
                          for t, v in zip(texts, vecs)])
        conn.commit()

    def stats(self) -> dict:
        if not self.path.exists():
            return {"entries": 0, "bytes": 0}
        conn = self._db()
        n = conn.execute("SELECT COUNT(*) FROM vec_cache").fetchone()[0]
        try:
            b = self.path.stat().st_size
        except OSError:
            b = 0
        return {"entries": n, "bytes": b}


def _snapshot_repr(env: dict) -> str:
    """快照的向量化代理文本 (标题 + 首条用户输入 + cwd) — 不全文向量化, 缓存友好。"""
    capture = env.get("capture") or {}
    first = ""
    for b in env.get("conversation") or []:
        if isinstance(b, dict) and b.get("role") == "user" and (b.get("content") or b.get("summary")):
            first = str(b.get("content") or b.get("summary"))[:400]
            break
    return f"{capture.get('cwd', '')}\n{first}"


def hybrid_search(idx, vault: Path, query: str, limit: int,
                  provider: DenseProvider | None = None, cache: EmbeddingCache | None = None) -> dict:
    """RRF 混合检索: BM25 候选池 × 稠密语义排序 → 融合。任何环节失败都优雅回退纯 BM25。
    idx: 已 warmed 的 VaultIndex; provider/cache 未传则按环境构造。"""
    lim = max(1, int(limit))
    base = idx.search(query, max(lim, RRF_CANDIDATES))
    fallback_hits = base["hits"][:lim]
    provider = provider or DenseProvider(vault=vault)
    if not provider.available or not base["hits"]:
        return {**base, "hits": fallback_hits, "hybrid": False}
    owned_cache = cache is None
    try:
        cache = cache or EmbeddingCache(vault)
        model = provider.model
        sids = [h["sid"] for h in base["hits"]]
        envs = {}
        texts = []
        for sid in sids:
            env = idx.load_env(sid)
            if env is None:
                continue
            envs[sid] = env
            texts.append(_snapshot_repr(env))
        cached, missing = cache.get_many(model, texts)
        if missing:
            uniq_missing = list(dict.fromkeys(missing))
            fresh = provider.embed(uniq_missing)
            by_text = dict(zip(uniq_missing, fresh))
            cached = [v if v is not None else by_text[t] for v, t in zip(cached, texts)]
            cache.put_many(model, uniq_missing, [by_text[t] for t in uniq_missing])
        qvec = provider.embed([query])[0]

        def dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        def norm(a):
            return max(sum(x * x for x in a) ** 0.5, 1e-9)

        qn = norm(qvec)
        dense_ranked = sorted(
            ((dot(qvec, v) / (qn * norm(v)), sid) for sid, v in zip(envs.keys(), cached)),
            key=lambda kv: (-kv[0], kv[1]))
        dense_order = [sid for _, sid in dense_ranked]
        bm25_order = sids
        fused = rrf_fuse([bm25_order, dense_order], k=HYBRID_K)
        keep = set(sids[:lim * 3])
        order = [sid for sid, _ in fused if sid in keep][:lim]
        by_sid = {h["sid"]: h for h in base["hits"]}
        hits = []
        for rank, sid in enumerate(order):
            h = dict(by_sid[sid])
            h["hybrid_rank"] = rank
            hits.append(h)
        return {**base, "hits": hits, "hybrid": True}
    except Exception:
        return {**base, "hits": fallback_hits, "hybrid": False}  # 稠密层任何故障 → 优雅回退, 结构化检索绝不失效
    finally:
        if owned_cache and cache is not None:
            cache.close()
