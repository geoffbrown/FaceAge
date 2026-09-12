#!/usr/bin/env python3
"""FaceAge — local web UI.

    faceage app          then open http://127.0.0.1:7860

Import -> pre-flight -> checklist -> score -> chart, in one place. Stdlib only,
no dependencies, no network calls. Scoring shells out to `tools/faceage`, so
there is one pipeline and this is a front end on it.

THREE THINGS THIS ENFORCES, not merely offers

1. Binds 127.0.0.1 only. The sessions folder holds photographs of people's
   faces. Nothing here should ever be reachable from another machine, so the
   bind address is not configurable.

2. The validity checklist is answered BEFORE the score is requested, and the
   answers are written before the pipeline is invoked (docs/PREREGISTRATION.md
   §1). A session invalidated after its number is known is not an exclusion, it
   is a result being discarded for being inconvenient. The server refuses to
   score a tracked session with no checklist on file.

3. Nothing modifies a photograph. Import copies; HEIC conversion is a format
   change with frozen settings. When pre-flight fails, the remedy offered is
   always the rig, never the file.

PEOPLE, NOT ACCOUNTS. There is no auth here because there is nothing to
authenticate against - a "person" is a folder under $FACEAGE_DATA/subjects/.
Each has an independent series, exposure baseline and tracker. There is
deliberately no screen that shows two people's numbers together: different
cameras and rooms move FaceAge by more than any real difference between two
similar-aged people would (docs/MAC_APP.md).
"""
from __future__ import print_function

import os
import re
import csv
import sys
import json
import html
import time
import shutil
import threading
import subprocess
import datetime
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.expanduser(os.environ.get('FACEAGE_DATA', '~/FaceAgeData'))
INBOX = os.path.expanduser(os.environ.get('FACEAGE_INBOX', '~/Downloads'))
FACEAGE = os.path.join(HERE, 'faceage')
PORT = int(os.environ.get('FACEAGE_PORT', '7860'))
HOST = '127.0.0.1'          # not configurable, deliberately

NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
# A session label is a date, optionally with a take letter: 2026-09-12b.
# A reshoot is a NEW take, not an overwrite -- see next_take().
DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})([b-z])?$')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.heic', '.heif')

sys.path.insert(0, HERE)
import faceage_preflight as pf          # noqa: E402
import faceage_analysis as fa            # noqa: E402

# The §1 pre-committed validity conditions, phrased as things to confirm. Each
# is a condition under which the session does NOT count; leaving one unconfirmed
# records a protocol failure with that reason.
CHECKLIST = [
    ('grooming', 'Makeup, tinted product and grooming match baseline'),
    ('light', 'Frontal light from the fixed rig only — nothing overhead'),
    ('camera', 'Same camera, propped at the same distance and height (never hand-held)'),
    ('pose', 'Neutral expression, mouth closed, head level'),
    ('photoday', 'Photo-day controls met (alcohol 48h, sodium, 7h+ sleep, '
                 'wake interval, no hot shower/sauna/hard training within 2h, '
                 'no facial treatment within 2 weeks)'),
    ('skin', 'No sunburn, allergy flare, illness, or visible blemish in an '
             'attention region'),
]


# ----------------------------------------------------------------------------
# paths and state
# ----------------------------------------------------------------------------

def safe_subject(name):
    if not name or not NAME_RE.match(name):
        raise ValueError('invalid person name (letters, digits, dash, underscore)')
    return name


def safe_date(d):
    m = DATE_RE.match(d or '')
    if not m:
        raise ValueError('invalid session (YYYY-MM-DD, optionally with a take '
                         'letter like 2026-09-12b)')
    try:
        datetime.date.fromisoformat(m.group(1))
    except ValueError:
        raise ValueError('invalid session date')
    return d


def next_take(name, date):
    """The next unused take letter for this calendar day.

    A reshoot is its own session: its own photos, its own checklist, its own
    score. The take that failed keeps its recorded failure and stays excluded,
    which is what §1 asks for -- nothing is rewritten after the fact. The
    analysis keys sessions by date[:10], so takes sit on the same day.
    """
    day = DATE_RE.match(safe_date(date)).group(1)
    for suffix in [''] + [chr(c) for c in range(ord('b'), ord('z') + 1)]:
        label = day + suffix
        if not os.path.isdir(session_dir(name, label)) \
                and not session_scored(name, label) \
                and read_checklist(name, label) is None:
            return label
    raise ValueError('no take letters left for %s' % day)


def subj_dir(name):
    return os.path.join(DATA, 'subjects', safe_subject(name))


def results_dir(name):
    return os.path.join(subj_dir(name), 'results')


def session_dir(name, date):
    return os.path.join(subj_dir(name), 'sessions', safe_date(date))


def list_people():
    base = os.path.join(DATA, 'subjects')
    if not os.path.isdir(base):
        return []
    out = []
    for n in sorted(os.listdir(base)):
        if not NAME_RE.match(n) or not os.path.isdir(os.path.join(base, n)):
            continue
        hist = os.path.join(base, n, 'results', 'faceage_history.csv')
        rows = 0
        if os.path.exists(hist):
            with open(hist) as fh:
                rows = max(sum(1 for _ in fh) - 1, 0)
        sess = os.path.join(base, n, 'sessions')
        nsess = len([d for d in os.listdir(sess)
                     if os.path.isdir(os.path.join(sess, d))]) \
            if os.path.isdir(sess) else 0
        out.append({'name': n, 'sessions': nsess, 'logged': rows})
    return out


def baseline_info(name, source=None):
    """The exposure baseline: the FIRST logged session's mean face-crop
    brightness (the pipeline's crop_luma_mean, averaged over the session).
    Every later session is held to it within +/-5.

    With a source, the first logged session shot on that camera. The number
    is only meaningful against the same instrument: a phone baseline says
    nothing about how bright the Mac's camera should read.
    """
    hist = os.path.join(results_dir(name), 'faceage_history.csv')
    if not os.path.exists(hist):
        return None
    with open(hist) as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: (r.get('session_date') or '').strip())
    for r in rows:
        v = (r.get('mean_luma') or '').strip()
        d = (r.get('session_date') or '').strip()
        if not v or not d:
            continue
        try:
            luma = float(v)
        except ValueError:
            continue
        src = session_source(name, d)
        if source and src != source:
            continue
        return {'luma': luma, 'date': d, 'source': src}
    return None


def baseline_luma(name, source=None):
    b = baseline_info(name, source)
    return b['luma'] if b else None


HOME = os.path.expanduser('~')


def safe_dir(path):
    """Resolve a browse path, refusing anything outside the user's own files.

    The server runs as you on your own machine, so reading your filesystem is
    within its remit — but browsing is driven from a web page, so the reachable
    area is bounded to $HOME and /Volumes (external drives). Symlinks are
    resolved first, so a link out of $HOME does not widen it.
    """
    if not path:
        return INBOX
    p = os.path.realpath(os.path.expanduser(path))
    # $HOME and /Volumes cover the real cases (your files, external drives).
    # The configured inbox and data roots are added because they are allowed to
    # live anywhere -- FACEAGE_INBOX is explicitly settable.
    roots = [os.path.realpath(HOME), '/Volumes',
             os.path.realpath(INBOX), os.path.realpath(DATA)]
    if not any(p == r or p.startswith(r + os.sep) for r in roots):
        raise ValueError('folder must be inside your home folder or /Volumes')
    if not os.path.isdir(p):
        raise ValueError('not a folder: %s' % p)
    return p


def list_dir(path):
    """Subfolders and images in one folder, for the picker."""
    d = safe_dir(path)
    dirs, images = [], []
    try:
        entries = os.listdir(d)
    except PermissionError:
        raise ValueError('no permission to read %s' % d)
    for f in sorted(entries):
        if f.startswith('.'):
            continue
        full = os.path.join(d, f)
        if os.path.isdir(full):
            dirs.append(f)
        elif f.lower().endswith(IMAGE_EXTS) and os.path.isfile(full):
            st = os.stat(full)
            images.append({'file': f, 'size': st.st_size, 'mtime': st.st_mtime,
                           'when': time.strftime('%d %b %H:%M',
                                                 time.localtime(st.st_mtime)),
                           'heic': f.lower().endswith(('.heic', '.heif'))})
    images.sort(key=lambda r: -r['mtime'])
    parent = os.path.dirname(d)
    try:
        safe_dir(parent)
    except ValueError:
        parent = None
    return {'path': d, 'parent': parent, 'dirs': dirs[:200],
            'images': images[:400], 'n_images': len(images)}


def scan_inbox(limit=60):
    """Recent images in the inbox folder — where AirDrop lands them."""
    if not os.path.isdir(INBOX):
        return []
    out = []
    for f in os.listdir(INBOX):
        if f.startswith('.') or not f.lower().endswith(IMAGE_EXTS):
            continue
        p = os.path.join(INBOX, f)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        out.append({'file': f, 'size': st.st_size, 'mtime': st.st_mtime,
                    'when': time.strftime('%d %b %H:%M', time.localtime(st.st_mtime)),
                    'heic': f.lower().endswith(('.heic', '.heif'))})
    out.sort(key=lambda r: -r['mtime'])
    return out[:limit]


def staged(name, date):
    d = session_dir(name, date)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if not f.startswith('.') and f.lower().endswith(IMAGE_EXTS))


def checklist_path(name, date):
    return os.path.join(results_dir(name), 'checklists', '%s.json' % safe_date(date))


def read_checklist(name, date):
    p = checklist_path(name, date)
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return json.load(fh)


def checklist_stale(name, date):
    """True if photos were staged after the checklist was recorded.

    Not a reason to clear the checklist -- clearing it on import would create a
    route to re-answer §1 after seeing the number, which is the one thing the
    ordering exists to prevent. It is a reason to say so on screen.
    """
    cl = read_checklist(name, date)
    if not cl:
        return False
    try:
        rec = datetime.datetime.fromisoformat(cl['recorded_at']).timestamp()
    except (ValueError, KeyError, TypeError):
        return False
    d = session_dir(name, date)
    if not os.path.isdir(d):
        return False
    for f in staged(name, date):
        if os.stat(os.path.join(d, f)).st_mtime > rec + 1:
            return True
    return False


def session_result(name, date):
    """The session's own number, read from the summary the pipeline writes.

    Deliberately no comparison against the previous session: §3 is explicit
    that pairwise deltas are not interpreted, only the fitted trend. Exposure IS
    compared against the baseline, because that is a capture check rather than a
    result.
    """
    p = os.path.join(results_dir(name), '%s_summary.json' % safe_date(date))
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            d = json.load(fh)
    except (ValueError, OSError):
        return None

    base = baseline_luma(name, session_source(name, date))
    luma = d.get('luma')
    out = {'mean': d.get('mean'), 'median': d.get('median'), 'std': d.get('std'),
           'n': d.get('n'), 'n_total': d.get('n_total_images'),
           'n_failed': d.get('n_failed'), 'n_flagged': d.get('n_flagged'),
           'min': d.get('min'), 'max': d.get('max'), 'luma': luma,
           'baseline_luma': base,
           'fellback': bool(d.get('fellback_to_flagged'))}
    if luma is not None and base is not None:
        out['luma_delta'] = round(luma - base, 1)
        out['luma_ok'] = abs(luma - base) <= 5.0
    cl = read_checklist(name, date)
    out['valid'] = bool(cl is None or cl.get('valid', True))
    out['excluded_reason'] = None if out['valid'] else '; '.join(cl.get('failed') or [])
    # Scored but no history row: a one-off. The number exists and can still be
    # added, which is the point -- deciding before seeing it was a one-way door.
    out['logged'] = session_scored(name, date)
    out['in_series'] = out['logged'] and out['valid']
    out['has_checklist'] = cl is not None
    return out


def session_scored(name, date):
    hist = os.path.join(results_dir(name), 'faceage_history.csv')
    if not os.path.exists(hist):
        return False
    with open(hist) as fh:
        return any((r.get('session_date') or '').strip() == date
                   for r in csv.DictReader(fh))


# ----------------------------------------------------------------------------
# the one long-running job (scoring)
# ----------------------------------------------------------------------------

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# The pipeline prints "(3/10) Running the face localization step for ..." and
# "(2/9) Running the age estimation step for ...". Parsing its own output beats
# inventing a second source of truth for progress.
PROG_RE = re.compile(r'\((\d+)/(\d+)\)')


def parse_progress(lines):
    """Phase, position and percent from the tail of the log."""
    phase, done, total = None, 0, 0
    for line in lines:
        low = line.lower()
        if 'localization step' in low:
            phase = 'Finding faces'
        elif 'estimation step' in low:
            phase = 'Estimating age'
        elif 'converting' in low and 'heic' in low:
            phase = 'Converting HEIC'
        elif 'scoring' in low and 'photo' in low:
            phase = phase or 'Starting'
        m = PROG_RE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
    pct = (100.0 * done / total) if total else 0.0
    return {'phase': phase, 'done': done, 'total': total, 'pct': round(pct, 1)}


def estimate_remaining(progress, elapsed, phase_started):
    """Seconds left, from the rate of the CURRENT phase only.

    Phases run at very different speeds -- face localization dominates -- so
    extrapolating from the whole run would be wrong every time the phase
    changes. Needs two completed items before it will guess at all.
    """
    done, total = progress.get('done') or 0, progress.get('total') or 0
    if done < 2 or total <= done or phase_started is None:
        return None
    spent = elapsed - phase_started
    if spent <= 0:
        return None
    return int(round((spent / done) * (total - done)))


class Job(object):
    """Runs one pipeline invocation and collects its output.

    The CLI colours its output for a terminal; rendered in HTML those escapes
    show up as literal [36m noise. They are stripped on the way in.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.label = ''
        self.log = []
        self.rc = None
        self.started = None
        self._phase = None
        self._phase_at = None

    def snapshot(self):
        with self.lock:
            elapsed = (time.time() - self.started) if self.started else 0
            prog = parse_progress(self.log[-60:])
            if prog['phase'] != self._phase:
                self._phase, self._phase_at = prog['phase'], elapsed
            return {'running': self.running, 'label': self.label,
                    'rc': self.rc, 'log': self.log[-400:],
                    'progress': prog,
                    'eta': estimate_remaining(prog, elapsed, self._phase_at),
                    'elapsed': elapsed}

    def clear(self):
        """Forget the last run. Its output describes a session that has just
        been discarded, so leaving it on screen reads as 'nothing happened'."""
        with self.lock:
            if self.running:
                return False
            self.label, self.log, self.rc = '', [], None
            self.started = self._phase = self._phase_at = None
            return True

    def start(self, label, argv, cwd=None):
        with self.lock:
            if self.running:
                raise RuntimeError('a job is already running')
            self.running, self.label, self.log, self.rc = True, label, [], None
            self.started = time.time()
            self._phase, self._phase_at = None, None

        def run():
            try:
                p = subprocess.Popen(argv, cwd=cwd or REPO, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=False,
                                     bufsize=0, env=dict(os.environ,
                                                         FACEAGE_REPO=REPO,
                                                         FACEAGE_DATA=DATA))
                # The pipeline prints per-image progress with end='\r', so
                # iterating lines would block until the phase ended and the
                # progress would never be seen. Read raw and split on BOTH
                # terminators, treating \r the way a terminal does: it
                # overwrites the line rather than adding one.
                fd = p.stdout.fileno()
                buf, last_was_cr = '', False
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    buf += data.decode('utf-8', 'replace')
                    while True:
                        m = re.search(r'[\r\n]', buf)
                        if not m:
                            break
                        line = ANSI_RE.sub('', buf[:m.start()])
                        sep = buf[m.start()]
                        buf = buf[m.end():]
                        with self.lock:
                            if line.strip():
                                if last_was_cr and self.log:
                                    self.log[-1] = line
                                else:
                                    self.log.append(line)
                            last_was_cr = (sep == '\r')
                if buf.strip():
                    with self.lock:
                        self.log.append(ANSI_RE.sub('', buf))
                p.wait()
                rc = p.returncode
            except Exception as exc:                      # noqa: BLE001
                with self.lock:
                    self.log.append('error: %s' % exc)
                rc = 1
            with self.lock:
                self.running, self.rc = False, rc

        threading.Thread(target=run, daemon=True).start()


JOB = Job()


# ----------------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------------

def do_create_person(body):
    name = safe_subject((body.get('name') or '').strip())
    d = subj_dir(name)
    if os.path.isdir(d):
        raise ValueError('%s already exists' % name)
    os.makedirs(os.path.join(d, 'sessions'))
    os.makedirs(os.path.join(d, 'results'))
    return {'ok': True, 'name': name}


def do_rename(body):
    """Rename a person. The folder IS the identity, so this is one move."""
    old = safe_subject(body.get('old'))
    new = safe_subject((body.get('new') or '').strip())
    if old == new:
        return {'ok': True, 'name': new}
    if os.path.isdir(subj_dir(new)):
        raise ValueError('%s already exists' % new)
    if not os.path.isdir(subj_dir(old)):
        raise ValueError('%s does not exist' % old)
    os.rename(subj_dir(old), subj_dir(new))
    build_chart(new)          # the tracker badge comes from the folder name
    return {'ok': True, 'name': new}


def do_import(body):
    """Copy selected inbox files into the session folder. Copy, never move --
    the original stays where it was until the person deletes it themselves."""
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    files = body.get('files') or []
    src_dir = safe_dir(body.get('dir') or INBOX)
    if not files:
        raise ValueError('no photos selected')

    # A scored session is closed. Adding photos to one leaves a confusing
    # half-state -- a checklist describing an earlier capture, a pre-flight
    # describing earlier photos, a number already in the series. Open the next
    # take instead, which resets all three by construction.
    moved_to = None
    if session_scored(name, date):
        date = next_take(name, date)
        moved_to = date

    dest = session_dir(name, date)
    os.makedirs(dest, exist_ok=True)
    copied, skipped = [], []
    for f in files:
        if os.path.basename(f) != f or f.startswith('.'):
            raise ValueError('bad filename: %s' % f)
        if not f.lower().endswith(IMAGE_EXTS):
            raise ValueError('not an image: %s' % f)
        src = os.path.join(src_dir, f)
        if not os.path.isfile(src):
            skipped.append(f)
            continue
        target = os.path.join(dest, f)
        if os.path.exists(target):
            skipped.append(f)
            continue
        shutil.copy2(src, target)
        copied.append(f)
    return {'ok': True, 'copied': copied, 'skipped': skipped,
            'session': date, 'moved_to_new_take': moved_to,
            'staged': staged(name, date)}


CAPTURE_MANIFEST = 'capture.json'
BATCH_RE = re.compile(r'^\d{8}-\d{6}$')
MAX_CAPTURE_BYTES = 12 << 20        # one 4K JPEG is well under this


def capture_manifest(name, date):
    """How this session's photos were captured, if the app captured them.

    Absent for imported photos. Present, with source 'mac-camera', when the
    frames came from the Mac's own camera via /api/capture.
    """
    p = os.path.join(session_dir(name, date), CAPTURE_MANIFEST)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def session_source(name, date):
    """'mac-camera', 'import', or None when the session has no photos."""
    m = capture_manifest(name, date)
    if m and m.get('source'):
        return m['source']
    return 'import' if staged(name, date) else None


def series_source(name):
    """The camera behind the most recently logged session, so a session shot
    on a different camera can be warned about before it joins the series.
    A trend line that mixes two cameras compares two instruments, not two
    dates."""
    hist = os.path.join(results_dir(name), 'faceage_history.csv')
    if not os.path.exists(hist):
        return None
    with open(hist) as fh:
        dates = [(r.get('session_date') or '').strip() for r in csv.DictReader(fh)]
    dates = [d for d in dates if d]
    if not dates:
        return None
    return session_source(name, max(dates))


def do_capture(body):
    """Save one frame from the Mac's camera into the session folder.

    The browser captures the frame (it is the only thing that can reach the
    camera); this end only writes bytes. Frames arrive one at a time so the
    ten-shot sequence survives a slow write, and the first frame of a batch
    opens a new take when the session is already closed, exactly as import
    does. Nothing here touches the tracker.
    """
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    batch = str(body.get('batch') or '')
    if not BATCH_RE.match(batch):
        raise ValueError('bad batch stamp')
    try:
        index = int(body.get('index'))
    except (TypeError, ValueError):
        raise ValueError('bad frame index')
    if not 1 <= index <= 99:
        raise ValueError('bad frame index')

    data = body.get('image') or ''
    if ',' in data:                         # data:image/jpeg;base64,....
        head, data = data.split(',', 1)
        if not head.startswith('data:image/jpeg'):
            raise ValueError('frame must be a JPEG')
    import base64
    import binascii
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('frame is not valid base64')
    if raw[:3] != b'\xff\xd8\xff':
        raise ValueError('frame is not a JPEG')
    if len(raw) > MAX_CAPTURE_BYTES:
        raise ValueError('frame too large')

    moved_to = None
    if session_scored(name, date):
        date = next_take(name, date)
        moved_to = date
    dest = session_dir(name, date)
    os.makedirs(dest, exist_ok=True)

    fname = 'cam_%s_%02d.jpg' % (batch, index)
    target = os.path.join(dest, fname)
    if os.path.exists(target):
        raise ValueError('%s already exists' % fname)
    tmp = target + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(raw)
    os.replace(tmp, target)

    # One manifest per session, appended per frame, so a sequence that stops
    # halfway still records what was saved and how.
    settings = body.get('settings') if isinstance(body.get('settings'), dict) else {}
    keep = {k: settings.get(k) for k in ('camera', 'width', 'height', 'frame_rate',
                                          'device_id', 'mirrored', 'quality', 'crop',
                                          'saved_width', 'saved_height', 'guide',
                                          'user_agent') if settings.get(k) is not None}
    frame = body.get('frame') if isinstance(body.get('frame'), dict) else {}
    doc = capture_manifest(name, date) or {
        'source': 'mac-camera', 'session_date': date, 'person': name,
        'frames': []}
    doc['settings'] = keep or doc.get('settings') or {}
    doc['frames'].append({
        'file': fname, 'batch': batch, 'index': index, 'bytes': len(raw),
        'captured_at': datetime.datetime.now().replace(microsecond=0).isoformat(),
        'luma': frame.get('luma', settings.get('luma')),
        'fill': frame.get('fill'), 'dx': frame.get('dx'), 'dy': frame.get('dy'),
        'aligned': frame.get('aligned')})
    mp = os.path.join(dest, CAPTURE_MANIFEST)
    with open(mp + '.tmp', 'w') as fh:
        json.dump(doc, fh, indent=2)
    os.replace(mp + '.tmp', mp)

    return {'ok': True, 'file': fname, 'session': date,
            'moved_to_new_take': moved_to, 'staged': staged(name, date)}


def do_checklist(body):
    """Record the §1 validity answers. Written BEFORE scoring, always."""
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    answers = body.get('answers') or {}
    notes = (body.get('notes') or '').strip()

    if session_scored(name, date):
        raise ValueError(
            'session %s has already been scored. Recording validity now would '
            'be deciding after seeing the number (§1). Use the CLI with '
            '--force if the failure was genuinely identified beforehand.' % date)

    # Re-answering BEFORE a score exists is just correcting your own answer --
    # no number has been seen, so nothing can be motivated by it. Every version
    # is kept in the JSON with its timestamp, so the record stays auditable.
    prior = read_checklist(name, date)

    failed = [label for key, label in CHECKLIST if not answers.get(key)]
    doc = {'session_date': date, 'person': name,
           'recorded_at': datetime.datetime.now().replace(microsecond=0).isoformat(),
           'answers': {k: bool(answers.get(k)) for k, _ in CHECKLIST},
           'failed': failed, 'notes': notes,
           'valid': not failed}
    if prior:
        doc['superseded'] = (prior.get('superseded') or []) + [
            {k: prior.get(k) for k in ('recorded_at', 'answers', 'failed', 'notes')}]

    p = checklist_path(name, date)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as fh:
        json.dump(doc, fh, indent=2)

    # A failed checklist is a protocol failure, recorded in the same file the
    # analysis reads, with its reason, before any number exists.
    if failed:
        vfile = os.path.join(results_dir(name), 'session_validity.csv')
        existing = set()
        if os.path.exists(vfile):
            with open(vfile) as fh:
                existing = {(r.get('session_date') or '').strip()
                            for r in csv.DictReader(fh)}
        else:
            with open(vfile, 'w') as fh:
                fh.write('session_date,valid,reason,recorded_at\n')
        if date not in existing:
            reason = '; '.join(failed)[:300].replace(',', ';')
            with open(vfile, 'a') as fh:
                fh.write('%s,no,%s,%s\n' % (date, reason, doc['recorded_at']))
    else:
        # Corrected to all-pass before any score existed: drop the failure row
        # so the session is not excluded for an answer that was withdrawn
        # before it could have been influenced. Only reachable while unscored,
        # since scoring bars this whole endpoint.
        vfile = os.path.join(results_dir(name), 'session_validity.csv')
        if os.path.exists(vfile):
            with open(vfile) as fh:
                rows = list(csv.DictReader(fh))
                cols = list(rows[0].keys()) if rows else []
            keep = [r for r in rows
                    if (r.get('session_date') or '').strip() != date]
            if cols and len(keep) != len(rows):
                tmp = vfile + '.tmp'
                with open(tmp, 'w', newline='') as fh:
                    w = csv.DictWriter(fh, fieldnames=cols)
                    w.writeheader()
                    w.writerows(keep)
                os.replace(tmp, vfile)
    return {'ok': True, 'checklist': doc}


def do_reshoot(body):
    """Open a fresh take for the same day."""
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    label = next_take(name, date)
    os.makedirs(session_dir(name, label), exist_ok=True)
    return {'ok': True, 'session': label}


def previous_checklist(name, date):
    """The most recent checklist before this session, for pre-filling.

    Pre-filled boxes are shown ticked and still have to be submitted, so it is
    a starting point to review rather than an answer given on your behalf.
    """
    d = os.path.join(results_dir(name), 'checklists')
    if not os.path.isdir(d):
        return None
    names = sorted(f[:-5] for f in os.listdir(d) if f.endswith('.json'))
    earlier = [n for n in names if n < date]
    if not earlier:
        return None
    return read_checklist(name, earlier[-1])


def do_discard(body):
    """Throw a session away completely and start over.

    Deleting the photos alone left every trace behind -- the summary, the
    per-image QA, the checklist, the validity row and the history row all key
    off the session label, not the folder. This removes all of them.

    Photos and records are MOVED to discarded/, never deleted: docs/MAC_APP.md
    keeps photographs because a weird session needs inspecting later, and the
    discard itself is logged so the record shows what happened.

    On the §1 question: a rehearsal session is not study data. §4 says these do
    not enter the series at all, and before B is set there is no series for a
    discard to bias. Once B exists, discarding a SCORED session is a real
    deletion of study data -- it is still allowed, because refusing would just
    move the decision somewhere unrecorded, but it is logged with its reason and
    flagged in the UI.
    """
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    reason = (body.get('reason') or '').strip()[:300].replace(',', ';')

    res = results_dir(name)
    was_scored = session_scored(name, date)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    bin_dir = os.path.join(subj_dir(name), 'discarded', '%s_%s' % (date, stamp))
    os.makedirs(bin_dir, exist_ok=True)

    moved = []
    sd = session_dir(name, date)
    if os.path.isdir(sd):
        shutil.move(sd, os.path.join(bin_dir, 'photos'))
        moved.append('photos')
    for fname in ('%s_summary.json' % date, '%s_per_image.csv' % date):
        src = os.path.join(res, fname)
        if os.path.exists(src):
            shutil.move(src, os.path.join(bin_dir, fname))
            moved.append(fname)
    cl = checklist_path(name, date)
    if os.path.exists(cl):
        shutil.move(cl, os.path.join(bin_dir, 'checklist.json'))
        moved.append('checklist')

    # drop the rows keyed to this label
    for path, key in ((os.path.join(res, 'faceage_history.csv'), 'session_date'),
                      (os.path.join(res, 'session_validity.csv'), 'session_date')):
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
            cols = list(rows[0].keys()) if rows else []
        keep = [r for r in rows if (r.get(key) or '').strip() != date]
        if cols and len(keep) != len(rows):
            tmp = path + '.tmp'
            with open(tmp, 'w', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(keep)
            os.replace(tmp, path)
            moved.append(os.path.basename(path) + ' row')

    JOB.clear()          # its log described the session just removed

    log = os.path.join(res, 'discarded.csv')
    new = not os.path.exists(log)
    os.makedirs(res, exist_ok=True)
    with open(log, 'a', newline='') as fh:
        if new:
            fh.write('session,discarded_at,was_scored,reason,archived_to\n')
        fh.write('%s,%s,%s,%s,%s\n' % (date,
                                        datetime.datetime.now().replace(microsecond=0).isoformat(),
                                        'yes' if was_scored else 'no',
                                        reason or '(none given)',
                                        os.path.basename(bin_dir)))
    return {'ok': True, 'session': date, 'was_scored': was_scored,
            'removed': moved, 'archived_to': bin_dir}


def do_remove(body):
    """Take a session's row out of the tracker, and nothing else.

    The photos, checklist, summary and per-image QA stay where they are, so
    the session reads as "scored, not in the tracker" in the app and can be
    added back. Only the history row goes, and the removal is logged with its
    reason in removed.csv.

    Same stance as discard on the pre-registration: before B there is no
    series to bias. After B this is dropping study data after seeing it, which
    is allowed here rather than pushed somewhere unrecorded, but it is logged
    and the UI says so. Marking the session invalid with a reason keeps the
    row and is the cleaner option for a genuine protocol failure.
    """
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    reason = (body.get('reason') or '').strip()[:300].replace(',', ';')
    res = results_dir(name)
    hist = os.path.join(res, 'faceage_history.csv')
    if not session_scored(name, date):
        raise ValueError('%s is not in the tracker' % date)
    with open(hist) as fh:
        rows = list(csv.DictReader(fh))
        cols = list(rows[0].keys()) if rows else []
    gone = [r for r in rows if (r.get('session_date') or '').strip() == date]
    keep = [r for r in rows if (r.get('session_date') or '').strip() != date]
    tmp = hist + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(keep)
    os.replace(tmp, hist)

    b = fa_anchors(name).get('B')
    after_b = bool(b) and datetime.date.fromisoformat(date[:10]) >= b
    log = os.path.join(res, 'removed.csv')
    new = not os.path.exists(log)
    with open(log, 'a', newline='') as fh:
        if new:
            fh.write('session,removed_at,mean,after_baseline_anchor,reason\n')
        fh.write('%s,%s,%s,%s,%s\n' % (
            date, datetime.datetime.now().replace(microsecond=0).isoformat(),
            (gone[0].get('mean') or '') if gone else '',
            'yes' if after_b else 'no', reason or '(none given)'))
    build_chart(name)
    return {'ok': True, 'session': date, 'after_baseline_anchor': after_b,
            'remaining': len(keep)}


def do_score(body):
    """Analyse the staged photos. Never writes to the tracker.

    Scoring is a dry run by design: the number and the diagnosis come back,
    and whether the session goes into the tracker is a separate, explicit
    decision made on the result screen (do_add). That removes the old
    one-off checkbox, which forced the choice before the number existed and
    could not be undone in the useful direction.
    """
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    if not staged(name, date):
        raise ValueError('no photos in this session yet')
    if read_checklist(name, date) is None:
        raise ValueError('confirm the shooting conditions first')
    if session_scored(name, date):
        raise ValueError('this session is already in your tracker')
    argv = [FACEAGE, 'run', date, '--subject', name, '--no-log']
    JOB.start('Analysing %s — %s' % (name, date), argv)
    return {'ok': True, 'started': True}


HISTORY_COLS = ['session_date', 'run_timestamp', 'n', 'n_total_images', 'n_failed',
                'n_flagged', 'mean', 'median', 'std', 'min', 'max', 'mean_luma',
                'model_sha256', 'image_dir', 'notes']


def model_sha_short():
    """First 16 hex chars of the weights' SHA-256, as the pipeline records it."""
    path = os.path.join(REPO, 'models', 'faceage_model.h5')
    if not os.path.exists(path):
        return ''
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


def do_add(body):
    """Put a scored session into the tracker.

    Writes the history row from the summary the pipeline already produced,
    with the same columns and rounding as src/faceage_run.py append_history,
    so a row added here is indistinguishable from one the pipeline wrote --
    except that promoted.csv records that it was added after the number was
    known. Allowed, but not silent.
    """
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    res = results_dir(name)
    summ = os.path.join(res, '%s_summary.json' % date)
    if not os.path.exists(summ):
        raise ValueError('nothing to add -- analyse the session first')
    if read_checklist(name, date) is None:
        raise ValueError('confirm the shooting conditions first')
    if session_scored(name, date):
        raise ValueError('this session is already in your tracker')
    with open(summ) as fh:
        d = json.load(fh)
    if not d.get('n'):
        raise ValueError('no usable photos were scored, so there is nothing to add')

    def r4(v):
        return round(v, 4) if isinstance(v, (int, float)) else ''

    row = {
        'session_date': date,
        'run_timestamp': datetime.datetime.now(datetime.timezone.utc)
                                 .replace(microsecond=0).isoformat(),
        'n': d.get('n'), 'n_total_images': d.get('n_total_images'),
        'n_failed': d.get('n_failed'), 'n_flagged': d.get('n_flagged'),
        'mean': r4(d.get('mean')), 'median': r4(d.get('median')),
        'std': r4(d.get('std')) if d.get('std') == d.get('std') else '',
        'min': r4(d.get('min')), 'max': r4(d.get('max')),
        'mean_luma': (round(d['luma'], 1)
                      if isinstance(d.get('luma'), (int, float)) else ''),
        'model_sha256': model_sha_short(),
        'image_dir': session_dir(name, date),
        'notes': (body.get('notes') or '').strip()[:300].replace(',', ';'),
    }

    hist = os.path.join(res, 'faceage_history.csv')
    rows = []
    if os.path.exists(hist):
        with open(hist) as fh:
            rows = [x for x in csv.DictReader(fh)
                    if (x.get('session_date') or '').strip() != date]
    rows.append(row)
    rows.sort(key=lambda x: x.get('session_date') or '')
    tmp = hist + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=HISTORY_COLS, extrasaction='ignore')
        w.writeheader()
        for x in rows:
            w.writerow({c: x.get(c, '') for c in HISTORY_COLS})
    os.replace(tmp, hist)

    log = os.path.join(res, 'promoted.csv')
    new = not os.path.exists(log)
    with open(log, 'a', newline='') as fh:
        if new:
            fh.write('session,promoted_at,mean_at_promotion\n')
        fh.write('%s,%s,%s\n' % (date, row['run_timestamp'], row['mean']))

    build_chart(name)
    return {'ok': True, 'session': date, 'mean': row['mean']}


def do_preflight(person, date):
    """Diagnose the session -- but only if the QA on disk still describes the
    photos that are staged now.

    Importing more photos after a run leaves the per-image CSV describing the
    old set. Showing that as if it were current is worse than showing nothing:
    it is a verdict about photographs that are no longer the session.
    """
    per_image = os.path.join(results_dir(person), '%s_per_image.csv' % safe_date(date))
    if not os.path.exists(per_image):
        return {'available': False,
                'why': 'Pre-flight reads the per-image QA the pipeline writes, '
                       'so it becomes available once this session has been run.'}

    images = pf.load_per_image(per_image)
    scored_files = {i['file'] for i in images}
    current = set(staged(person, date))
    if current and scored_files != current:
        added = sorted(current - scored_files)
        removed = sorted(scored_files - current)
        bits = []
        if added:
            bits.append('%d added' % len(added))
        if removed:
            bits.append('%d removed' % len(removed))
        return {'available': False, 'stale': True,
                'why': 'The staged photos have changed since this session was '
                       'scored (%s), so the last pre-flight describes a '
                       'different set. Score again to refresh it.'
                       % ', '.join(bits)}
    findings = pf.diagnose(images, baseline_luma(person, session_source(person, date)))
    return {'available': True, 'n_frames': len(images),
            'verdict': pf.verdict(findings),
            'verdict_text': pf.VERDICT_TEXT[pf.verdict(findings)],
            'findings': [f.as_dict() for f in findings]}


def do_notes(body):
    """Save session notes typed into the tracker.

    Notes are commentary. Nothing reads them for analysis, and exclusion is
    governed solely by session_validity.csv, so a note cannot quietly remove a
    session from the series. That is why this needs no before/after ordering
    rule, unlike the checklist.
    """
    person = safe_subject(body.get('person'))
    notes = body.get('notes') or {}
    if not isinstance(notes, dict):
        raise ValueError('notes must be an object of session_date -> text')

    hist = os.path.join(results_dir(person), 'faceage_history.csv')
    if not os.path.exists(hist):
        raise ValueError('no history for %s yet' % person)
    with open(hist) as fh:
        known = {(r.get('session_date') or '').strip()
                 for r in csv.DictReader(fh)}

    clean = {}
    for k, v in notes.items():
        k = str(k).strip()
        if k not in known:
            raise ValueError('unknown session %s' % k)
        clean[k] = str(v)[:500].replace('\r', ' ').replace('\n', ' ')

    env = dict(os.environ, FACEAGE_RESULTS=results_dir(person),
               FACEAGE_SUBJECT_LABEL=person)
    code = ('import sys, json; sys.path.insert(0, %r); '
            'import faceage_chart as c; c.write_notes(json.load(sys.stdin))' % HERE)
    out = subprocess.run([sys.executable, '-c', code], input=json.dumps(clean),
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or 'could not write notes')
    build_chart(person)          # regenerate so the page matches the file
    return {'ok': True, 'saved': len(clean)}


def build_chart(person):
    out = subprocess.run([sys.executable, os.path.join(HERE, 'faceage_chart.py')],
                         capture_output=True, text=True,
                         env=dict(os.environ,
                                  FACEAGE_RESULTS=results_dir(person),
                                  FACEAGE_SUBJECT_LABEL=person))
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def fa_anchors(person):
    """B and R, if set. Before B exists there is no series yet (§4)."""
    try:
        return fa.load_anchors(os.path.join(results_dir(person), 'anchors.csv'))
    except Exception:                                    # noqa: BLE001
        return {}


def state(person=None, date=None, browse=None):
    people = list_people()
    if person is None and people:
        person = people[0]['name']
    today = datetime.date.today().isoformat()
    if not date and person:
        # Land on today's session unless it is already in the tracker, in which
        # case open the next take so a new visit always starts fresh.
        date = today if not session_scored(person, today) else next_take(person, today)
    date = date or today
    listing = list_dir(browse or INBOX)
    s = {'people': people, 'person': person, 'date': date, 'today': today,
         'inbox_path': INBOX, 'browse': listing,
         'inbox': listing['images'], 'data_path': DATA,
         'checklist_items': [{'key': k, 'label': l} for k, l in CHECKLIST],
         'job': JOB.snapshot()}
    if person:
        s.update({'staged': staged(person, date),
                  'checklist': read_checklist(person, date),
                  'checklist_stale': checklist_stale(person, date),
                  'prev_checklist': previous_checklist(person, date),
                  'has_b': bool(fa_anchors(person).get('B')),
                  'scored': session_scored(person, date),
                  'result': session_result(person, date),
                  'capture': capture_manifest(person, date),
                  'source': session_source(person, date),
                  'series_source': series_source(person),
                  'baseline_luma': baseline_luma(person, session_source(person, date)),
                  'baseline': baseline_info(person, session_source(person, date)),
                  # the camera step runs before any frame exists, so it cannot
                  # infer the source from the session: ask for the Mac's directly
                  'baseline_camera': baseline_info(person, 'mac-camera'),
                  'preflight': do_preflight(person, date)})
    return s


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaceAge</title>
<style>
:root{color-scheme:light dark;
  --bg:#f6f5f2;--card:#fff;--line:#e6e4df;--ink:#141414;--ink2:#5a5955;--ink3:#8a8880;
  --accent:#1f6feb;--accent-ink:#fff;--good:#1a8f3c;--good-bg:#e8f6ec;
  --warn:#9a6b00;--warn-bg:#fff5da;--bad:#c62828;--bad-bg:#fdecec;
  --radius:14px;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px -12px rgba(0,0,0,.12)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
  --bg:#141413;--card:#1e1e1c;--line:#302f2c;--ink:#f3f2ee;--ink2:#b9b7ae;--ink3:#84827a;
  --accent:#4c8df5;--good:#4fc26a;--good-bg:#173321;--warn:#e6b64a;--warn-bg:#3a2f12;
  --bad:#f06767;--bad-bg:#3b1c1c;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6)}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:640px;margin:0 auto;padding:36px 20px 80px}

/* header */
.top{display:flex;align-items:center;gap:12px;margin-bottom:28px;flex-wrap:wrap}
.brand{font-weight:700;font-size:18px;letter-spacing:-.01em;margin-right:auto}
.chip{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:99px;
  background:var(--card);border:1px solid var(--line);font-size:13px;color:var(--ink2)}
.chip b{color:var(--ink)}
.chip button{all:unset;cursor:pointer;color:var(--accent);font-size:12.5px;margin-left:2px}
.chip button:hover{text-decoration:underline}

/* step rail */
.rail{display:flex;gap:6px;margin:0 0 22px;padding:0;list-style:none}
.rail li{flex:1;display:flex;flex-direction:column;gap:7px;font-size:12px;color:var(--ink3);
  text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.rail li i{display:block;height:4px;border-radius:99px;background:var(--line)}
.rail li.done i{background:var(--good)}
.rail li.now{color:var(--ink)}
.rail li.now i{background:var(--accent)}
@media (max-width:480px){.rail li span{display:none}}

/* completed steps, compact */
.donerow{display:flex;align-items:center;gap:10px;padding:12px 16px;margin-bottom:10px;
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  font-size:14px;color:var(--ink2)}
.donerow .tick{color:var(--good);font-weight:700}
.donerow b{color:var(--ink);font-weight:600}
.donerow .sp{flex:1}
.donerow button{all:unset;cursor:pointer;color:var(--accent);font-size:13px}
.donerow button:hover{text-decoration:underline}

/* the active card */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:24px 24px 22px;box-shadow:var(--shadow);margin-bottom:14px}
.card h2{font-size:20px;margin:0 0 4px;letter-spacing:-.015em;font-weight:700}
.card .lead{color:var(--ink2);margin:0 0 18px;font-size:14.5px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:20px}
.actions .sp{flex:1}

/* controls */
button{font:inherit;font-weight:600;padding:11px 18px;border-radius:10px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
  transition:transform .05s ease,background .15s ease}
button:hover:not(:disabled){background:var(--bg)}
button:active:not(:disabled){transform:translateY(1px)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.primary:hover:not(:disabled){filter:brightness(1.06);background:var(--accent)}
button.quiet{border-color:transparent;background:transparent;color:var(--ink2);font-weight:500}
button.quiet:hover:not(:disabled){background:var(--bg);color:var(--ink)}
button.danger{color:var(--bad);border-color:transparent;background:transparent;font-weight:500}
button.danger:hover:not(:disabled){background:var(--bad-bg)}
button:disabled{opacity:.45;cursor:not-allowed}
input[type=text],select{font:inherit;padding:10px 12px;border-radius:10px;
  border:1px solid var(--line);background:var(--bg);color:var(--ink);min-width:0}
select{padding-right:32px}
.hint{font-size:13px;color:var(--ink3);margin-top:10px;line-height:1.5}
.err{margin-top:12px;padding:10px 13px;border-radius:10px;background:var(--bad-bg);color:var(--bad);font-size:14px}

/* conditions */
.cond{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--line)}
.cond:first-of-type{border-top:0}
.cond input{margin-top:4px;width:18px;height:18px;accent-color:var(--accent)}
.cond label{flex:1;font-size:14.5px;cursor:pointer}
.cond small{display:block;color:var(--ink3);font-size:12.5px;margin-top:2px}

/* file picker */
.pathbar{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.pathbar input{flex:1;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.pathbar button{padding:9px 12px}
.folders{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;max-height:96px;overflow:auto}
.folders button{padding:6px 11px;font-size:13px;font-weight:500;border-radius:8px}
.files{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:10px;
  max-height:260px;overflow:auto}
.files li{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);font-size:14px}
.files li:last-child{border-bottom:0}
.files li:hover{background:var(--bg)}
.files input{width:17px;height:17px;accent-color:var(--accent)}
.files .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-variant-numeric:tabular-nums}
.files .when{color:var(--ink3);font-size:12.5px}
.tag{font-size:11px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:6px;
  background:var(--bg);border:1px solid var(--line);color:var(--ink2)}
.staged{margin-top:14px;font-size:13.5px;color:var(--ink2)}
.staged b{color:var(--ink)}

/* camera */
.modes{display:flex;gap:8px;margin-bottom:16px}
.modes button{flex:1;padding:12px 14px;text-align:left;border-radius:12px;line-height:1.3}
.modes button.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);background:var(--card)}
.modes small{display:block;font-weight:500;color:var(--ink3);font-size:12.5px;margin-top:2px}
.camwrap{position:relative;background:#000;border-radius:14px;overflow:hidden;aspect-ratio:3/4;
  width:min(100%,400px);margin:0 auto;box-shadow:0 0 0 3px transparent;transition:box-shadow .25s}
.camwrap video{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.camwrap canvas{width:100%;height:100%;display:block}
.camwrap svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.camwrap .dim{fill:rgba(0,0,0,.42);transition:fill .25s}
.camwrap .oval{stroke:rgba(255,255,255,.75);stroke-dasharray:6 4;transition:stroke .25s}
.camwrap.near .oval{stroke:#ffcf4d}
.camwrap.ok .oval{stroke:#4fe08a;stroke-dasharray:none}
.camwrap.ok .dim{fill:rgba(30,120,60,.35)}
.camwrap.ok{box-shadow:0 0 0 3px #4fe08a}
.camwrap .cd{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:120px;font-weight:700;color:#fff;text-shadow:0 2px 28px rgba(0,0,0,.7);pointer-events:none}
.camwrap .flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .3s}
.camwrap .flash.on{opacity:.65;transition:none}
.camwrap .msg{position:absolute;left:0;right:0;bottom:0;padding:10px 14px;background:rgba(0,0,0,.55);
  color:#fff;font-size:15px;font-weight:600;text-align:center;font-variant-numeric:tabular-nums}
.camwrap.ok .msg{background:rgba(20,110,55,.8)}
.camstats{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:12px;font-size:13.5px;color:var(--ink2);
  font-variant-numeric:tabular-nums;justify-content:center}
.camstats b{color:var(--ink);font-weight:600}
.camstats .good{color:var(--good)} .camstats .good b{color:var(--good)}
.camstats .bad{color:var(--bad)} .camstats .bad b{color:var(--bad)}
.camopts{display:flex;flex-wrap:wrap;gap:6px 20px;margin:10px 0 14px;font-size:13.5px;color:var(--ink2)}
.camopts label{display:flex;align-items:center;gap:6px;cursor:pointer}
.camopts input{accent-color:var(--accent);width:16px;height:16px}

/* progress */
.prog{margin:8px 0 6px}
.bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden}
.fill{height:100%;background:var(--accent);border-radius:99px;transition:width .35s ease}
.fill.indet{width:100%!important;background:linear-gradient(90deg,var(--line) 0%,var(--accent) 50%,var(--line) 100%);
  background-size:200% 100%;animation:shimmer 1.6s linear infinite}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}
.progmeta{display:flex;gap:8px;align-items:baseline;margin-top:10px;font-size:14px;color:var(--ink2);
  font-variant-numeric:tabular-nums}
.progmeta b{color:var(--ink);font-size:15px}
.progmeta .pct{margin-left:auto;font-weight:700;color:var(--ink);font-size:20px;letter-spacing:-.01em}
details{margin-top:14px}
summary{cursor:pointer;color:var(--ink3);font-size:13px}
details.guide{margin:0 0 16px;padding:12px 16px;border:1px solid var(--line);border-radius:12px;background:var(--bg)}
details.guide summary{color:var(--accent);font-weight:600;font-size:14px}
details.guide ol{margin:12px 0 0;padding-left:20px;font-size:14px;line-height:1.55}
details.guide li{margin-bottom:9px}
details.guide li b{color:var(--ink)}
details.guide .hint{margin-top:8px}
pre.log{margin:8px 0 0;padding:10px;background:var(--bg);border:1px solid var(--line);border-radius:10px;
  font-size:12px;white-space:pre-wrap;max-height:220px;overflow:auto}

/* result */
.verdict{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:99px;
  font-size:13px;font-weight:700;letter-spacing:.02em;margin-bottom:14px}
.verdict.GOOD{background:var(--good-bg);color:var(--good)}
.verdict.CHECK{background:var(--warn-bg);color:var(--warn)}
.verdict.RESHOOT{background:var(--bad-bg);color:var(--bad)}
.big{display:flex;align-items:baseline;gap:12px;margin:2px 0 4px}
.big .n{font-size:56px;font-weight:700;letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
.big .u{color:var(--ink2);font-size:15px}
.stats{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink2);font-size:13.5px;
  font-variant-numeric:tabular-nums;margin-bottom:16px}
.stats b{color:var(--ink);font-weight:600}
.stats .bad{color:var(--bad)}
.stats .bad b{color:var(--bad)}
.reco{padding:14px 16px;border-radius:12px;font-size:15px;margin-bottom:16px;line-height:1.5}
.reco.GOOD{background:var(--good-bg)}
.reco.CHECK{background:var(--warn-bg)}
.reco.RESHOOT{background:var(--bad-bg)}
.find{border-left:3px solid var(--line);padding:10px 14px;margin:10px 0;border-radius:0 10px 10px 0;background:var(--bg)}
.find.RESHOOT{border-color:var(--bad)}
.find.CHECK{border-color:var(--warn)}
.find.INFO{border-color:var(--accent)}
.find .w{font-weight:600;margin-bottom:4px;font-size:14.5px}
.find p{margin:4px 0;font-size:13.5px;color:var(--ink2)}
.find .k{display:inline-block;width:32px;color:var(--ink3);font-size:11px;font-weight:700;letter-spacing:.06em}
.find .fl{color:var(--ink3);font-size:12.5px}
.note{margin-top:14px;padding:11px 14px;border-radius:10px;background:var(--warn-bg);font-size:13.5px}
.note.ok{background:var(--good-bg)}
.done-big{text-align:center;padding:12px 0 6px}
.done-big .tick{width:56px;height:56px;border-radius:50%;background:var(--good-bg);color:var(--good);
  display:inline-flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;margin-bottom:12px}
.done-big h2{margin-bottom:6px}
.foot{margin-top:28px;font-size:12px;color:var(--ink3);text-align:center}
.foot code{font-size:11.5px}
</style></head><body><div class="wrap">
<div class="top">
  <div class="brand">FaceAge</div>
  <div id="who"></div>
  <div id="sess"></div>
</div>
<ul class="rail" id="rail"></ul>
<div id="app"></div>
<p class="foot" id="foot"></p>
</div>
<script src="/static/pico.js"></script>
<script>
'use strict';
var S = null, sel = {}, ui = {browseDir:null, prefill:null, note:null, logOpen:false,
                             photoMode:'import', camError:null};

var STEPS = ['Who','Conditions','Photos','Analyse','Result'];

function h(x){return String(x==null?'':x).replace(/[&<>"']/g,
  function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}

function api(path, body){
  var o = body ? {method:'POST',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify(body)} : {};
  return fetch(path, o).then(function(r){
    return r.json().then(function(j){
      if(!r.ok) throw new Error(j.error || ('HTTP '+r.status));
      return j;});});
}

function load(){
  var qs = '?person='+encodeURIComponent(S&&S.person||'')+
           '&date='+encodeURIComponent(S&&S.date||'')+
           '&browse='+encodeURIComponent(ui.browseDir||'');
  return api('/api/state'+qs).then(function(j){ S = j; render(); })
    .catch(function(e){
      document.getElementById('app').innerHTML =
        '<div class="card"><h2>Could not load</h2><div class="err">'+h(e.message)+
        '</div><p class="hint">Check the terminal running <code>faceage app</code>.</p></div>';
    });
}

function fail(e){
  var a = document.getElementById('app');
  var d = document.createElement('div'); d.className='err'; d.textContent = e.message;
  a.appendChild(d);
}

function fmtSecs(n){
  if(n==null) return '';
  if(n < 60) return n+'s';
  var m = Math.floor(n/60), r = n%60;
  return m+'m'+(r?' '+r+'s':'');
}

function niceDate(label){
  var d = (label||'').slice(0,10), take = (label||'').slice(10);
  var dt = new Date(d+'T12:00:00');
  var s = isNaN(dt) ? d : dt.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'});
  return s + (take ? ' · take '+take : '');
}

/* ---- which step are we on? derived from disk state, nothing remembered ---- */
function stepOf(){
  if(!S.person) return 0;
  if(S.result || (S.job && S.job.running)) return 4;
  if(!S.checklist) return 1;
  if(!S.staged.length || ui.forcePhotos) return 2;
  return 3;
}

/* ---- render ---------------------------------------------------------------- */
function render(){
  var step = stepOf();
  if(!(step===2 && ui.photoMode==='camera')) camStop();
  document.getElementById('foot').innerHTML =
    'Everything stays on this Mac. Data in <code>'+h(S.data_path)+'</code>';

  // header chips
  document.getElementById('who').innerHTML = S.person
    ? '<span class="chip"><b>'+h(S.person)+'</b><button onclick="renamePerson()">rename</button>'+
      '<button onclick="changePerson()">change</button></span>' : '';
  document.getElementById('sess').innerHTML = S.person
    ? '<span class="chip">'+h(niceDate(S.date))+
      ((S.staged.length||S.checklist||S.result)?'<button onclick="discard()">start over</button>':'')+
      '</span>' : '';

  // rail
  document.getElementById('rail').innerHTML = STEPS.map(function(n,i){
    return '<li class="'+(i<step?'done':(i===step?'now':''))+'"><i></i><span>'+n+'</span></li>';
  }).join('');

  var o = [];
  if(step > 1 && S.checklist)
    o.push(doneRow('Conditions', S.checklist.valid ? 'all good' : 'problem noted',
                   step < 4 ? 'redoConditions()' : null));
  if(step > 2 && S.staged.length)
    o.push(doneRow('Photos', S.staged.length+' selected', step < 4 ? 'morePhotos()' : null));

  if(step===0) o.push(cardWho());
  else if(step===1) o.push(cardConditions());
  else if(step===2) o.push(cardPhotos());
  else if(step===3) o.push(cardAnalyse());
  else o.push(cardResult());

  document.getElementById('app').innerHTML = o.join('');
  wire();
}

function doneRow(label, value, change){
  return '<div class="donerow"><span class="tick">✓</span><span>'+h(label)+': <b>'+h(value)+'</b></span>'+
         '<span class="sp"></span>'+(change?'<button onclick="'+change+'">change</button>':'')+'</div>';
}

/* ---- 1 · who --------------------------------------------------------------- */
function cardWho(){
  var o = ['<div class="card"><h2>Who are we measuring?</h2>',
           '<p class="lead">Each person gets their own tracker. Nothing is compared between people.</p>'];
  if(S.people.length){
    o.push('<div class="row"><select id="pick">');
    S.people.forEach(function(p){
      o.push('<option value="'+h(p.name)+'">'+h(p.name)+' · '+p.logged+' in tracker</option>');});
    o.push('</select><button class="primary" onclick="pickPerson()">Continue</button></div>');
    o.push('<p class="hint">Or add someone new:</p>');
  }
  o.push('<div class="row"><input type="text" id="newname" placeholder="name" size="14">'+
         '<button onclick="addPerson()">Add</button></div>');
  o.push('<p class="hint">Photos of someone else are theirs. Ask first, and delete their folder when you are done.</p>');
  o.push('</div>');
  return o.join('');
}

/* ---- shooting guide -------------------------------------------------------- */
var GUIDE = [
  ['Light', 'Blinds closed, ceiling light off. One desk lamp in front of you, a little above eye level, bounced off a wall so it does not glare. Tape its position. This is the big one: lighting alone moved the result by 5 years in your own test shots.'],
  ['Shave', 'Shave the morning of every session. Stubble changes length daily, and the model looks hardest at exactly that part of the face.'],
  ['Camera', 'Either your phone\u2019s selfie camera, propped and never hand-held, or this Mac\u2019s camera with the app taking the shots. Pick one and keep it: sessions from two different cameras cannot be compared. Mark where the camera sits and where you sit.'],
  ['Framing', 'Your face should fill about 85% of the frame height: top of head near the top edge, chin near the bottom, not touching either. The app will tell you the exact number afterwards.'],
  ['Background', 'A plain wall or a hung sheet. Slats and patterns throw striped shadows and confuse the face detector.'],
  ['On the phone', 'Tap and hold on your face until AE/AF LOCK appears, so it stops re-metering between shots. Portrait mode off. Glasses off, hair off the forehead.'],
  ['The shots', 'Neutral face, mouth closed, look straight at the lens. Take 10 and bin any blinks.'],
  ['Then', 'Change nothing between sessions. Your first session with this setup becomes the baseline everything after is measured against.']
];
function guideHtml(open){
  var o = ['<details class="guide"'+(open?' open':'')+'><summary>How to set up the shot</summary><ol>'];
  GUIDE.forEach(function(g){ o.push('<li><b>'+h(g[0])+'</b> '+h(g[1])+'</li>'); });
  o.push('</ol><p class="hint">Fastest way to dial it in: take a test set, run it, and read the numbers on the result screen. Adjust until it says good capture.</p></details>');
  return o.join('');
}

/* ---- 2 · conditions -------------------------------------------------------- */
var TIPS = {
  grooming:'Same as your very first session, whatever that was.',
  light:'Same lamp, same spot, blinds closed. Not the ceiling light.',
  camera:'The same phone propped in the same spot, or this Mac in the same spot. Never a different camera, never hand-held.',
  pose:'Look straight ahead, relaxed face, mouth closed.',
  photoday:'No alcohol for two days, decent sleep, not straight after a shower or a workout.',
  skin:'No sunburn, breakout, or allergy flare on the forehead or cheeks.'
};
function cardConditions(){
  var pre = ui.prefill || {};
  var o = ['<div class="card"><h2>Same setup as last time?</h2>',
           '<p class="lead">Tick each one that is true right now. Lighting changes alone can move the result by years, so this matters more than it looks.</p>'];
  S.checklist_items.forEach(function(it){
    o.push('<div class="cond"><input type="checkbox" class="cl" id="c_'+h(it.key)+'" value="'+h(it.key)+'"'+
           (pre[it.key]?' checked':'')+'><label for="c_'+h(it.key)+'">'+h(it.label)+
           (TIPS[it.key]?'<small>'+h(TIPS[it.key])+'</small>':'')+'</label></div>');});
  o.push(guideHtml(!S.prev_checklist));
  o.push('<div class="actions">');
  if(S.prev_checklist)
    o.push('<button class="quiet" onclick="sameAsLast()">Same as '+h(niceDate(S.prev_checklist.session_date))+'</button>');
  o.push('<span class="sp"></span>');
  o.push('<button class="primary" onclick="saveConditions()">Continue</button></div>');
  o.push('<p class="hint">Leave anything unticked and this session will still be scored, but it will be kept out of your trend line.</p>');
  o.push('</div>');
  return o.join('');
}

/* ---- 3 · photos ------------------------------------------------------------ */
function modesHtml(){
  var cam = ui.photoMode==='camera';
  return '<div class="modes">'+
    '<button class="'+(cam?'':'on')+'" onclick="setMode(\'import\')">Import from my phone<small>Photos you already took, AirDropped to this Mac</small></button>'+
    '<button class="'+(cam?'on':'')+'" onclick="setMode(\'camera\')">Use this Mac\u2019s camera<small>Live framing guide, 10 photos taken for you</small></button>'+
    '</div>';
}

function cardPhotos(){
  if(ui.photoMode==='camera') return cardCamera();
  var B = S.browse || {path:S.inbox_path, dirs:[], images:[], parent:null};
  var o = ['<div class="card"><h2>Add the photos</h2>',
           '<p class="lead">Ten or so from this session, all from the same spot. AirDrop lands them in Downloads.</p>',
           modesHtml(), guideHtml(false)];
  o.push('<div class="pathbar"><button onclick="goUp()"'+(B.parent?'':' disabled')+' title="up">↑</button>'+
         '<input type="text" id="path" value="'+h(B.path)+'"><button onclick="goPath()">Go</button>'+
         '<button onclick="goHome()">Downloads</button></div>');
  if(B.dirs.length){
    o.push('<div class="folders">');
    B.dirs.forEach(function(d){ o.push('<button class="dir" data-dir="'+h(d)+'">📁 '+h(d)+'</button>'); });
    o.push('</div>');
  }
  if(!B.images.length){
    o.push('<p class="hint">No photos here. Open a folder above, or paste a path and press Go.</p>');
  } else {
    o.push('<ul class="files">');
    B.images.forEach(function(f){
      o.push('<li><input type="checkbox" class="ib" value="'+h(f.file)+'"'+(sel[f.file]?' checked':'')+'>'+
             '<span class="nm">'+h(f.file)+'</span>'+(f.heic?'<span class="tag">HEIC</span>':'')+
             '<span class="when">'+h(f.when)+'</span></li>');});
    o.push('</ul>');
    o.push('<div class="actions"><button class="quiet" onclick="pickRecent()">Newest 10</button>'+
           '<button class="quiet" onclick="pickAll()">All '+B.images.length+'</button>'+
           '<button class="quiet" onclick="pickNone()">None</button><span class="sp"></span>'+
           '<button class="primary" onclick="doImport()">Use '+countSel()+' selected</button></div>');
    if(B.images.some(function(f){return f.heic;}))
      o.push('<p class="hint">HEIC photos are converted to JPEG for analysis; the originals are kept.</p>');
  }
  if(S.staged.length) o.push('<div class="staged">Already in this session: <b>'+S.staged.length+'</b> photo'+(S.staged.length===1?'':'s')+'. '+
                              '<button class="quiet" style="padding:4px 8px" onclick="goAnalyse()">Continue without adding more</button></div>');
  o.push('</div>');
  return o.join('');
}
function countSel(){ return Object.keys(sel).filter(function(k){return sel[k];}).length; }

/* ---- 3b · the Mac camera --------------------------------------------------- */
var SHOTS = 10, SHOT_GAP_MS = 900, COUNTDOWN = 3, HOLD_MS = 1500, WAIT_POS_MS = 3000;
/* The saved photo is a fixed centre crop of the sensor: two thirds of its height,
   3:4 portrait. Fixed, so distance stays comparable between sessions; at desk
   distance it puts the face near 85% of the saved height without leaning in. */
var CROP = {h:0.667, aspect:0.75};
/* The pipeline measures the detector box (forehead to chin) against frame height,
   bar 80%. The live detector reports a face size about 0.89 of that box, measured
   on the authors’ own acceptable image, so its readings are scaled by this. */
var PICO_TO_BOX = 1.12;
/* The detector centres on the eyes and nose, above the middle of the face box,
   so a box centred in the frame reads as a detection centre near 0.44. */
var TARGET = {fillMin:0.72, fillMax:0.84, cy:0.44, tolX:0.06, tolY:0.07, lumaTol:5};
var CAM = {stream:null, starting:false, running:false, luma:null, w:0, h:0, fps:null, id:'', label:'',
           raf:null, face:null, posOk:false, lumaOk:true, aligned:false, alignedSince:0, guide:'',
           armed:true, muted:false, classify:null, mem:null, guideErr:null, gray:null, lastFrame:0};
try { CAM.armed = localStorage.getItem('faceage.autostart') !== 'off';
      CAM.muted = localStorage.getItem('faceage.sound') === 'off'; } catch(e){}

function cameraSupported(){
  return typeof navigator !== 'undefined' && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}
function now(){ return Date.now(); }

/* Face oval, eye line and centre line, in units of the saved 3:4 frame. */
function overlaySvg(){
  var cx = 60, cy = 80, ry = 67, rx = 52, ey = 62;
  return '<svg viewBox="0 0 120 160" preserveAspectRatio="none">'+
    '<path class="dim" fill-rule="evenodd" d="M0 0H120V160H0Z '+
    'M'+(cx-rx)+' '+cy+'a'+rx+' '+ry+' 0 1 0 '+(2*rx)+' 0a'+rx+' '+ry+' 0 1 0 '+(-2*rx)+' 0Z"/>'+
    '<ellipse class="oval" cx="'+cx+'" cy="'+cy+'" rx="'+rx+'" ry="'+ry+'" fill="none" stroke-width="2" vector-effect="non-scaling-stroke"/>'+
    '<line x1="'+cx+'" y1="'+(cy-ry)+'" x2="'+cx+'" y2="'+(cy+ry)+'" stroke="rgba(255,255,255,.5)" stroke-width="1" vector-effect="non-scaling-stroke"/>'+
    '<line x1="'+(cx-rx)+'" y1="'+ey+'" x2="'+(cx+rx)+'" y2="'+ey+'" stroke="rgba(255,255,255,.5)" stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>'+
    '</svg>';
}

function baselineLine(){
  var b = S.baseline_camera;
  if(b && b.luma!=null)
    return 'Brightness baseline for this camera: <b>'+Math.round(b.luma)+'</b>, set '+h(niceDate(b.date))+'.';
  return 'No brightness baseline for this camera yet. This session sets it, so get the light how you want it and keep it that way.';
}

function cardCamera(){
  var o = ['<div class="card"><h2>Add the photos</h2>',
           '<p class="lead">Sit where you always sit and fill the oval. The frame turns green when you are lined up, then the app counts down and takes '+SHOTS+' photos.</p>',
           modesHtml()];
  if(S.series_source && S.series_source!=='mac-camera'){
    o.push('<div class="note">Your tracker so far is built from phone photos, which cannot be compared with this camera. '+
           (S.has_b ? 'If you switch, switch for good and treat the first Mac session as your new starting point.'
                    : 'Those were rehearsals: once your baseline anchor is set at the first Mac session, they drop out of the trend.')+'</div>');
  }
  if(!cameraSupported()){
    o.push('<div class="err">This browser cannot use the camera. Try Safari or Chrome, or import from your phone instead.</div></div>');
    return o.join('');
  }
  if(ui.camError){
    o.push('<div class="err">'+h(ui.camError)+'</div>');
    o.push('<p class="hint">Allow camera access for 127.0.0.1 in the browser’s address bar or in System Settings › Privacy &amp; Security › Camera, then <button class="quiet" style="padding:2px 6px" onclick="camRetry()">try again</button>.</p></div>');
    return o.join('');
  }
  o.push('<div class="camwrap none" id="camwrap"><video id="cam" autoplay playsinline muted></video><canvas id="camview"></canvas>'+overlaySvg()+
         '<div class="flash" id="camflash"></div><div class="cd" id="camcd"></div>'+
         '<div class="msg" id="cammsg">'+(CAM.stream?'Looking for your face…':'Starting the camera…')+'</div></div>');
  o.push('<div class="camstats"><span id="camlight"></span><span id="camfill"></span><span id="camres"></span></div>');
  o.push('<p class="hint" id="cambase">'+baselineLine()+'</p>');
  o.push('<div class="camopts"><label><input type="checkbox" id="camauto"'+(CAM.armed?' checked':'')+'> Start automatically when lined up</label>'+
         '<label><input type="checkbox" id="camsound"'+(CAM.muted?'':' checked')+'> Sound</label></div>');
  o.push(guideHtml(false));
  o.push('<div class="actions">');
  if(S.staged.length)
    o.push('<span class="staged" style="margin:0">In this session: <b>'+S.staged.length+'</b></span>');
  o.push('<span class="sp"></span>');
  if(S.staged.length)
    o.push('<button class="quiet" onclick="goAnalyse()">Continue with '+S.staged.length+'</button>');
  o.push('<button class="primary" id="camstart" onclick="startCapture()"'+(CAM.stream?'':' disabled')+'>'+
         (S.staged.length?'Take '+SHOTS+' more':'Start · '+SHOTS+' photos')+'</button></div>');
  o.push('<p class="hint">Photos are saved unmirrored, straight into this session’s folder, as the part of the picture inside the frame above. Keep the Mac in the same place every time; mark it if you can.</p>');
  o.push('</div>');
  return o.join('');
}

function setMode(m){
  if(ui.photoMode===m) return;
  ui.photoMode = m; ui.camError = null;
  if(m==='camera') audioUnlock();               // inside the click, so Safari lets sound play later
  render();
}
function camRetry(){ ui.camError = null; render(); }

/* ---- sound: a small confirmation, never required ---- */
function audioUnlock(){
  try {
    var A = (typeof window!=='undefined') && (window.AudioContext || window.webkitAudioContext);
    if(!A) return;
    if(!audioUnlock.ctx) audioUnlock.ctx = new A();
    if(audioUnlock.ctx.resume) audioUnlock.ctx.resume();
  } catch(e){}
}
if(typeof document !== 'undefined' && document.addEventListener){
  // Safari only lets a page make sound after a gesture; any click or key will do
  ['pointerdown','keydown','touchstart'].forEach(function(ev){ document.addEventListener(ev, audioUnlock, {passive:true}); });
}
function beep(notes){
  if(CAM.muted) return;
  try {
    audioUnlock(); var ctx = audioUnlock.ctx; if(!ctx) return;
    if(ctx.state === 'suspended' && ctx.resume){ ctx.resume().then(function(){ playNotes(ctx, notes); }); return; }
    playNotes(ctx, notes);
  } catch(e){}
}
function playNotes(ctx, notes){
  try {
    var t = ctx.currentTime;
    notes.forEach(function(n){               // [frequency, start offset, length]
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine'; o.frequency.value = n[0];
      g.gain.setValueAtTime(0.0001, t+n[1]);
      g.gain.exponentialRampToValueAtTime(0.2, t+n[1]+0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t+n[1]+n[2]);
      o.connect(g); g.connect(ctx.destination);
      o.start(t+n[1]); o.stop(t+n[1]+n[2]+0.05);
    });
  } catch(e){}
}
var SND = {lined:[[880,0,.12],[1320,.12,.2]], tick:[[660,0,.09]], shutter:[[1500,0,.035]],
           done:[[784,0,.12],[988,.13,.12],[1175,.26,.3]], lost:[[330,0,.18]]};

/* ---- camera start/stop ---- */
function camStart(){
  if(CAM.stream || CAM.starting || ui.camError || !cameraSupported()) return;
  CAM.starting = true;
  loadGuide();
  navigator.mediaDevices.getUserMedia({audio:false,
      video:{facingMode:'user', width:{ideal:1920}, height:{ideal:1080}}})
    .then(function(stream){
      CAM.starting = false; CAM.stream = stream;
      var t = stream.getVideoTracks()[0], st = (t && t.getSettings) ? t.getSettings() : {};
      CAM.label = (t && t.label) || ''; CAM.w = st.width||0; CAM.h = st.height||0; CAM.fps = st.frameRate||null; CAM.id = st.deviceId||'';
      camAttach(); camLoop();
    })
    .catch(function(e){
      CAM.starting = false;
      ui.camError = (e && e.name==='NotAllowedError') ? 'Camera access was refused.' :
                    (e && e.name==='NotFoundError') ? 'No camera found on this Mac.' :
                    'Could not start the camera: '+(e && e.message ? e.message : e);
      render();
    });
}
function loadGuide(){
  if(CAM.classify || CAM.guideErr || typeof pico === 'undefined' || typeof fetch !== 'function') { if(typeof pico === 'undefined') CAM.guideErr = 'face guide not loaded'; return; }
  fetch('/static/facefinder').then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.arrayBuffer(); })
    .then(function(buf){ CAM.classify = pico.unpack_cascade(new Int8Array(buf)); CAM.mem = pico.instantiate_detection_memory(3); })
    .catch(function(e){ CAM.guideErr = 'face guide unavailable ('+e.message+')'; });
}
function camAttach(){
  var v = document.getElementById('cam');
  if(!v || !CAM.stream) return;
  if(v.srcObject !== CAM.stream){ v.srcObject = CAM.stream; if(v.play) { var p = v.play(); if(p && p.catch) p.catch(function(){}); } }
  var b = document.getElementById('camstart'); if(b && !CAM.running) b.disabled = false;
  var r = document.getElementById('camres'); if(r) r.innerHTML = CAM.w ? h(CAM.label||'camera')+' · <b>'+CAM.w+'×'+CAM.h+'</b>' : '';
  var a = document.getElementById('camauto'); if(a) a.onchange = function(){ CAM.armed = a.checked; try{ localStorage.setItem('faceage.autostart', a.checked?'on':'off'); }catch(e){} };
  var s = document.getElementById('camsound'); if(s) s.onchange = function(){ CAM.muted = !s.checked; try{ localStorage.setItem('faceage.sound', s.checked?'on':'off'); }catch(e){} if(s.checked) beep(SND.tick); };
}
function camStop(){
  if(CAM.raf && typeof cancelAnimationFrame === 'function'){ cancelAnimationFrame(CAM.raf); }
  CAM.raf = null;
  if(CAM.stream){ CAM.stream.getTracks().forEach(function(t){ t.stop(); }); }
  CAM.stream = null; CAM.starting = false; CAM.running = false; CAM.luma = null; CAM.face = null;
  CAM.posOk = false; CAM.aligned = false;
}

/* The crop of the sensor that becomes the saved photo, in sensor pixels. */
function cropRect(){
  var w = CAM.w, hh = CAM.h;
  var ch = Math.round(hh*CROP.h), cw = Math.round(ch*CROP.aspect);
  if(cw > w){ cw = w; ch = Math.round(cw/CROP.aspect); }
  return {x:Math.round((w-cw)/2), y:Math.round((hh-ch)/2), w:cw, h:ch};
}

/* ---- the live loop: draw the crop, find the face, say what to change ---- */
function camLoop(){
  if(!CAM.stream) return;
  var v = document.getElementById('cam'), view = document.getElementById('camview');
  if(v && view && v.videoWidth && view.getContext){
    if(!CAM.w){ CAM.w = v.videoWidth; CAM.h = v.videoHeight; }
    var R = cropRect();
    var W = 360, H = Math.round(W/CROP.aspect);
    if(view.width !== W){ view.width = W; view.height = H; }
    var g = view.getContext('2d');
    g.save(); g.translate(W, 0); g.scale(-1, 1);            // mirror for the preview only
    g.drawImage(v, R.x, R.y, R.w, R.h, 0, 0, W, H);
    g.restore();
    var t = now();
    if(t - CAM.lastFrame > 90){ CAM.lastFrame = t; detect(v, R); guidance(); }
    if(CAM.face){                                             // a light ring on the face it sees
      var f = CAM.face;
      g.beginPath(); g.arc((1-f.cx)*W, f.cy*H, f.s*H/2, 0, Math.PI*2);
      g.strokeStyle = CAM.posOk ? 'rgba(80,220,120,.9)' : 'rgba(255,255,255,.55)'; g.lineWidth = 2; g.stroke();
    }
  }
  if(typeof requestAnimationFrame === 'function') CAM.raf = requestAnimationFrame(camLoop);
}

function detect(v, R){
  /* Detect on the whole sensor frame, not the crop: the detector scans square
     windows, and a well-framed face is wider than the 3:4 crop is. Results are
     mapped into crop units, which is what the saved photo is measured in. */
  var Hd = 200, Wd = Math.round(Hd*CAM.w/CAM.h);
  var c = detect.c || (detect.c = document.createElement('canvas'));
  if(!c.getContext) return;
  if(c.width !== Wd || c.height !== Hd){ c.width = Wd; c.height = Hd; }
  var g = c.getContext('2d', {willReadFrequently:true});
  g.drawImage(v, 0, 0, Wd, Hd);
  var d = g.getImageData(0, 0, Wd, Hd).data, gray = new Uint8Array(Wd*Hd);
  for(var i=0;i<Wd*Hd;i++) gray[i] = (2*d[4*i] + 7*d[4*i+1] + d[4*i+2]) / 10;
  var k = Hd/CAM.h;                                   // sensor px -> detection px
  var rx0 = R.x*k, ry0 = R.y*k, rw = R.w*k, rh = R.h*k;
  CAM.face = null;
  if(CAM.classify){
    var dets = pico.run_cascade({pixels:gray, nrows:Hd, ncols:Wd, ldim:Wd}, CAM.classify,
                                {shiftfactor:0.1, minsize:Math.round(rh*0.3), maxsize:Math.min(Hd, Wd), scalefactor:1.1});
    dets = CAM.mem(dets);
    dets = pico.cluster_detections(dets, 0.2).filter(function(x){ return x[3] > 30; })
              .sort(function(a,b){ return b[3]-a[3]; });
    if(dets.length){
      var b = dets[0];                                // [row, col, size, score]
      CAM.face = {cy:(b[0]-ry0)/rh, cx:(b[1]-rx0)/rw, s:b[2]/rh, q:b[3]};
    }
  }
  /* Brightness the way the pipeline measures it: the plain mean of R, G and B
     over the face box (forehead to chin, about 0.79 as wide as tall), whose
     centre sits a little below the detector centre. Without a face, the oval. */
  var f = CAM.face;
  var bh = f ? f.s*PICO_TO_BOX*rh : rh*0.85, bw = bh*0.79;
  var cx = f ? rx0 + f.cx*rw : rx0 + rw/2, cy = f ? ry0 + (f.cy+0.07)*rh : ry0 + rh*0.5;
  var y0 = Math.max(0, Math.round(cy-bh/2)), y1 = Math.min(Hd, Math.round(cy+bh/2));
  var x0 = Math.max(0, Math.round(cx-bw/2)), x1 = Math.min(Wd, Math.round(cx+bw/2));
  var sum = 0, n = 0;
  for(var y=y0;y<y1;y++) for(var x=x0;x<x1;x++){
    var i4 = (y*Wd+x)*4; sum += d[i4] + d[i4+1] + d[i4+2]; n += 3;
  }
  CAM.luma = n ? sum/n : null;
}

function guidance(){
  var f = CAM.face, msg, state, wasAligned = CAM.aligned;
  var base = (S.baseline_camera && S.baseline_camera.luma!=null) ? S.baseline_camera.luma : null;
  var L = CAM.luma, lumaMsg = '', lumaCls = '';
  if(L!=null){
    if(base!=null){
      var dl = L - base;
      CAM.lumaOk = Math.abs(dl) <= TARGET.lumaTol;
      lumaCls = CAM.lumaOk ? 'good' : 'bad';
      lumaMsg = CAM.lumaOk ? '' : (dl>0 ? 'a bit bright: dim the lamp or move it back' : 'a bit dark: bring the lamp closer or turn it up');
    } else {
      CAM.lumaOk = L >= 70 && L <= 190;
      lumaCls = CAM.lumaOk ? '' : 'bad';
      lumaMsg = CAM.lumaOk ? '' : (L < 70 ? 'dark: put more light on your face' : 'very bright: dim it');
    }
  }
  if(!f){
    CAM.posOk = false; state = 'none';
    msg = CAM.guideErr ? 'Face guide unavailable. Line up with the oval by eye.' : 'Looking for your face…';
  } else {
    var dx = f.cx - 0.5, dy = f.cy - TARGET.cy, fill = f.s;
    var fixes = [];
    if(fill < TARGET.fillMin) fixes.push(fill < TARGET.fillMin-0.12 ? 'Come closer' : 'Come a little closer');
    else if(fill > TARGET.fillMax) fixes.push('Move back a little');
    if(Math.abs(dx) > TARGET.tolX) fixes.push((1-f.cx) < 0.5 ? 'Move a little to your right' : 'Move a little to your left');
    if(Math.abs(dy) > TARGET.tolY) fixes.push(dy < 0 ? 'Sit a little lower, or tilt the screen up' : 'Sit up a little, or tilt the screen down');
    CAM.posOk = !fixes.length;
    if(fixes.length){ state = 'near'; msg = fixes[0]; }
    else if(!CAM.lumaOk){ state = 'ok'; msg = 'Position is good. Light is '+lumaMsg+'.'; }
    else { state = 'ok'; msg = CAM.running ? msg : (CAM.armed ? 'Perfect. Hold still…' : 'Perfect. Press Start.'); }
  }
  CAM.aligned = CAM.posOk && CAM.lumaOk;
  if(CAM.aligned && !wasAligned){ CAM.alignedSince = now(); if(!CAM.running) beep(SND.lined); }
  if(!CAM.posOk && wasAligned && !CAM.running) beep(SND.lost);

  var wrap = document.getElementById('camwrap'); if(wrap) wrap.className = 'camwrap '+state;
  if(!CAM.running && msg) camSay(msg);
  var el = document.getElementById('camlight');
  if(el){
    el.className = lumaCls;
    el.innerHTML = L==null ? '' : 'brightness on your face <b>'+Math.round(L)+'</b>'+
      (base!=null ? ' · baseline '+Math.round(base)+(CAM.lumaOk?' ✓':'') : '');
  }
  var fe = document.getElementById('camfill');
  if(fe) fe.innerHTML = f ? 'face <b>'+Math.round(f.s*PICO_TO_BOX*100)+'%</b> of frame height' : '';

  if(CAM.aligned && CAM.armed && !CAM.running && CAM.stream && now()-CAM.alignedSince >= HOLD_MS) startCapture();
}

/* ---- taking the photos ---- */
function grabFrame(){
  var v = document.getElementById('cam'), R = cropRect();
  var c = document.createElement('canvas');
  c.width = R.w; c.height = R.h;
  c.getContext('2d').drawImage(v, R.x, R.y, R.w, R.h, 0, 0, R.w, R.h);   // 1:1 pixels, not mirrored
  return c.toDataURL('image/jpeg', 0.95);
}
function camSay(t){ var m = document.getElementById('cammsg'); if(m) m.textContent = t; }
function camCount(t){ var c = document.getElementById('camcd'); if(c) c.textContent = t; }
function camFlash(){
  var f = document.getElementById('camflash'); if(!f) return;
  f.className = 'flash on'; setTimeout(function(){ f.className = 'flash'; }, 60);
}
function stamp(){
  var d = new Date(), p = function(n){ return (n<10?'0':'')+n; };
  return ''+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'-'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds());
}

function startCapture(){
  if(!CAM.stream || CAM.running) return;
  CAM.running = true;
  var b = document.getElementById('camstart'); if(b){ b.disabled = true; b.textContent = 'Taking photos…'; }
  var batch = stamp(), saved = 0, n = COUNTDOWN, R = cropRect();
  var settings = {camera:CAM.label, width:CAM.w, height:CAM.h, frame_rate:CAM.fps, device_id:CAM.id,
                  mirrored:false, quality:0.95, crop:{x:R.x, y:R.y, w:R.w, h:R.h, height_frac:CROP.h, aspect:CROP.aspect},
                  saved_width:R.w, saved_height:R.h, guide:CAM.classify ? 'pico' : 'none',
                  user_agent:(typeof navigator!=='undefined'&&navigator.userAgent)||''};
  function finish(err){
    CAM.running = false; camCount('');
    if(err){ fail(err); if(b){ b.disabled = false; b.textContent = 'Try again'; } return; }
    beep(SND.done); camSay('Done · '+saved+' saved');
    load();                                        // lands on Analyse: staged count comes from disk
  }
  function abort(){                                // lost the face during the countdown
    CAM.running = false; camCount(''); beep(SND.lost);
    camSay('Lost you — line up again'); CAM.alignedSince = now();
    if(b){ b.disabled = false; b.textContent = S.staged.length ? 'Take '+SHOTS+' more' : 'Start · '+SHOTS+' photos'; }
  }
  function shot(i, waited){
    if(i > SHOTS) return finish(null);
    if(!CAM.posOk && CAM.face && (waited||0) < WAIT_POS_MS){     // wait for them to settle, briefly
      camSay('Hold still · lining up '+i+' of '+SHOTS);
      return setTimeout(function(){ shot(i, (waited||0)+100); }, 100);
    }
    camSay('Hold still · '+i+' of '+SHOTS); camFlash(); beep(SND.shutter);
    var img, f = CAM.face;
    try { img = grabFrame(); } catch(e){ return finish(e); }
    var frame = {luma: CAM.luma!=null ? Math.round(CAM.luma*10)/10 : null,
                 fill: f ? Math.round(f.s*PICO_TO_BOX*1000)/1000 : null,
                 dx: f ? Math.round((f.cx-0.5)*1000)/1000 : null,
                 dy: f ? Math.round((f.cy-TARGET.cy)*1000)/1000 : null,
                 aligned: !!CAM.posOk};
    api('/api/capture', {person:S.person, date:S.date, batch:batch, index:i, image:img,
                         settings:settings, frame:frame})
      .then(function(j){ saved++; if(j.moved_to_new_take) S.date = j.session;
                         setTimeout(function(){ shot(i+1, 0); }, SHOT_GAP_MS); })
      .catch(finish);
  }
  function tick(){
    if(CAM.face && !CAM.posOk) return abort();
    if(n === 0){ camCount(''); camSay('Hold still'); return shot(1, 0); }
    camCount(String(n)); beep(SND.tick); camSay('Neutral face, mouth closed, look at the camera'); n--;
    setTimeout(tick, 1000);
  }
  tick();
}

/* ---- 4 · analyse ----------------------------------------------------------- */
function cardAnalyse(){
  var o = ['<div class="card"><h2>Ready to analyse</h2>',
           '<p class="lead"><b>'+S.staged.length+'</b> photo'+(S.staged.length===1?'':'s')+' in this session'+
           (S.source==='mac-camera'?', taken with this Mac\u2019s camera':'')+'. This takes about half a minute. Nothing is saved to your tracker until you say so.</p>'];
  o.push('<div class="actions"><button class="quiet" onclick="morePhotos()">Add more photos</button><span class="sp"></span>'+
         '<button class="primary" onclick="analyse()">Analyse</button></div></div>');
  return o.join('');
}

function cardProgress(){
  var j = S.job, pr = (j && j.progress) || {};
  var pct = pr.total ? pr.pct : null;
  var o = ['<div class="card"><h2>Analysing…</h2>',
           '<p class="lead">Finding the face in each photo, then estimating age.</p>',
           '<div class="prog"><div class="bar"><div id="pfill" class="fill'+(pct===null?' indet':'')+
           '" style="width:'+(pct===null?100:pct)+'%"></div></div>',
           '<div class="progmeta"><b id="pphase">'+h(pr.phase||'Starting')+'</b>'+
           '<span id="pcount">'+(pr.total?(pr.done+' of '+pr.total):'')+'</span>'+
           '<span id="peta">'+(j.eta!=null?('· about '+fmtSecs(j.eta)+' left'):'')+'</span>'+
           '<span class="pct" id="ppct">'+(pct===null?'':Math.round(pct)+'%')+'</span></div></div>',
           '<details id="logdet"'+(ui.logOpen?' open':'')+'><summary>Details</summary>'+
           '<pre class="log" id="plog">'+h(j.log.join('\n'))+'</pre></details></div>'];
  return o.join('');
}

/* Patch the progress card in place. Re-rendering it every poll restarted the
   CSS animation from zero each time, which read as a hard cut to the left. */
function updateProgress(j){
  var pr = j.progress || {}, pct = pr.total ? pr.pct : null;
  var f = document.getElementById('pfill'); if(!f) return false;
  if(pct===null){ f.className='fill indet'; f.style.width='100%'; }
  else { f.className='fill'; f.style.width=pct+'%'; }
  document.getElementById('pphase').textContent = pr.phase||'Starting';
  document.getElementById('pcount').textContent = pr.total?(pr.done+' of '+pr.total):'';
  document.getElementById('peta').textContent = j.eta!=null?('· about '+fmtSecs(j.eta)+' left'):'';
  document.getElementById('ppct').textContent = pct===null?'':Math.round(pct)+'%';
  var l = document.getElementById('plog'); if(l) l.textContent = j.log.join('\n');
  return true;
}

/* ---- 5 · result ------------------------------------------------------------ */
var VERDICT_LABEL = {GOOD:'Good capture', CHECK:'Usable, with notes', RESHOOT:'Reshoot recommended'};
var VERDICT_ICON  = {GOOD:'✓', CHECK:'!', RESHOOT:'✕'};
var RECO = {
  GOOD:   'This looks like a clean session. Add it to your tracker.',
  CHECK:  'Usable. Read the notes below, then decide.',
  RESHOOT:'We would skip this one. Fix the items below and shoot again — adding it would put a number you cannot trust into your trend.'
};

function cardResult(){
  if(S.job && S.job.running) return cardProgress();
  var R = S.result;
  if(!R || R.mean == null){
    return '<div class="card"><h2>Analysis did not produce a result</h2>'+
           '<p class="lead">Usually no face was found in any photo. Check the details, then start over.</p>'+
           (S.job && S.job.log.length ? '<details open><summary>Details</summary><pre class="log">'+h(S.job.log.join('\n'))+'</pre></details>':'')+
           '<div class="actions"><span class="sp"></span><button class="danger" onclick="discard()">Start over</button></div></div>';
  }
  if(R.logged) return cardDone(R);

  var P = S.preflight || {}, v = P.available ? P.verdict : 'CHECK';
  var o = ['<div class="card">'];
  o.push('<span class="verdict '+h(v)+'">'+VERDICT_ICON[v]+' '+h(VERDICT_LABEL[v])+'</span>');
  o.push('<div class="big"><span class="n">'+R.mean.toFixed(1)+'</span><span class="u">FaceAge · average of '+R.n+' photos</span></div>');
  var st = [];
  if(R.std!=null) st.push('<span>spread <b>±'+R.std.toFixed(1)+'</b></span>');
  if(R.n_total && R.n < R.n_total) st.push('<span><b>'+(R.n_total-R.n)+'</b> photo'+((R.n_total-R.n)===1?'':'s')+' unusable</span>');
  if(R.luma!=null){
    var l = 'brightness <b>'+R.luma.toFixed(0)+'</b>';
    if(R.luma_delta!=null) l += ' ('+(R.luma_delta>0?'+':'')+R.luma_delta.toFixed(0)+' vs your baseline'+
                                (S.baseline&&S.baseline.date?' from '+h(niceDate(S.baseline.date)):'')+')';
    else if(!S.baseline) l += ' (sets the baseline for this camera)';
    st.push('<span class="'+(R.luma_ok===false?'bad':'')+'">'+l+'</span>');
  }
  o.push('<div class="stats">'+st.join('')+'</div>');
  o.push('<div class="reco '+h(v)+'">'+h(RECO[v])+'</div>');

  if(P.available && P.findings.length){
    P.findings.forEach(function(f){
      o.push('<div class="find '+h(f.severity)+'"><div class="w">'+h(f.what)+'</div>'+
             '<p><span class="k">WHY</span>'+h(f.why)+'</p><p><span class="k">DO</span>'+h(f.do)+'</p>'+
             (f.frames&&f.frames.length?'<p class="fl">'+h(f.frames.slice(0,5).join(', '))+(f.frames.length>5?' +'+(f.frames.length-5)+' more':'')+'</p>':'')+
             '</div>');});
  }
  if(!R.valid)
    o.push('<div class="note">You noted a problem with the conditions, so if you add this it will show on the chart but stay out of your trend line'+
           (R.excluded_reason?': '+h(R.excluded_reason):'')+'.</div>');
  if(R.fellback)
    o.push('<div class="note">Every photo had something off, so the average used all of them rather than only the clean ones.</div>');
  if(S.source && S.series_source && S.source !== S.series_source)
    o.push('<div class="note">This session was shot on '+(S.source==='mac-camera'?'this Mac’s camera':'your phone')+
           ', but your tracker so far is from '+(S.series_source==='mac-camera'?'the Mac’s camera':'phone photos')+
           '. '+(S.has_b ? 'Adding it would compare two cameras, not two dates.'
                        : 'Those earlier sessions were rehearsals: set your baseline anchor at this session and they drop out of the trend.')+'</div>');

  o.push('<div class="actions"><button class="danger" onclick="discard()">Discard this session</button><span class="sp"></span>'+
         '<button class="primary" onclick="addToTracker()">Add to my tracker</button></div>');
  o.push('<p class="hint">Photos are never edited. If something is off, change the setup and shoot again.</p>');
  o.push('</div>');
  return o.join('');
}

function cardDone(R){
  return '<div class="card"><div class="done-big"><div class="tick">✓</div>'+
         '<h2>Added to your tracker</h2>'+
         '<p class="lead"><b>'+R.mean.toFixed(1)+'</b> on '+h(niceDate(S.date))+
         (R.valid?'':' · kept out of the trend line because of the conditions you noted')+'</p></div>'+
         '<div class="actions" style="justify-content:center">'+
         '<a href="/tracker?person='+encodeURIComponent(S.person)+'" target="_blank"><button>View tracker</button></a>'+
         '<button class="primary" onclick="newSession()">Start a new session</button></div>'+
         '<p class="hint" style="text-align:center">Changed your mind? <button class="quiet" style="padding:2px 6px" onclick="removeFromTracker()">Remove it from the tracker</button></p></div>';
}

/* ---- wiring ---------------------------------------------------------------- */
function wire(){
  document.querySelectorAll('.ib').forEach(function(c){ c.onchange = function(){ sel[c.value] = c.checked; refreshUseBtn(); }; });
  document.querySelectorAll('button.dir').forEach(function(b){
    b.onclick = function(){ var base = S.browse.path;
      browseTo(base + (base.charAt(base.length-1)==='/'?'':'/') + b.getAttribute('data-dir')); }; });
  var det = document.getElementById('logdet');
  if(det) det.addEventListener('toggle', function(){ ui.logOpen = det.open; });
  var p = document.getElementById('path');
  if(p) p.addEventListener('keydown', function(e){ if(e.key==='Enter') goPath(); });
  var nn = document.getElementById('newname');
  if(nn) nn.addEventListener('keydown', function(e){ if(e.key==='Enter') addPerson(); });
  if(stepOf()===2 && ui.photoMode==='camera'){ camStart(); camAttach(); }
}
function refreshUseBtn(){
  var b = document.querySelector('.actions .primary');
  if(b && /selected/.test(b.textContent)) b.textContent = 'Use '+countSel()+' selected';
}

/* ---- actions --------------------------------------------------------------- */
function pickPerson(){ var s = document.getElementById('pick'); S.person = s.value; S.date = null; sel = {}; load(); }
function changePerson(){ S.person = null; render(); }
function renamePerson(){
  var n = prompt('Rename '+S.person+' to:', S.person);
  if(!n || n.trim()===S.person) return;
  api('/api/person/rename', {old:S.person, new:n.trim()})
    .then(function(j){ S.person = j.name; load(); }).catch(fail);
}
function addPerson(){
  var n = (document.getElementById('newname').value||'').trim();
  if(!n) return;
  api('/api/person', {name:n}).then(function(){ S.person = n; S.date = null; load(); }).catch(fail);
}

function sameAsLast(){ ui.prefill = (S.prev_checklist && S.prev_checklist.answers) || null; render(); }
function saveConditions(){
  var a = {};
  document.querySelectorAll('.cl').forEach(function(c){ a[c.value] = c.checked; });
  api('/api/checklist', {person:S.person, date:S.date, answers:a})
    .then(function(){ ui.prefill = null; load(); }).catch(fail);
}
function redoConditions(){
  if(S.scored){ return; }
  ui.prefill = (S.checklist && S.checklist.answers) || null;
  S.checklist = null; render();     // re-answer; the server keeps the history
}

function imgs(){ return (S.browse && S.browse.images) || []; }
function pickRecent(){ sel={}; imgs().slice(0,10).forEach(function(f){sel[f.file]=true;}); render(); }
function pickAll(){ sel={}; imgs().forEach(function(f){sel[f.file]=true;}); render(); }
function pickNone(){ sel={}; render(); }
function browseTo(p){ ui.browseDir = p; sel = {}; load(); }
function goPath(){ browseTo(document.getElementById('path').value.trim()); }
function goUp(){ if(S.browse && S.browse.parent) browseTo(S.browse.parent); }
function goHome(){ browseTo(S.inbox_path); }
function doImport(){
  var files = Object.keys(sel).filter(function(k){return sel[k];});
  if(!files.length){ fail(new Error('Tick at least one photo.')); return; }
  api('/api/import', {person:S.person, date:S.date, files:files, dir:(S.browse&&S.browse.path)||S.inbox_path})
    .then(function(j){ sel = {}; ui.forcePhotos = false; if(j.moved_to_new_take) S.date = j.session; load(); }).catch(fail);
}
function morePhotos(){ ui.forcePhotos = true; render(); }
function goAnalyse(){ ui.forcePhotos = false; render(); }

function analyse(){
  api('/api/score', {person:S.person, date:S.date}).then(poll).catch(fail);
}
function poll(){
  api('/api/job').then(function(j){
    S.job = j;
    if(j.running){ if(!updateProgress(j)) render(); setTimeout(poll, 800); }
    else load();
  });
}

function addToTracker(){
  api('/api/add', {person:S.person, date:S.date}).then(function(){ load(); }).catch(fail);
}
function removeFromTracker(){
  var msg = 'Remove '+niceDate(S.date)+' from the tracker? The photos and result stay; only the tracker row goes, and the removal is logged.';
  if(S.has_b) msg += '\n\nThis session is after your baseline anchor, so it is study data. Removing it after seeing the number is the thing the rules warn about; marking it invalid with a reason is the cleaner path.';
  if(!confirm(msg)) return;
  var reason = prompt('Why? (kept in the log)', '') || '';
  api('/api/remove', {person:S.person, date:S.date, reason:reason}).then(function(){ load(); }).catch(fail);
}
function discard(){
  var msg = 'Start over? The photos, answers and result for this session move to discarded/ (nothing is deleted).';
  if(!confirm(msg)) return;
  var reason = prompt('Why? (optional — kept with the discarded session)', '') || '';
  api('/api/discard', {person:S.person, date:S.date, reason:reason})
    .then(function(){ sel = {}; ui.prefill = null; load(); }).catch(fail);
}
function newSession(){
  api('/api/reshoot', {person:S.person, date:S.date})
    .then(function(j){ S.date = j.session; sel = {}; ui.prefill = null; load(); }).catch(fail);
}

load();
</script></body></html>
'''


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

# Vendored, served from tools/web. pico.js (MIT, Nenad Markus) is a tiny
# pure-JS face detector: it gives the live framing guide something to measure
# without any network or native dependency.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
STATIC = {'pico.js': 'application/javascript', 'facefinder': 'application/octet-stream'}

ROUTES_POST = {
    '/api/person': do_create_person,
    '/api/import': do_import,
    '/api/capture': do_capture,
    '/api/checklist': do_checklist,
    '/api/score': do_score,
    '/api/notes': do_notes,
    '/api/reshoot': do_reshoot,
    '/api/discard': do_discard,
    '/api/add': do_add,
    '/api/remove': do_remove,
    '/api/person/rename': do_rename,
}


class Handler(BaseHTTPRequestHandler):
    server_version = 'faceage'

    def log_message(self, fmt, *args):
        # Quiet for normal traffic -- this is a local tool, not a web server --
        # but never silent about failures: a blank page with a silent server is
        # the hardest thing to diagnose.
        msg = fmt % args
        if ' 200 ' not in msg and ' 304 ' not in msg:
            sys.stderr.write('%s\n' % msg)

    def _send(self, code, body, ctype='application/json'):
        raw = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        # Local-only tool handling biometric data: no caching, no embedding.
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def do_GET(self):
        path = self.path.split('?')[0]
        q = {}
        if '?' in self.path:
            for part in self.path.split('?', 1)[1].split('&'):
                if '=' in part:
                    k, v = part.split('=', 1)
                    from urllib.parse import unquote_plus
                    q[unquote_plus(k)] = unquote_plus(v)
        try:
            if path == '/':
                return self._send(200, PAGE, 'text/html')
            if path == '/api/state':
                return self._json(state(q.get('person') or None,
                                        q.get('date') or None,
                                        q.get('browse') or None))
            if path == '/api/job':
                return self._json(JOB.snapshot())
            if path.startswith('/static/'):
                name = path[len('/static/'):]
                if name not in STATIC:
                    return self._json({'error': 'not found'}, 404)
                with open(os.path.join(WEB_DIR, name), 'rb') as fh:
                    return self._send(200, fh.read(), STATIC[name])
            if path == '/tracker':
                person = safe_subject(q.get('person'))
                out = build_chart(person)
                if not out or not os.path.exists(out):
                    return self._send(404, '<p>No chart yet — score a session '
                                           'first.</p>', 'text/html')
                with open(out) as fh:
                    return self._send(200, fh.read(), 'text/html')
            return self._json({'error': 'not found'}, 404)
        except ValueError as exc:
            return self._json({'error': str(exc)}, 400)
        except Exception as exc:                          # noqa: BLE001
            return self._json({'error': str(exc)}, 500)

    def do_POST(self):
        path = self.path.split('?')[0]
        fn = ROUTES_POST.get(path)
        if not fn:
            return self._json({'error': 'not found'}, 404)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            # A camera frame is a base64 JPEG; everything else is a few KB.
            limit = (MAX_CAPTURE_BYTES * 4 // 3 + 4096) if path == '/api/capture' else 1 << 20
            if n > limit:
                return self._json({'error': 'payload too large'}, 413)
            body = json.loads(self.rfile.read(n) or b'{}')
            return self._json(fn(body))
        except ValueError as exc:
            return self._json({'error': str(exc)}, 400)
        except RuntimeError as exc:
            return self._json({'error': str(exc)}, 409)
        except Exception as exc:                          # noqa: BLE001
            return self._json({'error': str(exc)}, 500)


def main(argv=None):
    argv = argv or sys.argv[1:]
    port = PORT
    if argv and argv[0].isdigit():
        port = int(argv[0])
    srv = HTTPServer((HOST, port), Handler)
    url = 'http://%s:%d' % (HOST, port)
    print('FaceAge  %s' % url)
    print('data   : %s' % DATA)
    print('inbox  : %s' % INBOX)
    print('Bound to 127.0.0.1 only — not reachable from other machines.')
    print('Ctrl-C to stop.')
    if '--no-open' not in argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
