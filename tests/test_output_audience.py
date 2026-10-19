import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'src/skills'


class OutputAudienceTest(unittest.TestCase):
    def modules(self):
        for skill, name in [('stock-screener', 'screen'), ('stock-buddy', 'danger_scan')]:
            spec = importlib.util.spec_from_file_location('audience_' + name, ROOT / skill / 'scripts' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield module

    def test_public_distribution_keeps_notice_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            for module in self.modules():
                with self.subTest(module=module.__name__):
                    text = module.compliance_notice(['测试来源'], '2026-09-18')
                    self.assertIn('不构成投资建议', text)
                    self.assertIn('数据来源', text)
                    self.assertIn('2026-09-18', text)

    def test_explicit_personal_keeps_source_and_date(self):
        for module in self.modules():
            text = module.compliance_notice(['测试来源'], '2026-09-18', audience='personal')
            self.assertNotIn('风险自担', text)
            self.assertIn('数据来源', text)
            self.assertIn('2026-09-18', text)

    def test_invalid_audience_is_not_silently_downgraded(self):
        for module in self.modules():
            with self.assertRaises(ValueError):
                module.compliance_notice([], None, audience='invalid')


if __name__ == '__main__':
    unittest.main()
