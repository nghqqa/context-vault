"""v0 基线快照 (Baseline 0 对照组) — 升级前 cv_index.py 的原样语义副本。

W2 内核升级时, v0 的词集交集打分与规则记忆被替换; 本文件保持其原始实现逐字不变,
供 benchmarks/run_bench.py 在完全相同的语料上复现升级前行为 (消融实验 Baseline 0)。
只读对照件, 不参与运行时。
"""

from __future__ import annotations

import json


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
    """块的检索文本 (v0: tool_result 截 500 字符, tool_call 参数截 200)。"""
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
    """v0 词集交集: 词项 ∈ set(tokenize(块文本)) 计 1 分/词项/块 — 无 IDF / 无长度归一 /
    无块角色加权, 500 行 tool_result 日志与 2 行关键拍板同权。"""
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


# ---------------- v0 规则记忆 (RuleMemoryEngine 原样语义) ----------------

DECISION_MARKERS = ("决定", "决策", "采用", "方案是", "结论是", "结论：", "选型",
                    "我们选", "定了", "agreed", "we decide", "let's go with",
                    "chose", "final answer", "the plan is")
PREFERENCE_MARKERS = ("不要", "必须", "总是", "记住", "以后都", "每次都",
                      "never ", "always ", "remember to", "from now on")
MAX_MEMORY_PER_SNAPSHOT = 40


def _memory_line_noise(s: str) -> bool:
    if s.endswith(("?", "？")):
        return True
    if s[0] in "-*+#>" or (len(s) > 1 and s[0].isdigit() and s[1] in ".、)）"):
        return True
    return s.count("`") >= 2


def extract_memories_v0(env: dict) -> list:
    """v0 规则提取 (丢失 block_idx / 快照真实时间 / 覆盖关系 — 这正是 W2 修掉的缺陷)。"""
    items, sid = [], env.get("_sid") or "?"
    project = None
    cwd = env["capture"].get("cwd")
    if cwd:
        project = str(cwd).rstrip("\\").rsplit("\\", 1)[-1] or str(cwd)
    seen = set()
    for i, b in enumerate(env["conversation"]):
        kind, text = None, ""
        if b["type"] == "compaction":
            kind, text = "summary", b.get("summary", "")
        elif b["type"] == "text" and b.get("role") == "assistant":
            text = b.get("content", "")
            kind = "decision" if any(m in text.lower() for m in DECISION_MARKERS) else None
        elif b["type"] == "text" and b.get("role") == "user":
            text = b.get("content", "")
            kind = "preference" if any(m in text.lower() for m in PREFERENCE_MARKERS) else None
        if not kind or not text:
            continue
        in_fence = False
        for sent in text.replace("\r", "").split("\n"):
            s = sent.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not (12 <= len(s) <= 400):
                continue
            if kind != "summary" and _memory_line_noise(s):
                continue
            low = s.lower()
            if kind == "summary" \
               or (kind == "decision" and any(m in low[:32] for m in DECISION_MARKERS)) \
               or (kind == "preference" and any(m in low[:24] for m in PREFERENCE_MARKERS)):
                key = s[:120]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"snapshot_id": sid, "kind": kind, "content": s,
                              "project": project, "block_idx": i})
        if len(items) >= MAX_MEMORY_PER_SNAPSHOT:
            break
    return items


def memory_search_v0(rows: list, query: str, limit: int) -> list:
    """v0 记忆检索: 子串计数 + summary 固定 +3, 无 active/superseded 概念 (过期记忆同权返回)。"""
    terms = tokenize(query)
    scored = []
    for r in rows:
        low = r["content"].lower()
        n = sum(low.count(t) for t in terms)
        if n:
            scored.append((n + (3 if r["kind"] == "summary" else 0), r["id"], r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out, seen = [], set()
    for n, _, r in scored:
        key = (r["kind"], (r["content"] or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": r["kind"], "content": r["content"], "project": r["project"],
                    "snapshot": r["snapshot_id"], "score": n})
        if len(out) >= limit:
            break
    return out


def extract_memories_v0_loose(env: dict) -> list:
    """v0 宽松规则 = v0.1 代码去掉行级噪音闸门 (消融对照: 量化硬闸门前的召回基线)。"""
    items, sid = [], env.get("_sid") or "?"
    project = None
    cwd = env["capture"].get("cwd")
    if cwd:
        project = str(cwd).rstrip("\\").rsplit("\\", 1)[-1] or str(cwd)
    seen = set()
    for i, b in enumerate(env["conversation"]):
        kind, text = None, ""
        if b["type"] == "compaction":
            kind, text = "summary", b.get("summary", "")
        elif b["type"] == "text" and b.get("role") == "assistant":
            text = b.get("content", "")
            kind = "decision" if any(m in text.lower() for m in DECISION_MARKERS) else None
        elif b["type"] == "text" and b.get("role") == "user":
            text = b.get("content", "")
            kind = "preference" if any(m in text.lower() for m in PREFERENCE_MARKERS) else None
        if not kind or not text:
            continue
        in_fence = False
        for sent in text.replace("\r", "").split("\n"):
            s = sent.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not (12 <= len(s) <= 400):
                continue
            low = s.lower()
            if kind == "summary" \
               or (kind == "decision" and any(m in low[:32] for m in DECISION_MARKERS)) \
               or (kind == "preference" and any(m in low[:24] for m in PREFERENCE_MARKERS)):
                key = s[:120]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"snapshot_id": sid, "kind": kind, "content": s,
                              "project": project, "block_idx": i})
        if len(items) >= MAX_MEMORY_PER_SNAPSHOT:
            break
    return items
