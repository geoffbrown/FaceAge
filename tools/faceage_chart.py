#!/usr/bin/env python3
"""Generate a local FaceAge tracker dashboard from the session history CSV.

Reads ~/FaceAgeData/results/faceage_history.csv and writes tracker.html beside it.
Stdlib only, no network, no external assets - the page works offline and nothing
leaves this machine. Contains summary statistics only; no photographs.
"""
import csv, json, os, sys, math, html, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faceage_analysis as fa

SUBJECT = os.environ.get('FACEAGE_SUBJECT_LABEL', 'me')
RESULTS = os.path.expanduser(os.environ.get(
    'FACEAGE_RESULTS', '~/FaceAgeData/subjects/%s/results' % SUBJECT))
HISTORY  = os.path.join(RESULTS, 'faceage_history.csv')
VALIDITY = os.path.join(RESULTS, 'session_validity.csv')
ANCHORS  = os.path.join(RESULTS, 'anchors.csv')
NOTES    = os.path.join(RESULTS, 'session_notes.json')
OUT      = os.path.join(RESULTS, 'tracker.html')
PROFILE  = os.path.join(os.path.dirname(RESULTS), 'profile.json')
STUDY_DAYS = 182            # the pre-registered six-month window

LUMA_TOL = float(os.environ.get('FACEAGE_LUMA_TOL') or 5.0)   # set with `faceage tolerance`
W, H     = 760, 210     # chart geometry
PAD_L, PAD_R, PAD_T, PAD_B = 54, 18, 14, 30


def run_time(raw):
    """The session's run timestamp, rendered in local time.

    Timestamps written since the UTC change carry an explicit offset and are
    converted. Rows written before it are naive LOCAL time — treating those as
    UTC would shift them by the local offset, which for PDT is 7 hours and moves
    a morning session into the previous evening. A naive value is therefore
    shown as-is, because local is exactly what it already is.
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    try:
        ts = datetime.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return ''
    if ts.tzinfo is None:
        return ts.strftime('%-I:%M %p')
    return ts.astimezone().strftime('%-I:%M %p')


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def load():
    if not os.path.exists(HISTORY):
        sys.exit("No history yet at %s. Run a session first." % HISTORY)
    # A session recorded as a protocol failure (§1) must not be drawn as if it
    # were data. It is listed in the table, struck through, with its reason.
    validity = fa.load_validity(VALIDITY)
    rows, excluded = [], []
    with open(HISTORY) as fh:
        for r in csv.DictReader(fh):
            d = (r.get('session_date') or '').strip()
            try:
                date = datetime.date.fromisoformat(d[:10])
            except ValueError:
                continue                      # skip non-date session labels
            mean = num(r.get('mean'))
            if mean is None:
                continue
            n = int(num(r.get('n')) or 0)
            std = num(r.get('std'))
            rec = {
                'date': date, 'label': d, 'mean': mean, 'n': n, 'std': std,
                'se': (std / math.sqrt(n)) if (std and n > 1) else 0.0,
                'median': num(r.get('median')),
                'luma': num(r.get('mean_luma')),
                'flagged': int(num(r.get('n_flagged')) or 0),
                'failed': int(num(r.get('n_failed')) or 0),
                'run_time': run_time(r.get('run_timestamp')),
                'notes': (r.get('notes') or '').strip(),
                'camera': camera_of(d, r.get('image_dir')),
            }
            ok, reason = validity.get(d, (True, ''))
            if ok:
                rows.append(rec)
            else:
                rec['reason'] = reason
                excluded.append(rec)
    if not rows:
        sys.exit("History has no usable dated sessions.")

    # Notes typed into a tracker opened as a plain file arrive as a sidecar the
    # browser downloaded. Served by `faceage app` they are saved directly and
    # this path never runs.
    if os.path.exists(NOTES):
        try:
            with open(NOTES) as nf:
                sidecar = json.load(nf)
            merged = False
            for r in rows + excluded:
                if r['label'] in sidecar:
                    r['notes'] = sidecar[r['label']]
                    merged = True
            if merged:
                write_notes(sidecar)
                os.remove(NOTES)
        except (json.JSONDecodeError, OSError):
            pass
    return (sorted(rows, key=lambda r: r['date']),
            sorted(excluded, key=lambda r: r['date']))


def camera_of(label, image_dir=None):
    """Which camera a session was shot on.

    The app writes capture.json beside photos it took with the Mac's camera;
    a session without one was imported, which for this study means the phone.
    Sessions on different cameras are different instruments and are not
    comparable, so the table says which is which.
    """
    candidates = []
    if image_dir:
        candidates.append(os.path.expanduser(image_dir))
    candidates.append(os.path.join(os.path.dirname(RESULTS), 'sessions', label))
    for d in candidates:
        p = os.path.join(d, 'capture.json')
        if os.path.exists(p):
            try:
                with open(p) as fh:
                    src = json.load(fh).get('source')
            except (OSError, ValueError):
                src = None
            return 'Mac' if src == 'mac-camera' else 'Mac?'
        if os.path.isdir(d):
            return 'Phone'
    return ''


def birthday():
    try:
        with open(PROFILE) as fh:
            b = (json.load(fh) or {}).get('birthday')
        return datetime.date.fromisoformat(b) if b else None
    except (OSError, ValueError, TypeError):
        return None


def age_on(bday, day):
    return (day - bday).days / 365.2425 if bday else None


def scale(vals, pad_frac=0.2):
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * pad_frac
    return lo - pad, hi + pad


def camera_baselines(rows):
    """First logged brightness per camera. Brightness only compares within one
    camera; a phone number says nothing about how bright the Mac should read."""
    base = {}
    for r in rows:
        cam = r.get('camera') or ''
        if r.get('luma') is not None and cam not in base:
            base[cam] = r['luma']
    return base


MARK = {'Mac': 'circle', 'Phone': 'triangle', 'Mac?': 'circle', '': 'circle'}
SERIES = {'Mac': 'var(--series-1)', 'Phone': 'var(--series-2)',
          'Mac?': 'var(--series-1)', '': 'var(--series-1)'}


def mark(x, y, kind, fill, hollow=False, extra=''):
    style = ('fill:var(--surface-2);stroke:%s;stroke-width:2' % fill) if hollow \
        else ('fill:%s' % fill)
    if kind == 'triangle':
        return ('<polygon class="dot%s" points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" style="%s"/>'
                % (extra, x, y - 6.5, x - 6, y + 4.5, x + 6, y + 4.5, style))
    return '<circle class="dot%s" cx="%.1f" cy="%.1f" r="5.5" style="%s"/>' % (extra, x, y, style)


def plot(rows, pre_b, fit, anchors, W=760, H=250, domain=None, bday=None):
    """The one chart: FaceAge per session over time.

    One measure, so one axis. Camera is carried by shape and hue (legend in the
    card header). Sessions not in the trend (before the baseline anchor) are
    hollow. The thin bar is +/-1 SE across the session's photos. The dashed
    line and ribbon are the fitted trend once three sessions exist.
    """
    PL, PR, PT, PB = 48, 18, 16, 30
    pts = [r for r in rows if r.get('mean') is not None]
    if not pts:
        return '<p class="empty">No sessions yet.</p>'
    vals = []
    for r in pts:
        vals += [r['mean'] - r['se'], r['mean'] + r['se']]
    if fit:
        x0, x1 = int(fit['x0']), int(fit['x1'])
        vals += [fit['fn'](x0)[0], fit['fn'](x1)[0]]      # the line, not its ribbon
    d0, d1 = pts[0]['date'], pts[-1]['date']
    for a in (anchors or {}).values():
        d0, d1 = min(d0, a), max(d1, a)
    if domain:
        d0, d1 = domain
        pts = [r for r in pts if d0 <= r['date'] <= d1]
        if not pts:
            return '<p class="empty">No sessions in this window.</p>'
        vals = []
        for r in pts:
            vals += [r['mean'] - r['se'], r['mean'] + r['se']]
    elif d0 == d1:                           # one session: centre it, a week either side
        d0, d1 = d0 - datetime.timedelta(days=7), d1 + datetime.timedelta(days=7)
    if bday:                                 # the real-age line should be in view when close
        a0, a1 = age_on(bday, d0), age_on(bday, d1)
        if min(vals) - 6 < a1 and max(vals) + 6 > a0:
            vals += [a0, a1]
    ymin, ymax = scale(vals)
    if ymax - ymin < 4:                      # never let a flat week look dramatic
        mid = (ymax + ymin) / 2
        ymin, ymax = mid - 2, mid + 2
    # whole-number ticks that sit inside the range, so the axis reads 39, 40, 41
    step = 1 if (ymax - ymin) <= 7 else (2 if (ymax - ymin) <= 14 else 5)
    ticks = [v for v in range(int(math.floor(ymin)), int(math.ceil(ymax)) + 1) if v % step == 0 and ymin <= v <= ymax]

    span = max((d1 - d0).days, 1)
    pre_labels = {r['label'] for r in pre_b}

    def X(d):
        return PL + ((d - d0).days / span) * (W - PL - PR)

    def Y(v):
        return PT + (1 - (v - ymin) / (ymax - ymin)) * (H - PT - PB)

    out = ['<defs><clipPath id="plotclip"><rect x="%d" y="%d" width="%d" height="%d"/></clipPath></defs>'
           % (PL, PT, W - PL - PR, H - PT - PB)]
    for v in ticks:
        y = Y(v)
        out.append('<line class="grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (PL, y, W - PR, y))
        out.append('<text class="ylab" x="%.1f" y="%.1f">%d</text>' % (PL - 8, y + 3.5, v))
    # x labels: the window's ends and its middle, so an empty future still reads
    for d, anc in ((d0, 'start'), (d0 + datetime.timedelta(days=span // 2), 'middle'), (d1, 'end')):
        out.append('<text class="xlab" x="%.1f" y="%.1f">%s</text>'
                   % (X(d), H - 9, d.strftime('%-d %b' if d0.year == d1.year else '%-d %b %y')))
    today = datetime.date.today()
    if d0 < today < d1:
        out.append('<line class="today" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (X(today), PT, X(today), H - PB))
    if bday:
        y0, y1 = Y(age_on(bday, d0)), Y(age_on(bday, d1))
        out.append('<line class="agelin" clip-path="url(#plotclip)" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (PL, y0, W - PR, y1))
        if PT <= y1 <= H - PB:
            out.append('<text class="agelab" x="%.1f" y="%.1f">your age</text>' % (W - PR, y1 - 5))

    if fit:
        x0, x1 = int(fit['x0']), int(fit['x1'])
        step = max(1, (x1 - x0) // 40 or 1)
        xs = list(range(x0, x1 + 1, step))
        if xs[-1] != x1:
            xs.append(x1)
        ref = fit['ref']
        def FX(dx):
            return X(ref + datetime.timedelta(days=dx))
        hi = ' '.join('%.1f,%.1f' % (FX(dx), Y(fit['fn'](dx)[2])) for dx in xs)
        lo = ' '.join('%.1f,%.1f' % (FX(dx), Y(fit['fn'](dx)[1])) for dx in reversed(xs))
        out.append('<polygon class="fitband" clip-path="url(#plotclip)" points="%s %s"/>' % (hi, lo))
        out.append('<line class="fit" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (FX(x0), Y(fit['fn'](x0)[0]), FX(x1), Y(fit['fn'](x1)[0])))

    for name, adate in sorted((anchors or {}).items()):
        ax = X(adate)
        out.append('<line class="anchor" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (ax, PT, ax, H - PB))
        out.append('<text class="anchorlab" x="%.1f" y="%.1f">%s</text>'
                   % (ax + 5, PT + 9, 'start' if name == 'B' else 'retest'))

    if len(pts) > 1:
        out.append('<polyline class="line" points="%s"/>'
                   % ' '.join('%.1f,%.1f' % (X(r['date']), Y(r['mean'])) for r in pts))
    for r in pts:
        x, y = X(r['date']), Y(r['mean'])
        if r['se']:
            out.append('<line class="se" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                       % (x, Y(r['mean'] - r['se']), x, Y(r['mean'] + r['se'])))
        cam = r.get('camera') or ''
        out.append(mark(x, y, MARK.get(cam, 'circle'), SERIES.get(cam, 'var(--series-1)'),
                        hollow=r['label'] in pre_labels))
        tip = '%s: %.1f, %d photos%s' % (r['date'].strftime('%-d %b %Y'), r['mean'], r['n'],
                                          (', ' + cam) if cam else '')
        out.append('<circle class="hit" cx="%.1f" cy="%.1f" r="16" data-tip="%s"/>'
                   % (x, y, html.escape(tip, quote=True)))
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="FaceAge per session over time">%s</svg>'
            % (W, H, ''.join(out)))


def spark(rows, base_by_cam, W=760, H=120):
    """Small brightness chart for the details section, banded per camera."""
    PL, PR, PT, PB = 48, 18, 10, 24
    pts = [r for r in rows if r.get('luma') is not None]
    if not pts:
        return '<p class="empty">No brightness data yet.</p>'
    vals = [r['luma'] for r in pts]
    for b in base_by_cam.values():
        vals += [b - LUMA_TOL, b + LUMA_TOL]
    ymin, ymax = scale(vals)
    d0, d1 = pts[0]['date'], pts[-1]['date']
    span = max((d1 - d0).days, 1)

    def X(d):
        return PL + ((d - d0).days / span) * (W - PL - PR)

    def Y(v):
        return PT + (1 - (v - ymin) / (ymax - ymin)) * (H - PT - PB)

    out = []
    for cam, b in base_by_cam.items():
        out.append('<rect class="band" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>'
                   % (PL, Y(b + LUMA_TOL), W - PL - PR, max(Y(b - LUMA_TOL) - Y(b + LUMA_TOL), 1)))
    for i in range(3):
        v = ymin + (ymax - ymin) * i / 2.0
        out.append('<text class="ylab" x="%.1f" y="%.1f">%.0f</text>' % (PL - 8, Y(v) + 3.5, v))
    for r in pts:
        x, y = X(r['date']), Y(r['luma'])
        cam = r.get('camera') or ''
        out.append(mark(x, y, MARK.get(cam, 'circle'), SERIES.get(cam, 'var(--series-1)')))
        out.append('<circle class="hit" cx="%.1f" cy="%.1f" r="14" data-tip="%s"/>'
                   % (x, y, html.escape('%s: brightness %.0f' % (r['label'], r['luma']), quote=True)))
    out.append('<text class="xlab" x="%.1f" y="%.1f">%s</text>' % (X(d0), H - 6, d0.strftime('%-d %b')))
    out.append('<text class="xlab" x="%.1f" y="%.1f">%s</text>' % (X(d1), H - 6, d1.strftime('%-d %b')))
    return '<svg viewBox="0 0 %d %d" role="img" aria-label="brightness per session">%s</svg>' % (W, H, ''.join(out))


def status(series, trend, sig, cameras_in_series, anchors):
    """One headline and one paragraph, in plain words, from the pre-registered
    numbers. The states are fixed so the page never invents a story."""
    n = len(series)
    span = (series[-1]['date'] - series[0]['date']).days if n > 1 else 0
    wob = ('Numbers move by about %.1f years between sessions with nothing real changing, '
           % sig['sigma']) if sig.get('sigma') else ''
    if len(cameras_in_series) > 1:
        return ('warn', 'Two cameras in the mix',
                'These sessions come from %s. The camera changes the number before your face does, '
                'so this line is not one story yet. Keep one camera from here on, and delete or '
                'set aside the sessions from the other.' % ' and '.join(cameras_in_series))
    if n < 3 or not trend.get('ok'):
        return ('neutral', 'Too early to call',
                'You have %d session%s. A trend needs three, and about a month of them before it '
                'means much. Keep shooting weekly, the same way each time.' % (n, '' if n == 1 else 's'))
    if span < 28:
        return ('neutral', 'Early days',
                '%d sessions over %d days. %sso read the trend after a month or more, '
                'not from one week to the next.' % (n, span, wob or 'Week to week is mostly noise, '))
    per = trend['slope_per_month']
    if trend['detected'] and per < 0:
        return ('good', 'Trending younger',
                'About %.1f years per month over %d days, and the change is bigger than the '
                'wobble. Keep doing exactly what you are doing.' % (abs(per), span))
    if trend['detected'] and per > 0:
        return ('warn', 'Trending older',
                'About %.1f years per month over %d days. Before reading anything into it, check '
                'that the light, camera and framing have stayed the same. Those move the number '
                'more than real change does.' % (per, span))
    return ('neutral', 'Holding steady',
            'Across %d sessions and %d days the line is flat within the normal wobble. '
            'No change yet, in either direction.' % (n, span))


HELP = {
    'mean': 'The average FaceAge across the photos in this session. This is the number the tracker follows.',
    'median': 'The middle photo of the session. Less swayed by one odd photo than the average.',
    'n': 'Photos that went into the number. Ten is the target; fewer means a noisier session.',
    'sd': 'How much the photos in this session disagreed with each other. Under about 1.5 is normal.',
    'exposure': 'Brightness on your face, 0 to 255. Keep it within %.0f of your first session on this camera. Lighting alone can move FaceAge by years.' % LUMA_TOL,
    'flagged': 'Photos the analysis had a doubt about: face too small in the frame, low confidence, or clipped at an edge.',
    'time': 'When the analysis ran, in local time.',
    'camera': 'Which camera took the photos. Only sessions from the same camera can be compared.',
    'setup': 'Clean means the brightness on your face matched your first session on this camera and every photo passed. The model reacts to light and framing as much as to your face.',
}


def hlp(key, label=None):
    return '<span class="help" data-help="%s">%s</span>' % (html.escape(HELP[key], quote=True),
                                                             html.escape(label or key.upper()))


def main():
    rows, excluded = load()
    anchors = fa.load_anchors(ANCHORS)
    series, pre_b = fa.series_for_trend(rows, anchors)
    trend = fa.trend(series)
    sig = fa.sigma(series) if len(series) >= 2 else {}
    cams_in_series = {r.get('camera') for r in series if r.get('camera')}
    fit = make_fit(series, series[0]['date']) if (len(series) >= 3 and len(cams_in_series) <= 1) else None
    if fit:
        fit['ref'] = series[0]['date']
    latest = rows[-1]
    bday = birthday()
    base_by_cam = camera_baselines(rows)
    for r in rows + excluded:
        b = base_by_cam.get(r.get('camera') or '')
        r['_base'] = b
        r['_suspect'] = (b is not None and r.get('luma') is not None
                         and abs(r['luma'] - b) > LUMA_TOL)
    cams_series = sorted({r['camera'] for r in series if r.get('camera')})
    cams_all = sorted({r['camera'] for r in rows + excluded if r.get('camera')})
    tone, head, para = status(series, trend, sig, cams_series, anchors)

    # ---- hero ----
    cam_word = {'Mac': 'this Mac’s camera', 'Phone': 'phone photos'}.get(latest.get('camera') or '', '')
    hero_sub = '%s%s · %d photos' % (latest['date'].strftime('%-d %b %Y'),
                                          (' · ' + cam_word) if cam_word else '', latest['n'])
    gap_html = ''
    if bday:
        real = age_on(bday, latest['date'])
        gap = latest['mean'] - real
        word = 'younger' if gap < 0 else 'older'
        gap_html = ('<div class="gap %s"><b>%.1f years %s</b> than your age of %.1f</div>'
                    % ('good' if gap < 0 else ('warn' if gap > 0 else ''), abs(gap), word, real))
    else:
        gap_html = '<div class="gap muted">Add your birthday on the people page to see this against your real age.</div>'
    if trend.get('ok') and bday:
        per = trend['slope_per_month']
        para += (' For scale: the calendar adds 0.08 years a month; over this stretch your FaceAge %s %.2f a month.'
                 % ('fell' if per < 0 else 'rose', abs(per)))
    hero = ('<section class="card hero"><div class="hero-l"><div class="eyebrow">Your FaceAge, latest session</div>'
            '<div class="hero-num">%.1f</div><div class="hero-sub">%s</div>%s</div>'
            '<div class="hero-r"><div class="status %s"><div class="status-h">%s</div><p>%s</p></div></div></section>'
            % (latest['mean'], html.escape(hero_sub), gap_html, tone, html.escape(head), html.escape(para)))

    # ---- chart ----
    legend = ''
    if len(cams_all) > 1 or pre_b:
        items = []
        for c in cams_all:
            items.append('<span class="lg"><i class="sw %s" style="--c:%s"></i>%s</span>'
                         % (MARK.get(c, 'circle'), SERIES.get(c, 'var(--series-1)'), html.escape(c)))
        if pre_b:
            items.append('<span class="lg"><i class="sw circle hollow"></i>before the start line, not in the trend</span>')
        legend = '<div class="legend">%s</div>' % ''.join(items)
    caption = ('Each point is one session: the average of its photos, with a thin bar for how much '
               'those photos disagreed. ')
    if bday:
        caption += 'The grey line is your real age, rising as the calendar does. '
    if fit:
        caption += 'The dashed line is the trend, with the grey ribbon showing how sure it is.'
    elif len(cams_series) > 1:
        caption += 'No trend line while two cameras are mixed.'
    elif len(rows) == 1:
        caption += 'Your first point. The line starts with your next session.'
    else:
        caption += 'A trend line appears once there are three sessions.'
    # ---- ranges: the study window is the default; the empty right side is the future ----
    today = datetime.date.today()
    start = anchors.get('B') or rows[0]['date']
    study_end = max(start + datetime.timedelta(days=STUDY_DAYS), rows[-1]['date'])
    ranges = [('30d', '30 days', (max(rows[0]['date'], today - datetime.timedelta(days=30)), max(today, rows[-1]['date']))),
              ('90d', '90 days', (max(rows[0]['date'], today - datetime.timedelta(days=90)), max(today, rows[-1]['date']))),
              ('study', '6 months', (start, study_end)),
              ('all', 'All', None)]
    default = 'study'
    if bday:
        legend = legend.replace('</div>', '<span class="lg"><i class="sw agesw"></i>your age</span></div>') if legend             else '<div class="legend"><span class="lg"><i class="sw agesw"></i>your age</span></div>'
    rng_buttons = ''.join('<button class="rng%s" data-range="%s">%s</button>'
                          % (' on' if k == default else '', k, html.escape(lab)) for k, lab, _ in ranges)
    rng_svgs = ''.join('<div class="rview" data-range="%s"%s>%s</div>'
                       % (k, '' if k == default else ' hidden',
                          plot(rows, pre_b, fit, anchors, domain=dom, bday=bday))
                       for k, _, dom in ranges)
    chart_card = ('<section class="card"><div class="card-h"><h2>Over time</h2><div class="ranges">%s</div>%s</div>%s'
                  '<p class="caption">%s</p></section>' % (rng_buttons, legend, rng_svgs, caption))

    good_n = sum(1 for r in rows if not r['_suspect'] and r['flagged'] == 0)

    # ---- sessions table ----
    def setup_pill(r):
        probs = []
        if r['_base'] is not None and r['luma'] is not None and r['_suspect']:
            probs.append('light off by %.0f' % abs(r['luma'] - r['_base']))
        if r['flagged']:
            probs.append('%d photo%s flagged' % (r['flagged'], '' if r['flagged'] == 1 else 's'))
        if probs:
            return '<span class="setup bad" title="%s">! %s</span>' % (html.escape('; '.join(probs), quote=True), html.escape(', '.join(probs)))
        first = r['_base'] is not None and r['luma'] is not None and abs(r['luma'] - r['_base']) < 0.05
        return '<span class="setup ok" title="Brightness matched your first session on this camera and every photo passed">✓ %s</span>' % ('baseline' if first else 'clean')

    def row_html(r, exc=False):
        lab = html.escape(r['label'], quote=True)
        cam = html.escape(r.get('camera') or '')
        if exc:
            main_cells = ('<td class="d">%s<span class="tagx">set aside</span></td><td>%s</td><td>%s</td>'
                          '<td class="r">%.2f</td><td class="r">%s</td><td class="r">%d</td><td>%s</td>'
                          % (html.escape(r['label']), cam, html.escape(r['run_time']), r['mean'],
                             ('%.2f' % r['median']) if r['median'] is not None else '–', r['n'], setup_pill(r)))
        else:
            main_cells = ('<td class="d">%s</td><td>%s</td><td>%s</td><td class="r">%.2f</td>'
                          '<td class="r">%s</td><td class="r">%d</td><td>%s</td>'
                          % (html.escape(r['label']), cam, html.escape(r['run_time']), r['mean'],
                             ('%.2f' % r['median']) if r['median'] is not None else '–', r['n'], setup_pill(r)))
        exp = ('%.0f' % r['luma']) if r['luma'] is not None else '–'
        if r['luma'] is not None and r['_base'] is not None:
            dlt = r['luma'] - r['_base']
            exp += ' <span class="muted">(%s%.0f vs baseline)</span>' % ('+' if dlt > 0 else '', dlt) if abs(dlt) >= 0.5 else ' <span class="muted">(baseline)</span>'
        sd = ('%.2f' % r['std']) if r['std'] else '–'
        detail = ('<tr class="detail" data-for="%s"><td colspan="10"><div class="dgrid">'
                  '<div><div class="dk">%s</div><div class="dv">%s</div></div>'
                  '<div><div class="dk">%s</div><div class="dv">%s</div></div>'
                  '<div><div class="dk">%s</div><div class="dv">%d</div></div>'
                  '%s'
                  '<div class="dnote"><div class="dk">NOTES</div><div class="note" contenteditable data-session="%s">%s</div></div>'
                  '</div></td></tr>'
                  % (lab, hlp('sd', 'Spread'), sd, hlp('exposure', 'Brightness'), exp,
                     hlp('flagged', 'Flagged photos'), r['flagged'],
                     ('<div><div class="dk">WHY SET ASIDE</div><div class="dv">%s</div></div>' % html.escape(r.get('reason') or '')) if exc else '',
                     lab, html.escape(r['notes'])))
        return ('<tr class="row%s" data-session="%s"><td class="chev"><button class="more" aria-label="details">›</button></td>'
                '<td class="sel"><input type="checkbox" class="pick" value="%s" aria-label="select %s"></td>'
                '%s<td class="act"><button class="btn sm fo" data-session="%s" title="Show this session\'s photos in Finder">Folder</button>'
                '<button class="btn sm del" data-session="%s">Delete</button></td></tr>%s'
                % (' exc' if exc else '', lab, lab, lab, main_cells, lab, lab, detail))
    trows = ''.join(row_html(r) for r in reversed(rows)) + ''.join(row_html(r, True) for r in reversed(excluded))
    table = ('<section class="card sessions" id="sessions"><div class="card-h"><h2>Sessions</h2>'
             '<button class="btn" id="edit">Edit</button><span class="sp"></span><span class="muted">%d of %d shot cleanly</span></div>'
             % (good_n, len(rows)) +
             '<div class="editbar" id="editbar" hidden><label class="sel-all"><input type="checkbox" id="pickall"> select all</label>'
             '<span id="selcount" class="muted">0 selected</span><span class="sp"></span>'
             '<button class="btn danger" id="delsel" disabled>Delete selected</button><button class="btn" id="editdone">Done</button></div>'
             '<div class="tablewrap"><table><thead><tr><th class="chev"></th><th class="sel"></th><th>Session</th><th>%s</th><th>%s</th>'
             '<th class="r">%s</th><th class="r">%s</th><th class="r">%s</th><th>%s</th><th></th></tr></thead>'
             '<tbody>%s</tbody></table></div>'
             '<p class="caption">Click a row for its spread, brightness, flagged photos and notes. '
             'Hover a column name for what it means.</p></section>'
             % (hlp('camera', 'Camera'), hlp('time', 'Time'), hlp('mean', 'Mean'), hlp('median', 'Median'), hlp('n', 'Photos'),
                hlp('setup', 'Setup'), trows))

    # ---- the numbers behind this ----
    sci = []
    if trend.get('ok'):
        lo, hi = trend['slope_ci']
        sci.append('Fitted trend: %+.2f years per month, 95%% confidence %+.2f to %+.2f, over %d days and %d sessions. '
                   'The pre-registered call is "%s": a change counts only when that range excludes zero.'
                   % (trend['slope_per_month'], lo, hi, trend['window_days'], trend['n'], trend['verdict']))
    else:
        sci.append('No fitted trend yet: it needs at least three sessions in the series.')
    if sig.get('sigma'):
        sci.append('Session to session wobble (standard deviation of session means): %.2f years across %d sessions.'
                   % (sig['sigma'], sig['n']))
    if not anchors.get('B'):
        sci.append('No start line is set, so every session including rehearsals is in the trend. '
                   'Set it with: faceage anchor B YYYY-MM-DD')
    elif pre_b:
        sci.append('%d rehearsal session(s) before the start line are shown hollow and left out of the trend.' % len(pre_b))
    if excluded:
        sci.append('%d session(s) set aside as protocol failures and kept out of every number here: %s.'
                   % (len(excluded), '; '.join('%s (%s)' % (r['label'], r.get('reason') or 'no reason') for r in excluded)))
    if len(cams_all) > 1:
        sci.append('Sessions were shot on more than one camera (%s). Numbers from different cameras are not comparable.'
                   % ', '.join('%s: %s' % (c, ', '.join(r['label'] for r in rows + excluded if r.get('camera') == c)) for c in cams_all))
    sci.append('Pairwise session to session differences are never interpreted; only the fitted trend is. '
               'Real change over one week is close to zero, so week to week movement is noise.')
    details = ('<details class="card nerd"><summary>The numbers behind this</summary>'
               '<h3>Brightness per session</h3><p class="sub">Green band: within %.0f of the first session on that camera.</p>%s'
               '<ul class="sci">%s</ul></details>'
               % (LUMA_TOL, spark(rows, base_by_cam), ''.join('<li>%s</li>' % html.escape(t) for t in sci)))

    page = PAGE.replace('__GEN__', datetime.datetime.now().strftime('%-d %b %Y, %H:%M'))
    page = (page.replace('__HERO__', hero).replace('__CHART__', chart_card)
                .replace('__CONS__', '').replace('__TABLE__', table)
                .replace('__DETAILS__', details).replace('__SUBJ__', html.escape(SUBJECT)))
    with open(OUT, 'w') as fh:
        fh.write(page)
    print(OUT)


# Raw string: no Python escapes can reach the JavaScript, so a \n written here
# is two characters on the page and never a newline inside a JS literal.
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaceAge tracker: __SUBJ__</title>
<style>
:root{color-scheme:light dark}
.viz-root{
  --surface-1:#f7f6f2; --surface-2:#ffffff; --border:#e6e4de;
  --text-primary:#111110; --text-secondary:#52514e; --text-muted:#7c7a73;
  --series-1:#2a78d6; --series-2:#eb6834; --grid:#edebe6;
  --band:rgba(12,163,12,.10); --good:#0ca30c; --good-bg:#e9f6e9; --warn:#b47600; --warn-bg:#fff4d6;
  --crit:#d03b3b; --crit-bg:#fdecec; --accent:#2a78d6; --accent-ink:#fff;
  --shadow:0 1px 2px rgba(0,0,0,.04),0 10px 30px -18px rgba(0,0,0,.18);
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
  --surface-1:#161615; --surface-2:#1f1f1e; --border:#33322f;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8e8c83;
  --series-1:#3987e5; --series-2:#d95926; --grid:#2b2a28;
  --band:rgba(12,163,12,.16); --good:#4fc26a; --good-bg:#173321; --warn:#e6b64a; --warn-bg:#3a2f12;
  --crit:#f06767; --crit-bg:#3b1c1c; --accent:#4c8df5; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);
}}
:root[data-theme="dark"] .viz-root{
  --surface-1:#161615; --surface-2:#1f1f1e; --border:#33322f;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8e8c83;
  --series-1:#3987e5; --series-2:#d95926; --grid:#2b2a28;
  --band:rgba(12,163,12,.16); --good:#4fc26a; --good-bg:#173321; --warn:#e6b64a; --warn-bg:#3a2f12;
  --crit:#f06767; --crit-bg:#3b1c1c; --accent:#4c8df5; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.viz-root{background:var(--surface-1);color:var(--text-primary);max-width:860px;margin:0 auto;padding:28px 20px 64px}
.top{display:flex;align-items:center;gap:12px;margin-bottom:18px}
h1{font-size:22px;margin:0;letter-spacing:-.015em}
.subj{padding:3px 10px;border-radius:99px;background:var(--surface-2);border:1px solid var(--border);font-size:12.5px;font-weight:600;color:var(--text-secondary)}
.sp{flex:1}
.gen{font-size:12px;color:var(--text-muted)}
.card{background:var(--surface-2);border:1px solid var(--border);border-radius:16px;padding:20px 22px;margin-bottom:14px;box-shadow:var(--shadow)}
.card-h{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.card h2{font-size:15px;margin:0;font-weight:700}
.card h3{font-size:13px;margin:16px 0 4px;color:var(--text-secondary)}
.sub,.caption{font-size:13px;color:var(--text-secondary);margin:4px 0 12px;line-height:1.5}
.caption{margin:10px 0 0}
.muted{color:var(--text-muted);font-size:12.5px}

/* hero */
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:24px;align-items:center;padding:26px 26px}
.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);font-weight:600}
.hero-num{font-size:76px;font-weight:700;letter-spacing:-.035em;line-height:1;margin:6px 0 8px}
.hero-sub{font-size:13.5px;color:var(--text-secondary)}
.status{padding:16px 18px;border-radius:14px;background:var(--surface-1);border:1px solid var(--border)}
.status.good{background:var(--good-bg);border-color:transparent}
.status.warn{background:var(--warn-bg);border-color:transparent}
.status-h{font-size:17px;font-weight:700;margin-bottom:4px;display:flex;align-items:center;gap:8px}
.status-h::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--text-muted)}
.status.good .status-h::before{background:var(--good)}
.status.warn .status-h::before{background:var(--warn)}
.status p{margin:0;font-size:14px;color:var(--text-secondary);line-height:1.5}
@media (max-width:640px){.hero{grid-template-columns:1fr}.hero-num{font-size:60px}}

/* chart */
svg{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}
.line{fill:none;stroke:var(--series-1);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.se{stroke:var(--series-1);stroke-width:1.5;opacity:.45}
.fitband{fill:var(--text-secondary);opacity:.10}
.fit{stroke:var(--text-primary);stroke-width:1.6;stroke-dasharray:5 4;opacity:.7}
.anchor{stroke:var(--text-muted);stroke-width:1;stroke-dasharray:2 3}
.anchorlab{fill:var(--text-muted);font-size:10px;font-weight:700;text-anchor:start;text-transform:uppercase;letter-spacing:.06em}
.dot{stroke:var(--surface-2);stroke-width:2}
.hit{fill:transparent;cursor:pointer}
.band{fill:var(--band)}
.ylab{fill:var(--text-muted);font-size:10.5px;text-anchor:end;font-variant-numeric:tabular-nums}
.xlab{fill:var(--text-muted);font-size:10.5px;text-anchor:middle}
.empty{color:var(--text-secondary);font-size:13px;padding:0 4px 12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--text-secondary);margin-left:auto}
.ranges{display:flex;gap:2px;padding:2px;border-radius:9px;background:var(--surface-1);border:1px solid var(--border)}
.ranges button{font:inherit;font-size:12px;font-weight:600;padding:4px 10px;border:0;border-radius:7px;background:transparent;color:var(--text-secondary);cursor:pointer}
.ranges button.on{background:var(--surface-2);color:var(--text-primary);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.rview[hidden]{display:none}
.today{stroke:var(--text-muted);stroke-width:1;stroke-dasharray:1 3}
.agelin{stroke:var(--text-muted);stroke-width:1.4;opacity:.8}
.agelab{fill:var(--text-muted);font-size:10px;text-anchor:end;font-weight:600}
.sw.agesw{border-radius:0;height:2px;width:14px;background:var(--text-muted)}
.gap{margin-top:10px;font-size:14px;color:var(--text-secondary)}
.gap.good b{color:var(--good)} .gap.warn b{color:var(--warn)}
.gap.muted{font-size:12.5px}
.lg{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:10px;height:10px;background:var(--c,var(--series-1));border-radius:50%}
.sw.triangle{border-radius:0;clip-path:polygon(50% 0,0 100%,100% 100%)}
.sw.hollow{background:transparent;border:2px solid var(--series-1)}
@media (prefers-reduced-motion:no-preference){
  .line{stroke-dasharray:2000;stroke-dashoffset:2000;animation:draw 1.2s ease-out forwards}
  .dot,.se{opacity:0;animation:pop .4s ease-out .6s forwards}
  .se{animation-name:popse}
  .card{animation:rise .5s ease-out both}
  .card:nth-child(2){animation-delay:.05s}.card:nth-child(3){animation-delay:.1s}.card:nth-child(4){animation-delay:.15s}
}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes pop{to{opacity:1}}
@keyframes popse{to{opacity:.45}}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* buttons */
.btn{font:inherit;font-size:13px;font-weight:600;padding:8px 12px;border-radius:9px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text-primary);cursor:pointer}
.btn:hover:not(:disabled){background:var(--surface-1)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.sm{padding:6px 9px;font-size:12.5px}
.btn.danger{color:var(--crit)}
.btn.danger:hover:not(:disabled){background:var(--crit-bg)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}

/* sessions */
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:10px 6px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);font-weight:600}
td.r,th.r{text-align:right}
td.d{font-weight:600;white-space:nowrap}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--surface-1)}
tr.row.open td{border-bottom-color:transparent}
tr.detail{display:none}
tr.detail.show{display:table-row}
tr.detail td{background:var(--surface-1);padding:8px 12px 14px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px 18px}
.dnote{grid-column:1/-1}
.dk{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);font-weight:600}
.dv{font-size:14px}
.note{min-height:22px;padding:4px 0;color:var(--text-secondary);cursor:text}
.note:empty::before{content:'add a note';color:var(--text-muted);font-style:italic}
.note:focus{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
td.act{white-space:nowrap;text-align:right;padding-right:0}
td.act .btn{margin-left:4px;padding:6px 8px}
td.chev,th.chev{width:28px;padding-left:4px;padding-right:0}
.more{all:unset;cursor:pointer;display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:6px;
  color:var(--text-muted);font-size:16px;transition:transform .2s}
.more:hover{background:var(--surface-1);color:var(--text-primary)}
tr.open .more{transform:rotate(90deg)}
.setup{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.setup.ok{background:var(--good-bg);color:var(--good)}
.setup.bad{background:var(--warn-bg);color:var(--warn)}
tr.exc td.d{color:var(--text-muted);text-decoration:line-through}
.tagx{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:6px;font-size:10.5px;font-weight:600;text-decoration:none;
  background:var(--warn-bg);color:var(--warn);text-transform:uppercase;letter-spacing:.04em}
.warn-dot{display:inline-block;margin-left:6px;width:16px;height:16px;border-radius:50%;background:var(--warn-bg);color:var(--warn);
  font-size:11px;font-weight:700;text-align:center;line-height:16px}
.sel{width:28px}
.sessions:not(.editing) .sel{display:none}
.sessions.editing td.act .del,.sessions.editing td.act .fo{display:none}
.editbar{display:flex;gap:12px;align-items:center;margin:8px 0 10px;padding:10px 12px;border-radius:10px;background:var(--surface-1)}
.editbar[hidden]{display:none}
.sel-all{display:flex;gap:6px;align-items:center;font-size:13px}
.help{border-bottom:1px dotted var(--text-muted);cursor:help}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;max-width:280px;
  background:var(--text-primary);color:var(--surface-1);padding:7px 10px;border-radius:8px;font-size:12.5px;line-height:1.4;z-index:9}
#save-bar{display:none;position:fixed;bottom:0;left:0;right:0;background:var(--accent);color:#fff;text-align:center;
  padding:10px;font-size:13px;font-weight:600;cursor:pointer;z-index:10}
details.nerd summary{cursor:pointer;font-weight:600;color:var(--text-secondary);font-size:14px}
ul.sci{margin:12px 0 0;padding-left:18px;color:var(--text-secondary);font-size:13px}
ul.sci li{margin-bottom:7px}
</style></head>
<body><div class="viz-root">
<header class="top"><h1>FaceAge</h1><span class="subj">__SUBJ__</span><span class="sp"></span>
  <span class="gen">Updated __GEN__</span><button class="btn sm" id="open-results">Open data folder</button></header>
__HERO__
__CHART__
__CONS__
__TABLE__
__DETAILS__
<p class="muted" style="text-align:center;margin-top:20px">Everything on this page stays on this Mac. Summary numbers only, no photographs.</p>
</div>
<div id="tip"></div>
<div id="save-bar">Save notes</div>
<script>
'use strict';
var SUBJ = '__SUBJ__';
var served = (location.protocol === 'http:' || location.protocol === 'https:');
var tip = document.getElementById('tip');
function showTip(text, x, y){ tip.textContent = text; tip.style.opacity = '1'; moveTip(x, y); }
function moveTip(x, y){ tip.style.left = Math.min(x + 14, window.innerWidth - 300) + 'px'; tip.style.top = (y - 36) + 'px'; }
function hideTip(){ tip.style.opacity = '0'; }
function bindTips(sel, attr){
  document.querySelectorAll(sel).forEach(function(el){
    el.addEventListener('mouseenter', function(e){ showTip(el.getAttribute(attr), e.clientX, e.clientY); });
    el.addEventListener('mousemove', function(e){ moveTip(e.clientX, e.clientY); });
    el.addEventListener('mouseleave', hideTip);
  });
}
bindTips('.hit', 'data-tip');
bindTips('.help', 'data-help');
/* time range: the study window by default; the rest are the same data re-framed */
document.querySelectorAll('.ranges button').forEach(function(b){
  b.addEventListener('click', function(){
    var k = b.getAttribute('data-range');
    document.querySelectorAll('.ranges button').forEach(function(x){ x.classList.toggle('on', x === b); });
    document.querySelectorAll('.rview').forEach(function(v){ v.hidden = v.getAttribute('data-range') !== k; });
    try { localStorage.setItem('faceage.range', k); } catch(e){}
    setTimeout(tell, 50);
  });
});
try { var savedRange = localStorage.getItem('faceage.range');
      var rb = savedRange && document.querySelector('.ranges button[data-range="' + savedRange + '"]');
      if(rb) rb.click(); } catch(e){}
/* inside the app: tell the frame how tall the page is */
function tell(){ if(window.parent !== window) window.parent.postMessage({faceage:'height', h:document.body.offsetHeight}, '*'); }
window.addEventListener('load', tell); window.addEventListener('resize', tell);
document.addEventListener('click', function(){ setTimeout(tell, 250); });
document.addEventListener('toggle', function(){ setTimeout(tell, 50); }, true);

function post(path, body){
  return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j; }); });
}
function needServer(){ alert('Open the tracker from the FaceAge app (faceage app) to use this.'); }

/* rows expand and collapse; buttons inside do not toggle */
document.querySelectorAll('tr.row').forEach(function(tr){
  tr.addEventListener('click', function(e){
    if(e.target.closest('button') || e.target.closest('input') || e.target.closest('.note')) return;
    toggle(tr);
  });
  tr.querySelector('.more').addEventListener('click', function(){ toggle(tr); });
});
function toggle(tr){
  var d = document.querySelector('tr.detail[data-for="' + tr.getAttribute('data-session') + '"]');
  tr.classList.toggle('open');
  if(d) d.classList.toggle('show');
}

/* open the folder */
document.querySelectorAll('button.fo').forEach(function(b){
  b.addEventListener('click', function(){
    if(!served) return needServer();
    post('/api/reveal', {person:SUBJ, date:b.getAttribute('data-session'), what:'session'})
      .catch(function(e){ alert('Could not open: ' + e.message); });
  });
});
document.getElementById('open-results').addEventListener('click', function(){
  if(!served) return needServer();
  post('/api/reveal', {person:SUBJ, what:'results'}).catch(function(e){ alert('Could not open: ' + e.message); });
});

/* delete: one, or the selection */
function del(dates){
  if(!served) return needServer();
  var list = dates.join(', ');
  var msg = (dates.length === 1 ? 'Delete session ' + list + '?' : 'Delete ' + dates.length + ' sessions (' + list + ')?') +
            ' This removes them from the tracker and deletes their photos and results from this Mac. It cannot be undone.';
  if(!confirm(msg)) return;
  post('/api/delete', {person:SUBJ, dates:dates})
    .then(function(){ location.reload(); })
    .catch(function(e){ alert('Could not delete: ' + e.message); });
}
document.querySelectorAll('button.del').forEach(function(b){
  b.addEventListener('click', function(){ del([b.getAttribute('data-session')]); });
});

/* edit mode: pick several, delete together */
var sessions = document.getElementById('sessions'), editbar = document.getElementById('editbar');
function picked(){ return Array.prototype.slice.call(document.querySelectorAll('.pick:checked')).map(function(c){ return c.value; }); }
function refreshSel(){
  var n = picked().length;
  document.getElementById('selcount').textContent = n + ' selected';
  document.getElementById('delsel').disabled = n === 0;
}
document.getElementById('edit').addEventListener('click', function(){ sessions.classList.add('editing'); editbar.hidden = false; refreshSel(); });
document.getElementById('editdone').addEventListener('click', function(){
  sessions.classList.remove('editing'); editbar.hidden = true;
  document.querySelectorAll('.pick').forEach(function(c){ c.checked = false; });
  document.getElementById('pickall').checked = false;
});
document.getElementById('pickall').addEventListener('change', function(e){
  document.querySelectorAll('.pick').forEach(function(c){ c.checked = e.target.checked; }); refreshSel();
});
document.querySelectorAll('.pick').forEach(function(c){ c.addEventListener('change', refreshSel); });
document.getElementById('delsel').addEventListener('click', function(){ var d = picked(); if(d.length) del(d); });

/* notes: saved through the app when served, downloaded as a sidecar when opened as a file */
var bar = document.getElementById('save-bar');
function gatherNotes(){
  var m = {};
  document.querySelectorAll('.note').forEach(function(td){ m[td.getAttribute('data-session')] = td.textContent.trim(); });
  return m;
}
document.querySelectorAll('.note').forEach(function(td){
  td.addEventListener('input', function(){ bar.style.display = 'block'; });
  td.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); td.blur(); } });
});
function done(msg, ms){ bar.textContent = msg; setTimeout(function(){ bar.style.display = 'none'; bar.textContent = 'Save notes'; }, ms); }
bar.addEventListener('click', function(){
  var notes = gatherNotes();
  if(served){
    bar.textContent = 'Saving';
    post('/api/notes', {person:SUBJ, notes:notes}).then(function(){ done('Saved', 1400); })
      .catch(function(e){ done('Could not save: ' + e.message, 5000); });
    return;
  }
  var blob = new Blob([JSON.stringify(notes, null, 2)], {type:'application/json'});
  var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'session_notes.json'; a.click(); URL.revokeObjectURL(a.href);
  done('Saved. Drop session_notes.json next to tracker.html, then regenerate', 3500);
});
</script>
</body></html>"""


def make_fit(series, ref_date):
    """§3 OLS fit as a drawable band: returns dx -> (yhat, lo, hi).

    The ribbon is the 95% CI for the FITTED MEAN, s*sqrt(1/n + (x-xbar)^2/Sxx),
    not a prediction interval for a future session -- it shows how well the
    trend line itself is pinned down, which is the quantity §3 reports.
    """
    if len(series) < 3:
        return None
    xs = [(r['date'] - ref_date).days for r in series]
    ys = [r['mean'] for r in series]
    f = fa.ols(xs, ys)
    if f is None or f['df'] < 1:
        return None
    t = fa.t_crit(f['df'])
    n, mx, sxx, sres = f['n'], sum(xs) / len(xs), f['sxx'], f['resid_se']

    def fn(dx):
        yhat = f['intercept'] + f['slope'] * dx
        half = t * sres * math.sqrt(1.0 / n + ((dx - mx) ** 2) / sxx)
        return (yhat, yhat - half, yhat + half)

    return {'fn': fn, 'x0': min(xs), 'x1': max(xs), 'ols': f, 't': t}


def write_notes(notes_dict):
    """Merge {session_date: note} into the history CSV, atomically.

    This rewrites the study's data of record, so it is written to a temporary
    file in the same directory and then renamed over the original. os.replace
    is atomic within a filesystem, so an interruption leaves the old history
    intact rather than a truncated one.

    Notes are commentary and nothing reads them for analysis. Session exclusion
    is governed solely by session_validity.csv, so a note can never quietly
    remove a session from the series.
    """
    with open(HISTORY, newline='') as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if 'notes' not in fieldnames:
        fieldnames.append('notes')
    for r in rows:
        sd = (r.get('session_date') or '').strip()
        if sd in notes_dict:
            r['notes'] = notes_dict[sd]
    tmp = HISTORY + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, HISTORY)


# kept so an older caller does not break
_write_notes_to_csv = write_notes


def set_note(session_date, note_text):
    """Set or replace the note for a session in the history CSV."""
    if not os.path.exists(HISTORY):
        sys.exit("No history yet at %s" % HISTORY)
    with open(HISTORY, newline='') as fh:
        dates = [(r.get('session_date') or '').strip() for r in csv.DictReader(fh)]
    if session_date not in dates:
        sys.exit("No session '%s' in history. Available: %s" % (session_date, ', '.join(dates)))
    _write_notes_to_csv({session_date: note_text})
    print("Note for %s: %s" % (session_date, note_text if note_text else '(cleared)'))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'note':
        session = sys.argv[2]
        text = ' '.join(sys.argv[3:]) if len(sys.argv) > 3 else ''
        set_note(session, text)
        main()  # regenerate the tracker after editing
    else:
        main()
