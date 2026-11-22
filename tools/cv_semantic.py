"""可选语义增强层 — Ollama / OpenAI 兼容向量配置、本地 LLM 记忆精炼与自动会话标题 (零第三方依赖)。

铁律:
- 默认完全关闭, 未配置环境变量或 vault/ai_config.json 时零网络请求、零额外文件;
- 对 snapshots/*.json 永远只读: 精炼记忆写 memory_items, 自动标题写 snapshots.summary_title (纯派生列);
- 支持 Ollama (http://127.0.0.1:11434/v1/...) 或任意 OpenAI 兼容端点。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

LLM_URL_ENV = "CV_LLM_URL"          # 如 http://127.0.0.1:11434/v1/chat/completions
LLM_MODEL_ENV = "CV_LLM_MODEL"      # 如 qwen2.5:7b
LLM_KEY_ENV = "CV_LLM_KEY"          # 可选 Bearer
LLM_TIMEOUT_ENV = "CV_LLM_TIMEOUT"  # 秒, 默认 30
OLLAMA_HOST_ENV = "CV_OLLAMA_HOST"  # 默认 http://127.0.0.1:11434

_VALID_KINDS = ("decision", "preference", "pitfall", "constraint")

# 常见低信息量泛化首句 (如 "看一下这个项目" / "了解当前项目" / "继续" 等)
_GENERIC_TITLE_PATTERNS = (
    re.compile(r"^(?:请)?(?:帮我)?(?:看|了解|熟悉|检查|分析|整理|总结)(?:一下)?(?:这个|当前|我们这?两?个)?(?:项目|仓库|代码|文件|材料|目录)[。！!？?\s]*$"),
    re.compile(r"^(?:继续|开始|hello|hi|hey|test|ping|ok|好的|谢谢|收到)[。！!？?\s]*$", re.IGNORECASE),
    re.compile(r"^[\W_]+$"),
)

_EMBED_MODEL_HINTS = ("nomic-embed", "bge-", "mxbai-embed", "snowflake-arctic-embed", "all-minilm", "embed")


def ai_config_path(vault: Path) -> Path:
    return Path(vault) / "ai_config.json"


def load_ai_config(vault: Path | None) -> dict:
    """读取 vault/ai_config.json (缺失/损坏返回空字典, 不报错)。"""
    if vault is None:
        return {}
    p = ai_config_path(vault)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ai_config(vault: Path, updates: dict) -> dict:
    """合并更新 vault/ai_config.json (原子写, 仅保留白名单配置键)。"""
    import cv
    allowed = {
        "embeddings_url", "embeddings_model", "embeddings_key",
        "llm_url", "llm_model", "llm_key", "ollama_host",
    }
    cur = load_ai_config(vault)
    for k, v in (updates or {}).items():
        if k not in allowed:
            continue
        if v is None or str(v).strip() == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v).strip()
    Path(vault).mkdir(parents=True, exist_ok=True)
    cv.atomic_write_text(ai_config_path(vault), json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    return cur


def _pick(env_name: str, cfg: dict, cfg_key: str, default: str = "") -> str:
    v = (os.environ.get(env_name) or "").strip()
    if v:
        return v
    cv_v = str(cfg.get(cfg_key) or "").strip()
    return cv_v if cv_v else default


def probe_ollama(host: str | None = None, timeout: float = 2.5) -> dict:
    """探测本机 Ollama 服务 (/api/tags) 与已拉取的模型列表。"""
    base = (host or os.environ.get(OLLAMA_HOST_ENV) or "http://127.0.0.1:11434").strip().rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags", headers={"User-Agent": "ContextVault/0.7"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")]
        embed_rec = next((m for m in models if any(h in m.lower() for h in _EMBED_MODEL_HINTS)), "")
        chat_rec = next((m for m in models if not any(h in m.lower() for h in _EMBED_MODEL_HINTS)), "")
        return {
            "ok": True,
            "host": base,
            "models": models,
            "recommended_embed_model": embed_rec or "nomic-embed-text",
            "recommended_llm_model": chat_rec or "qwen2.5:7b",
        }
    except Exception as e:
        return {
            "ok": False,
            "host": base,
            "models": [],
            "recommended_embed_model": "nomic-embed-text",
            "recommended_llm_model": "qwen2.5:7b",
            "error": str(e),
        }


def configure_ollama(
    vault: Path,
    host: str = "http://127.0.0.1:11434",
    embed_model: str | None = None,
    llm_model: str | None = None,
    enable_llm: bool = True,
) -> dict:
    """一键将 Ollama 向量端点 (/v1/embeddings) 与可选对话端点写入 vault/ai_config.json。"""
    base = (host or "http://127.0.0.1:11434").strip().rstrip("/")
    pr = probe_ollama(base)
    emb = (embed_model or pr.get("recommended_embed_model") or "nomic-embed-text").strip()
    updates = {
        "ollama_host": base,
        "embeddings_url": f"{base}/v1/embeddings",
        "embeddings_model": emb,
    }
    if enable_llm:
        lm = (llm_model or pr.get("recommended_llm_model") or "qwen2.5:7b").strip()
        updates["llm_url"] = f"{base}/v1/chat/completions"
        updates["llm_model"] = lm
    saved = save_ai_config(vault, updates)
    return {
        "configured": True,
        "ollama_reachable": pr["ok"],
        "models_detected": pr["models"],
        "embeddings_url": saved.get("embeddings_url", ""),
        "embeddings_model": saved.get("embeddings_model", ""),
        "llm_url": saved.get("llm_url", ""),
        "llm_model": saved.get("llm_model", ""),
        "next_steps": [] if pr["ok"] else [
            "安装 Ollama: https://ollama.com/download",
            f"拉取向量模型: ollama pull {emb}",
            f"可选拉取精炼模型: ollama pull {updates.get('llm_model', 'qwen2.5:7b')}",
        ],
    }


def semantic_status(vault: Path, probe: bool = False) -> dict:
    """汇总当前语义增强层配置状态 (密钥绝不明文回显)。"""
    from cv_dense import DenseProvider, EmbeddingCache
    cfg = load_ai_config(vault)
    dp = DenseProvider(vault=vault)
    llm = LocalLLMClient(vault=vault)
    cache_stats = {"cached_vectors": 0, "size_mb": 0.0}
    if (Path(vault) / "embeddings.db").is_file():
        ec = EmbeddingCache(vault)
        try:
            cache_stats = ec.stats()
        finally:
            ec.close()
    out = {
        "embeddings": {
            "enabled": dp.available,
            "url": dp.url,
            "model": dp.model,
            "has_key": bool(dp.key),
            "cache": cache_stats,
        },
        "llm": {
            "enabled": llm.available,
            "url": llm.url,
            "model": llm.model,
            "has_key": bool(llm.key),
        },
        "config_file": str(ai_config_path(vault)),
        "config_exists": ai_config_path(vault).is_file(),
    }
    if probe:
        out["ollama"] = probe_ollama(cfg.get("ollama_host") or None)
    return out


class LocalLLMClient:
    """OpenAI 兼容 /v1/chat/completions 极简客户端 (纯 stdlib, 默认关闭)。"""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        key: str | None = None,
        vault: Path | None = None,
        timeout: float | None = None,
    ):
        cfg = load_ai_config(vault)
        self.url = (url or _pick(LLM_URL_ENV, cfg, "llm_url")).strip()
        self.model = (model or _pick(LLM_MODEL_ENV, cfg, "llm_model", "qwen2.5:7b")).strip()
        self.key = (key or _pick(LLM_KEY_ENV, cfg, "llm_key")).strip()
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            try:
                self.timeout = float(os.environ.get(LLM_TIMEOUT_ENV, "30"))
            except ValueError:
                self.timeout = 30.0

    @property
    def available(self) -> bool:
        return bool(self.url)

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
        if not self.available:
            raise RuntimeError("LLM 端点未配置 (请设 CV_LLM_URL 或运行 cv semantic setup --ollama)")
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Content-Type": "application/json", "User-Agent": "ContextVault/0.7"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("LLM 返回空 choices")
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip()


def _extract_json_array(raw: str) -> list:
    """从 LLM 回复中宽容提取 JSON 数组 (支持 ```json 包裹)。"""
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        text = m.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
        return arr if isinstance(arr, list) else []
    except ValueError:
        return []


def _compact_dialog_excerpt(env: dict, max_chars: int = 3600) -> str:
    """将快照对话压缩为适合本地小模型精炼的紧凑摘录 (跳过超长 tool_result)。"""
    import cv
    lines = []
    used = 0
    for b in env.get("conversation") or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type", "")
        if bt in ("tool_call", "tool_result", "thinking"):
            continue
        role = b.get("role") or bt or "text"
        txt = (cv.block_text(b) or "").strip()
        if not txt:
            continue
        piece = f"[{role}] {txt[:420]}"
        if used + len(piece) > max_chars:
            break
        lines.append(piece)
        used += len(piece) + 1
    return "\n".join(lines)


def refine_memories_with_llm(
    vault: Path,
    limit: int = 10,
    snapshot_id: str | None = None,
    force: bool = False,
    client: LocalLLMClient | None = None,
) -> dict:
    """使用本地/兼容 LLM 对快照做隐式决策/踩坑/偏好/红线精炼, 幂等写入 memory_items。"""
    import cv
    import cv_memory

    llm = client or LocalLLMClient(vault=vault)
    if not llm.available:
        return {
            "ok": False,
            "error": "未配置 LLM 端点 (设置 CV_LLM_URL 或运行 cv semantic setup --ollama)",
            "scanned": 0,
            "added": 0,
        }

    conn = cv.db(vault)
    try:
        conn.executescript(cv.SCHEMA)
        cv_memory.ensure_memory_schema(conn)
        done = set()
        if not force and not snapshot_id:
            done = {
                r[0]
                for r in conn.execute("SELECT snapshot_id FROM memory_extracted WHERE engine='llm-refine'")
            }
        snap_dir = Path(vault) / "snapshots"
        if snapshot_id:
            files = [snap_dir / f"{snapshot_id}.json"]
        else:
            files = sorted(snap_dir.glob("cv_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if snap_dir.exists() else []

        sys_prompt = (
            "你是编程记忆提炼助手。从对话中仅提炼长期有价值的架构决策(decision)、用户偏好(preference)、"
            "踩坑根因(pitfall)、架构红线(constraint)。严禁编造。"
            "只输出 JSON 数组，每项格式: {\"kind\":\"decision|preference|pitfall|constraint\",\"content\":\"简洁中文结论\"}。"
            "没有值得长期记住的内容时输出 []。"
        )
        scanned, added, errors = 0, 0, []
        for f in files:
            if scanned >= max(1, int(limit)):
                break
            if not f.exists():
                continue
            sid = f.stem
            if sid in done:
                continue
            try:
                env = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            excerpt = _compact_dialog_excerpt(env)
            if len(excerpt) < 40:
                continue
            scanned += 1
            proj = (env.get("capture") or {}).get("project") or Path((env.get("capture") or {}).get("cwd") or "default").name or "default"
            ets = (env.get("created_at") or "")[:19] or None
            try:
                raw = llm.chat(sys_prompt, excerpt)
                items = _extract_json_array(raw)
            except Exception as e:
                errors.append(f"{sid}: {e}")
                continue

            for it in items[:10]:
                if not isinstance(it, dict):
                    continue
                k = str(it.get("kind") or "").strip().lower()
                c = str(it.get("content") or "").strip()
                if k not in _VALID_KINDS or len(c) < 6:
                    continue
                c = c[:400]
                dup = conn.execute(
                    "SELECT id FROM memory_items WHERE kind=? AND content=? AND status='active' LIMIT 1",
                    (k, c),
                ).fetchone()
                if dup:
                    conn.execute(
                        "UPDATE memory_items SET confidence=MIN(COALESCE(confidence,1.0)+0.3, 5.0) WHERE id=?",
                        (dup["id"],),
                    )
                    continue
                conn.execute(
                    "INSERT INTO memory_items(created_at,snapshot_id,kind,content,project,"
                    "event_ts,status,confidence) VALUES(?,?,?,?,?,?,'active',2.0)",
                    (cv.now_iso(), sid, k, c, proj, ets),
                )
                added += 1
            conn.execute(
                "INSERT OR REPLACE INTO memory_extracted(snapshot_id,engine,extracted_at) VALUES(?,?,?)",
                (sid, "llm-refine", cv.now_iso()),
            )
            conn.commit()
        superseded = cv_memory.recompute_supersession(conn)
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "model": llm.model,
        "scanned": scanned,
        "added": added,
        "superseded": superseded,
        "errors": errors[:5],
    }


def is_low_info_title(title: str) -> bool:
    """判断会话标题是否为低信息量泛化句 (如 '看一下这个项目' / '继续')。"""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if len(t) < 6:
        return True
    for pat in _GENERIC_TITLE_PATTERNS:
        if pat.match(t):
            return True
    return False


def heuristic_session_title(env: dict) -> str:
    """零依赖启发式标题提炼: 跳过寒暄/泛化首句, 优先提取含具体文件/模块/任务意图的一句话。"""
    import cv
    candidates = []
    for b in env.get("conversation") or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type", "")
        if bt not in ("text", "agent_message", "compaction"):
            continue
        txt = (cv.block_text(b) or "").strip()
        if not txt:
            continue
        for line in txt.splitlines():
            clean = re.sub(r"^[#>*`\-\s]+", "", line).strip()
            clean = re.sub(r"\s+", " ", clean)
            if len(clean) < 8 or is_low_info_title(clean):
                continue
            if clean.startswith(("{", "[", "Traceback", "File \"")):
                continue
            score = 0
            if b.get("role") == "user":
                score += 3
            if re.search(r"[A-Za-z0-9_]+\.(?:py|js|ts|tsx|go|rs|sql|json|md|yaml|toml)", clean):
                score += 4
            if re.search(r"(?:修复|重构|实现|设计|优化|排查|迁移|配置|新增|支持|升级|决定|方案)", clean):
                score += 3
            candidates.append((score, len(candidates), clean[:64]))
            if len(candidates) >= 24:
                break
        if len(candidates) >= 24:
            break
    if candidates:
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][2]
    proj = Path((env.get("capture") or {}).get("cwd") or "").name
    return f"{proj} 会话记录" if proj else "(无标题会话)"


def generate_session_titles(
    vault: Path,
    limit: int = 30,
    force: bool = False,
    use_llm: bool = True,
    client: LocalLLMClient | None = None,
) -> dict:
    """为低信息量标题的快照自动生成精准标题, 写回 snapshots.summary_title (永不改原快照 JSON)。"""
    import cv
    llm = client or (LocalLLMClient(vault=vault) if use_llm else None)
    llm_ready = bool(llm and llm.available)

    conn = cv.db(vault)
    updated, llm_used, heuristic_used = 0, 0, 0
    samples = []
    try:
        conn.executescript(cv.SCHEMA)
        rows = conn.execute(
            "SELECT id, path, summary_title, originator FROM snapshots ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)) * 4,),
        ).fetchall()
        for r in rows:
            if updated >= max(1, int(limit)):
                break
            cur_title = (r["summary_title"] or r["originator"] or "").strip()
            if not force and not is_low_info_title(cur_title):
                continue
            p = Path(r["path"]) if r["path"] else (Path(vault) / "snapshots" / f"{r['id']}.json")
            if not p.exists():
                p = Path(vault) / "snapshots" / f"{r['id']}.json"
            if not p.exists():
                continue
            try:
                env = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            new_title = ""
            if llm_ready:
                excerpt = _compact_dialog_excerpt(env, max_chars=1800)
                if excerpt:
                    try:
                        cand = llm.chat(
                            "为这段编程对话生成一个 10~26 字的精准中文标题，直接输出标题文本，不要引号或前缀。",
                            excerpt,
                        )
                        cand = re.sub(r"[\r\n\"'《》「」]+", " ", cand).strip()[:64]
                        if len(cand) >= 4 and not is_low_info_title(cand):
                            new_title = cand
                            llm_used += 1
                    except Exception:
                        pass
            if not new_title:
                new_title = heuristic_session_title(env)
                heuristic_used += 1
            if new_title and new_title != cur_title:
                conn.execute("UPDATE snapshots SET summary_title=? WHERE id=?", (new_title, r["id"]))
                updated += 1
                if len(samples) < 10:
                    samples.append({"id": r["id"], "old": cur_title[:40], "new": new_title})
        conn.commit()
    finally:
        conn.close()
    return {
        "updated": updated,
        "llm_used": llm_used,
        "heuristic_used": heuristic_used,
        "llm_enabled": llm_ready,
        "samples": samples,
    }
