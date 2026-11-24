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
import bisect
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen

# 同目录兄弟模块: HOME/CODEX_SESSIONS/db/SCHEMA/iter_jsonl/now_iso/vault_dir。
# 全部路径取自 cv.HOME / cv.CODEX_SESSIONS 的运行时属性访问 — 测试 monkeypatch cv.HOME 即全隔离。
import cv
from cv_core import fmt_usd

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
# 订阅制型号 (glm-5*/gpt-5.6*/gpt-6* 等) 按「公开 API 挂牌价等效折算」收录参考近似价 —
# UI 标注估算非账单; 精确价以 vault/pricing.json 覆盖 (逐条生效, 不需改代码)。
PRICING_REV = 5  # 内置表修订号: 纳入配置指纹 (_cfg_fingerprint), 表变更即自动重算存量事件
DEFAULT_PRICING = {
    # Anthropic (cache 读 0.1x 输入, 写 1.25x 输入; 含空格可读名与连字符 id)
    "claude-opus-4*":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-3*":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude opus*":       {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4*":   {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-7-sonnet*": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-sonnet*": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude sonnet*":     {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4*":    {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "claude-3-5-haiku*":  {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "claude haiku*":      {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # OpenAI (cached input 折扣档)
    "gpt-5-mini*": {"input": 0.25, "output": 2.0, "cache_read": 0.025},
    "gpt-5-nano*": {"input": 0.05, "output": 0.40, "cache_read": 0.005},
    "gpt-5.6*":    {"input": 2.50, "output": 16.0, "cache_read": 0.31},
    "gpt-5*":      {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "gpt-6*":      {"input": 4.00, "output": 20.0, "cache_read": 0.50},
    "gpt-oss*":    {"input": 0.50, "output": 2.0, "cache_read": 0.10},
    "o3*":         {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "o4-mini*":    {"input": 1.10, "output": 4.40, "cache_read": 0.275},
    "gpt-4.1*":    {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "gpt-4o*":     {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    # Google Gemini & Antigravity 模型池
    "gemini 4 hydrogen*": {"input": 2.00, "output": 12.0, "cache_read": 0.50},
    "gemini-4-hydrogen*": {"input": 2.00, "output": 12.0, "cache_read": 0.50},
    "gemini 4 argon*":    {"input": 2.00, "output": 12.0, "cache_read": 0.50},
    "gemini-4-argon*":    {"input": 2.00, "output": 12.0, "cache_read": 0.50},
    "gemini 4 carbon*":   {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-4-carbon*":   {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini 4 neon*":     {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-4-neon*":     {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini 4*":          {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-4*":          {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "orchestrator*":      {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini 3.8 flash*":  {"input": 0.50, "output": 3.0, "cache_read": 0.125},
    "gemini-3.8-flash*":  {"input": 0.50, "output": 3.0, "cache_read": 0.125},
    "gemini-3-pro*":      {"input": 2.0, "output": 12.0, "cache_read": 0.50},
    "gemini-3-flash*":    {"input": 0.50, "output": 3.0, "cache_read": 0.125},
    "gemini 3*flash*":    {"input": 0.50, "output": 3.0, "cache_read": 0.125},
    "gemini-3*":          {"input": 2.0, "output": 12.0, "cache_read": 0.50},
    "gemini-2.5-pro*":   {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-2.5-flash*": {"input": 0.30, "output": 2.50, "cache_read": 0.075},
    # Zhipu GLM (订阅制, 公开 API 挂牌价等效折算; flash 档按 ~1/5 输入近似)
    "glm-5.3-flash*": {"input": 0.20, "output": 0.90, "cache_read": 0.04},
    "glm-5.3*":       {"input": 1.00, "output": 3.50, "cache_read": 0.20},
    "glm-5*":         {"input": 0.60, "output": 2.20, "cache_read": 0.12},
    # 其他主流 (近似列表价)
    "glm-4*":             {"input": 0.60, "output": 2.20},
    "deepseek-chat*":     {"input": 0.27, "output": 1.10, "cache_read": 0.07},
    "deepseek-v4-flash*": {"input": 0.14, "output": 0.56, "cache_read": 0.028},
    "deepseek-flash*":    {"input": 0.14, "output": 0.56, "cache_read": 0.028},
    "deepseek-v4*":       {"input": 0.28, "output": 1.10, "cache_read": 0.028},
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

# 套餐预设 (官方/社区核实的「每 5 小时提示次数」口径; 一次提示可能包含多次模型调用,
# 与本账本"事件=模型调用"口径不同) — 仅作选择 token 上限时的参考基准, 不直接当限制用。
# 厂商实时额度优先级更高: 把 Coding Plan 用户 Token 放进 vault/plan_token.json 或环境变量
# CV_PLAN_TOKEN, quota/控制台即显示厂商返回的真实百分比与重置时间 (见 fetch_remote_quota)。
PLAN_PRESETS = {
    "glm-coding-lite": {"prompts_5h": 120, "prompts_week": 400,
                        "note": "GLM Coding Plan Lite (~120 次提示/5h, 周上限约 400 次)"},
    "glm-coding-pro": {"prompts_5h": 600, "note": "GLM Coding Plan Pro (~600 次提示/5h)"},
    "glm-coding-max": {"prompts_5h": 2400, "note": "GLM Coding Plan Max (~2400 次提示/5h)"},
    "claude-pro": {"prompts_5h": 45, "note": "Claude Pro (~45 条消息/5h)"},
    "claude-max5x": {"prompts_5h": 220, "note": "Claude Max 5x (~220 条消息/5h)"},
    "claude-max20x": {"prompts_5h": 880, "note": "Claude Max 20x (~880 条消息/5h)"},
}


def plan_presets_text() -> str:
    lines = ["套餐预设 (每 5 小时提示次数, 厂商公开/社区口径):"]
    for name, p in PLAN_PRESETS.items():
        lines.append(f"  {name:<18} {p.get('note', '')}")
    return "\n".join(lines)


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


# ------------------------------------------------- 厂商实时额度 (Antigravity-Manager 路线)

DEFAULT_PLAN_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
# 返回形状 (社区核实): {"success":true,"data":{"level":"PRO","limits":
#   [{"type":"TOKENS_LIMIT","percentage":42.0,"nextResetTime":"..."}]}}


def load_plan_token(vault: Path):
    """Coding Plan 用户 Token (非普通 API Key): 环境变量 CV_PLAN_TOKEN > vault/plan_token.json。"""
    tok = os.environ.get("CV_PLAN_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    p = Path(vault) / "plan_token.json"
    if p.exists():
        try:
            return (json.loads(p.read_text(encoding="utf-8")) or {}).get("token") or None
        except (ValueError, OSError, AttributeError):
            return None
    return None


def fetch_remote_quota(token: str, url: str = None, timeout: float = 4.0) -> dict:
    """拉厂商套餐真实额度 (尽力而为, 永不抛异常)。Authorization 先 Bearer 后裸值各试一次
    (社区脚本两种写法都有); 仅返回百分比与重置时间, Token 不落日志。"""
    url = url or os.environ.get("CV_PLAN_URL") or DEFAULT_PLAN_URL
    last_err = None
    for auth in (f"Bearer {token}", token):
        req = Request(url, headers={
            "Authorization": auth, "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
        if not (isinstance(data, dict) and data.get("success")):
            last_err = str((data.get("msg") if isinstance(data, dict) else None)
                           or "响应形状不符 (success=false)")
            continue
        d = data.get("data") or {}
        limits = []
        for it in (d.get("limits") or []):
            if not isinstance(it, dict):
                continue
            r_at = it.get("nextResetTime")
            if isinstance(r_at, (int, float)) and not isinstance(r_at, bool):
                # 智谱实测为数值 epoch (毫秒): 统一归一成 ISO, 前端/CLI 共用
                r_at = epoch_to_iso(r_at if r_at < 1e12 else r_at / 1000.0)
            limits.append({"type": str(it.get("type") or "?"),
                           "pct_used": round(_num(it.get("percentage"), 0.0), 2),
                           "reset_at": r_at})
        return {"ok": True, "plan": str(d.get("level") or "?"), "limits": limits,
                "fetched_at": epoch_to_iso(time.time())}
    return {"ok": False, "error": f"额度接口不可用: {last_err}"}


_REMOTE_Q = {"at": 0.0, "data": None}
_REMOTE_Q_LOCK = threading.Lock()
_REMOTE_Q_INFLIGHT = False


def _refresh_remote_quota_async(token: str) -> None:
    global _REMOTE_Q_INFLIGHT
    with _REMOTE_Q_LOCK:
        if _REMOTE_Q_INFLIGHT:
            return
        _REMOTE_Q_INFLIGHT = True

    def _bg():
        global _REMOTE_Q_INFLIGHT
        try:
            r = fetch_remote_quota(token)
            _REMOTE_Q.update(at=time.time(), data=r)
        except Exception:
            pass
        finally:
            with _REMOTE_Q_LOCK:
                _REMOTE_Q_INFLIGHT = False

    threading.Thread(target=_bg, daemon=True).start()


def remote_quota_cached(vault: Path, ttl: float = 60.0, now: float = None,
                        swr: bool = False, nonblocking_cold: bool = False):
    """带 TTL 缓存的远端额度 (serve 热路径每次 /ledger/usage 都调, 不能每请求打厂商)。
    未配置 Token → None (调用方不附 remote_quota 字段)。
    swr=True: 已有旧缓存且超时时立刻返回旧值, 同时后台单飞刷新 (绝不阻塞控制台首屏)。
    nonblocking_cold=True: 首次冷启无缓存时立刻触发后台拉取并返回 {"_pending": True}。"""
    token = load_plan_token(vault)
    if not token:
        return None
    now = time.time() if now is None else float(now)
    cur = _REMOTE_Q.get("data")
    if cur is not None and now - float(_REMOTE_Q.get("at") or 0.0) < ttl:
        return cur
    if swr and cur is not None:
        _refresh_remote_quota_async(token)
        return cur
    if nonblocking_cold and cur is None:
        _refresh_remote_quota_async(token)
        return {"_pending": True}
    r = fetch_remote_quota(token)
    _REMOTE_Q.update(at=now, data=r)
    return r


def cmd_ledger_limit(args):
    """cv ledger limit — 额度池配置助手: --list 看套餐预设; --preset 写入 accounts.json 上限。"""
    vault = cv.vault_dir(args)
    if getattr(args, "list", False) or not getattr(args, "preset", None):
        print(plan_presets_text())
        if not getattr(args, "preset", None):
            print("配置上限 (token 口径, 按实测消耗折算):")
            print("  cv ledger limit --preset glm-coding-pro --tokens-5h 30000000 \\")
            print("      [--tokens-week 150000000] [--source zcode] [--client zcode] [--model-prefix 'glm-*']")
            print("厂商实时额度 (免估算, 推荐): 把 Coding Plan 用户 Token 存入环境变量 CV_PLAN_TOKEN")
            print("  或 vault/plan_token.json ({\"token\": \"...\"}); quota/控制台即显示真实百分比与重置时间")
        return
    if not getattr(args, "tokens_5h", None) and not getattr(args, "tokens_week", None):
        raise SystemExit("需要 --tokens-5h 或 --tokens-week (token 口径; 参考上方 --list 的提示次数口径折算)")
    accounts = load_accounts(vault)
    profs = accounts.setdefault("profiles", [])
    name = getattr(args, "name", None) or str(args.preset)
    prof = {"name": name, "preset": str(args.preset)}
    mt = {}
    if getattr(args, "source", None):
        mt["source"] = [s.strip() for s in str(args.source).split(",") if s.strip()]
    if getattr(args, "client", None):
        mt["client"] = [s.strip() for s in str(args.client).split(",") if s.strip()]
    if getattr(args, "model_prefix", None):
        mt["model_prefix"] = [s.strip() for s in str(args.model_prefix).split(",") if s.strip()]
    if mt:
        prof["match"] = mt
    if getattr(args, "tokens_5h", None):
        prof["window_5h"] = {"tokens": int(args.tokens_5h)}
    if getattr(args, "tokens_week", None):
        prof["period"] = {"kind": "7d", "tokens": int(args.tokens_week)}
    profs[:] = [p for p in profs if not (isinstance(p, dict) and p.get("name") == name)]
    profs.append(prof)
    cv.atomic_write_text(vault / "accounts.json", json.dumps(accounts, ensure_ascii=False, indent=2))
    print(f"额度池已写入 {vault / 'accounts.json'}: {name} (preset={args.preset})")
    print("生效: cv ledger sync --recompute 重算归因; cv ledger quota / 控制台查看占比与恢复 ETA")


# ------------------------------------------------- Antigravity 本机 RPC 实时额度
# 思路移植自 antigravity-token-dashboard (ADR-021): Antigravity IDE 登录 Google 后,
# 其 language_server 常驻本机并持有会话; 从进程命令行提取 CSRF 令牌, 探测本地 HTTP 端口,
# 即可调 RetrieveUserQuotaSummary 拿实时额度百分比 (ground-truth, 零额外登录)。
# Windows 发现: PowerShell CIM 查进程命令行 + netstat 探端口 (上游是 macOS ps/lsof)。

AGV_LSP_RPC = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
AGV_LSP_STATUS = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
# GetUserStatus 标准请求体 (真机实测需要 metadata 才 200)
AGV_STATUS_BODY = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                                "locale": "en"}}


def _powershell_out(script: str, timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(cv, "NO_WINDOW", 0) if os.name == "nt" else 0)
        # 中文 Windows 输出是 GBK/系统码页: 只解析 ASCII 字段, 解码坏字节替换不致命
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _netstat_out() -> str:
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10,
                           creationflags=getattr(cv, "NO_WINDOW", 0))
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _ps_env_out() -> str:
    """POSIX: ps -E 环境变量进程列表 (独立助手便于测试 mock, 与 nt 的 _powershell_out 对称)。"""
    try:
        r = subprocess.run(["ps", "-ww", "-E", "-e", "-o", "pid,command"],
                           capture_output=True, timeout=8)
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _lsof_out(pid: int) -> str:
    """POSIX: lsof 单进程 LISTEN 端口 (独立助手便于测试 mock, 与 nt 的 _netstat_out 对称)。"""
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-p", str(pid)],
                           capture_output=True, timeout=8)
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def discover_agv_lsp_credentials():
    """发现本机 Antigravity language_server → (pid, csrf, port, scheme) 或 None。
    路线 1 (官方, IDE ≥2.29 实测): LSP 自写发现文件 ~/.gemini/antigravity/daemon/
        hub_<httpPort>.json {pid, httpsPort, httpPort, lsVersion, csrfToken} —
        2.34 起 token 走 stdin 不再上命令行, 该文件是唯一免凭据落盘点;
        mtime 新→旧, GetUserStatus 探活定夺 (残留死文件自然被拒)。
    路线 2 (旧版 IDE): 命令行 --csrf_token (放宽字符集) + WMI Name 过滤 (空则回退命令行
        匹配, 免全表扫) + netstat/lsof/命令行端口 (--extension_server_port/--port 补充)。"""
    for c in agv_hub_candidates()[:8]:
        if c["pid"] and not _pid_alive(c["pid"]):
            continue
        port = c["http_port"] or c["https_port"]
        if not port:
            continue
        data, used = _agv_call(port, c["csrf"], AGV_LSP_STATUS, body=AGV_STATUS_BODY,
                               timeout=1.5)
        if isinstance(data, dict):
            return c["pid"], c["csrf"], port, used or ("http" if c["http_port"] else "https")
    cands = []  # (pid, csrf, standalone, cmd_ports)
    if os.name == "nt":
        out = _powershell_out(
            "Get-CimInstance Win32_Process -Filter \"Name LIKE '%language_server%'\""
            " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        if not out.strip():  # Name 过滤没中 (个别版本进程名不同) → 回退命令行匹配 (慢)
            out = _powershell_out(
                "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match"
                " 'language_server'} | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+)\t(.+)$", line)
            if not m:
                continue
            mc = re.search(r"--csrf_token[= ][\"']?([A-Za-z0-9._-]{8,})", m.group(2))
            arg_ports = [int(x) for x in re.findall(
                r"--(?:https_server_port|extension_server_port|port)[= ](\d{3,5})",
                m.group(2)) if int(x) > 0]
            if mc:
                cands.append((int(m.group(1)), mc.group(1),
                              "--standalone" in m.group(2), arg_ports))
    else:
        for line in _ps_env_out().splitlines():
            if "language_server" not in line or "--csrf_token" not in line:
                continue
            mc = re.search(r"--csrf_token[= ][\"']?([A-Za-z0-9._-]{8,})", line)
            mp = re.match(r"^\s*(\d+)", line)
            arg_ports = [int(x) for x in re.findall(
                r"--(?:https_server_port|extension_server_port|port)[= ](\d{3,5})",
                line) if int(x) > 0]
            if mc and mp:
                cands.append((int(mp.group(1)), mc.group(1),
                              "--standalone" in line, arg_ports))
    if not cands:
        return None
    cands.sort(key=lambda c: not c[2])  # standalone 优先

    def _ports(pid, arg_ports):
        ports = []
        if os.name == "nt":
            for line in _netstat_out().splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[4] == str(pid):
                    m = re.search(r":(\d+)$", parts[1])
                    if m:
                        ports.append(int(m.group(1)))
        else:
            for line in _lsof_out(pid).splitlines():
                m = re.search(r":(\d+)\s+\(LISTEN\)", line)
                if m:
                    ports.append(int(m.group(1)))
        ports += [p for p in arg_ports if p not in ports]  # 命令行端口补充
        return ports[:12]

    for pid, csrf, _std, arg_ports in cands:
        for port in _ports(pid, arg_ports):
            data, used = _agv_call(port, csrf, AGV_LSP_STATUS, body=AGV_STATUS_BODY,
                                   timeout=1.2)
            if isinstance(data, dict):
                return pid, csrf, port, used
    return None


_SSL_UNVERIFIED = None


def _ssl_unverified():
    """https 自签证书上下文 (language_server 本地端口实测为自签 https + 明文 http 双栈)。"""
    global _SSL_UNVERIFIED
    if _SSL_UNVERIFIED is None:
        try:
            import ssl
            _SSL_UNVERIFIED = ssl._create_unverified_context()
        except Exception:
            return None
    return _SSL_UNVERIFIED


def _agv_call(port: int, csrf: str, method: str, body=None, timeout: float = 2.0,
              scheme: str = None):
    """单次本机 Connect-RPC (JSON 帧): POST body + X-Codeium-Csrf-Token。
    scheme=None → https (自签免校验) 与明文 http 双栈自动回退。返回 (data|None, scheme|None), 不抛。"""
    if scheme not in ("https", "http"):
        schemes = ("https", "http")
    else:
        schemes = (scheme,)
    payload = json.dumps(body).encode("utf-8") if body is not None else b"{}"
    for sch in schemes:
        req = Request(f"{sch}://127.0.0.1:{port}{method}", data=payload, headers={
            "Content-Type": "application/json", "X-Codeium-Csrf-Token": csrf})
        try:
            ctx = _ssl_unverified() if sch == "https" else None
            with urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8")), sch
        except Exception:
            continue
    return None, None


def _agv_rpc(port: int, csrf: str, method: str, body=None, timeout: float = 2.0,
             scheme: str = None):
    """兼容入口: 返回 dict, 失败抛 OSError (fetch 路径已带探活 scheme)。"""
    data, _ = _agv_call(port, csrf, method, body, timeout, scheme)
    if data is None:
        raise OSError(f"AGV RPC 不可达: {method} (https/http 均失败)")
    return data


def agv_hub_candidates():
    """LSP 自写发现文件 (~/.gemini/antigravity/daemon/hub_<httpPort>.json,
    形状 {pid, httpsPort, httpPort, lsVersion, csrfToken}) → 候选列表 (mtime 新→旧)。
    历史实例文件自然残留, 由调用方探活/试连定夺。"""
    root = cv.HOME / ".gemini" / "antigravity" / "daemon"
    if not root.is_dir():
        return []
    files = list(root.glob("hub_*.json"))
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    out = []
    for f in files[:20]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(d, dict) or not d.get("csrfToken"):
            continue
        out.append({"pid": int(_num(d.get("pid"), 0)),
                    "csrf": str(d["csrfToken"]),
                    "http_port": int(_num(d.get("httpPort"), 0) or 0),
                    "https_port": int(_num(d.get("httpsPort"), 0) or 0),
                    "version": str(d.get("lsVersion") or "")})
    return out


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _agv_field(d, key):
    """Connect-RPC 响应解包: 顶层字段优先, 兼容 response/userQuotaSummary/userStatus 包装
    (真机实测: GetUserStatus 顶层即 {userStatus}, QuotaSummary 顶层 {response:{groups}})。"""
    if not isinstance(d, dict):
        return None
    for w in (d, d.get("response"), d.get("userQuotaSummary"), d.get("userStatus")):
        if isinstance(w, dict) and w.get(key) is not None:
            return w.get(key)
    return None


def _agv_pool(label):
    """模型/分组名 → 共享配额池名 (claude/gpt 系模型共享池; 其余按原名分组)。"""
    s = str(label or "")
    if any(k in s.lower() for k in ("claude", "gpt", "3p", "o3", "o4")):
        return "claude/gpt"
    return s or "gemini"


def _agv_track(label):
    """路径 B 分组轨道 (官方聚合视图): claude/gpt 系 vs 其余归 gemini (保持旧口径)。"""
    return "claude/gpt" if any(k in str(label or "").lower()
                               for k in ("claude", "gpt", "3p")) else "gemini"


def _parse_model_configs(configs, now: float):
    """路径 A: GetUserStatus → userStatus.cascadeModelConfigData.clientModelConfigs[].quotaInfo
    (真机形状: label='Gemini 4 Neon (High)', quotaInfo{remainingFraction, resetTime})。
    同 (池, remainingFraction, resetTime) = 共享配额池 → 去重一行; 档位后缀 (High/Medium)
    剥离。resetTime 距今 >24h 视为 weekly 周期。"""
    limits, seen = [], set()
    for c in configs or []:
        if not isinstance(c, dict):
            continue
        qi = c.get("quotaInfo")
        if not isinstance(qi, dict) or "remainingFraction" not in qi:
            continue  # 无 quotaInfo 字段的模型不参与额度
        rem = float(_num(qi.get("remainingFraction"), 0.0))
        label = str(c.get("label") or (c.get("modelOrAlias") or {}).get("model") or "")
        pool = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip() or label
        reset = qi.get("resetTime") or None
        ep = ts_to_epoch(reset)
        window = "weekly" if (ep and ep - now > 24 * 3600) else "5h"
        key = (_agv_pool(pool), round(rem, 4), str(reset))
        if key in seen:
            continue
        seen.add(key)
        limits.append({"type": f"{key[0]} {window}",
                       "pct_used": round(max(0.0, (1.0 - rem) * 100.0), 1),
                       "reset_at": reset})
    limits.sort(key=lambda x: -x["pct_used"])  # 用得最多的池排前
    return limits


def _parse_quota_groups(groups):
    """路径 B: RetrieveUserQuotaSummary → groups[].buckets[] (官方聚合视图)。
    proto3 零值省略语义: remainingFraction 键缺省 = 0.0 剩余 (全用光)。"""
    limits, seen_types = [], set()
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        dname = str(g.get("displayName") or "").strip()
        desc = str(g.get("description") or "").strip() or None
        track = _agv_track(dname)
        for b in (g.get("buckets") or []):
            if not isinstance(b, dict):
                continue
            rem = float(_num(b.get("remainingFraction"), 0.0))
            b_id = str(b.get("bucketId") or "").lower()
            window = str(b.get("window") or ("weekly" if "weekly" in b_id else "5h"))
            tname = f"{track} {window}"
            if tname in seen_types and dname:
                tname = f"{dname.lower()} {window}"
            seen_types.add(tname)
            item = {"type": tname,
                    "pct_used": round(max(0.0, 100.0 - rem * 100.0), 2),
                    "reset_at": b.get("resetTime") or None}
            if desc:
                item["desc"] = desc
            limits.append(item)
    return limits


def fetch_agv_quota(timeout: float = 2.0, creds=None) -> dict:
    """Antigravity 实时额度 (尽力而为, 永不抛异常)。
    解析双路径: 先 GetUserStatus 的 clientModelConfigs[].quotaInfo (同调用顺带取
    userTier.name 作 plan, 按模型池细分); 无结果再取 RetrieveUserQuotaSummary 的
    官方 groups/buckets 聚合。失败返回 ok:false + 具体原因 (CLI/前端据实显示, 不静默)。"""
    creds = creds or discover_agv_lsp_credentials()
    if not creds:
        return {"ok": False, "error": "Antigravity language_server 未发现 (IDE 未运行?)"}
    creds = tuple(creds)
    creds += (None,) * (4 - len(creds))
    _pid, csrf, port, scheme = creds
    limits, plan, errs = [], "Antigravity", []
    try:  # 路径 B 优先: 专用额度端点的官方聚合 buckets (gemini / claude-gpt × 5h/weekly)
        q = _agv_rpc(port, csrf, AGV_LSP_RPC, timeout=timeout, scheme=scheme)
        limits = _parse_quota_groups(_agv_field(q, "groups"))
    except Exception as e:
        errs.append(f"QuotaSummary: {e}")
    try:  # GetUserStatus: 档位名 + 独立扣减的具体模型子池合并 (路径 A 无 B 时全量兜底)
        st = _agv_rpc(port, csrf, AGV_LSP_STATUS, body=AGV_STATUS_BODY, timeout=timeout,
                      scheme=scheme)
        us = _agv_field(st, "userStatus") or {}
        tier = us.get("userTier") if isinstance(us.get("userTier"), dict) else {}
        if tier.get("name"):
            plan = f"Antigravity {tier['name']}"
        ccd = _agv_field(us, "cascadeModelConfigData") or {}
        per_model = _parse_model_configs(
            (ccd.get("clientModelConfigs") or []) if isinstance(ccd, dict) else [],
            time.time())
        if not limits:
            limits = per_model
        else:
            covered_resets = {l.get("reset_at") for l in limits if l.get("reset_at")}
            covered_descs = " ".join(str(l.get("desc") or "").lower() for l in limits)
            for pm in per_model:
                base_m = re.sub(r"\s+(5h|weekly)$", "", pm["type"], flags=re.I).strip().lower()
                if pm["pct_used"] > 0 and pm.get("reset_at") not in covered_resets \
                        and (not base_m or base_m not in covered_descs):
                    limits.append(pm)
    except Exception as e:
        errs.append(f"GetUserStatus: {e}")
    err = "; ".join(errs)
    if not limits:
        return {"ok": False,
                "error": f"额度 RPC 无 quota (未登录或接口变更?)" + (f" [{err}]" if err else "")}
    return {"ok": True, "plan": plan, "limits": limits, "source": "antigravity-lsp",
            "fetched_at": epoch_to_iso(time.time())}


_AGV_Q = {"at": 0.0, "data": None}
_AGV_Q_LOCK = threading.Lock()
_AGV_Q_INFLIGHT = False


def _refresh_agv_quota_async() -> None:
    global _AGV_Q_INFLIGHT
    with _AGV_Q_LOCK:
        if _AGV_Q_INFLIGHT:
            return
        _AGV_Q_INFLIGHT = True

    def _bg():
        global _AGV_Q_INFLIGHT
        try:
            r = fetch_agv_quota()
            _AGV_Q.update(at=time.time(), data=r)
        except Exception:
            pass
        finally:
            with _AGV_Q_LOCK:
                _AGV_Q_INFLIGHT = False

    threading.Thread(target=_bg, daemon=True).start()


def agv_quota_cached(ttl: float = 60.0, now: float = None,
                     swr: bool = False, nonblocking_cold: bool = False):
    """带 TTL (含失败负缓存) 的 Antigravity 实时额度 — 避免热路径反复 spawn 进程扫描。
    swr=True: 过期时旧值 0ms 秒回 + 后台单飞线程异步更新。
    nonblocking_cold=True: 冷启无缓存时不阻塞请求, 返回 {"_pending": True} 并后台探测。"""
    now = time.time() if now is None else float(now)
    cur = _AGV_Q.get("data")
    if cur is not None and now - float(_AGV_Q.get("at") or 0.0) < ttl:
        return cur
    if swr and cur is not None:
        _refresh_agv_quota_async()
        return cur
    if nonblocking_cold and cur is None:
        _refresh_agv_quota_async()
        return {"_pending": True}
    r = fetch_agv_quota()
    _AGV_Q.update(at=now, data=r)
    return r


def cmd_ledger_plan_token(args):
    """cv ledger plan-token — 配置智谱 Coding Plan 用户 Token (厂商实时额度入口)。"""
    vault = cv.vault_dir(args)
    p = vault / "plan_token.json"
    if getattr(args, "clear", False):
        if p.exists():
            p.unlink()
            print("已清除 vault/plan_token.json")
        else:
            print("本就未配置")
        return
    tok = (getattr(args, "token", None) or "").strip()
    if tok:
        cv.atomic_write_text(p, json.dumps({"token": tok, "set_at": cv.now_iso()},
                                           ensure_ascii=False))
        print(f"已写入 {p} (vault 目录不入 git)")
        r = fetch_remote_quota(tok)
        if r.get("ok"):
            for l in r.get("limits", []):
                print(f"  验证成功: [{r.get('plan')}] {plan_limit_label(l.get('type'))} "
                      f"已用 {_num(l.get('pct_used'), 0.0):.1f}% · 重置 {_fmt_eta(l.get('reset_at'))}")
            print("刷新控制台 / cv ledger quota 即见「套餐余额 (厂商实时)」")
        else:
            print(f"  验证失败: {r.get('error')}")
            print("  注: 该接口通常不支持普通 API Key (sk-...) — 需要 Coding Plan 用户 Token:")
            print("      浏览器登录 bigmodel.cn → F12 → Network → 复制请求头 Authorization")
        return
    cur = load_plan_token(vault)
    print("当前状态:", ("已配置 (****" + cur[-6:] + ")") if cur else "未配置")
    print("用法: cv ledger plan-token <TOKEN> | cv ledger plan-token --clear")


# ---- 已拆解至 tools/cv_ledger_collect.py (全量 re-export 保持向后兼容与测试 monkeypatch) ----
from cv_ledger_collect import (  # noqa: F401,E402
    _AGV_CONTENT_COLS,
    _AGV_TS_COLS,
    AGV_EST_MODEL,
    COLLECTORS,
    _agv_base_state,
    _cfg_fingerprint,
    _file_sig,
    _insert_events,
    _maybe_recompute,
    _meta_flag,
    _meta_step_ts,
    _parse_gen_metadata_row,
    _pb_kv_map,
    _pb_varint,
    _set_meta_flag,
    _snapshot_proj_map,
    _wm_changed,
    _wm_update,
    agv_gen_metadata_migration,
    agv_perstep_migration,
    agv_workspaces,
    cmd_ledger_sync,
    collect_antigravity,
    collect_claude,
    collect_codex,
    collect_dsh,
    collect_zcode,
    collect_zcode_db,
    ensure_schema,
    recompute_derived,
    sync_from_agm,
    sync_usage_events,
    zcode_project_backfill,
)


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


def _eta_slide(evs, threshold: float, now: float):
    """纯消耗模式 (无上限) 的窗口回落 ETA: 当前 5h 已用滑出 (1-threshold) 的时刻。
    threshold=1.0 → 完全冷却 (最后一批事件滑出)。evs 须按 occurred_at 升序。"""
    if not evs:
        return None
    if threshold >= 1.0:
        last = max((r["occurred_at"] or now) for r in evs)
        return epoch_to_iso(max(last + WINDOW_5H, now))
    used = sum(_row_weight(r) for r in evs)
    target = used * (1.0 - threshold)
    freed = 0.0
    for r in evs:
        freed += _row_weight(r)
        if freed >= target - 1e-9:
            slide = (r["occurred_at"] or now) + WINDOW_5H
            return epoch_to_iso(slide if slide > now else now)
    return None


def _fiveh_accounts(conn, now: float, accounts: dict, account: str = None):
    """5h 滚动窗口: 按账号 (含隐式 client 分组) 汇已用/占比/冷却 ETA。
    配了 limit → 恢复至 50%/80%/100% 的 ETA; 未配置 (纯消耗模式) → 窗口回落
    50%/80%/完全清零 ETA (始终计算, 绝不因缺上限而隐藏), 并附 7d 单 5h 峰值比。"""
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
    # 近 7d 单 5h 翻滚桶峰值 (无上限时的进度条基准; 一次查询全账号共享)
    peak_by_acct = {}
    try:
        rows7 = conn.execute(
            "SELECT account, client, source, occurred_at, tokens_in, tokens_out,"
            " cache_read, cache_write FROM usage_events WHERE occurred_at > ?",
            (now - 7 * 86400,)).fetchall()
    except sqlite3.OperationalError:
        rows7 = []
    for r in rows7:
        k = _account_of(r)
        b = int((r["occurred_at"] or 0) // WINDOW_5H)
        peak_by_acct.setdefault(k, {})
        peak_by_acct[k][b] = peak_by_acct[k].get(b, 0) + _row_weight(r)
    peaks = {k: max(v.values()) for k, v in peak_by_acct.items()}
    window_total = sum(sum(_row_weight(x) for x in g) for g in groups.values())
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
        if limit:
            for th, label in ((0.5, "50%"), (0.8, "80%"), (1.0, "100%")):
                etas[label] = _eta(evs, limit, th, now, weight)
        else:  # 纯消耗模式: 窗口回落/清零倒计时 (基于当前已用, 与上限无关)
            for th, label in ((0.5, "50%"), (0.8, "80%"), (1.0, "100%")):
                etas[label] = _eta_slide(evs, th, now)
        peak = peaks.get(key)
        by_m = {}
        for x in evs:
            mn = x["model"] or ("antigravity · 估算" if x["source"] == "antigravity" else "(未知)")
            by_m[mn] = by_m.get(mn, 0.0) + _row_weight(x, weight)
        models_top = [
            {"name": mn, "used": round(mv, 1),
             "share": round(mv / used_w, 4) if used_w else 0.0}
            for mn, mv in sorted(by_m.items(), key=lambda kv: -kv[1])[:4]
        ]
        out.append({"account": key, "client": evs[0]["client"] or evs[0]["source"],
                    "events": len(evs), "used": int(sum(_row_weight(x) for x in evs)),
                    "weight": weight, "used_weighted": round(used_w, 1), "limit": limit,
                    "pct": round(pct, 4) if pct is not None else None,
                    "mode": "limit" if limit else "consumption",
                    "peak_5h": round(peak, 1) if peak else None,
                    "pct_of_peak": round(used_w / peak, 4) if peak else None,
                    "share": round(used_w / window_total, 4) if window_total else None,
                    "models": models_top,
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
            "SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)"
            "+COALESCE(cache_read,0)+COALESCE(cache_write,0)),0),"
            " COALESCE(SUM(COALESCE(cost_est_usd,0)),0.0)"
            " FROM usage_events WHERE occurred_at > ? AND account=?",
            (now - 86400, name)).fetchone()
        rate_tok = int(r24[0] or 0) if r24 else 0
        rate_usd = float(r24[1] or 0.0) if r24 else 0.0
        start = _period_start(kind, now)
        r_per = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)"
            "+COALESCE(cache_read,0)+COALESCE(cache_write,0)),0),"
            " COALESCE(SUM(COALESCE(cost_est_usd,0)),0.0)"
            " FROM usage_events WHERE occurred_at >= ? AND account=?",
            (start, name)).fetchone()
        used_tok = int(r_per[0] or 0) if r_per else 0
        used_usd = float(r_per[1] or 0.0) if r_per else 0.0
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
               "runway": [], "sources": {}, "window_sources": {}}
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
        wsrc = {}
        for r in win:
            wsrc[r["source"]] = wsrc.get(r["source"], 0) + 1
        payload["window_sources"] = wsrc  # 窗口内来源分解 (sources 为全量口径)
        denom = totals["total_input"]
        payload["cache_hit_rate"] = round(totals["cache_read"] / denom, 4) if denom else None
        payload["cache_save_est_usd"] = round(cache_save, 4)
        payload["precision"] = {
            "exact_events": len(win) - est_ev, "estimated_events": est_ev,
            "estimated_share": round(est_ev / len(win), 4) if win else 0.0}
        payload["by_model"] = _agg_rows(
            win, lambda r: r["model"]
            or ("antigravity · 估算" if r["source"] == "antigravity" else None))
        def _norm_proj(p):
            if not p:
                return "(未标注)"
            s = str(p).strip()
            if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
                s = (s[0].upper() + ":" + s[2:].replace("/", "\\")).rstrip("\\")
            return s or "(未标注)"

        payload["by_project"] = _agg_rows(win, lambda r: _norm_proj(r["project"]))
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


def plan_limit_label(t: str) -> str:
    """智谱 limit 类型 → 中文显示 (实测: TOKENS_LIMIT=5小时额度, TIME_LIMIT=MCP 每月额度)。"""
    return {"TOKENS_LIMIT": "5h Token 额度", "TIME_LIMIT": "MCP 月额度",
            "PROMPTS_LIMIT": "提示次数", "REQUESTS_LIMIT": "请求数"}.get(str(t or ""), str(t or "?"))


def _fmt_eta(v):
    """重置倒计时: 接受 ISO 字符串或 epoch 数值 (秒/毫秒, 智谱 nextResetTime 实测为数值)。"""
    if v is None:
        return "—"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        epoch = v if v < 1e12 else v / 1000.0  # 毫秒 epoch 归一
        iso = epoch_to_iso(epoch)
    else:
        iso = str(v).strip() or None
        if not iso:
            return "—"
    try:
        norm_iso = iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso
        delta = dt.datetime.fromisoformat(norm_iso) - dt.datetime.now().astimezone()
    except ValueError:
        return iso
    s = int(delta.total_seconds())
    if s <= 0:
        return "已恢复"
    h, m = s // 3600, (s % 3600) // 60
    if h >= 48:
        return f"{h / 24:.1f} 天后"
    return f"{h}h{m:02d}m 后" if h else f"{m}m 后"


# fmt_usd 已收敛至 cv_core (顶部导入)


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
    rq = remote_quota_cached(vault)
    if rq and rq.get("ok"):
        print("== 套餐余额 (厂商实时) ==")
        for l in rq.get("limits", []):
            pct = _num(l.get("pct_used"), 0.0)
            print(f"  [{rq.get('plan', '?')}] {plan_limit_label(l.get('type'))}: 已用 {pct:.1f}% · "
                  f"剩余 {100 - pct:.1f}% · 重置 {_fmt_eta(l.get('reset_at'))}")
    elif rq is not None:
        print(f"== 套餐余额: {rq.get('error', '不可用')} ==")
    aq = agv_quota_cached()
    if aq and aq.get("ok"):
        print("== Antigravity 额度 (本机 IDE 实时) ==")
        for l in aq.get("limits", []):
            pct = _num(l.get("pct_used"), 0.0)
            print(f"  {l.get('type')}: 已用 {pct:.1f}% · 剩余 {100 - pct:.1f}%"
                  f" · 重置 {_fmt_eta(l.get('reset_at'))}")
    elif aq is not None:
        print(f"== Antigravity 额度: {aq.get('error', '不可用')} ==")
    accs = p["fiveh"]["accounts"]
    if any(a["limit"] for a in accs):
        print("== 5 小时额度窗口 (本地额度池) ==")
    else:
        print("== 5h 消耗构成 (按客户端归因; 厂商总池占比见上方套餐余额) ==")
    for a in accs:
        lim = f" / {a['limit']:,.0f}" if a["limit"] else ""
        pct = f"{a['pct'] * 100:5.1f}%" if a["pct"] is not None else "     —"
        peak = f" · 峰值比 {a['pct_of_peak'] * 100:.0f}%" \
            if not a["limit"] and a.get("pct_of_peak") is not None else ""
        bar_pct = a["pct"] if a["pct"] is not None else a.get("pct_of_peak")
        print(f"  {a['account']:<26} {_bar(bar_pct)} {pct}{lim}  已用 {a['used_weighted']:,.0f}"
              f" ({a['weight']}{peak})")
        if a.get("eta"):
            if a.get("mode") == "consumption":
                print(f"      窗口回落 50%: {_fmt_eta(a['eta'].get('50%'))} · 80%: "
                      f"{_fmt_eta(a['eta'].get('80%'))} · 完全冷却: {_fmt_eta(a['eta'].get('100%'))}")
            else:
                print(f"      恢复至 50%: {_fmt_eta(a['eta'].get('50%'))} · 80%: "
                      f"{_fmt_eta(a['eta'].get('80%'))} · 100%: {_fmt_eta(a['eta'].get('100%'))}")
        ms = a.get("models") or []
        if ms:
            ms_s = " · ".join(
                f"{m['name']} {m['share'] * 100:.0f}% ({m['used']:,.0f})"
                for m in ms)
            print(f"      模型拆分: {ms_s}")
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
