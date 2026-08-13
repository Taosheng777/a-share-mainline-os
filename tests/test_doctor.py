import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts/doctor.py"
INSTALLER = ROOT / "scripts/manage_install.py"
SOURCE = ROOT / "plugins/a-share-mainline-os-codex/skills"


class DoctorTest(unittest.TestCase):
    def run_doctor(self, skills: Path, config: Path, *, check: bool = True):
        return subprocess.run(
            [
                "python3",
                str(DOCTOR),
                "--platform",
                "codex",
                "--skills-root",
                str(skills),
                "--config",
                str(config),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_ready_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skills = root / "skills"
            subprocess.run(
                [
                    "python3",
                    str(INSTALLER),
                    "--platform",
                    "codex",
                    "--target",
                    str(skills),
                    "--source-root",
                    str(SOURCE),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            data_skill = skills / "a-stock-data"
            data_skill.mkdir()
            (data_skill / "SKILL.md").write_text(
                "---\nname: a-stock-data\ndescription: test\n---\n",
                encoding="utf-8",
            )
            vault = root / "vault"
            subprocess.run(["cp", "-R", str(ROOT / "vault-template"), str(vault)], check=True)
            config = root / "config.json"
            config.write_text(
                json.dumps({"vault_root": str(vault)}, ensure_ascii=False),
                encoding="utf-8",
            )

            completed = self.run_doctor(skills, config)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ready"])
            self.assertTrue(payload["checks"]["product_skills"]["ok"])
            self.assertTrue(payload["checks"]["free_data_skill"]["ok"])
            self.assertTrue(payload["checks"]["vault_contract"]["ok"])

    def test_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skills = root / "skills"
            skills.mkdir()
            completed = self.run_doctor(skills, root / "missing.json", check=False)
            self.assertEqual(completed.returncode, 1)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ready"])
            self.assertFalse(payload["checks"]["config"]["ok"])


if __name__ == "__main__":
    unittest.main()
