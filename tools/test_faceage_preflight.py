#!/usr/bin/env python3
"""Tests for tools/faceage_preflight.py.

The engine's job is to produce a remedy, not a flag, so the tests assert on
severity and on the action text -- a finding that says the right thing in the
wrong register is still a failure.

    python3 tools/test_faceage_preflight.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faceage_preflight as pf


def frame(name='IMG_0001.jpg', luma=121.0, conf=0.999, fill=0.93,
          hard=None, adv=None, crop=(420, 520)):
    return {'file': name, 'luma': luma, 'luma_std': 40.0, 'confidence': conf,
            'fill': fill, 'crop_w': crop[0], 'crop_h': crop[1],
            'hard': hard or [], 'adv': adv or []}


def session(n=10, **kw):
    return [frame('IMG_%04d.jpg' % i, **kw) for i in range(n)]


def codes(findings):
    return [f.code for f in findings]


def by_code(findings, code):
    return next(f for f in findings if f.code == code)


class TestCleanSession(unittest.TestCase):
    def test_good_session_is_clean(self):
        f = pf.diagnose(session(10), baseline_luma=121.0)
        self.assertEqual(f, [])
        self.assertEqual(pf.verdict(f), 'GOOD')

    def test_first_session_records_a_baseline(self):
        f = pf.diagnose(session(10), baseline_luma=None)
        self.assertEqual(codes(f), ['LUMA_BASELINE'])
        # An INFO note must not downgrade the verdict: the first session is a
        # good session, it just has nothing to be compared against yet.
        self.assertEqual(pf.verdict(f), 'GOOD')
        self.assertEqual(by_code(f, 'LUMA_BASELINE').severity, pf.INFO)
        self.assertIn('reproduced', by_code(f, 'LUMA_BASELINE').do)


class TestExposure(unittest.TestCase):
    def test_drift_beyond_tolerance_is_a_reshoot(self):
        f = pf.diagnose(session(10, luma=130.0), baseline_luma=121.0)
        self.assertIn('LUMA_DRIFT', codes(f))
        self.assertEqual(by_code(f, 'LUMA_DRIFT').severity, pf.RESHOOT)
        self.assertEqual(pf.verdict(f), pf.RESHOOT)

    def test_remedy_names_the_direction(self):
        bright = by_code(pf.diagnose(session(10, luma=130.0), 121.0), 'LUMA_DRIFT')
        dark = by_code(pf.diagnose(session(10, luma=112.0), 121.0), 'LUMA_DRIFT')
        self.assertIn('brighter', bright.do)
        self.assertIn('darker', dark.do)

    def test_remedy_is_actionable_not_just_a_number(self):
        d = by_code(pf.diagnose(session(10, luma=130.0), 121.0), 'LUMA_DRIFT')
        # the action must mention the rig, not the file
        self.assertTrue(any(w in d.do for w in ('lamp', 'daylight', 'light')))
        self.assertIn('reshoot', d.do.lower())

    def test_small_shift_warns_without_demanding_a_reshoot(self):
        f = pf.diagnose(session(10, luma=124.0), baseline_luma=121.0)
        self.assertIn('LUMA_SHIFT', codes(f))
        self.assertEqual(by_code(f, 'LUMA_SHIFT').severity, pf.CHECK)
        self.assertNotIn('LUMA_DRIFT', codes(f))

    def test_inside_tolerance_is_silent(self):
        self.assertEqual(pf.diagnose(session(10, luma=122.0), 121.0), [])

    def test_unstable_light_within_a_session(self):
        """Frames averaging to the baseline while swinging wildly is the case a
        session-mean check alone would miss."""
        imgs = ([frame('a%d.jpg' % i, luma=115.0) for i in range(5)]
                + [frame('b%d.jpg' % i, luma=127.0) for i in range(5)])
        f = pf.diagnose(imgs, baseline_luma=121.0)
        self.assertIn('LUMA_UNSTABLE', codes(f))
        self.assertNotIn('LUMA_DRIFT', codes(f))   # mean is exactly on baseline
        self.assertIn('AE/AF lock', by_code(f, 'LUMA_UNSTABLE').do)


class TestFrameCount(unittest.TestCase):
    def test_slightly_short_is_a_check(self):
        f = pf.diagnose(session(7), 121.0)
        self.assertEqual(by_code(f, 'FRAMES_SHORT').severity, pf.CHECK)
        self.assertIn('3 more', by_code(f, 'FRAMES_SHORT').do)

    def test_badly_short_is_a_reshoot(self):
        f = pf.diagnose(session(4), 121.0)
        self.assertEqual(by_code(f, 'FRAMES_SHORT').severity, pf.RESHOOT)

    def test_hard_failures_do_not_count_as_usable(self):
        imgs = session(6) + [frame('x%d.jpg' % i, hard=['NO_FACE_DETECTED'])
                             for i in range(4)]
        f = pf.diagnose(imgs, 121.0)
        self.assertIn('NO_FACE_DETECTED', codes(f))
        self.assertIn('6 usable', by_code(f, 'FRAMES_SHORT').what)


class TestHardFailures(unittest.TestCase):
    def test_no_face_lists_the_files(self):
        imgs = session(9) + [frame('bad.jpg', hard=['NO_FACE_DETECTED'])]
        f = pf.diagnose(imgs, 121.0)
        self.assertEqual(by_code(f, 'NO_FACE_DETECTED').frames, ['bad.jpg'])

    def test_multiple_faces_warns_it_may_not_be_you(self):
        imgs = session(9) + [frame('two.jpg', hard=['MULTIPLE_FACES'])]
        d = by_code(pf.diagnose(imgs, 121.0), 'MULTIPLE_FACES')
        self.assertEqual(d.severity, pf.RESHOOT)
        self.assertIn('someone else', d.why)

    def test_empty_session(self):
        imgs = [frame('x%d.jpg' % i, hard=['NO_FACE_DETECTED']) for i in range(4)]
        f = pf.diagnose(imgs, 121.0)
        self.assertIn('SESSION_EMPTY', codes(f))
        self.assertEqual(pf.verdict(f), pf.RESHOOT)


class TestFraming(unittest.TestCase):
    def test_low_fill_on_every_frame_is_a_reshoot(self):
        f = pf.diagnose(session(10, fill=0.62), 121.0)
        self.assertEqual(by_code(f, 'FACE_FILL_LOW').severity, pf.RESHOOT)
        self.assertIn('tripod', by_code(f, 'FACE_FILL_LOW').do)

    def test_low_fill_on_some_frames_is_a_check(self):
        imgs = session(8) + [frame('f%d.jpg' % i, fill=0.62) for i in range(2)]
        self.assertEqual(by_code(pf.diagnose(imgs, 121.0),
                                 'FACE_FILL_LOW').severity, pf.CHECK)

    def test_low_confidence_points_at_forehead_and_temples(self):
        imgs = session(8) + [frame('c%d.jpg' % i, conf=0.80) for i in range(2)]
        d = by_code(pf.diagnose(imgs, 121.0), 'LOW_CONFIDENCE')
        self.assertEqual(d.severity, pf.CHECK)
        self.assertIn('temples', d.do)

    def test_clipped_face(self):
        imgs = session(9) + [frame('e.jpg', adv=['FACE_CLIPPED_AT_BORDER'])]
        self.assertIn('FACE_CLIPPED', codes(pf.diagnose(imgs, 121.0)))


class TestOrderingAndVerdict(unittest.TestCase):
    def test_worst_first(self):
        imgs = (session(6, luma=132.0)
                + [frame('c%d.jpg' % i, luma=132.0, conf=0.8) for i in range(2)])
        f = pf.diagnose(imgs, 121.0)
        self.assertEqual(f[0].severity, pf.RESHOOT)
        sev = [pf._RANK[x.severity] for x in f]
        self.assertEqual(sev, sorted(sev))

    def test_verdict_precedence(self):
        self.assertEqual(pf.verdict([]), 'GOOD')
        self.assertEqual(pf.verdict([pf.Finding(pf.CHECK, 'c', 'w', 'y', 'd')]),
                         pf.CHECK)
        self.assertEqual(pf.verdict([pf.Finding(pf.CHECK, 'c', 'w', 'y', 'd'),
                                     pf.Finding(pf.RESHOOT, 'r', 'w', 'y', 'd')]),
                         pf.RESHOOT)

    def test_every_finding_carries_a_remedy(self):
        """The whole premise: no finding may be a bare flag."""
        imgs = ([frame('a.jpg', luma=140.0, conf=0.5, fill=0.4,
                       adv=['FACE_CLIPPED_AT_BORDER', 'CROP_UPSCALED'])]
                + [frame('b.jpg', hard=['NO_FACE_DETECTED'])]
                + [frame('c.jpg', hard=['MULTIPLE_FACES'])])
        for x in pf.diagnose(imgs, 121.0):
            self.assertTrue(x.do and len(x.do) > 20, '%s has no remedy' % x.code)
            self.assertTrue(x.why and len(x.why) > 20, '%s has no rationale' % x.code)
            self.assertTrue(x.what, '%s has no measurement' % x.code)

    def test_render_does_not_crash_and_states_no_modification(self):
        imgs = session(10, luma=131.0)
        text = pf.render(pf.diagnose(imgs, 121.0), imgs)
        self.assertIn('VERDICT: RESHOOT', text)
        self.assertIn('No photograph was modified', text)


class TestNoPixelMutation(unittest.TestCase):
    def test_module_exposes_no_adjustment_function(self):
        """Guard against someone later adding a 'fix the exposure' helper. The
        remedy is always the rig, never the file."""
        banned = ('adjust', 'normalise', 'normalize', 'correct_exposure',
                  'rebalance', 'retouch')
        for name in dir(pf):
            low = name.lower()
            self.assertFalse(any(b in low for b in banned),
                             'suspicious symbol %s' % name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
