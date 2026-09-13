#!/usr/bin/env python3
"""Put two or more sessions side by side, frame by frame.

When two sittings shot minutes apart give different numbers, this is how
you find out what actually differed: face size in the frame, brightness,
detector confidence, and the FaceAge of each photo. Reads the per-image
QA the pipeline writes (<date>_per_image.csv) and, when present, the
capture manifest the Mac camera writes (capture.json).

    faceage compare 2026-09-12f 2026-09-12g [2026-09-12h ...]
"""
import argparse
import csv
import json
import os
import statistics
import sys


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_session(results_dir, sessions_dir, name):
    per = os.path.join(results_dir, '%s_per_image.csv' % name)
    if not os.path.exists(per):
        raise SystemExit('no QA data for session %s (expected %s)' % (name, per))
    frames = []
    with open(per) as fh:
        for r in csv.DictReader(fh):
            frames.append({
                'file': r.get('file') or r.get('subj_id') or '?',
                'faceage': _num(r.get('faceage')),
                'fill': _num(r.get('face_fill_height_frac')),
                'luma': _num(r.get('crop_luma_mean')),
                'confidence': _num(r.get('confidence')),
                'flags': [f for f in ((r.get('hard_flags') or '') + ';' + (r.get('advisory_flags') or '')).split(';') if f],
            })
    summary = {}
    sp = os.path.join(results_dir, '%s_summary.json' % name)
    if os.path.exists(sp):
        try:
            summary = json.load(open(sp))
        except Exception:
            summary = {}
    manifest = {}
    mp = os.path.join(sessions_dir, name, 'capture.json') if sessions_dir else None
    if mp and os.path.exists(mp):
        try:
            manifest = json.load(open(mp))
        except Exception:
            manifest = {}
    return {'name': name, 'frames': frames, 'summary': summary, 'manifest': manifest}


def median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def stats(sess):
    fr = [f for f in sess['frames'] if f['faceage'] is not None]
    ages = [f['faceage'] for f in fr]
    out = {
        'n': len(fr),
        'faceage': (sum(ages) / len(ages)) if ages else None,
        'spread': statistics.pstdev(ages) if len(ages) > 1 else None,
        'fill': median(f['fill'] for f in fr),
        'luma': median(f['luma'] for f in fr),
        'confidence': median(f['confidence'] for f in fr),
        'flagged': sum(1 for f in sess['frames'] if f['flags']),
        'camera': None, 'live_fill': None, 'dx': None, 'dy': None,
    }
    m = sess['manifest']
    if m:
        out['camera'] = (m.get('settings') or {}).get('camera')
        frames = m.get('frames') or []
        out['live_fill'] = median(_num(f.get('fill')) for f in frames)
        out['dx'] = median(_num(f.get('dx')) for f in frames)
        out['dy'] = median(_num(f.get('dy')) for f in frames)
    return out


def fmt(v, spec='%.2f', none='   -'):
    return none if v is None else (spec % v)


def render(sessions):
    lines = []
    st = [stats(s) for s in sessions]
    lines.append('')
    lines.append('  %-16s %8s %7s %7s %6s %6s %5s  %s' % ('session', 'FaceAge', 'spread', 'face', 'light', 'conf', 'flags', 'camera'))
    for s, t in zip(sessions, st):
        lines.append('  %-16s %8s %7s %6s%% %6s %6s %5d  %s' % (
            s['name'], fmt(t['faceage'], '%.1f'), fmt(t['spread'], '%.2f'),
            fmt(t['fill'] * 100 if t['fill'] is not None else None, '%.0f'),
            fmt(t['luma'], '%.0f'), fmt(t['confidence'], '%.3f'), t['flagged'], t['camera'] or ''))
    lines.append('')
    lines.append('  face = how much of the crop height the face fills (median of the photos)')
    lines.append('  light = brightness of the face box, 0 to 255 (median); conf = detector confidence (median)')

    # frame by frame, side by side
    width = max(len(s['frames']) for s in sessions)
    lines.append('')
    head = '  %-3s' % '#'
    for s in sessions:
        head += ' | %-12s %5s %5s %5s' % (s['name'][-12:], 'age', 'face', 'light')
    lines.append(head)
    for i in range(width):
        row = '  %-3d' % (i + 1)
        for s in sessions:
            f = s['frames'][i] if i < len(s['frames']) else None
            if f is None:
                row += ' | %-12s %5s %5s %5s ' % ('', '', '', '')
            else:
                row += ' | %-12s %5s %4s%% %5s' % (
                    f['file'][-12:], fmt(f['faceage'], '%.1f', '  -'),
                    fmt(f['fill'] * 100 if f['fill'] is not None else None, '%.0f', '  -'),
                    fmt(f['luma'], '%.0f', '  -'))
                row += '*' if f['flags'] else ' '
        lines.append(row)
    if any(f['flags'] for s in sessions for f in s['frames']):
        lines.append('  * flagged by the pipeline QA')

    # what changed between first and last
    if len(sessions) >= 2:
        a, b = st[0], st[-1]
        lines.append('')
        lines.append('  %s -> %s' % (sessions[0]['name'], sessions[-1]['name']))
        notes = []
        if a['faceage'] is not None and b['faceage'] is not None:
            notes.append('FaceAge moved %+.1f years.' % (b['faceage'] - a['faceage']))
        if a['fill'] is not None and b['fill'] is not None:
            d = (b['fill'] - a['fill']) * 100
            word = 'larger' if d > 0 else 'smaller'
            if abs(d) >= 3:
                notes.append('The face was %.0f points %s in the frame (%.0f%% vs %.0f%%). That is the first thing to hold constant: same distance from the screen every time.' % (abs(d), word, a['fill'] * 100, b['fill'] * 100))
            else:
                notes.append('Face size was the same (%.0f%% vs %.0f%%).' % (a['fill'] * 100, b['fill'] * 100))
        if a['luma'] is not None and b['luma'] is not None:
            d = b['luma'] - a['luma']
            if abs(d) >= 3:
                notes.append('Brightness differed by %+.0f (%.0f vs %.0f).' % (d, a['luma'], b['luma']))
            else:
                notes.append('Brightness was the same (%.0f vs %.0f).' % (a['luma'], b['luma']))
        if a['confidence'] is not None and b['confidence'] is not None:
            d = b['confidence'] - a['confidence']
            if abs(d) >= 0.01:
                notes.append('Detector confidence changed by %+.3f; a lower value usually means glasses, a turned head, or hair over the face.' % d)
        if a['dy'] is not None and b['dy'] is not None and abs(b['dy'] - a['dy']) >= 0.02:
            notes.append('The face sat %s in the frame (vertical offset %+.2f vs %+.2f).' % ('higher' if b['dy'] < a['dy'] else 'lower', a['dy'], b['dy']))
        same = [n for n in notes if 'was the same' in n]
        if len(same) >= 2 and notes and 'FaceAge moved' in notes[0]:
            notes.append('Framing and light did not move, so the difference is in the face itself: expression, head tilt, glasses, or how fresh you look. Neutral face, chin level, same glasses choice every time.')
        for n in notes:
            lines.append('  ' + n)
    if len(sessions) >= 3:
        ages = [t['faceage'] for t in st if t['faceage'] is not None]
        if len(ages) == len(st):
            steps = [ages[i + 1] - ages[i] for i in range(len(ages) - 1)]
            if all(s < 0 for s in steps) or all(s > 0 for s in steps):
                lines.append('  Every sitting moved the same direction (%s). That is drift, not noise: something changes as you sit longer. Likely candidates are a settling expression, posture, or the camera warming its exposure.' % ', '.join('%.1f' % x for x in ages))
    lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('sessions', nargs='+', help='two or more session names, e.g. 2026-09-12f 2026-09-12g')
    p.add_argument('--results', required=True, help='results folder holding <name>_per_image.csv')
    p.add_argument('--sessions-dir', default=None, help='sessions folder holding <name>/capture.json')
    a = p.parse_args(argv)
    if len(a.sessions) < 2:
        p.error('give at least two sessions to compare')
    sessions = [load_session(a.results, a.sessions_dir, n) for n in a.sessions]
    print(render(sessions))
    return 0


if __name__ == '__main__':
    sys.exit(main())
