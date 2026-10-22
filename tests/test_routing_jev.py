import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader
plugin_loader.load_root()
jev = importlib.import_module('agents_cs_dashboard.routing.jev')

class JevTests(unittest.TestCase):
    def api(self, label, confidence=1):
        def call(state, questions):
            return {'usage': {'input_tokens': 100}, 'answers': {k: {
                'type': 'choice', 'choice': label, 'confidence': confidence,
                'probabilities': {c: int(c == label) for c in q['criteria']}}
                for k, q in questions.items()}}
        return call

    def test_uncertain_selection_keeps_original(self):
        rows = [{'id': 'a', 'text': 'critical evidence'}]
        decisions, _, failed = jev.evaluate(rows, 'select', 'a question', api=self.api('omit', .8))
        self.assertEqual(decisions[0]['label'], 'keep')
        self.assertFalse(failed)

    def test_all_omitted_keeps_original(self):
        d, _, _ = jev.evaluate([{'id': 'a', 'text': 'evidence'}], 'select', 'q', api=self.api('omit'))
        self.assertEqual(d[0]['label'], 'keep')

    def test_timeout_and_malformed_response_review(self):
        def timeout(*a): raise TimeoutError()
        for api in (timeout, lambda *a: {'answers': {}, 'usage': {'input_tokens': 0}}):
            d, _, failed = jev.evaluate([{'id': 'a', 'text': 'payment'}], 'transaction', 'category', api=api)
            self.assertTrue(failed)
            self.assertEqual(d[0]['label'], 'review')

    def test_path_secret_instruction_and_cross_profile_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'a', Path(td)/'b'
            a.mkdir(); b.mkdir()
            for p, text in [(b/'data.txt', 'outside'), (a/'AGENTS.md', 'instructions'),
                            (a/'key.txt', 'access_token=abcdefghijklmnop')]:
                p.write_text(text)
                with self.assertRaises(ValueError): jev.records_from_file(p, a)
            (a/'linked.txt').symlink_to(b/'data.txt')
            with self.assertRaises(ValueError): jev.records_from_file(a/'linked.txt', a)

    def test_original_preserved_metrics_private(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'records.json'
            original = json.dumps([{'id':'private-id', 'text':'private message'}])
            p.write_text(original)
            result = jev.run({'source_path':str(p),'mode':'inbox','query':'classify'},
                             home=td,workspace=td,api=self.api('marketing'))
            self.assertEqual(p.read_text(), original)
            self.assertEqual(result['decisions'][0]['label'], 'marketing')
            metrics=(Path(td)/'logs/jev-usage.jsonl').read_text()
            self.assertNotIn('private', metrics)
            self.assertNotIn('records', result)

    def test_disabled_skips_api(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'jev.disabled').touch()
            r=jev.run({},home=td,workspace=td,api=lambda *a: self.fail('API called'))
            self.assertEqual(r['reason'],'disabled')

if __name__ == '__main__': unittest.main()
