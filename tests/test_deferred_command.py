"""`pipeline deferred`: the open deferred items of merged steps, their verdicts, and the nudges to sweep them.

Run: python3 -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, REPO, RunnerCase, write  # noqa: E402

import core  # noqa: E402

OTHER_REPO = 'acme/other'


def deferred(item, evidence='', owner=''):
    return {'item': item, 'evidence': evidence, 'owner': owner}


HANDOFF = deferred('a wider rename', 'core.py:12', '02')
GAP = deferred('the old cache leaks', 'README.md:1-4')


class DeferredCommandCase(RunnerCase):
    """Features whose steps carry deferred items, seeded straight into their state files."""

    def setUp(self):
        super().setUp()
        (self.fixture.feature / '01-thing').rename(self.fixture.feature / '01-x')

    def root(self, where='live', repo=REPO):
        plans = self.fixture.plans if repo == REPO else self.fixture.other / '.claude/plans'

        return {'live': plans, 'backlog': plans / '_backlog', 'archive': plans / '_archive'}[where]

    def seed(self, feature, step_id, items, closed=True, number=5, where='live', repo=REPO, respond=None):
        root = self.root(where, repo)
        write(root / feature / f'{step_id}-x' / 'brief.md', '# x\n')
        state = core.new_state(repo, feature, step_id)
        state['sessions'] = {'implement': 'one', **({'respond-1': 'two'} if respond is not None else {})}
        state['results'] = {'implement': {'deferred': items}, **({'respond-1': {'deferred': respond}}
                                                                 if respond is not None else {})}
        state['pr'] = {'number': number, 'url': f'https://example/pull/{number}'}
        state['history'] = [{'at': '2026-01-01T00:00:00', 'stage': 'done', 'status': 'done', 'reason': 'x'}]

        if closed:
            state['merged'] = {'number': number, 'url': f'https://example/pull/{number}'}

        state['status'] = 'closed' if closed else 'done'
        write(core.state_file(repo, feature, step_id, root), json.dumps(state))

    def verdicts(self, feature, value, where='live', repo=REPO):
        write(core.deferred_file(repo, feature, self.root(where, repo)), json.dumps(value))

    def verdict(self, verdict, owner='', note='why'):
        return {'verdict': verdict, 'owner': owner, 'note': note, 'at': '2026-01-01T00:00:00Z'}

    def add_other_repo(self):
        config = json.loads(self.fixture.config.read_text())
        config['repos'][OTHER_REPO] = str(self.fixture.other)
        write(self.fixture.config, json.dumps(config))

    def listing(self, *argv):
        self.assertEqual(self.main('deferred', *argv), 0, self.errors)

        return self.output.split('\n\n')[0]


class ListTest(DeferredCommandCase):
    def test_the_list_shows_the_dict_items_of_merged_steps_with_id_pr_owner_and_evidence(self):
        self.seed(FEATURE, '01', ['an old free-text note', GAP, HANDOFF])
        self.seed(FEATURE, '02', [deferred('not merged yet')], closed=False, number=6)
        self.assertEqual(self.listing(FEATURE),
                         'thing 01.2  PR #5  no owner  the old cache leaks\n'
                         '  evidence: README.md:1-4\n'
                         'thing 01.3  PR #5  step 02  a wider rename\n'
                         '  evidence: core.py:12')
        self.assertNotIn('not merged yet', self.output)
        self.assertNotIn('free-text', self.output)
        self.assertIn('legacy 1', self.output.splitlines()[-1])

    def test_an_item_with_a_verdict_leaves_the_list(self):
        self.seed(FEATURE, '01', [GAP, HANDOFF])
        self.verdicts(FEATURE, {'01.1': self.verdict('done')})
        self.assertEqual(self.listing(), 'thing 01.2  PR #5  step 02  a wider rename\n  evidence: core.py:12')
        self.verdicts(FEATURE, {'01.1': self.verdict('done'), '01.2': self.verdict('owned', 'later')})
        self.assertEqual(self.listing(), 'no open deferred items')

    def test_an_item_repeated_across_stages_has_one_id(self):
        self.seed(FEATURE, '01', [GAP], respond=[GAP, HANDOFF])
        self.assertEqual(self.listing(),
                         'thing 01.1  PR #5  no owner  the old cache leaks\n  evidence: README.md:1-4\n'
                         'thing 01.2  PR #5  step 02  a wider rename\n  evidence: core.py:12')

    def test_json_holds_the_raw_items_and_the_counts(self):
        evidence = 'core.py "a  b"\tc'
        self.seed(FEATURE, '01', ['legacy', deferred('keep  \x1b[2J this', evidence)])
        self.assertEqual(self.main('deferred', '--json'), 0, self.errors)
        value = json.loads(self.output)
        self.assertEqual(value['items'], [{'repo': REPO, 'feature': FEATURE, 'id': '01.2', 'pr': 5, 'owner': '',
                                           'item': 'keep  \x1b[2J this', 'evidence': evidence}])
        self.assertNotIn('\x1b', self.output, 'JSON escapes control characters')
        self.assertEqual(value['metrics'], {'merged_prs': 1, 'handoff': 0, 'gap': 1, 'legacy': 1, 'finished_items': 1,
                                            'finished_with_verdict': 0, 'verdicts': 0, 'done': 0, 'moot': 0,
                                            'wontdo': 0, 'owned': 0, 'unreadable': 0})

    def test_text_output_never_carries_terminal_controls(self):
        self.seed(FEATURE, '01', [deferred('a\x1b[2Jb\rc\x9bd‮e f', 'x.py\x07:1', '0\x1b2')])
        listing = self.listing()

        for character in ('\x1b', '\r', '\x9b', '‮', ' ', '\x07'):
            self.assertNotIn(character, self.output)

        self.assertEqual(listing, 'thing 01.1  PR #5  step 0 2  a [2Jb c d e f\n  evidence: x.py :1')

    def test_a_hand_edited_item_with_null_fields_still_lists(self):
        self.seed(FEATURE, '01', [{'item': 'edited', 'evidence': None, 'owner': None}])
        self.assertEqual(self.listing(), 'thing 01.1  PR #5  no owner  edited')

    def test_the_footer_counts_every_feature_of_the_repo(self):
        self.seed(FEATURE, '01', ['old note', HANDOFF, GAP], number=5)
        self.seed(FEATURE, '02', [deferred('c')], number=6)
        self.verdicts(FEATURE, {'01.2': self.verdict('done'), '01.3': self.verdict('owned', 'later')})
        self.seed('open', '01', [deferred('d')], number=7)
        self.seed('open', '02', [deferred('e')], closed=False, number=8)
        self.seed('old', '01', ['legacy one', 'legacy two', deferred('f')], number=3, where='archive')
        self.verdicts('old', {'01.3': self.verdict('moot')}, where='archive')
        footer = ("per merged PR: 0.25 handoff, 1.00 gap (4 PRs) · re-read 75% (3 of 4) · survive 33% (1 owned of 3) "
                  "· done 1 · moot 1 · won't do 0 · legacy 3\n")
        self.assertEqual(self.main('deferred'), 0, self.errors)
        self.assertEqual(self.output, 'open 01.1  PR #7  no owner  d\nthing 02.1  PR #6  no owner  c\n\n' + footer)
        self.assertEqual(self.main('deferred', 'open'), 0, self.errors)
        self.assertEqual(self.output, 'open 01.1  PR #7  no owner  d\n\n' + footer, 'a name narrows only the list')
        (self.root('archive') / 'old').rename(self.root() / 'old')
        self.assertEqual(self.main('deferred'), 0, self.errors)
        self.assertTrue(self.output.endswith('\n\n' + footer), self.output)

    def test_no_merged_steps_reads_n_a(self):
        self.assertEqual(self.main('deferred'), 0, self.errors)
        self.assertEqual(self.output, "no open deferred items\n\nper merged PR: n/a handoff, n/a gap (0 PRs) · re-read "
                                      "n/a (0 of 0) · survive n/a (0 owned of 0) · done 0 · moot 0 · won't do 0 · "
                                      "legacy 0\n")

    def test_a_broken_store_of_a_listed_feature_fails_and_one_read_for_the_footer_does_not(self):
        self.seed(FEATURE, '01', [GAP])
        self.seed('old', '01', [GAP], where='archive')
        path = core.deferred_file(REPO, 'old', self.root('archive'))
        path.write_text('{"broken')
        self.assertEqual(self.main('deferred'), 0, self.errors)
        self.assertIn(f'{path}: unreadable', self.errors)
        self.assertEqual(len(self.errors.splitlines()), 1, self.errors)
        self.assertIn('thing 01.1', self.output)
        self.assertTrue(self.output.endswith(' · partial: 1 feature unreadable\n'), self.output)
        path = core.deferred_file(REPO, FEATURE)
        path.write_text('[]')
        self.assertEqual(self.main('deferred'), 1)
        self.assertIn(f'{path}: unreadable', self.errors)
        self.assertEqual(self.main('deferred', FEATURE), 1)
        self.assertIn(f'{path}: unreadable', self.errors)

    def test_a_parked_feature_is_listed_and_found_by_name(self):
        self.seed('later', '01', [GAP], where='backlog')
        self.assertEqual(self.listing('later'), 'later 01.1  PR #5  no owner  the old cache leaks\n'
                                                '  evidence: README.md:1-4')
        self.assertIn('later 01.1', self.listing())

    def test_a_name_in_two_repos_needs_repo(self):
        self.add_other_repo()
        self.seed(FEATURE, '01', [GAP])
        self.seed(FEATURE, '01', [HANDOFF], repo=OTHER_REPO)
        self.assertEqual(self.main('deferred', FEATURE), 1)
        self.assertIn('pass --repo', self.errors)
        self.assertEqual(self.listing(FEATURE, '--repo', OTHER_REPO),
                         'thing 01.1  PR #5  step 02  a wider rename\n  evidence: core.py:12')
        self.assertIn('other/thing 01.1', self.listing())
        self.assertIn('shop/thing 01.1', self.listing())

    def test_a_missing_or_archived_name_says_so(self):
        self.assertEqual(self.main('deferred', 'nothing'), 1)
        self.assertIn('no feature nothing in the plans or _backlog/ of any registered checkout', self.errors)
        self.seed('old', '01', [GAP], where='archive')
        self.assertEqual(self.main('deferred', 'old'), 1)
        self.assertIn('old is archived', self.errors)

    def test_a_bad_name_exits_1_and_bad_grammar_exits_2(self):
        self.assertEqual(self.main('deferred', 'Bad_x'), 1)
        self.assertEqual(self.errors, 'Bad_x is not a feature name (lowercase letters, digits and dashes)\n')

        for argv in (('--bogus',), (FEATURE, 'extra'), ('--json', '--json', '-x')):
            with self.subTest(argv=argv):
                self.assertEqual(self.main('deferred', *argv), 2)
                self.assertTrue(self.errors.startswith('usage: pipeline deferred'), self.errors)

    def test_a_folder_named_deferred_is_reserved(self):
        self.seed('deferred', '01', [GAP])
        self.assertNotIn(('acme/shop', 'deferred'), core.adapter.all_features())


if __name__ == '__main__':
    unittest.main()
