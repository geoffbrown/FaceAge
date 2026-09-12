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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.inbox = tempfile.mkdtemp()
        os.environ['FACEAGE_DATA'] = self.data
        os.environ['FACEAGE_INBOX'] = self.inbox
        for mod in ('faceage_web',):
            sys.modules.pop(mod, None)
        import faceage_web
        self.w = faceage_web
        self.w.DATA = self.data
        self.w.INBOX = self.inbox

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.inbox, ignore_errors=True)

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
    """§1 — the ordering is the point, so it is enforced server-side."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_1.jpg']})

    def test_score_refused_without_checklist(self):
        with self.assertRaises(ValueError) as cm:
            self.w.do_score({'person': 'me', 'date': '2026-09-13'})
        self.assertIn('checklist', str(cm.exception))

    def test_oneoff_is_exempt(self):
        """A one-off enters no series, so there is nothing to protect."""
        calls = []
        self.w.JOB.start = lambda label, argv, cwd=None: calls.append(argv)
        self.w.do_score({'person': 'me', 'date': '2026-09-13', 'oneoff': True})
        self.assertIn('--no-log', calls[0])

    def test_score_allowed_once_checklist_recorded(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        calls = []
        self.w.JOB.start = lambda label, argv, cwd=None: calls.append(argv)
        self.w.do_score({'person': 'me', 'date': '2026-09-13'})
        self.assertNotIn('--no-log', calls[0])
        self.assertIn('--subject', calls[0])
        self.assertIn('me', calls[0])

    def test_checklist_refused_after_scoring(self):
        """Recording validity after the number is known is not an exclusion."""
        self.history('me', '2026-09-13')
        with self.assertRaises(ValueError) as cm:
            self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                                 'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.assertIn('already been scored', str(cm.exception))

    def test_failed_item_records_a_protocol_failure(self):
        a = {k: True for k, _ in self.w.CHECKLIST}
        a['light'] = False
        r = self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                                 'answers': a})
        self.assertFalse(r['checklist']['valid'])
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]['session_date'], '2026-09-13')
        self.assertEqual(rows[0]['valid'], 'no')
        self.assertIn('Frontal light', rows[0]['reason'])

    def test_reason_has_no_comma_to_break_the_csv(self):
        a = {k: False for k, _ in self.w.CHECKLIST}
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13', 'answers': a})
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        with open(vfile) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['valid'], 'no')
        self.assertTrue(rows[0]['recorded_at'])

    def test_all_passed_writes_no_failure_row(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        vfile = os.path.join(self.w.results_dir('me'), 'session_validity.csv')
        self.assertFalse(os.path.exists(vfile))

    def test_score_refused_with_no_photos(self):
        with self.assertRaises(ValueError):
            self.w.do_score({'person': 'me', 'date': '2026-09-20'})


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

    def test_valid_checklist_is_in_series(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        self.summary()
        self.assertTrue(self.w.session_result('me', '2026-09-13')['in_series'])

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


class TestOneOff(WebTestCase):
    """The choice that decides whether a session enters the series has to sit
    next to the button that acts on it, and has to be readable from the code."""

    def setUp(self):
        WebTestCase.setUp(self)
        self.person()
        self.put_inbox('IMG_1.jpg')
        self.w.do_import({'person': 'me', 'date': '2026-09-13',
                          'files': ['IMG_1.jpg']})

    def script(self):
        import re
        return re.search(r'<script>(.*?)</script>', self.w.PAGE, re.S).group(1)

    def test_button_label_says_what_it_does(self):
        js = self.script()
        self.assertIn('Score and add to series', js)
        self.assertIn('Score only', js)

    def test_toggle_lives_in_the_score_card(self):
        """It used to sit in the Person card at the top, far from its effect."""
        js = self.script()
        score_at = js.index('---- score ----')
        person_at = js.index('---- person ----')
        oneoff_at = js.index("id=\"oneoff\"")
        self.assertGreater(oneoff_at, score_at)
        self.assertGreater(score_at, person_at)

    def test_oneoff_passes_no_log(self):
        calls = []
        self.w.JOB.start = lambda label, argv, cwd=None: calls.append(argv)
        self.w.do_score({'person': 'me', 'date': '2026-09-13', 'oneoff': True})
        self.assertIn('--no-log', calls[0])

    def test_tracked_run_does_not(self):
        self.w.do_checklist({'person': 'me', 'date': '2026-09-13',
                             'answers': {k: True for k, _ in self.w.CHECKLIST}})
        calls = []
        self.w.JOB.start = lambda label, argv, cwd=None: calls.append(argv)
        self.w.do_score({'person': 'me', 'date': '2026-09-13'})
        self.assertNotIn('--no-log', calls[0])


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

    def test_checklist_items_match_preregistration(self):
        s = self.w.state()
        keys = [i['key'] for i in s['checklist_items']]
        self.assertEqual(keys, ['grooming', 'light', 'camera', 'pose',
                                'photoday', 'skin'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
