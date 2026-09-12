#!/usr/bin/env python3
"""Pre-registered analysis for the local FaceAge series.

Implements docs/PREREGISTRATION.md exactly. It does not offer alternatives:
the point of a pre-registration is that the analysis was fixed before the data
existed, so this file has no options for trying a different model form.

  sigma   §4  repeatability study -> sigma -> the build/no-build gate
  trend   §3  OLS slope of session mean against date, 95% CI, yr/month
  luma    §6  falsification check against the exposure trace

Stdlib only, no network. Reads summary statistics; never touches photographs.

TWO RULES THIS FILE ENFORCES

1. Session validity is read from session_validity.csv and applied BEFORE any
   number is computed (§1). A session recorded as a protocol failure is dropped
   with its reason shown. Sessions are never dropped on the basis of their
   FaceAge value, and this file has no mechanism to do so.

2. "If the CI includes zero, the result is not detected" (§3). Not trending,
   not early signs. The verdict string is generated from the CI, not chosen.
"""
import os
import csv
import sys
import json
import math
import argparse
import datetime

DAYS_PER_MONTH = 365.2425 / 12.0        # 30.436875

# The repeatability set is pre-registered (§4): five sessions in seven days, on
# these dates. Hardcoded so `faceage gate` cannot quietly fold in some other
# session that happens to sit in the window -- an ad-hoc test run, a reshoot --
# and report a sigma that was not the one the study committed to.
REHEARSAL_SESSIONS = ('2026-09-11', '2026-09-13', '2026-09-14',
                      '2026-09-16', '2026-09-17')

# Two-sided 95% Student-t critical values. scipy is not a dependency and the
# study will never have more than a few dozen sessions, so a table is honest
# and exact where it matters. Beyond df=30 the normal quantile is within 0.05.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
       14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
       20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
       26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def t_crit(df):
    if df < 1:
        return float('nan')
    return T95.get(df, 1.959964)


# ----------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------

def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def load_validity(path):
    """session_date -> (is_valid, reason). Absent file means everything valid.

    Written by `faceage invalidate` BEFORE a session is scored. See §1: a
    session invalidated after its number is known is not an exclusion, it is a
    result being discarded for being inconvenient.
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path) as fh:
        for r in csv.DictReader(fh):
            d = (r.get('session_date') or '').strip()
            if not d:
                continue
            v = (r.get('valid') or '').strip().lower()
            out[d] = (v not in ('0', 'no', 'false', 'invalid'),
                      (r.get('reason') or '').strip())
    return out


def load_anchors(path):
    """B (baseline) and R (retest) dates, if set. anchors.csv: anchor,date,note.

    These are study landmarks, not data: B starts the six-month clock and R is
    the retest. Sessions before B are rehearsal and do not enter the series
    (§4), so the trend is fitted from B onward once B exists.
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path) as fh:
        for r in csv.DictReader(fh):
            k = (r.get('anchor') or '').strip().upper()
            if k not in ('B', 'R'):
                continue
            try:
                out[k] = datetime.date.fromisoformat((r.get('date') or '').strip()[:10])
            except ValueError:
                continue
    return out


def series_for_trend(rows, anchors):
    """§4: rehearsal sessions are not submitted and do not enter the series.

    With B set, the series starts at B. Without B every session is in, which is
    right early on -- there is nothing else to fit -- but it means the trend
    shown before B includes rehearsal sessions, and the chart says so.
    """
    b = (anchors or {}).get('B')
    if not b:
        return list(rows), []
    keep = [r for r in rows if r['date'] >= b]
    pre = [r for r in rows if r['date'] < b]
    return keep, pre


def load_history(path, validity=None):
    """Read faceage_history.csv into sorted session records."""
    if not os.path.exists(path):
        raise SystemExit('no history at %s -- run a session first' % path)
    validity = validity or {}
    rows, dropped = [], []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            d = (r.get('session_date') or '').strip()
            try:
                date = datetime.date.fromisoformat(d[:10])
            except ValueError:
                continue
            mean = _num(r.get('mean'))
            if mean is None:
                continue
            rec = {'date': date, 'label': d, 'mean': mean,
                   'n': int(_num(r.get('n')) or 0),
                   'std': _num(r.get('std')),
                   'luma': _num(r.get('mean_luma')),
                   'notes': (r.get('notes') or '').strip()}
            ok, reason = validity.get(d, (True, ''))
            if ok:
                rows.append(rec)
            else:
                rec['reason'] = reason
                dropped.append(rec)
    rows.sort(key=lambda r: r['date'])
    dropped.sort(key=lambda r: r['date'])
    return rows, dropped


# ----------------------------------------------------------------------------
# §4 repeatability
# ----------------------------------------------------------------------------

def sigma(rows):
    """Between-session measurement noise: the sample SD (ddof=1) of the session
    means. Valid only where real change is ~0, i.e. the rehearsal week."""
    vals = [r['mean'] for r in rows]
    n = len(vals)
    if n < 2:
        return {'n': n, 'sigma': None, 'mean': vals[0] if vals else None}
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return {'n': n, 'sigma': math.sqrt(var), 'mean': m,
            'min': min(vals), 'max': max(vals), 'range': max(vals) - min(vals)}


def gate(sigma_value):
    """The §4 decision table. Returns (decision, detail)."""
    if sigma_value is None:
        return 'INCOMPLETE', 'need at least 2 sessions to estimate sigma'
    if sigma_value <= 0.5:
        return 'PROCEED', 'sigma <= 0.5 yr: the series can resolve the effect'
    if sigma_value <= 1.0:
        return 'IMPROVE RIG', ('0.5 < sigma <= 1.0 yr: improve the rig and '
                               're-run the repeatability study')
    return 'DO NOT BUILD', ('sigma > 1.0 yr: the instrument cannot resolve the '
                            'effect; do not build tooling on it')


def se_total_change(sigma_value, n, spacing_days, window_days=182.0):
    """SE of the total modelled change over `window_days`, for n sessions spaced
    `spacing_days` apart. This is the §3 'Sampling rationale' table, computed
    rather than looked up.

    For n points spaced h apart, Sxx = h^2 * n(n^2-1)/12, and the total change
    is slope * window, so SE(total) = window * sigma / sqrt(Sxx).

    Note the convention: cadence fixes the spacing, while the total change is
    reported over the nominal 26-week window even though n points spaced h
    apart span only (n-1)*h. That reproduces the pre-registration's weekly
    figure exactly (n=26, h=7 -> 0.680 sigma).

    It does not reproduce the table's monthly figure: n=6 at h=30.44 gives
    1.43 sigma where §3 states 1.31. Every natural reading lands between 1.19
    and 1.43, and none on 1.31, so the table entry looks like a slip. It does
    not affect any result -- the table is sampling rationale, and §3's actual
    analysis is the fitted CI computed in trend() from real data -- but it is
    worth correcting in the document.
    """
    if sigma_value is None or n < 3 or spacing_days <= 0 or window_days <= 0:
        return None
    sxx = (spacing_days ** 2) * n * (n ** 2 - 1) / 12.0
    return window_days * sigma_value / math.sqrt(sxx)


# ----------------------------------------------------------------------------
# §3 trend
# ----------------------------------------------------------------------------

def ols(xs, ys):
    """Plain OLS. Returns slope, intercept, residual SE, SE(slope), df, Sxx."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    sse = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    df = n - 2
    s = math.sqrt(sse / df) if df > 0 else float('nan')
    return {'slope': b, 'intercept': a, 'resid_se': s,
            'se_slope': (s / math.sqrt(sxx)) if df > 0 else float('nan'),
            'df': df, 'sxx': sxx, 'n': n}


def trend(rows):
    """§3 primary estimate: OLS slope of session mean against date.

    Reported in FaceAge-years per month with a 95% CI. Secondary: total
    modelled change across the observed window, with CI.
    """
    if len(rows) < 3:
        return {'n': len(rows), 'ok': False,
                'why': 'need at least 3 sessions for a slope with a CI'}
    t0 = rows[0]['date']
    xs = [(r['date'] - t0).days for r in rows]
    ys = [r['mean'] for r in rows]
    fit = ols(xs, ys)
    if fit is None:
        return {'n': len(rows), 'ok': False, 'why': 'sessions share one date'}

    t = t_crit(fit['df'])
    per_day, se_day = fit['slope'], fit['se_slope']

    per_month = per_day * DAYS_PER_MONTH
    se_month = se_day * DAYS_PER_MONTH
    lo_m, hi_m = per_month - t * se_month, per_month + t * se_month

    window = xs[-1] - xs[0]
    total = per_day * window
    se_total = se_day * window
    lo_t, hi_t = total - t * se_total, total + t * se_total

    detected = not (lo_m <= 0.0 <= hi_m)
    return {
        'ok': True, 'n': fit['n'], 'df': fit['df'], 't_crit': t,
        'window_days': window,
        'first': rows[0]['label'], 'last': rows[-1]['label'],
        'slope_per_month': per_month, 'slope_ci': (lo_m, hi_m),
        'se_per_month': se_month,
        'total_change': total, 'total_ci': (lo_t, hi_t),
        'resid_se': fit['resid_se'],
        'detected': detected,
        # §3: the verdict is generated from the CI, not chosen.
        'verdict': ('detected' if detected else 'not detected'),
    }


def deviation_note(slope_per_month):
    """§3: chronological drift is linear and known, so the deviation slope is
    the raw slope minus 1 chronological year per 12 months."""
    if slope_per_month is None:
        return None
    return slope_per_month - 1.0 / 12.0


# ----------------------------------------------------------------------------
# §6 falsification
# ----------------------------------------------------------------------------

def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def luma_check(rows):
    """§6: if the FaceAge trend and the exposure trend share a shape, the study
    has measured photography, not biology. Run before interpreting, not after
    being challenged."""
    pairs = [(r['luma'], r['mean']) for r in rows if r['luma'] is not None]
    if len(pairs) < 3:
        return {'ok': False, 'why': 'need mean_luma on at least 3 sessions'}
    lum = [p[0] for p in pairs]
    fa = [p[1] for p in pairs]
    r = pearson(lum, fa)
    t0 = rows[0]['date']
    xs = [(rr['date'] - t0).days for rr in rows if rr['luma'] is not None]
    lfit = ols(xs, lum)
    base = lum[0]
    drift = [abs(v - base) for v in lum]
    lrange = max(lum) - min(lum)
    # Correlation ALONE is not the trigger. On a stable rig the exposure trace
    # is near-constant, and a near-constant series still correlates strongly
    # with anything by chance -- 0.2 units of luma wander can hit r=0.8 while
    # explaining none of a 6-year FaceAge swing. Requiring real movement as
    # well keeps the falsification check from crying wolf, which matters: a
    # check that fires on a good rig is one that gets ignored on a bad one.
    LUMA_FLOOR = 1.0
    return {
        'ok': True, 'n': len(pairs), 'r': r,
        'luma_slope_per_month': (lfit['slope'] * DAYS_PER_MONTH) if lfit else None,
        'luma_first': base, 'luma_last': lum[-1], 'luma_range': lrange,
        'max_drift_from_session_one': max(drift),
        'over_tolerance': [i for i, d in enumerate(drift) if d > 5.0],
        # A shared shape is the warning. |r| is not a p-value and not a
        # pass/fail; it is a prompt to look at the two traces together.
        'concern': (r is not None and abs(r) >= 0.7 and lrange >= LUMA_FLOOR),
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _fmt(v, nd=3):
    return '--' if v is None else ('%.*f' % (nd, v))


def report(rows, dropped, args, rehearsal=None, anchors=None, pre_b=None):
    out = []
    A = out.append
    A('=' * 74)
    A('FACEAGE SERIES -- pre-registered analysis (docs/PREREGISTRATION.md)')
    A('=' * 74)
    A('sessions valid   : %d' % len(rows))
    if dropped:
        A('sessions dropped : %d (protocol failures, recorded before scoring)'
          % len(dropped))
        for d in dropped:
            A('    %s  %s' % (d['label'], d.get('reason') or '(no reason given)'))
    A('')

    if args.sigma or args.all:
        rset = rows if rehearsal is None else rehearsal
        s = sigma(rset)
        dec, why = gate(s['sigma'])
        A('-- §4 REPEATABILITY AND GATE ' + '-' * 45)
        A('pre-registered   : %s' % ', '.join(REHEARSAL_SESSIONS))
        have = {r['label'] for r in rset}
        miss = [d for d in REHEARSAL_SESSIONS if d not in have]
        extra = sorted(have - set(REHEARSAL_SESSIONS))
        if miss:
            A('NOT YET RUN      : %s' % ', '.join(miss))
        if extra:
            A('NON-REHEARSAL    : %s  (included by --sessions)' % ', '.join(extra))
        A('sessions         : %d' % s['n'])
        if s['sigma'] is None:
            A('sigma            : -- (%s)' % why)
            if miss:
                A('                   the study is not finished; %d session(s) to go'
                  % len(miss))
        else:
            A('session means    : %s' % ', '.join('%.2f' % r['mean'] for r in rset))
            A('mean of means    : %s' % _fmt(s['mean'], 2))
            A('range            : %s .. %s  (%s yr)'
              % (_fmt(s['min'], 2), _fmt(s['max'], 2), _fmt(s['range'], 2)))
            A('sigma (SD, n-1)  : %s yr' % _fmt(s['sigma']))
            A('')
            if miss:
                A('GATE DECISION    : NOT YET -- %d of %d rehearsal sessions run'
                  % (len(rset), len(REHEARSAL_SESSIONS)))
                A('                   sigma above is provisional. The gate is')
                A('                   evaluated once, on the full set (§4).')
            else:
                A('GATE DECISION    : %s' % dec)
                A('                   %s' % why)
            for cad, nn, hh in (('monthly', 6, DAYS_PER_MONTH),
                                ('weekly', 26, 7.0)):
                se = se_total_change(s['sigma'], nn, hh)
                if se:
                    A('  %-8s n=%-3d SE(total change over 26wk) = %.2f yr'
                      '   detectable at 2 SE = %.2f yr'
                      % (cad, nn, se, 2 * se))
        A('')

    if args.trend or args.all:
        t = trend(rows)
        A('-- §3 TREND ' + '-' * 62)
        b = (anchors or {}).get('B')
        if b:
            A('series starts at : B = %s' % b.isoformat())
            if pre_b:
                A('excluded pre-B   : %d rehearsal session(s)' % len(pre_b))
        else:
            A('B not set        : every session is in the fit, rehearsal included.')
            A('                   Set it with `faceage anchor B YYYY-MM-DD`.')
        if not t.get('ok'):
            A('not computed: %s' % t.get('why'))
        else:
            A('window           : %s .. %s  (%d days, n=%d)'
              % (t['first'], t['last'], t['window_days'], t['n']))
            lo, hi = t['slope_ci']
            A('slope            : %+.3f yr/month   95%% CI [%+.3f, %+.3f]'
              % (t['slope_per_month'], lo, hi))
            lo2, hi2 = t['total_ci']
            A('total change     : %+.3f yr          95%% CI [%+.3f, %+.3f]'
              % (t['total_change'], lo2, hi2))
            A('residual SE      : %s yr' % _fmt(t['resid_se']))
            dev = deviation_note(t['slope_per_month'])
            A('deviation slope  : %+.3f yr/month  (raw minus chronological drift)'
              % dev)
            A('')
            A('RESULT           : %s' % t['verdict'].upper())
            if not t['detected']:
                A('                   The CI includes zero. Not "trending", not')
                A('                   "early signs", not "directionally encouraging".')
        A('')

    if args.luma or args.all:
        l = luma_check(rows)
        A('-- §6 FALSIFICATION CHECK (exposure) ' + '-' * 37)
        if not l.get('ok'):
            A('not computed: %s' % l.get('why'))
        else:
            A('sessions w/ luma : %d' % l['n'])
            A('luma first/last  : %s -> %s' % (_fmt(l['luma_first'], 1),
                                               _fmt(l['luma_last'], 1)))
            A('max drift vs s1  : %s  (tolerance +/-5)'
              % _fmt(l['max_drift_from_session_one'], 1))
            A('luma range       : %s' % _fmt(l['luma_range'], 2))
            if l['over_tolerance']:
                A('OVER TOLERANCE   : session index %s'
                  % ', '.join(str(i) for i in l['over_tolerance']))
            A('corr(luma, mean) : %s' % _fmt(l['r']))
            A('luma slope       : %s /month' % _fmt(l['luma_slope_per_month'], 2))
            if l['concern']:
                A('')
                A('WARNING          : the exposure trace and the FaceAge trace share')
                A('                   a shape. Treat the trend as photography until')
                A('                   shown otherwise (§6).')
        A('')

    A('=' * 74)
    return '\n'.join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description='Pre-registered FaceAge analysis')
    p.add_argument('--history', default=None)
    p.add_argument('--results', default=None,
                   help='results dir containing faceage_history.csv')
    p.add_argument('--validity', default=None,
                   help='session_validity.csv (default: alongside the history)')
    p.add_argument('--sessions', default=None,
                   help='comma-separated session dates for the repeatability '
                        'set (default: the pre-registered §4 dates)')
    p.add_argument('--anchors', default=None,
                   help='anchors.csv holding B and R (default: alongside history)')
    p.add_argument('--sigma', action='store_true', help='§4 repeatability + gate')
    p.add_argument('--trend', action='store_true', help='§3 OLS slope + CI')
    p.add_argument('--luma', action='store_true', help='§6 falsification check')
    p.add_argument('--all', action='store_true', help='everything')
    p.add_argument('--json', action='store_true')
    args = p.parse_args(argv)

    if not (args.sigma or args.trend or args.luma or args.all):
        args.all = True

    results = os.path.expanduser(
        args.results or os.environ.get('FACEAGE_RESULTS', '~/FaceAgeData/results'))
    history = args.history or os.path.join(results, 'faceage_history.csv')
    validity = args.validity or os.path.join(results, 'session_validity.csv')

    anchors_path = args.anchors or os.path.join(results, 'anchors.csv')

    rows, dropped = load_history(history, load_validity(validity))
    anchors = load_anchors(anchors_path)

    # The repeatability set is the pre-registered §4 dates unless overridden.
    want = ({s.strip() for s in args.sessions.split(',') if s.strip()}
            if args.sessions else set(REHEARSAL_SESSIONS))
    rehearsal = [r for r in rows if r['label'] in want]

    # The trend runs on the series, which starts at B once B is set.
    series, pre_b = series_for_trend(rows, anchors)

    if args.json:
        st = sigma(rehearsal)
        complete = all(d in {r['label'] for r in rehearsal}
                       for d in REHEARSAL_SESSIONS)
        dec, why = gate(st['sigma']) if complete else ('NOT YET', 'rehearsal incomplete')
        print(json.dumps({
            'n_valid': len(rows),
            'dropped': [{'session': d['label'], 'reason': d.get('reason')}
                        for d in dropped],
            'anchors': {k: v.isoformat() for k, v in anchors.items()},
            'rehearsal': {'requested': sorted(want),
                          'found': [r['label'] for r in rehearsal],
                          'complete': complete},
            'sigma': st, 'gate': {'decision': dec, 'detail': why},
            'trend': trend(series), 'luma': luma_check(series),
        }, indent=2, default=str))
        return 0

    print(report(series, dropped, args, rehearsal=rehearsal,
                 anchors=anchors, pre_b=pre_b))
    return 0


if __name__ == '__main__':
    sys.exit(main())
