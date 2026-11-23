"""Context Vault 常驻 HTTP 热索引服务、Web 控制台、记忆 CRUD、语义增强与统一 /aml/* 接口。"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cv_index import VaultIndex, block_text, format_snapshot_messages
from cv_ui import load_console_html as _load_console_html


def _cv():
    import cv
    return cv


# ------------------------------------------------- serve: 常驻服务 + 热索引 (M2)
# 索引实现已解耦至 tools/cv_index.py, 前端模板解耦至 tools/ui/ (经 cv_ui 组装)

CONSOLE_HTML = _load_console_html()


class _KeyStub:
    key = None


def build_vault_server(vault: Path, port: int, token: str | None = None):
    """构建热索引 HTTP 服务 (不阻塞); cmd_serve 与 Pro 托盘共用, 避免实现漂移。
    token: 商业化/局域网档的可选鉴权 — 设置后所有请求需凭据 (X-Vault-Token 头 /
    Cookie vault_token / 首访 ?token= 自动 Set-Cookie), 未授权一律 401。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # 用局部子类隔离端口复用策略: Windows 下置 False 防止静默双绑抢占; Linux 容器允许复用防 TIME_WAIT 撞端口
    class _VaultHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"

    from urllib.parse import urlparse, parse_qs
    sys.path.insert(0, str(Path(__file__).parent))
    from mcp_server import ledger_summary
    import cv_ledger  # 本地多端账本 (usage_events); 延迟导入防循环 (cv_ledger 顶层 import cv)  # ledger_summary: /stats 端点 (曾漏导入致 500)
    idx = VaultIndex(vault, block_text)  # block_text 用本模块规范实现, 与 mcp 冷回退同源
    aml_router_holder = [None]

    def _get_aml_router():
        if aml_router_holder[0] is None:
            import aml_adapter as _aml
            aml_router_holder[0] = _aml.TenantRouter(vault, default_idx=idx)
        return aml_router_holder[0]

    class H(BaseHTTPRequestHandler):
        # HTTP/1.0 下 Chromium 系 webview 会复用已被服务端关闭的连接, fetch 全部挂起
        # (HTML 导航走新连接所以页面能开, 数据接口全卡"加载中")。1.1 + 全端点 Content-Length = 正确 keep-alive。
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            # 请求落盘: 诊断"浏览器 fetch 挂起"类问题的关键证据 (请求到没到服务)。
            # token 脱敏 (B03): ?token= 会随 requestline 落盘, 不能明文进日志。
            # parse_qs 会解码百分号编码键名 (%74oken= 也是有效凭据) — 先解码再匹配,
            # 且大小写不敏感, 否则编码形态的 token 明文绕过脱敏 (二轮 serve-8)
            line = self.requestline
            try:
                line = urllib.parse.unquote(line)
            except Exception:
                pass
            line = re.sub(r"([?&])token=[^& ]*", r"\1token=***", line, flags=re.IGNORECASE)
            try:
                with (vault / "serve_http.log").open("a", encoding="utf-8") as fh:
                    fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} "
                             f"{self.client_address[0]} \"{line}\" {code} "
                             f"UA={str(self.headers.get('User-Agent', ''))[:48]}\n")
            except OSError:
                pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            """统一响应出口: _new_cookie (首访 ?token= 种 cookie) 对 HTML 与 JSON 一视同仁 —
            曾只在 _json 发, 致控制台首访 HTML 后的 fetch 全部 401。"""
            self.send_response(code)
            nc = getattr(self, "_new_cookie", None)
            if nc:
                self.send_header("Set-Cookie", nc)
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", code)

        def _stats_overlay(self, sid: str, s: dict) -> dict:
            """快照文件不可变: zcode 历史版本的用量在索引层回填 (backfill_zcode_usage), 文件里
            仍是 0 — 详情/迁移预览优先展示索引里的回填值, 不再对有用量的会话显示"0 tokens"。"""
            if s.get("usage_events"):
                return s
            try:
                c = _cv().db(vault)
                r = c.execute("SELECT tokens_in, tokens_out, reasoning_tokens, cache_read, cache_write,"
                              " total_tokens, usage_events FROM snapshots WHERE id=?", (sid,)).fetchone()
                c.close()
            except sqlite3.Error:
                return s
            return {**s, **dict(r)} if r and r["usage_events"] else s

        @staticmethod
        def _sid_ok(sid: str) -> bool:
            # 快照 id 形态校验 (cv_ + hex): 挡路径穿越与任意文件读; 同时给迁移一个
            # 稳定的 JSON 400/404, 而不是让 load_snapshot 的 SystemExit 逃过 except 断连
            import re as _re
            return bool(_re.fullmatch(r"cv_[0-9a-f]{8,64}", str(sid or "")))

        def _guard(self, post: bool) -> bool:
            """本地访问防护: Host 必须回环 (挡 DNS rebinding 读会话史); Origin 若带必须回环
            (挡恶意网页跨站); POST 强制 application/json (跨站简单请求发不出该类型 = 强制预检,
            预检无 CORS 应答即被浏览器拦截)。本地进程 (MCP 热索引/托盘/CLI) 不受影响。"""
            host = (self.headers.get("Host") or "").strip().lower()
            hn = host.rsplit(":", 1)[0].strip("[]") if host.count(":") == 1 else host.strip("[]")
            if hn not in ("127.0.0.1", "localhost", "::1"):
                self._json({"error": f"forbidden: host '{host}' 不在本地白名单"}, 403)
                return False
            org = self.headers.get("Origin")
            if org:
                if (urlparse(org).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                    self._json({"error": f"forbidden: origin '{org}' 非本地"}, 403)
                    return False
            if post:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    self._json({"error": "forbidden: POST 需要 Content-Type: application/json"}, 403)
                    return False
            if token:  # 鉴权档: 头 / Cookie / 首访 query(种 cookie) 三种凭据形态
                header = self.headers.get("X-Vault-Token")
                cookie = ""
                for part in (self.headers.get("Cookie") or "").split(";"):
                    if part.strip().startswith("vault_token="):
                        cookie = part.split("=", 1)[1].strip()
                        break
                qtok = "" if post else parse_qs(urlparse(self.path).query).get("token", [""])[0]
                # 候选顺序 header > ?token= > cookie: 旧 cookie (token 轮换前/他实例所种, cookie 不按
                # 端口隔离) 曾先占位导致 ?token= 首访永久 401 且 HttpOnly 前端清不掉 (二轮 serve-5)
                ok = False
                for cand in (header, qtok, cookie):
                    if cand and hmac.compare_digest(str(cand), str(token)):
                        ok = True
                        break
                if not ok:
                    self._json({"error": "unauthorized: 需要 token (?token= 首访, 或 X-Vault-Token 头)"}, 401)
                    return False
                if "vault_token=" not in (self.headers.get("Cookie") or "") or (cookie and cookie != token):
                    self._new_cookie = f"vault_token={token}; Path=/; HttpOnly; SameSite=Strict"  # 失效 cookie 轮换刷新
                return True
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._guard(False):
                return
            try:
                if u.path == "/":
                    self._send(_load_console_html().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if u.path == "/healthz":
                    idx.request_rebuild(60)  # 后台单飞重建; 本请求拿现态秒回, 增长期绝不阻塞
                    # 单次读取 meta/term_count: 快照数与词项数出自同一代索引 (v2 SQLite 倒排)
                    return self._json({"service": "context-vault", "status": "ok",
                                       "snapshots": len(idx.meta), "terms": idx.term_count})
                if u.path == "/audit":
                    return self._json(_cv().audit_summary_and_timeline(
                        vault, q.get("since", ""), "all"))
                if u.path == "/memory":
                    try:
                        mlim = max(1, min(int(q.get("limit", 8)), 200))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    mkind = q.get("kind", "")
                    return self._json({
                        "query": q.get("q", ""),
                        "kind": mkind,
                        "counts": _cv().memory_counts_api(vault),
                        "memories": _cv().memory_search_api(vault, q.get("q", ""), mlim, kind=mkind,
                                                      include_history=(q.get("history") == "1")),
                    })
                if u.path == "/semantic/status":
                    import cv_semantic as _sem
                    return self._json(_sem.semantic_status(vault, probe=(q.get("probe") == "1")))
                if u.path == "/probe_events":
                    conn = _cv().db(vault)  # ThreadingHTTPServer 每请求短连接; 显式关闭归还 WAL 句柄
                    try:
                        plim = int(q.get("limit", 100))
                    except ValueError:
                        conn.close()
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    try:
                        rows = conn.execute("SELECT ts,profile,status,http_code,latency_ms,models,missing_models"
                                            " FROM probe_events ORDER BY id DESC LIMIT ?",
                                            (plim,)).fetchall()
                    except sqlite3.OperationalError:
                        rows = []  # 零表空库: 诚实空态, 不落 500 (二轮 concurrency-1)
                    conn.close()
                    return self._json([dict(r) for r in rows])
                if u.path == "/ledger":
                    conn = _cv().db(vault)
                    conn.executescript(_cv().SCHEMA)  # 只 connect 不建表会留零表空库毒化冷路径 (全面测试 M5)
                    try:
                        mem_n = conn.execute("SELECT COUNT(*) n FROM memory_items").fetchone()["n"]
                    except sqlite3.OperationalError:
                        mem_n = 0
                    prices = {}
                    prices_p = vault / "prices.json"  # {"模型": {"input":x,"output":y,"cache_read":z}} $/Mtok
                    if prices_p.exists():
                        num_ok = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
                        try:
                            pj = json.loads(prices_p.read_text(encoding="utf-8"))
                            # bool 是 int 子类, float(True)=1.0 会编造假成本 (二轮 regression-1)
                            prices = {m: v for m, v in pj.items()
                                      if isinstance(v, dict) and num_ok(v.get("input")) and num_ok(v.get("output"))}
                        except (ValueError, AttributeError, OSError):
                            pass  # 非法 JSON/顶层非 dict/prices 是目录: 无价目诚实显示, 不 500
                    zcode_usage = {}
                    zcode_snapshots = 0
                    try:  # 真相源: zcode 主库 model_usage (全量调用); model-io 审计文件仅零头
                        zdb = sqlite3.connect(f"file:{_cv().ZCODE_DB}?mode=ro", uri=True)
                        try:
                            for r in zdb.execute("SELECT model_id, COUNT(*), COALESCE(SUM(input_tokens),0),"
                                                 " COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_input_tokens),0)"
                                                 " FROM model_usage GROUP BY model_id"):
                                zcode_usage.setdefault(r[0] or "未知",
                                                       {"n": r[1], "tin": r[2], "tout": r[3], "cr": r[4]})
                        finally:
                            zdb.close()
                    except sqlite3.Error:
                        pass  # 无 zcode / 无表 = 无自有用量, 合法空态
                    try:
                        zn = conn.execute("SELECT COUNT(*) FROM snapshots WHERE agent='zcode'").fetchone()[0]
                        if zn:
                            zcode_snapshots = zn
                    except sqlite3.Error:
                        pass
                    for m, a in zcode_usage.items():
                        pr = prices.get(m)
                        if pr is None:
                            a["cost"] = None
                            continue
                        try:  # 值级坏形状 (字符串/null/list) 只损失该模型成本, 不拖垮整个 /ledger
                            crp = pr.get("cache_read")
                            a["cost"] = round((a["tin"] * float(pr["input"]) + a["tout"] * float(pr["output"])
                                               + a["cr"] * (float(crp) if num_ok(crp) else 0)) / 1e6, 2)
                        except (TypeError, ValueError):
                            a["cost"] = None
                    lt = conn.execute("SELECT tokens_saved, cost_usd, raw FROM ledger_events"
                                      " WHERE source='headroom-lifetime'").fetchone()
                    by_model = conn.execute("SELECT model, COUNT(*) n, SUM(tokens_saved) t, SUM(cost_usd) c"
                                            " FROM ledger_events WHERE kind='compression'"
                                            " GROUP BY model ORDER BY c DESC").fetchall()
                    monthly = conn.execute("SELECT substr(occurred_at,1,7) m, SUM(cost_usd) c"
                                           " FROM ledger_events WHERE kind='compression'"
                                           " GROUP BY m ORDER BY m").fetchall()
                    conn.close()
                    raw = json.loads(lt["raw"]) if lt else {}
                    return self._json({"lifetime": {"tokens_saved": lt["tokens_saved"] if lt else 0,
                                                    "saved_usd": lt["cost_usd"] if lt else 0, **raw},
                                       "by_model": [dict(r) for r in by_model],
                                       "monthly": [dict(r) for r in monthly],
                       "owned": _cv().owned_stats(vault), "memories": mem_n,
                       "zcode_usage": zcode_usage, "zcode_snapshots": zcode_snapshots})
                if u.path == "/ledger/usage":
                    # 本地多端账本滚动窗口 (无 headroom 主账本): 5h 进度/恢复 ETA/跑道/多维聚合
                    w = q.get("window", "5h") or "5h"
                    if w not in cv_ledger.WINDOW_CHOICES:
                        return self._json({"error": "invalid window: 可选 5h|24h|7d|30d"}, 400)
                    payload = cv_ledger.usage_payload(
                        vault, window=w, account=q.get("account") or None)
                    rq = cv_ledger.remote_quota_cached(vault)  # 配置了 CV_PLAN_TOKEN 才有值
                    if rq is not None:
                        payload["remote_quota"] = rq  # 厂商实时套餐额度 (TTL 60s 缓存)
                    aq = cv_ledger.agv_quota_cached()  # Antigravity IDE 本机 RPC (TTL 60s 含负缓存)
                    if aq is not None:
                        payload["remote_quota_agv"] = aq
                    return self._json(payload)
                if u.path == "/preview":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    env2 = idx.load_env(qid)
                    if not env2:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    blocks = env2["conversation"]
                    dist = dict(Counter(b["type"] for b in blocks))
                    live, comp = _cv().live_context(blocks)
                    live_dist = dict(Counter(b["type"] for b in live))
                    s = self._stats_overlay(qid, env2["stats"])
                    return self._json({"id": q.get("id"), "agent": env2["capture"].get("agent"),
                                       "completeness": env2["capture"].get("completeness"),
                                       "cwd": env2["capture"].get("cwd"),
                                       "block_dist": dist, "live_blocks": len(live),
                                       "live_dist": live_dist, "compactions": dist.get("compaction", 0),
                                       "live_tokens_est": _cv().estimate_tokens(live),
                                       "history_tokens_in": s["tokens_in"],
                                       "thinking": dist.get("thinking", 0),
                                       "agent_messages": dist.get("agent_message", 0),
                                       "dropped_source": s["blocks_dropped"],
                                       "no_assistant": env2["capture"].get("completeness") == "no_assistant_text"})
                if u.path == "/search":
                    idx.rebuild()
                    try:
                        lim = int(q.get("limit", 5))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    hyb = q.get("hybrid")
                    use_dense = (hyb == "1")
                    if hyb is None:
                        try:
                            from cv_dense import DenseProvider
                            use_dense = DenseProvider(vault=vault).available
                        except Exception:
                            use_dense = False
                    if use_dense:  # 可选稠密重排 (CV_EMBEDDINGS_URL 或 ai_config.json 配置时生效;
                        # 未配置/故障时 hybrid_search 内部回退纯结构化, 行为不变)
                        try:
                            from cv_dense import DenseProvider, hybrid_search
                            return self._json(hybrid_search(
                                idx, vault, q.get("q", ""), lim,
                                provider=DenseProvider(vault=vault)))
                        except Exception:
                            pass
                    return self._json(idx.search(q.get("q", ""), lim))
                if u.path == "/context":
                    idx.rebuild()
                    try:
                        lim = int(q.get("limit", 3))
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    return self._json(idx.context(q.get("q", ""), lim))
                if u.path == "/recent":
                    idx.request_rebuild(60)  # 后台单飞重建; 分页总数与徽章同源同新鲜度且秒回
                    meta, sids = idx.recent_pair()  # 锁内同代一次读取 (两次独立属性读可能跨代撕裂)
                    total = len(meta)  # 可列出库存 = 唯一对外口径: 徽章/资产卡/分页同一数字
                    try:
                        lim = max(1, min(int(q.get("limit", 5)), 200))
                        off = max(int(q.get("offset", 0)), 0)
                    except ValueError:
                        return self._json({"error": "invalid limit/offset: 需要整数"}, 400)
                    desc = q.get("order", "desc") != "asc"
                    # 迭代只追加的 sids 而非 dict.items(): 增量插入并发时 dict 迭代会 RuntimeError
                    pairs = [(sid, meta[sid]) for sid in sids]
                    versions = {}
                    if q.get("leaves") == "1":
                        # 每个会话只列演进链的当前版本 (叶子), 附该会话的版本数 (×N 徽章);
                        # 口径与 owned_stats 一致 (库内 parent_snapshot), 库不可读时退化为全量
                        try:
                            c = _cv().db(vault)
                            parents = {r[0] for r in c.execute(
                                "SELECT parent_snapshot FROM snapshots WHERE parent_snapshot IS NOT NULL")}
                            per_sess = {(r[0], r[1]): r[2] for r in c.execute(
                                "SELECT agent, session_ref, COUNT(*) FROM snapshots GROUP BY agent, session_ref")}
                            sess_of = {r[0]: (r[1], r[2]) for r in c.execute(
                                "SELECT id, agent, session_ref FROM snapshots")}
                            c.close()
                            pairs = [kv for kv in pairs if kv[0] not in parents]
                            versions = {sid: per_sess.get(sess_of.get(sid), 1) for sid, _ in pairs}
                        except sqlite3.Error:
                            pass
                        total = len(pairs)  # leaves 模式分页按会话数
                    rows = sorted(pairs, key=lambda kv: kv[1]["created_at"] or "",
                                  reverse=desc)[off:off + lim]
                    return self._json({"total": total, "versions_total": len(meta),
                                       "rows": [{"sid": k, **v, **({"versions": versions[k]}
                                                                   if k in versions else {})}
                                                for k, v in rows]})
                if u.path == "/timeline":
                    # 会话时间线: 该 session 的全部版本按天分组 (T3 leaves 的下钻视图)
                    ref = q.get("session", "")
                    agent = q.get("agent", "")
                    if not ref:
                        return self._json({"error": "invalid session: 需要 session_ref"}, 400)
                    try:
                        c = _cv().db(vault)
                        if agent:
                            rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                             " parent_snapshot FROM snapshots WHERE session_ref=? AND agent=?"
                                             " ORDER BY created_at", (ref, agent)).fetchall()
                        else:
                            rows = c.execute("SELECT id, created_at, tokens_in, total_tokens, blocks_total,"
                                             " parent_snapshot FROM snapshots WHERE session_ref=?"
                                             " ORDER BY created_at", (ref,)).fetchall()
                        c.close()
                    except sqlite3.Error:
                        rows = []
                    child = {r[5] for r in rows if r[5]}  # parent_snapshot 列 (index 5); 曾误用 x[4] 块数
                    days = {}
                    for r in rows:
                        days.setdefault(str(r["created_at"] or "")[:10], []).append(
                            {"sid": r["id"], "time": str(r["created_at"] or "")[11:19],
                             "tokens_in": r["tokens_in"] or 0, "total_tokens": r["total_tokens"] or 0,
                             "blocks": r["blocks_total"] or 0,
                             "leaf": r["id"] not in child})
                    return self._json({"session": ref, "agent": agent,
                                       "days": [{"day": d, "versions": days[d]}
                                                for d in sorted(days)]})
                if u.path == "/stats":
                    return self._json({"stats": ledger_summary(vault), "owned": _cv().owned_stats(vault),
                                       "usage": cv_ledger.usage_stats_summary(vault)})
                if u.path == "/projects":
                    return self._json(_cv().group_conversations_by_project(
                        vault,
                        project=q.get("project") or None,
                        include_subagents=(q.get("subagents") == "1"),
                        include_archived=(q.get("archived") == "1"),
                        agent=q.get("agent") or None,
                    ))
                if u.path == "/antigravity/sync":
                    only_ag = q["only_agent"].strip() or None if "only_agent" in q else "zcode"
                    res = _cv().sync_antigravity_archived(
                        vault,
                        project=q.get("project") or None,
                        apply=False,
                        dedup=(q.get("dedup", "1") == "1"),
                        only_agent=only_ag,
                    )
                    return self._json(self._agy_sync_json(res))
                if u.path == "/antigravity/batch":
                    try:
                        lim = int(q.get("limit", 0) or 0)
                    except ValueError:
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    res = _cv().sync_to_antigravity_batch(
                        vault,
                        agent=q.get("agent", "zcode"),
                        project=q.get("project") or None,
                        include_archived=(q.get("archived") == "1"),
                        limit=lim,
                        apply=False,
                    )
                    return self._json(res)
                if u.path == "/messages":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    try:
                        off = max(0, int(q.get("offset", 0) or 0))
                        lim = max(1, min(int(q.get("limit", 40) or 40), 100))
                    except ValueError:
                        return self._json({"error": "invalid offset/limit: 需要整数"}, 400)
                    env_m = idx.load_env(qid)
                    if not env_m:
                        return self._json({"error": "not found"}, 404)
                    res_m = format_snapshot_messages(
                        env_m,
                        scope=q.get("scope", "live"),
                        offset=off,
                        limit=lim,
                        query=q.get("q", ""),
                    )
                    res_m["id"] = qid
                    return self._json(res_m)
                if u.path == "/snapshot":
                    qid = q.get("id", "")
                    if not self._sid_ok(qid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    env = idx.load_env(qid)
                    if not env:
                        return self._json({"error": "not found"}, 404)
                    from collections import Counter
                    dist = dict(Counter(b["type"] for b in env["conversation"]))
                    return self._json({"id": q.get("id"), "capture": env["capture"],
                                       "lineage": env.get("lineage") or {},
                                       "stats": self._stats_overlay(qid, env["stats"]), "block_dist": dist})
                self._json({"error": "unknown route",
                            "routes": ["/", "/healthz", "/search?q=", "/context?q=", "/preview?id=",
                                       "/recent", "/projects", "/timeline?session=", "/stats",
                                       "/snapshot?id=", "/messages?id=", "/memory", "/ledger",
                                       "/ledger/usage?window=", "/probe_events",
                                       "/antigravity/sync", "/antigravity/batch",
                                       "POST /sync", "POST /migrate", "POST /migrate_project",
                                       "POST /fork", "POST /memory/extract", "POST /memory/dedup",
                                       "POST /antigravity/sync", "POST /antigravity/batch", "POST /probe"]}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        @staticmethod
        def _agy_sync_json(res: dict) -> dict:
            return {
                "applied": res["applied"],
                "targets_count": len(res["targets"]),
                "targets": [
                    {
                        "cid": t["cid"],
                        "sid": t["sid"],
                        "agent": t["agent"],
                        "cwd": t["cwd"],
                        "title": t["title"],
                        "reason": t["reason"],
                        "has_db": t["has_db"],
                        "has_summary": t["has_summary"],
                        "has_proto": t["has_proto"],
                    }
                    for t in res["targets"]
                ],
                "empty_projects": [
                    {"id": e["id"], "name": e["name"], "cwd": e["cwd"]}
                    for e in res.get("empty_projects") or []
                ],
                "db_files": res["db_files"],
                "summary_ids": res["summary_ids"],
                "proto_ids": res["proto_ids"],
                "dup_count": res["dup_count"],
                "agent_count": res["agent_count"],
                "removed_dbs": res["removed_dbs"],
                "removed_projects": res.get("removed_projects", 0),
                "cleaned_summaries": res["cleaned_summaries"],
                "cleaned_proto": res["cleaned_proto"],
                "summary_check_error": res.get("summary_check_error"),
            }

        def do_POST(self):
            import urllib.parse as _up
            u = _up.urlparse(self.path)
            # 先按 Content-Length 读净 body 再守卫: 守卫拒绝 (401/403) 若不读 body 就应答,
            # 残留字节会被 keep-alive 连接的下一轮当成请求行解析, 污染整条连接 (全面测试 M1)
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._json({"error": "invalid Content-Length"}, 400)
                return
            raw = self.rfile.read(n) if n > 0 else b""
            if not self._guard(True):
                return
            try:
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
                if not isinstance(body, dict):  # [1,2]/null/"str"/42 曾落 500 兜底回显异常 (二轮 serve-6)
                    return self._json({"error": "invalid JSON body: 需要对象"}, 400)
                if u.path == "/migrate":
                    # 回归: 不存在的 sid 曾让 load_snapshot 抛 SystemExit (BaseException),
                    # 逃过 except Exception 断连无 JSON; 先校验形态与存在性, 稳定 400/404
                    mid = str(body.get("id", ""))
                    if not self._sid_ok(mid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    if not (vault / "snapshots" / f"{mid}.json").exists():
                        return self._json({"error": f"snapshot not found: {mid}"}, 404)
                    try:
                        r = _cv().do_import(vault, mid, body.get("target", "codex"),
                                      bool(body.get("apply")),
                                      budget_tokens=body.get("budget_tokens"))
                    except _cv().SnapshotNotFound as e:  # 存在性检查到加载之间被删的竞态兜底
                        return self._json({"error": str(e)}, 404)
                    except _cv().BadMigrateTarget as e:  # HTTP 层没有 argparse choices, 未知 target 挡在这
                        return self._json({"error": str(e)}, 400)
                    return self._json(r)
                if u.path == "/migrate_project":
                    proj = str(body.get("project") or "").strip()
                    if not proj:
                        return self._json({"error": "missing project: 需要 project 参数"}, 400)
                    target = str(body.get("target") or "antigravity")
                    try:
                        lim = int(body.get("limit") or 0)
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    items = _cv()._resolve_project_items(
                        vault, proj, limit=lim,
                        include_archived=bool(body.get("archived")),
                        agent=str(body.get("agent") or "").strip() or None,
                    )
                    if not items:
                        return self._json({"error": f"未找到项目的顶层主对话: {proj}"}, 404)
                    apply_flag = bool(body.get("apply")) and target in ("codex", "claude", "antigravity", "zcode")
                    results = []
                    try:
                        for it in items:
                            results.append(_cv().do_import(vault, it["id"], target, apply_flag))
                    except _cv().SnapshotNotFound as e:
                        return self._json({"error": str(e)}, 404)
                    except _cv().BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json({
                        "project": proj,
                        "target": target,
                        "applied": apply_flag,
                        "count": len(results),
                        "dest_dir": str(Path(results[0]["dest"]).parent),
                        "files": [r["dest"] for r in results],
                        "total_lines": sum(r["lines"] for r in results),
                    })
                if u.path == "/sync":
                    res = _cv().sync_vault(vault, debounce=bool(body.get("debounce")), quiet=True)
                    idx.rebuild()
                    if body.get("extract_memory", True):
                        try:
                            mres = _cv().extract_memories_incremental(vault)
                            res["memories_added"] = mres["added"]
                            res["memories_total"] = mres["total"]
                        except Exception:
                            res["memories_added"] = 0
                    return self._json(res)
                if u.path == "/memory/extract":
                    mres = _cv().extract_memories_incremental(vault, force=bool(body.get("force")))
                    return self._json(mres)
                if u.path == "/memory/dedup":
                    return self._json(_cv().memory_dedup(vault))
                if u.path == "/memory/add":
                    try:
                        mres = _cv().memory_add_manual(
                            vault,
                            body.get("content", ""),
                            kind=body.get("kind") or "decision",
                            project=body.get("project") or "manual",
                            pinned=bool(body.get("pinned", False)),
                        )
                        return self._json(mres)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if u.path == "/memory/delete":
                    try:
                        dres = _cv().memory_delete(vault, body.get("id"))
                        return self._json(dres)
                    except (TypeError, ValueError) as e:
                        return self._json({"error": str(e)}, 400)
                if u.path == "/memory/pin":
                    try:
                        pres = _cv().memory_set_pin(vault, body.get("id"), pinned=bool(body.get("pinned", True)))
                        return self._json(pres)
                    except (TypeError, ValueError) as e:
                        return self._json({"error": str(e)}, 400)
                if u.path in ("/aml/add", "/aml/search"):
                    import aml_adapter as _aml
                    try:
                        uid = body.get("user_id")
                        adapter = _get_aml_router().for_user(uid)
                        if u.path == "/aml/add":
                            req_id = body.get("request_id")
                            if req_id is None:
                                req_id = (
                                    self.headers.get("Idempotency-Key")
                                    or self.headers.get("X-Idempotency-Key")
                                )
                            ares = adapter.add(
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
                            if not uid:
                                idx.rebuild()  # 默认主库写入同步刷新热索引
                            return self._json(ares)
                        top_k_raw = body.get("top_k", 100)
                        if isinstance(top_k_raw, bool):
                            raise ValueError("top_k 必须为整数")
                        ares = adapter.search(
                            body.get("query") or "",
                            top_k=int(top_k_raw),
                            budget_tokens=body.get("budget_tokens"),
                            include_memories=bool(body.get("include_memories", True)),
                            include_history=bool(body.get("include_history", False)),
                        )
                        return self._json(ares)
                    except _aml.RequestConflictError as e:
                        return self._json({"error": str(e)}, 409)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if u.path == "/fork":
                    fid = str(body.get("id") or "")
                    if not self._sid_ok(fid):
                        return self._json({"error": "invalid id: 需要 cv_ 前缀的快照 id"}, 400)
                    msg = str(body.get("message") or "").strip()[:200] or None
                    try:
                        fres = _cv().fork_snapshot(vault, fid, msg)
                    except _cv().SnapshotNotFound as e:
                        return self._json({"error": str(e)}, 404)
                    idx.rebuild()
                    return self._json(fres)
                if u.path == "/antigravity/sync":
                    only_ag = str(body["only_agent"]).strip() or None if "only_agent" in body else "zcode"
                    try:
                        res = _cv().sync_antigravity_archived(
                            vault,
                            project=str(body.get("project") or "").strip() or None,
                            apply=bool(body.get("apply")),
                            dedup=bool(body.get("dedup", True)),
                            only_agent=only_ag,
                        )
                    except _cv().BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(self._agy_sync_json(res))
                if u.path == "/antigravity/batch":
                    try:
                        lim = int(body.get("limit") or 0)
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    ag = str(body.get("agent") if "agent" in body else "zcode").strip()
                    try:
                        res = _cv().sync_to_antigravity_batch(
                            vault,
                            agent=ag,
                            project=str(body.get("project") or "").strip() or None,
                            include_archived=bool(body.get("archived")),
                            limit=lim,
                            apply=bool(body.get("apply")),
                        )
                    except _cv().BadMigrateTarget as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json(res)
                if u.path == "/probe":
                    baseline_p = vault / "probe_baseline.json"
                    baseline = json.loads(baseline_p.read_text(encoding="utf-8")) if baseline_p.exists() else {}
                    alerts = _cv().probe_once(vault, _cv().load_profiles(), _cv().resolve_key(_KeyStub()), baseline,
                                        verbose=False)
                    _cv().atomic_write_text(baseline_p, json.dumps(baseline, ensure_ascii=False, indent=1))
                    return self._json({"alerts": alerts})
                if u.path == "/semantic/config":
                    import cv_semantic as _sem
                    if body.get("ollama"):
                        res = _sem.configure_ollama(
                            vault,
                            host=str(body.get("host") or "http://127.0.0.1:11434"),
                            embed_model=body.get("embeddings_model") or body.get("embed_model"),
                            llm_model=body.get("llm_model"),
                            enable_llm=bool(body.get("enable_llm", True)),
                        )
                        return self._json(res)
                    _sem.save_ai_config(vault, body)
                    return self._json(_sem.semantic_status(vault, probe=False))
                if u.path == "/semantic/refine":
                    import cv_semantic as _sem
                    try:
                        lim = max(1, min(int(body.get("limit") or 10), 100))
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    sid = str(body.get("snapshot_id") or "").strip() or None
                    if sid and not self._sid_ok(sid):
                        return self._json({"error": "invalid snapshot_id"}, 400)
                    return self._json(_sem.refine_memories_with_llm(
                        vault, limit=lim, snapshot_id=sid, force=bool(body.get("force")),
                    ))
                if u.path == "/semantic/titles":
                    import cv_semantic as _sem
                    try:
                        lim = max(1, min(int(body.get("limit") or 30), 200))
                    except (TypeError, ValueError):
                        return self._json({"error": "invalid limit: 需要整数"}, 400)
                    return self._json(_sem.generate_session_titles(
                        vault, limit=lim, force=bool(body.get("force")),
                        use_llm=bool(body.get("use_llm", True)),
                    ))
                self._json({"error": "unknown route"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    # 首选端口可能落进 Windows 动态保留区段 (Hyper-V/WinNAT 重启后漂移, 绑定报 WinError 10013) —
    # 托盘曾把任何绑定失败都当成"已有实例"直接退出, 11-06 起整整停摆。现在: 首选端口失败时先探活,
    # 真有 Context Vault 实例在跑才按单实例语义抛出; 否则依次退到备用端口, 实际地址写 serve.json
    srv, last_err = None, None
    for cand in ([port] if port == 0 else [port, *SERVE_FALLBACK_PORTS, 0]):
        try:
            srv = _VaultHTTPServer(("127.0.0.1", cand), H)
            break
        except OSError as e:
            if cand == port and _cv_service_alive(port):
                raise  # 另一个 Context Vault 实例已占用: 交调用方处理 (托盘单实例退出)
            last_err = e
    if srv is None:
        raise last_err
    actual = srv.server_address[1]
    if port and actual != port:
        print(f"  ⚠ 端口 {port} 不可用 ({last_err}) — 改用 {actual}, 实际地址见 vault/serve.json",
              file=sys.stderr)
    try:  # 服务发现: MCP 热路径/托盘按此找到实际地址 (显式 CV_VAULT_SVC 仍优先)
        _cv().atomic_write_text(vault / "serve.json", json.dumps(
            {"url": f"http://127.0.0.1:{actual}", "port": actual, "pid": os.getpid(),
             "started": _cv().now_iso()}))
    except OSError:
        pass
    orig_close = srv.server_close

    def _close_with_aml():
        try:
            if aml_router_holder[0] is not None:
                aml_router_holder[0].close_all()
        finally:
            orig_close()

    srv.server_close = _close_with_aml
    return srv, idx


SERVE_FALLBACK_PORTS = [18791, 28791]


def _cv_service_alive(port: int) -> bool:
    """该端口上是否已有 Context Vault 服务在应答 (区分"重复实例"与"端口被系统保留/他用")。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz",
                                     headers={"User-Agent": "ContextVault/0.1"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("service") == "context-vault"
    except urllib.error.HTTPError as e:
        return e.code == 401  # 鉴权档实例拒绝无凭据探活: 也说明是我们的服务
    except Exception:
        return False


def cmd_serve(args):
    token = args.token or os.environ.get("CV_SERVE_TOKEN")
    srv, idx = build_vault_server(_cv().vault_dir(args), args.port, token=token)
    port = srv.server_address[1]  # 首选端口被保留时会退到备用端口: 打印实际地址
    base_url = f"http://127.0.0.1:{port}/"
    print(f"vault 热索引就绪: {len(idx.meta)} 快照, {idx.term_count} 词项 (引擎 v2, SQLite 倒排)")
    print(f"Context Vault 服务已启动: {base_url} (Ctrl+C 退出)")
    if token:
        print("  鉴权已启用: 首访 http://127.0.0.1:{}/?token=*** 自动登录; "
              "MCP/脚本侧设 CV_SERVE_TOKEN 同值".format(port))
    print("  MCP server 自动发现本服务走热索引 (vault/serve.json; CV_VAULT_SVC 可显式覆盖)")
    print("  本地访问防护已启用: Host/Origin 回环校验 + POST 强制 application/json"
          " (浏览器跨站与 DNS rebinding 会被 403 拦截, 本地进程不受影响)")
    if getattr(args, "open", False):
        import threading
        import time
        import webbrowser
        open_url = f"{base_url}?token={urllib.parse.quote(str(token))}" if token else base_url

        def _open_browser():
            time.sleep(0.35)
            try:
                webbrowser.open(open_url)
            except Exception:
                pass

        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()
        idx._close()

