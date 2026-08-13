import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import manage_install


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/manage_install.py"
SOURCE = ROOT / "plugins/a-share-mainline-os-codex/skills"
SKILLS = ("stock-daily", "stock-screener", "stock-buddy")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ManageInstallTest(unittest.TestCase):
    def run_cli(self, target: Path, *args: str, check: bool = True):
        return subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--platform",
                "codex",
                "--target",
                str(target),
                "--source-root",
                str(SOURCE),
                *args,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_fresh_install_records_version(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "skills"
            completed = self.run_cli(target)
            self.assertIn("安装完成", completed.stdout)
            for skill in SKILLS:
                self.assertTrue((target / skill / "SKILL.md").is_file())
            receipt = json.loads(
                (target / ".a-share-mainline-os/install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["version"], VERSION)
            self.assertEqual(receipt["platform"], "codex")

    def test_existing_install_requires_explicit_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "skills"
            (target / "stock-daily").mkdir(parents=True)
            marker = target / "stock-daily/old.txt"
            marker.write_text("old", encoding="utf-8")
            completed = self.run_cli(target, check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(marker.is_file())

    def test_dry_run_does_not_modify_existing_install(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "skills"
            for skill in SKILLS:
                directory = target / skill
                directory.mkdir(parents=True)
                (directory / "old.txt").write_text(skill, encoding="utf-8")
            before = digest_tree(target)
            completed = self.run_cli(target, "--upgrade", "--dry-run")
            self.assertIn("DRY RUN", completed.stdout)
            self.assertEqual(digest_tree(target), before)

    def test_upgrade_and_rollback_preserve_user_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "skills"
            user_data = root / "user-data"
            user_data.mkdir()
            (user_data / "config.json").write_text('{"vault_root":"unchanged"}\n')
            (user_data / "vault.md").write_text("用户事实\n", encoding="utf-8")
            user_digest = digest_tree(user_data)

            for skill in SKILLS:
                directory = target / skill
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\ndescription: old\n---\nold\n",
                    encoding="utf-8",
                )
                (directory / "old.txt").write_text(skill, encoding="utf-8")

            self.run_cli(target, "--upgrade")
            self.assertEqual(digest_tree(user_data), user_digest)
            self.assertFalse((target / "stock-daily/old.txt").exists())
            backup_root = target / ".a-share-mainline-os/backups"
            self.assertEqual(len([path for path in backup_root.iterdir() if path.is_dir()]), 1)

            completed = self.run_cli(target, "--rollback")
            self.assertIn("回滚完成", completed.stdout)
            self.assertEqual(digest_tree(user_data), user_digest)
            for skill in SKILLS:
                self.assertEqual((target / skill / "old.txt").read_text(), skill)

    def test_upgrade_failure_restores_previous_skills(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "skills"
            for skill in SKILLS:
                directory = target / skill
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\ndescription: old\n---\nold\n",
                    encoding="utf-8",
                )
                (directory / "old.txt").write_text(skill, encoding="utf-8")

            real_move = manage_install.shutil.move
            calls = 0

            def fail_once(source, destination):
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise OSError("simulated move failure")
                return real_move(source, destination)

            with mock.patch.object(manage_install.shutil, "move", side_effect=fail_once):
                with self.assertRaises(OSError):
                    manage_install.install(
                        platform="codex",
                        target=target,
                        source=SOURCE,
                        upgrade=True,
                        dry_run=False,
                    )
            for skill in SKILLS:
                self.assertEqual((target / skill / "old.txt").read_text(), skill)
            backup_root = target / ".a-share-mainline-os/backups"
            self.assertFalse(backup_root.exists() and any(backup_root.iterdir()))

    def test_legacy_force_install_environment_still_upgrades(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "skills"
            for skill in SKILLS:
                directory = target / skill
                directory.mkdir(parents=True)
                (directory / "old.txt").write_text(skill, encoding="utf-8")
            env = {
                **os.environ,
                "CODEX_SKILLS_DIR": str(target),
                "ASM_FORCE_INSTALL": "1",
            }
            subprocess.run(
                ["bash", str(ROOT / "adapters/codex/install.sh")],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
            )
            receipt = json.loads(
                (target / ".a-share-mainline-os/install.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["version"], VERSION)


if __name__ == "__main__":
    unittest.main()
