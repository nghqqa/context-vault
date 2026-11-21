#!/usr/bin/env python3
"""cv_ledger — 本地多端 token 用量与额度账本 (纯 stdlib, 脱离 headroom)。

直接只读采集本机 AI 客户端会话日志, 形成逐次/逐轮 usage 事件流 (usage_events 表),
计算 5 小时滚动窗口、恢复 ETA、7d/月跑道、真实缓存命中率与多维归因。
零第三方依赖 (Python 3.9+ stdlib only); 对外部客户端目录一律只读 (SQLite mode=ro)。

诚实边界:
  - usage 字段来自客户端精确记录 → is_estimated=0 (zcode/claude/codex);
    antigravity 本地库为 protobuf blob 无精确 token, 字符体积估算 → is_estimated=1。
  - 费用按内置公开列表价折算 (可 vault/pricing.json 覆盖); 未知单价 → cost_est_usd=NULL。
  - 订阅制模型同样按列表价折算, 只是估算, 非账单。

口径归一化 (写库即规范, 跨端可比):
  Claude 语义 input_tokens 不含 cache; OpenAI/Codex/ZCode 语义 input 已含 cache。
  统一规范为 Anthropic 口径: tokens_in 不含缓存, 总输入 = tokens_in + cache_read + cache_write。

event_key 幂等 (重复 sync 零重复入账):
  claude: sha1("claude|"+sessionId+"|"+(message.id or requestId or uuid)), 同一 message.id
          的多内容块行取 output_tokens 最大的一条 (流式分块不重复计费);
  codex:  顺序跟踪 prev_total, 仅 total_token_usage 真增长时取增量,
          key = sha1("codex|"+session_id+"|"+f"{tot_in}:{tot_out}:{tot_cached}") (心跳重复天然去重);
  zcode:  sha1("zcode|"+requestId+"|"+attempt);
  antigravity: sha1("agv|"+conv_id+"|"+step_idx) (或会话级 "|conv");
  agm:    sha1("agm|"+上游 id 或字段哈希)。

命名说明: snapshots 表已有同名整型「列」usage_events (快照内嵌 usage 计数), 与本「表」
usage_events 属不同 SQLite 命名空间, 互不影响 — 既有代码均以列限定方式引用。

配置 (均可选, 缺失时功能完整可用):
  vault/pricing.json   单价覆盖 {"模型或通配": {"input","output","cache_read","cache_write"}} $/Mtok
  vault/prices.json    旧价目文件 (pricing.json 优先于它, 两者都先于内置表)
  vault/accounts.json  账号与额度池 {"profiles":[{name, match{source,client,model_prefix},
                        window_5h{tokens,usd,weight}, period{kind:7d|monthly,tokens,usd}}]}

水位: usage_sync_state 记录 (source,path) 的 mtime/size; antigravity 的 .db 额外纳入
同名 .db-wal 的 mtime/size (WAL 未 checkpoint 的活跃会话不漏采)。
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.request import urlopen

# 同目录兄弟模块: HOME/CODEX_SESSIONS/db/SCHEMA/iter_jsonl/now_iso/vault_dir。
# 全部路径取自 cv.HOME / cv.CODEX_SESSIONS 的运行时属性访问 — 测试 monkeypatch cv.HOME 即全隔离。
import cv

WINDOW_5H = 5 * 3600
WINDOW_CHOICES = {"5h": 5 * 3600, "24h": 24 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}
USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  client TEXT, account TEXT, model TEXT, project TEXT, session_ref TEXT,
  occurred_at REAL NOT NULL,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  cost_est_usd REAL, is_estimated INTEGER DEFAULT 0, price_known INTEGER DEFAULT 0,
  raw TEXT);
CREATE INDEX IF NOT EXISTS idx_ue_time ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_ue_acct ON usage_events(account, occurred_at);
CREATE TABLE IF NOT EXISTS usage_sync_state(
  source TEXT, path TEXT, mtime REAL, size INTEGER,
  wal_mtime REAL DEFAULT 0, wal_size INTEGER DEFAULT 0, synced_at TEXT,
  PRIMARY KEY(source, path));
"""
CFG_FP_KEY = "usage_cfg_fp_v1"  # pricing/accounts 变更指纹 (vault_meta), 变更即自动重算派生列


# ---------------------------------------------------------------- 基础工具

def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def _num(v, default=0):
    """int/float 宽容转换 (bool 排除, 字符串数字接受, None/坏形状回默认)。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return v
    try:
        return int(str(v).strip() or default)
    except (ValueError, TypeError):
        return default


def ts_to_epoch(v):
    """ISO (Z 后缀/偏移) 或 epoch 数值 → epoch 秒; 解析失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def epoch_to_iso(epoch):
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def local_day(epoch):
    return dt.datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 单价表

# 内置公开列表价近似口径 (2026-11 整理, $/Mtok; cache_read 取各家公开折扣档)。
# 定位是"估算可覆盖": 订阅制/未公开型号 (glm-5*, gpt-6* 等) 刻意不收录 —
# 未知单价诚实返回 NULL, 用户以 vault/pricing.json 补充, 绝不编造精确价格。
DEFAULT_PRICING = {
    # Anthropic (cache 读 0.1x 输入, 写 1.25x 输入)
    "claude-opus-4*":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-3*":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4*":   {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-7-sonnet*": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-sonnet*": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4*":    {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "claude-3-5-haiku*":  {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # OpenAI (cached input 折扣档)
    "gpt-5-mini*": {"input": 0.25, "output": 2.0, "cache_read": 0.025},
    "gpt-5-nano*": {"input": 0.05, "output": 0.40, "cache_read": 0.005},
    "gpt-5*":      {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "o3*":         {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "o4-mini*":    {"input": 1.10, "output": 4.40, "cache_read": 0.275},
    "gpt-4.1*":    {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "gpt-4o*":     {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    # Google Gemini
    "gemini-3-pro*":     {"input": 2.0, "output": 12.0, "cache_read": 0.50},
    "gemini-2.5-pro*":   {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-2.5-flash*": {"input": 0.30, "output": 2.50, "cache_read": 0.075},
    # 其他主流 (近似列表价)
    "glm-4*":             {"input": 0.60, "output": 2.20},
    "deepseek-chat*":     {"input": 0.27, "output": 1.10, "cache_read": 0.07},
    "deepseek-v3*":       {"input": 0.27, "output": 1.10, "cache_read": 0.07},
    "deepseek-reasoner*": {"input": 0.55, "output": 2.19, "cache_read": 0.14},
    "deepseek-r1*":       {"input": 0.55, "output": 2.19, "cache_read": 0.14},
    "kimi-k2*":           {"input": 0.60, "output": 2.50},
}


def _num_ok(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _load_price_file(path: Path) -> dict:
    """{"模型": {"input":x,"output":y,...}} → 校验后的价目 dict; 坏形状逐条丢弃不炸。"""
    try:
        pj = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, AttributeError, OSError):
        return {}
    if not isinstance(pj, dict):
        return {}
    out = {}
    for m, v in pj.items():
        if isinstance(v, dict) and _num_ok(v.get("input")) and _num_ok(v.get("output")):
            rule = {"input": float(v["input"]), "output": float(v["output"])}
            if _num_ok(v.get("cache_read")):
                rule["cache_read"] = float(v["cache_read"])
            if _num_ok(v.get("cache_write")):
                rule["cache_write"] = float(v["cache_write"])
            out[str(m)] = rule
    return out


def load_pricing(vault: Path) -> dict:
    """内置表 < vault/prices.json < vault/pricing.json (后者逐条覆盖前者)。"""
    pricing = dict(DEFAULT_PRICING)
    for name in ("prices.json", "pricing.json"):
        p = vault / name
        if p.exists():
            pricing.update(_load_price_file(p))
    return pricing


def price_for(pricing: dict, model):
    """精确键优先, 再按最长通配模式匹配 (gpt-5-mini* 优先于 gpt-5*); 无命中 → (None, None)。"""
    m = str(model or "").strip().lower()
    if not m:
        return None, None
    if m in pricing:
        return pricing[m], m
    best = None
    for pat in pricing:
        p = pat.lower()
        if "*" in p or "?" in p or "[" in p:
            if fnmatch.fnmatchcase(m, p) and (best is None or len(p) > len(best)):
                best = p
    return (pricing[best], best) if best else (None, None)


def cost_of(rule, tin, tout, cr, cw):
    """规范化口径计价: 未缓存入×in + 出×out + 读×cr价(缺省按 in) + 写×cw价(缺省按 in)。"""
    if not rule:
        return None
    ci = _num(rule.get("cache_read"), rule["input"])
    cwid = _num(rule.get("cache_write"), rule["input"])
    return (tin * float(rule["input"]) + tout * float(rule["output"])
            + cr * float(ci) + cw * float(cwid)) / 1e6


# ---------------------------------------------------------------- 账号与额度池

def load_accounts(vault: Path) -> dict:
    """vault/accounts.json; 缺失/坏 JSON → 空 profiles (纯消耗模式, 功能不受影响)。"""
    p = vault / "accounts.json"
    if not p.exists():
        return {"version": 1, "profiles": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"version": 1, "profiles": []}
    if not isinstance(d, dict) or not isinstance(d.get("profiles"), list):
        return {"version": 1, "profiles": []}
    return d


def resolve_account(accounts: dict, source, client, model):
    """首条命中 profile 生效; 全不命中 → None (展示层以 client:<name> 伪账号分组)。"""
    m = str(model or "").lower()
    for prof in accounts.get("profiles", []):
        if not isinstance(prof, dict) or not prof.get("name"):
            continue
        mt = prof.get("match") or {}
        if not isinstance(mt, dict):
            mt = {}
        srcs = mt.get("source") or []
        if srcs and source not in srcs:
            continue
        cls = mt.get("client") or []
        if cls and (client or "") not in cls:
            continue
        pfs = mt.get("model_prefix") or []
        if pfs and not any(fnmatch.fnmatchcase(m, str(p).lower()) for p in pfs):
            continue
        return str(prof["name"])
    return None


# ---------------------------------------------------------------- 采集器 (只读)

def _file_sig(path: Path, with_wal: bool):
    """(mtime, size[, wal_mtime, wal_size]) — antigravity 的 .db 需把 .db-wal 一并纳入水位,
    否则 WAL 未 checkpoint 时活跃会话漏采。"""
    st = path.stat()
    mtime, size = st.st_mtime, st.st_size
    if with_wal:
        wal = Path(str(path) + "-wal")
        if wal.exists():
            wst = wal.stat()
            return mtime, size, wst.st_mtime, wst.st_size
        return mtime, size, 0.0, 0
    return mtime, size


def _wm_changed(conn, source: str, path: Path, sig: tuple) -> bool:
    try:
        row = conn.execute("SELECT mtime,size,wal_mtime,wal_size FROM usage_sync_state"
                           " WHERE source=? AND path=?", (source, str(path))).fetchone()
    except sqlite3.OperationalError:
        return True
    if not row:
        return True
    cur = (row["mtime"] or 0, row["size"] or 0, row["wal_mtime"] or 0, row["wal_size"] or 0)
    want = (sig + (0.0, 0))[:4]
    return cur != want


def _wm_update(conn, source: str, path: Path, sig: tuple):
    s2 = (sig + (0.0, 0, 0))[:4]
    conn.execute("INSERT INTO usage_sync_state(source,path,mtime,size,wal_mtime,wal_size,synced_at)"
                 " VALUES(?,?,?,?,?,?,?)"
                 " ON CONFLICT(source,path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,"
                 " wal_mtime=excluded.wal_mtime, wal_size=excluded.wal_size,"
                 " synced_at=excluded.synced_at",
                 (source, str(path), s2[0], s2[1], s2[2], s2[3], cv.now_iso()))


def collect_claude(base_dir=None, skip_fn=None):
    """~/.claude/projects/**/*.jsonl — assistant 行 message.usage (Claude 语义: input 不含 cache)。
    同文件内按 message.id (回退 requestId → uuid) 去重取 output_tokens 最大的一条:
    同一次请求的流式多内容块各自带累计 usage, 逐行入账会重复计费。
    skip_fn(path, sig) 为真时该文件未变更, 不读盘直接跳过 (水位照记)。"""
    root = Path(base_dir) if base_dir else cv.HOME / ".claude" / "projects"
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.rglob("*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        groups = {}
        last_cwd = None
        try:
            for obj in cv.iter_jsonl(f):
                if obj.get("type") != "assistant":
                    if obj.get("cwd"):
                        last_cwd = obj.get("cwd")
                    continue
                msg = obj.get("message") or {}
                u = msg.get("usage") or {}
                if not isinstance(u, dict) or ("input_tokens" not in u and "output_tokens" not in u):
                    continue
                if obj.get("cwd"):
                    last_cwd = obj.get("cwd")
                sid = obj.get("sessionId") or f.stem
                gkey = msg.get("id") or obj.get("requestId") or obj.get("uuid") or ""
                if not gkey:
                    continue
                tout = int(_num(u.get("output_tokens")))
                prev = groups.get(gkey)
                if prev is not None and tout <= prev["tokens_out"]:
                    continue  # 同 message.id 的旧分块: 只保留 output 最大的一条
                groups[gkey] = {
                    "event_key": _sha(f"claude|{sid}|{gkey}"), "source": "claude",
                    "client": "claude-code", "model": msg.get("model") or None,
                    "project": last_cwd, "session_ref": sid,
                    "occurred_at": ts_to_epoch(obj.get("timestamp")) or 0.0,
                    "tokens_in": max(0, int(_num(u.get("input_tokens")))), "tokens_out": tout,
                    "reasoning_tokens": 0,
                    "cache_read": max(0, int(_num(u.get("cache_read_input_tokens")))),
                    "cache_write": max(0, int(_num(u.get("cache_creation_input_tokens")))),
                    "is_estimated": 0,
                    "raw": json.dumps({"requestId": obj.get("requestId"),
                                       "message_id": msg.get("id"), "cwd": last_cwd,
                                       "sidechain": bool(obj.get("isSidechain"))},
                                      ensure_ascii=False)}
        except (OSError, ValueError):
            continue  # 半截写入/编码坏行: 跳过该文件, 水位未推下轮重试
        events.extend(groups.values())
        marks.append((f, sig))
    return events, marks


def collect_codex(base_dir=None, skip_fn=None):
    """~/.codex/sessions/**/rollout-*.jsonl — event_msg/token_count 事件 (OpenAI 语义: input 含 cache)。
    顺序跟踪 prev_total, 仅 total_token_usage 真增长时取增量 (心跳重复不双计)。
    skip_fn(path, sig) 为真时该文件未变更, 不读盘直接跳过。"""
    root = Path(base_dir) if base_dir else cv.CODEX_SESSIONS
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.rglob("rollout-*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        sid, cwd, model = None, None, None
        prev = None  # (in, out, cached, cw, reason) 上次累计
        try:
            for obj in cv.iter_jsonl(f):
                t, payload = obj.get("type"), obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if t == "session_meta":
                    sid = payload.get("id") or payload.get("session_id") or sid
                    cwd = payload.get("cwd") or cwd
                elif t == "turn_context":
                    model = payload.get("model") or model
                    cwd = payload.get("cwd") or cwd
                elif t == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    tot = info.get("total_token_usage")
                    if not isinstance(tot, dict):
                        continue
                    settings = payload.get("thread_settings") or {}
                    if isinstance(settings, dict) and settings.get("model"):
                        model = settings["model"] or model
                    ti = int(_num(tot.get("input_tokens")))
                    to = int(_num(tot.get("output_tokens")))
                    tc = int(_num(tot.get("cached_input_tokens")))
                    tcw = int(_num(tot.get("cache_write_input_tokens")))
                    tr = int(_num(tot.get("reasoning_output_tokens")))
                    cur = (ti, to, tc)
                    if prev is not None and cur <= prev[0:3]:
                        continue  # 心跳/回退: 累计未真增长, 不出事件
                    base = prev if prev is not None else (0, 0, 0, 0, 0)
                    d_in, d_out = ti - base[0], to - base[1]
                    d_cached, d_cw, d_reason = tc - base[2], tcw - base[3], tr - base[4]
                    prev = (ti, to, tc, tcw, tr)
                    if d_in <= 0 and d_out <= 0:
                        continue
                    sid_eff = sid or f.stem
                    cw = max(0, d_cw)
                    events.append({
                        "event_key": _sha(f"codex|{sid_eff}|{ti}:{to}:{tc}"),
                        "source": "codex", "client": "codex-cli", "model": model or None,
                        "project": cwd, "session_ref": sid_eff,
                        "occurred_at": ts_to_epoch(obj.get("timestamp")) or 0.0,
                        "tokens_in": max(0, d_in - max(0, d_cached) - cw),  # 规范化: 扣除缓存
                        "tokens_out": max(0, d_out), "reasoning_tokens": max(0, d_reason),
                        "cache_read": max(0, d_cached), "cache_write": cw,
                        "is_estimated": 0,
                        "raw": json.dumps({"thread_id": sid, "model": model},
                                          ensure_ascii=False)})
        except (OSError, ValueError):
            continue
        marks.append((f, sig))
    return events, marks


def collect_zcode(base_dir=None, skip_fn=None):
    """~/.zcode/cli/rollout/model-io-*.jsonl — 每行一次模型调用 (requestId 幂等)。
    usage 双格式: camelCase (inputTokens 含缓存, OpenAI 语义) / snake_case (Anthropic 语义),
    分别按各自口径规范化, 结果跨格式一致。skip_fn(path, sig) 为真时不读盘跳过。"""
    root = Path(base_dir) if base_dir else cv.HOME / ".zcode" / "cli" / "rollout"
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.glob("model-io-*.jsonl")):
        try:
            sig = _file_sig(f, with_wal=False)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        stem_session = f.stem.replace("model-io-", "")
        try:
            for obj in cv.iter_jsonl(f):
                rid = obj.get("requestId")
                if not rid:
                    continue
                resp = obj.get("response") or {}
                u = resp.get("usage") or {}
                if not isinstance(u, dict):
                    continue
                mo = obj.get("model") or {}
                model = (mo.get("modelId") if isinstance(mo, dict) else None) \
                    or resp.get("modelId") or None
                if "inputTokens" in u:  # camelCase 聚合: input 已含 cache (OpenAI 语义)
                    cr = max(0, int(_num(u.get("cacheReadTokens"))))
                    cw = max(0, int(_num(u.get("cacheWriteTokens"))))
                    tin = max(0, int(_num(u.get("inputTokens"))) - cr - cw)
                    tout = int(_num(u.get("outputTokens")))
                elif "input_tokens" in u:  # snake_case 原始体: Claude 语义, input 不含 cache
                    tin = max(0, int(_num(u.get("input_tokens"))))
                    tout = int(_num(u.get("output_tokens")))
                    cr = max(0, int(_num(u.get("cache_read_input_tokens"))))
                    cw = max(0, int(_num(u.get("cache_creation_input_tokens"))))
                else:
                    continue
                events.append({
                    "event_key": _sha(f"zcode|{rid}|{_num(obj.get('attempt'), 0)}"),
                    "source": "zcode", "client": "zcode", "model": model,
                    "project": None, "session_ref": stem_session,
                    "occurred_at": ts_to_epoch(obj.get("completedAt")) or 0.0,
                    "tokens_in": tin, "tokens_out": tout, "reasoning_tokens": 0,
                    "cache_read": cr, "cache_write": cw, "is_estimated": 0,
                    "raw": json.dumps({"requestId": rid,
                                       "provider": mo.get("providerId")
                                       if isinstance(mo, dict) else None,
                                       "duration_ms": obj.get("durationMs")},
                                      ensure_ascii=False)})
        except (OSError, ValueError):
            continue
        marks.append((f, sig))
    return events, marks


_AGV_TS_COLS = ("ts", "timestamp", "created_at", "time", "event_ts")
# 内容 blob 列 (按优先级): 真实 schema (2026-11 实测) steps(step_payload/metadata, 无时间戳列);
# "data" 为旧版假设布局, 保留兼容
_AGV_CONTENT_COLS = ("step_payload", "data")


def collect_antigravity(base_dir=None, skip_fn=None):
    """~/.gemini/antigravity/conversations/<uuid>.db (SQLite, 内容为 protobuf blob)。
    无精确 token — 按 steps 内容列字节体积估算 (÷4), 一律 is_estimated=1;
    无逐轮时间戳列 → 会话级单事件 (occurred_at=db mtime), upsert 语义 (文件变大时刷新估算);
    水位含 .db-wal (修正4); skip_fn(path, sig) 为真时不读盘跳过。"""
    root = Path(base_dir) if base_dir else cv.HOME / ".gemini" / "antigravity" / "conversations"
    if not root.is_dir():
        return [], []
    events, marks = [], []
    for f in sorted(root.glob("*.db")):
        try:
            sig = _file_sig(f, with_wal=True)
        except OSError:
            continue
        if skip_fn and skip_fn(f, sig):
            marks.append((f, sig))
            continue
        conv_id = f.stem
        try:
            adb = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                cols = [r[1] for r in adb.execute("PRAGMA table_info(steps)").fetchall()]
                content_col = next((c for c in _AGV_CONTENT_COLS if c in cols), None)
                if not cols or content_col is None:
                    continue  # 无 steps 内容列: 非会话库, 跳过
                ts_col = next((c for c in _AGV_TS_COLS if c in cols), None)
                evs = []
                if ts_col:
                    for idx, ts, blob_n in adb.execute(
                            f"SELECT idx, {ts_col}, COALESCE(LENGTH({content_col}),0)"
                            f" FROM steps ORDER BY idx"):
                        evs.append((idx, ts_to_epoch(ts) or sig[0], int(blob_n)))
                else:
                    row = adb.execute(f"SELECT COALESCE(SUM(LENGTH({content_col})),0)"
                                      " FROM steps").fetchone()
                    blob_total = int(row[0] or 0)
                    if blob_total:
                        evs.append(("conv", sig[0], blob_total))  # 无逐轮时间戳: 回落 db mtime
                for idx, ep, blob_n in evs:
                    e = {
                        "event_key": _sha(f"agv|{conv_id}|{idx}"),
                        "source": "antigravity", "client": "antigravity", "model": None,
                        "project": None, "session_ref": conv_id, "occurred_at": ep,
                        "tokens_in": max(1, blob_n // 4), "tokens_out": 0,
                        "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                        "is_estimated": 1,
                        "raw": json.dumps({"step_idx": str(idx),
                                           "content_col": content_col,
                                           "estimate_basis": "blob_bytes/4"},
                                          ensure_ascii=False)}
                    if idx == "conv":
                        e["upsert"] = True  # 会话级估算: 文件变大时刷新体积/时间戳, 不被 OR IGNORE 冻结
                    events.append(e)
            finally:
                adb.close()
        except sqlite3.Error:
            continue  # 活跃库锁/损坏: 跳过, 水位未推下轮重试
        marks.append((f, sig))
    return events, marks


COLLECTORS = {"claude": collect_claude, "codex": collect_codex,
              "zcode": collect_zcode, "antigravity": collect_antigravity}


def sync_from_agm(vault: Path, url: str, conn=None, pricing=None, accounts=None):
    """可选外部管理器快照 (如本机 Antigravity-Manager): 尽力而为, 失败静默跳过, 绝不强依赖。
    认形状: {"events":[{id,account,model,occurred_at,tokens_in,tokens_out,cache_read,cache_write}]}。
    pricing/accounts 缺省时按 vault 当前配置加载 — agm 事件同样参与账号归因与费用估算。"""
    try:
        with urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"skipped": f"agm 不可达/超时: {e}"}
    evs = data.get("events") if isinstance(data, dict) else None
    if not isinstance(evs, list):
        return {"skipped": "agm 返回形状未识别 (缺 events 数组)"}
    if pricing is None:
        pricing = load_pricing(vault)
    if accounts is None:
        accounts = load_accounts(vault)
    own_conn = conn is None
    if own_conn:
        conn = cv.db(vault)
        ensure_schema(conn)
    n = 0
    for e in evs:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or _sha(json.dumps(e, sort_keys=True, ensure_ascii=False,
                                                 default=str)))
        ep = ts_to_epoch(e.get("occurred_at") or e.get("timestamp"))
        if ep is None:
            continue
        ev = {"event_key": _sha(f"agm|{eid}"), "source": "agm",
              "client": e.get("client") or "agm", "model": e.get("model"),
              "project": e.get("project"), "session_ref": e.get("session_ref"),
              "occurred_at": ep,
              "tokens_in": max(0, int(_num(e.get("tokens_in")))),
              "tokens_out": max(0, int(_num(e.get("tokens_out")))),
              "reasoning_tokens": max(0, int(_num(e.get("reasoning_tokens")))),
              "cache_read": max(0, int(_num(e.get("cache_read")))),
              "cache_write": max(0, int(_num(e.get("cache_write")))),
              "is_estimated": 1 if e.get("is_estimated") else 0,
              "raw": json.dumps({"agm_url": url}, ensure_ascii=False)}
        n += _insert_events(conn, [ev], pricing, accounts)[0]
    if own_conn:
        conn.commit()
        conn.close()
    return {"ingested": n}


# ---------------------------------------------------------------- 入账与派生列

def _insert_events(conn, events: list, pricing: dict, accounts: dict) -> tuple:
    """event_key 唯一入账 + 派生列 (account/cost/price_known) 现算。
    默认 INSERT OR IGNORE (幂等); e["upsert"]=True 的会话级估算事件改 ON CONFLICT DO UPDATE —
    文件变大时刷新体积/时间戳/费用, 不被 OR IGNORE 冻结在首次估算值。"""
    inserted = 0
    for e in events:
        rule, _ = price_for(pricing, e.get("model"))
        cost = cost_of(rule, e["tokens_in"], e["tokens_out"], e["cache_read"], e["cache_write"])
        acct = resolve_account(accounts, e.get("source"), e.get("client"), e.get("model"))
        args = (e["event_key"], e["source"], e.get("client"), acct, e.get("model"),
                e.get("project"), e.get("session_ref"), e["occurred_at"], e["tokens_in"],
                e["tokens_out"], e.get("reasoning_tokens") or 0, e["cache_read"],
                e["cache_write"], cost, 1 if e.get("is_estimated") else 0,
                1 if rule else 0, e.get("raw"))
        if e.get("upsert"):
            cur = conn.execute(
                "INSERT INTO usage_events(event_key,source,client,account,model,project,"
                "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,"
                "cache_write,cost_est_usd,is_estimated,price_known,raw)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_key) DO UPDATE SET occurred_at=excluded.occurred_at,"
                " tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out,"
                " cost_est_usd=excluded.cost_est_usd, raw=excluded.raw", args)
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO usage_events(event_key,source,client,account,model,project,"
                "session_ref,occurred_at,tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,"
                "cost_est_usd,is_estimated,price_known,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                args)
        if cur.rowcount:
            inserted += 1
    return inserted, len(events) - inserted


def recompute_derived(conn, pricing: dict, accounts: dict) -> int:
    """按当前 pricing/accounts 重算全部存量事件的 account/cost_est_usd/price_known (修正3)。"""
    rows = conn.execute("SELECT id,source,client,model,tokens_in,tokens_out,cache_read,cache_write"
                        " FROM usage_events").fetchall()
    n = 0
    for r in rows:
        rule, _ = price_for(pricing, r["model"])
        cost = cost_of(rule, r["tokens_in"], r["tokens_out"], r["cache_read"], r["cache_write"])
        acct = resolve_account(accounts, r["source"], r["client"], r["model"])
        conn.execute("UPDATE usage_events SET account=?, cost_est_usd=?, price_known=? WHERE id=?",
                     (acct, cost, 1 if rule else 0, r["id"]))
        n += 1
    return n


def _cfg_fingerprint(vault: Path) -> str:
    parts = []
    for name in ("pricing.json", "prices.json", "accounts.json"):
        p = vault / name
        if p.exists():
            try:
                st = p.stat()
                parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                pass
    return "|".join(parts) or "none"


def _maybe_recompute(conn, vault: Path, pricing: dict, accounts: dict, force: bool) -> int:
    fp = _cfg_fingerprint(vault)
    try:
        row = conn.execute("SELECT value FROM vault_meta WHERE key=?", (CFG_FP_KEY,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not force and row and row[0] == fp:
        return 0
    n = recompute_derived(conn, pricing, accounts)
    conn.execute("INSERT INTO vault_meta(key,value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CFG_FP_KEY, fp))
    return n


def ensure_schema(conn):
    conn.executescript(USAGE_TABLE)


def cmd_ledger_sync(args):
    """cv ledger sync — 多端直采入账 (幂等; --sources 过滤; --from-agm 可选; --recompute 重算)。"""
    vault = cv.vault_dir(args)
    if not (vault / "vault.db").exists():
        raise SystemExit("vault 尚无数据库 — 先运行 cv init 或 cv sync 建库 (usage_events 随库建表)")
    conn = cv.db(vault)
    try:
        ensure_schema(conn)
        try:  # vault_meta 同库兜底 (老库可能缺)
            conn.executescript(cv.SCHEMA)
        except sqlite3.OperationalError:
            pass
        pricing = load_pricing(vault)
        accounts = load_accounts(vault)
        want = getattr(args, "sources", None) or "all"
        srcs = list(COLLECTORS) if want == "all" else \
            [s.strip() for s in str(want).split(",") if s.strip()]
        bad = [s for s in srcs if s not in COLLECTORS]
        if bad:
            raise SystemExit(f"未知来源: {','.join(bad)} (可选: all|claude|codex|zcode|antigravity)")
        total_new = total_dup = 0
        for src in srcs:
            changed = []  # 真变更文件集: skip_fn 判未变更 → 采集器不读盘 (水位跳过)

            def _skip(p, s, _src=src):
                if _wm_changed(conn, _src, p, s):
                    changed.append((p, s))
                    return False
                return True

            events, marks = COLLECTORS[src](skip_fn=_skip)
            ins, dup = _insert_events(conn, events, pricing, accounts)
            for f, sig in marks:
                _wm_update(conn, src, f, sig)
            total_new += ins
            total_dup += dup
            print(f"  {src:<11} 新 {ins:>5} · 幂等跳过 {dup:>5} · 文件 {len(marks)}"
                  f" (变更 {len(changed)} · 跳过 {len(marks) - len(changed)})")
        agm = getattr(args, "from_agm", None)
        agm_res = None
        if agm:
            agm_res = sync_from_agm(vault, agm, conn=conn, pricing=pricing, accounts=accounts)
        re_n = _maybe_recompute(conn, vault, pricing, accounts,
                                force=bool(getattr(args, "recompute", False)))
        conn.commit()
        print(f"账本同步完成: 新入账 {total_new} 条, 幂等跳过 {total_dup} 条"
              + (f", 配置变更重算 {re_n} 条" if re_n else "")
              + (f", agm: {json.dumps(agm_res, ensure_ascii=False)}" if agm_res else ""))
    finally:
        conn.close()


# ---------------------------------------------------------------- 统计引擎

def _zero_totals() -> dict:
    return {"events": 0, "tokens_in": 0, "tokens_out": 0, "reasoning_tokens": 0,
            "cache_read": 0, "cache_write": 0, "total_input": 0, "total_output": 0,
            "cost_est_usd": 0.0, "cost_known_events": 0}


def _round_totals(t: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}


def _add_total(t: dict, r) -> dict:
    tin = int(r["tokens_in"] or 0)
    tout = int(r["tokens_out"] or 0)
    cr = int(r["cache_read"] or 0)
    cw = int(r["cache_write"] or 0)
    t["events"] += 1
    t["tokens_in"] += tin
    t["tokens_out"] += tout
    t["reasoning_tokens"] += int(r["reasoning_tokens"] or 0)
    t["cache_read"] += cr
    t["cache_write"] += cw
    t["total_input"] += tin + cr + cw
    t["total_output"] += tout
    if r["cost_est_usd"] is not None:
        t["cost_est_usd"] += float(r["cost_est_usd"])
        t["cost_known_events"] += 1
    return t


def _row_weight(r, weight="total_tokens"):
    if weight == "usd":
        return float(r["cost_est_usd"] or 0)
    return (int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0)
            + int(r["cache_read"] or 0) + int(r["cache_write"] or 0))


def _agg_rows(rows, key_fn):
    agg, order = {}, []
    for r in rows:
        name = key_fn(r) or "(未知)"
        if name not in agg:
            agg[name] = _zero_totals()
            agg[name]["estimated_events"] = 0
            order.append(name)
        a = agg[name]
        _add_total(a, r)
        if r["is_estimated"]:
            a["estimated_events"] += 1
    out = []
    for name in order:
        a = agg[name]
        out.append({"name": name, **_round_totals(a)})
    out.sort(key=lambda d: -(d["total_input"] + d["total_output"]))
    return out


def _account_of(r) -> str:
    return r["account"] or f"client:{r['client'] or r['source']}"


def _account_filter_sql(account: str):
    """account 过滤: 具名账号精确匹配; client:x 伪账号匹配 account IS NULL 且 client=x。"""
    if account and account.startswith("client:"):
        return (" AND account IS NULL AND COALESCE(client, source)=?",
                (account[len("client:"):],))
    if account:
        return " AND account=?", (account,)
    return "", ()


def _period_start(kind: str, now: float) -> float:
    if kind == "monthly":
        d = dt.datetime.fromtimestamp(now).astimezone().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()
    return now - 7 * 86400  # 7d 滚动


def _eta(evs, limit, threshold: float, now: float, weight="total_tokens"):
    """恢复 ETA: 最早一批消耗滑出 5h 窗口、剩余恢复到 threshold×limit 的时刻。
    已达标 → now; limit 空 → None; 理论不可达 → None。"""
    if not limit:
        return None
    used = sum(_row_weight(r, weight) for r in evs)
    if used <= (1.0 - threshold) * limit + 1e-9:
        return epoch_to_iso(now)
    target = used - (1.0 - threshold) * limit  # 需滑出的量
    freed = 0.0
    for r in evs:
        freed += _row_weight(r, weight)
        if freed >= target - 1e-9:
            slide = (r["occurred_at"] or now) + WINDOW_5H
            return epoch_to_iso(slide if slide > now else now)
    return None


def _fiveh_accounts(conn, now: float, accounts: dict, account: str = None):
    """5h 滚动窗口: 按账号 (含隐式 client 分组) 汇已用/占比/恢复 ETA。"""
    since = now - WINDOW_5H
    cond, cargs = _account_filter_sql(account)
    rows = conn.execute(
        "SELECT account, client, source, model, occurred_at, tokens_in, tokens_out,"
        " cache_read, cache_write, cost_est_usd, is_estimated"
        f" FROM usage_events WHERE occurred_at > ?{cond}", (since,) + cargs).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(_account_of(r), []).append(r)
    prof_by_name = {str(p.get("name")): p for p in accounts.get("profiles", [])
                    if isinstance(p, dict) and p.get("name")}
    out = []
    for key in sorted(groups, key=lambda k: -sum(_row_weight(x) for x in groups[k])):
        evs = sorted(groups[key], key=lambda r: r["occurred_at"] or 0)
        prof = prof_by_name.get(key) or {}
        w5 = prof.get("window_5h") or {}
        lim_tok, lim_usd = w5.get("tokens"), w5.get("usd")
        weight = w5.get("weight") or ("usd" if (lim_usd and not lim_tok) else "total_tokens")
        limit = float(lim_usd) if weight == "usd" and lim_usd else \
            (float(lim_tok) if lim_tok else None)
        used_w = sum(_row_weight(x, weight) for x in evs)
        pct = (used_w / limit) if limit else None
        etas = {}
        for th, label in ((0.5, "50%"), (0.8, "80%"), (1.0, "100%")):
            etas[label] = _eta(evs, limit, th, now, weight) if limit else None
        out.append({"account": key, "client": evs[0]["client"] or evs[0]["source"],
                    "events": len(evs), "used": int(sum(_row_weight(x) for x in evs)),
                    "weight": weight, "used_weighted": round(used_w, 1), "limit": limit,
                    "pct": round(pct, 4) if pct is not None else None,
                    "eta": etas, "configured": key in prof_by_name})
    return out


def _runway(conn, now: float, accounts: dict):
    """7d/月跑道: 按近 24h 速率外推距离周期上限的剩余天数 (未配置 → 空表)。
    速率与已用均按账号隔离 (account 精确匹配 profile 名), 多账号互不串账。"""
    out = []
    profs = [p for p in accounts.get("profiles", []) if isinstance(p, dict) and p.get("name")]
    if not profs:
        return out
    for prof in profs:
        period = prof.get("period") or {}
        kind = period.get("kind") or "7d"
        cap_tok, cap_usd = _num(period.get("tokens"), 0), _num(period.get("usd"), 0.0)
        if not cap_tok and not cap_usd:
            continue
        name = str(prof["name"])
        r24 = conn.execute(
            "SELECT tokens_in, tokens_out, cache_read, cache_write, cost_est_usd"
            " FROM usage_events WHERE occurred_at > ? AND account=?",
            (now - 86400, name)).fetchall()
        rate_tok = sum(int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0)
                       + int(r["cache_read"] or 0) + int(r["cache_write"] or 0) for r in r24)
        rate_usd = sum(float(r["cost_est_usd"] or 0) for r in r24)
        start = _period_start(kind, now)
        rows = conn.execute(
            "SELECT tokens_in, tokens_out, cache_read, cache_write, cost_est_usd"
            " FROM usage_events WHERE occurred_at >= ? AND account=?",
            (start, name)).fetchall()
        used_tok = sum(int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0)
                       + int(r["cache_read"] or 0) + int(r["cache_write"] or 0) for r in rows)
        used_usd = sum(float(r["cost_est_usd"] or 0) for r in rows)
        entry = {"account": name, "kind": kind,
                 "used_tokens": used_tok, "used_usd": round(used_usd, 4),
                 "rate_tokens_per_day": rate_tok, "rate_usd_per_day": round(rate_usd, 4)}
        if cap_tok:
            rem = max(0.0, float(cap_tok) - used_tok)
            entry.update({"cap": float(cap_tok), "metric": "tokens", "remaining": rem,
                          "days_left": round(rem / rate_tok, 2) if rate_tok > 0 else None})
        else:
            rem = max(0.0, float(cap_usd) - used_usd)
            entry.update({"cap": float(cap_usd), "metric": "usd", "remaining": round(rem, 4),
                          "days_left": round(rem / rate_usd, 2) if rate_usd > 0 else None})
        out.append(entry)
    return out


def usage_payload(vault: Path, window: str = "5h", account: str = None, now: float = None) -> dict:
    """统一统计出口: CLI quota / HTTP /ledger/usage / MCP vault_stats 共用 (冷热口径一致)。
    无库/无表/空账本 → 全零诚实空态, 读路径绝不建库建表毒化冷路径。"""
    now = float(now if now is not None else time.time())
    hours = WINDOW_CHOICES.get(window, WINDOW_CHOICES["5h"])
    payload = {"now": now, "generated_at": epoch_to_iso(now), "window": window,
               "accounts_configured": False, "events_total": 0,
               "totals": _round_totals(_zero_totals()),
               "by_model": [], "by_project": [], "by_account": [], "by_client": [],
               "by_day": [], "cache_hit_rate": None, "cache_save_est_usd": 0.0,
               "precision": {"exact_events": 0, "estimated_events": 0, "estimated_share": 0.0},
               "fiveh": {"totals": _round_totals(_zero_totals()), "accounts": []},
               "runway": [], "sources": {}}
    if not (Path(vault) / "vault.db").exists():
        return payload
    conn = cv.db(vault)
    try:
        try:
            src_rows = conn.execute(
                "SELECT source, COUNT(*) n FROM usage_events GROUP BY source").fetchall()
        except sqlite3.OperationalError:
            return payload  # 老库无 usage_events 表: 诚实空态, 读路径不建表
        if not src_rows:
            return payload
        accounts = load_accounts(vault)
        payload["accounts_configured"] = bool(accounts.get("profiles"))
        payload["events_total"] = sum(int(r["n"]) for r in src_rows)
        payload["sources"] = {r["source"]: int(r["n"]) for r in src_rows}
        cond, cargs = _account_filter_sql(account)
        since = now - hours
        win = conn.execute(
            "SELECT source,client,account,model,project,occurred_at,tokens_in,tokens_out,"
            "reasoning_tokens,cache_read,cache_write,cost_est_usd,is_estimated"
            f" FROM usage_events WHERE occurred_at > ?{cond}", (since,) + cargs).fetchall()
        totals = _zero_totals()
        est_ev = 0
        cache_save = 0.0
        pricing = load_pricing(vault)
        for r in win:
            _add_total(totals, r)
            if r["is_estimated"]:
                est_ev += 1
            # 缓存节省 = 读×(in价−读价) + 写×(in价−写价), 仅已知单价模型参与
            rule, _ = price_for(pricing, r["model"])
            if rule:
                cr, cw = int(r["cache_read"] or 0), int(r["cache_write"] or 0)
                cin = float(rule["input"])
                cache_save += (cr * (cin - _num(rule.get("cache_read"), cin))
                               + cw * (cin - _num(rule.get("cache_write"), cin))) / 1e6
        payload["totals"] = _round_totals(totals)
        denom = totals["total_input"]
        payload["cache_hit_rate"] = round(totals["cache_read"] / denom, 4) if denom else None
        payload["cache_save_est_usd"] = round(cache_save, 4)
        payload["precision"] = {
            "exact_events": len(win) - est_ev, "estimated_events": est_ev,
            "estimated_share": round(est_ev / len(win), 4) if win else 0.0}
        payload["by_model"] = _agg_rows(win, lambda r: r["model"])
        payload["by_project"] = _agg_rows(win, lambda r: r["project"] or "(未标注)")
        payload["by_account"] = _agg_rows(win, _account_of)
        payload["by_client"] = _agg_rows(win, lambda r: r["client"] or r["source"])
        days = {}
        for r in win:
            d = local_day(r["occurred_at"] or now)
            days.setdefault(d, _zero_totals())
            _add_total(days[d], r)
        payload["by_day"] = [{"day": d, "events": v["events"], "total_input": v["total_input"],
                              "total_output": v["total_output"],
                              "cost_est_usd": round(v["cost_est_usd"], 4)}
                             for d, v in sorted(days.items())]
        frows = conn.execute(
            "SELECT tokens_in,tokens_out,reasoning_tokens,cache_read,cache_write,cost_est_usd"
            f" FROM usage_events WHERE occurred_at > ?{cond}",
            (now - WINDOW_5H,) + cargs).fetchall()
        ft = _zero_totals()
        for r in frows:
            _add_total(ft, r)
        payload["fiveh"] = {"totals": _round_totals(ft),
                            "accounts": _fiveh_accounts(conn, now, accounts, account)}
        payload["runway"] = _runway(conn, now, accounts)
        return payload
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI: quota

def _bar(pct, width=18):
    if pct is None:
        return "░" * width
    n = max(0, min(width, int(round(pct * width))))
    return "█" * n + "░" * (width - n)


def _fmt_eta(iso):
    if not iso:
        return "—"
    try:
        delta = dt.datetime.fromisoformat(iso) - dt.datetime.now().astimezone()
    except ValueError:
        return iso
    s = int(delta.total_seconds())
    if s <= 0:
        return "已恢复"
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h{m:02d}m 后" if h else f"{m}m 后"


def fmt_usd(x):
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "-"


def cmd_ledger_quota(args):
    """cv ledger quota — 终端直印 5h 滚动窗口/恢复 ETA/窗口消耗/Top 模型与项目/跑道。"""
    vault = cv.vault_dir(args)
    window = getattr(args, "window", None) or "5h"
    account = getattr(args, "account", None)
    p = usage_payload(vault, window=window, account=account)
    if getattr(args, "json", False):
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return
    if not p["events_total"]:
        print("usage_events 为空 — 先运行: python tools/cv.py ledger sync (本地多端直采)")
        return
    t = p["totals"]
    print(f"== 本地多端账本 ({p['events_total']} 事件 · 来源 "
          f"{json.dumps(p['sources'], ensure_ascii=False)}) ==")
    print(f"  窗口 {window}: 总输入 {t['total_input']:,} (cache读 {t['cache_read']:,} · "
          f"命中率 {(p['cache_hit_rate'] or 0) * 100:.1f}%) · 输出 {t['total_output']:,} · "
          f"费用估算 {fmt_usd(t['cost_est_usd'])} (精确事件 "
          f"{(1 - p['precision']['estimated_share']) * 100:.0f}% / 缓存节省约 "
          f"{fmt_usd(p['cache_save_est_usd'])})")
    print("== 5 小时滚动窗口 ==")
    for a in p["fiveh"]["accounts"]:
        lim = f" / {a['limit']:,.0f}" if a["limit"] else " (未配置上限)"
        pct = f"{a['pct'] * 100:5.1f}%" if a["pct"] is not None else "     —"
        print(f"  {a['account']:<26} {_bar(a['pct'])} {pct}{lim}  已用 {a['used_weighted']:,.0f}"
              f" ({a['weight']})")
        if a["limit"] and a["eta"]:
            print(f"      恢复至 50%: {_fmt_eta(a['eta'].get('50%'))} · 80%: "
                  f"{_fmt_eta(a['eta'].get('80%'))} · 100%: {_fmt_eta(a['eta'].get('100%'))}")
    if not p["fiveh"]["accounts"]:
        print("  近 5h 无消耗")
    if p["runway"]:
        print("== 跑道 (按近 24h 速率外推) ==")
        for r in p["runway"]:
            dl = f"{r['days_left']:.1f} 天" if r["days_left"] is not None else "∞ (近 24h 零消耗)"
            cap = f"{r['cap']:,.0f} tok" if r["metric"] == "tokens" else fmt_usd(r["cap"])
            used = f"{r['used_tokens']:,.0f} tok" if r["metric"] == "tokens" else fmt_usd(r["used_usd"])
            print(f"  {r['account']:<22} [{r['kind']}] 上限 {cap} · 本期已用 {used} · 剩余跑道 {dl}")
    for title, key in (("Top 模型", "by_model"), ("Top 项目", "by_project")):
        rows = p[key][:5]
        if rows:
            print(f"== {title} (窗口 {window}) ==")
            for x in rows:
                est = " [含估算]" if x.get("estimated_events") else ""
                cost = f" · {fmt_usd(x['cost_est_usd'])}" if x["cost_est_usd"] else ""
                print(f"  {x['name']:<38} 入 {x['total_input']:>12,} 出 {x['total_output']:>9,}"
                      f"{cost}{est}")


# ---------------------------------------------------------------- MCP/HTTP 汇总 (数字与 payload 同源)

def usage_stats_summary(vault: Path) -> dict:
    """给 MCP vault_stats / HTTP /stats 的精简块 (口径与 usage_payload 一致)。"""
    p = usage_payload(vault, window="24h")
    t5 = (p.get("fiveh") or {}).get("totals") or {}
    return {
        "events_total": p["events_total"],
        "sources": p["sources"],
        "window_5h_total_input": t5.get("total_input", 0),
        "window_5h_tokens_out": t5.get("total_output", 0),
        "window_5h_cost_est_usd": t5.get("cost_est_usd", 0.0),
        "window_24h_total_input": p["totals"].get("total_input", 0),
        "window_24h_tokens_out": p["totals"].get("total_output", 0),
        "window_24h_cost_est_usd": p["totals"].get("cost_est_usd", 0.0),
        "cache_hit_rate": p["cache_hit_rate"],
        "cache_save_est_usd": p["cache_save_est_usd"],
        "estimated_share": p["precision"]["estimated_share"],
        "accounts_configured": p["accounts_configured"],
    }


if __name__ == "__main__":
    pass  # 经 cv.py ledger 子命令调用; 独立执行无意义
