#!/usr/bin/env python3
"""Tests for tools/faceage_analysis.py.

These are known-answer tests, not smoke tests. The gate decides whether the app
gets built and the trend verdict is the study's result, so the arithmetic is
checked against values derived independently rather than against whatever the
code happened to print first.

    python3 tools/test_faceage_analysis.py
"""
import os
import sys
import csv
import math
import tempfile
import unittest
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faceage_analysis as fa


def write_history(path, rows):
    cols = ['session_date', 'run_timestamp', 'n', 'n_total_images', 'n_failed',
            'n_flagged', 'mean', 'median', 'std', 'min', 'max', 'mean_luma',
            'model_sha256', 'image_dir', 'notes']
    with open(path, 'w') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            row = {c: '' for c in cols}
            row.update(r)
            w.writerow(row)


def mkrows(dates, means, lumas=None):
    out = []
    for i, (d, m) in enumerate(zip(dates, means)):
        r = {'date': datetime.date.fromisoformat(d), 'label': d, 'mean': m,
             'n': 10, 'std': 1.0, 'luma': None if lumas is None else lumas[i],
             'notes': ''}
        out.append(r)
    return out


class TestSigmaAndGate(unittest.TestCase):
    def test_sigma_is_sample_sd(self):
        rows = mkrows(['2026-09-11', '2026-09-13', '2026-09-14'], [40.0, 42.0, 44.0])
        s = fa.sigma(rows)
        self.assertEqual(s['n'], 3)
        self.assertAlmostEqual(s['mean'], 42.0)
        self.assertAlmostEqual(s['sigma'], 2.0)          # ddof=1
        self.assertAlmostEqual(s['range'], 4.0)

    def test_sigma_needs_two(self):
        s = fa.sigma(mkrows(['2026-09-11'], [40.0]))
        self.assertIsNone(s['sigma'])
        self.assertEqual(fa.gate(s['sigma'])[0], 'INCOMPLETE')

    def test_gate_boundaries(self):
        # The table in §4 is inclusive at the boundaries.
        self.assertEqual(fa.gate(0.0)[0], 'PROCEED')
        self.assertEqual(fa.gate(0.5)[0], 'PROCEED')
        self.assertEqual(fa.gate(0.50001)[0], 'IMPROVE RIG')
        self.assertEqual(fa.gate(1.0)[0], 'IMPROVE RIG')
        self.assertEqual(fa.gate(1.00001)[0], 'DO NOT BUILD')
        self.assertEqual(fa.gate(3.0)[0], 'DO NOT BUILD')

    def test_five_session_rehearsal(self):
        """The actual §4 shape: 5 sessions in 7 days, real change ~0."""
        rows = mkrows(['2026-09-11', '2026-09-13', '2026-09-14',
                       '2026-09-16', '2026-09-17'],
                      [44.1, 44.5, 43.9, 44.3, 44.2])
        s = fa.sigma(rows)
        self.assertEqual(s['n'], 5)
        # independently: mean 44.2, deviations -.1,.3,-.3,.1,0 -> var .05 -> sd .2236
        self.assertAlmostEqual(s['sigma'], 0.2236, places=3)
        self.assertEqual(fa.gate(s['sigma'])[0], 'PROCEED')


class TestSEFormula(unittest.TestCase):
    def test_weekly_matches_preregistration(self):
        """§3 sampling table: weekly n=26, h=7d -> SE ~= 0.68 sigma. This one
        reproduces the document exactly and pins the convention."""
        se = fa.se_total_change(1.0, 26, 7.0)
        self.assertAlmostEqual(se, 0.68, places=2)

    def test_monthly_does_not_match_the_document(self):
        """Deliberate: §3 states 1.31 sigma for monthly n=6, but the same
        convention that yields the documented weekly figure gives 1.43. No
        natural reading lands on 1.31. Pinned so the discrepancy stays visible
        rather than being quietly absorbed."""
        se = fa.se_total_change(1.0, 6, fa.DAYS_PER_MONTH)
        self.assertAlmostEqual(se, 1.43, places=2)
        self.assertNotAlmostEqual(se, 1.31, places=2)

    def test_scales_linearly_with_sigma(self):
        a = fa.se_total_change(1.0, 26, 7.0)
        b = fa.se_total_change(2.5, 26, 7.0)
        self.assertAlmostEqual(b, 2.5 * a, places=9)

    def test_more_sessions_is_tighter(self):
        self.assertLess(fa.se_total_change(1.0, 26, 7.0),
                        fa.se_total_change(1.0, 6, fa.DAYS_PER_MONTH))

    def test_degenerate(self):
        self.assertIsNone(fa.se_total_change(1.0, 2, 7.0))
        self.assertIsNone(fa.se_total_change(None, 26, 7.0))
        self.assertIsNone(fa.se_total_change(1.0, 26, 0))


class TestOLS(unittest.TestCase):
    def test_exact_line(self):
        """y = 3 + 2x exactly -> slope 2, zero residual, zero SE."""
        fit = fa.ols([0, 1, 2, 3, 4], [3, 5, 7, 9, 11])
        self.assertAlmostEqual(fit['slope'], 2.0)
        self.assertAlmostEqual(fit['intercept'], 3.0)
        self.assertAlmostEqual(fit['resid_se'], 0.0)
        self.assertAlmostEqual(fit['se_slope'], 0.0)
        self.assertEqual(fit['df'], 3)

    def test_known_regression(self):
        """Independently computed: xs 0..4 (mean 2), ys [1,3,2,5,4] (mean 3).
        Sxx=10, Sxy=8 -> slope 0.8, intercept 1.4.
        Fitted 1.4,2.2,3.0,3.8,4.6; residuals -.4,.8,-1,1.2,-.6; SSE=3.6;
        s=sqrt(3.6/3)=sqrt(1.2)."""
        fit = fa.ols([0, 1, 2, 3, 4], [1, 3, 2, 5, 4])
        self.assertAlmostEqual(fit['slope'], 0.8)
        self.assertAlmostEqual(fit['intercept'], 1.4)
        self.assertAlmostEqual(fit['sxx'], 10.0)
        self.assertAlmostEqual(fit['resid_se'], math.sqrt(1.2))
        self.assertAlmostEqual(fit['se_slope'], math.sqrt(1.2) / math.sqrt(10.0))

    def test_too_few_points(self):
        self.assertIsNone(fa.ols([0, 1], [1, 2]))

    def test_no_x_variance(self):
        self.assertIsNone(fa.ols([2, 2, 2], [1, 2, 3]))


class TestTrend(unittest.TestCase):
    def test_perfect_decline_is_detected(self):
        """Exactly -1 yr per 30 days, no noise -> CI collapses, detected."""
        dates = ['2026-01-01', '2026-01-31', '2026-03-02', '2026-04-01']
        rows = mkrows(dates, [50.0, 49.0, 48.0, 47.0])
        t = fa.trend(rows)
        self.assertTrue(t['ok'])
        self.assertAlmostEqual(t['slope_per_month'], -1.0 * fa.DAYS_PER_MONTH / 30.0,
                               places=6)
        self.assertTrue(t['detected'])
        self.assertEqual(t['verdict'], 'detected')
        self.assertAlmostEqual(t['total_change'], -3.0, places=6)

    def test_flat_noisy_is_not_detected(self):
        """Symmetric scatter about a flat line: slope 0, CI spans zero."""
        dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01',
                 '2026-05-01']
        rows = mkrows(dates, [44.0, 45.0, 44.0, 45.0, 44.0])
        t = fa.trend(rows)
        self.assertTrue(t['ok'])
        self.assertFalse(t['detected'])
        self.assertEqual(t['verdict'], 'not detected')
        lo, hi = t['slope_ci']
        self.assertLessEqual(lo, 0.0)
        self.assertGreaterEqual(hi, 0.0)

    def test_verdict_follows_ci_not_sign(self):
        """A visible downward slope that the CI cannot separate from zero is
        still 'not detected'. This is the rule §3 exists to enforce."""
        dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01']
        rows = mkrows(dates, [50.0, 44.0, 52.0, 46.0])
        t = fa.trend(rows)
        self.assertLess(t['slope_per_month'], 0.0)      # slopes downward
        self.assertFalse(t['detected'])                 # but not separable
        self.assertEqual(t['verdict'], 'not detected')

    def test_needs_three_sessions(self):
        rows = mkrows(['2026-01-01', '2026-02-01'], [50.0, 49.0])
        self.assertFalse(fa.trend(rows)['ok'])

    def test_deviation_offset(self):
        self.assertAlmostEqual(fa.deviation_note(0.0), -1.0 / 12.0)
        self.assertAlmostEqual(fa.deviation_note(1.0 / 12.0), 0.0)

    def test_ci_brackets_point_estimate(self):
        dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01']
        rows = mkrows(dates, [50.0, 49.2, 48.9, 47.8])
        t = fa.trend(rows)
        lo, hi = t['slope_ci']
        self.assertLess(lo, t['slope_per_month'])
        self.assertGreater(hi, t['slope_per_month'])


class TestLuma(unittest.TestCase):
    def test_shared_shape_raises_concern(self):
        """FaceAge tracking exposure exactly is the §6 failure mode."""
        dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01']
        rows = mkrows(dates, [40.0, 42.0, 44.0, 46.0], [100.0, 104.0, 108.0, 112.0])
        l = fa.luma_check(rows)
        self.assertTrue(l['ok'])
        self.assertAlmostEqual(l['r'], 1.0, places=6)
        self.assertTrue(l['concern'])

    def test_stable_rig_does_not_cry_wolf(self):
        """Near-constant exposure correlates strongly with the FaceAge swing
        (r ~ 0.80) purely by chance -- 0.4 units of luma wander cannot explain
        a 6-year swing. Correlation alone must not raise the alarm, or the
        check gets ignored when it matters."""
        dates = ['2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01']
        rows = mkrows(dates, [40.0, 46.0, 41.0, 45.0], [100.0, 100.2, 99.8, 100.1])
        l = fa.luma_check(rows)
        self.assertGreater(abs(l['r']), 0.7)        # correlated ...
        self.assertLess(l['luma_range'], 1.0)       # ... but nothing moved
        self.assertFalse(l['concern'])
        self.assertLess(l['max_drift_from_session_one'], 5.0)

    def test_drift_over_tolerance_flagged(self):
        dates = ['2026-01-01', '2026-02-01', '2026-03-01']
        rows = mkrows(dates, [40.0, 41.0, 42.0], [100.0, 103.0, 110.0])
        l = fa.luma_check(rows)
        self.assertEqual(l['over_tolerance'], [2])       # 110 is 10 off baseline

    def test_needs_luma(self):
        rows = mkrows(['2026-01-01', '2026-02-01', '2026-03-01'], [40, 41, 42])
        self.assertFalse(fa.luma_check(rows)['ok'])


class TestValidityGate(unittest.TestCase):
    """§1: protocol failures are dropped, and only for a recorded reason."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.hist = os.path.join(self.dir, 'faceage_history.csv')
        write_history(self.hist, [
            {'session_date': '2026-09-11', 'n': 10, 'mean': 44.1, 'mean_luma': 120},
            {'session_date': '2026-09-13', 'n': 10, 'mean': 44.5, 'mean_luma': 121},
            {'session_date': '2026-09-14', 'n': 10, 'mean': 51.0, 'mean_luma': 140},
        ])

    def test_all_valid_without_file(self):
        rows, dropped = fa.load_history(self.hist, fa.load_validity(None))
        self.assertEqual(len(rows), 3)
        self.assertEqual(dropped, [])

    def test_invalid_session_dropped_with_reason(self):
        vpath = os.path.join(self.dir, 'session_validity.csv')
        with open(vpath, 'w') as fh:
            fh.write('session_date,valid,reason,recorded_at\n')
            fh.write('2026-09-14,no,overhead light; rig moved,2026-09-14T08:00\n')
        rows, dropped = fa.load_history(self.hist, fa.load_validity(vpath))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]['label'], '2026-09-14')
        self.assertIn('overhead light', dropped[0]['reason'])

    def test_sigma_changes_when_failure_excluded(self):
        """The dropped session is the one that would have inflated sigma."""
        rows_all, _ = fa.load_history(self.hist, {})
        vpath = os.path.join(self.dir, 'session_validity.csv')
        with open(vpath, 'w') as fh:
            fh.write('session_date,valid,reason\n2026-09-14,no,rig moved\n')
        rows_ok, _ = fa.load_history(self.hist, fa.load_validity(vpath))
        self.assertGreater(fa.sigma(rows_all)['sigma'], 3.0)
        self.assertLess(fa.sigma(rows_ok)['sigma'], 0.5)

    def test_rows_sorted_by_date(self):
        write_history(self.hist, [
            {'session_date': '2026-09-14', 'n': 10, 'mean': 44.0},
            {'session_date': '2026-09-11', 'n': 10, 'mean': 45.0},
        ])
        rows, _ = fa.load_history(self.hist, {})
        self.assertEqual([r['label'] for r in rows], ['2026-09-11', '2026-09-14'])


class TestCLI(unittest.TestCase):
    def test_runs_end_to_end(self):
        d = tempfile.mkdtemp()
        hist = os.path.join(d, 'faceage_history.csv')
        write_history(hist, [
            {'session_date': '2026-09-11', 'n': 10, 'mean': 44.1, 'mean_luma': 120},
            {'session_date': '2026-09-13', 'n': 10, 'mean': 44.5, 'mean_luma': 121},
            {'session_date': '2026-09-14', 'n': 10, 'mean': 43.9, 'mean_luma': 119},
            {'session_date': '2026-09-16', 'n': 10, 'mean': 44.3, 'mean_luma': 120},
            {'session_date': '2026-09-17', 'n': 10, 'mean': 44.2, 'mean_luma': 121},
        ])
        rc = fa.main(['--results', d, '--all'])
        self.assertEqual(rc, 0)

    def test_json_mode(self):
        import io
        import json
        import contextlib
        d = tempfile.mkdtemp()
        hist = os.path.join(d, 'faceage_history.csv')
        write_history(hist, [
            {'session_date': '2026-09-11', 'n': 10, 'mean': 44.1},
            {'session_date': '2026-09-13', 'n': 10, 'mean': 44.5},
            {'session_date': '2026-09-14', 'n': 10, 'mean': 43.9},
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fa.main(['--results', d, '--json'])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc['n_valid'], 3)
        self.assertIn('decision', doc['gate'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
