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
