"""跨工具 Skill 技能迁移引擎、CLI 与 HTTP API 回归测试。"""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import cv  # noqa: E402
import cv_skills  # noqa: E402


def _make_skill(base: Path, rel: str, content: str, extra_files: dict[str, str] | None = None) -> Path:
    d = base / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    for k, v in (extra_files or {}).items():
        fp = d / k
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(v, encoding="utf-8")
    return d


class TestCvSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv_skills_test_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "snapshots").mkdir(parents=True)
        self._patch_home = mock.patch.object(cv, "HOME", self.home)
        self._patch_home.start()

        # 构造合成 ZCode 独立 Skill + 插件缓存二级目录 Skill
        _make_skill(
            self.home / ".zcode" / "skills",
            "academic-figure-prompt",
            "---\nid: academic-figure-prompt\nname: Academic Figure Prompt\nversion: 1.0.0\n"
            "description: Generate publication-grade academic figure prompts.\n---\n\n# Body\nRead ~/.zcode/config.\n",
            extra_files={"scripts/gen.py": "print('ok')\n", "__pycache__/x.pyc": "ignored"},
        )
        _make_skill(
            self.home / ".zcode" / "skills",
            "bare-skill",
            "# Bare Skill Without Frontmatter\n\nHelpful instructions for debugging pipelines.\n",
        )
        _make_skill(
            self.home / ".zcode" / "cli" / "plugins" / "cache" / "xhs-pack" / "1.0.0" / "skills",
            "01-内容创作/cover-design",
            "---\nname: cover-design\ndescription: >-\n  Design clean social covers.\n---\n\n# Cover\n",
        )
        # Codex 系统隐藏目录不应被误扫
        _make_skill(
            self.home / ".codex" / "skills",
            ".system/internal-only",
            "---\nname: internal-only\ndescription: system\n---\n",
        )

    def tearDown(self):
        self._patch_home.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discover_and_frontmatter_normalization(self):
        st = cv_skills.discover_skills("zcode", include_plugins=False)
        self.assertEqual({s["slug"] for s in st}, {"academic-figure-prompt", "bare-skill"})
        afp = next(s for s in st if s["slug"] == "academic-figure-prompt")
        self.assertTrue(any("规范化 name" in r for r in afp["rewrites"]))
        self.assertTrue(any("~/.zcode" in w for w in afp["warnings"]))
        self.assertEqual(afp["files"], 2)  # SKILL.md + scripts/gen.py (排除 __pycache__)

        bare = next(s for s in st if s["slug"] == "bare-skill")
        self.assertFalse(bare["has_frontmatter"])
        self.assertTrue(any("frontmatter" in r for r in bare["rewrites"]))

        with_plug = cv_skills.discover_skills("zcode", include_plugins=True)
        self.assertEqual({s["slug"] for s in with_plug}, {"academic-figure-prompt", "bare-skill", "cover-design"})
        # Codex .system 跳过
        self.assertEqual(cv_skills.discover_skills("codex"), [])

    def test_migrate_antigravity_skills_and_plugin_modes(self):
        src_md = (self.home / ".zcode" / "skills" / "academic-figure-prompt" / "SKILL.md").read_text(encoding="utf-8")

        # 1) 模式 A: 直写 ~/.gemini/config/skills/ (先 dry-run 再 apply)
        dry = cv_skills.migrate_skills(source="zcode", target="antigravity", agy_mode="skills", apply=False)
        self.assertFalse(dry["applied"])
        self.assertEqual(dry["counts"]["planned_copy"], 2)
        self.assertFalse((self.home / ".gemini" / "config" / "skills").exists())

        res = cv_skills.migrate_skills(source="zcode", target="antigravity", agy_mode="skills", apply=True)
        self.assertTrue(res["applied"])
        self.assertEqual(res["counts"]["migrated"], 2)
        dst_md = self.home / ".gemini" / "config" / "skills" / "academic-figure-prompt" / "SKILL.md"
        self.assertTrue(dst_md.is_file())
        self.assertTrue((dst_md.parent / "scripts" / "gen.py").is_file())
        # 验证目标副本已规范化且源文件一字未动
        meta, _, has_fm = cv_skills.parse_frontmatter(dst_md.read_text(encoding="utf-8"))
        self.assertTrue(has_fm)
        self.assertEqual(meta["name"], "academic-figure-prompt")
        self.assertEqual(
            (self.home / ".zcode" / "skills" / "academic-figure-prompt" / "SKILL.md").read_text(encoding="utf-8"),
            src_md,
        )

        # 二次迁移幂等跳过 (已一致)
        res2 = cv_skills.migrate_skills(source="zcode", target="antigravity", agy_mode="skills", apply=True)
        self.assertEqual(res2["counts"]["skipped_identical"], 2)
        self.assertEqual(res2["counts"]["migrated"], 0)

        # 2) 模式 B: 打包为 Antigravity 独立插件 (含 plugin.json + 展平插件内二级目录)
        pres = cv_skills.migrate_skills(
            source="zcode",
            target="antigravity",
            agy_mode="plugin",
            plugin_name="ZCode Skills Pack",
            include_plugins=True,
            apply=True,
        )
        self.assertEqual(pres["plugin_name"], "zcode-skills-pack")
        manifest = Path(pres["plugin_manifest"])
        self.assertTrue(manifest.is_file())
        mj = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(mj["name"], "zcode-skills-pack")
        self.assertTrue((manifest.parent / "skills" / "cover-design" / "SKILL.md").is_file())

    def test_conflict_policies_skip_overwrite_backup_and_rename(self):
        # 在目标预先放一个同名但不同内容的 Skill
        existing = _make_skill(
            self.home / ".claude" / "skills",
            "academic-figure-prompt",
            "---\nname: academic-figure-prompt\ndescription: old version\n---\n\nold body\n",
        )

        # skip
        r_skip = cv_skills.migrate_skills(
            source="zcode", target="claude", selected=["academic-figure-prompt"], conflict="skip", apply=True
        )
        self.assertEqual(r_skip["counts"]["skipped_conflict"], 1)
        self.assertIn("old body", (existing / "SKILL.md").read_text(encoding="utf-8"))

        # rename
        r_ren = cv_skills.migrate_skills(
            source="zcode", target="claude", selected=["academic-figure-prompt"], conflict="rename", apply=True
        )
        self.assertEqual(r_ren["counts"]["renamed"], 1)
        self.assertTrue((self.home / ".claude" / "skills" / "academic-figure-prompt-2" / "SKILL.md").is_file())

        # overwrite (带备份)
        r_ow = cv_skills.migrate_skills(
            source="zcode", target="claude", selected=["academic-figure-prompt"],
            conflict="overwrite", apply=True, vault=self.vault,
        )
        self.assertEqual(r_ow["counts"]["overwritten"], 1)
        self.assertIsNotNone(r_ow["backup_dir"])
        self.assertTrue((Path(r_ow["backup_dir"]) / "academic-figure-prompt" / "SKILL.md").is_file())
        self.assertIn("# Body", (existing / "SKILL.md").read_text(encoding="utf-8"))

    def test_http_skills_endpoints(self):
        srv, idx = cv.build_vault_server(self.vault, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/skills/list?source=zcode&target=antigravity&agy_mode=plugin&plugin_name=my-pack")
            r = c.getresponse()
            data = json.loads(r.read().decode("utf-8"))
            c.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(data["total"], 2)
            self.assertEqual(data["plugin_name"], "my-pack")

            c = HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({
                "source": "zcode", "target": "antigravity", "agy_mode": "plugin",
                "plugin_name": "my-pack", "selected": ["academic-figure-prompt"], "apply": True,
            }).encode("utf-8")
            c.request("POST", "/skills/migrate", body=body, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            mres = json.loads(r.read().decode("utf-8"))
            c.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(mres["counts"]["migrated"], 1)
            self.assertTrue(Path(mres["plugin_manifest"]).is_file())
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()


if __name__ == "__main__":
    unittest.main()
