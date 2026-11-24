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

    def test_cache_self_invalidation_and_refresh_param(self):
        # 1) 首次迁入后进入温缓存态, 状态为 identical
        cv_skills.migrate_skills(
            source="zcode", target="antigravity", selected=["academic-figure-prompt"], apply=True
        )
        r1 = cv_skills.list_skills_api(source="zcode", target="antigravity")
        st1 = {s["slug"]: s["target_status"] for s in r1["skills"]}
        self.assertEqual(st1["academic-figure-prompt"], "identical")

        # 2) 不清缓存直接改动目标 SKILL.md -> mtime/size 自失效立即识出 conflict
        dst_dir = self.home / ".gemini" / "config" / "skills" / "academic-figure-prompt"
        dst_md = dst_dir / "SKILL.md"
        dst_md.write_text(dst_md.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        r2 = cv_skills.list_skills_api(source="zcode", target="antigravity")
        st2 = {s["slug"]: s["target_status"] for s in r2["skills"]}
        self.assertEqual(st2["academic-figure-prompt"], "conflict")

        # 3) 覆盖修复一致后, 再直接改动源端附属脚本 scripts/gen.py -> 立即识出 conflict
        cv_skills.migrate_skills(
            source="zcode", target="antigravity", selected=["academic-figure-prompt"],
            conflict="overwrite", apply=True, vault=self.vault,
        )
        src_gen = self.home / ".zcode" / "skills" / "academic-figure-prompt" / "scripts" / "gen.py"
        src_gen.write_text("print('modified-in-test')\n", encoding="utf-8")
        r3 = cv_skills.list_skills_api(source="zcode", target="antigravity")
        st3 = {s["slug"]: s["target_status"] for s in r3["skills"]}
        self.assertEqual(st3["academic-figure-prompt"], "conflict")

        # 4) HTTP GET /skills/list?refresh=1 正常强制重扫并返回最新状态
        srv, idx = cv.build_vault_server(self.vault, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/skills/list?source=zcode&target=antigravity&refresh=1")
            resp = c.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            c.close()
            self.assertEqual(resp.status, 200)
            st_http = {s["slug"]: s["target_status"] for s in data["skills"]}
            self.assertEqual(st_http["academic-figure-prompt"], "conflict")
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()

    def test_inspect_diff_remove_and_rollback(self):
        # 1) 迁入前预览与 list_skills_api target_skills
        pre = cv_skills.inspect_skill_api("zcode", "academic-figure-prompt", target="antigravity")
        self.assertFalse(pre["target_exists"])
        self.assertEqual(pre["target_status"], "new")
        self.assertIn("name: academic-figure-prompt", pre["planned_skill_md"])

        cv_skills.migrate_skills(
            source="zcode", target="antigravity", selected=["academic-figure-prompt"], apply=True, vault=self.vault
        )
        lst = cv_skills.list_skills_api(source="zcode", target="antigravity", refresh=True)
        self.assertEqual([x["slug"] for x in lst["target_skills"]], ["academic-figure-prompt"])

        # 2) 制造目标端差异: 改 SKILL.md、删附属脚本、多一个仅目标文件 -> 校验 diff 与文件清单
        dst_dir = self.home / ".gemini" / "config" / "skills" / "academic-figure-prompt"
        (dst_dir / "SKILL.md").write_text("---\nname: academic-figure-prompt\n---\nOLD_TARGET\n", encoding="utf-8")
        (dst_dir / "scripts" / "gen.py").unlink()
        (dst_dir / "only-target.txt").write_text("extra\n", encoding="utf-8")

        ins = cv_skills.inspect_skill_api("zcode", "academic-figure-prompt", target="antigravity")
        self.assertTrue(ins["target_exists"])
        self.assertEqual(ins["target_status"], "conflict")
        self.assertIn("scripts/gen.py", ins["files_added"])
        self.assertIn("only-target.txt", ins["files_removed"])
        self.assertIn("SKILL.md", ins["files_changed"])
        self.assertTrue(any("OLD_TARGET" in ln for ln in ins["diff_lines"]))

        # 3) 覆盖迁入自动备份, 再一键回滚恢复覆盖前的目标版
        ov = cv_skills.migrate_skills(
            source="zcode", target="antigravity", selected=["academic-figure-prompt"],
            conflict="overwrite", apply=True, vault=self.vault,
        )
        self.assertTrue(ov["backup_id"])
        self.assertIn("# Body", (dst_dir / "SKILL.md").read_text(encoding="utf-8"))
        bks = cv_skills.list_skill_backups_api(self.vault)
        self.assertTrue(any(b["id"] == ov["backup_id"] and b["restorable"] for b in bks))

        rb_ov = cv_skills.rollback_skill_backup_api(self.vault, ov["backup_id"], apply=True)
        self.assertEqual(rb_ov["restored"], 1)
        self.assertIn("OLD_TARGET", (dst_dir / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((dst_dir / "only-target.txt").is_file())

        # 4) 路径穿越拦截 + dry-run/apply 安全移除 + plugin.json 计数更新 + 从移除备份恢复
        for bad_slug in ("../escape", "a/b", "."):
            with self.assertRaises(ValueError):
                cv_skills.remove_skills_api(target="antigravity", slugs=[bad_slug], vault=self.vault)

        cv_skills.migrate_skills(
            source="zcode", target="antigravity", agy_mode="plugin", plugin_name="rm-pack",
            apply=True, vault=self.vault,
        )
        plug_json = self.home / ".gemini" / "config" / "plugins" / "rm-pack" / "plugin.json"
        self.assertIn("2 skills migrated", json.loads(plug_json.read_text(encoding="utf-8"))["description"])

        dry_rm = cv_skills.remove_skills_api(
            target="antigravity", agy_mode="plugin", plugin_name="rm-pack",
            slugs=["academic-figure-prompt"], vault=self.vault, apply=False,
        )
        self.assertFalse(dry_rm["applied"])
        self.assertEqual(dry_rm["items"][0]["action"], "planned_remove")

        rm = cv_skills.remove_skills_api(
            target="antigravity", agy_mode="plugin", plugin_name="rm-pack",
            slugs=["academic-figure-prompt"], vault=self.vault, apply=True,
        )
        self.assertEqual(rm["removed"], 1)
        self.assertTrue(rm["backup_id"])
        plug_sk = self.home / ".gemini" / "config" / "plugins" / "rm-pack" / "skills" / "academic-figure-prompt"
        self.assertFalse(plug_sk.exists())
        self.assertIn("1 skills migrated", json.loads(plug_json.read_text(encoding="utf-8"))["description"])

        rb_rm = cv_skills.rollback_skill_backup_api(self.vault, rm["backup_id"], apply=True)
        self.assertEqual(rb_rm["restored"], 1)
        self.assertTrue((plug_sk / "SKILL.md").is_file())
        self.assertIn("2 skills migrated", json.loads(plug_json.read_text(encoding="utf-8"))["description"])

        # 5) 坏 backup_id 与被篡改 dest_root 的 manifest 必须被拒绝
        with self.assertRaises(ValueError):
            cv_skills.rollback_skill_backup_api(self.vault, "../evil", apply=False)
        mp = self.vault / "backups" / rm["backup_id"] / "manifest.json"
        mdata = json.loads(mp.read_text(encoding="utf-8"))
        mdata["dest_root"] = str(self.tmp / "outside-evil")
        mp.write_text(json.dumps(mdata), encoding="utf-8")
        with self.assertRaises(ValueError):
            cv_skills.rollback_skill_backup_api(self.vault, rm["backup_id"], apply=False)

    def test_skills_http_inspect_remove_backups_rollback(self):
        cv_skills.migrate_skills(
            source="zcode", target="antigravity", selected=["academic-figure-prompt"], apply=True, vault=self.vault
        )
        dst_md = self.home / ".gemini" / "config" / "skills" / "academic-figure-prompt" / "SKILL.md"
        dst_md.write_text("---\nname: academic-figure-prompt\n---\nHTTP_DIFF_OLD\n", encoding="utf-8")

        srv, idx = cv.build_vault_server(self.vault, 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def _req(method: str, path: str, payload: dict | None = None):
                c = HTTPConnection("127.0.0.1", port, timeout=5)
                if payload is not None:
                    c.request(method, path, body=json.dumps(payload).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
                else:
                    c.request(method, path)
                r = c.getresponse()
                txt = r.read().decode("utf-8")
                c.close()
                return r.status, json.loads(txt)

            sc, ins = _req("GET", "/skills/inspect?source=zcode&target=antigravity&id=academic-figure-prompt")
            self.assertEqual(sc, 200)
            self.assertEqual(ins["target_status"], "conflict")
            self.assertTrue(any("HTTP_DIFF_OLD" in ln for ln in ins["diff_lines"]))

            sc_rm, rm = _req("POST", "/skills/remove", {
                "target": "antigravity", "slugs": ["academic-figure-prompt"], "apply": True,
            })
            self.assertEqual(sc_rm, 200)
            self.assertEqual(rm["removed"], 1)
            self.assertFalse(dst_md.exists())

            sc_bk, bks = _req("GET", "/skills/backups")
            self.assertEqual(sc_bk, 200)
            self.assertTrue(any(b["id"] == rm["backup_id"] for b in bks["backups"]))

            sc_rb, rb = _req("POST", "/skills/rollback", {"backup_id": rm["backup_id"], "apply": True})
            self.assertEqual(sc_rb, 200)
            self.assertEqual(rb["restored"], 1)
            self.assertIn("HTTP_DIFF_OLD", dst_md.read_text(encoding="utf-8"))
        finally:
            srv.shutdown()
            srv.server_close()
            idx._close()


if __name__ == "__main__":
    unittest.main()
