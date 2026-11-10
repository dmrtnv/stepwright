"""Deferred items: `{item, evidence, owner}` objects the runner checks, hands to their step, keeps and renders.

Run: python3 -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, IMPLEMENT, RESOLVE, RESPOND, REVIEW, STEP, RunnerCase, move_main, write  # noqa: E402

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


class CarryTest(RunnerCase):
    """Items survive a fix-up rerun that replaces the stage's result; a review's never count."""

    def setUp(self):
        super().setUp()
        write(self.fixture.feature / '02-other/brief.md', '# 02 Other\n')

    def comment(self):
        return next(iter(self.github.comments.values()))

    def test_a_ci_fix_keeps_the_items_of_the_result_it_replaces(self):
        handoff = deferred('a wider rename', 'core.py:12', '02')
        fix = {'run': ["echo fixed >> thing.txt && git add -A && git commit -qm 'fix: Make CI pass'"]}
        self.happy_path(**{'thing-01-respond-1': [respond_with(deferred=[handoff]), fix]})
        self.main(FEATURE)
        self.ci(self.state()['published_sha'], 'success', 'failure')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(len(self.claude_calls('thing-01-respond-1')), 2, 'the CI fix ran')
        self.assertEqual(state['results']['respond-1']['deferred'], [])
        self.assertEqual(core.merged_results(state, 'deferred'), [handoff])
        self.assertIn('- a wider rename (in a later PR of this feature); evidence: core.py:12', self.comment())

    def test_a_conflict_during_the_final_respond_judges_its_items_before_the_rebase(self):
        conflicting = respond_with(deferred=[deferred('no such step', '', '07')])
        conflicting['run'] = RESPOND['run'] + [move_main('main', 'thing.txt')]
        fixed = deferred('a gap', 'core.py:12')
        self.published(**{'thing-01-respond-1': [conflicting, {'result': {'deferred': [fixed]}},
                                                 dict(RESOLVE, result={'deferred': []})]})
        prompts = [call['prompt'] for call in self.claude_calls('thing-01-respond-1')]
        self.assertEqual(len(prompts), 3, prompts)
        self.assertIn('owner "07" is not a later step', prompts[1])
        self.assertIn('conflicts', prompts[2])
        self.assertIn('- deferred: a gap; evidence: core.py:12', ' '.join(self.claude_calls('thing-01-respond-1')[2]['argv']))
        state = self.state()
        self.assertEqual(state['retired_deferred'], [fixed])
        self.assertEqual(core.merged_results(state, 'deferred'), [fixed])
        self.assertIn('- a gap; evidence: core.py:12', self.comment())

    def test_a_blocked_ci_fix_drops_its_own_items(self):
        stuck = {'result': {'status': 'blocked', 'summary': 'the log shows no cause', 'questions': [],
                            'deferred': [deferred('a blocked leftover', 'core.py:1')]}}
        kept = deferred('a wider rename', 'core.py:12', '02')
        self.happy_path(**{'thing-01-respond-1': [respond_with(deferred=[kept]), stuck]})
        self.main(FEATURE)
        sha = self.state()['published_sha']
        self.ci(sha, 'success', 'failure')
        self.main(FEATURE)
        self.assertEqual(self.state()['status'], 'paused', self.state()['reason'])
        self.ci(sha, 'success', 'success')
        self.main(FEATURE)
        state = self.state()
        self.assertEqual((state['stage'], state['status']), ('done', 'done'), state['reason'])
        self.assertEqual(core.merged_results(state, 'deferred'), [kept])
        self.assertNotIn('a blocked leftover', self.comment())

    def test_a_review_result_never_reaches_the_comment_or_already_recorded(self):
        review = dict(REVIEW, result={'deferred': [deferred('a review finding', 'core.py:9')]})
        self.published(**{'thing-01-review-1': [review]})
        self.assertNotIn('a review finding', ' '.join(self.claude_calls('thing-01-respond-1')[0]['argv']))
        self.assertNotIn('a review finding', self.comment())
        self.assertEqual(core.merged_results(self.state(), 'deferred'), [])

    def test_a_retired_item_is_kept_once_and_first(self):
        state = core.new_state('acme/shop', FEATURE, '01')
        self.assertEqual(state['retired_deferred'], [])
        older, newer = deferred('older', 'core.py:1'), deferred('newer', 'core.py:2')
        state['sessions'] = {'implement': 'a', 'respond-1': 'b'}
        state['results'] = {'implement': {'deferred': [newer]}, 'respond-1': {'deferred': [older, 'legacy'],
                                                                              'deferred_checked': True}}
        core.retire_result(state, 'respond-1')
        core.retire_result(state, 'respond-1')
        self.assertNotIn('respond-1', state['results'])
        self.assertEqual(state['retired_deferred'], [older, 'legacy'])
        state['results']['respond-1'] = {'deferred': [older]}
        core.retire_result(state, 'respond-1')
        self.assertEqual(state['retired_deferred'], [older, 'legacy'])
        self.assertEqual(core.merged_results(state, 'deferred'), [older, 'legacy', newer])


class HandOffTest(RunnerCase):
    """A later step's stages see the items earlier steps handed to it."""

    def setUp(self):
        super().setUp()
        write(self.fixture.feature / '02-other/brief.md', '# 02 Other\n')
        write(self.fixture.feature / '03-third/brief.md', '# 03 Third\n')
        self.handed = deferred('a wider rename', 'core.py:12', '02')
        self.later = deferred('a later cleanup', '', '03')
        first = self.state()
        first['status'], first['stage'] = 'closed', 'done'
        first['sessions'] = {'implement': 'a'}
        first['results'] = {'implement': {'deferred': [self.handed, self.later, deferred('an unowned leak', 'core.py:1'),
                                                       'a legacy item']}}
        self.save(first)

    def test_the_owner_steps_stages_get_the_items_handed_to_it(self):
        self.script({'thing-02-plan': [{'run': ["printf '# Plan 2\\n' > $HOME/plans/thing/02-other/plan.md"]}]})
        self.assertEqual(self.main(FEATURE, '--until', 'plan'), 0, self.errors)
        prompt = ' '.join(self.claude_calls('thing-02-plan')[0]['argv'])
        self.assertIn('## Handed to this step\n\n- a wider rename (for step 02-other); evidence: core.py:12\n', prompt)

        for absent in ('a later cleanup', 'an unowned leak', 'a legacy item'):
            self.assertNotIn(absent, prompt)

    def test_an_item_handed_on_reaches_the_next_owner(self):
        self.assertEqual(core.handed_lines(self.state('03')),
                         ['', '## Handed to this step', '', '- a later cleanup (for step 03-third)'])
        second = self.state('02')
        second['sessions'] = {'plan': 'b'}
        second['results'] = {'plan': {'deferred': [dict(self.handed, owner='03')]}}
        self.save(second)
        self.assertEqual(core.handed_lines(self.state('03'))[-1], '- a later cleanup (for step 03-third)\n'
                         '- a wider rename (for step 03-third); evidence: core.py:12')
        self.assertEqual(core.handed_lines(self.state('01')), [])

    def test_a_step_nobody_handed_anything_gets_no_block(self):
        first = self.state()
        first['results'] = {'implement': {'deferred': [self.handed]}}
        self.save(first)
        self.assertEqual(core.handed_lines(self.state('03')), [])

    def test_only_earlier_steps_hand_items_on(self):
        for step_id in ('02', '03'):
            state = self.state(step_id)
            state['sessions'] = {'plan': step_id}
            state['results'] = {'plan': {'deferred': [deferred(f'from step {step_id}', '', '02')]}}
            self.save(state)

        self.assertNotIn('from step', core.handed_lines(self.state('02'))[-1])


if __name__ == '__main__':
    unittest.main()
