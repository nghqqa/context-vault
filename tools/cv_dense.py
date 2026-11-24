"""向后兼容别名层 — 稠密向量检索与缓存实现已并入 tools/cv_semantic.py。"""
from cv_semantic import (  # noqa: F401
    EMBED_KEY_ENV,
    EMBED_MODEL_ENV,
    EMBED_TIMEOUT_ENV,
    EMBED_URL_ENV,
    HYBRID_K,
    RRF_CANDIDATES,
    DenseProvider,
    EmbeddingCache,
    _snapshot_repr,
    hybrid_search,
)
