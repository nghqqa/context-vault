#!/usr/bin/env python3
"""AML (Agent Memory Leaderboard) 兼容 Add / Search 契约适配器。

将 Context Vault 的快照存储、SQLite 倒排检索与双时态记忆封装为标准两段式接口:
  - add(messages, ...): 写入标准化 context-snapshot 并增量更新索引与记忆
  - search(query, top_k=100, budget_tokens=None): 联合记忆与快照检索, 支持显式 token 预算裁剪

可作为 Python 库直接调用, 也可通过 `--serve` 启动本地 HTTP 接口 (默认 127.0.0.1:8792)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cv  # noqa: E402
import cv_memory  # noqa: E402
from cv_index import VaultIndex  # noqa: E402


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
        role = str(msg.get("role") or "user").lower()
        text = _flatten_content(msg.get("content"))
        if not text:
            continue
        if role == "tool":
            blocks.append({"type": "tool_result", "role": "tool", "content": text})
        else:
            blocks.append({"type": "text", "role": role, "content": text})
    return blocks


def _safe_tenant_id(user_id: str) -> str:
    """将 user_id 规范化为安全的单层目录名, 防止路径穿越。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (user_id or "").strip()).strip("._")
    if not cleaned:
        raise ValueError("非法的 user_id")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:40]}_{digest}"


class TenantRouter:
    """按 user_id 隔离的多租户适配器路由器 (防止 AML 跨用户检索串话)。"""

    def __init__(self, base_vault: Path | str):
        self.base_vault = Path(base_vault)
        self._lock = threading.Lock()
        self._adapters: dict[str, AMLMemoryAdapter] = {}

    def for_user(self, user_id: str | None = None) -> AMLMemoryAdapter:
        key = "" if not user_id else _safe_tenant_id(str(user_id))
        with self._lock:
            ad = self._adapters.get(key)
            if ad is None:
                target = self.base_vault if not key else (self.base_vault / "tenants" / key)
                ad = AMLMemoryAdapter(target)
                self._adapters[key] = ad
            return ad

    def close_all(self):
        with self._lock:
            for ad in self._adapters.values():
                ad.close()
            self._adapters.clear()


class AMLMemoryAdapter:
    """AML Add / Search 协议适配器 (零第三方依赖)。"""

    def __init__(self, vault: Path | str):
        self.vault = Path(vault)
        (self.vault / "snapshots").mkdir(parents=True, exist_ok=True)
        conn = cv.db(self.vault)
        conn.executescript(cv.SCHEMA + cv_memory.MEMORY_TABLE)
        conn.commit()
        conn.close()
        self._idx = VaultIndex(self.vault, cv.block_text)

    def close(self):
        if getattr(self, "_idx", None) is not None:
            self._idx._close()

    def add(
        self,
        messages: list[dict],
        *,
        session_id: str | None = None,
        project: str = "aml",
        cwd: str = "",
        agent: str = "aml",
        parent_snapshot: str | None = None,
        extract_memory: bool = True,
    ) -> dict:
        blocks = _to_blocks(messages)
        if not blocks:
            raise ValueError("messages 为空, 无法创建快照")
        now = cv.now_iso()
        src_sid = session_id or f"aml_{uuid.uuid4().hex[:12]}"
        payload_hash = hashlib.sha256(
            json.dumps(blocks, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        sid = f"cv_aml_{payload_hash}"
        snap_path = self.vault / "snapshots" / f"{sid}.json"
        env = {
            "spec": "context-snapshot/0.1",
            "id": sid,
            "created_at": now,
            "capture": {
                "agent": agent,
                "source_format": "aml-api",
                "session_ref": src_sid,
                "source_session_id": src_sid,
                "cwd": cwd or f"/aml/{project}",
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
        if not snap_path.exists():
            snap_path.write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
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
        return {
            "snapshot_id": sid,
            "source_session_id": src_sid,
            "blocks": len(blocks),
            "memories_extracted": mem_res.get("added", 0) if isinstance(mem_res, dict) else 0,
        }

    def search(
        self,
        query: str,
        *,
        top_k: int = 100,
        budget_tokens: int | None = None,
        include_memories: bool = True,
        include_history: bool = False,
    ) -> dict:
        top_k = max(1, min(int(top_k), 1000))
        results: list[dict] = []
        if include_memories:
            mems = cv_memory.memory_search_api(
                self.vault, query, limit=min(top_k, 50), include_history=include_history
            )
            for m in mems:
                results.append({
                    "source": "memory",
                    "sid": m.get("snapshot", ""),
                    "score": round(float(m.get("confidence") or 1.0), 4),
                    "content": m.get("content", ""),
                    "metadata": {
                        "kind": m.get("kind"),
                        "status": m.get("status", "active"),
                        "project": m.get("project"),
                        "block_idx": m.get("block_idx"),
                    },
                })
        ctx = self._idx.context(query, limit=top_k)
        for r in ctx.get("results", []):
            results.append({
                "source": "snapshot",
                "sid": r.get("sid", ""),
                "score": round(float(r.get("score") or 0.0), 4),
                "content": r.get("excerpt", ""),
                "metadata": {
                    "block": r.get("block"),
                    "role": r.get("role"),
                    "cwd": r.get("cwd"),
                    "versions": r.get("versions", 1),
                },
            })
        results = results[:top_k]
        # 轻量去重: 避免同一句话同时以 memory 和 snapshot 摘录重复占满预算
        seen_norm: set[str] = set()
        deduped: list[dict] = []
        for item in results:
            norm = " ".join(item["content"].split())[:160]
            if norm and norm in seen_norm:
                continue
            if norm:
                seen_norm.add(norm)
            deduped.append(item)
        results = deduped
        trimmed = False
        if budget_tokens is not None and int(budget_tokens) > 0:
            budget = max(64, int(budget_tokens))
            kept, used = [], 0
            for item in results:
                cost = cv.estimate_text_tokens(item["content"])
                if kept and used + cost > budget:
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


def serve_aml(vault: Path, host: str = "127.0.0.1", port: int = 8792, token: str | None = None):
    router = TenantRouter(vault)
    auth_token = token or os.environ.get("CV_AML_TOKEN") or ""
    ThreadingHTTPServer.allow_reuse_address = False

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

        def _send(self, code: int, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send(400, {"error": f"invalid json: {e}"})
                return
            try:
                uid = body.get("user_id")
                adapter = router.for_user(uid)
                if self.path == "/add":
                    res = adapter.add(
                        body.get("messages") or [],
                        session_id=body.get("session_id"),
                        project=body.get("project") or "aml",
                        cwd=body.get("cwd") or "",
                        agent=body.get("agent") or "aml",
                        parent_snapshot=body.get("parent_snapshot"),
                        extract_memory=bool(body.get("extract_memory", True)),
                    )
                    self._send(200, res)
                    return
                if self.path == "/search":
                    res = adapter.search(
                        body.get("query") or "",
                        top_k=int(body.get("top_k", 100)),
                        budget_tokens=body.get("budget_tokens"),
                        include_memories=bool(body.get("include_memories", True)),
                        include_history=bool(body.get("include_history", False)),
                    )
                    self._send(200, res)
                    return
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(400, {"error": str(e)})

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"AML adapter listening on http://{host}:{port} (POST /add, POST /search, GET /health)")
    try:
        srv.serve_forever()
    finally:
        router.close_all()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Context Vault AML Add/Search 适配器")
    ap.add_argument("--vault", default="vault")
    ap.add_argument("--serve", action="store_true", help="启动 HTTP Add/Search 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--token", default=None, help="可选鉴权令牌 (或设 CV_AML_TOKEN)")
    args = ap.parse_args(argv)
    if args.serve:
        serve_aml(Path(args.vault), host=args.host, port=args.port, token=args.token)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
