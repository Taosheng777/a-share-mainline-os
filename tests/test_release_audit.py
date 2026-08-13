import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_audit.py"
SPEC = importlib.util.spec_from_file_location("release_audit", SCRIPT)
release_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_audit)


class ReleaseAuditTest(unittest.TestCase):
    def test_detects_private_path_and_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.md"
            path.write_text(
                "private=/Users/" + "author/x\n"
                + "api_key='" + "abcdefghijklmnop" + "'\n",
                encoding="utf-8",
            )
            failures = release_audit.audit([path])
        self.assertTrue(any("私人" in item for item in failures))
        self.assertTrue(any("凭据" in item for item in failures))

    def test_clean_text_has_no_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ok.md"
            path.write_text("vault_root=/absolute/path/to/vault\n", encoding="utf-8")
            failures = release_audit.audit([path])
        self.assertFalse(any("ok.md" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
