"""Deferred items: `{item, evidence, owner}` objects the runner checks, hands to their step, keeps and renders.

Run: python3 -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, IMPLEMENT, RESPOND, REVIEW, STEP, RunnerCase, write  # noqa: E402

import core  # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / 'result.schema.json'


def deferred(item, evidence='', owner=''):
    return {'item': item, 'evidence': evidence, 'owner': owner}


def respond_with(**result):
    return dict(RESPOND, result=dict(RESPOND['result'], **result))


class SchemaTest(unittest.TestCase):
    def test_a_deferred_item_is_an_object_with_item_evidence_and_owner(self):
        items = json.loads(SCHEMA.read_text())['properties']['deferred']['items']
        self.assertEqual(items['type'], 'object')
        self.assertEqual(items['required'], ['item', 'evidence', 'owner'])
        self.assertIs(items['additionalProperties'], False)

        for field in ('item', 'evidence', 'owner'):
            self.assertEqual(items['properties'][field]['type'], 'string')
            self.assertTrue(items['properties'][field]['description'])

        self.assertIn('never a command', items['properties']['evidence']['description'])


class RenderTest(RunnerCase):
    def setUp(self):
        super().setUp()
        write(self.fixture.feature / '02-other/brief.md', '# 02 Other\n')

    def comment(self):
        return next(iter(self.github.comments.values()))

    def test_a_handoff_names_a_later_pr_publicly_and_its_step_in_done_md(self):
        handoff = deferred('a wider rename', 'core.py:12', '02')
        gap = deferred('the old cache leaks', 'README.md:1-4')
        self.published(**{'thing-01-implement': [dict(IMPLEMENT, result={'deferred': [handoff, gap]})]})
        review = ' '.join(self.claude_calls('thing-01-review-1')[0]['argv'])
        self.assertIn('- deferred: a wider rename (for step 02-other); evidence: core.py:12', review)
        comment = self.comment()
        self.assertIn('## Deferred\n\n- a wider rename (in a later PR of this feature); evidence: core.py:12\n'
                      '- the old cache leaks; evidence: README.md:1-4', comment)
        self.assertNotIn('02-other', comment)
        notice = (self.fixture.step / 'done.md').read_text()
        self.assertIn('- a wider rename (for step 02-other); evidence: core.py:12', notice)
        self.assertIn('- the old cache leaks; evidence: README.md:1-4', notice)

    def test_an_owner_whose_folder_is_gone_shows_its_number_in_done_md(self):
        state = core.new_state('acme/shop', FEATURE, '01')
        self.assertEqual(core.deferred_text(state, deferred('later', '', '07'), False), 'later (for step 07)')
        self.assertEqual(core.deferred_text(state, 'a legacy item', False), 'a legacy item')

    def test_legacy_string_items_render_beside_objects(self):
        items = ['an old string item', deferred('a new one', 'core.py:3')]
        self.published(**{'thing-01-implement': [dict(IMPLEMENT, result={'deferred': items})]})
        review = ' '.join(self.claude_calls('thing-01-review-1')[0]['argv'])
        self.assertIn('- deferred: an old string item', review)
        self.assertIn('- deferred: a new one; evidence: core.py:3', review)
        self.assertIn('- an old string item\n- a new one; evidence: core.py:3', self.comment())


if __name__ == '__main__':
    unittest.main()
