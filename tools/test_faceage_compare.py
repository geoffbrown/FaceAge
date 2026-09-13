#!/usr/bin/env python3
"""Tests for `faceage compare`: two sittings side by side."""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faceage_compare as fc  # noqa: E402

COLS = ['subj_id', 'file', 'faceage', 'status', 'hard_flags', 'advisory_flags',
        'confidence', 'n_faces', 'source_w', 'source_h', 'crop_w', 'crop_h',
        'crop_luma_mean', 'crop_luma_std', 'face_fill_height_frac',
        'face_fill_area_frac', 'error']


def write_session(results, sessions, name, ages, fill, luma, conf=0.99, flags=(), camera='Studio Display Camera', dy=0.0):
    os.makedirs(results, exist_ok=True)
    with open(os.path.join(results, '%s_per_image.csv' % name), 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for i, a in enumerate(ages):
            w.writerow({'subj_id': 'f%02d' % i, 'file': 'cam_%02d.jpg' % i, 'faceage': a,
                        'status': 'ok', 'hard_flags': '', 'advisory_flags': ';'.join(flags) if i == 0 else '',
                        'confidence': conf, 'crop_luma_mean': luma, 'face_fill_height_frac': fill})
    d = os.path.join(sessions, name)
    os.makedirs(d, exist_ok=True)
    json.dump({'settings': {'camera': camera},
               'frames': [{'fill': fill, 'dx': 0.0, 'dy': dy} for _ in ages]},
              open(os.path.join(d, 'capture.json'), 'w'))


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.results = os.path.join(self.d, 'results')
        self.sessions = os.path.join(self.d, 'sessions')

    def run_compare(self, *names):
        return fc.render([fc.load_session(self.results, self.sessions, n) for n in names])

    def test_smaller_face_is_named(self):
        write_session(self.results, self.sessions, 'a', [40.4] * 10, 0.90, 177)
        write_session(self.results, self.sessions, 'b', [39.2] * 10, 0.84, 177)
        out = self.run_compare('a', 'b')
        self.assertIn('FaceAge moved -1.2 years', out)
        self.assertIn('6 points smaller', out)
        self.assertIn('Brightness was the same', out)
        self.assertIn('Studio Display Camera', out)

    def test_same_framing_points_at_the_face(self):
        write_session(self.results, self.sessions, 'a', [40.4] * 10, 0.90, 177)
        write_session(self.results, self.sessions, 'b', [39.2] * 10, 0.90, 178)
        out = self.run_compare('a', 'b')
        self.assertIn('Face size was the same', out)
        self.assertIn('difference is in the face itself', out)

    def test_three_sittings_one_direction_is_drift(self):
        write_session(self.results, self.sessions, 'a', [40.4] * 10, 0.90, 177)
        write_session(self.results, self.sessions, 'b', [39.2] * 10, 0.90, 177)
        write_session(self.results, self.sessions, 'c', [38.7] * 10, 0.90, 176)
        out = self.run_compare('a', 'b', 'c')
        self.assertIn('drift, not noise', out)
        self.assertIn('40.4, 39.2, 38.7', out)

    def test_frame_table_and_flags(self):
        write_session(self.results, self.sessions, 'a', [40.0, 41.0], 0.90, 177, flags=('LOW_CONFIDENCE',))
        write_session(self.results, self.sessions, 'b', [39.0], 0.90, 177)
        out = self.run_compare('a', 'b')
        self.assertIn('* flagged by the pipeline QA', out)
        self.assertIn('cam_01.jpg', out)
        self.assertRegex(out, r'\n  2 ')

    def test_missing_session_is_a_clear_error(self):
        write_session(self.results, self.sessions, 'a', [40.0], 0.9, 177)
        with self.assertRaises(SystemExit) as cm:
            fc.load_session(self.results, self.sessions, 'zzz')
        self.assertIn('no QA data for session zzz', str(cm.exception))

    def test_main_requires_two(self):
        write_session(self.results, self.sessions, 'a', [40.0], 0.9, 177)
        with self.assertRaises(SystemExit):
            fc.main(['a', '--results', self.results])

    def test_no_em_dashes_in_copy(self):
        src = open(fc.__file__).read()
        self.assertNotIn('—', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
