#!/usr/bin/env python3
"""Generate a local FaceAge tracker dashboard from the session history CSV.

Reads ~/FaceAgeData/results/faceage_history.csv and writes tracker.html beside it.
Stdlib only, no network, no external assets - the page works offline and nothing
leaves this machine. Contains summary statistics only; no photographs.
"""
import csv, json, os, sys, math, html, datetime

SUBJECT = os.environ.get('FACEAGE_SUBJECT_LABEL', 'me')
RESULTS = os.path.expanduser(os.environ.get(
    'FACEAGE_RESULTS', '~/FaceAgeData/subjects/%s/results' % SUBJECT))
HISTORY = os.path.join(RESULTS, 'faceage_history.csv')
NOTES   = os.path.join(RESULTS, 'session_notes.json')
OUT     = os.path.join(RESULTS, 'tracker.html')

LUMA_TOL = 5.0          # exposure drift beyond this makes a session suspect
W, H     = 760, 210     # chart geometry
PAD_L, PAD_R, PAD_T, PAD_B = 54, 18, 14, 30


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def load():
    if not os.path.exists(HISTORY):
        sys.exit("No history yet at %s — run a session first." % HISTORY)
    rows = []
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
            # parse run timestamp (stored as UTC) to local time
            ts_raw = (r.get('run_timestamp') or '').strip()
            try:
                ts_dt = datetime.datetime.fromisoformat(ts_raw)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=datetime.timezone.utc)
                ts_local = ts_dt.astimezone()
                run_time = ts_local.strftime('%-I:%M %p')
            except (ValueError, TypeError):
                run_time = ''
            rows.append({
                'date': date, 'label': d, 'mean': mean, 'n': n, 'std': std,
                'se': (std / math.sqrt(n)) if (std and n > 1) else 0.0,
                'median': num(r.get('median')),
                'luma': num(r.get('mean_luma')),
                'flagged': int(num(r.get('n_flagged')) or 0),
                'failed': int(num(r.get('n_failed')) or 0),
                'run_time': run_time,
                'notes': (r.get('notes') or '').strip(),
            })
    if not rows:
        sys.exit("History has no usable dated sessions.")
    # merge notes from sidecar (written by the web UI)
    if os.path.exists(NOTES):
        try:
            with open(NOTES) as nf:
                sidecar = json.load(nf)
            merged = False
            for r in rows:
                if r['label'] in sidecar:
                    r['notes'] = sidecar[r['label']]
                    merged = True
            if merged:
                _write_notes_to_csv(sidecar)
                os.remove(NOTES)
        except (json.JSONDecodeError, OSError):
            pass
    return sorted(rows, key=lambda r: r['date'])


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


def chart(rows, key, color, se_key=None, band=None, fmt='%.2f', empty_msg=''):
    """One time-series panel. Single series, so no legend - the card title names it."""
    pts = [r for r in rows if r.get(key) is not None]
    if not pts:
        return '<p class="empty">%s</p>' % html.escape(empty_msg)

    ymin, ymax = scale(pts, key, se_key, band=band)
    d0, d1 = rows[0]['date'], rows[-1]['date']
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


def main():
    rows = load()
    base, last = rows[0], rows[-1]
    delta = last['mean'] - base['mean']
    luma_base = base.get('luma')

    for r in rows:
        r['_suspect'] = (luma_base is not None and r.get('luma') is not None
                         and abs(r['luma'] - luma_base) > LUMA_TOL)

    band = (luma_base - LUMA_TOL, luma_base + LUMA_TOL) if luma_base is not None else None
    suspect = [r for r in rows if r['_suspect']]

    tiles = [
        ('Latest FaceAge', '%.2f' % last['mean'], last['label'], ''),
        ('Change vs baseline',
         ('%+.2f' % delta) if len(rows) > 1 else '—',
         'since %s' % base['label'] if len(rows) > 1 else 'baseline session',
         ('up' if delta > 0 else 'down') if len(rows) > 1 else ''),
        ('Sessions logged', str(len(rows)),
         '%d photo%s total' % (sum(r['n'] for r in rows),
                               '' if sum(r['n'] for r in rows) == 1 else 's'), ''),
        ('This session precision',
         ('±%.2f' % last['se']) if last['se'] else '—',
         'standard error, n=%d' % last['n'], ''),
    ]
    tile_html = ''.join(
        '<div class="tile"><div class="tl">%s</div><div class="tv %s">%s</div>'
        '<div class="ts">%s</div></div>' % (html.escape(t), cls, html.escape(v), html.escape(s))
        for t, v, s, cls in tiles)

    trows = ''.join(
        '<tr%s><td>%s</td><td class="r">%s</td><td class="r">%.2f</td><td class="r">%s</td>'
        '<td class="r">%d</td><td class="r">%s</td><td class="r">%s</td>'
        '<td class="r">%s</td><td class="note" contenteditable data-session="%s">%s</td></tr>'
        % (' class="sus"' if r['_suspect'] else '', html.escape(r['label']),
           html.escape(r['run_time']), r['mean'],
           ('%.2f' % r['median']) if r['median'] is not None else '—', r['n'],
           ('%.2f' % r['std']) if r['std'] else '—',
           ('%.1f' % r['luma']) if r['luma'] is not None else '—',
           ('%d' % r['flagged']) if r['flagged'] else '0',
           html.escape(r['label'], quote=True), html.escape(r['notes']))
        for r in rows)

    notes = []
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
  <p>Shaded band is ±1 standard error. Ringed points had an exposure shift.</p>
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
  <table><thead><tr><th>Session</th><th class="r">Time</th><th class="r">Mean</th><th class="r">Median</th>
  <th class="r">n</th><th class="r">SD</th><th class="r">Exposure</th>
  <th class="r">Flagged</th><th>Notes</th></tr></thead><tbody>__ROWS__</tbody></table>
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
bar.addEventListener('click',function(){
  var notes=gatherNotes();
  var blob=new Blob([JSON.stringify(notes,null,2)],{type:'application/json'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='session_notes.json';a.click();URL.revokeObjectURL(a.href);
  bar.textContent='Saved — drop session_notes.json next to tracker.html, then regenerate';
  setTimeout(function(){bar.style.display='none';bar.textContent='Save notes';dirty=false;},3500);
});
</script>
</body></html>"""

    page = (page.replace('__GEN__', datetime.datetime.now().strftime('%d %b %Y, %H:%M'))
                .replace('__TILES__', tile_html)
                .replace('__C1__', chart(rows, 'mean', 'var(--series-1)', se_key='se'))
                .replace('__C2__', chart(rows, 'luma', 'var(--series-2)', band=band,
                                         fmt='%.0f',
                                         empty_msg='No exposure data yet — sessions scored '
                                                   'before exposure tracking was added.'))
                .replace('__TOL__', '%.0f' % LUMA_TOL)
                .replace('__ROWS__', trows)
                .replace('__NOTES__', note_html)
                .replace('__SUBJ__', html.escape(SUBJECT)))

    with open(OUT, 'w') as fh:
        fh.write(page)
    print(OUT)


def _write_notes_to_csv(notes_dict):
    """Merge a {session_date: note_text} dict into the history CSV."""
    with open(HISTORY, newline='') as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if 'notes' not in fieldnames:
        fieldnames.append('notes')
    for r in rows:
        sd = (r.get('session_date') or '').strip()
        if sd in notes_dict:
            r['notes'] = notes_dict[sd]
    with open(HISTORY, 'w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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
