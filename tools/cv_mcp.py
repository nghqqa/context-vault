"""Context Vault MCP 客户端接入与状态检测 (纯 Python 3.9+ 标准库)。

由 tools/cv.py 拆出，集中管理 Claude Code / Codex / Cursor / Antigravity /
DeepSeek Harness 的 MCP 配置写入、卸载、主程序探测与 CLI/HTTP API。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import cv_core

_cv = cv_core.cv_mod


def _mcp_cmd_parts(vault: Path) -> tuple:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ["--mcp", "--vault", str(vault.resolve())]
    server = (Path(__file__).resolve().parent / "mcp_server.py")
    py = "python"
    return py, [str(server), "--vault", str(vault.resolve())]


def _has_local_bin_cmd(home: Path, names: tuple[str, ...]) -> str | None:
    local_bin = home / ".local" / "bin"
    if not local_bin.is_dir():
        return None
    exts = ("", ".exe", ".cmd", ".ps1", ".bat") if sys.platform.startswith("win") else ("",)
    for name in names:
        for ext in exts:
            cand = local_bin / f"{name}{ext}"
            if cand.is_file():
                return str(cand)
    return None


def _is_real_home_dir() -> bool:
    home = _cv().HOME
    try:
        return Path(home).resolve() == Path.home().resolve()
    except OSError:
        return str(home) == str(Path.home())


def _detect_mcp_client_app(client_id: str) -> bool:
    """探测客户端主程序/CLI 是否真实存在于本机, 避免将卸载残留配置目录误判为已安装应用。"""
    home = Path(_cv().HOME)
    real_home = _is_real_home_dir()

    def _has_cli(names: tuple[str, ...]) -> bool:
        if _has_local_bin_cmd(home, names):
            return True
        return bool(real_home and any(shutil.which(n) for n in names))

    pf_dirs = [Path(os.environ[k]) for k in ("ProgramFiles", "ProgramFiles(x86)") if real_home and os.environ.get(k)]

    if client_id == "claude":
        return _has_cli(("claude",))
    if client_id == "codex":
        if _has_cli(("codex",)):
            return True
        for ext_dir in (home / ".vscode" / "extensions", home / ".vscode-insiders" / "extensions"):
            if ext_dir.is_dir() and any(ext_dir.glob("openai.chatgpt-*")):
                return True
        return False
    if client_id == "cursor":
        if _has_cli(("cursor",)):
            return True
        cands = [
            home / "AppData" / "Local" / "Programs" / "cursor" / "Cursor.exe",
            home / "Applications" / "Cursor.app",
        ]
        if real_home:
            cands.extend(d / "cursor" / "Cursor.exe" for d in pf_dirs)
            cands.extend((Path("/Applications/Cursor.app"), Path("/usr/share/cursor"), Path("/opt/cursor")))
        return any(p.exists() for p in cands)
    if client_id == "antigravity":
        if _has_cli(("antigravity", "agy")):
            return True
        cands = [
            home / "AppData" / "Local" / "Programs" / "Antigravity" / "Antigravity.exe",
            home / "Applications" / "Antigravity.app",
        ]
        if real_home:
            cands.extend(d / "Antigravity" / "Antigravity.exe" for d in pf_dirs)
            cands.extend((Path("/Applications/Antigravity.app"), Path("/usr/share/antigravity"), Path("/opt/antigravity")))
        return any(p.exists() for p in cands)
    if client_id == "dsh":
        if _has_cli(("dsh",)):
            return True
        cands = [
            home / "AppData" / "Local" / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe",
            home / "Applications" / "DeepSeek Harness.app",
        ]
        if real_home:
            cands.extend(d / "DeepSeek Harness" / "DeepSeek Harness.exe" for d in pf_dirs)
            cands.extend((Path("/Applications/DeepSeek Harness.app"), Path("/usr/share/deepseek-harness"),
                          Path("/opt/deepseek-harness")))
        return any(p.exists() for p in cands)
    return False


def _claude_exe() -> str | None:
    local_cmd = _has_local_bin_cmd(Path(_cv().HOME), ("claude",))
    if local_cmd:
        return local_cmd
    if _is_real_home_dir():
        return shutil.which("claude")  # npm .cmd shim 需 which 解析全路径
    return None


def _install_mcp_claude(vault: Path) -> str:
    exe = _claude_exe()
    if not exe:
        return "未找到 claude CLI (跳过)"
    c = _cv()
    r = c.subprocess.run([exe, "mcp", "list"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", creationflags=c.NO_WINDOW)
    if "context-vault" in (r.stdout or ""):
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    r = c.subprocess.run([exe, "mcp", "add", "context-vault", "-s", "user", "--", py] + args,
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         creationflags=c.NO_WINDOW)
    return "已写入 ~/.claude.json (user scope)" if r.returncode == 0 else f"失败: {(r.stderr or r.stdout)[:120]}"


def _install_mcp_codex(vault: Path) -> str:
    c = _cv()
    cfg = Path(c.HOME) / ".codex" / "config.toml"
    if not cfg.parent.exists():
        if _detect_mcp_client_app("codex"):
            cfg.parent.mkdir(parents=True, exist_ok=True)
        else:
            return "未找到 ~/.codex (codex 未安装)"
    text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.exists() else ""
    if "context-vault" in text:
        return "已安装 (跳过)"
    had_cfg = cfg.exists()
    if had_cfg:
        cfg.with_suffix(".toml.bak-cv").write_text(text, encoding="utf-8")  # 备份
    py, args = _mcp_cmd_parts(vault)
    sep = "" if not text else (("\n" if not text.endswith("\n") else "") + ("\n" if text.strip() else ""))
    toml = (f"{sep}[mcp_servers.context-vault]\n"
            f'command = {json.dumps(str(py), ensure_ascii=False)}\n'
            "args = [" + ", ".join(json.dumps(str(a), ensure_ascii=False) for a in args) + "]\n")
    c.atomic_write_text(cfg, text + toml)
    return "已追加到 ~/.codex/config.toml (备份 .bak-cv)" if had_cfg else "已写入 ~/.codex/config.toml"


def _install_mcp_cursor(vault: Path) -> str:
    c = _cv()
    cfg = Path(c.HOME) / ".cursor" / "mcp.json"
    if not cfg.parent.exists():
        if _detect_mcp_client_app("cursor"):
            cfg.parent.mkdir(parents=True, exist_ok=True)
        else:
            return "未找到 ~/.cursor (cursor 未安装)"
    raw = cfg.read_text(encoding="utf-8", errors="replace").strip() if cfg.exists() else ""
    data = json.loads(raw) if raw else {}
    if raw:
        cfg.with_suffix(".json.bak-cv").write_text(json.dumps(data, indent=2), encoding="utf-8")
    servers = data.setdefault("mcpServers", {})
    if "context-vault" in servers:
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    servers["context-vault"] = {"command": py, "args": args}
    c.atomic_write_text(cfg, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return "已写入 ~/.cursor/mcp.json"


def _install_mcp_antigravity(vault: Path) -> str:
    c = _cv()
    gemini_dir = Path(c.HOME) / ".gemini"
    if not gemini_dir.exists():
        if _detect_mcp_client_app("antigravity"):
            gemini_dir.mkdir(parents=True, exist_ok=True)
        else:
            return "未找到 ~/.gemini (antigravity 未安装)"
    cfg = gemini_dir / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    raw = cfg.read_text(encoding="utf-8").strip() if cfg.exists() else ""
    if raw:
        data = json.loads(raw)
        cfg.with_suffix(".json.bak-cv").write_text(json.dumps(data, indent=2), encoding="utf-8")
    servers = data.setdefault("mcpServers", {})
    if "context-vault" in servers:
        return "已安装 (跳过)"
    py, args = _mcp_cmd_parts(vault)
    servers["context-vault"] = {"command": py, "args": args}
    c.atomic_write_text(cfg, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return "已写入 ~/.gemini/config/mcp_config.json"


def _uninstall_mcp_json_file(cfg: Path) -> bool:
    """安全移除 JSON `mcpServers['context-vault']` 并保留其余配置与 `.json.bak-cv` 备份。"""
    if not cfg.is_file():
        return False
    raw = cfg.read_text(encoding="utf-8", errors="replace")
    if "context-vault" not in raw or not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "context-vault" not in servers:
        return False
    cfg.with_suffix(".json.bak-cv").write_text(raw, encoding="utf-8")
    servers.pop("context-vault", None)
    _cv().atomic_write_text(cfg, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def _uninstall_mcp_claude(vault: Path) -> str:
    c = _cv()
    cli_removed = False
    exe = _claude_exe()
    if exe:
        try:
            r = c.subprocess.run([exe, "mcp", "remove", "context-vault", "-s", "user"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=10, creationflags=c.NO_WINDOW)
            cli_removed = (r.returncode == 0)
        except Exception:
            pass
    if _uninstall_mcp_json_file(Path(c.HOME) / ".claude.json"):
        return "已从 ~/.claude.json 移除 context-vault (备份 .json.bak-cv)"
    if cli_removed:
        return "已通过 claude CLI 移除 context-vault"
    return "未安装 (无需移除)"


def _parse_toml_table_header(line: str) -> list[str] | None:
    """若为 TOML 表头 (`[...]` 或 `[[...]]`) 返回点分段落名, 否则返回 None。"""
    s = line.strip()
    if not s.startswith("["):
        return None
    if s.startswith("[["):
        end = s.find("]]")
        if end < 0:
            return None
        rest = s[end + 2:].strip()
        if rest and not rest.startswith("#"):
            return None
        inner = s[2:end]
    else:
        in_dq = False
        in_sq = False
        esc = False
        end = -1
        for i, ch in enumerate(s[1:], 1):
            if esc:
                esc = False
                continue
            if in_dq:
                if ch == "\\":
                    esc = True
                elif ch == '"':
                    in_dq = False
            elif in_sq:
                if ch == "'":
                    in_sq = False
            else:
                if ch == '"':
                    in_dq = True
                elif ch == "'":
                    in_sq = True
                elif ch == "]":
                    end = i
                    break
        if end < 0:
            return None
        rest = s[end + 1:].strip()
        if rest and not rest.startswith("#"):
            return None
        inner = s[1:end]
    parts: list[str] = []
    buf: list[str] = []
    in_dq = False
    in_sq = False
    esc = False
    for ch in inner:
        if esc:
            buf.append(ch)
            esc = False
            continue
        if in_dq:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_dq = False
            else:
                buf.append(ch)
        elif in_sq:
            if ch == "'":
                in_sq = False
            else:
                buf.append(ch)
        else:
            if ch == '"':
                in_dq = True
            elif ch == "'":
                in_sq = True
            elif ch == ".":
                parts.append("".join(buf).strip())
                buf.clear()
            else:
                buf.append(ch)
    parts.append("".join(buf).strip())
    return parts


def _uninstall_mcp_codex(vault: Path) -> str:
    c = _cv()
    cfg = Path(c.HOME) / ".codex" / "config.toml"
    if not cfg.is_file():
        return "未安装 (无需移除)"
    raw = cfg.read_text(encoding="utf-8", errors="replace")
    if "context-vault" not in raw:
        return "未安装 (无需移除)"
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    skipping = False
    removed = False
    for ln in norm.splitlines():
        parts = _parse_toml_table_header(ln)
        if parts is not None:
            if len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] == "context-vault":
                skipping = True
                removed = True
                continue
            skipping = False
        if skipping:
            continue
        out.append(ln)
    if not removed:
        return "未安装 (无需移除)"
    cfg.with_suffix(".toml.bak-cv").write_text(raw, encoding="utf-8")
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    c.atomic_write_text(cfg, (body + "\n") if body else "")
    return "已从 ~/.codex/config.toml 移除 context-vault (备份 .toml.bak-cv)"


def _uninstall_mcp_cursor(vault: Path) -> str:
    if _uninstall_mcp_json_file(Path(_cv().HOME) / ".cursor" / "mcp.json"):
        return "已从 ~/.cursor/mcp.json 移除 context-vault (备份 .json.bak-cv)"
    return "未安装 (无需移除)"


def _uninstall_mcp_antigravity(vault: Path) -> str:
    home = Path(_cv().HOME)
    removed = []
    for cfg, lbl in (
        (home / ".gemini" / "config" / "mcp_config.json", "~/.gemini/config/mcp_config.json"),
        (home / ".gemini" / "antigravity" / "mcp_config.json", "~/.gemini/antigravity/mcp_config.json"),
    ):
        if _uninstall_mcp_json_file(cfg):
            removed.append(lbl)
    if removed:
        return f"已从 {'、'.join(removed)} 移除 context-vault (备份 .json.bak-cv)"
    return "未安装 (无需移除)"


def mcp_status_api(vault: Path, probe_cli: bool = True) -> dict:
    """结构化检测各客户端 MCP 接入状态 (区分真实主程序与卸载残留目录, probe_cli=False 时仅快速读配置)。"""
    c = _cv()
    home = Path(c.HOME)
    py, args_ = _mcp_cmd_parts(vault)

    def _has_cv(p: Path) -> bool:
        try:
            return p.is_file() and "context-vault" in p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    claude_cfg = home / ".claude.json"
    claude_app = _detect_mcp_client_app("claude")
    claude_exe = _claude_exe()
    claude_on = _has_cv(claude_cfg)
    if not claude_on and probe_cli and claude_app and claude_exe:
        try:
            out = c.subprocess.run([claude_exe, "mcp", "list"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=5,
                                   creationflags=c.NO_WINDOW).stdout or ""
            claude_on = "context-vault" in out
        except Exception:
            pass
    codex_dir = home / ".codex"
    codex_cfg = codex_dir / "config.toml"
    codex_app = _detect_mcp_client_app("codex")
    cursor_dir = home / ".cursor"
    cursor_cfg = cursor_dir / "mcp.json"
    cursor_app = _detect_mcp_client_app("cursor")
    gemini_dir = home / ".gemini"
    agy_cfg1 = gemini_dir / "config" / "mcp_config.json"
    agy_cfg2 = gemini_dir / "antigravity" / "mcp_config.json"
    agy_app = _detect_mcp_client_app("antigravity")

    def _entry(cid: str, name: str, detected: bool, app_detected: bool, installed: bool, cfg: Path) -> dict:
        return {
            "id": cid, "name": name,
            "detected": bool(detected),
            "app_detected": bool(app_detected),
            "residual": bool(detected and not app_detected),
            "installed": bool(installed),
            "config": str(cfg),
        }

    clients = [
        _entry("claude", "Claude Code", claude_app or claude_cfg.exists(), claude_app, claude_on, claude_cfg),
        _entry("codex", "Codex", codex_dir.exists() or codex_app, codex_app, _has_cv(codex_cfg), codex_cfg),
        _entry("cursor", "Cursor", cursor_dir.exists() or cursor_app, cursor_app, _has_cv(cursor_cfg), cursor_cfg),
        _entry("antigravity", "Antigravity", gemini_dir.exists() or agy_app, agy_app,
               _has_cv(agy_cfg1) or _has_cv(agy_cfg2),
               agy_cfg2 if _has_cv(agy_cfg2) else agy_cfg1),
    ]
    import cv_dsh
    clients.append(cv_dsh.dsh_mcp_status())
    return {
        "server_cmd": f"{py} {' '.join(args_)}",
        "clients": clients,
        "installed_count": sum(1 for item in clients if item["installed"]),
        "detected_count": sum(1 for item in clients if item["detected"]),
        "app_detected_count": sum(1 for item in clients if item.get("app_detected")),
    }


def _resolve_mcp_targets(valid_ids: dict, client: str | None = None,
                         clients: list[str] | None = None, *, empty_msg: str) -> tuple:
    """解析单目标 / 多选列表 (去重保序并校验 ID); 返回 (targets, is_all_mode)。"""
    if clients is not None:
        if not isinstance(clients, list):
            raise ValueError("clients 必须为数组")
        seen: list[str] = []
        for raw in clients:
            cid = str(raw or "").strip()
            if not cid:
                continue
            if cid not in valid_ids:
                raise ValueError(f"未知客户端: {cid}")
            if cid not in seen:
                seen.append(cid)
        if not seen:
            raise ValueError(empty_msg)
        return seen, False
    cid = (client or "all").strip()
    if cid == "all":
        return list(valid_ids), True
    if cid not in valid_ids:
        raise ValueError(f"未知客户端: {cid}")
    return [cid], False


def mcp_install_api(vault: Path, client: str | None = "all", clients: list[str] | None = None) -> dict:
    """结构化执行 MCP 写入 (支持多选列表; client='all' 自动跳过卸载残留目录) 并返回最新状态。"""
    import cv_dsh
    installers = {"claude": _install_mcp_claude, "codex": _install_mcp_codex,
                  "cursor": _install_mcp_cursor, "antigravity": _install_mcp_antigravity,
                  "dsh": cv_dsh.install_mcp_dsh}
    targets, is_all = _resolve_mcp_targets(installers, client, clients, empty_msg="请至少选择一个要接入的客户端")
    pre_status = {item["id"]: item for item in mcp_status_api(vault)["clients"]} if is_all else {}
    results = []
    for cid in targets:
        if is_all:
            st = pre_status.get(cid, {})
            if st.get("residual"):
                results.append({"client": cid, "message": "未检测到主程序，仅存在残留配置目录 (已跳过; 如确需接入请显式指定)"})
                continue
            if not st.get("app_detected"):
                results.append({"client": cid, "message": f"未检测到 {st.get('name', cid)} (跳过)"})
                continue
        try:
            msg = installers[cid](vault)
        except Exception as e:
            msg = f"失败: {e}"
        results.append({"client": cid, "message": msg})
    return {"results": results, "status": mcp_status_api(vault)}


def mcp_uninstall_api(vault: Path, client: str | None = None, clients: list[str] | None = None) -> dict:
    """结构化移除指定客户端配置中的 context-vault 条目并返回最新状态。"""
    import cv_dsh
    uninstallers = {"claude": _uninstall_mcp_claude, "codex": _uninstall_mcp_codex,
                    "cursor": _uninstall_mcp_cursor, "antigravity": _uninstall_mcp_antigravity,
                    "dsh": cv_dsh.uninstall_mcp_dsh}
    if clients is None and not (client and str(client).strip()):
        raise ValueError("请指定要移除的客户端")
    targets, _ = _resolve_mcp_targets(uninstallers, client, clients, empty_msg="请至少选择一个要移除的客户端")
    results = []
    for cid in targets:
        try:
            msg = uninstallers[cid](vault)
        except Exception as e:
            msg = f"失败: {e}"
        results.append({"client": cid, "message": msg})
    return {"results": results, "status": mcp_status_api(vault)}


def cmd_mcp(args):
    vault = _cv().vault_dir(args)
    if args.mcp_cmd == "install":
        raw_clients = getattr(args, "clients", None)
        if raw_clients:
            clist = [x.strip() for x in str(raw_clients).split(",") if x.strip()]
            res = mcp_install_api(vault, client=None, clients=clist)
        else:
            target = "all" if getattr(args, "all", False) else args.client
            res = mcp_install_api(vault, client=target)
        for item in res["results"]:
            print(f"[{item['client']}] {item['message']}")
    elif args.mcp_cmd == "uninstall":
        raw_clients = getattr(args, "clients", None)
        if raw_clients:
            clist = [x.strip() for x in str(raw_clients).split(",") if x.strip()]
            res = mcp_uninstall_api(vault, client=None, clients=clist)
        else:
            target = "all" if getattr(args, "all", False) else args.client
            res = mcp_uninstall_api(vault, client=target)
        for item in res["results"]:
            print(f"[{item['client']}] {item['message']}")
    elif args.mcp_cmd == "status":
        st = mcp_status_api(vault)
        print(f"server: {st['server_cmd']}")
        for c in st["clients"]:
            if c["installed"] and c.get("residual"):
                tag = "✓ 已接入 (未检测到主程序)"
            elif c["installed"]:
                tag = "✓ 已配置"
            elif c.get("residual"):
                tag = "· 仅残留配置目录"
            elif c["detected"]:
                tag = "✗ 未接入 (已检测到主程序)"
            else:
                tag = "✗ 未检测到"
            print(f"[{c['id']:<11}] {tag}  ({c['config']})")
