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

LUMA_TOL = 5.0          # exposure drift beyond this makes a session suspect
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
        sys.exit("No history yet at %s — run a session first." % HISTORY)
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


def scale(rows, key, lo_key=None, hi_key=None, pad_frac=0.18, band=None):
    vals = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        vals.append(v)
        if lo_key:
            vals += [v - r.get(lo_key, 0), v + r.get(hi_key or lo_key, 0)]
    if band:
        vals += list(band)
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * pad_frac
    return lo - pad, hi + pad


def chart(rows, key, color, se_key=None, band=None, fmt='%.2f', empty_msg='',
          fit=None, anchors=None):
    """One time-series panel. Single series, so no legend - the card title names it.

    `fit` is the §3 regression: {'fn': dx -> (y, lo, hi), 'x0': day-offset of the
    first fitted session, 'x1': day-offset of the last}. Drawn as a line with a
    95% confidence ribbon for the fitted mean.

    `anchors` marks B and R. The x domain is widened to include them, so a retest
    date months past the last session still appears.
    """
    pts = [r for r in rows if r.get(key) is not None]
    if not pts:
        return '<p class="empty">%s</p>' % html.escape(empty_msg)

    fitvals = []
    if fit:
        for dx in range(int(fit['x0']), int(fit['x1']) + 1,
                        max(1, (int(fit['x1']) - int(fit['x0'])) // 60 or 1)):
            fitvals += list(fit['fn'](dx))
    ymin, ymax = scale(pts, key, se_key, band=band or (tuple(
        (min(fitvals), max(fitvals))) if fitvals else None))

    d0, d1 = rows[0]['date'], rows[-1]['date']
    for a in (anchors or {}).values():
        if a < d0:
            d0 = a
        if a > d1:
            d1 = a
    span = max((d1 - d0).days, 1)

    def X(r):
        return PAD_L + ((r['date'] - d0).days / span) * (W - PAD_L - PAD_R)

    def Y(v):
        return PAD_T + (1 - (v - ymin) / (ymax - ymin)) * (H - PAD_T - PAD_B)

    out = []
    # target band (exposure only)
    if band:
        y_hi, y_lo = Y(band[1]), Y(band[0])
        out.append('<rect class="band" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>'
                   % (PAD_L, y_hi, W - PAD_L - PAD_R, max(y_lo - y_hi, 1)))

    # gridlines + y labels
    for i in range(4):
        v = ymin + (ymax - ymin) * i / 3.0
        y = Y(v)
        out.append('<line class="grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (PAD_L, y, W - PAD_R, y))
        out.append('<text class="ylab" x="%.1f" y="%.1f">%s</text>'
                   % (PAD_L - 8, y + 3.5, (fmt % v)))

    # x labels: first, last, and a middle one when there is room
    idxs = {0, len(pts) - 1}
    if len(pts) > 4:
        idxs.add(len(pts) // 2)
    for i in sorted(idxs):
        r = pts[i]
        out.append('<text class="xlab" x="%.1f" y="%.1f">%s</text>'
                   % (X(r), H - 9, r['date'].strftime('%d %b')))

    # §3 fitted slope with its 95% confidence ribbon, under everything else
    if fit:
        x0, x1 = int(fit['x0']), int(fit['x1'])
        step = max(1, (x1 - x0) // 60 or 1)
        xs = list(range(x0, x1 + 1, step))
        if xs[-1] != x1:
            xs.append(x1)
        shift = (rows[0]['date'] - d0).days
        def FX(dx):
            return PAD_L + ((dx + shift) / span) * (W - PAD_L - PAD_R)
        hi = ' '.join('%.1f,%.1f' % (FX(dx), Y(fit['fn'](dx)[2])) for dx in xs)
        lo = ' '.join('%.1f,%.1f' % (FX(dx), Y(fit['fn'](dx)[1]))
                      for dx in reversed(xs))
        out.append('<polygon class="fitband" points="%s %s"/>' % (hi, lo))
        out.append('<line class="fit" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (FX(x0), Y(fit['fn'](x0)[0]), FX(x1), Y(fit['fn'](x1)[0])))

    # B / R study anchors
    for name, adate in sorted((anchors or {}).items()):
        ax = PAD_L + ((adate - d0).days / span) * (W - PAD_L - PAD_R)
        out.append('<line class="anchor" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (ax, PAD_T, ax, H - PAD_B))
        out.append('<text class="anchorlab" x="%.1f" y="%.1f">%s</text>'
                   % (ax, PAD_T - 2, html.escape(name)))

    # error band (±1 SE) drawn under the line
    if se_key and any(r.get(se_key) for r in pts):
        up = ' '.join('%.1f,%.1f' % (X(r), Y(r[key] + r.get(se_key, 0))) for r in pts)
        dn = ' '.join('%.1f,%.1f' % (X(r), Y(r[key] - r.get(se_key, 0)))
                      for r in reversed(pts))
        out.append('<polygon class="se" points="%s %s"/>' % (up, dn))

    if len(pts) > 1:
        out.append('<polyline class="line" style="stroke:%s" points="%s"/>'
                   % (color, ' '.join('%.1f,%.1f' % (X(r), Y(r[key])) for r in pts)))

    for r in pts:
        x, y = X(r), Y(r[key])
        cls = 'dot flagged' if (key == 'mean' and r.get('_suspect')) else 'dot'
        out.append('<circle class="%s" cx="%.1f" cy="%.1f" r="5" style="fill:%s"/>'
                   % (cls, x, y, color))
        tip = '%s — %s' % (r['label'], fmt % r[key])
        if key == 'mean':
            tip += ' (n=%d' % r['n']
            if r['se']:
                tip += ', ±%.2f SE' % r['se']
            tip += ')'
        out.append('<circle class="hit" cx="%.1f" cy="%.1f" r="14" data-tip="%s"/>'
                   % (x, y, html.escape(tip, quote=True)))

    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="%s over time">%s</svg>'
            % (W, H, html.escape(key), ''.join(out)))


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


def main():
    rows, excluded = load()
    anchors = fa.load_anchors(ANCHORS)
    series, pre_b = fa.series_for_trend(rows, anchors)
    trend = fa.trend(series)
    fit = make_fit(series, rows[0]['date'])
    base, last = rows[0], rows[-1]
    delta = last['mean'] - base['mean']
    luma_base = base.get('luma')

    for r in rows:
        r['_suspect'] = (luma_base is not None and r.get('luma') is not None
                         and abs(r['luma'] - luma_base) > LUMA_TOL)

    band = (luma_base - LUMA_TOL, luma_base + LUMA_TOL) if luma_base is not None else None
    suspect = [r for r in rows if r['_suspect']]

    tiles = [
        ('Latest in series', '%.2f' % last['mean'], last['label'], ''),
        # §3: pairwise deltas are not interpreted, only the fitted trend. The
        # tile therefore reports the fitted slope and its verdict, not the
        # last-minus-first difference, which is the number most likely to be
        # over-read.
        ('Fitted slope (§3)',
         ('%+.2f' % trend['slope_per_month']) if trend.get('ok') else '—',
         ('yr/month, 95%% CI %+.2f to %+.2f' % trend['slope_ci']
          if trend.get('ok') else 'needs 3+ sessions in the series'),
         ('' if not trend.get('ok') or not trend['detected']
          else ('up' if trend['slope_per_month'] > 0 else 'down'))),
        # "logged" and "in the series" are different counts, and conflating
        # them hid excluded sessions entirely.
        ('Sessions in series', str(len(rows)),
         ('%d photo%s' % (sum(r['n'] for r in rows),
                          '' if sum(r['n'] for r in rows) == 1 else 's'))
         + (' \u00b7 %d excluded' % len(excluded) if excluded else ''), ''),
        ('Result (§3)',
         (trend['verdict'].upper() if trend.get('ok') else 'NOT YET'),
         ('CI excludes zero' if trend.get('ok') and trend['detected']
          else ('CI includes zero' if trend.get('ok')
                else 'not enough sessions')),
         ''),
    ]
    tile_html = ''.join(
        '<div class="tile"><div class="tl">%s</div><div class="tv %s">%s</div>'
        '<div class="ts">%s</div></div>' % (html.escape(t), cls, html.escape(v), html.escape(s))
        for t, v, s, cls in tiles)

    notes = []
    trows = ''.join(
        '<tr%s><td>%s</td><td>%s</td><td class="r">%s</td><td class="r">%.2f</td><td class="r">%s</td>'
        '<td class="r">%d</td><td class="r">%s</td><td class="r">%s</td>'
        '<td class="r">%s</td><td class="note" contenteditable data-session="%s">%s</td>'
        '<td class="rm"><button class="rm" data-session="%s" title="Remove this session from the tracker">remove</button></td></tr>'
        % (' class="sus"' if r['_suspect'] else '', html.escape(r['label']),
           html.escape(r.get('camera') or ''),
           html.escape(r['run_time']), r['mean'],
           ('%.2f' % r['median']) if r['median'] is not None else '—', r['n'],
           ('%.2f' % r['std']) if r['std'] else '—',
           ('%.1f' % r['luma']) if r['luma'] is not None else '—',
           ('%d' % r['flagged']) if r['flagged'] else '0',
           html.escape(r['label'], quote=True), html.escape(r['notes']),
           html.escape(r['label'], quote=True))
        for r in rows)
    # Same eleven columns as the rows above, or the table misaligns.
    trows += ''.join(
        '<tr class="exc"><td>%s</td><td>%s</td><td class="r">%s</td><td class="r">%.2f</td>'
        '<td class="r">—</td><td class="r">%d</td><td class="r">—</td>'
        '<td class="r">%s</td><td class="r" title="%s">excluded</td>'
        '<td class="note" contenteditable data-session="%s">%s</td>'
        '<td class="rm"><button class="rm" data-session="%s" title="Remove this session from the tracker">remove</button></td></tr>'
        % (html.escape(r['label']), html.escape(r.get('camera') or ''),
           html.escape(r['run_time']), r['mean'], r['n'],
           ('%.1f' % r['luma']) if r['luma'] is not None else '—',
           html.escape(r.get('reason') or '', quote=True),
           html.escape(r['label'], quote=True), html.escape(r['notes']),
           html.escape(r['label'], quote=True))
        for r in excluded)

    cameras = sorted({r['camera'] for r in rows + excluded if r.get('camera')})
    if len(cameras) > 1:
        notes.append('Sessions were shot on more than one camera (%s). Numbers from '
                     'different cameras are not comparable: a Mac session and a phone '
                     'session differ by the camera before they differ by the face. '
                     'Keep one camera for the series.'
                     % ', '.join('%s: %s' % (c, ', '.join(r['label'] for r in rows + excluded
                                                            if r.get('camera') == c))
                                 for c in cameras))

    if excluded:
        notes.append('%d session(s) recorded as protocol failures and excluded '
                     'from every statistic on this page (§1): %s. They are shown '
                     'struck through for completeness.'
                     % (len(excluded),
                        '; '.join('%s — %s' % (r['label'], r.get('reason') or 'no reason')
                                  for r in excluded)))
    if trend.get('ok') and not trend['detected']:
        notes.append('The fitted slope\u2019s confidence interval includes zero, so the '
                     'result is "not detected". Not trending, not early signs. '
                     'Pairwise session-to-session deltas are not interpreted (§3).')
    if not anchors.get('B'):
        notes.append('B (baseline) is not set, so every session including rehearsal '
                     'ones is in the fit. Set it with `faceage anchor B YYYY-MM-DD`.')
    elif pre_b:
        notes.append('%d rehearsal session(s) before B are plotted but excluded from '
                     'the fit (§4).' % len(pre_b))
    if len(rows) < 2:
        notes.append('Only one session so far. A trend needs several; treat this as the '
                     'baseline, not a result.')
    if suspect:
        notes.append('Exposure drifted more than %.0f from baseline in %d session(s): %s. '
                     'Lighting alone moves FaceAge by up to ~3.7 years, so treat those '
                     'changes as photographic until reshot.'
                     % (LUMA_TOL, len(suspect), ', '.join(r['label'] for r in suspect)))
    if any(r['failed'] for r in rows):
        notes.append('Some sessions had photos that failed face detection — see the '
                     'per-image CSVs.')
    notes.append('Weekly cadence note: real biological change over one week is ~0. '
                 'Week-to-week differences are mostly measurement noise; read the trend '
                 'across a month or more, not consecutive points.')
    note_html = ''.join('<li>%s</li>' % html.escape(n) for n in notes)

    if not fit:
        fitnote = 'No fit yet — the series needs at least three sessions.'
    else:
        lo, hi = trend['slope_ci']
        fitnote = ('Fit: %+.3f yr/month (95%% CI %+.3f to %+.3f) over %d days, n=%d.'
                   % (trend['slope_per_month'], lo, hi,
                      trend['window_days'], trend['n']))

    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaceAge tracker — __SUBJ__</title>
<style>
:root{color-scheme:light dark}
.viz-root{
  --surface-1:#fcfcfb; --surface-2:#ffffff; --border:#e5e4e0;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78766f;
  --series-1:#2a78d6; --series-2:#eb6834; --grid:#eceae5;
  --band:rgba(12,163,12,.10); --warn:#fab219; --crit:#d03b3b;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
  --surface-1:#1a1a19; --surface-2:#222221; --border:#33322f;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8e8c83;
  --series-1:#3987e5; --series-2:#d95926; --grid:#2b2a28;
  --band:rgba(12,163,12,.16);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.viz-root{background:var(--surface-1);color:var(--text-primary);
  max-width:840px;margin:0 auto;padding:32px 20px 56px}
h1{font-size:20px;margin:0 0 2px;letter-spacing:-.01em}
.subj{display:inline-block;vertical-align:2px;margin-left:6px;padding:2px 8px;border-radius:99px;
  background:var(--surface-2);border:1px solid var(--border);font-size:12px;font-weight:600;
  color:var(--text-secondary);letter-spacing:.02em}
.sub{color:var(--text-secondary);margin:0 0 24px;font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:24px}
.tile{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}
.tv{font-size:26px;font-weight:600;margin:3px 0 1px;font-variant-numeric:tabular-nums}
.tv.up{color:var(--crit)} .tv.down{color:#0ca30c}
.ts{font-size:12px;color:var(--text-secondary)}
.card{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;
  padding:14px 12px 4px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 2px;padding:0 4px;font-weight:600}
.card p{font-size:12px;color:var(--text-secondary);margin:0 0 4px;padding:0 4px}
svg{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}
.line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.se{fill:var(--series-1);opacity:.14}
.fitband{fill:var(--text-secondary);opacity:.13}
.fit{stroke:var(--text-primary);stroke-width:1.6;stroke-dasharray:5 3;opacity:.75}
.anchor{stroke:var(--text-muted);stroke-width:1;stroke-dasharray:2 3;opacity:.8}
.anchorlab{fill:var(--text-muted);font-size:10px;font-weight:700;text-anchor:middle}
tr.exc td{opacity:.55;text-decoration:line-through}
tr.exc td:last-child{text-decoration:none;font-style:italic}
.dot{stroke:var(--surface-2);stroke-width:2}
.dot.flagged{stroke:var(--warn);stroke-width:3}
.hit{fill:transparent;cursor:pointer}
.band{fill:var(--band)}
.ylab{fill:var(--text-muted);font-size:10px;text-anchor:end;font-variant-numeric:tabular-nums}
.xlab{fill:var(--text-muted);font-size:10px;text-anchor:middle}
.empty{color:var(--text-secondary);font-size:13px;padding:0 4px 12px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--border)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);font-weight:600}
td.r,th.r{text-align:right}
tr.sus td:first-child::after{content:" ⚠";color:var(--warn)}
td.note{color:var(--text-secondary);cursor:text;min-width:100px}
td.note:empty::before{content:'add note…';color:var(--text-muted);font-style:italic}
td.note:focus{outline:2px solid var(--series-1);outline-offset:-2px;border-radius:3px}
td.rm{text-align:right;white-space:nowrap}
button.rm{all:unset;cursor:pointer;color:var(--text-muted);font-size:12px}
button.rm:hover{color:var(--warn);text-decoration:underline}
body.file button.rm{display:none}
#save-bar{display:none;position:fixed;bottom:0;left:0;right:0;
  background:var(--series-1);color:#fff;text-align:center;padding:8px;
  font-size:13px;font-weight:600;cursor:pointer;z-index:10}
ul.notes{margin:18px 0 0;padding-left:18px;color:var(--text-secondary);font-size:13px}
ul.notes li{margin-bottom:7px}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;
  background:var(--text-primary);color:var(--surface-1);padding:5px 9px;border-radius:6px;
  font-size:12px;white-space:nowrap;z-index:9;font-variant-numeric:tabular-nums}
</style></head>
<body><div class="viz-root">
<h1>FaceAge tracker <span class="subj">__SUBJ__</span></h1>
<p class="sub">Local summary statistics only — no photographs. Generated __GEN__.</p>
<div class="tiles">__TILES__</div>

<div class="card">
  <h2>FaceAge — session mean</h2>
  <p>Blue band is ±1 standard error per session. Dashed line is the §3 OLS fit
     with its 95% confidence ribbon. Ringed points had an exposure shift.
     __FITNOTE__</p>
  __C1__
</div>

<div class="card">
  <h2>Face exposure — session mean brightness (0–255)</h2>
  <p>Green band is ±__TOL__ of your baseline. Drift outside it can move FaceAge by
     more than real change does.</p>
  __C2__
</div>

<div class="card" style="padding-bottom:10px">
  <h2>All sessions</h2>
  <table><thead><tr><th>Session</th><th>Camera</th><th class="r">Time</th><th class="r">Mean</th><th class="r">Median</th>
  <th class="r">n</th><th class="r">SD</th><th class="r">Exposure</th>
  <th class="r">Flagged</th><th>Notes</th><th></th></tr></thead><tbody>__ROWS__</tbody></table>
</div>

<ul class="notes">__NOTES__</ul>
</div>
<div id="tip"></div>
<div id="save-bar">Save notes</div>
<script>
var tip=document.getElementById('tip');
document.querySelectorAll('.hit').forEach(function(el){
  el.addEventListener('mouseenter',function(e){
    tip.textContent=el.getAttribute('data-tip');tip.style.opacity='1';});
  el.addEventListener('mousemove',function(e){
    tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY-32)+'px';});
  el.addEventListener('mouseleave',function(){tip.style.opacity='0';});
});
/* --- remove a session from the tracker (served by `faceage app` only) --- */
if(!(location.protocol==='http:'||location.protocol==='https:')) document.body.className='file';
document.querySelectorAll('button.rm').forEach(function(b){
  b.addEventListener('click',function(){
    var d=b.getAttribute('data-session');
    if(!confirm('Remove '+d+' from the tracker?\n\nThe row leaves the history and the chart. The photos, '+
                'checklist and result stay on disk, and the removal is logged with your reason, '+
                'so the session can be added back later.')) return;
    var reason=prompt('Why? (kept in the log)','')||'';
    fetch('/api/remove',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({person:'__SUBJ__',date:d,reason:reason})})
      .then(function(r){return r.json().then(function(j){
        if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
        location.reload();});})
      .catch(function(e){ alert('Could not remove: '+e.message); });
  });
});
/* --- editable notes --- */
var dirty=false, bar=document.getElementById('save-bar');
function gatherNotes(){
  var m={};
  document.querySelectorAll('td.note').forEach(function(td){
    m[td.getAttribute('data-session')]=td.textContent.trim();});
  return m;
}
document.querySelectorAll('td.note').forEach(function(td){
  td.addEventListener('input',function(){dirty=true;bar.style.display='block';});
  td.addEventListener('keydown',function(e){
    if(e.key==='Enter'){e.preventDefault();td.blur();}});
});
function done(msg,ms){
  bar.textContent=msg;
  setTimeout(function(){bar.style.display='none';bar.textContent='Save notes';dirty=false;},ms);
}
bar.addEventListener('click',function(){
  var notes=gatherNotes();
  /* Served by `faceage app` there is a server to save to, so save directly.
     Opened as a plain file there is not, and the browser download + sidecar
     merge is the fallback. */
  if(location.protocol==='http:'||location.protocol==='https:'){
    bar.textContent='Saving\u2026';
    fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({person:'__SUBJ__',notes:notes})})
      .then(function(r){return r.json().then(function(j){
        if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
        done('Saved',1400);});})
      .catch(function(e){ done('Could not save: '+e.message,5000); });
    return;
  }
  var blob=new Blob([JSON.stringify(notes,null,2)],{type:'application/json'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='session_notes.json';a.click();URL.revokeObjectURL(a.href);
  done('Saved \u2014 drop session_notes.json next to tracker.html, then regenerate',3500);
});
</script>
</body></html>"""

    page = (page.replace('__GEN__', datetime.datetime.now().strftime('%d %b %Y, %H:%M'))
                .replace('__TILES__', tile_html)
                .replace('__C1__', chart(rows, 'mean', 'var(--series-1)', se_key='se',
                                         fit=fit, anchors=anchors))
                .replace('__FITNOTE__', html.escape(fitnote))
                .replace('__C2__', chart(rows, 'luma', 'var(--series-2)', band=band,
                                         fmt='%.0f', anchors=anchors,
                                         empty_msg='No exposure data yet — sessions scored '
                                                   'before exposure tracking was added.'))
                .replace('__TOL__', '%.0f' % LUMA_TOL)
                .replace('__ROWS__', trows)
                .replace('__NOTES__', note_html)
                .replace('__SUBJ__', html.escape(SUBJECT)))

    with open(OUT, 'w') as fh:
        fh.write(page)
    print(OUT)


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
