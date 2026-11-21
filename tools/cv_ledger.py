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
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen

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
# 订阅制型号 (glm-5*/gpt-5.6*/gpt-6* 等) 按「公开 API 挂牌价等效折算」收录参考近似价 —
# UI 标注估算非账单; 精确价以 vault/pricing.json 覆盖 (逐条生效, 不需改代码)。
PRICING_REV = 2  # 内置表修订号: 纳入配置指纹 (_cfg_fingerprint), 表变更即自动重算存量事件
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
    "gpt-5.6*":    {"input": 2.50, "output": 16.0, "cache_read": 0.31},
    "gpt-5*":      {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "gpt-6*":      {"input": 4.00, "output": 20.0, "cache_read": 0.50},
    "o3*":         {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "o4-mini*":    {"input": 1.10, "output": 4.40, "cache_read": 0.275},
    "gpt-4.1*":    {"input": 2.0, "output": 8.0, "cache_read": 0.50},
    "gpt-4o*":     {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    # Google Gemini
    "gemini-3-pro*":     {"input": 2.0, "output": 12.0, "cache_read": 0.50},
    "gemini-3-flash*":   {"input": 0.50, "output": 3.0, "cache_read": 0.125},
    "gemini-3*":         {"input": 2.0, "output": 12.0, "cache_read": 0.50},
    "gemini-2.5-pro*":   {"input": 1.25, "output": 10.0, "cache_read": 0.31},
    "gemini-2.5-flash*": {"input": 0.30, "output": 2.50, "cache_read": 0.075},
    # Zhipu GLM (订阅制, 公开 API 挂牌价等效折算; flash 档按 ~1/5 输入近似)
    "glm-5.3-flash*": {"input": 0.20, "output": 0.90, "cache_read": 0.04},
    "glm-5.3*":       {"input": 1.00, "output": 3.50, "cache_read": 0.20},
    "glm-5*":         {"input": 0.60, "output": 2.20, "cache_read": 0.12},
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


def remote_quota_cached(vault: Path, ttl: float = 60.0, now: float = None):
    """带 TTL 缓存的远端额度 (serve 热路径每次 /ledger/usage 都调, 不能每请求打厂商)。
    未配置 Token → None (调用方不附 remote_quota 字段)。"""
    token = load_plan_token(vault)
    if not token:
        return None
    now = time.time() if now is None else float(now)
    if _REMOTE_Q["data"] is not None and now - _REMOTE_Q["at"] < ttl:
        return _REMOTE_Q["data"]
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


def discover_agv_lsp_credentials():
    """发现本机 Antigravity language_server → (pid, csrf_token, port) 或 None。
    Windows: Get-CimInstance 查命令行 (--csrf_token) + netstat LISTENING 探端口;
    POSIX: ps -E + lsof (上游同款)。--standalone 进程优先。"""
    cands = []  # (pid, csrf, standalone)
    if os.name == "nt":
        out = _powershell_out(
            "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'language_server'}"
            " | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+)\t(.+)$", line)
            if not m:
                continue
            mc = re.search(r"--csrf_token[= ]([0-9a-fA-F-]{8,})", m.group(2))
            if mc:
                cands.append((int(m.group(1)), mc.group(1),
                              "--standalone" in m.group(2)))
    else:
        try:
            r = subprocess.run(["ps", "-ww", "-E", "-e", "-o", "pid,command"],
                               capture_output=True, timeout=8)
            for line in (r.stdout or b"").decode("utf-8", errors="replace").splitlines():
                if "language_server" not in line or "--csrf_token" not in line:
                    continue
                mc = re.search(r"--csrf_token[= ]([0-9a-fA-F-]{8,})", line)
                mp = re.match(r"^\s*(\d+)", line)
                if mc and mp:
                    cands.append((int(mp.group(1)), mc.group(1),
                                  "--standalone" in line))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not cands:
        return None
    cands.sort(key=lambda c: not c[2])  # standalone 优先

    def _ports(pid):
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
            try:
                r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-p", str(pid)],
                                   capture_output=True, timeout=8)
                for line in (r.stdout or b"").decode("utf-8", errors="replace").splitlines():
                    m = re.search(r":(\d+)\s+\(LISTEN\)", line)
                    if m:
                        ports.append(int(m.group(1)))
            except (OSError, subprocess.TimeoutExpired):
                pass
        return ports[:12]

    for pid, csrf, _std in cands:
        for port in _ports(pid):
            try:
                probe = _agv_rpc(port, csrf, AGV_LSP_RPC, timeout=1.2)
            except Exception:
                continue
            if isinstance(probe, dict):
                return pid, csrf, port
    return None


def _agv_rpc(port: int, csrf: str, method: str, timeout: float = 2.0):
    """本机 language_server Connect-RPC (JSON 帧): POST {} + X-Codeium-Csrf-Token。"""
    url = f"http://127.0.0.1:{port}{method}"
    req = Request(url, data=b"{}", headers={
        "Content-Type": "application/json", "X-Codeium-Csrf-Token": csrf})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_agv_quota(timeout: float = 2.0, creds=None) -> dict:
    """Antigravity 实时额度 (尽力而为, 永不抛异常): 5h/weekly × gemini/claude-gpt 双轨。"""
    creds = creds or discover_agv_lsp_credentials()
    if not creds:
        return {"ok": False, "error": "Antigravity language_server 未发现 (IDE 未运行?)"}
    _pid, csrf, port = creds
    try:
        data = _agv_rpc(port, csrf, AGV_LSP_RPC, timeout)
    except Exception as e:
        return {"ok": False, "error": f"额度 RPC 失败: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "额度 RPC 响应形状不符"}
    groups = ((data.get("response") or {}).get("groups")) or []
    limits = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        dname = str(g.get("displayName") or "").lower()
        track = "claude/gpt" if any(k in dname for k in ("claude", "gpt", "3p")) else "gemini"
        for b in (g.get("buckets") or []):
            if not isinstance(b, dict):
                continue
            rem = float(_num(b.get("remainingFraction"), 0.0))
            b_id = str(b.get("bucketId") or "").lower()
            window = str(b.get("window") or ("weekly" if "weekly" in b_id else "5h"))
            limits.append({"type": f"{track} {window}",
                           "pct_used": round(max(0.0, 100.0 - rem * 100.0), 2),
                           "reset_at": b.get("resetTime") or None})
    plan = "Antigravity"
    try:  # 档位名 (可选, 失败不影响额度)
        st = _agv_rpc(port, csrf, AGV_LSP_STATUS, min(timeout, 1.5))
        us = (st or {}).get("userStatus") or ((st or {}).get("response") or {}).get("userStatus") or {}
        tier = (us.get("userTier") or {}) if isinstance(us.get("userTier"), dict) else {}
        if tier.get("name"):
            plan = f"Antigravity {tier['name']}"
    except Exception:
        pass
    if not limits:
        return {"ok": False, "error": "额度 RPC 无 buckets (未登录或接口变更?)"}
    return {"ok": True, "plan": plan, "limits": limits, "source": "antigravity-lsp",
            "fetched_at": epoch_to_iso(time.time())}


_AGV_Q = {"at": 0.0, "data": None}


def agv_quota_cached(ttl: float = 60.0, now: float = None):
    """带 TTL (含失败负缓存) 的 Antigravity 实时额度 — 避免热路径反复 spawn 进程扫描。"""
    now = time.time() if now is None else float(now)
    if _AGV_Q["data"] is not None and now - _AGV_Q["at"] < ttl:
        return _AGV_Q["data"]
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


def collect_zcode_db(skip_fn=None, proj_map=None):
    """ZCode 主库 ~/.zcode/cli/db/db.sqlite model_usage 表 — 全量调用真相源 (55k+ 行)。
    每行一次模型调用: id 稳定幂等键, started_at/completed_at 毫秒 epoch, input 含缓存
    (OpenAI 语义, 入库规范化扣除 cache)。
    项目归因: 主库 session 表 directory (主源, 同库 join) > proj_map (vault snapshots 兜底)。
    返回 (events, marks) 或 None (不可用→回落)。"""
    db_path = getattr(cv, "ZCODE_DB", None)
    if not db_path or not Path(db_path).exists():
        return None
    db_path = Path(db_path)
    try:
        sig = _file_sig(db_path, with_wal=True)
    except OSError:
        return None
    if skip_fn and skip_fn(db_path, sig):
        return [], [(db_path, sig)]  # 未变更: 不读盘
    zdb = None
    events = []
    try:
        zdb = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        tables = {r[0] for r in zdb.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r[1] for r in zdb.execute("PRAGMA table_info(model_usage)").fetchall()] \
            if "model_usage" in tables else []
        if "input_tokens" not in cols:
            return None  # 无 model_usage 表 → 回落 model-io
        sess_dir = {}
        if "session" in tables:  # 项目归因主源: session.directory (工作区路径)
            try:
                scols = [r[1] for r in zdb.execute("PRAGMA table_info(session)").fetchall()]
                if "directory" in scols:
                    for sid, d in zdb.execute(
                            "SELECT id, directory FROM session"
                            " WHERE directory IS NOT NULL"):
                        if d:
                            sess_dir[str(sid)] = str(d)
            except sqlite3.Error:
                pass
        sel_cols = [c for c in ("id", "session_id", "model_id", "agent", "started_at",
                                "completed_at", "input_tokens", "output_tokens",
                                "reasoning_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens", "provider_id")
                    if c in cols]
        for row in zdb.execute(f"SELECT {', '.join(sel_cols)} FROM model_usage"):
            r = dict(zip(sel_cols, row))
            rid = str(r.get("id") or r.get("logical_request_id") or "").strip()
            if not rid:
                continue
            raw_ms = _num(r.get("completed_at") or r.get("started_at"), 0)
            ep = (raw_ms / 1000.0) if raw_ms > 1e12 else (float(raw_ms) if raw_ms else 0.0)
            tin_all = int(_num(r.get("input_tokens")))
            cr = max(0, int(_num(r.get("cache_read_input_tokens"))))
            cw = max(0, int(_num(r.get("cache_creation_input_tokens"))))
            session_id = r.get("session_id") or None
            events.append({
                "event_key": _sha(f"zdb|{rid}"),
                "source": "zcode", "client": "zcode", "model": r.get("model_id") or None,
                "project": sess_dir.get(str(session_id or ""))
                or (proj_map or {}).get(session_id),
                "session_ref": session_id,
                "occurred_at": ep,
                "tokens_in": max(0, tin_all - cr - cw),  # 规范化: input 含缓存 → 扣除
                "tokens_out": int(_num(r.get("output_tokens"))),
                "reasoning_tokens": int(_num(r.get("reasoning_tokens"))),
                "cache_read": cr, "cache_write": cw, "is_estimated": 0,
                "raw": json.dumps({"zdb_id": rid, "session_id": session_id,
                                   "agent": r.get("agent"), "provider": r.get("provider_id")},
                                  ensure_ascii=False)})
        return events, [(db_path, sig)]
    except sqlite3.Error:
        return None  # 锁/损坏: 回落 model-io, 下轮重试
    finally:
        if zdb:
            zdb.close()


def collect_zcode(base_dir=None, skip_fn=None, proj_map=None):
    """ZCode 用量采集 — 首选主库 model_usage 全量真相源 (统一口径, 不再只吃 model-io 零头);
    主库缺失/损坏时回落 ~/.zcode/cli/rollout/model-io-*.jsonl 审计文件 (双格式规范化)。
    proj_map: {session_ref: 项目路径} (sync 层从 vault snapshots 合成, 归因兜底)。
    skip_fn(path, sig) 为真时不读盘跳过。"""
    zdb = collect_zcode_db(skip_fn=skip_fn, proj_map=proj_map)
    if zdb is not None:
        return zdb  # 主库真相源 (events, marks)
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
                    "project": (proj_map or {}).get(stem_session),
                    "session_ref": stem_session,
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
AGV_EST_MODEL = "antigravity (估算)"  # 无模型字段的估算事件命名 (替代 "(未知)")


def _pb_varint(b: bytes, i: int):
    """protobuf varint 解码 → (value, next_index); 越界/超长返回已解码部分。"""
    v = 0
    shift = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not c & 0x80:
            return v, i
        shift += 7
        if shift > 70:
            break
    return v, i


def _meta_step_ts(meta):
    """steps.metadata protobuf → 步级 epoch 秒; 解析不出 → None。
    布局 (2026-11 真机 93/93 库逐步骤实测, 与 cv 导出产物同构):
    顶层 field1 wiretype2 = Timestamp 消息, 内层 field1 varint = epoch 秒。"""
    b = bytes(meta) if meta else b""
    i = 0
    n = len(b)
    while i < n:
        tag, j = _pb_varint(b, i)
        wt, fno = tag & 7, tag >> 3
        if fno == 0:
            return None
        if wt == 0:
            v, i = _pb_varint(b, j)
            if fno == 1 and 1_500_000_000 < v < 2_100_000_000:
                return float(v)
        elif wt == 2:
            ln, k = _pb_varint(b, j)
            i = k + ln
            if fno == 1 and ln:
                inner = b[k:i]
                if inner and inner[0] == 0x08:
                    v, _ = _pb_varint(inner, 1)
                    if 1_500_000_000 < v < 2_100_000_000:
                        return float(v)
        elif wt == 5:
            i = j + 4
        elif wt == 1:
            i = j + 8
        else:
            return None
    return None


def agv_workspaces():
    """Antigravity 摘要索引 (conversation_summaries.db, 只读) → {conv_id: 工作区路径}。
    项目归因主源; 失败/缺库返回 {} (诚实留空, 绝不编造)。"""
    p = cv.HOME / ".gemini" / "antigravity" / "conversation_summaries.db"
    if not p.exists():
        return {}
    out = {}
    try:
        sdb = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            cols = [r[1] for r in sdb.execute(
                "PRAGMA table_info(conversation_summaries)").fetchall()]
            if "workspace_uris" not in cols:
                return {}
            for cid, wu in sdb.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries"):
                try:
                    ws = json.loads(wu) if wu else []
                except ValueError:
                    continue
                for u in ws if isinstance(ws, list) else []:
                    if isinstance(u, str) and u.startswith("file://"):
                        path = unquote(u[len("file://"):])
                        if path.startswith("/") and len(path) > 2 and path[2] == ":":
                            path = path[1:]  # /c:/x → c:/x (Windows file URI 三斜线)
                        out[str(cid)] = path
                        break
        finally:
            sdb.close()
    except sqlite3.Error:
        return out
    return out


def collect_antigravity(base_dir=None, skip_fn=None, agv_state=None):
    """~/.gemini/antigravity/conversations/<uuid>.db (SQLite, 内容为 protobuf blob)。
    无精确 token — 按 steps 内容列字节体积估算 (÷4), 一律 is_estimated=1。
    时间戳优先级 (修复「老会话全量灌入近 5h」失真):
      1) steps.metadata protobuf 步级秒级时间戳 (原生与导出产物同构) — 每步一事件,
         各步落自己的真实时刻, 历史体积不再按 db mtime 集中进当前窗口;
      2) 无任何步级时间戳的老库 → 差量记账: 存量基准事件 agv|<conv>|base 锚在最早已知
         时间 (agv_state 提供快照/基准锚点, 兜底文件创建时间), 体积增长只对滚动增量事件
         agv|<conv>|delta 按 mtime 记账, 存量不再重复入窗口。
    agv_state: {conv_id: {"base_bytes": 已入库基准 blob 字节, "anchor": 最早锚点 epoch}}
    (由 sync 层从 usage_events 基准行 + snapshots 快照时间合成); 缺省 = 首次观测。
    项目归因: agv_workspaces() (conversation_summaries workspace_uris)。
    水位含 .db-wal; skip_fn(path, sig) 为真时不读盘跳过。"""
    root = Path(base_dir) if base_dir else cv.HOME / ".gemini" / "antigravity" / "conversations"
    if not root.is_dir():
        return [], []
    ws_map = agv_workspaces()
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
        project = ws_map.get(conv_id)
        try:
            adb = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                cols = [r[1] for r in adb.execute("PRAGMA table_info(steps)").fetchall()]
                content_col = next((c for c in _AGV_CONTENT_COLS if c in cols), None)
                if not cols or content_col is None:
                    continue  # 无 steps 内容列: 非会话库, 跳过 (finally 兜底关连接)
                ts_col = next((c for c in _AGV_TS_COLS if c in cols), None)
                meta_expr = "metadata" if "metadata" in cols else "NULL"
                sel = f"SELECT idx, {meta_expr}, COALESCE(LENGTH({content_col}),0)"
                if ts_col:
                    sel += f", {ts_col}"
                step_rows = adb.execute(sel + " FROM steps ORDER BY idx").fetchall()
            finally:
                adb.close()
        except sqlite3.Error:
            continue  # 活跃库锁/损坏: 跳过, 水位未推下轮重试
        blob_total = 0
        step_evs = []  # (idx, epoch, bytes) 有步级时间戳的步骤
        leftover = 0
        for row in step_rows:
            idx, meta, blob_n = row[0], row[1], int(row[2] or 0)
            ts_col_v = row[3] if len(row) > 3 else None
            blob_total += blob_n
            # 时间戳优先级: metadata protobuf (原生同构) > 显式时间戳列 (旧布局)
            ts = _meta_step_ts(meta) or ts_to_epoch(ts_col_v)
            if ts:
                step_evs.append((idx, ts, blob_n))
            else:
                leftover += blob_n
        if step_evs:
            for idx, ep, blob_n in step_evs:
                events.append({
                    "event_key": _sha(f"agv|{conv_id}|{idx}"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id, "occurred_at": ep,
                    "tokens_in": max(1, blob_n // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1,
                    "raw": json.dumps({"step_idx": str(idx),
                                       "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
            if leftover:  # 无时间戳步骤的字节并一条补遗, 锚在会话最早步 (历史性, 不进当前窗口)
                events.append({
                    "event_key": _sha(f"agv|{conv_id}|base"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id, "occurred_at": step_evs[0][1],
                    "tokens_in": max(1, leftover // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1, "upsert": True,
                    "raw": json.dumps({"step_idx": "base", "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
        elif blob_total:
            # 老库兜底: 差量记账 (存量基准 + 滚动增量), 绝不把全量按 mtime 灌入当前窗口
            st = (agv_state or {}).get(conv_id) or {}
            base_bytes = int(st.get("base_bytes") or 0)
            anchor = st.get("anchor")
            if not anchor:
                try:
                    anchor = f.stat().st_ctime  # Windows=创建时间; POSIX=变更时间, 皆早于 now
                except OSError:
                    anchor = sig[0]
            if base_bytes and base_bytes <= blob_total:
                delta = blob_total - base_bytes
                if delta > 0:
                    events.append({
                        "event_key": _sha(f"agv|{conv_id}|delta"),
                        "source": "antigravity", "client": "antigravity",
                        "model": AGV_EST_MODEL, "project": project,
                        "session_ref": conv_id, "occurred_at": sig[0],
                        "tokens_in": max(1, delta // 4), "tokens_out": 0,
                        "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                        "is_estimated": 1, "upsert": True,
                        "raw": json.dumps({"step_idx": "delta",
                                           "content_col": content_col,
                                           "estimate_basis": "blob_bytes/4"},
                                          ensure_ascii=False)})
            else:  # 首次观测: 全量作为存量基准, 锚在最早已知时间 (快照/创建时间)
                events.append({
                    "event_key": _sha(f"agv|{conv_id}|base"),
                    "source": "antigravity", "client": "antigravity",
                    "model": AGV_EST_MODEL, "project": project,
                    "session_ref": conv_id,
                    "occurred_at": min(anchor, sig[0]) if anchor else sig[0],
                    "tokens_in": max(1, blob_total // 4), "tokens_out": 0,
                    "reasoning_tokens": 0, "cache_read": 0, "cache_write": 0,
                    "is_estimated": 1,
                    "raw": json.dumps({"step_idx": "base", "content_col": content_col,
                                       "estimate_basis": "blob_bytes/4"},
                                      ensure_ascii=False)})
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
    parts = [f"builtin:{PRICING_REV}"]  # 内置表变更 (含订阅模型补价) 也触发存量重算
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


# ------------------------------------------------- 升级迁移 (vault_meta 旗标, 一次性)

def _meta_flag(conn, key: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM vault_meta WHERE key=?", (key,)).fetchone())
    except sqlite3.OperationalError:
        return False


def _set_meta_flag(conn, key: str):
    conn.execute("INSERT INTO vault_meta(key,value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, cv.now_iso()))


def agv_perstep_migration(conn) -> int:
    """一次性: AGV 记账升级为步级时间戳/差量记账 — 删除旧「会话级全量按 mtime」事件
    (event_key 是 sha1 hex 无结构差异, 以 raw 的 step_idx=conv 判别), 防止老会话历史体积
    继续污染近 5h 窗口 (新事件由采集器按真实时间重出)。
    同时清 AGV 水位: 旧水位会把全部 .db 判为未变更 → 步级事件补不进来。"""
    if _meta_flag(conn, "agv_perstep_v1"):
        return 0
    n = conn.execute("DELETE FROM usage_events WHERE source='antigravity'"
                     " AND raw LIKE '%\"step_idx\": \"conv\"%'").rowcount
    conn.execute("DELETE FROM usage_sync_state WHERE source='antigravity'")
    _set_meta_flag(conn, "agv_perstep_v1")
    return n


def zcode_project_backfill(conn, vault: Path) -> int:
    """一次性: 存量 zcode 事件 project 回填 — 主库 session.directory 主源,
    vault snapshots (最新 cwd 优先) 兜底; 消除满屏 (未标注)。"""
    if _meta_flag(conn, "zcode_project_v1"):
        return 0
    proj = {}
    zdb_path = getattr(cv, "ZCODE_DB", None)
    if zdb_path and Path(zdb_path).exists():
        z = None
        try:
            z = sqlite3.connect(f"file:{Path(zdb_path).as_posix()}?mode=ro", uri=True, timeout=5)
            tabs = {r[0] for r in z.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" in tabs and "directory" in \
                    [r[1] for r in z.execute("PRAGMA table_info(session)").fetchall()]:
                for sid, d in z.execute(
                        "SELECT id, directory FROM session WHERE directory IS NOT NULL"):
                    if d:
                        proj[str(sid)] = str(d)
        except sqlite3.Error:
            pass
        finally:
            if z:
                z.close()
    proj.update(_snapshot_proj_map(conn))
    n = 0
    try:
        refs = conn.execute("SELECT DISTINCT session_ref FROM usage_events"
                            " WHERE source='zcode' AND session_ref IS NOT NULL"
                            " AND (project IS NULL OR project='')").fetchall()
    except sqlite3.OperationalError:
        refs = []
    for (ref,) in refs:
        d = proj.get(ref)
        if d:
            n += conn.execute("UPDATE usage_events SET project=?"
                              " WHERE source='zcode' AND session_ref=?", (d, ref)).rowcount
    _set_meta_flag(conn, "zcode_project_v1")
    return n


def _snapshot_proj_map(conn) -> dict:
    """vault snapshots → {session_ref: cwd}; 同会话多版本时取最新快照的 cwd。"""
    try:
        rows = conn.execute("SELECT session_ref, cwd FROM snapshots"
                            " WHERE cwd IS NOT NULL AND session_ref IS NOT NULL"
                            " ORDER BY created_at DESC").fetchall()
    except sqlite3.OperationalError:
        return {}
    m = {}
    for r in rows:
        m.setdefault(r["session_ref"], r["cwd"])
    return m


def _agv_base_state(conn) -> dict:
    """存量 AGV 基准事件 → {conv_id: {base_bytes, anchor}} (差量记账状态)。
    另合入 snapshots 里 antigravity 会话的最早 created_at 作首观测锚点 (agy-src- 前缀归一)。"""
    st = {}
    try:
        rows = conn.execute("SELECT session_ref, tokens_in, occurred_at FROM usage_events"
                            " WHERE source='antigravity' AND event_key LIKE '%|base'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        st[r["session_ref"]] = {"base_bytes": int(r["tokens_in"] or 0) * 4,
                                "anchor": r["occurred_at"]}
    try:
        snaps = conn.execute("SELECT session_ref, created_at FROM snapshots"
                             " WHERE agent LIKE 'antigravity%' AND created_at IS NOT NULL"
                             " ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:
        snaps = []
    for r in snaps:
        ep = ts_to_epoch(r["created_at"])
        key = r["session_ref"] or ""
        if key.startswith("agy-src-"):
            key = key[len("agy-src-"):]
        if not ep or not key:
            continue
        cur = st.get(key)
        if not cur:
            st[key] = {"base_bytes": 0, "anchor": ep}
        elif not cur.get("anchor") or ep < cur["anchor"]:
            cur["anchor"] = ep
    return st


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
        # 主库升为 zcode 真相源的一次性迁移: model-io 零头事件与 zdb 全量重叠, 不删会双计
        zdb_active = bool(getattr(cv, "ZCODE_DB", None)) and Path(cv.ZCODE_DB).exists()
        try:
            mig = conn.execute("SELECT value FROM vault_meta WHERE key='zcode_zdb_primary_v1'"
                               ).fetchone()
        except sqlite3.OperationalError:
            mig = None
        if zdb_active and not mig:
            n_del = conn.execute("DELETE FROM usage_events WHERE source='zcode'"
                                 " AND raw LIKE '%\"requestId\"%'").rowcount
            _set_meta_flag(conn, "zcode_zdb_primary_v1")
            if n_del:
                print(f"  迁移: zcode 真相源升级为主库全量, 移除 model-io 零头事件 {n_del} 条 (防双计)")
        if "antigravity" in srcs:
            n_agv = agv_perstep_migration(conn)
            if n_agv:
                print(f"  迁移: AGV 记账升级为步级时间戳/差量, 移除旧会话级全量事件 {n_agv} 条"
                      " (防历史体积污染 5h 窗口)")
        if "zcode" in srcs:
            n_proj = zcode_project_backfill(conn, vault)
            if n_proj:
                print(f"  迁移: zcode 存量事件项目回填 {n_proj} 条 (session.directory/snapshots)")
        agv_state = _agv_base_state(conn) if "antigravity" in srcs else {}
        proj_map = _snapshot_proj_map(conn) if "zcode" in srcs else {}
        total_new = total_dup = 0
        for src in srcs:
            changed = []  # 真变更文件集: skip_fn 判未变更 → 采集器不读盘 (水位跳过)

            def _skip(p, s, _src=src):
                if _wm_changed(conn, _src, p, s):
                    changed.append((p, s))
                    return False
                return True

            if src == "antigravity":
                events, marks = collect_antigravity(skip_fn=_skip, agv_state=agv_state)
            elif src == "zcode":
                events, marks = collect_zcode(skip_fn=_skip, proj_map=proj_map)
            else:
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
        out.append({"account": key, "client": evs[0]["client"] or evs[0]["source"],
                    "events": len(evs), "used": int(sum(_row_weight(x) for x in evs)),
                    "weight": weight, "used_weighted": round(used_w, 1), "limit": limit,
                    "pct": round(pct, 4) if pct is not None else None,
                    "mode": "limit" if limit else "consumption",
                    "peak_5h": round(peak, 1) if peak else None,
                    "pct_of_peak": round(used_w / peak, 4) if peak else None,
                    "share": round(used_w / window_total, 4) if window_total else None,
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
        delta = dt.datetime.fromisoformat(iso) - dt.datetime.now().astimezone()
    except ValueError:
        return iso
    s = int(delta.total_seconds())
    if s <= 0:
        return "已恢复"
    h, m = s // 3600, (s % 3600) // 60
    if h >= 48:
        return f"{h / 24:.1f} 天后"
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
