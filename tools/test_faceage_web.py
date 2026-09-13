#!/usr/bin/env python3
"""Tests for tools/faceage_web.py.

Weighted toward the rules the UI is supposed to ENFORCE rather than merely
display — an ordering you can click past is not an ordering — and toward the
input validation, since this process copies files and spawns the pipeline.

    python3 tools/test_faceage_web.py
"""
import os
import sys
import csv
import json
import shutil
import tempfile
import unittest
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.inbox = tempfile.mkdtemp()
        self.trash = tempfile.mkdtemp()
        os.environ['FACEAGE_DATA'] = self.data
        os.environ['FACEAGE_INBOX'] = self.inbox
        os.environ['FACEAGE_TRASH'] = self.trash        # never the real Trash from a test
        for mod in ('faceage_web',):
            sys.modules.pop(mod, None)
        import faceage_web
        self.w = faceage_web
        self.w.DATA = self.data
        self.w.INBOX = self.inbox

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.inbox, ignore_errors=True)
        shutil.rmtree(self.trash, ignore_errors=True)
        os.environ.pop('FACEAGE_TRASH', None)

    def person(self, name='me'):
        self.w.do_create_person({'name': name})
        return name

    def put_inbox(self, *names):
        for n in names:
            with open(os.path.join(self.inbox, n), 'wb') as fh:
                fh.write(b'\xff\xd8\xff\xe0stub')

    def history(self, name, date, luma=121.3):
        r = self.w.results_dir(name)
        os.makedirs(r, exist_ok=True)
        p = os.path.join(r, 'faceage_history.csv')
        new = not os.path.exists(p)
        with open(p, 'a') as fh:
            if new:
                fh.write('session_date,run_timestamp,n,n_total_images,n_failed,'
                         'n_flagged,mean,median,std,min,max,mean_luma,'
                         'model_sha256,image_dir,notes\n')
            fh.write('%s,x,10,10,0,0,44.1,44.0,0.8,43,45,%s,dd,x,\n' % (date, luma))


class TestValidation(WebTestCase):
    def test_rejects_bad_person_names(self):
        for bad in ('', '../etc', 'a/b', 'name with space', 'x' * 41,
                    '.hidden', 'a;rm -rf /'):
            with self.assertRaises(ValueError, msg=bad):
                self.w.safe_subject(bad)

    def test_accepts_reasonable_names(self):
        for good in ('me', 'jackie', 'Geoff_B', 'test-1'):
            self.assertEqual(self.w.safe_subject(good), good)

    def test_rejects_bad_dates(self):
        for bad in ('', '2026-13-01', '2026-02-30', 'today', '../..',
                    '2026-1-1'):
            with self.assertRaises(ValueError, msg=bad):
                self.w.safe_date(bad)

    def test_import_rejects_path_traversal(self):
        self.person()
        with self.assertRaises(ValueError):
            self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'files': ['../../../etc/passwd']})

    def test_import_rejects_non_images(self):
        self.person()
        self.put_inbox('notes.txt')
        with self.assertRaises(ValueError):
            self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'files': ['notes.txt']})

    def test_bind_address_is_localhost(self):
        """Biometric data: this must never be reachable off the machine."""
        self.assertEqual(self.w.HOST, '127.0.0.1')


class TestPeople(WebTestCase):
    def test_create_and_list(self):
        self.person('me')
        self.person('jackie')
        names = [p['name'] for p in self.w.list_people()]
        self.assertEqual(names, ['jackie', 'me'])

    def test_duplicate_refused(self):
        self.person('me')
        with self.assertRaises(ValueError):
            self.w.do_create_person({'name': 'me'})

    def test_series_are_independent(self):
        self.person('me')
        self.person('jackie')
        self.history('me', '2026-09-11', luma=121.3)
        self.history('jackie', '2026-09-11', luma=98.0)
        self.assertAlmostEqual(self.w.baseline_luma('me'), 121.3)
        self.assertAlmostEqual(self.w.baseline_luma('jackie'), 98.0)
        self.assertTrue(self.w.session_scored('me', '2026-09-11'))
        self.assertFalse(self.w.session_scored('jackie', '2026-09-12'))

    def test_no_combined_view_exists(self):
        """There must be no endpoint or helper that returns two people's
        numbers together (docs/MAC_APP.md: no shared leaderboard)."""
        for bad in ('leaderboard', 'compare', 'ranking', 'all_histories'):
            self.assertFalse(hasattr(self.w, bad), 'unexpected %s' % bad)


class TestRename(WebTestCase):
    def test_renames_the_folder(self):
        self.person('me')
        self.history('me', '2026-09-11')
        r = self.w.do_rename({'old': 'me', 'new': 'Geoffrey'})
        self.assertEqual(r['name'], 'Geoffrey')
        self.assertFalse(os.path.isdir(self.w.subj_dir('me')))
        self.assertTrue(self.w.session_scored('Geoffrey', '2026-09-11'))

    def test_refuses_to_clobber(self):
        self.person('me'); self.person('jackie')
        with self.assertRaises(ValueError):
            self.w.do_rename({'old': 'me', 'new': 'jackie'})

    def test_validates_the_new_name(self):
        self.person('me')
        with self.assertRaises(ValueError):
            self.w.do_rename({'old': 'me', 'new': 'Geoff Brown'})


class TestImport(WebTestCase):
    def test_copies_and_leaves_original(self):
        self.person()
        self.put_inbox('IMG_1.jpg', 'IMG_2.jpg')
        r = self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'files': ['IMG_1.jpg', 'IMG_2.jpg']})
        self.assertEqual(sorted(r['copied']), ['IMG_1.jpg', 'IMG_2.jpg'])
        self.assertTrue(os.path.exists(os.path.join(self.inbox, 'IMG_1.jpg')),
                        'import must copy, not move')
        self.assertEqual(len(self.w.staged('me', '2026-09-13')), 2)

    def test_reimport_skips_existing(self):
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_1.jpg']})
        r = self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'files': ['IMG_1.jpg']})
        self.assertEqual(r['copied'], [])
        self.assertEqual(r['skipped'], ['IMG_1.jpg'])

    def test_empty_selection_refused(self):
        self.person()
        with self.assertRaises(ValueError):
            self.w.do_import({'person': 'me', 'date': '2026-09-13', 'files': []})

    def test_heic_is_importable(self):
        self.person()
        self.put_inbox('IMG_9.HEIC')
        r = self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'files': ['IMG_9.HEIC']})
        self.assertEqual(r['copied'], ['IMG_9.HEIC'])


class TestBrowsing(WebTestCase):
    """Folder picking. The server can read the filesystem, so the reachable
    area is bounded and every path is resolved before use."""

    def test_lists_images_and_subfolders(self):
        os.makedirs(os.path.join(self.inbox, 'trip'))
        os.makedirs(os.path.join(self.inbox, '.hidden'))
        self.put_inbox('a.jpg', 'b.HEIC', 'notes.txt')
        r = self.w.list_dir(self.inbox)
        self.assertEqual(r['dirs'], ['trip'])            # hidden dir skipped
        names = [i['file'] for i in r['images']]
        self.assertIn('a.jpg', names)
        self.assertIn('b.HEIC', names)
        self.assertNotIn('notes.txt', names)             # not an image

    def test_refuses_outside_allowed_roots(self):
        for bad in ('/etc', '/usr/bin', '/'):
            with self.assertRaises(ValueError, msg=bad):
                self.w.safe_dir(bad)

    def test_refuses_a_file(self):
        self.put_inbox('a.jpg')
        with self.assertRaises(ValueError):
            self.w.safe_dir(os.path.join(self.inbox, 'a.jpg'))

    def test_symlink_out_does_not_widen_the_boundary(self):
        link = os.path.join(self.inbox, 'escape')
        os.symlink('/etc', link)
        with self.assertRaises(ValueError):
            self.w.safe_dir(link)

    def test_traversal_is_resolved_before_checking(self):
        with self.assertRaises(ValueError):
            self.w.safe_dir(os.path.join(self.inbox, '..', '..', '..', 'etc'))

    def test_parent_is_none_at_the_boundary(self):
        r = self.w.list_dir(self.inbox)
        self.assertIsNotNone(r['path'])
        # walking up eventually leaves the allowed roots and stops
        seen, cur = 0, r
        while cur['parent'] and seen < 20:
            cur = self.w.list_dir(cur['parent'])
            seen += 1
        self.assertIsNone(cur['parent'])

    def test_import_from_a_chosen_folder(self):
        other = os.path.join(self.inbox, 'pics', '9_11_26')
        os.makedirs(other)
        for n in ('IMG_1.jpg', 'IMG_2.jpg'):
            with open(os.path.join(other, n), 'wb') as fh:
                fh.write(b'\xff\xd8\xff\xe0stub')
        self.person()
        r = self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'dir': other, 'files': ['IMG_1.jpg', 'IMG_2.jpg']})
        self.assertEqual(sorted(r['copied']), ['IMG_1.jpg', 'IMG_2.jpg'])
        self.assertTrue(os.path.exists(os.path.join(other, 'IMG_1.jpg')),
                        'import must copy, not move')

    def test_import_from_a_forbidden_folder_is_refused(self):
        self.person()
        with self.assertRaises(ValueError):
            self.w.do_import({'person': 'me', 'date': '2026-09-13',
                              'dir': '/etc', 'files': ['passwd']})

    def test_state_carries_the_browsed_folder(self):
        sub = os.path.join(self.inbox, 'trip')
        os.makedirs(sub)
        self.person()
        st = self.w.state('me', '2026-09-13', browse=sub)
        self.assertEqual(st['browse']['path'], os.path.realpath(sub))


class TestChecklistOrdering(WebTestCase):
    """Conditions are confirmed BEFORE analysis, and analysis never writes to
    the tracker. The only path in is do_add, on the result screen."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_1.jpg']})

    def ok(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})

    def test_score_refused_without_checklist(self):
        with self.assertRaises(ValueError) as cm:
            self.w.do_score({'person': 'me', 'date': '2026-09-13'})
        self.assertIn('conditions', str(cm.exception))

    def test_score_is_always_a_dry_run(self):
        self.ok()
        calls = []
        self.w.JOB.start = lambda label, argv, cwd=None: calls.append(argv)
        self.w.do_score({'person': 'me', 'date': '2026-09-13'})
        self.assertIn('--no-log', calls[0])
        self.assertIn('--subject', calls[0])

    def test_score_refused_once_in_tracker(self):
        self.ok()
        self.history('me', '2026-09-13')
        with self.assertRaises(ValueError):
            self.w.do_score({'person': 'me', 'date': '2026-09-13'})

    def test_checklist_refused_after_scoring(self):
        self.history('me', '2026-09-13')
        with self.assertRaises(ValueError) as cm:
            self.ok()
        self.assertIn('already been scored', str(cm.exception))

    def test_failed_item_records_a_protocol_failure(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        r = self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        self.assertFalse(r['checklist']['valid'])
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual((rows[0]['session_date'], rows[0]['valid']), ('2026-09-13', 'no'))
        self.assertIn('Frontal light', rows[0]['reason'])

    def test_all_passed_writes_no_failure_row(self):
        self.ok()
        self.assertFalse(os.path.exists(
            os.path.join(self.w.results_dir('me'), 'session_validity.csv')))

    def test_score_refused_with_no_photos(self):
        with self.assertRaises(ValueError):
            self.w.do_score({'person': 'me', 'date': '2026-09-20'})


class TestAddToTracker(WebTestCase):
    """do_add writes the history row from the pipeline's summary, with the
    pipeline's own columns and rounding."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13', 'files': ['IMG_1.jpg']})

    def summary(self, **kw):
        d = {'n': 9, 'n_total_images': 10, 'n_failed': 1, 'n_flagged': 0,
             'fellback_to_flagged': False, 'mean': 44.123456, 'median': 44.05,
             'std': 0.81, 'min': 42.9, 'max': 45.4, 'luma': 121.34,
             'session_date': '2026-09-13'}
        d.update(kw)
        with open(os.path.join(self.w.results_dir('me'), '2026-09-13_summary.json'), 'w') as fh:
            json.dump(d, fh)

    def ok(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})

    def rows(self):
        with open(os.path.join(self.w.results_dir('me'), 'faceage_history.csv')) as fh:
            return list(csv.DictReader(fh))

    def test_refused_before_analysis(self):
        self.ok()
        with self.assertRaises(ValueError):
            self.w.do_add({'person': 'me', 'date': '2026-09-13'})

    def test_refused_without_checklist(self):
        self.summary()
        with self.assertRaises(ValueError):
            self.w.do_add({'person': 'me', 'date': '2026-09-13'})

    def test_writes_the_row_with_pipeline_columns_and_rounding(self):
        self.ok(); self.summary()
        self.assertTrue(self.w.do_add({'person': 'me', 'date': '2026-09-13'})['ok'])
        r = self.rows()[0]
        self.assertEqual(list(r.keys()), self.w.HISTORY_COLS)
        self.assertEqual(r['mean'], '44.1235')          # 4 dp, like the pipeline
        self.assertEqual(r['mean_luma'], '121.3')       # 1 dp
        self.assertEqual(r['n'], '9')
        self.assertIn('+00:00', r['run_timestamp'])     # UTC, like the pipeline

    def test_columns_match_the_pipeline_source(self):
        """Pinned against src/faceage_run.py so a column added there fails
        here rather than silently misaligning the row."""
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src', 'faceage_run.py')).read()
        m = re.search(r"def append_history.*?cols = \[(.*?)\]", src, re.S)
        self.assertEqual(re.findall(r"'([a-z_0-9]+)'", m.group(1)), self.w.HISTORY_COLS)

    def test_refused_twice(self):
        self.ok(); self.summary()
        self.w.do_add({'person': 'me', 'date': '2026-09-13'})
        with self.assertRaises(ValueError):
            self.w.do_add({'person': 'me', 'date': '2026-09-13'})
        self.assertEqual(len(self.rows()), 1)

    def test_keeps_other_rows_and_sorts(self):
        self.history('me', '2026-09-20', luma=120.0)
        self.ok(); self.summary()
        self.w.do_add({'person': 'me', 'date': '2026-09-13'})
        self.assertEqual([r['session_date'] for r in self.rows()], ['2026-09-13', '2026-09-20'])

    def test_promotion_is_logged(self):
        self.ok(); self.summary()
        self.w.do_add({'person': 'me', 'date': '2026-09-13'})
        with open(os.path.join(self.w.results_dir('me'), 'promoted.csv')) as fh:
            self.assertEqual(list(csv.DictReader(fh))[0]['session'], '2026-09-13')

    def test_result_reports_logged_afterwards(self):
        self.ok(); self.summary()
        self.assertFalse(self.w.session_result('me', '2026-09-13')['logged'])
        self.w.do_add({'person': 'me', 'date': '2026-09-13'})
        self.assertTrue(self.w.session_result('me', '2026-09-13')['logged'])

    def test_no_usable_photos_refused(self):
        self.ok(); self.summary(n=0, mean=None)
        with self.assertRaises(ValueError):
            self.w.do_add({'person': 'me', 'date': '2026-09-13'})


class TestPageJavaScript(WebTestCase):
    """The page is built from a Python string, so a `\\n` written as `\\\\n`
    becomes a REAL newline inside a JS string literal. That does not fail
    loudly: the whole script stops parsing, no code runs, and the page renders
    its static header over an empty body — which looks like a server problem
    and is not. It shipped once; these tests are why it cannot again."""

    def script(self):
        import re
        m = re.search(r'<script>(.*?)</script>', self.w.PAGE, re.S)
        self.assertIsNotNone(m, 'no script block in PAGE')
        return m.group(1)

    @staticmethod
    def _strip_regex_literals(line):
        """Drop /.../flags literals so their contents are not counted as quotes.

        Heuristic, not a JS lexer: a `/` only starts a regex where a value
        cannot already have ended, i.e. after ( , = : [ ! & | ? { ; return.
        That covers this file and errs toward leaving text in place, which
        would fail loudly rather than silently pass.
        """
        import re as _re
        return _re.sub(r'(?<=[(,=:\[!&|?{;])\s*/(?:\\.|\[[^\]]*\]|[^/\n\\])+/[gimsuy]*',
                       ' RE ', line)

    def test_no_unterminated_string_literals(self):
        """A literal newline inside a quoted string leaves the line ending
        while still inside that string. Single pass, tracking which quote
        opened, so a nested other-quote (like "'") is not miscounted."""
        for i, line in enumerate(self.script().split('\n'), 1):
            code = self._strip_regex_literals(line)
            quote = None
            esc = False
            for ch in code:
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                elif quote is None and ch in ('"', "'"):
                    quote = ch
                elif quote is not None and ch == quote:
                    quote = None
                elif quote is None and ch == '/' and code[code.index(ch):].startswith('//'):
                    break
            self.assertIsNone(
                quote,
                'line %d ends inside a %s string -- a raw newline in a JS '
                'literal breaks the whole script: %s'
                % (i, quote, line.strip()[:90]))

    def test_brackets_balance(self):
        js = self.script()
        for o, c in (('{', '}'), ('(', ')'), ('[', ']')):
            self.assertEqual(js.count(o), js.count(c),
                             'unbalanced %s%s' % (o, c))

    def test_parses_under_node_if_available(self):
        import shutil as sh
        import subprocess
        node = sh.which('node')
        if not node:
            self.skipTest('node not available')
        import tempfile as tf
        with tf.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
            fh.write(self.script())
            path = fh.name
        r = subprocess.run([node, '--check', path], capture_output=True, text=True)
        os.unlink(path)
        self.assertEqual(r.returncode, 0, 'JS does not parse:\n' + r.stderr)

    def test_load_surfaces_errors(self):
        """A failed state fetch must show a message, never a blank page."""
        self.assertIn('.catch(', self.script())
        self.assertIn('Could not load', self.script())


class TestStaleness(WebTestCase):
    """Importing photos changes what a session IS. Pre-flight must stop
    describing the old set; the checklist must not silently clear."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg', 'IMG_2.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_1.jpg', 'IMG_2.jpg']})

    def per_image(self, files):
        p = os.path.join(self.w.results_dir('me'), '2026-09-13_per_image.csv')
        with open(p, 'w') as fh:
            fh.write('subj_id,file,faceage,status,hard_flags,advisory_flags,'
                     'confidence,n_faces,source_w,source_h,crop_w,crop_h,'
                     'crop_luma_mean,crop_luma_std,face_fill_height_frac,'
                     'face_fill_area_frac,error\n')
            for f in files:
                fh.write('%s,%s,44.1,OK,,,0.999,1,3024,4032,420,520,121.3,41.0,'
                         '0.93,0.51,\n' % (f.split('.')[0], f))

    def test_preflight_available_when_it_matches(self):
        self.per_image(['IMG_1.jpg', 'IMG_2.jpg'])
        r = self.w.do_preflight('me', '2026-09-13')
        self.assertTrue(r['available'])

    def test_preflight_goes_stale_when_photos_are_added(self):
        self.per_image(['IMG_1.jpg', 'IMG_2.jpg'])
        self.put_inbox('IMG_3.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_3.jpg']})
        r = self.w.do_preflight('me', '2026-09-13')
        self.assertFalse(r['available'])
        self.assertTrue(r['stale'])
        self.assertIn('1 added', r['why'])

    def test_preflight_goes_stale_when_photos_are_removed(self):
        self.per_image(['IMG_1.jpg', 'IMG_2.jpg', 'IMG_9.jpg'])
        r = self.w.do_preflight('me', '2026-09-13')
        self.assertTrue(r['stale'])
        self.assertIn('1 removed', r['why'])

    def test_checklist_is_not_cleared_by_import(self):
        """Clearing on import would be a route to re-answer after the number
        is known. It must survive and be flagged instead."""
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.put_inbox('IMG_4.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_4.jpg']})
        self.assertIsNotNone(self.w.read_checklist('me', '2026-09-13'))

    def test_checklist_flagged_stale_after_later_import(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        import time as _t
        self.put_inbox('IMG_5.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_5.jpg']})
        p = os.path.join(self.w.session_dir('me', '2026-09-13'), 'IMG_5.jpg')
        os.utime(p, (_t.time() + 60, _t.time() + 60))
        self.assertTrue(self.w.checklist_stale('me', '2026-09-13'))

    def test_re_answering_while_unscored_keeps_the_old_version(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        r = self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                                 'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.assertTrue(r['checklist']['valid'])
        self.assertEqual(len(r['checklist']['superseded']), 1)
        self.assertIn('Frontal light', r['checklist']['superseded'][0]['failed'][0])

    def test_corrected_to_pass_clears_the_failure_row(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        with open(vfile) as fh:
            self.assertEqual(list(csv.DictReader(fh)), [])

    def test_cannot_re_answer_once_scored(self):
        """The escape hatch closes the moment a number exists."""
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.history('me', '2026-09-13')
        with self.assertRaises(ValueError):
            self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                                 'answers': {k: True for k, _ in self.w.CHECKLIST}})


class TestSessionResult(WebTestCase):
    """After scoring, the number must be visible in the app, not only in the
    chart."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def summary(self, date='2026-09-13', **kw):
        d = {'n': 9, 'n_total_images': 10, 'n_failed': 1, 'n_flagged': 0,
             'fellback_to_flagged': False, 'mean': 44.12, 'median': 44.05,
             'std': 0.81, 'min': 42.9, 'max': 45.4, 'luma': 121.3,
             'session_date': date}
        d.update(kw)
        p = os.path.join(self.w.results_dir('me'), '%s_summary.json' % date)
        with open(p, 'w') as fh:
            json.dump(d, fh)

    def test_none_before_scoring(self):
        self.assertIsNone(self.w.session_result('me', '2026-09-13'))

    def test_reads_the_summary(self):
        self.summary()
        r = self.w.session_result('me', '2026-09-13')
        self.assertAlmostEqual(r['mean'], 44.12)
        self.assertEqual((r['n'], r['n_total'], r['n_failed']), (9, 10, 1))

    def test_exposure_compared_to_baseline(self):
        self.history('me', '2026-09-11', luma=121.3)
        self.summary(luma=128.0)
        r = self.w.session_result('me', '2026-09-13')
        self.assertAlmostEqual(r['luma_delta'], 6.7)
        self.assertFalse(r['luma_ok'])              # outside the +/-5 band

    def test_exposure_within_band(self):
        self.history('me', '2026-09-11', luma=121.3)
        self.summary(luma=123.0)
        self.assertTrue(self.w.session_result('me', '2026-09-13')['luma_ok'])

    def test_protocol_failure_is_marked_out_of_series(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        self.summary()
        r = self.w.session_result('me', '2026-09-13')
        self.assertFalse(r['in_series'])
        self.assertIn('Frontal light', r['excluded_reason'])

    def test_valid_but_unlogged_is_not_in_the_series(self):
        """A one-off: the checklist passed and the number exists, but nothing
        was written. That is exactly the case the Add button is for."""
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.summary()
        r = self.w.session_result('me', '2026-09-13')
        self.assertTrue(r['valid'])
        self.assertFalse(r['logged'])
        self.assertFalse(r['in_series'])
        self.assertTrue(r['has_checklist'])

    def test_valid_and_logged_is_in_series(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.summary()
        self.history('me', '2026-09-13')
        r = self.w.session_result('me', '2026-09-13')
        self.assertTrue(r['logged'])
        self.assertTrue(r['in_series'])

    def test_logged_but_invalid_is_not_in_series(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        self.summary()
        self.history('me', '2026-09-13')
        r = self.w.session_result('me', '2026-09-13')
        self.assertTrue(r['logged'])
        self.assertFalse(r['valid'])
        self.assertFalse(r['in_series'])

    def test_no_pairwise_delta_is_exposed(self):
        """§3: pairwise session deltas are not interpreted. The result must not
        hand the UI a change-since-last-session to render."""
        self.history('me', '2026-09-11', luma=121.3)
        self.summary()
        r = self.w.session_result('me', '2026-09-13')
        for k in r:
            self.assertNotIn('delta', k.replace('luma_delta', ''),
                             'unexpected delta field %s' % k)

    def test_fellback_flag_surfaces(self):
        self.summary(fellback_to_flagged=True)
        self.assertTrue(self.w.session_result('me', '2026-09-13')['fellback'])

    def test_corrupt_summary_does_not_break_state(self):
        p = os.path.join(self.w.results_dir('me'), '2026-09-13_summary.json')
        with open(p, 'w') as fh:
            fh.write('{not json')
        self.assertIsNone(self.w.session_result('me', '2026-09-13'))


class TestTakes(WebTestCase):
    """A reshoot is a new take, not an overwrite. That is the forgiving path:
    §1 stays intact because nothing already recorded is rewritten."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def test_labels_with_take_letters_are_valid(self):
        for good in ('2026-09-12', '2026-09-12b', '2026-09-12z'):
            self.assertEqual(self.w.safe_date(good), good)

    def test_bad_take_letters_refused(self):
        for bad in ('2026-09-12a', '2026-09-12B', '2026-09-12bb', '2026-09-12-b'):
            with self.assertRaises(ValueError, msg=bad):
                self.w.safe_date(bad)

    def test_first_take_is_the_bare_date(self):
        self.assertEqual(self.w.next_take('me', '2026-09-12'), '2026-09-12')

    def test_next_take_skips_a_used_one(self):
        os.makedirs(self.w.session_dir('me', '2026-09-12'))
        self.assertEqual(self.w.next_take('me', '2026-09-12'), '2026-09-12b')

    def test_next_take_skips_a_scored_one_without_a_folder(self):
        self.history('me', '2026-09-12')
        self.assertEqual(self.w.next_take('me', '2026-09-12'), '2026-09-12b')

    def test_take_letter_ignored_when_asking_for_the_next(self):
        os.makedirs(self.w.session_dir('me', '2026-09-12'))
        os.makedirs(self.w.session_dir('me', '2026-09-12b'))
        self.assertEqual(self.w.next_take('me', '2026-09-12b'), '2026-09-12c')

    def test_reshoot_creates_the_folder(self):
        os.makedirs(self.w.session_dir('me', '2026-09-12'))
        r = self.w.do_reshoot({'person': 'me', 'date': '2026-09-12'})
        self.assertEqual(r['session'], '2026-09-12b')
        self.assertTrue(os.path.isdir(self.w.session_dir('me', '2026-09-12b')))

    def test_a_take_has_its_own_checklist(self):
        """The failed take keeps its failure; the reshoot answers fresh."""
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12', 'answers': a})
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12b',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.assertFalse(self.w.read_checklist('me', '2026-09-12')['valid'])
        self.assertTrue(self.w.read_checklist('me', '2026-09-12b')['valid'])

    def test_failed_take_stays_excluded_after_a_reshoot(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12', 'answers': a})
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12b',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r['session_date'] for r in rows], ['2026-09-12'])

    def test_analysis_keys_takes_to_the_same_day(self):
        """The pre-registered analysis parses date[:10], so a take sits on its
        calendar day rather than being dropped."""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import faceage_analysis as fa
        import datetime as dt
        hist = os.path.join(self.w.results_dir('me'), 'faceage_history.csv')
        os.makedirs(os.path.dirname(hist), exist_ok=True)
        with open(hist, 'w') as fh:
            fh.write('session_date,run_timestamp,n,n_total_images,n_failed,'
                     'n_flagged,mean,median,std,min,max,mean_luma,model_sha256,'
                     'image_dir,notes\n')
            fh.write('2026-09-12b,x,10,10,0,0,44.1,44.0,0.8,43,45,121,dd,x,\n')
        rows, _ = fa.load_history(hist, {})
        self.assertEqual(rows[0]['date'], dt.date(2026, 9, 12))
        self.assertEqual(rows[0]['label'], '2026-09-12b')


class TestScoredSessionIsClosed(WebTestCase):
    """Importing into a scored session left a checklist describing an earlier
    capture, a pre-flight describing earlier photos, and a number already in
    the series. Opening the next take resets all three by construction."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg', 'IMG_2.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12',
                          'files': ['IMG_1.jpg']})

    def test_import_into_unscored_session_stays_put(self):
        r = self.w.do_import({'person': 'me', 'date': '2026-09-12',
                              'files': ['IMG_2.jpg']})
        self.assertEqual(r['session'], '2026-09-12')
        self.assertIsNone(r['moved_to_new_take'])
        self.assertEqual(len(r['staged']), 2)

    def test_import_into_scored_session_opens_the_next_take(self):
        self.history('me', '2026-09-12')
        r = self.w.do_import({'person': 'me', 'date': '2026-09-12',
                              'files': ['IMG_2.jpg']})
        self.assertEqual(r['session'], '2026-09-12b')
        self.assertEqual(r['moved_to_new_take'], '2026-09-12b')
        self.assertEqual(r['staged'], ['IMG_2.jpg'])

    def test_the_new_take_starts_clean(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12', 'answers': a})
        self.history('me', '2026-09-12')
        r = self.w.do_import({'person': 'me', 'date': '2026-09-12',
                              'files': ['IMG_2.jpg']})
        new = r['session']
        self.assertIsNone(self.w.read_checklist('me', new))
        self.assertFalse(self.w.session_scored('me', new))
        self.assertIsNone(self.w.session_result('me', new))
        # and the failed take is untouched
        self.assertFalse(self.w.read_checklist('me', '2026-09-12')['valid'])


JPEG_STUB = b'\xff\xd8\xff\xe0' + b'\x00' * 64 + b'\xff\xd9'


def data_url(raw=JPEG_STUB, mime='image/jpeg'):
    import base64
    return 'data:%s;base64,%s' % (mime, base64.b64encode(raw).decode('ascii'))


class TestCapture(WebTestCase):
    """The Mac's camera is an option beside import. The browser grabs the
    frame; this end only writes bytes into the session folder, one frame per
    request, and records how they were taken. Nothing here reaches the
    tracker."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def frame(self, i, date='2026-09-13', batch='20260913-101500', **extra):
        body = {'person': 'me', 'date': date, 'batch': batch, 'index': i,
                'image': data_url(),
                'settings': {'camera': 'FaceTime HD', 'width': 1920,
                             'height': 1080, 'luma': 118.4}}
        body.update(extra)
        return self.w.do_capture(body)

    def test_frame_lands_in_the_session_folder_unchanged(self):
        r = self.frame(1)
        self.assertEqual(r['file'], 'cam_20260913-101500_01.jpg')
        p = os.path.join(self.w.session_dir('me', '2026-09-13'), r['file'])
        with open(p, 'rb') as fh:
            self.assertEqual(fh.read(), JPEG_STUB)
        self.assertEqual(self.w.staged('me', '2026-09-13'), [r['file']])

    def test_ten_frames_stage_ten_photos_in_order(self):
        for i in range(1, 11):
            r = self.frame(i)
        self.assertEqual(len(r['staged']), 10)
        self.assertEqual(r['staged'][0], 'cam_20260913-101500_01.jpg')
        self.assertEqual(r['staged'][-1], 'cam_20260913-101500_10.jpg')

    def test_manifest_records_source_and_settings(self):
        self.frame(1); self.frame(2)
        m = self.w.capture_manifest('me', '2026-09-13')
        self.assertEqual(m['source'], 'mac-camera')
        self.assertEqual(m['settings']['camera'], 'FaceTime HD')
        self.assertEqual([f['index'] for f in m['frames']], [1, 2])
        self.assertEqual(m['frames'][0]['luma'], 118.4)
        self.assertEqual(self.w.session_source('me', '2026-09-13'), 'mac-camera')

    def test_manifest_is_not_a_photo(self):
        self.frame(1)
        self.assertNotIn('capture.json', self.w.staged('me', '2026-09-13'))

    def test_imported_session_has_no_manifest(self):
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13', 'files': ['IMG_1.jpg']})
        self.assertIsNone(self.w.capture_manifest('me', '2026-09-13'))
        self.assertEqual(self.w.session_source('me', '2026-09-13'), 'import')
        self.assertIsNone(self.w.session_source('me', '2026-09-14'))

    def test_scored_session_opens_the_next_take(self):
        self.history('me', '2026-09-13')
        r = self.frame(1)
        self.assertEqual(r['session'], '2026-09-13b')
        self.assertEqual(r['moved_to_new_take'], '2026-09-13b')
        self.assertTrue(os.path.exists(os.path.join(
            self.w.session_dir('me', '2026-09-13b'), r['file'])))

    def test_no_overwrite(self):
        self.frame(1)
        with self.assertRaises(ValueError):
            self.frame(1)

    def test_rejects_non_jpeg(self):
        with self.assertRaises(ValueError):
            self.frame(1, image=data_url(b'\x89PNG\r\n' + b'\x00' * 20, 'image/png'))
        with self.assertRaises(ValueError):
            self.frame(1, image=data_url(b'not a jpeg at all'))
        with self.assertRaises(ValueError):
            self.frame(1, image='data:image/jpeg;base64,***not base64***')
        self.assertEqual(self.w.staged('me', '2026-09-13'), [])

    def test_rejects_bad_batch_or_index(self):
        for bad in ({'batch': '../x'}, {'batch': '2026-09-13'}, {'index': 0},
                    {'index': 100}, {'index': 'one'}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.frame(1, **bad)

    def test_rejects_oversized_frame(self):
        self.w.MAX_CAPTURE_BYTES = 128
        with self.assertRaises(ValueError):
            self.frame(1, image=data_url(b'\xff\xd8\xff' + b'\x00' * 200))

    def test_capture_never_writes_history(self):
        for i in range(1, 4):
            self.frame(i)
        self.assertFalse(os.path.exists(os.path.join(
            self.w.results_dir('me'), 'faceage_history.csv')))
        self.assertFalse(self.w.session_scored('me', '2026-09-13'))

    def test_series_source_follows_the_latest_logged_session(self):
        self.assertIsNone(self.w.series_source('me'))
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-10', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-10')
        self.assertEqual(self.w.series_source('me'), 'import')
        self.frame(1, date='2026-09-13')
        self.history('me', '2026-09-13')
        self.assertEqual(self.w.series_source('me'), 'mac-camera')

    def test_state_exposes_sources(self):
        self.frame(1)
        st = self.w.state('me', '2026-09-13')
        self.assertEqual(st['source'], 'mac-camera')
        self.assertEqual(st['capture']['source'], 'mac-camera')
        self.assertIsNone(st['series_source'])

    def test_discard_takes_the_manifest_with_it(self):
        self.frame(1)
        self.w.do_discard({'person': 'me', 'date': '2026-09-13', 'reason': 'test'})
        self.assertIsNone(self.w.capture_manifest('me', '2026-09-13'))
        self.assertEqual(self.w.staged('me', '2026-09-13'), [])

    def test_route_is_registered(self):
        self.assertIs(self.w.ROUTES_POST['/api/capture'], self.w.do_capture)

    def test_frame_metrics_recorded(self):
        r = self.frame(1, frame={'luma': 117.2, 'fill': 0.86, 'dx': -0.01,
                                 'dy': 0.02, 'aligned': True},
                       settings={'camera': 'FaceTime HD', 'width': 1920, 'height': 1080,
                                 'crop': {'x': 300, 'y': 180, 'w': 540, 'h': 720},
                                 'saved_width': 540, 'saved_height': 720, 'guide': 'pico'})
        m = self.w.capture_manifest('me', '2026-09-13')
        f = m['frames'][0]
        self.assertEqual((f['luma'], f['fill'], f['aligned']), (117.2, 0.86, True))
        self.assertEqual(m['settings']['crop']['h'], 720)
        self.assertEqual(m['settings']['guide'], 'pico')

    def test_static_files_exist_and_are_whitelisted(self):
        for name, ctype in self.w.STATIC.items():
            path = os.path.join(self.w.WEB_DIR, name)
            self.assertTrue(os.path.isfile(path), path)
        self.assertNotIn('..', ''.join(self.w.STATIC))
        self.assertTrue(os.path.isfile(os.path.join(self.w.WEB_DIR, 'LICENSE-pico')),
                        'vendored code ships with its license')


class TestRemoveFromTracker(WebTestCase):
    """A row can leave the tracker without the session leaving the disk, and
    the removal is logged. The session then reads as scored-but-not-logged, so
    it can be added back."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-10', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-10')
        with open(os.path.join(self.w.results_dir('me'), '2026-09-10_summary.json'), 'w') as fh:
            json.dump({'mean': 44.1, 'n': 10, 'luma': 121.3}, fh)

    def test_row_goes_everything_else_stays(self):
        self.history('me', '2026-09-11')
        r = self.w.do_remove({'person': 'me', 'date': '2026-09-10', 'reason': 'wrong lamp'})
        self.assertEqual(r['remaining'], 1)
        self.assertFalse(self.w.session_scored('me', '2026-09-10'))
        self.assertTrue(self.w.session_scored('me', '2026-09-11'))
        self.assertEqual(self.w.staged('me', '2026-09-10'), ['IMG_1.jpg'])
        res = self.w.session_result('me', '2026-09-10')
        self.assertEqual(res['mean'], 44.1)
        self.assertFalse(res['logged'], 'can be added back')

    def test_logged_with_reason(self):
        self.w.do_remove({'person': 'me', 'date': '2026-09-10', 'reason': 'wrong, lamp'})
        with open(os.path.join(self.w.results_dir('me'), 'removed.csv')) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['session'], '2026-09-10')
        self.assertEqual(rows[0]['reason'], 'wrong; lamp')
        self.assertEqual(rows[0]['after_baseline_anchor'], 'no')
        self.assertEqual(rows[0]['mean'], '44.1')

    def test_after_anchor_is_flagged_not_refused(self):
        self.history('me', '2026-09-11')
        with open(os.path.join(self.w.results_dir('me'), 'anchors.csv'), 'w') as fh:
            fh.write('anchor,date,note\nB,2026-09-11,first Mac session\n')
        r = self.w.do_remove({'person': 'me', 'date': '2026-09-11', 'reason': ''})
        self.assertTrue(r['after_baseline_anchor'])
        with open(os.path.join(self.w.results_dir('me'), 'removed.csv')) as fh:
            self.assertIn(',yes,(none given)', fh.read())

    def test_not_in_tracker_refused(self):
        with self.assertRaises(ValueError):
            self.w.do_remove({'person': 'me', 'date': '2026-09-12', 'reason': ''})

    def test_route_is_registered(self):
        self.assertIs(self.w.ROUTES_POST['/api/remove'], self.w.do_remove)

    def test_tracker_names_the_camera_and_offers_remove(self):
        import base64
        self.w.do_capture({'person': 'me', 'date': '2026-09-11', 'batch': '20260911-101500',
                           'index': 1, 'image': data_url(), 'settings': {}})
        self.history('me', '2026-09-11')
        out = self.w.build_chart('me')
        with open(out) as fh:
            page = fh.read()
        self.assertIn('>Camera</span>', page)
        self.assertIn('<td class="d">2026-09-10</td><td>Phone</td>', page)
        self.assertIn('<td class="d">2026-09-11</td><td>Mac</td>', page)
        self.assertIn('more than one camera', page)
        self.assertIn('Two cameras in the mix', page)
        self.assertIn('class="btn sm del" data-session="2026-09-11"', page)
        self.assertIn("'/api/delete'", page)


class TestReveal(WebTestCase):
    """Opening a folder in Finder is the one thing here that leaves the
    browser. The path is built server-side from person and date, never taken
    from the request, and only the session or results folder can be opened."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.opened = []
        self.w.reveal_path = lambda p: self.opened.append(p)

    def test_session_folder(self):
        os.makedirs(self.w.session_dir('me', '2026-09-13'))
        r = self.w.do_reveal({'person': 'me', 'date': '2026-09-13'})
        self.assertEqual(self.opened, [self.w.session_dir('me', '2026-09-13')])
        self.assertEqual(r['path'], self.w.session_dir('me', '2026-09-13'))

    def test_results_folder(self):
        os.makedirs(self.w.results_dir('me'), exist_ok=True)
        self.w.do_reveal({'person': 'me', 'what': 'results'})
        self.assertEqual(self.opened, [self.w.results_dir('me')])

    def test_nothing_else(self):
        for body in ({'person': 'me', 'what': '/etc'}, {'person': 'me', 'date': '../x'},
                     {'person': 'me', 'date': '2026-09-13'}):      # last: no folder yet
            with self.assertRaises(ValueError, msg=str(body)):
                self.w.do_reveal(body)
        self.assertEqual(self.opened, [])

    def test_route_and_tracker_links(self):
        self.assertIs(self.w.ROUTES_POST['/api/reveal'], self.w.do_reveal)
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-10', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-10')
        with open(self.w.build_chart('me')) as fh:
            page = fh.read()
        self.assertIn('class="btn sm fo" data-session="2026-09-10"', page)
        self.assertIn('id="open-results"', page)
        self.assertIn("'/api/reveal'", page)


class TestDelete(WebTestCase):
    """The one path that destroys data. It removes the tracker row, the
    photos, the results and the checklist, logs the date and number, and
    touches nothing else."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        for d in ('2026-09-06', '2026-09-11', '2026-09-12'):
            self.put_inbox('IMG_%s.jpg' % d)
            self.w.do_import({'person': 'me', 'date': d, 'files': ['IMG_%s.jpg' % d]})
            self.history('me', d)
            with open(os.path.join(self.w.results_dir('me'), '%s_summary.json' % d), 'w') as fh:
                json.dump({'mean': 44.1, 'n': 10, 'luma': 121.3}, fh)
            self.w.do_checklist({'person': 'me', 'date': d, 'answers': {}}) if False else None

    def test_deletes_rows_and_files_for_the_given_sessions(self):
        r = self.w.do_delete({'person': 'me', 'dates': ['2026-09-06', '2026-09-11']})
        self.assertEqual(r['deleted'], ['2026-09-06', '2026-09-11'])
        for d in ('2026-09-06', '2026-09-11'):
            self.assertFalse(os.path.exists(self.w.session_dir('me', d)))
            self.assertFalse(os.path.exists(os.path.join(self.w.results_dir('me'), '%s_summary.json' % d)))
            self.assertFalse(self.w.session_scored('me', d))
        self.assertTrue(self.w.session_scored('me', '2026-09-12'))
        self.assertEqual(self.w.staged('me', '2026-09-12'), ['IMG_2026-09-12.jpg'])

    def test_logged(self):
        self.w.do_delete({'person': 'me', 'dates': ['2026-09-06']})
        with open(os.path.join(self.w.results_dir('me'), 'deleted.csv')) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['session'], '2026-09-06')
        self.assertEqual(rows[0]['mean'], '44.1')

    def test_unknown_session_is_a_no_op(self):
        r = self.w.do_delete({'person': 'me', 'dates': ['2026-09-30']})
        self.assertEqual(r['deleted'], [])
        self.assertFalse(os.path.exists(os.path.join(self.w.results_dir('me'), 'deleted.csv')))

    def test_bad_input_refused(self):
        for body in ({'person': 'me'}, {'person': 'me', 'dates': ['../x']},
                     {'person': 'me', 'dates': 'no'}):
            with self.assertRaises(ValueError, msg=str(body)):
                self.w.do_delete(body)

    def test_route(self):
        self.assertIs(self.w.ROUTES_POST['/api/delete'], self.w.do_delete)


class TestTrackerPage(WebTestCase):
    """The tracker is a Python string wrapped around JavaScript. A stray escape
    turns into a newline inside a JS literal and the whole page goes dead:
    folder, delete and notes all stop working with no error on screen. That
    shipped once. So: execute the script through node, every time."""

    def build(self):
        self.person()
        for d, luma in (('2026-09-06', 128.6), ('2026-09-11', 123.9)):
            self.put_inbox('IMG_%s.jpg' % d)
            self.w.do_import({'person': 'me', 'date': d, 'files': ['IMG_%s.jpg' % d]})
            self.history('me', d, luma=luma)
        self.w.do_capture({'person': 'me', 'date': '2026-09-12', 'batch': '20260912-101500',
                           'index': 1, 'image': data_url(), 'settings': {}})
        self.history('me', '2026-09-12', luma=173.3)
        with open(self.w.build_chart('me')) as fh:
            return fh.read()

    def test_script_parses(self):
        import re
        import shutil as sh
        import subprocess
        import tempfile
        node = sh.which('node')
        if not node:
            self.skipTest('node not available')
        page = self.build()
        js = re.search(r'<script>(.*?)</script>', page, re.S).group(1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            r = subprocess.run([node, '--check', path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_raw_newline_inside_a_js_string(self):
        import re
        page = self.build()
        js = re.search(r'<script>(.*?)</script>', page, re.S).group(1)
        for i, line in enumerate(js.split('\n'), 1):
            stripped = re.sub(r"'(?:[^'\\]|\\.)*'", '', line)
            stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '', stripped)
            self.assertNotIn("'", re.sub(r'//.*$', '', stripped),
                             'line %d ends inside a string: %s' % (i, line[:80]))

    def test_consumer_copy(self):
        page = self.build()
        body = page.split('<body>', 1)[1].split('<script>', 1)[0]
        self.assertNotIn('\u2014', body, 'no em dashes on the tracker')
        self.assertNotIn('\u00a7', body, 'no section references on the tracker')
        for term in ('Fitted slope', 'CI includes zero', 'NOT DETECTED', 'OLS'):
            self.assertNotIn(term, body.split('<details', 1)[0],
                             '%r belongs under the details disclosure, not on the page' % term)
        self.assertIn('Your FaceAge, latest session', page)
        self.assertIn('Too early to call', page) if 'Two cameras' not in page else None
        self.assertIn('>Setup</span>', page)
        self.assertIn('of 3 shot cleanly', page)
        self.assertIn('<td class="chev"><button class="more"', page)
        self.assertIn('The numbers behind this', page)

    def test_brightness_compares_within_camera(self):
        page = self.build()
        # the Mac session is the first on its camera: it is the baseline, not a drift
        self.assertIn('<td class="d">2026-09-12</td><td>Mac</td>', page)
        self.assertIn('✓ baseline</span>', page)
        self.assertIn('(baseline)', page)


class TestFlags(WebTestCase):
    """Conditions are recorded as exceptions: tap what is different, and the
    absence of flags is the all-clear. Each flag fails the condition it belongs
    to, the record is written before any number exists, and camera changes are
    measured rather than asked."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def doc(self):
        return self.w.read_checklist('me', '2026-09-13')

    def test_no_flags_is_all_clear(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'flags': [], 'source': 'mac-camera'})
        d = self.doc()
        self.assertTrue(d['valid'])
        self.assertEqual(d['failed'], [])
        self.assertEqual(d['flags'], [])
        self.assertTrue(all(d['answers'].values()))

    def test_each_flag_fails_its_condition(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'flags': ['shave', 'sleep'], 'source': 'mac-camera'})
        d = self.doc()
        self.assertFalse(d['valid'])
        self.assertFalse(d['answers']['grooming'])
        self.assertFalse(d['answers']['photoday'])
        self.assertTrue(d['answers']['light'])
        self.assertEqual(d['failed'], ['did not shave', 'poor sleep'])
        with open(os.path.join(self.w.results_dir('me'), 'session_validity.csv')) as fh:
            self.assertIn('2026-09-13,no,did not shave; poor sleep', fh.read())

    def test_something_else_carries_its_note(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'flags': ['other'], 'note': 'new glasses', 'source': 'mac-camera'})
        d = self.doc()
        self.assertFalse(d['valid'])
        self.assertEqual(d['failed'], ['something else: new glasses'])

    def test_unknown_flag_refused(self):
        with self.assertRaises(ValueError):
            self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'flags': ['hat']})

    def test_camera_change_is_measured_once_the_study_has_started(self):
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-09', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-09')
        # before the start line: rehearsal, nothing is failed for it
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'flags': [], 'source': 'mac-camera'})
        self.assertTrue(self.doc()['valid'])
        # after: a different camera from the series fails the camera condition by itself
        with open(os.path.join(self.w.results_dir('me'), 'anchors.csv'), 'w') as fh:
            fh.write('anchor,date,note\nB,2026-09-09,x\n')
        self.w.do_checklist({'person': 'me', 'date': '2026-09-14', 'flags': [], 'source': 'mac-camera'})
        d = self.w.read_checklist('me', '2026-09-14')
        self.assertFalse(d['valid'])
        self.assertFalse(d['answers']['camera'])
        self.assertIn('different camera', d['failed'][0])

    def test_old_answers_form_still_accepted(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.assertTrue(self.doc()['valid'])
        self.assertIsNone(self.doc()['flags'])

    def test_still_refused_after_scoring(self):
        self.history('me', '2026-09-13')
        with self.assertRaises(ValueError):
            self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'flags': []})

    def test_embedded_tracker_hides_its_own_header(self):
        import io
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-09', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-09')
        out = self.w.build_chart('me')
        with open(out) as fh:
            page = fh.read()
        self.assertIn("postMessage({faceage:'height'", page)


class TestFillCalibration(WebTestCase):
    """The live face-size estimate is gated against the pipeline bar, so the
    conversion between the two must come from this person's own frames once
    they exist. Otherwise the guide passes frames the pipeline sets aside."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def capture(self, date, raws, batch='20260913-101500'):
        for i, raw in enumerate(raws, 1):
            self.w.do_capture({'person': 'me', 'date': date, 'batch': batch, 'index': i,
                               'image': data_url(), 'settings': {},
                               'frame': {'fill_raw': raw, 'fill': round(raw * 1.12, 3)}})

    def per_image(self, date, fills):
        p = os.path.join(self.w.results_dir('me'), '%s_per_image.csv' % date)
        with open(p, 'w') as fh:
            fh.write('subj_id,file,faceage,status,hard_flags,advisory_flags,confidence,n_faces,'
                     'source_w,source_h,crop_w,crop_h,crop_luma_mean,crop_luma_std,'
                     'face_fill_height_frac,face_fill_area_frac,error\n')
            for i, fill in enumerate(fills, 1):
                fh.write('x,cam_20260913-101500_%02d.jpg,44.1,OK,,,0.999,1,540,720,300,%d,121.3,41,%.4f,0.5,\n'
                         % (i, int(720 * fill), fill))

    def test_nothing_until_five_matched_frames(self):
        self.capture('2026-09-13', [0.788] * 4)
        self.per_image('2026-09-13', [0.79] * 4)
        self.assertIsNone(self.w.fill_calibration('me'))
        self.assertIsNone(self.w.state('me', '2026-09-14')['fill_calibration'])

    def test_learns_the_ratio_from_matched_frames(self):
        # the guide estimated 0.788 * 1.12 = 0.88, the pipeline measured 0.79
        self.capture('2026-09-13', [0.788] * 10)
        self.per_image('2026-09-13', [0.79] * 10)
        c = self.w.fill_calibration('me')
        self.assertEqual(c['n'], 10)
        self.assertAlmostEqual(c['k'], 0.79 / 0.788, places=2)
        self.assertLess(c['k'], 1.12, 'this face maps lower than the default')

    def test_median_ignores_an_odd_frame(self):
        self.capture('2026-09-13', [0.788] * 9 + [0.30])
        self.per_image('2026-09-13', [0.79] * 9 + [0.79])
        self.assertAlmostEqual(self.w.fill_calibration('me')['k'], 0.79 / 0.788, places=2)

    def test_unscored_sessions_do_not_count(self):
        self.capture('2026-09-13', [0.788] * 10)
        self.assertIsNone(self.w.fill_calibration('me'))


class TestBirthday(WebTestCase):
    """FaceAge only means something against a real age. The birthday is kept
    in the person's own folder, validated, and the tracker uses it for the
    gap, the real-age line, and the aging rate."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def test_set_and_clear(self):
        r = self.w.do_profile({'name': 'me', 'birthday': '1982-03-04'})
        self.assertEqual(r['birthday'], '1982-03-04')
        p = [x for x in self.w.list_people() if x['name'] == 'me'][0]
        self.assertEqual(p['birthday'], '1982-03-04')
        self.assertGreater(p['age'], 40)
        self.w.do_profile({'name': 'me', 'birthday': ''})
        self.assertIsNone([x for x in self.w.list_people() if x['name'] == 'me'][0]['birthday'])

    def test_rejects_nonsense(self):
        for bad in ('yesterday', '2030-01-01', '1800-01-01', '2020-13-01'):
            with self.assertRaises(ValueError, msg=bad):
                self.w.do_profile({'name': 'me', 'birthday': bad})

    def test_route(self):
        self.assertIs(self.w.ROUTES_POST['/api/person/profile'], self.w.do_profile)

    def test_tracker_shows_the_gap_and_the_age_line(self):
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-12')                 # mean 44.1
        self.w.do_profile({'name': 'me', 'birthday': '1980-09-12'})   # exactly 46.0 that day
        with open(self.w.build_chart('me')) as fh:
            page = fh.read()
        self.assertIn('<b>1.9 years younger</b> than your age of 46.0', page)
        self.assertIn('class="agelin"', page)
        self.assertIn('>your age<', page)
        self.assertIn('data-range="30d"', page)
        self.assertIn('class="rng on" data-range="study"', page)
        self.assertIn('data-range="all"', page)

    def test_tracker_without_birthday_invites_one(self):
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-12')
        with open(self.w.build_chart('me')) as fh:
            page = fh.read()
        self.assertIn('Add your birthday', page)
        self.assertNotIn('class="agelin"', page)


class TestLumaCalibration(WebTestCase):
    """The camera screen should hold a session to the analysis baseline in the
    analysis's own units. The offset between the live readout and the
    pipeline is learned from matched frames, current readout method only."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def capture(self, date, lives, method='box-rgb-2'):
        for i, live in enumerate(lives, 1):
            self.w.do_capture({'person': 'me', 'date': date, 'batch': '20260913-101500', 'index': i,
                               'image': data_url(), 'settings': {'luma_method': method},
                               'frame': {'luma': live}})

    def per_image(self, date, lumas):
        p = os.path.join(self.w.results_dir('me'), '%s_per_image.csv' % date)
        with open(p, 'w') as fh:
            fh.write('subj_id,file,faceage,status,hard_flags,advisory_flags,confidence,n_faces,'
                     'source_w,source_h,crop_w,crop_h,crop_luma_mean,crop_luma_std,'
                     'face_fill_height_frac,face_fill_area_frac,error\n')
            for i, l in enumerate(lumas, 1):
                fh.write('x,cam_20260913-101500_%02d.jpg,44.1,OK,,,0.999,1,540,720,300,600,%.1f,41,0.85,0.5,\n' % (i, l))

    def test_learns_the_offset(self):
        self.capture('2026-09-13', [101.0] * 10)
        self.per_image('2026-09-13', [173.3] * 10)
        c = self.w.luma_calibration('me')
        self.assertEqual(c['n'], 10)
        self.assertAlmostEqual(c['offset'], 72.3, places=1)
        self.assertEqual(self.w.state('me', '2026-09-14')['luma_calibration']['offset'], 72.3)

    def test_ignores_old_readout_method(self):
        self.capture('2026-09-13', [104.0] * 10, method=None)
        self.per_image('2026-09-13', [173.3] * 10)
        self.assertIsNone(self.w.luma_calibration('me'))

    def test_needs_five_frames(self):
        self.capture('2026-09-13', [101.0] * 4)
        self.per_image('2026-09-13', [173.3] * 4)
        self.assertIsNone(self.w.luma_calibration('me'))


class TestTolerance(WebTestCase):
    """One brightness tolerance, read from the data folder's settings, applied
    by the result, the pre-flight and the tracker alike."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        os.environ.pop('FACEAGE_LUMA_TOL', None)

    def tearDown(self):
        os.environ.pop('FACEAGE_LUMA_TOL', None)
        WebTestCase.tearDown(self)

    def test_default_is_five(self):
        self.assertEqual(self.w.luma_tol(), 5.0)
        self.assertEqual(self.w.pf.LUMA_TOL, 5.0)

    def test_setting_applies_everywhere(self):
        with open(os.path.join(self.data, 'settings.json'), 'w') as fh:
            json.dump({'luma_tol': 8, 'luma_tol_reason': 'sensitivity test'}, fh)
        self.assertEqual(self.w.luma_tol(), 8.0)
        self.assertEqual(self.w.pf.LUMA_TOL, 8.0)
        self.assertEqual(os.environ['FACEAGE_LUMA_TOL'], '8.0')
        self.assertEqual(self.w.state('me', '2026-09-13')['luma_tol'], 8.0)
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-12', luma=121.3)
        self.w.do_import({'person': 'me', 'date': '2026-09-13', 'files': ['IMG_1.jpg']}) if False else None
        with open(self.w.build_chart('me')) as fh:
            self.assertIn('within 8 of the first session', fh.read())

    def test_result_uses_it(self):
        with open(os.path.join(self.data, 'settings.json'), 'w') as fh:
            json.dump({'luma_tol': 8}, fh)
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12', 'files': ['IMG_1.jpg']})
        self.history('me', '2026-09-12', luma=173.3)
        self.w.do_import({'person': 'me', 'date': '2026-09-13', 'files': ['IMG_1.jpg']})
        with open(os.path.join(self.w.results_dir('me'), '2026-09-13_summary.json'), 'w') as fh:
            json.dump({'mean': 40.2, 'n': 10, 'luma': 179.9}, fh)
        r = self.w.session_result('me', '2026-09-13')
        self.assertTrue(r['luma_ok'], '+6.6 is inside a tolerance of 8')


class TestCameraIdentity(WebTestCase):
    """A Studio Display camera and a built-in camera are different
    instruments. Each gets its own baseline and its own tolerance, and a
    session from one is warned about against a tracker from the other."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def cam_session(self, date, label, luma):
        self.w.do_capture({'person': 'me', 'date': date, 'batch': '20260913-101500', 'index': 1,
                           'image': data_url(), 'settings': {'camera': label} if label else {},
                           'frame': {'luma': 100.0}})
        self.history('me', date, luma=luma)

    def test_camera_is_named(self):
        self.cam_session('2026-09-10', 'Studio Display Camera', 173.3)
        self.cam_session('2026-09-11', None, 170.0)
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-09', 'files': ['IMG_1.jpg']})
        self.assertEqual(self.w.session_camera('me', '2026-09-10'), 'Studio Display Camera')
        self.assertEqual(self.w.session_camera('me', '2026-09-11'), 'mac-camera')
        self.assertEqual(self.w.session_camera('me', '2026-09-09'), 'phone')
        self.assertIsNone(self.w.session_camera('me', '2026-09-20'))

    def test_baselines_are_per_camera(self):
        self.cam_session('2026-09-10', 'Studio Display Camera', 173.3)
        self.cam_session('2026-09-11', 'FaceTime HD Camera', 140.0)
        b = self.w.baselines_by_camera('me')
        self.assertEqual(b['Studio Display Camera']['luma'], 173.3)
        self.assertEqual(b['FaceTime HD Camera']['luma'], 140.0)
        self.assertEqual(self.w.baseline_luma('me', 'mac-camera', 'FaceTime HD Camera'), 140.0)
        self.assertEqual(self.w.baseline_luma('me', 'mac-camera', 'Studio Display Camera'), 173.3)
        self.assertEqual(self.w.series_camera('me'), 'FaceTime HD Camera')

    def test_unnamed_old_session_matches_any_mac(self):
        self.assertTrue(self.w.same_camera('mac-camera', 'Studio Display Camera'))
        self.assertFalse(self.w.same_camera('phone', 'Studio Display Camera'))
        self.assertFalse(self.w.same_camera('FaceTime HD Camera', 'Studio Display Camera'))

    def test_tolerance_per_camera(self):
        with open(os.path.join(self.data, 'settings.json'), 'w') as fh:
            json.dump({'luma_tol': 5, 'luma_tol_by_camera': {'Studio Display Camera': 10}}, fh)
        self.assertEqual(self.w.luma_tol('Studio Display Camera'), 10.0)
        self.assertEqual(self.w.luma_tol('FaceTime HD Camera'), 5.0)
        self.assertEqual(self.w.luma_tol(), 5.0)
        self.assertEqual(self.w.tolerances()['by_camera']['Studio Display Camera'], 10)

    def test_result_uses_the_cameras_own_tolerance(self):
        with open(os.path.join(self.data, 'settings.json'), 'w') as fh:
            json.dump({'luma_tol_by_camera': {'Studio Display Camera': 10}}, fh)
        self.cam_session('2026-09-10', 'Studio Display Camera', 173.3)
        self.w.do_capture({'person': 'me', 'date': '2026-09-13', 'batch': '20260913-101500', 'index': 1,
                           'image': data_url(), 'settings': {'camera': 'Studio Display Camera'}})
        with open(os.path.join(self.w.results_dir('me'), '2026-09-13_summary.json'), 'w') as fh:
            json.dump({'mean': 39.5, 'n': 10, 'luma': 180.4}, fh)
        r = self.w.session_result('me', '2026-09-13')
        self.assertEqual(r['luma_delta'], 7.1)
        self.assertTrue(r['luma_ok'], '+7.1 passes this camera\'s tolerance of 10')

    def test_a_different_mac_camera_fails_the_camera_condition_after_anchor(self):
        self.cam_session('2026-09-10', 'Studio Display Camera', 173.3)
        with open(os.path.join(self.w.results_dir('me'), 'anchors.csv'), 'w') as fh:
            fh.write('anchor,date,note\nB,2026-09-10,x\n')
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'flags': [], 'source': 'mac-camera',
                             'camera': 'FaceTime HD Camera'})
        d = self.w.read_checklist('me', '2026-09-13')
        self.assertFalse(d['answers']['camera'])
        self.assertIn('FaceTime HD camera, not Studio Display camera', d['failed'][0])
        self.w.do_checklist({'person': 'me', 'date': '2026-09-14', 'flags': [], 'source': 'mac-camera',
                             'camera': 'Studio Display Camera'})
        self.assertTrue(self.w.read_checklist('me', '2026-09-14')['valid'])

    def test_tracker_names_each_camera(self):
        self.cam_session('2026-09-10', 'Studio Display Camera', 173.3)
        self.cam_session('2026-09-11', 'FaceTime HD Camera', 140.0)
        with open(self.w.build_chart('me')) as fh:
            page = fh.read()
        self.assertIn('<td class="d">2026-09-10</td><td>Studio Display</td>', page)
        self.assertIn('<td class="d">2026-09-11</td><td>FaceTime HD</td>', page)
        self.assertIn('Two cameras in the mix', page)


class TestBaselinePerCamera(WebTestCase):
    """The exposure baseline is the first logged session's face brightness.
    That number only means something against the same camera, so a session
    is held to the first logged session shot the same way it was."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def imported(self, date, luma):
        self.put_inbox('IMG_%s.jpg' % date)
        self.w.do_import({'person': 'me', 'date': date, 'files': ['IMG_%s.jpg' % date]})
        self.history('me', date, luma=luma)

    def captured(self, date, luma):
        import base64
        self.w.do_capture({'person': 'me', 'date': date, 'batch': '20260913-101500',
                           'index': 1, 'image': data_url(), 'settings': {}})
        self.history('me', date, luma=luma)

    def test_no_history_no_baseline(self):
        self.assertIsNone(self.w.baseline_info('me'))
        self.assertIsNone(self.w.baseline_luma('me', 'mac-camera'))

    def test_without_a_source_it_is_the_first_session(self):
        self.imported('2026-09-09', 128.6)
        self.captured('2026-09-12', 119.0)
        b = self.w.baseline_info('me')
        self.assertEqual((b['luma'], b['date'], b['source']), (128.6, '2026-09-09', 'import'))

    def test_mac_session_is_not_held_to_a_phone_baseline(self):
        self.imported('2026-09-09', 128.6)
        self.assertIsNone(self.w.baseline_info('me', 'mac-camera'))
        self.captured('2026-09-12', 119.0)
        b = self.w.baseline_info('me', 'mac-camera')
        self.assertEqual((b['luma'], b['date']), (119.0, '2026-09-12'))
        self.assertEqual(self.w.baseline_luma('me', 'import'), 128.6)

    def test_first_by_date_not_by_row_order(self):
        self.imported('2026-09-11', 130.0)
        self.imported('2026-09-09', 128.6)      # appended later, earlier date
        self.assertEqual(self.w.baseline_info('me', 'import')['date'], '2026-09-09')

    def test_state_carries_the_baseline_for_this_camera(self):
        self.imported('2026-09-09', 128.6)
        self.w.do_capture({'person': 'me', 'date': '2026-09-13', 'batch': '20260913-101500',
                           'index': 1, 'image': data_url(), 'settings': {}})
        st = self.w.state('me', '2026-09-13')
        self.assertIsNone(st['baseline'], 'a Mac session has no phone baseline')
        self.assertIsNone(st['baseline_luma'])
        st = self.w.state('me', '2026-09-09')
        self.assertEqual(st['baseline']['luma'], 128.6)

    def test_mac_baseline_carries_its_live_readings(self):
        for i in (1, 2):
            self.w.do_capture({'person': 'me', 'date': '2026-09-12', 'batch': '20260912-101500',
                               'index': i, 'image': data_url(), 'settings': {'luma_method': 'box-rgb-2'},
                               'frame': {'luma': 100.0 + i}})
        self.history('me', '2026-09-12', luma=173.3)
        b = self.w.baseline_info('me', 'mac-camera')
        self.assertEqual(b['luma'], 173.3, 'the pipeline number stays the study baseline')
        self.assertEqual(b['live_luma'], 101.5, 'the live readout compares to what it said then')
        self.assertEqual(b['live_method'], 'box-rgb-2', 'and only against readings taken the same way')
        self.assertNotIn('live_luma', self.w.baseline_info('me', 'import') or {})

    def test_camera_step_gets_the_mac_baseline_before_any_frame_exists(self):
        self.imported('2026-09-06', 128.6)
        st = self.w.state('me', '2026-09-13')          # empty session, no source yet
        self.assertEqual(st['baseline']['luma'], 128.6, 'generic baseline is the first row')
        self.assertIsNone(st['baseline_camera'], 'but the Mac camera has none yet')
        self.captured('2026-09-12', 115.0)
        st = self.w.state('me', '2026-09-13')
        self.assertEqual(st['baseline_camera']['luma'], 115.0)
        self.assertEqual(st['baseline_camera']['date'], '2026-09-12')

    def test_session_result_compares_like_with_like(self):
        self.imported('2026-09-09', 128.6)
        self.captured('2026-09-12', 119.0)
        self.captured('2026-09-13', 121.0)
        with open(os.path.join(self.w.results_dir('me'), '2026-09-13_summary.json'), 'w') as fh:
            json.dump({'mean': 44.0, 'n': 10, 'luma': 121.0}, fh)
        r = self.w.session_result('me', '2026-09-13')
        self.assertEqual(r['baseline_luma'], 119.0)
        self.assertEqual(r['luma_delta'], 2.0)
        self.assertTrue(r['luma_ok'])


class TestDiscard(WebTestCase):
    """Deleting the photos left every other trace behind, because the summary,
    QA, checklist, validity row and history row key off the label rather than
    the folder. Discard has to remove all of them."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-12',
                          'files': ['IMG_1.jpg']})
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        self.w.do_checklist({'person': 'me', 'date': '2026-09-12', 'answers': a})
        self.history('me', '2026-09-12')
        res = self.w.results_dir('me')
        with open(os.path.join(res, '2026-09-12_summary.json'), 'w') as fh:
            json.dump({'mean': 45.62, 'n': 14, 'n_total_images': 14}, fh)
        with open(os.path.join(res, '2026-09-12_per_image.csv'), 'w') as fh:
            fh.write('subj_id,file,faceage,status,hard_flags,advisory_flags,'
                     'confidence,n_faces,source_w,source_h,crop_w,crop_h,'
                     'crop_luma_mean,crop_luma_std,face_fill_height_frac,'
                     'face_fill_area_frac,error\n')

    def test_everything_is_gone_afterwards(self):
        r = self.w.do_discard({'person': 'me', 'date': '2026-09-12',
                               'reason': 'bad light'})
        self.assertTrue(r['ok'])
        self.assertEqual(self.w.staged('me', '2026-09-12'), [])
        self.assertIsNone(self.w.read_checklist('me', '2026-09-12'))
        self.assertIsNone(self.w.session_result('me', '2026-09-12'))
        self.assertFalse(self.w.session_scored('me', '2026-09-12'))
        pf = self.w.do_preflight('me', '2026-09-12')
        self.assertFalse(pf['available'])

    def test_validity_row_is_removed_too(self):
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)
        self.w.do_discard({'person': 'me', 'date': '2026-09-12', 'reason': 'x'})
        with open(vfile) as fh:
            self.assertEqual(list(csv.DictReader(fh)), [])

    def test_it_goes_to_the_trash_as_one_folder(self):
        """Photographs go to the Trash, where they can be put back, and
        nothing stays inside the data folder."""
        trash_dir = self.trash
        r = self.w.do_discard({'person': 'me', 'date': '2026-09-12', 'reason': 'x'})
        bin_dir = r['archived_to']
        self.assertTrue(bin_dir.startswith(trash_dir), bin_dir)
        self.assertIn('FaceAge me 2026-09-12 (discarded', os.path.basename(bin_dir))
        self.assertTrue(os.path.isfile(os.path.join(bin_dir, 'photos', 'IMG_1.jpg')))
        self.assertTrue(os.path.isfile(os.path.join(bin_dir, 'checklist.json')))
        self.assertTrue(os.path.isfile(os.path.join(bin_dir, '2026-09-12_summary.json')))
        self.assertFalse(os.path.exists(os.path.join(self.w.subj_dir('me'), 'discarded')))
        self.assertEqual([d for d in os.listdir(self.w.subj_dir('me')) if 'discarded' in d], [])

    def test_the_discard_is_logged_with_its_reason(self):
        self.w.do_discard({'person': 'me', 'date': '2026-09-12',
                           'reason': 'overhead light'})
        log = os.path.join(self.w.results_dir('me'), 'discarded.csv')
        with open(log) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['session'], '2026-09-12')
        self.assertEqual(rows[0]['was_scored'], 'yes')
        self.assertEqual(rows[0]['reason'], 'overhead light')

    def test_other_sessions_are_untouched(self):
        self.history('me', '2026-09-11')
        self.w.do_discard({'person': 'me', 'date': '2026-09-12', 'reason': 'x'})
        self.assertTrue(self.w.session_scored('me', '2026-09-11'))

    def test_the_label_is_reusable_afterwards(self):
        """Start fresh means the same date works again, not a take letter."""
        self.w.do_discard({'person': 'me', 'date': '2026-09-12', 'reason': 'x'})
        self.assertEqual(self.w.next_take('me', '2026-09-12'), '2026-09-12')
        r = self.w.do_import({'person': 'me', 'date': '2026-09-12',
                              'files': ['IMG_1.jpg']})
        self.assertIsNone(r['moved_to_new_take'])

    def test_job_log_is_cleared(self):
        """The last run's output described the session just removed; leaving it
        on screen reads as 'nothing happened'."""
        self.w.JOB.running = False
        self.w.JOB.log = ['Scoring 14 photo(s)', 'done']
        self.w.JOB.rc = 0
        self.w.do_discard({'person': 'me', 'date': '2026-09-12', 'reason': 'x'})
        snap = self.w.JOB.snapshot()
        self.assertEqual(snap['log'], [])
        self.assertIsNone(snap['rc'])

    def test_a_running_job_is_not_cleared(self):
        self.w.JOB.running = True
        self.w.JOB.log = ['in progress']
        try:
            self.assertFalse(self.w.JOB.clear())
            self.assertEqual(self.w.JOB.snapshot()['log'], ['in progress'])
        finally:
            self.w.JOB.running = False
            self.w.JOB.log = []

    def test_discarding_an_empty_session_is_harmless(self):
        r = self.w.do_discard({'person': 'me', 'date': '2026-09-20', 'reason': ''})
        self.assertTrue(r['ok'])
        self.assertEqual(r['removed'], [])


class TestPrefill(WebTestCase):
    def setUp(self):
        WebTestCase.setUp(self)
        self.person()

    def test_none_when_there_is_no_history(self):
        self.assertIsNone(self.w.previous_checklist('me', '2026-09-13'))

    def test_finds_the_most_recent_earlier_checklist(self):
        for d in ('2026-09-11', '2026-09-12'):
            self.w.do_checklist({'person': 'me', 'date': d,
                                 'answers': {k: True for k, _ in self.w.CHECKLIST}})
        r = self.w.previous_checklist('me', '2026-09-13')
        self.assertEqual(r['session_date'], '2026-09-12')

    def test_ignores_later_sessions(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-20',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.assertIsNone(self.w.previous_checklist('me', '2026-09-13'))

    def test_prefill_is_not_an_answer(self):
        """Nothing is recorded by looking one up -- the user still submits."""
        self.w.do_checklist({'person': 'me', 'date': '2026-09-11',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.w.previous_checklist('me', '2026-09-13')
        self.assertIsNone(self.w.read_checklist('me', '2026-09-13'))


class TestProgress(WebTestCase):
    def test_parses_phase_and_position(self):
        p = self.w.parse_progress([
            'Processing 10 image(s) from: /x',
            '(3/10) Running the face localization step for "IMG_0003.jpg"'])
        self.assertEqual(p['phase'], 'Finding faces')
        self.assertEqual((p['done'], p['total']), (3, 10))
        self.assertEqual(p['pct'], 30.0)

    def test_second_phase_wins(self):
        p = self.w.parse_progress([
            '(10/10) Running the face localization step for "a.jpg"',
            '(2/9) Running the age estimation step for "b"'])
        self.assertEqual(p['phase'], 'Estimating age')
        self.assertEqual((p['done'], p['total']), (2, 9))

    def test_heic_phase(self):
        p = self.w.parse_progress(['Converting 10 HEIC photo(s) to JPEG (quality 100)...'])
        self.assertEqual(p['phase'], 'Converting HEIC')

    def test_no_progress_lines(self):
        p = self.w.parse_progress(['something else'])
        self.assertEqual(p['total'], 0)
        self.assertEqual(p['pct'], 0.0)

    def test_ansi_is_stripped_from_the_log(self):
        """Terminal colour codes rendered as HTML show up as literal [36m."""
        self.assertEqual(
            self.w.ANSI_RE.sub('', '\x1b[36mConverting 10 HEIC\x1b[0m'),
            'Converting 10 HEIC')


class TestState(WebTestCase):
    def test_state_without_people(self):
        s = self.w.state()
        self.assertEqual(s['people'], [])
        self.assertIsNone(s['person'])

    def test_state_includes_preflight_placeholder(self):
        self.person()
        s = self.w.state('me', '2026-09-13')
        self.assertFalse(s['preflight']['available'])
        self.assertIn('pipeline writes', s['preflight']['why'])

    def test_fresh_visit_lands_on_today(self):
        self.person()
        st = self.w.state('me')
        self.assertEqual(st['date'], st['today'])

    def test_fresh_visit_skips_a_session_already_in_tracker(self):
        """A new visit always starts fresh: if today is already in the
        tracker, open the next take rather than the finished one."""
        self.person()
        import datetime as dt
        today = dt.date.today().isoformat()
        self.history('me', today)
        self.assertEqual(self.w.state('me')['date'], today + 'b')

    def test_checklist_items_match_preregistration(self):
        s = self.w.state()
        keys = [i['key'] for i in s['checklist_items']]
        self.assertEqual(keys, ['grooming', 'light', 'camera', 'pose',
                                'photoday', 'skin'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
