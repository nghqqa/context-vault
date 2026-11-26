"""DeepSeek Harness (dsh) 路径编码、会话发现与五级无硬依赖 Zstd 帧编解码原语 (纯 Python 3.9+ stdlib)。"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
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


def _active_zstd_backend() -> str | None:
    """优先读取 cv_dsh.zstd_backend，兼容测试对 cv_dsh.zstd_backend 的 monkeypatch。"""
    dsh_mod = sys.modules.get("cv_dsh")
    fn = getattr(dsh_mod, "zstd_backend", zstd_backend) if dsh_mod is not None else zstd_backend
    return fn()


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


def _node_zstd_compress_frames(parts: list[bytes]) -> bytes:
    """在单次 Node 子进程中依次压缩多个非空字节段并拼接为连续 Zstd 帧 (写会话双帧减半子进程开销)。"""
    chunks = [bytes(p) for p in parts if p]
    if not chunks:
        return b""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node binary not found for zstd compression")
    stdin_payload = b"".join(struct.pack("<I", len(c)) + c for c in chunks)
    script = (
        "const fs=require('node:fs'),z=require('node:zlib');"
        "const inp=fs.readFileSync(0);"
        "const opts={params:{[z.constants.ZSTD_c_checksumFlag]:1}};"
        "const out=[];let off=0;"
        "while(off+4<=inp.length){"
        "const ln=inp.readUInt32LE(off);off+=4;"
        "out.push(z.zstdCompressSync(inp.subarray(off,off+ln),opts));"
        "off+=ln;"
        "}"
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
        raise RuntimeError(f"node zstd compress failed: {err}")
    return proc.stdout


def _node_zstd_compress(data: bytes) -> bytes:
    return _node_zstd_compress_frames([data])


def decompress_zstd_frames(raw: bytes, max_frames: int | None = None, backend: str | None = None) -> bytes:
    """扫描并解压完整 Zstd 帧 (安全忽略末尾因进程崩溃造成的撕裂帧)。"""
    frames, _torn = scan_zstd_frames(raw, max_frames=max_frames)
    if not frames:
        return b""
    bk = backend or _active_zstd_backend()
    if bk is None:
        raise RuntimeError(
            "读取 DeepSeek Harness .zstd 会话需要 zstd 解码器 "
            "(Node 22.18+ / Python 3.14+ / zstandard / pyzstd / zstd CLI)"
        )
    if bk == "stdlib":
        from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
        return b"".join(_stdlib_zstd.decompress(raw[s:e]) for s, e in frames)
    if bk == "zstandard":
        import io
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
    bk = backend or _active_zstd_backend()
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
