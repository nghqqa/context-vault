"""DeepSeek Harness (dsh) 协议引擎: 路径编码、拼接式 Zstd 帧编解码、V4/V3/V2 读取与 V4 写回、工作区登记及 MCP Cordis 注入。

严格依据 deepseek-ai/deepseek-harness 源码 (format.ts / zstd.ts / validation.ts /
workspace / mcp-client / skill-filesystem) 实现, 保持纯 Python 3.9+ 标准库零强依赖:
- .jsonl.zstd 采用多级无硬依赖编解码链 (stdlib 3.14+ → zstandard → pyzstd → node:zlib → zstd CLI)
- 纯文本 .jsonl 读写无需任何外部模块或二进制。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from cv_core import cv_mod as _cv

ZSTD_MAGIC = 0xFD2FB528
_GEN_RE = re.compile(r"^session(?:\.v(\d+))?\.jsonl(\.zstd)?$")

_UNSET = object()
_ZSTD_BACKEND = _UNSET
_BACKEND_LOCK = threading.Lock()


# ------------------------------------------------------------------ 路径解析

def dsh_home() -> Path:
    """解析 DeepSeek Harness 主目录: $DSH_HOME (非空白) 优先, 否则 ~/.dsh。"""
    env_home = os.environ.get("DSH_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path(_cv().HOME) / ".dsh"


def dsh_sessions_root() -> Path:
    return dsh_home() / "sessions"


def dsh_skills_dir() -> Path:
    return dsh_home() / "skills"


def dsh_workspace_json() -> Path:
    return dsh_home() / "storages" / "workspace.json"


def dsh_projcache_dir() -> Path:
    return dsh_home() / "storages" / "session_projcache" / "sessions"


# ------------------------------------------------------------------ 路径编码 (1:1 对齐 format.ts)

def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _is_safe_ascii(code: int) -> bool:
    return (48 <= code <= 57) or (65 <= code <= 90) or (97 <= code <= 122) or code in (45, 46, 95)


def encode_segment(raw: str) -> str:
    """将任意会话 ID 编码为单个文件系统安全路径段 (按 UTF-16 码元转义 ~XXXX)。"""
    if not raw:
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    parts: list[str] = []
    for code in _utf16_units(raw):
        if _is_safe_ascii(code):
            parts.append(chr(code))
        else:
            parts.append(f"~{code:04X}")
    return "".join(parts)


def project_key(cwd: str) -> str:
    """构造人类可读的项目目录键: `/`, `\\`, `:` 折叠为 `-`, 前导 `-` 剥离, 限长 251 字符并包 `--...--`。"""
    if not cwd:
        raise ValueError("cannot encode an empty project path")
    parts: list[str] = []
    sep_run = False
    for code in _utf16_units(cwd):
        if code in (47, 92, 58):  # '/', '\\', ':'
            if not sep_run:
                parts.append("-")
            sep_run = True
        elif _is_safe_ascii(code):
            parts.append(chr(code))
            sep_run = False
        else:
            parts.append(f"~{code:04X}")
            sep_run = False
    slug = "".join(parts).lstrip("-") or "root"
    return f"--{slug[:251]}--"


def generation_info(filename: str) -> tuple[int, str] | None:
    """解析 `session[.vN].jsonl[.zstd]` → (version_int, 'zstd'|'none'), 不匹配返回 None。"""
    m = _GEN_RE.match(filename)
    if not m:
        return None
    ver = int(m.group(1) or 0)
    comp = "zstd" if m.group(2) else "none"
    return ver, comp


def discover_dsh_session_files(sessions_root: Path | None = None) -> list[Path]:
    """扫描 `$DSH_HOME/sessions/` 下全部会话, 每会话只取最高代际号 (.vN), 跳过 .segments 等隐藏目录。"""
    root = Path(sessions_root) if sessions_root is not None else dsh_sessions_root()
    if not root.is_dir():
        return []
    found: list[Path] = []
    try:
        proj_entries = list(os.scandir(root))
    except OSError:
        return []
    for proj_ent in proj_entries:
        if proj_ent.name.startswith("."):
            continue
        try:
            if not proj_ent.is_dir():
                continue
            sess_entries = list(os.scandir(proj_ent.path))
        except OSError:
            continue
        for sess_ent in sess_entries:
            if sess_ent.name.startswith("."):
                continue
            try:
                if not sess_ent.is_dir():
                    continue
                files = list(os.scandir(sess_ent.path))
            except OSError:
                continue
            best_rank: tuple[int, int] | None = None
            best_path: Path | None = None
            for f_ent in files:
                info = generation_info(f_ent.name)
                if info is None:
                    continue
                try:
                    if not f_ent.is_file() or f_ent.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                rank = (info[0], 1 if info[1] == "zstd" else 0)
                if best_rank is None or rank > best_rank:
                    best_rank = rank
                    best_path = Path(f_ent.path)
            if best_path is not None:
                found.append(best_path)
    return sorted(found)


def detect_root_compression(sessions_root: Path | None = None) -> str:
    """探测目标 sessions 根目录现有物理编码 (`'zstd'` 或 `'none'`), 空目录默认 `'zstd'` (DSH 严禁混放)。"""
    root = Path(sessions_root) if sessions_root is not None else dsh_sessions_root()
    if not root.is_dir():
        return "zstd"
    checked = 0
    saw_plain = False
    for f in discover_dsh_session_files(root):
        info = generation_info(f.name)
        if not info:
            continue
        if info[1] == "zstd":
            return "zstd"
        saw_plain = True
        checked += 1
        if checked >= 20:
            break
    return "none" if saw_plain else "zstd"


# ------------------------------------------------------------------ 拼接式 Zstd 帧扫描与五级编解码

def scan_zstd_frames(buf: bytes, max_frames: int | None = None) -> tuple[list[tuple[int, int]], int | None]:
    """纯 Python 定位拼接式 Zstandard 帧边界 (不解压块体)。

    1:1 对齐 session-persistence-jsonl/src/zstd.ts `scanZstdFrames`:
    - 完整帧加入 `frames` [(start, end), ...]
    - 崩溃截断造成的末尾不完整帧返回 `torn_start` (可安全跳过)
    - 非法帧魔数 / 保留帧头位 / 保留块类型抛出 ValueError
    """
    frames: list[tuple[int, int]] = []
    n = len(buf)
    offset = 0

    while offset < n:
        start = offset
        if n - offset < 4:
            return frames, start
        if int.from_bytes(buf[offset:offset + 4], "little") != ZSTD_MAGIC:
            raise ValueError(f"corrupt Zstandard session log: invalid frame magic at byte {offset}")
        offset += 4

        if offset == n:
            return frames, start
        descriptor = buf[offset]
        offset += 1
        if descriptor & 0x18:
            raise ValueError(f"corrupt Zstandard session log: reserved frame-header bit at byte {offset - 1}")

        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        checksum = bool(descriptor & 0x04)
        dict_flag = descriptor & 0x03
        dict_bytes = 4 if dict_flag == 3 else dict_flag
        content_size_bytes = (1 if single_segment else 0) if content_size_flag == 0 else (1 << content_size_flag)
        rem_header = (0 if single_segment else 1) + dict_bytes + content_size_bytes
        if n - offset < rem_header:
            return frames, start
        offset += rem_header

        while True:
            if n - offset < 3:
                return frames, start
            bh = buf[offset] | (buf[offset + 1] << 8) | (buf[offset + 2] << 16)
            offset += 3
            last_block = bool(bh & 1)
            block_type = (bh >> 1) & 0x03
            block_size = bh >> 3
            if block_type == 0x03:
                raise ValueError(f"corrupt Zstandard session log: reserved block type at byte {offset - 3}")
            payload_bytes = 1 if block_type == 1 else block_size
            if n - offset < payload_bytes:
                return frames, start
            offset += payload_bytes
            if last_block:
                break

        if checksum:
            if n - offset < 4:
                return frames, start
            offset += 4

        frames.append((start, offset))
        if max_frames is not None and len(frames) >= max_frames:
            return frames, None

    return frames, None


def reset_zstd_backend_cache() -> None:
    """重置 Zstd 后端探测缓存 (供测试切换后端使用)。"""
    global _ZSTD_BACKEND
    with _BACKEND_LOCK:
        _ZSTD_BACKEND = _UNSET


def _no_window_flags() -> int:
    try:
        return int(getattr(_cv(), "NO_WINDOW", 0))
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def zstd_backend() -> str | None:
    """按优先级探测本机可用的 Zstd 编解码器 (结果进程内缓存)。"""
    global _ZSTD_BACKEND
    with _BACKEND_LOCK:
        if _ZSTD_BACKEND is not _UNSET:
            return _ZSTD_BACKEND  # type: ignore[return-value]

        picked: str | None = None
        try:
            from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
            if hasattr(_stdlib_zstd, "compress") and hasattr(_stdlib_zstd, "decompress"):
                picked = "stdlib"
        except Exception:
            pass

        if picked is None:
            try:
                import zstandard as _zstd_mod  # noqa: F401
                picked = "zstandard"
            except Exception:
                pass

        if picked is None:
            try:
                import pyzstd as _pyzstd_mod  # noqa: F401
                picked = "pyzstd"
            except Exception:
                pass

        if picked is None:
            node = shutil.which("node")
            if node:
                try:
                    probe = subprocess.run(
                        [
                            node,
                            "-e",
                            "const z=require('node:zlib');"
                            "process.exit(typeof z.zstdCompressSync==='function'"
                            "&&typeof z.zstdDecompressSync==='function'?0:1);",
                        ],
                        capture_output=True,
                        timeout=5,
                        creationflags=_no_window_flags(),
                    )
                    if probe.returncode == 0:
                        picked = "node"
                except Exception:
                    pass

        if picked is None and shutil.which("zstd"):
            picked = "cli"

        _ZSTD_BACKEND = picked
        return picked


def _node_zstd_decompress(raw: bytes, frames: list[tuple[int, int]]) -> bytes:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node binary not found for zstd decompression")
    meta = json.dumps(frames, separators=(",", ":")).encode("utf-8")
    end_limit = frames[-1][1]
    stdin_payload = struct.pack("<I", len(meta)) + meta + raw[:end_limit]
    script = (
        "const fs=require('node:fs'),z=require('node:zlib');"
        "const all=fs.readFileSync(0);"
        "const hl=all.readUInt32LE(0);"
        "const frames=JSON.parse(all.subarray(4,4+hl).toString('utf8'));"
        "const b=all.subarray(4+hl);"
        "const out=frames.map(([s,e])=>z.zstdDecompressSync(b.subarray(s,e)));"
        "process.stdout.write(Buffer.concat(out));"
    )
    proc = subprocess.run(
        [node, "-e", script],
        input=stdin_payload,
        capture_output=True,
        timeout=30,
        creationflags=_no_window_flags(),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"node zstd decompress failed: {err}")
    return proc.stdout


def _node_zstd_compress(data: bytes) -> bytes:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node binary not found for zstd compression")
    script = (
        "const fs=require('node:fs'),z=require('node:zlib');"
        "const inp=fs.readFileSync(0);"
        "process.stdout.write(z.zstdCompressSync(inp,{params:{[z.constants.ZSTD_c_checksumFlag]:1}}));"
    )
    proc = subprocess.run(
        [node, "-e", script],
        input=data,
        capture_output=True,
        timeout=30,
        creationflags=_no_window_flags(),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"node zstd compress failed: {err}")
    return proc.stdout


def decompress_zstd_frames(raw: bytes, max_frames: int | None = None, backend: str | None = None) -> bytes:
    """扫描并解压完整 Zstd 帧 (安全忽略末尾因进程崩溃造成的撕裂帧)。"""
    frames, _torn = scan_zstd_frames(raw, max_frames=max_frames)
    if not frames:
        return b""
    bk = backend or zstd_backend()
    if bk is None:
        raise RuntimeError(
            "读取 DeepSeek Harness .zstd 会话需要 zstd 解码器 "
            "(Node 22.18+ / Python 3.14+ / zstandard / pyzstd / zstd CLI)"
        )
    if bk == "stdlib":
        from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
        return b"".join(_stdlib_zstd.decompress(raw[s:e]) for s, e in frames)
    if bk == "zstandard":
        import zstandard
        dctx = zstandard.ZstdDecompressor()
        chunks: list[bytes] = []
        for s, e in frames:
            with dctx.stream_reader(io.BytesIO(raw[s:e])) as rdr:
                chunks.append(rdr.read())
        return b"".join(chunks)
    if bk == "pyzstd":
        import pyzstd
        return b"".join(pyzstd.decompress(raw[s:e]) for s, e in frames)
    if bk == "node":
        return _node_zstd_decompress(raw, frames)
    if bk == "cli":
        cli = shutil.which("zstd")
        if not cli:
            raise RuntimeError("zstd CLI not found")
        proc = subprocess.run(
            [cli, "-d", "-c", "--no-progress"],
            input=raw[:frames[-1][1]],
            capture_output=True,
            timeout=30,
            creationflags=_no_window_flags(),
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"zstd CLI decompress failed: {err}")
        return proc.stdout
    raise RuntimeError(f"未知 zstd 后端: {bk!r}")


def compress_zstd_frame(data: bytes, backend: str | None = None) -> bytes:
    """将字节块压缩为单个带 32 位校验和的独立 Zstandard 帧 (对齐 DSH CHECKSUM_OPTIONS)。"""
    bk = backend or zstd_backend()
    if bk is None:
        raise RuntimeError(
            "写入 DeepSeek Harness .zstd 会话需要 zstd 编码器 "
            "(Node 22.18+ / Python 3.14+ / zstandard / pyzstd / zstd CLI)"
        )
    if bk == "stdlib":
        from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
        try:
            cp = getattr(_stdlib_zstd, "CompressionParameter", None)
            if cp is not None and hasattr(cp, "checksum_flag"):
                return _stdlib_zstd.compress(data, options={cp.checksum_flag: 1})
        except Exception:
            pass
        return _stdlib_zstd.compress(data)
    if bk == "zstandard":
        import zstandard
        return zstandard.ZstdCompressor(write_checksum=True).compress(data)
    if bk == "pyzstd":
        import pyzstd
        return pyzstd.compress(data, level_or_option={pyzstd.CParameter.checksumFlag: 1})
    if bk == "node":
        return _node_zstd_compress(data)
    if bk == "cli":
        cli = shutil.which("zstd")
        if not cli:
            raise RuntimeError("zstd CLI not found")
        proc = subprocess.run(
            [cli, "-c", "--check", "--no-progress"],
            input=data,
            capture_output=True,
            timeout=30,
            creationflags=_no_window_flags(),
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"zstd CLI compress failed: {err}")
        return proc.stdout
    raise RuntimeError(f"未知 zstd 后端: {bk!r}")


def read_dsh_text(path: Path, first_frame_only: bool = False) -> str:
    """读取 `.jsonl` 或 `.jsonl.zstd` 会话为 UTF-8 文本; 按需仅解码第 0 帧 (header)。"""
    raw = Path(path).read_bytes()
    is_zstd = raw.startswith(b"\x28\xb5\x2f\xfd") or str(path).endswith(".zstd")
    if is_zstd:
        if not raw:
            return ""
        raw = decompress_zstd_frames(raw, max_frames=1 if first_frame_only else None)
    return raw.decode("utf-8", errors="replace")


def first_dsh_jsonl(path: Path) -> dict:
    """只解第 0 帧并返回首行 JSON 对象, 用于轻量格式识别。"""
    text = read_dsh_text(path, first_frame_only=True)
    for line in text.splitlines():
        s = line.strip()
        if s:
            return json.loads(s)
    raise SystemExit(f"空文件: {path}")


def iter_dsh_jsonl(path: Path):
    """流式解析 DSH 会话行 (安全跳过空白与末尾半截 JSON 行)。"""
    text = read_dsh_text(path, first_frame_only=False)
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


# ------------------------------------------------------------------ 会话读取 (DSH V4/V3/V2 -> Vault 快照)

def _ms_to_iso(ms) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms < 0:
        return ""
    try:
        t = dt.datetime.fromtimestamp(float(ms) / 1000.0, tz=dt.timezone.utc)
        return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return ""


def _extract_text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text") or ""))
        return "\n".join(parts)
    return ""


def _stream_first_ms(stream) -> int | float | None:
    if not isinstance(stream, list):
        return None
    for item in stream:
        if not isinstance(item, dict):
            continue
        t = item.get("time")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            return t
        t0 = item.get("time0")
        if isinstance(t0, (int, float)) and not isinstance(t0, bool):
            return t0
    return None


def _dsh_usage_normalized(u: dict) -> tuple[int, int, int, int, int]:
    """返回 (tokens_in_uncached, tokens_out, cache_read, cache_write, reasoning_tokens)。

    DSH 官方实测口径与 Anthropic 一致: inputTokens 为纯未缓存输入, totalTokens = input + cacheRead + output。
    """
    raw_in = max(0, int(u.get("inputTokens") or 0))
    tout = max(0, int(u.get("outputTokens") or 0))
    cr = max(0, int(u.get("cacheReadTokens") or 0))
    cw = max(0, int(u.get("cacheWriteTokens") or 0))
    rt = max(0, int(u.get("reasoningTokens") or 0))
    tt = int(u.get("totalTokens") or 0)
    if tt > 0 and (raw_in + tout == tt) and (cr + cw > 0) and raw_in >= (cr + cw):
        tin = max(0, raw_in - cr - cw)
    else:
        tin = raw_in
    return tin, tout, cr, cw, rt


def _parse_tool_args(raw_args):
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except ValueError:
            return raw_args
    return raw_args


def export_dsh(path: Path, drop_developer: bool = False) -> dict:
    """将 DeepSeek Harness session[.vN].jsonl[.zstd] 只读导出为 Context Vault 规范会话结构。"""
    cv = _cv()
    blocks: list[dict] = []
    stats = cv.zero_stats()
    dropped = 0
    capture: dict = {
        "agent": "dsh",
        "source_format": "dsh-jsonl",
        "provider": None,
        "model": None,
        "session_ref": Path(path).parent.name,
        "completeness": "full",
    }
    header_ts = ""
    title_fallback: str | None = None
    title_llm: str | None = None
    seen_usage_ids: set[str] = set()
    last_compaction: str | None = None

    for obj in iter_dsh_jsonl(Path(path)):
        t = obj.get("type")
        if t == "session":
            if obj.get("id"):
                capture["session_ref"] = str(obj["id"])
            if obj.get("cwd"):
                capture["cwd"] = str(obj["cwd"])
            if obj.get("version") is not None:
                capture["agent_version"] = f"v{obj['version']}"
            header_ts = _ms_to_iso(obj.get("createdAt"))
            try:
                depth = int(obj.get("delegationDepth") or 0)
            except (TypeError, ValueError):
                depth = 0
            if obj.get("origin") == "subagent" or depth > 0:
                capture["is_subagent"] = True
                capture["parent_session_ref"] = str(obj.get("parentSession") or "dsh-subagent")
            continue

        data = obj.get("data")
        if not isinstance(data, dict):
            data = {}
        ts = _ms_to_iso(obj.get("time"))
        if not ts and t == "assistant/message":
            ts = _ms_to_iso(_stream_first_ms(data.get("stream")))
        if not ts:
            ts = header_ts

        if t == "session/title":
            ttl = str(data.get("title") or "").strip()
            if ttl:
                sk = str((data.get("source") or {}).get("kind") or "").strip().lower()
                if sk == "llm":
                    title_llm = ttl
                else:
                    title_fallback = ttl
        elif t in ("request/context", "request/header"):
            cfg = (data.get("header") or {}).get("config") if t == "request/header" else data
            if isinstance(cfg, dict):
                if cfg.get("provider") and not capture["provider"]:
                    capture["provider"] = str(cfg["provider"])
                if cfg.get("model") and not capture["model"]:
                    capture["model"] = str(cfg["model"])
        elif t in ("system/message", "developer/message"):
            dropped += 1
        elif t == "compaction/summary":
            s_txt = str(data.get("summary") or "").strip()
            if s_txt and s_txt != last_compaction:
                blocks.append({"type": "compaction", "summary": s_txt, "ts": ts})
                last_compaction = s_txt
        elif t == "user/message":
            src_kind = str((data.get("source") or {}).get("kind") or "").strip().lower()
            text = _extract_text_blocks(data.get("content"))
            if src_kind == "compact-checkpoint":
                stxt = text.strip()
                if stxt and stxt != last_compaction:
                    blocks.append({"type": "compaction", "summary": stxt, "ts": ts})
                    last_compaction = stxt
            elif src_kind in ("runtime-context", "skill-catalog", "time-context", "system-reminder"):
                if drop_developer:
                    dropped += 1
                elif text:
                    blocks.append({"type": "text", "role": "developer", "content": text, "ts": ts})
            elif text:
                blocks.append({"type": "text", "role": "user", "content": text, "ts": ts})
        elif t == "assistant/message":
            msg = data.get("message")
            if not isinstance(msg, dict):
                continue
            src = msg.get("source") or {}
            if isinstance(src, dict):
                if src.get("provider") and not capture["provider"]:
                    capture["provider"] = str(src["provider"])
                if src.get("model"):
                    capture["model"] = str(src["model"])
            u = data.get("usage")
            mid = str(msg.get("id") or f"seq:{obj.get('seq', len(blocks))}")
            if isinstance(u, dict) and ("inputTokens" in u or "outputTokens" in u) and mid not in seen_usage_ids:
                seen_usage_ids.add(mid)
                tin, tout, cr, cw, rt = _dsh_usage_normalized(u)
                stats["tokens_in"] += tin
                stats["tokens_out"] += tout
                stats["cache_read"] += cr
                stats["cache_write"] += cw
                stats["reasoning_tokens"] += rt
                stats["usage_events"] += 1
            for blk in (msg.get("content") or []):
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "reasoning":
                    rtxt = str(blk.get("text") or "").strip()
                    if rtxt:
                        blocks.append({"type": "thinking", "summary": rtxt, "ts": ts})
                elif bt == "text":
                    ttxt = str(blk.get("text") or "")
                    if ttxt:
                        blocks.append({"type": "text", "role": "assistant", "content": ttxt, "ts": ts})
                elif bt == "tool-call":
                    blocks.append({
                        "type": "tool_call",
                        "call_id": blk.get("id"),
                        "name": blk.get("name"),
                        "namespace": None,
                        "arguments": _parse_tool_args(blk.get("arguments")),
                        "ts": ts,
                    })
        elif t == "tool/result":
            msg = data.get("message")
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "tool" or "toolCallId" in msg:
                cid = msg.get("toolCallId") or (msg.get("source") or {}).get("callId")
                txt, _ = cv.clip(_extract_text_blocks(msg.get("content")))
                blocks.append({
                    "type": "tool_result",
                    "call_id": cid,
                    "content": txt,
                    "is_error": bool(msg.get("isError")),
                    "ts": ts,
                })
            elif isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if isinstance(item, dict) and item.get("type") == "tool-result":
                        cid = item.get("toolCallId") or (msg.get("source") or {}).get("callId")
                        txt, _ = cv.clip(_extract_text_blocks(item.get("content")))
                        blocks.append({
                            "type": "tool_result",
                            "call_id": cid,
                            "content": txt,
                            "is_error": bool(item.get("isError")),
                            "ts": ts,
                        })

    best_title = title_llm or title_fallback
    if best_title:
        capture["originator"] = best_title[:100]
    stats["total_tokens"] = (
        stats["tokens_in"] + stats["tokens_out"] + stats["cache_read"] + stats["cache_write"]
    )
    stats["blocks_total"] = len(blocks)
    stats["blocks_dropped"] = dropped
    return {"capture": capture, "conversation": blocks, "stats": stats}


# ------------------------------------------------------------------ 会话写入 (Vault -> DSH V4)

def _is_fully_qualified_cwd(p: str | None) -> bool:
    if not p or not isinstance(p, str):
        return False
    if os.name == "nt":
        drv, _ = os.path.splitdrive(p)
        return os.path.isabs(p) and bool(drv)
    return os.path.isabs(p)


def _norm_ws_path(p: str) -> str:
    if os.path.isdir(p):
        try:
            p = os.path.realpath(p)
        except OSError:
            pass
    return os.path.normcase(os.path.abspath(p))


def _resolve_canonical_cwd(raw_cwd: str | None) -> str:
    if _is_fully_qualified_cwd(raw_cwd):
        assert raw_cwd is not None
        if os.path.isdir(raw_cwd):
            try:
                return os.path.realpath(raw_cwd)
            except OSError:
                pass
        return os.path.abspath(raw_cwd)
    ws_file = dsh_workspace_json()
    if ws_file.is_file():
        try:
            ws = json.loads(ws_file.read_text(encoding="utf-8"))
            def_id = (ws.get("global") or {}).get("defaultWorkspaceId")
            workspaces = (ws.get("tables") or {}).get("workspaces") or {}
            cand = (workspaces.get(def_id) or {}).get("path") if def_id else None
            if _is_fully_qualified_cwd(cand):
                if os.path.isdir(cand):
                    return os.path.realpath(cand)
                return os.path.abspath(cand)
        except (OSError, ValueError, TypeError):
            pass
    return os.path.realpath(str(Path.cwd()))


def _iso_to_ms(ts: str | None, fallback_ms: int) -> int:
    if not ts or not isinstance(ts, str):
        return fallback_ms
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        ms = int(d.timestamp() * 1000)
        return ms if ms >= 0 else fallback_ms
    except (ValueError, OSError, OverflowError):
        return fallback_ms


def blocks_to_dsh(
    env: dict,
    strip_thinking: bool = False,
    session_id: str | None = None,
) -> tuple[dict, list[dict], int, str, str]:
    """将 Vault 快照转换为严格符合 DeepSeek Harness V4 校验契约的 (header, events, dropped, dsh_sid, cwd)。"""
    from cv_migrate import live_context

    cv = _cv()
    cap = env.get("capture") or {}
    base_id = str(session_id or uuid.uuid4())
    dsh_sid = base_id if base_id.startswith("session-") else f"session-{base_id}"
    canonical_cwd = _resolve_canonical_cwd(cap.get("cwd"))
    now_ms = int(time.time() * 1000)
    created_ms = _iso_to_ms(env.get("created_at"), now_ms)

    header = {
        "type": "session",
        "version": 4,
        "id": dsh_sid,
        "createdAt": created_ms,
        "cwd": canonical_cwd,
        "isSeeded": False,
        "delegationDepth": 0,
        "agentPreset": "standard",
    }

    provider = str(cap.get("provider") or "deepseek-official").strip() or "deepseek-official"
    model = str(cap.get("model") or "deepseek-flash").strip() or "deepseek-flash"

    events: list[dict] = []
    seq = 0
    cur_ms = created_ms
    dropped = 0
    known_calls: dict[str, int] = {}

    def emit(
        typ: str,
        data: dict,
        ts_hint: str | None = None,
        surface_op: str | None = None,
        source_event_seqs: list[int] | None = None,
    ) -> int:
        nonlocal seq, cur_ms
        t_ms = max(cur_ms, _iso_to_ms(ts_hint, cur_ms))
        cur_ms = t_ms
        ev: dict = {"type": typ, "seq": seq, "time": t_ms, "data": data}
        if surface_op is not None:
            ev["surfaceOp"] = surface_op
        if source_event_seqs is not None:
            ev["sourceEventSeqs"] = source_event_seqs
        events.append(ev)
        this_seq = seq
        seq += 1
        return this_seq

    emit("permission/preset", {"preset": "workspace-write"})
    emit("sandbox/mode", {"mode": "workspace-write"})
    emit("approval/policy", {"policy": "ask"})

    live, comp_summary = live_context(env.get("conversation") or [])
    items: list[dict] = []
    if comp_summary:
        items.append({
            "type": "text",
            "role": "user",
            "content": f"[context-vault 迁移摘要] 此前会话经过压缩, 摘要如下:\n\n{comp_summary}",
        })
    items.extend(live)

    turn = 0
    step = 0
    turn_open = False
    step_open = False
    saw_tool_result_in_step = False
    req_header_emitted = False
    title_emitted = False

    pending_asst: list[dict] = []
    pending_calls: list[tuple[str, str, str]] = []
    pending_ts: str | None = None

    def ensure_step(ts_hint: str | None = None) -> None:
        nonlocal turn, step, turn_open, step_open, saw_tool_result_in_step
        if not turn_open:
            turn += 1
            step = 1
            emit("turn/start", {"turn": turn}, ts_hint=ts_hint)
            emit("step/start", {"turn": turn, "step": step}, ts_hint=ts_hint)
            turn_open = True
            step_open = True
            saw_tool_result_in_step = False
        elif saw_tool_result_in_step or not step_open:
            if step_open:
                emit("step/end", {"turn": turn, "step": step}, ts_hint=ts_hint)
            step += 1
            emit("step/start", {"turn": turn, "step": step}, ts_hint=ts_hint)
            step_open = True
            saw_tool_result_in_step = False

    def flush_assistant() -> None:
        nonlocal pending_asst, pending_calls, pending_ts, req_header_emitted
        if not pending_asst:
            return
        ensure_step(pending_ts)
        if not req_header_emitted:
            emit(
                "request/header",
                {"header": {"config": {"provider": provider, "model": model}}, "reason": "initial"},
                ts_hint=pending_ts,
            )
            emit(
                "request/context",
                {"provider": provider, "model": model, "contextWindow": 128000},
                ts_hint=pending_ts,
            )
            req_header_emitted = True
        asst_seq = emit(
            "assistant/message",
            {
                "turn": turn,
                "step": step,
                "message": {
                    "role": "assistant",
                    "content": pending_asst,
                    "source": {"kind": "model", "provider": provider, "model": model},
                    "id": str(uuid.uuid4()),
                },
                "stream": [],
            },
            ts_hint=pending_ts,
            surface_op="append",
        )
        for cid, cname, cargs in pending_calls:
            known_calls[cid] = asst_seq
            emit(
                "tool/call",
                {"turn": turn, "step": step, "callId": cid, "name": cname, "arguments": cargs},
                ts_hint=pending_ts,
            )
        pending_asst = []
        pending_calls = []
        pending_ts = None

    def close_turn(ts_hint: str | None = None) -> None:
        nonlocal turn_open, step_open, saw_tool_result_in_step
        flush_assistant()
        if step_open:
            emit("step/end", {"turn": turn, "step": step}, ts_hint=ts_hint)
            step_open = False
        if turn_open:
            emit("turn/end", {"turn": turn, "reason": {"kind": "completed"}}, ts_hint=ts_hint)
            turn_open = False
        saw_tool_result_in_step = False

    def advance_step_after_tool(ts_hint: str | None = None) -> None:
        nonlocal step, step_open, saw_tool_result_in_step
        if not saw_tool_result_in_step:
            return
        flush_assistant()
        emit("step/end", {"turn": turn, "step": step}, ts_hint=ts_hint)
        step += 1
        emit("step/start", {"turn": turn, "step": step}, ts_hint=ts_hint)
        step_open = True
        saw_tool_result_in_step = False

    fallback_title = cv._derive_summary_title(env)

    for blk in items:
        if not isinstance(blk, dict):
            continue
        bt = blk.get("type")
        bts = blk.get("ts")
        if bt == "text":
            role = blk.get("role")
            if role == "developer":
                dropped += 1
                continue
            txt = str(blk.get("content") or "")
            if role == "user":
                close_turn(bts)
                turn += 1
                step = 1
                emit("turn/start", {"turn": turn}, ts_hint=bts)
                emit("step/start", {"turn": turn, "step": step}, ts_hint=bts)
                turn_open = True
                step_open = True
                saw_tool_result_in_step = False
                u_seq = emit(
                    "user/message",
                    {
                        "content": [{"type": "text", "text": txt}],
                        "source": {"kind": "user"},
                        "role": "user",
                        "id": str(uuid.uuid4()),
                    },
                    ts_hint=bts,
                    surface_op="append",
                )
                if not title_emitted:
                    title = (fallback_title or " ".join(txt.split()) or "Migrated session")[:80]
                    emit(
                        "session/title",
                        {"title": title, "messageSeqs": [u_seq], "source": {"kind": "fallback"}},
                        ts_hint=bts,
                    )
                    title_emitted = True
            else:
                advance_step_after_tool(bts)
                pending_asst.append({"type": "text", "text": txt})
                pending_ts = pending_ts or bts
        elif bt == "thinking":
            if strip_thinking:
                continue
            rtxt = str(blk.get("summary") or "").strip()
            if not rtxt:
                continue
            advance_step_after_tool(bts)
            pending_asst.append({"type": "reasoning", "text": rtxt})
            pending_ts = pending_ts or bts
        elif bt == "agent_message":
            txt = str(blk.get("content") or "")
            if not txt:
                continue
            advance_step_after_tool(bts)
            pending_asst.append({
                "type": "text",
                "text": f"[{blk.get('author')} → {blk.get('recipient')}] {txt}",
            })
            pending_ts = pending_ts or bts
        elif bt == "tool_call":
            advance_step_after_tool(bts)
            cid = str(blk.get("call_id") or "").strip() or f"call_{uuid.uuid4().hex[:16]}"
            cname = str(blk.get("name") or "").strip() or "tool"
            raw_args = blk.get("arguments")
            if isinstance(raw_args, str):
                args_str = raw_args
            elif raw_args is None:
                args_str = "{}"
            else:
                args_str = json.dumps(raw_args, ensure_ascii=False)
            pending_asst.append({
                "type": "tool-call",
                "id": cid,
                "name": cname,
                "arguments": args_str,
            })
            pending_calls.append((cid, cname, args_str))
            pending_ts = pending_ts or bts
        elif bt == "tool_result":
            flush_assistant()
            cid = str(blk.get("call_id") or "").strip()
            if not cid or cid not in known_calls:
                dropped += 1
                continue
            if not turn_open or not step_open:
                ensure_step(bts)
            emit(
                "tool/result",
                {
                    "turn": turn,
                    "step": step,
                    "message": {
                        "role": "tool",
                        "source": {"kind": "tool", "callId": cid},
                        "toolCallId": cid,
                        "content": [{"type": "text", "text": str(blk.get("content") or "")}],
                        "isError": bool(blk.get("is_error")),
                        "id": str(uuid.uuid4()),
                    },
                },
                ts_hint=bts,
                surface_op="append",
                source_event_seqs=[known_calls[cid]],
            )
            saw_tool_result_in_step = True

    close_turn()
    return header, events, dropped, dsh_sid, canonical_cwd


def write_dsh_session(
    dest: Path,
    header: dict,
    events: list[dict],
    compression: str = "zstd",
    backend: str | None = None,
) -> None:
    """原子写出 DeepSeek Harness V4 会话文件 (纯文本 .jsonl 或带校验和双帧 .jsonl.zstd)。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    hdr_text = json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n"
    ev_text = (
        "\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events)
        + ("\n" if events else "")
    )
    if compression == "zstd":
        hdr_frame = compress_zstd_frame(hdr_text.encode("utf-8"), backend=backend)
        ev_frame = compress_zstd_frame(ev_text.encode("utf-8"), backend=backend) if ev_text else b""
        payload = hdr_frame + ev_frame
        tmp = dest.parent / f".{dest.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    else:
        _cv().atomic_write_text(dest, hdr_text + ev_text)


def invalidate_dsh_projcache(dsh_sid: str, projcache_dir: Path | None = None) -> None:
    """清理过期的投影缓存 json, 强制 DSH 下次加载从最新会话文件重建视图。"""
    p = (Path(projcache_dir) if projcache_dir is not None else dsh_projcache_dir()) / f"{dsh_sid}.json"
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def register_dsh_workspace(
    dsh_sid: str,
    canonical_cwd: str,
    workspace_path: Path | None = None,
) -> bool:
    """若 `$DSH_HOME/storages/workspace.json` 已存在, 幂等登记迁入会话到匹配或新建工作区。

    若 workspace.json 尚未初始化, 返回 False (DSH 首次启动 bootstrap() 会自动扫描 sessions/ 并收录)。
    """
    ws_file = Path(workspace_path) if workspace_path is not None else dsh_workspace_json()
    if not ws_file.is_file():
        return False
    raw = ws_file.read_text(encoding="utf-8")
    ws = json.loads(raw)
    if not isinstance(ws, dict):
        return False
    gl = ws.get("global")
    tables = ws.get("tables")
    workspaces = tables.get("workspaces") if isinstance(tables, dict) else None
    if not isinstance(gl, dict) or not isinstance(workspaces, dict):
        return False

    bak = ws_file.with_suffix(".json.bak-cv")
    if not bak.exists():
        _cv().atomic_write_text(bak, raw)

    arch = gl.get("archivedSessionIds")
    if isinstance(arch, list) and dsh_sid in arch:
        gl["archivedSessionIds"] = [s for s in arch if s != dsh_sid]

    target_norm = _norm_ws_path(canonical_cwd)
    matched_id: str | None = None
    for wid, rec in list(workspaces.items()):
        if not isinstance(rec, dict):
            continue
        rpath = rec.get("path")
        if isinstance(rpath, str) and _norm_ws_path(rpath) == target_norm:
            matched_id = wid
            break

    # DSH validateStoredState 禁止同一 sessionId 同时归属于多个 workspace
    for wid, rec in list(workspaces.items()):
        if not isinstance(rec, dict) or wid == matched_id:
            continue
        sids = rec.get("sessionIds")
        if isinstance(sids, list) and dsh_sid in sids:
            rec["sessionIds"] = [s for s in sids if s != dsh_sid]

    now_z = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if matched_id is not None:
        rec = workspaces[matched_id]
        sids = rec.get("sessionIds")
        if not isinstance(sids, list):
            sids = []
        if dsh_sid not in sids:
            rec["sessionIds"] = [dsh_sid] + sids
        rec["updatedAt"] = now_z
    else:
        ws_id = str(uuid.uuid4())
        title = Path(canonical_cwd).name or canonical_cwd
        workspaces[ws_id] = {
            "path": canonical_cwd,
            "title": title,
            "sessionIds": [dsh_sid],
            "createdAt": now_z,
            "updatedAt": now_z,
        }
        wids = gl.get("workspaceIds")
        if not isinstance(wids, list):
            gl["workspaceIds"] = [ws_id]
        elif ws_id not in wids:
            gl["workspaceIds"] = [ws_id] + wids

    _cv().atomic_write_text(ws_file, json.dumps(ws, ensure_ascii=False, indent=2) + "\n")
    return True


# ------------------------------------------------------------------ MCP Cordis Patch 接入

def _has_cv_patch(p: Path) -> bool:
    try:
        if not p.is_file():
            return False
        txt = p.read_text(encoding="utf-8", errors="replace")
        return ("memory-context-vault" in txt) or ("context-vault" in txt)
    except OSError:
        return False


def dsh_mcp_status() -> dict:
    """结构化检测 DeepSeek Harness MCP 配置状态 (全局 $DSH_HOME/cordis.patch.yml 与各 profile)。"""
    root = dsh_home()
    app_det = bool(_cv()._detect_mcp_client_app("dsh"))
    detected = bool(root.exists() or app_det)
    home_cfg = root / "cordis.patch.yml"
    matched_cfg: Path | None = home_cfg if _has_cv_patch(home_cfg) else None
    if matched_cfg is None:
        profiles_dir = root / "profiles"
        if profiles_dir.is_dir():
            for cand in sorted(profiles_dir.glob("*/cordis.patch.yml")):
                if _has_cv_patch(cand):
                    matched_cfg = cand
                    break
    return {
        "id": "dsh",
        "name": "DeepSeek Harness",
        "detected": detected,
        "app_detected": app_det,
        "residual": bool(detected and not app_det),
        "installed": matched_cfg is not None,
        "config": str(matched_cfg or home_cfg),
    }


def install_mcp_dsh(vault: Path) -> str:
    """幂等写入 `$DSH_HOME/cordis.patch.yml` (全局跨 profile 生效), 安全处理注释与空列表 `[]`。"""
    root = dsh_home()
    if not root.exists():
        if _cv()._detect_mcp_client_app("dsh"):
            root.mkdir(parents=True, exist_ok=True)
        else:
            return "未找到 ~/.dsh (DeepSeek Harness 未安装)"
    cfg = root / "cordis.patch.yml"
    existing = cfg.read_text(encoding="utf-8", errors="replace") if cfg.exists() else ""
    if _has_cv_patch(cfg):
        return "已安装 (跳过)"

    # 若任意 profile 下已手工接过, 也跳过以免重复启动同名 serverName
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for cand in sorted(profiles_dir.glob("*/cordis.patch.yml")):
            if _has_cv_patch(cand):
                return f"已安装于 {cand.parent.name} profile (跳过)"

    if cfg.exists():
        cfg.with_suffix(".yml.bak-cv").write_text(existing, encoding="utf-8")

    py, args = _cv()._mcp_cmd_parts(vault)
    args_json = ", ".join(json.dumps(str(a), ensure_ascii=False) for a in args)
    patch_block = (
        "- insert:\n"
        "    - id: memory-context-vault\n"
        '      name: "@deepseek-ai/dsh-mcp-client"\n'
        "      config:\n"
        "        serverName: context-vault\n"
        "        transport: stdio\n"
        f"        command: {json.dumps(str(py), ensure_ascii=False)}\n"
        f"        args: [{args_json}]\n"
    )

    norm = existing.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.splitlines()
    active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not active or "".join(x.strip() for x in active) == "[]":
        comments = [ln for ln in lines if ln.lstrip().startswith("#")]
        prefix = ("\n".join(comments) + "\n") if comments else ""
        new_text = prefix + patch_block
    else:
        body = norm.rstrip("\n")
        new_text = body + "\n\n" + patch_block

    _cv().atomic_write_text(cfg, new_text)
    note = "已备份 .yml.bak-cv; " if existing else ""
    return f"已写入 {cfg} ({note}跨 profile 生效; 重启 DeepSeek Harness 生效)"


def _split_yaml_list_blocks(lines: list[str], indent: int) -> tuple[list[str], list[tuple[list[str], list[str]]]]:
    """按指定缩进的 `- ` 项切分 YAML 列表, 返回 (prefix_lines, [(item_lines, trailing_sep_lines), ...])。"""
    prefix: list[str] = []
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    ind_str = " " * indent
    for ln in lines:
        is_start = (
            len(ln) > indent
            and ln[:indent] == ind_str
            and (ln[indent:] == "-" or ln[indent:].startswith(("- ", "-\t")))
        )
        if is_start:
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is None:
            prefix.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)

    result: list[tuple[list[str], list[str]]] = []
    for blk in blocks:
        trail: list[str] = []
        while blk and (
            not blk[-1].strip()
            or (blk[-1].lstrip().startswith("#") and (len(blk[-1]) - len(blk[-1].lstrip())) <= indent)
        ):
            trail.insert(0, blk.pop())
        result.append((blk, trail))
    return prefix, result


def _is_cv_cordis_item(item_lines: list[str]) -> bool:
    for ln in item_lines:
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        if re.match(r"^(?:-\s+)?id:\s*[\"']?memory-context-vault[\"']?\s*(?:#.*)?$", s):
            return True
        if re.match(r"^serverName:\s*[\"']?context-vault[\"']?\s*(?:#.*)?$", s):
            return True
    return False


def _remove_cv_from_cordis_yaml(raw: str) -> tuple[str, bool]:
    """从 cordis.patch.yml 中精确移除 context-vault 注入项, 保留其余用户 patch 与注释。"""
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.splitlines()
    # 查找顶层列表缩进 (常规为 0)
    top_indent: int | None = None
    for ln in lines:
        s = ln.lstrip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(\s*)-(?:\s|$)", ln)
        if m:
            top_indent = len(m.group(1))
        break
    if top_indent is None:
        return raw, False

    prefix, top_blocks = _split_yaml_list_blocks(lines, top_indent)
    kept: list[tuple[list[str], list[str]]] = []
    removed = False
    trailing_comments: list[str] = []

    for blk, trail in top_blocks:
        if blk and re.match(r"^\s*-\s+insert:\s*(?:#.*)?$", blk[0]):
            sub_lines = blk[1:]
            child_indent: int | None = None
            for sln in sub_lines:
                ss = sln.lstrip()
                if not ss or ss.startswith("#"):
                    continue
                cm = re.match(r"^(\s+)-(?:\s|$)", sln)
                if cm:
                    child_indent = len(cm.group(1))
                break
            if child_indent is not None:
                sub_prefix, sub_blocks = _split_yaml_list_blocks(sub_lines, child_indent)
                kept_sub: list[tuple[list[str], list[str]]] = []
                blk_removed = False
                for sb, sb_trail in sub_blocks:
                    if _is_cv_cordis_item(sb):
                        blk_removed = True
                    else:
                        kept_sub.append((sb, sb_trail))
                if blk_removed:
                    removed = True
                    if not kept_sub:
                        trailing_comments.extend(ln for ln in trail if ln.lstrip().startswith("#"))
                        continue
                    rebuilt = [blk[0], *sub_prefix]
                    for j, (sb, sb_trail) in enumerate(kept_sub):
                        rebuilt.extend(sb)
                        if j < len(kept_sub) - 1:
                            rebuilt.extend(sb_trail)
                        else:
                            rebuilt.extend(ln for ln in sb_trail if ln.lstrip().startswith("#"))
                    kept.append((rebuilt, trail))
                    continue
        if _is_cv_cordis_item(blk):
            removed = True
            trailing_comments.extend(ln for ln in trail if ln.lstrip().startswith("#"))
            continue
        kept.append((blk, trail))

    if not removed:
        return raw, False

    if not kept:
        comments = [ln for ln in (*prefix, *trailing_comments) if ln.lstrip().startswith("#")]
        return ("\n".join(comments) + "\n" if comments else "") + "[]\n", True

    out = list(prefix)
    for i, (blk, trail) in enumerate(kept):
        out.extend(blk)
        if i < len(kept) - 1:
            out.extend(trail if trail else [""])
        else:
            out.extend(ln for ln in trail if ln.lstrip().startswith("#"))
    out.extend(trailing_comments)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return ((body + "\n") if body else "[]\n"), True


def uninstall_mcp_dsh(vault: Path) -> str:
    """安全移除 `$DSH_HOME/cordis.patch.yml` (及各 profile 下) 的 context-vault MCP 条目。"""
    root = dsh_home()
    cands: list[Path] = [root / "cordis.patch.yml"]
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        cands.extend(sorted(profiles_dir.glob("*/cordis.patch.yml")))

    removed_paths: list[str] = []
    for cfg in cands:
        if not _has_cv_patch(cfg):
            continue
        raw = cfg.read_text(encoding="utf-8", errors="replace")
        new_text, changed = _remove_cv_from_cordis_yaml(raw)
        if not changed:
            continue
        cfg.with_suffix(".yml.bak-cv").write_text(raw, encoding="utf-8")
        _cv().atomic_write_text(cfg, new_text)
        removed_paths.append(str(cfg))

    if removed_paths:
        return f"已从 {', '.join(removed_paths)} 移除 context-vault (备份 .yml.bak-cv)"
    return "未安装 (无需移除)"
