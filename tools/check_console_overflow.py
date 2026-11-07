#!/usr/bin/env python3
"""控制台布局回归探针 (CI 用): 真实 headless Chromium 在多档视口逐分区测页面级溢出。

check_console_js.py 的 node 检查只管语法与逻辑, 管不了 CSS 级联 —— 2026-11-02 的几处重叠
(搜索框内边距被同优先级规则覆盖 / 图表标签逃出容器 / 窄屏裸 1fr 撑爆整页 / select 按 option
文本定宽) 都只在真实渲染里出现, 当时靠人肉巡检才发现。

用法: pip install playwright && python -m playwright install chromium
      python tools/check_console_overflow.py
退出码: 0 = 全部视口 × 分区无页面级溢出; 1 = 有溢出 (打印元凶元素); 2 = 环境缺 playwright/chromium
"""
import gc
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # Windows CI 控制台默认 cp1252, 打印中文进度会 UnicodeEncodeError (同 doctor GBK 教训)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv  # noqa: E402

VIEWPORTS = [(380, 740), (900, 800), (1440, 900), (2560, 1300)]
TABS = ["ov", "snap", "mig", "probe"]
# 页面级溢出: 文档宽于视口, 或可见元素越出视口。.tbl-wrap (表格容器) 与 .sidebar (窄屏时栏内横滚)
# 的内部滚动是设计行为, 排除。
PROBE = """() => {
  const vw = window.innerWidth, bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (el.closest('.tbl-wrap') || el.closest('.sidebar')) continue;
    if (r.right > vw + 1 || r.left < -1) {
      bad.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + ' right=' + Math.round(r.right));
      if (bad.length >= 5) break;
    }
  }
  return {docW: document.documentElement.scrollWidth, vw: vw, bad: bad};
}"""

# 压力样本: 长中文首句、长路径、长 sid 选项文本 — 最容易撑破 min-content 的内容
TEXTS = [
    "帮我看看这个项目的设计 这是一个论文代码项目 paper 文件夹在 D:\\CoBRA-paper\\experiments\\ablation",
    "Context Vault 项目评估与 Bug 清单 审查日期 2026-11-02 (Asia/Shanghai) 代码版本 main af80c51d8f76",
    "你看一下我们这个项目 实际上是 tadner 论文项目 我们希望替换 bert 为更轻量的编码器并保持指标",
    "请分析当前控制台的宽视口布局问题并给出具体修复方案、色值与间距规范",
]


def make_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "snapshots").mkdir(parents=True)
    conn = cv.db(vault)
    conn.executescript(cv.SCHEMA)
    for i, text in enumerate(TEXTS):
        env = cv.build_envelope({
            "capture": {"agent": "zcode", "source_format": "zcode-sqlite", "completeness": "full",
                        "session_ref": f"sess_probe_{i}",
                        "cwd": "D:\\work\\very-long-project-directory-name\\subproject-" + str(i)},
            "conversation": [{"type": "text", "role": "user", "content": text},
                             {"type": "text", "role": "assistant", "content": "收到, 正在分析。"}],
            "stats": cv.zero_stats()})
        env["created_at"] = f"2026-11-0{i + 1}T00:00:00+08:00"
        sid = cv.snapshot_id(env)
        p = vault / "snapshots" / f"{sid}.json"
        p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        cv.register_snapshot(conn, env, sid, p)
    conn.commit()
    conn.close()
    return vault


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright: pip install playwright && python -m playwright install chromium")
        return 2
    failures = []
    root = Path(tempfile.mkdtemp(prefix="cv_layout_probe_"))
    try:
        vault = make_vault(root)
        srv, _ = cv.build_vault_server(vault, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch()
                except Exception as e:  # 浏览器二进制未安装
                    print(f"chromium 不可用 ({str(e)[:120]}) — 运行 python -m playwright install chromium")
                    return 2
                page = browser.new_page()
                for w, h in VIEWPORTS:
                    page.set_viewport_size({"width": w, "height": h})
                    page.goto(f"http://127.0.0.1:{port}/")
                    page.wait_for_load_state("networkidle")
                    for tab in TABS:
                        page.click(f'button[data-t="{tab}"]')
                        page.wait_for_timeout(400)
                        r = page.evaluate(PROBE)
                        ok = r["docW"] <= r["vw"] and not r["bad"]
                        print(f"{w:>5}px {tab:<5} docW={r['docW']:<5} "
                              + ("OK" if ok else "OVERFLOW " + "; ".join(r["bad"])))
                        if not ok:
                            failures.append((w, tab))
                browser.close()
        finally:
            srv.shutdown()
            srv.server_close()
            gc.collect()  # /ledger 的 sqlite 连接由守护线程回收, 时机晚于 rmtree → 忽略清理错误
            shutil.rmtree(root, ignore_errors=True)
    except Exception as e:
        print(f"探针环境异常: {str(e)[:200]}")
        return 2
    if failures:
        print(f"\n{len(failures)} 处页面级溢出 — 控制台布局回归: {failures}")
        return 1
    print(f"\n{len(VIEWPORTS)} 档视口 × {len(TABS)} 分区无页面级溢出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
