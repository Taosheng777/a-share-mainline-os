import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('system_check',Path(__file__).resolve().parents[1]/'scripts/system_check.py')
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)


class SystemChecks(unittest.TestCase):
    def setup_project(self,p):
        (p/'00-系统').mkdir()
        d={'schema_version':1,'workflow':'astra_full_v1','allowed_models':['gpt-6-astra','claude-opus-5-5'],'method_version':'test-v1','contract':'contract.md','journal':'live.jsonl','shadow_journal':'shadow.jsonl','validation_status':'state.json'}
        (p/'00-系统/投资运行配置.json').write_text(json.dumps(d))
        (p/'contract.md').write_text('test')
        (p/'live.jsonl').write_text('');(p/'shadow.jsonl').write_text('')
        (p/'state.json').write_text(json.dumps({'method_version':'test-v1','profitability':'not_established'}))
        return d

    def test_empty_journal_is_not_profit_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);self.setup_project(p);r=M.check(p)
            self.assertTrue(r['ok']);self.assertEqual(r['profitability'],'not_established')

    def test_external_paths_and_wrong_model_rejected(self):
        for key,value in [('journal','../escape.jsonl'),('allowed_models',['other-model']),('allowed_models',[])]:
            with tempfile.TemporaryDirectory() as t:
                p=Path(t);d=self.setup_project(p);d[key]=value
                (p/'00-系统/投资运行配置.json').write_text(json.dumps(d))
                with self.assertRaises(ValueError):M.check(p)

    def test_manual_profit_claim_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);self.setup_project(p)
            (p/'state.json').write_text(json.dumps({'method_version':'test-v1','profitability':'established'}))
            with self.assertRaises(ValueError):M.check(p)

    def test_runtime_overwrite_is_detected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);self.setup_project(p)
            (p/'00-系统/运行文件校验.json').write_text(json.dumps({'files':{'stock-daily/scripts/system_check.py':'0'*64},'files_claude':{'stock-daily/scripts/system_check.py':'0'*64}}))
            with self.assertRaisesRegex(ValueError,'runtime changed'):M.check(p)

    def test_runtime_manifest_requires_current_platform_entry(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);self.setup_project(p)
            (p/'00-系统/运行文件校验.json').write_text(json.dumps({'unrelated':{}}))
            with self.assertRaisesRegex(ValueError,'platform'):M.check(p)

    def test_empty_runtime_hashes_cannot_claim_validated_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);self.setup_project(p)
            (p/'00-系统/运行文件校验.json').write_text(json.dumps({'files':{},'files_claude':{}}))
            with self.assertRaisesRegex(ValueError,'nonempty'):M.check(p)

    def test_malformed_optional_maps_are_reported_as_configuration_errors(self):
        for key in ('baseline_journals','decision_ops'):
            with tempfile.TemporaryDirectory() as t:
                p=Path(t);d=self.setup_project(p);d[key]=[]
                (p/'00-系统/投资运行配置.json').write_text(json.dumps(d))
                with self.assertRaisesRegex(ValueError,key):M.check(p)


if __name__=='__main__':unittest.main()
