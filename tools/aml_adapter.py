#!/usr/bin/env python3
"""AML (Agent Memory Leaderboard) 兼容 Add / Search 契约适配器。

将 Context Vault 的快照存储、SQLite 倒排检索与双时态记忆封装为标准两段式接口:
  - add(messages, ...): 写入标准化 context-snapshot 并增量更新索引与记忆
  - search(query, top_k=100, budget_tokens=None): 联合记忆与快照检索, 支持显式 token 预算裁剪

可作为 Python 库直接调用, 也可通过 `--serve` 启动本地 HTTP 接口 (默认 127.0.0.1:8792)。
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
import tempfile
import threading
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cv  # noqa: E402
import cv_memory  # noqa: E402
from cv_index import VaultIndex  # noqa: E402

MAX_HTTP_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB 请求体上限


def _flatten_content(content) -> str:
    """将纯文本或 OpenAI 多模态/分块数组统一展平为文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(_flatten_content(item["content"]))
        return "\n".join(p for p in parts if p)
    return str(content)


def _to_blocks(messages: list[dict]) -> list[dict]:
    blocks = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").lower()
        text = _flatten_content(msg.get("content"))
        if not text:
            continue
        if role == "tool":
            blocks.append({"type": "tool_result", "role": "tool", "content": text})
        else:
            blocks.append({"type": "text", "role": role, "content": text})
    return blocks


AML_REQUEST_TABLE = """
CREATE TABLE IF NOT EXISTS aml_requests (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class RequestConflictError(ValueError):
    """同 request_id 携带不同 payload 时抛出 (映射 HTTP 409 Conflict)。"""


def _normalize_request_id(val) -> str | None:
    """校验并规范化可选的 request_id: 拒绝非字符串、纯空白与超长值。"""
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError("request_id 必须为字符串")
    cleaned = val.strip()
    if not cleaned:
        raise ValueError("request_id 不能为空白字符串")
    if len(cleaned) > 256:
        raise ValueError("request_id 长度不能超过 256 字符")
    return cleaned


def _safe_tenant_id(user_id: str) -> str:
    """将 user_id 规范化为安全的单层目录名, 防止路径穿越。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (user_id or "").strip()).strip("._")
    if not cleaned:
        raise ValueError("非法的 user_id")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:40]}_{digest}"


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace 原子写入, 避免并发读到半写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _parse_budget_tokens(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool):
        raise ValueError("budget_tokens 必须为整数, 不能是布尔值")
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return int(val.strip())
        except ValueError as e:
            raise ValueError(f"无效的 budget_tokens: {val!r}") from e
    raise ValueError(f"无效的 budget_tokens: {val!r}")


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """二分截断文本, 确保估算 token 数不超过 max_tokens。"""
    if max_tokens <= 0 or not text:
        return ""
    if cv.estimate_text_tokens(text) <= max_tokens:
        return text
    if max_tokens <= 1:
        return "…"
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + "…"
        if cv.estimate_text_tokens(cand) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return (text[:best] + "…") if best > 0 else "…"


class TenantRouter:
    """按 user_id 隔离的多租户适配器路由器 (带 LRU 上限, 防止 SQLite 句柄泄漏)。"""

    def __init__(self, base_vault: Path | str, max_adapters: int = 64, default_idx: VaultIndex | None = None):
        self.base_vault = Path(base_vault)
        self.max_adapters = max(1, int(max_adapters))
        self._default_idx = default_idx
        self._lock = threading.Lock()
        self._adapters: OrderedDict[str, AMLMemoryAdapter] = OrderedDict()

    def for_user(self, user_id: str | None = None) -> AMLMemoryAdapter:
        key = "" if not user_id else _safe_tenant_id(str(user_id))
        evicted: AMLMemoryAdapter | None = None
        with self._lock:
            ad = self._adapters.get(key)
            if ad is not None:
                self._adapters.move_to_end(key)
                return ad
            target = self.base_vault if not key else (self.base_vault / "tenants" / key)
            shared_idx = self._default_idx if not key else None
            ad = AMLMemoryAdapter(target, idx=shared_idx)
            self._adapters[key] = ad
            if len(self._adapters) > self.max_adapters:
                _, evicted = self._adapters.popitem(last=False)
        if evicted is not None:
            evicted.close()
        return ad

    def close_all(self):
        with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for ad in adapters:
            ad.close()


class AMLMemoryAdapter:
    """AML Add / Search 协议适配器 (零第三方依赖, 线程安全)。"""

    def __init__(self, vault: Path | str, idx: VaultIndex | None = None):
        self.vault = Path(vault)
        self._lock = threading.RLock()
        (self.vault / "snapshots").mkdir(parents=True, exist_ok=True)
        conn = cv.db(self.vault)
        try:
            conn.executescript(cv.SCHEMA + AML_REQUEST_TABLE)
            cv_memory.ensure_memory_schema(conn)
            conn.commit()
        finally:
            conn.close()
        self._owns_idx = idx is None
        self._idx = idx if idx is not None else VaultIndex(self.vault, cv.block_text)

    def close(self):
        with self._lock:
            if getattr(self, "_owns_idx", True) and getattr(self, "_idx", None) is not None:
                self._idx._close()

    def add(
        self,
        messages: list[dict],
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        project: str = "aml",
        cwd: str = "",
        agent: str = "aml",
        parent_snapshot: str | None = None,
        extract_memory: bool = True,
        redact: bool | None = None,
    ) -> dict:
        blocks = _to_blocks(messages)
        if not blocks:
            raise ValueError("messages 为空, 无法创建快照")
        do_redact = (os.environ.get("CV_REDACT_SECRETS") == "1") if redact is None else bool(redact)
        redacted_n = 0
        if do_redact:
            for b in blocks:
                if isinstance(b.get("content"), str):
                    b["content"], n = cv.redact_secrets(b["content"])
                    redacted_n += n
        req_id = _normalize_request_id(request_id)
        eff_cwd = cwd or f"/aml/{project}"
        # 哈希纳入命名空间与完整元数据, 生成纯 hex ID (匹配全链路 ^cv_[0-9a-f]{8,64}$ 守卫)
        hash_payload = {
            "ns": "aml",
            "blocks": blocks,
            "session_id": session_id or "",
            "project": project,
            "cwd": eff_cwd,
            "agent": agent,
            "parent_snapshot": parent_snapshot,
        }
        payload_hash = hashlib.sha256(
            json.dumps(hash_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        # 请求级幂等指纹额外包含 extract_memory, 避免同 request_id 切换行为参数时误重放
        req_hash = hashlib.sha256(
            json.dumps(
                {"payload": hash_payload, "extract_memory": bool(extract_memory)},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        sid = f"cv_{payload_hash}"
        snap_path = self.vault / "snapshots" / f"{sid}.json"

        with self._lock:
            if req_id is not None:
                conn = cv.db(self.vault)
                try:
                    row = conn.execute(
                        "SELECT payload_hash, response_json FROM aml_requests WHERE request_id=?",
                        (req_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if row is not None:
                    if row[0] != req_hash:
                        raise RequestConflictError(f"request_id 冲突: {req_id}")
                    cached = json.loads(row[1])
                    cached["idempotent_replay"] = True
                    return cached
            if snap_path.exists():
                env = json.loads(snap_path.read_text(encoding="utf-8"))
                src_sid = (env.get("capture") or {}).get("source_session_id") or session_id or f"aml_{uuid.uuid4().hex[:12]}"
            else:
                now = cv.now_iso()
                src_sid = session_id or f"aml_{uuid.uuid4().hex[:12]}"
                env = {
                    "spec": "context-snapshot/0.1",
                    "id": sid,
                    "created_at": now,
                    "capture": {
                        "agent": agent,
                        "source_format": "aml-api",
                        "session_ref": src_sid,
                        "source_session_id": src_sid,
                        "cwd": eff_cwd,
                        "project": project,
                        "completeness": "full",
                    },
                    "stats": {
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "reasoning_tokens": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0,
                        "usage_events": 0,
                        "blocks_total": len(blocks),
                        "blocks_dropped": 0,
                    },
                    "conversation": blocks,
                    "lineage": {
                        "parent_snapshot": parent_snapshot,
                        "fork_of": None,
                    },
                }
                _atomic_write_text(snap_path, json.dumps(env, ensure_ascii=False, indent=1))

            conn = cv.db(self.vault)
            try:
                cv.register_snapshot(conn, env, sid, snap_path)
                conn.commit()
            finally:
                conn.close()
            self._idx.rebuild()
            mem_res = {}
            if extract_memory:
                mem_res = cv_memory.extract_memories_incremental(self.vault)
            res = {
                "snapshot_id": sid,
                "source_session_id": src_sid,
                "blocks": len(blocks),
                "memories_extracted": mem_res.get("added", 0) if isinstance(mem_res, dict) else 0,
                "idempotent_replay": False,
            }
            if req_id is not None:
                conn = cv.db(self.vault)
                try:
                    conn.execute(
                        "INSERT INTO aml_requests(request_id, payload_hash, response_json, created_at) "
                        "VALUES(?,?,?,?)",
                        (req_id, req_hash, json.dumps(res, ensure_ascii=False), cv.now_iso()),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return res

    def search(
        self,
        query: str,
        *,
        top_k: int = 100,
        budget_tokens: int | None = None,
        include_memories: bool = True,
        include_history: bool = False,
    ) -> dict:
        if isinstance(top_k, bool):
            raise ValueError("top_k 必须为整数")
        top_k = max(1, min(int(top_k), 1000))
        parsed_budget = _parse_budget_tokens(budget_tokens)

        mem_candidates: list[dict] = []
        snap_candidates: list[dict] = []
        with self._lock:
            if include_memories:
                mems = cv_memory.memory_search_api(
                    self.vault, query, limit=min(top_k, 50), include_history=include_history
                )
                for m in mems:
                    rel = float(m.get("score") or 0.0)
                    conf = float(m.get("confidence") or 1.0)
                    # 用查询相关度做主分, confidence 仅作轻量加权
                    raw_score = rel * (1.0 + 0.05 * min(conf, 5.0)) if rel > 0 else 0.0
                    mem_candidates.append({
                        "source": "memory",
                        "sid": m.get("snapshot", ""),
                        "_raw_score": raw_score,
                        "content": m.get("content", ""),
                        "metadata": {
                            "kind": m.get("kind"),
                            "status": m.get("status", "active"),
                            "project": m.get("project"),
                            "block_idx": m.get("block_idx"),
                            "confidence": round(conf, 4),
                            "raw_score": round(rel, 4),
                        },
                    })
            ctx = self._idx.context(query, limit=top_k)
            ctx_results = list(ctx.get("results", []))
            # 若显式配置了 CV_EMBEDDINGS_URL, 自动用稠密向量 RRF 语义重排快照候选 (零配置零开销)
            try:
                from cv_dense import DenseProvider, hybrid_search
                prov = DenseProvider()
                if prov.available and ctx_results:
                    hyb = hybrid_search(self._idx, self.vault, query, top_k, provider=prov)
                    rank_map = {h["sid"]: i for i, h in enumerate(hyb.get("hits", []))}
                    if rank_map:
                        ctx_results.sort(key=lambda r: rank_map.get(r.get("sid", ""), len(rank_map)))
            except Exception:
                pass
            for r in ctx_results:
                raw_score = float(r.get("score") or 0.0)
                snap_candidates.append({
                    "source": "snapshot",
                    "sid": r.get("sid", ""),
                    "_raw_score": raw_score,
                    "content": r.get("excerpt", ""),
                    "metadata": {
                        "block": r.get("block"),
                        "role": r.get("role"),
                        "cwd": r.get("cwd"),
                        "versions": r.get("versions", 1),
                        "raw_score": round(raw_score, 4),
                    },
                })

        # 两路分数各自归一到 (0, 1] 后融合排序, 避免低相关 memory 挤掉高相关 snapshot
        max_mem = max((c["_raw_score"] for c in mem_candidates), default=0.0)
        max_snap = max((c["_raw_score"] for c in snap_candidates), default=0.0)
        merged: list[dict] = []
        for idx_i, c in enumerate(mem_candidates):
            norm = (c["_raw_score"] / max_mem) if max_mem > 0 else (0.5 / (idx_i + 1))
            c["score"] = round(norm, 4)
            c["_rank_key"] = (norm, 1 if c["_raw_score"] > 0 else 0, -idx_i)
            merged.append(c)
        for idx_i, c in enumerate(snap_candidates):
            norm = (c["_raw_score"] / max_snap) if max_snap > 0 else 0.0
            c["score"] = round(norm, 4)
            c["_rank_key"] = (norm, 0, -idx_i)
            merged.append(c)

        merged.sort(key=lambda x: x["_rank_key"], reverse=True)

        # 先去重, 再按 top_k 切片 (避免前 top_k 内重复项导致返回数无故不足)
        seen_norm: set[str] = set()
        results: list[dict] = []
        for item in merged:
            item.pop("_raw_score", None)
            item.pop("_rank_key", None)
            norm = " ".join(item["content"].split())[:160]
            if norm and norm in seen_norm:
                continue
            if norm:
                seen_norm.add(norm)
            results.append(item)
            if len(results) >= top_k:
                break

        trimmed = False
        if parsed_budget is not None and parsed_budget > 0:
            budget = max(64, parsed_budget)
            kept: list[dict] = []
            used = 0
            for item in results:
                cost = cv.estimate_text_tokens(item["content"])
                if kept and used + cost > budget:
                    trimmed = True
                    break
                if not kept and used + cost > budget:
                    clipped = dict(item)
                    clipped["content"] = _truncate_to_tokens(item["content"], budget)
                    kept.append(clipped)
                    trimmed = True
                    break
                kept.append(item)
                used += cost
            results = kept

        return {
            "query": query,
            "total": len(results),
            "trimmed": trimmed,
            "results": results,
        }


class _AMLHTTPServer(ThreadingHTTPServer):
    # Windows 上关闭地址复用以防端口抢占, POSIX/Linux 容器下启用 SO_REUSEADDR 避免重启撞 TIME_WAIT
    allow_reuse_address = os.name != "nt"


def serve_aml(vault: Path, host: str = "127.0.0.1", port: int = 8792, token: str | None = None):
    router = TenantRouter(vault)
    auth_token = token or os.environ.get("CV_AML_TOKEN") or ""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            hdr = self.headers.get("Authorization") or ""
            api_key = self.headers.get("X-API-Key") or ""
            return hdr == f"Bearer {auth_token}" or api_key == auth_token

        def _send(self, code: int, obj: dict, close: bool = False):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "adapter": "aml-v1", "tenant_isolation": True})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            if self.path not in ("/add", "/search"):
                self._send(404, {"error": "not found"})
                return
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or raw_len == "":
                length = 0
            else:
                try:
                    length = int(raw_len)
                except ValueError:
                    self._send(400, {"error": "invalid Content-Length"})
                    return
                if length < 0:
                    self._send(400, {"error": "invalid Content-Length"})
                    return
                if length > MAX_HTTP_BODY_BYTES:
                    self._send(413, {"error": f"payload too large (max {MAX_HTTP_BODY_BYTES} bytes)"}, close=True)
                    return
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send(400, {"error": f"invalid json: {e}"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "request body must be a JSON object"})
                return
            try:
                uid = body.get("user_id")
                adapter = router.for_user(uid)
                if self.path == "/add":
                    req_id = body.get("request_id")
                    if req_id is None:
                        req_id = (
                            self.headers.get("Idempotency-Key")
                            or self.headers.get("X-Idempotency-Key")
                        )
                    res = adapter.add(
                        body.get("messages") or [],
                        session_id=body.get("session_id"),
                        request_id=req_id,
                        project=body.get("project") or "aml",
                        cwd=body.get("cwd") or "",
                        agent=body.get("agent") or "aml",
                        parent_snapshot=body.get("parent_snapshot"),
                        extract_memory=bool(body.get("extract_memory", True)),
                        redact=body.get("redact"),
                    )
                    self._send(200, res)
                    return
                top_k_raw = body.get("top_k", 100)
                if isinstance(top_k_raw, bool):
                    raise ValueError("top_k 必须为整数")
                res = adapter.search(
                    body.get("query") or "",
                    top_k=int(top_k_raw),
                    budget_tokens=body.get("budget_tokens"),
                    include_memories=bool(body.get("include_memories", True)),
                    include_history=bool(body.get("include_history", False)),
                )
                self._send(200, res)
            except RequestConflictError as e:
                self._send(409, {"error": str(e)})
            except Exception as e:
                self._send(400, {"error": str(e)})

    srv = _AMLHTTPServer((host, port), Handler)
    prev_sigterm = None
    install_sigterm = (
        hasattr(signal, "SIGTERM")
        and threading.current_thread() is threading.main_thread()
    )
    if install_sigterm:
        def _handle_sigterm(_signum, _frame):
            # 在独立线程调用 shutdown, 避免主线程信号回调内自锁
            threading.Thread(target=srv.shutdown, daemon=True).start()
        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    print(f"AML adapter listening on http://{host}:{port} (POST /add, POST /search, GET /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if install_sigterm and prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        srv.server_close()
        router.close_all()


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Context Vault AML Add/Search 适配器")
    ap.add_argument("--vault", default=os.environ.get("CV_VAULT_DIR") or "vault")
    ap.add_argument("--serve", action="store_true", help="启动 HTTP Add/Search 服务")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8792"))
    ap.add_argument("--token", default=None, help="可选鉴权令牌 (或设 CV_AML_TOKEN)")
    return ap


def main(argv=None):
    ap = _build_arg_parser()
    args = ap.parse_args(argv)
    if args.serve:
        serve_aml(Path(args.vault), host=args.host, port=args.port, token=args.token)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
