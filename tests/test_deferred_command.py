"""`pipeline deferred`: the open deferred items of merged steps, their verdicts, and the nudges to sweep them.

Run: python3 -m unittest discover tests
"""
import json
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FEATURE, IMPLEMENT, REPO, RunnerCase, write  # noqa: E402
from test_deferred import DeferredCase  # noqa: E402

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


class CloseTest(DeferredCommandCase):
    def setUp(self):
        super().setUp()
        self.seed(FEATURE, '01', ['an old note', GAP, HANDOFF])
        self.seed(FEATURE, '02', [deferred('later')], closed=False, number=6)
        self.seed('follow-ups', '01', [], closed=False, where='backlog')

    def stored(self, feature=FEATURE, where='live'):
        path = core.deferred_file(REPO, feature, self.root(where))

        return json.loads(path.read_text()) if path.exists() else None

    def close(self, *argv):
        return self.main('deferred', 'close', *argv)

    def test_each_verdict_is_recorded_and_the_item_leaves_the_list(self):
        for verdict in ('done', 'moot', 'wontdo'):
            with self.subTest(verdict=verdict):
                self.assertEqual(self.close(FEATURE, '01.2', verdict, 'fixed in 01'), 0, self.errors)
                self.assertTrue(self.output.startswith(f'{FEATURE} 01.2: {verdict}'), self.output)
                stored = self.stored()['01.2']
                self.assertEqual(set(stored), {'verdict', 'owner', 'note', 'at'})
                self.assertEqual((stored['verdict'], stored['owner'], stored['note']), (verdict, '', 'fixed in 01'))
                self.assertNotIn('01.2', self.listing())

    def test_owned_takes_a_parked_target_with_or_without_a_note(self):
        self.assertEqual(self.close(FEATURE, '01.2', 'owned', 'follow-ups'), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} 01.2: owned by follow-ups\n')
        self.assertEqual(self.close(FEATURE, '01.3', 'owned', 'follow-ups', 'a later brief'), 0, self.errors)
        stored = self.stored()
        self.assertEqual((stored['01.2']['owner'], stored['01.2']['note']), ('follow-ups', ''))
        self.assertEqual((stored['01.3']['owner'], stored['01.3']['note']), ('follow-ups', 'a later brief'))
        self.assertEqual(self.listing(), 'no open deferred items')

    def test_a_re_close_replaces_the_verdict_and_says_what_it_was(self):
        self.close(FEATURE, '01.2', 'owned', 'follow-ups')
        self.assertEqual(self.close(FEATURE, '01.2', 'moot', 'gone'), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} 01.2: moot (was owned by follow-ups)\n')
        self.assertEqual(self.close(FEATURE, '01.2', 'done', 'fixed'), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} 01.2: done (was moot)\n')
        self.assertEqual(self.stored()['01.2']['verdict'], 'done')

    def test_the_note_is_one_printable_line_cut_at_300(self):
        self.close(FEATURE, '01.2', 'done', '  first\nsecond\x1b[2J  ' + 'x' * 400)
        note = self.stored()['01.2']['note']
        self.assertTrue(note.startswith('first second [2J xxx'), note)
        self.assertEqual(len(note), 300)

    def test_the_id_is_stored_in_normal_form(self):
        self.assertEqual(self.close(FEATURE, '01.002', 'done', 'why'), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE} 01.2: done\n')
        self.assertEqual(list(self.stored()), ['01.2'])

    def test_the_id_close_takes_is_the_one_the_list_printed_for_a_repeated_item(self):
        self.seed('twice', '01', [GAP], respond=[GAP, HANDOFF])
        self.assertIn('twice 01.2  PR #5  step 02  a wider rename', self.listing('twice'))
        self.assertEqual(self.close('twice', '01.2', 'done', 'why'), 0, self.errors)
        self.assertEqual(self.listing('twice'),
                         'twice 01.1  PR #5  no owner  the old cache leaks\n  evidence: README.md:1-4')

    def test_a_parked_feature_takes_a_verdict(self):
        self.seed('parked', '01', [GAP], where='backlog')
        self.assertEqual(self.close('parked', '01.1', 'wontdo', 'not worth it'), 0, self.errors)
        self.assertEqual(self.stored('parked', 'backlog')['01.1']['verdict'], 'wontdo')

    def test_a_folder_moved_away_before_the_write_fails_without_a_ghost_folder(self):
        original = core.read_verdicts
        moved = self.fixture.base / 'moved'

        def read_then_move(*arguments):
            verdicts = original(*arguments)

            if self.fixture.feature.exists():
                self.fixture.feature.rename(moved)

            return verdicts

        with unittest.mock.patch.object(core, 'read_verdicts', read_then_move):
            self.assertEqual(self.close(FEATURE, '01.2', 'done', 'why'), 1)

        self.assertEqual(len(self.errors.splitlines()), 1, self.errors)
        self.assertIn('nothing was recorded', self.errors)
        self.assertFalse(self.fixture.feature.exists(), 'the write recreated the moved folder')
        self.assertFalse((moved / '.pipeline/deferred.json').exists())

    def test_a_note_that_is_a_help_word_prints_help_and_records_nothing(self):
        self.assertEqual(self.close(FEATURE, '01.2', 'done', '-h'), 0)
        self.assertTrue(self.output.startswith('usage: pipeline deferred'), self.output)
        self.assertIsNone(self.stored())

    def test_refusals_exit_1_with_one_line_and_write_nothing(self):
        write(self.root() / 'empty' / 'notes.md', 'no steps\n')
        cases = {
            (FEATURE, '02.1', 'done', 'why'): f'{FEATURE} step 02 is not merged yet: only a merged step\'s items take a '
                                              'verdict',
            (FEATURE, '01x', 'done', 'why'): '01x is not an item id: use NN.K as pipeline deferred lists it',
            (FEATURE, '07.1', 'done', 'why'): f'{FEATURE} has no step 07',
            (FEATURE, '01.9', 'done', 'why'): f'{FEATURE} step 01 has no deferred item 9',
            (FEATURE, '01.0', 'done', 'why'): f'{FEATURE} step 01 has no deferred item 0',
            (FEATURE, '01.1', 'done', 'why'): f'{FEATURE} 01.1 is a legacy item: it takes no verdict',
            (FEATURE, '01.2', 'moot', ' \n '): 'a moot verdict needs a note that says why',
            (FEATURE, '01.2', 'owned', 'nowhere'): f'no feature nowhere with a step folder in {self.root()} or '
                                                   f'{self.root("backlog")}',
            (FEATURE, '01.2', 'owned', 'empty'): f'no feature empty with a step folder in {self.root()} or '
                                                 f'{self.root("backlog")}',
            ('Bad_x', '01.2', 'done', 'why'): 'Bad_x is not a feature name (lowercase letters, digits and dashes)',
            (FEATURE, '01.2', 'owned', 'Bad_y'): 'Bad_y is not a feature name (lowercase letters, digits and dashes)',
            (FEATURE, '01.2', 'owned', FEATURE): f'{FEATURE} cannot own its own item: name the feature that takes the '
                                                 'work on',
        }

        for argv, message in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(self.close(*argv), 1, self.output)
                self.assertEqual(self.errors, message + '\n')
                self.assertIsNone(self.stored())

    def test_a_held_run_lock_refuses(self):
        (self.fixture.base / 'logs').mkdir(exist_ok=True)
        holder = subprocess.Popen(['flock', str(self.fixture.base / f'logs/run-shop-{FEATURE}.lock'), 'sleep', '60'],
                                  start_new_session=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.assertEqual(self.close(FEATURE, '01.2', 'done', 'why'), 1)
        self.assertIn(f'holds run-shop-{FEATURE}', self.errors)
        self.assertIsNone(self.stored())

    def test_bad_grammar_prints_usage_and_exits_2(self):
        for argv in ((FEATURE, '01.2', 'done'), (FEATURE, '01.2', 'fixed', 'why'), (FEATURE, '01.2', 'done', 'a', 'b'),
                     (FEATURE, '01.2', 'owned'), (FEATURE, '01.2', 'owned', 'follow-ups', 'a', 'b'),
                     (FEATURE, '01.2', 'done', 'why', '--force'), ()):
            with self.subTest(argv=argv):
                self.assertEqual(self.close(*argv), 2)
                self.assertTrue(self.errors.startswith('usage: pipeline deferred'), self.errors)
                self.assertIsNone(self.stored())


class NudgeTest(DeferredCommandCase):
    ROW = f'1 deferred to sweep → /deferred-sweep {FEATURE}'

    def test_a_merged_feature_with_an_open_item_needs_you_everywhere(self):
        self.seed(FEATURE, '01', ['an old note', GAP])

        for argv in (('status',), ('status', '--all'), ('queue',)):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv), 0, self.errors)
                self.assertIn('NEEDS YOU', self.output)
                self.assertIn(self.ROW, self.output)
                self.assertNotIn(f'{FEATURE} (merged)', self.output)

        self.assertEqual(self.main('status', '-v'), 0, self.errors)
        self.assertIn(f'shop/{FEATURE} 01-x: ', self.output)
        self.assertIn(f'  next: /deferred-sweep {FEATURE} (1 deferred to sweep)\n', self.output)
        self.assertEqual(self.main('show', FEATURE), 0, self.errors)
        self.assertIn(f'every step is merged\nnext: /deferred-sweep {FEATURE} (1 deferred to sweep)\n', self.output)

    def test_a_merged_feature_without_open_items_stays_merged(self):
        for items in ([], ['only a legacy note']):
            with self.subTest(items=items):
                self.seed(FEATURE, '01', items)
                self.main('status')
                self.assertNotIn(FEATURE, self.output)
                self.main('status', '--all')
                self.assertIn(f'{FEATURE} (PR #5)', self.output)
                self.main('status', '-v')
                self.assertNotIn(f'shop/{FEATURE}', self.output)
                self.main('show', FEATURE)
                self.assertTrue(self.output.endswith('every step is merged\n'), self.output)

    def test_an_unreadable_store_needs_you(self):
        self.seed(FEATURE, '01', [GAP])
        write(core.deferred_file(REPO, FEATURE), '{"broken')
        self.assertEqual(self.main('status'), 0, self.errors)
        self.assertIn('NEEDS YOU', self.output)
        self.assertIn('deferred items unreadable: ', self.output)
        self.assertEqual(self.main('status', '-v'), 0, self.errors)
        self.assertIn('  deferred items unreadable: ', self.output)

    def test_an_unfinished_feature_shows_no_sweep(self):
        self.seed(FEATURE, '01', [GAP])
        self.seed(FEATURE, '02', [], closed=False)
        self.main('status')
        self.assertNotIn('deferred to sweep', self.output)
        self.main('status', '-v')
        self.assertNotIn('deferred-sweep', self.output)

    def test_archive_skips_a_feature_with_open_items_until_they_are_swept(self):
        self.seed(FEATURE, '01', [GAP, HANDOFF])
        self.assertEqual(self.main('archive'), 0, self.errors)
        self.assertEqual(self.output, f'{FEATURE}: skipped, because it has 2 open deferred items. Run /deferred-sweep '
                                      f'{FEATURE}, then pipeline archive again.\n'
                                      'moved 0, left 0 unfinished, skipped 1, failed 0\n')
        self.assertTrue(self.fixture.feature.is_dir())
        self.main('deferred', 'close', FEATURE, '01.1', 'wontdo', 'not worth it')
        self.main('deferred', 'close', FEATURE, '01.2', 'done', 'fixed in 02')
        self.assertEqual(self.main('archive'), 0, self.errors)
        self.assertIn('moved 1, left 0 unfinished, skipped 0, failed 0\n', self.output)
        self.assertFalse(self.fixture.feature.exists())

    def test_archive_counts_a_broken_store_as_failed(self):
        self.seed(FEATURE, '01', [GAP])
        path = core.deferred_file(REPO, FEATURE)
        write(path, '{"broken')
        self.assertEqual(self.main('archive'), 1)
        self.assertIn(f'{FEATURE}: {path}: unreadable', self.errors)
        self.assertIn('moved 0, left 0 unfinished, skipped 0, failed 1\n', self.output)
        self.assertTrue(self.fixture.feature.is_dir())

    def test_list_then_close_owned_then_the_row_is_gone(self):
        self.seed(FEATURE, '01', [GAP])
        self.seed('follow-ups', '01', [], closed=False, where='backlog')
        self.assertIn(f'{FEATURE} 01.1  PR #5  no owner  the old cache leaks', self.listing())
        self.main('status')
        self.assertIn(self.ROW, self.output)
        self.assertEqual(self.main('deferred', 'close', FEATURE, '01.1', 'owned', 'follow-ups'), 0, self.errors)
        self.assertEqual(json.loads(core.deferred_file(REPO, FEATURE).read_text())['01.1']['owner'], 'follow-ups')
        self.main('status')
        self.assertNotIn(self.ROW, self.output)
        self.assertNotIn('NEEDS YOU', self.output)


class MergePingTest(DeferredCase):
    def merge_first_step(self):
        self.published(**{'thing-01-implement': [dict(IMPLEMENT, result={'deferred': [GAP]})]})
        self.github.pull(5)['merged'] = True
        self.assertEqual(self.main(FEATURE, '--until', 'plan'), 0, self.errors)
        self.assertEqual(self.state()['status'], 'closed', self.state()['reason'])

        return [message for message in self.notifications if message.startswith('closed: PR #5')]

    def close_second_step(self):
        state = core.load_state(REPO, FEATURE, '02')
        state['history'] = [{'at': '2026-01-01T00:00:00', 'stage': 'done', 'status': 'closed', 'reason': 'x'}]
        core.mark(state, 'closed', 'merged as PR #6')

    def test_a_merge_that_leaves_a_step_open_pings_as_before(self):
        self.script({'thing-02-plan': [{'run': ["printf '# Plan 2\\n' > $HOME/plans/thing/02-other/plan.md"]}]})
        self.assertEqual(self.merge_first_step(), ['closed: PR #5 merged'])

    def test_the_last_merge_with_open_items_names_the_sweep(self):
        self.close_second_step()
        self.assertEqual(self.merge_first_step(),
                         [f'closed: PR #5 merged\n1 deferred to sweep → /deferred-sweep {FEATURE}'])

    def test_an_unreadable_store_still_pings(self):
        self.close_second_step()
        write(core.deferred_file(REPO, FEATURE), '{"broken')
        pings = self.merge_first_step()
        self.assertEqual(len(pings), 1, self.notifications)
        self.assertTrue(pings[0].startswith('closed: PR #5 merged\ndeferred items unreadable: '), pings[0])


if __name__ == '__main__':
    unittest.main()
