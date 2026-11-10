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


TOKEN = 'ghp_0123456789abcdefghijklmnopqrstuv'
REFUSED = [
    (deferred('no such step', '', '07'), 'Deferred item "no such step": owner "07" is not a later step of this feature. '
                                         'Use one of 02, or "" with evidence.'),
    (deferred('this step', '', '01'), 'owner "01" is not a later step of this feature. Use one of 02'),
    (deferred('bad format', '', '2'), 'owner "2" is not a later step of this feature. Use one of 02'),
    (deferred('nothing to show'), 'Deferred item "nothing to show" has no owner and no evidence. Add a repo-relative '
                                  'path[:line], fix it now, or drop it.'),
    (deferred('absolute', '/etc/x'), 'Deferred item "absolute": evidence must start with a repo-relative path[:line], '
                                     'not "/etc/x".'),
    (deferred('home', '~/x'), 'evidence must start with a repo-relative path[:line], not "~/x".'),
    (deferred('parent', '../x'), 'evidence must start with a repo-relative path[:line], not "../x".'),
    (deferred('a link', 'http://x'), 'evidence must start with a repo-relative path[:line], not "http://x".'),
    (deferred('a command', '$(x)'), 'evidence must start with a repo-relative path[:line], not "$(x)".'),
    (deferred('a backtick', '`rm`'), 'not "`rm`".'),
    (deferred('a pipe', 'a|b'), 'not "a|b".'),
    (deferred(f'leak {TOKEN}', 'core.py:1'), 'contains a token-shaped string (a GitHub token or a private key). '
                                            'Remove it.'),
    (deferred('leak in evidence', f'core.py:1 "{TOKEN}"'), 'contains a token-shaped string'),
]
ACCEPTED = [deferred('handed on', '', '02'), deferred('a gap', 'core.py:12'), deferred('lines', 'README.md:1-4'),
            deferred('dotted', '.github/workflows/ci.yml:3'),
            deferred('quoted', 'tests/test_core.py:405 "deferred: []"'), deferred('owned with proof', 'core.py', '02')]


class GateTest(RunnerCase):
    def setUp(self):
        super().setUp()
        write(self.fixture.feature / '02-other/brief.md', '# 02 Other\n')

    def test_each_refused_item_resumes_the_stage_with_its_reason(self):
        items = [item for item, _ in REFUSED]
        fixed = [deferred('a gap', 'core.py:12')]
        self.published(**{'thing-01-implement': [dict(IMPLEMENT, result={'deferred': items}),
                                                 {'result': {'deferred': fixed}}]})
        first, fix = self.claude_calls('thing-01-implement')
        self.assertEqual(fix['session'], first['session'])
        self.assertTrue(fix['resume'])
        self.assertIn('Fix each deferred item in your result (the runner checks owner and evidence):', fix['prompt'])

        for _, message in REFUSED:
            self.assertIn(message, fix['prompt'])

        self.assertNotIn(TOKEN, fix['prompt'], 'the refusal never repeats the token')
        self.assertTrue(self.state()['results']['implement']['deferred_checked'])

    def test_valid_owners_and_evidence_pass(self):
        for item in ACCEPTED:
            self.assertIsNone(core.deferred_problem(item, ['02']), item)

        self.assertIn('owner "02" is not a later step of this feature. No later step exists: use "" with evidence.',
                      core.deferred_problem(deferred('last step', '', '02'), []))

    def test_a_judged_result_is_not_judged_again_and_legacy_strings_pass(self):
        state = self.state()
        state['results']['implement'] = {'deferred': ['a legacy item', deferred('handed on', '', '02')]}
        gate = core.gate_deferred('implement')
        self.assertEqual(gate.__name__, 'gate_deferred')
        self.assertEqual(gate(state, None), [])
        self.assertTrue(state['results']['implement']['deferred_checked'])
        self.assertTrue(self.state()['results']['implement']['deferred_checked'], 'the mark is saved')
        (self.fixture.feature / '02-other/brief.md').unlink()
        (self.fixture.feature / '02-other').rmdir()
        self.assertEqual(gate(state, None), [], 'a judged result passes a re-gate though step 02 is gone')
        state['results']['implement'] = {'deferred': [deferred('handed on', '', '02')]}
        self.assertIn('owner "02" is not a later step', gate(state, None)[0])

    def test_a_review_result_is_never_judged(self):
        state = self.state()
        state['results']['review-1'] = {'deferred': [deferred('nothing to show')]}
        self.assertEqual(core.gate_deferred('review-1')(state, None), [])
        self.assertNotIn('deferred_checked', state['results']['review-1'])

    def test_a_new_result_after_another_red_gate_is_judged_again(self):
        handed = {'deferred': [deferred('handed on', '', '02')]}
        drop_step = {'run': [f"rm -r {self.fixture.feature / '02-other'}"], 'result': handed}
        self.pnpm({'check': [{'code': 1, 'output': 'broken'}]})
        self.published(**{'thing-01-implement': [dict(IMPLEMENT, result=handed), drop_step,
                                                 {'result': {'deferred': [deferred('a gap', 'core.py:12')]}}]})
        prompts = [call['prompt'] for call in self.claude_calls('thing-01-implement')]
        self.assertEqual(len(prompts), 3)
        self.assertIn('broken', prompts[1])
        self.assertIn('owner "02" is not a later step of this feature. No later step exists', prompts[2])


if __name__ == '__main__':
    unittest.main()
