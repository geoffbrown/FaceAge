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
    ('camera', 'Same camera, lens, distance, height and framing (rear main, not ultrawide)'),
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


def baseline_luma(name):
    """Session one's mean_luma — the exposure every later session is held to."""
    hist = os.path.join(results_dir(name), 'faceage_history.csv')
    if not os.path.exists(hist):
        return None
    with open(hist) as fh:
        for r in csv.DictReader(fh):
            v = (r.get('mean_luma') or '').strip()
            if v:
                try:
                    return float(v)
                except ValueError:
                    return None
    return None


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

    base = baseline_luma(name)
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


def do_score(body):
    name = safe_subject(body.get('person'))
    date = safe_date(body.get('date'))
    oneoff = bool(body.get('oneoff'))

    if not staged(name, date):
        raise ValueError('no photos staged for %s' % date)

    # §1 ordering, enforced: a tracked session cannot be scored until the
    # checklist exists. A one-off enters no series, so it is exempt.
    if not oneoff and read_checklist(name, date) is None:
        raise ValueError('answer the session checklist before scoring — it has '
                         'to be decided without knowing the number (§1)')

    promoting = bool(body.get('promote'))
    if promoting:
        # Adding an existing one-off result to the series. Logged, because it
        # is a decision taken with the number already known -- allowed, but not
        # silent.
        res = results_dir(name)
        os.makedirs(res, exist_ok=True)
        log = os.path.join(res, 'promoted.csv')
        new = not os.path.exists(log)
        with open(log, 'a', newline='') as fh:
            if new:
                fh.write('session,promoted_at,mean_at_promotion\n')
            prior = session_result(name, date) or {}
            fh.write('%s,%s,%s\n' % (
                date,
                datetime.datetime.now().replace(microsecond=0).isoformat(),
                prior.get('mean', '')))

    argv = [FACEAGE, 'run', date, '--subject', name]
    if oneoff:
        argv.append('--no-log')
    JOB.start('%s %s — %s' % ('Adding to series' if promoting else 'Scoring',
                              name, date), argv)
    return {'ok': True, 'started': True, 'promoting': promoting}


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
    findings = pf.diagnose(images, baseline_luma(person))
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
    date = date or datetime.date.today().isoformat()
    listing = list_dir(browse or INBOX)
    s = {'people': people, 'person': person, 'date': date,
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
                  'baseline_luma': baseline_luma(person),
                  'preflight': do_preflight(person, date)})
    return s


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FaceAge</title>
<style>
:root{color-scheme:light dark;
  --s1:#fcfcfb;--s2:#fff;--bd:#e5e4e0;--tx:#0b0b0b;--t2:#52514e;--t3:#78766f;
  --ac:#2a78d6;--ok:#0ca30c;--wn:#b8860b;--er:#d03b3b;--wnbg:rgba(250,178,25,.10);
  --erbg:rgba(208,59,59,.09);--okbg:rgba(12,163,12,.09)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
  --s1:#1a1a19;--s2:#222221;--bd:#33322f;--tx:#fff;--t2:#c3c2b7;--t3:#8e8c83;
  --ac:#3987e5;--ok:#3fbf3f;--wn:#fab219;--er:#e8564f;--wnbg:rgba(250,178,25,.14);
  --erbg:rgba(232,86,79,.13);--okbg:rgba(63,191,63,.12)}}
*{box-sizing:border-box}
body{margin:0;background:var(--s1);color:var(--tx);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:20px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--t2);font-size:13px;margin:0 0 22px}
.card{background:var(--s2);border:1px solid var(--bd);border-radius:10px;
  padding:14px 16px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 10px;font-weight:600;
  text-transform:uppercase;letter-spacing:.05em;color:var(--t3)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--bd);
  background:var(--s1);color:var(--tx);cursor:pointer}
button:hover:not(:disabled){border-color:var(--ac)}
button.primary{background:var(--ac);border-color:var(--ac);color:#fff;font-weight:600}
button.danger{border-color:var(--er);color:var(--er)}
button.danger:hover:not(:disabled){background:var(--erbg);border-color:var(--er)}
button:disabled{opacity:.45;cursor:not-allowed}
input,select{font:inherit;padding:6px 9px;border-radius:7px;
  border:1px solid var(--bd);background:var(--s1);color:var(--tx)}
.muted{color:var(--t2);font-size:12.5px}
.dirs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:9px;max-height:110px;overflow:auto}
button.dir{padding:4px 10px;font-size:12.5px;border-radius:6px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
  font-weight:600;border:1px solid var(--bd);color:var(--t2)}
ul.files{list-style:none;margin:8px 0 0;padding:0;max-height:230px;overflow:auto}
ul.files li{display:flex;gap:8px;align-items:center;padding:4px 2px;
  border-bottom:1px solid var(--bd);font-size:13px}
ul.files li:last-child{border-bottom:0}
.fname{flex:1;font-variant-numeric:tabular-nums}
.chk{display:flex;gap:9px;align-items:flex-start;padding:7px 0;
  border-bottom:1px solid var(--bd)}
.chk:last-of-type{border-bottom:0}
.chk label{flex:1;font-size:13px}
.find{border-left:3px solid var(--bd);padding:9px 12px;margin:9px 0;border-radius:0 7px 7px 0}
.find.RESHOOT{border-color:var(--er);background:var(--erbg)}
.find.CHECK{border-color:var(--wn);background:var(--wnbg)}
.find.INFO{border-color:var(--ac)}
.find .what{font-weight:600;margin-bottom:3px}
.find .lbl{color:var(--t3);font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;margin-right:5px}
.find p{margin:3px 0;font-size:13px;color:var(--t2)}
.verdict{padding:11px 13px;border-radius:8px;font-weight:600;margin-bottom:4px}
.verdict.GOOD{background:var(--okbg);color:var(--ok)}
.verdict.CHECK{background:var(--wnbg);color:var(--wn)}
.verdict.RESHOOT{background:var(--erbg);color:var(--er)}
pre.log{background:var(--s1);border:1px solid var(--bd);border-radius:7px;
  padding:9px;font-size:12px;max-height:220px;overflow:auto;margin:8px 0 0;
  white-space:pre-wrap}
.err{color:var(--er);font-size:13px;margin-top:8px}
.warn{background:var(--wnbg);border-left:3px solid var(--wn);padding:9px 12px;
  border-radius:0 7px 7px 0;font-size:13px;margin:8px 0}
.prog{margin:4px 0 10px}
.progbar{height:8px;background:var(--bd);border-radius:99px;overflow:hidden}
.progfill{height:100%;background:var(--ac);border-radius:99px;
  transition:width .3s ease}
.progfill.indet{animation:sweep 1.1s ease-in-out infinite;transform-origin:left}
@keyframes sweep{0%{opacity:.35}50%{opacity:1}100%{opacity:.35}}
.progline{display:flex;align-items:center;gap:6px;margin-top:7px;font-size:13px;
  color:var(--t2);font-variant-numeric:tabular-nums}
.result{border:1px solid var(--bd);border-radius:9px;padding:13px 15px;margin-top:10px;
  background:var(--s1)}
.rmain{display:flex;align-items:baseline;gap:9px}
.rnum{font-size:34px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.runit{font-size:12.5px;color:var(--t2)}
.rmeta{display:flex;flex-wrap:wrap;gap:14px;margin-top:7px;font-size:12.5px;
  color:var(--t2);font-variant-numeric:tabular-nums}
.rmeta .bad{color:var(--er)}
.oneoff{display:block;margin-top:10px;font-size:12.5px;color:var(--t2)}
.notlogged{margin-top:10px;padding:11px 13px;border-radius:8px;
  border:1px dashed var(--bd);background:var(--s2);font-size:13px}
.inseries{margin-top:9px;color:var(--ok);font-weight:600;font-size:13px}
.done{margin-top:9px;font-weight:600;font-size:13px}
.done.good{color:var(--ok)} .done.bad{color:var(--er)}
details summary{cursor:pointer;font-size:12.5px;margin-top:8px}
.step{display:flex;align-items:center;gap:7px;margin-bottom:11px;font-size:12px;
  color:var(--t3);flex-wrap:wrap}
.step b{color:var(--tx)}
a{color:var(--ac)}
.note{font-size:12px;color:var(--t3);margin-top:9px;line-height:1.5}
</style></head><body><div class="wrap">
<h1>FaceAge</h1>
<p class="sub">Local only. Photographs never leave this machine.</p>
<div id="app"></div>
<p class="note" id="foot"></p>
</div>
<script>
var S = null, sel = {}, ans = {};

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
           '&browse='+encodeURIComponent(S&&S.browseDir||'');
  return api('/api/state'+qs).then(function(j){
      var keep = S && S.browseDir; S = j; if(keep) S.browseDir = keep; render();})
    .catch(function(e){
      /* Never leave the page blank. A silent failure here is indistinguishable
         from a broken build. */
      document.getElementById('app').innerHTML =
        '<div class="card"><h2>Could not load</h2><p class="err">'+h(e.message)+
        '</p><p class="muted">Check the terminal running <code>faceage app</code>.</p></div>';
    });
}

function err(m){
  var e = document.createElement('p');
  e.className='err'; e.textContent=m;
  document.getElementById('app').appendChild(e);
}

function render(){
  var a = document.getElementById('app'), o = [];
  document.getElementById('foot').textContent =
    'data: '+S.data_path+'   ·   inbox: '+S.inbox_path;

  // ---- person ----
  o.push('<div class="card"><h2>Person</h2><div class="row">');
  o.push('<select id="who">');
  S.people.forEach(function(p){
    o.push('<option value="'+h(p.name)+'"'+(p.name===S.person?' selected':'')+'>'+
           h(p.name)+' — '+p.logged+' session'+(p.logged===1?'':'s')+'</option>');});
  if(!S.people.length) o.push('<option value="">no one yet</option>');
  o.push('</select>');
  o.push('<input id="newname" placeholder="new person" size="12">');
  o.push('<button onclick="addPerson()">Add</button>');
  o.push('</div>');
  o.push('<div class="note">Each person has their own series, exposure baseline and chart. '+
         'There is no combined view: different cameras and rooms move FaceAge by more than any '+
         'real difference between two people would. Other people’s photos are their '+
         'biometric data — get their agreement, and delete their folder when done.</div>');
  o.push('</div>');

  if(!S.person){ a.innerHTML = o.join(''); wire(); return; }

  var take = (S.date||'').length > 10 ? S.date.slice(10) : '';
  o.push('<div class="step"><b>'+h(S.person)+'</b> · session <input id="date" type="date" value="'+
         h((S.date||'').slice(0,10))+'" style="padding:3px 6px">'+
         (take?' <span class="pill">take '+h(take)+'</span>':'')+' · '+
         S.staged.length+' photo'+(S.staged.length===1?'':'s')+' staged'+
         (S.scored?' · <span class="pill">scored</span>':'')+
         (S.scored?' <button onclick="reshoot()" style="padding:3px 9px;font-size:12px">Reshoot</button>':'')+
         ((S.staged.length||S.scored||S.checklist)
            ? ' <button onclick="discard()" class="danger" style="padding:3px 9px;font-size:12px">Start fresh</button>'
            : '')+
         '</div>');

  // ---- import ----
  var B = S.browse || {path:S.inbox_path, dirs:[], images:[], parent:null};
  o.push('<div class="card"><h2>1 \u00b7 Import photos</h2>');
  if(S.discardNote)
    o.push('<div class="warn">Discarded <b>'+h(S.discardNote)+'</b>. '+
           'Everything was moved to <code>discarded/</code>, not deleted. '+
           'This session is empty and ready to start over.</div>');
  if(S.movedNote)
    o.push('<div class="warn">That session had already been scored, so these '+
           'photos opened a new take: <b>'+h(S.movedNote)+'</b>. It starts with '+
           'a clean checklist and no result of its own.</div>');
  o.push('<div class="row" style="margin-bottom:8px">');
  o.push('<button onclick="goUp()"'+(B.parent?'':' disabled')+' title="parent folder">\u2191</button>');
  o.push('<input id="path" value="'+h(B.path)+'" style="flex:1;font-family:ui-monospace,monospace;font-size:12.5px">');
  o.push('<button onclick="goPath()">Go</button>');
  o.push('<button onclick="goHome()">Downloads</button>');
  o.push('</div>');

  if(B.dirs.length){
    o.push('<div class="dirs">');
    B.dirs.forEach(function(d){
      o.push('<button class="dir" data-dir="'+h(d)+'">📁 '+h(d)+'</button>');});
    o.push('</div>');
  }

  if(!B.images.length){
    o.push('<p class="muted">No images in this folder. Open a subfolder above, or type a path and press Go.</p>');
  } else {
    o.push('<ul class="files">');
    B.images.forEach(function(f){
      o.push('<li><input type="checkbox" class="ib" value="'+h(f.file)+'"'+
             (sel[f.file]?' checked':'')+'>'+
             '<span class="fname">'+h(f.file)+'</span>'+
             (f.heic?'<span class="pill">HEIC</span>':'')+
             '<span class="muted">'+h(f.when)+'</span></li>');});
    o.push('</ul>');
    o.push('<div class="row" style="margin-top:9px">');
    o.push('<button onclick="pickAll()">Select all ('+B.images.length+')</button>');
    o.push('<button onclick="pickRecent()">Select 10 most recent</button>');
    o.push('<button onclick="pickNone()">Clear</button>');
    o.push('<span style="flex:1"></span>');
    o.push('<button class="primary" onclick="doImport()">Import selected</button>');
    o.push('</div>');
    if(B.images.some(function(f){return f.heic;}))
      o.push('<div class="note">HEIC files convert to JPEG on scoring, originals kept. '+
             'Simpler is Settings \u203a Camera \u203a Formats \u203a Most Compatible, so the '+
             'phone writes JPEG and there is no second encoder in the instrument.</div>');
  }
  if(S.staged.length)
    o.push('<div class="note">Staged: '+S.staged.map(h).join(', ')+'</div>');
  o.push('</div>');

  // ---- checklist ----
  var cl = S.checklist;
  o.push('<div class="card"><h2>2 · Session checklist</h2>');
  if(cl){
    o.push('<p class="muted">Recorded '+h(cl.recorded_at)+' — '+
           (cl.valid?'<b style="color:var(--ok)">valid</b>':
                     '<b style="color:var(--er)">protocol failure</b>')+'</p>');
    if(!cl.valid) o.push('<p class="muted">Failed: '+cl.failed.map(h).join('; ')+'</p>');
    if(S.checklist_stale)
      o.push('<div class="warn">Photos were imported after this was recorded, so '+
             'it may describe a different capture.'+
             (S.scored?' This session has been scored, so it cannot be re-answered '+
                       'now — deciding validity after seeing the number is what §1 '+
                       'rules out. Shoot a fresh session instead.'
                     :' Re-answer it below if you reshot.')+'</div>');
    if(!S.scored){
      o.push('<div class="row" style="margin-top:8px">'+
             '<button onclick="reopen()">Re-answer checklist</button>'+
             '<span class="muted">allowed only while unscored</span></div>');
      if(S.reopen){
        S.checklist_items.forEach(function(it){
          var pre = prefill && prefill[it.key];
          o.push('<div class="chk"><input type="checkbox" class="cl" id="cl_'+h(it.key)+
                 '" value="'+h(it.key)+'"'+(pre?' checked':'')+
                 '><label for="cl_'+h(it.key)+'">'+h(it.label)+
                 '</label></div>');});
        o.push('<div class="row" style="margin-top:9px">');
        o.push('<input id="clnotes" placeholder="notes (optional)" style="flex:1">');
        o.push('<button class="primary" onclick="saveChecklist()">Record</button></div>');
      }
    }
  } else if(S.scored){
    o.push('<p class="muted">This session was already scored without a checklist. '+
           'Recording one now would be deciding after seeing the number.</p>');
  } else {
    o.push('<p class="muted">Answer before scoring. Unchecking an item records a '+
           'protocol failure with that reason — the session still gets scored, '+
           'but is excluded from the analysis.</p>');
    S.checklist_items.forEach(function(it){
      var pre = prefill && prefill[it.key];
      o.push('<div class="chk"><input type="checkbox" class="cl" id="cl_'+h(it.key)+
             '" value="'+h(it.key)+'"'+(pre?' checked':'')+
             '><label for="cl_'+h(it.key)+'">'+h(it.label)+'</label></div>');});
    o.push('<div class="row" style="margin-top:9px">');
    if(S.prev_checklist)
      o.push('<button onclick="sameAsLast()">Same as '+h(S.prev_checklist.session_date)+'</button>');
    o.push('<input id="clnotes" placeholder="notes (optional)" style="flex:1">');
    o.push('<button class="primary" onclick="saveChecklist()">Record</button></div>');
    o.push('<div class="note">Ticking copies last session\u2019s answers so you can '+
           'review them rather than retype them \u2014 they are still yours to submit.</div>');
  }
  o.push('</div>');

  // ---- score ----
  o.push('<div class="card"><h2>3 \u00b7 Score</h2>');
  var j = S.job, blocked = !S.staged.length, pr = (j && j.progress) || {};
  if(j.running){
    var pct = pr.total ? pr.pct : null;
    o.push('<div class="prog">');
    o.push('<div class="progbar"><div class="progfill'+(pct===null?' indet':'')+
           '" style="width:'+(pct===null?100:pct)+'%"></div></div>');
    o.push('<div class="progline"><b>'+h(pr.phase||'Working')+'</b>'+
           (pr.total?(' &middot; '+pr.done+' of '+pr.total):'')+
           '<span style="flex:1"></span>'+
           (j.eta!=null?('~'+fmtSecs(j.eta)+' left &middot; '):'')+
           fmtSecs(Math.round(j.elapsed))+' elapsed</div>');
    o.push('</div>');
    o.push(details(j.log));
  } else {
    o.push('<div class="row"><button class="primary" id="scorebtn"'+
           (blocked?' disabled':'')+' onclick="score()">'+
           (S.oneoff?'Score only':'Score and add to series')+'</button>');
    if(blocked) o.push('<span class="muted">Import photos first.</span>');
    else if(!cl && !S.oneoff) o.push('<span class="muted">Needs the checklist.</span>');
    o.push('</div>');
    o.push('<label class="oneoff"><input type="checkbox" id="oneoff"'+
           (S.oneoff?' checked':'')+'> Just tell me the number \u2014 do not add '+
           'this to the series</label>');
    o.push('<div class="note">'+(S.oneoff
        ? 'Nothing is written to the series. You will still see the number, and '+
          'you can add it afterwards \u2014 this is not a one-way door.'
        : 'This writes a row to the series and it appears on the chart. Whether '+
          'the pre-registered analysis counts it is decided by the checklist, '+
          'before the number exists \u2014 not afterwards.')+'</div>');
    var R = S.result;
    if(R && R.mean != null){
      o.push('<div class="result">');
      o.push('<div class="rmain"><span class="rnum">'+R.mean.toFixed(2)+'</span>'+
             '<span class="runit">FaceAge, session mean</span></div>');
      o.push('<div class="rmeta">');
      o.push('<span><b>'+R.n+'</b> of '+R.n_total+' photos used</span>');
      if(R.std!=null) o.push('<span>SD <b>'+R.std.toFixed(2)+'</b></span>');
      if(R.min!=null&&R.max!=null)
        o.push('<span>range '+R.min.toFixed(1)+'\u2013'+R.max.toFixed(1)+'</span>');
      if(R.luma!=null){
        var lt = 'exposure <b>'+R.luma.toFixed(1)+'</b>';
        if(R.luma_delta!=null)
          lt += ' ('+(R.luma_delta>0?'+':'')+R.luma_delta+' vs baseline)';
        o.push('<span class="'+(R.luma_ok===false?'bad':'')+'">'+lt+'</span>');
      }
      o.push('</div>');
      if(!R.logged){
        o.push('<div class="notlogged">');
        o.push('<div><b>Not in the series.</b> This was a one-off \u2014 nothing '+
               'was written to the tracker.</div>');
        if(R.has_checklist)
          o.push('<button class="primary" style="margin-top:9px" onclick="promote()">'+
                 'Add this session to the series</button>');
        else
          o.push('<div class="muted" style="margin-top:7px">Answer the checklist '+
                 'above first \u2014 a session joins the series with its validity '+
                 'recorded, not without it.</div>');
        o.push('</div>');
      } else if(!R.valid)
        o.push('<div class="warn" style="margin:9px 0 0">Recorded as a protocol '+
               'failure, so this number is <b>not in the series</b> and is excluded '+
               'from the analysis'+(R.excluded_reason?': '+h(R.excluded_reason):'')+'.</div>');
      else
        o.push('<div class="inseries">\u2713 In the series</div>');
      if(R.fellback)
        o.push('<div class="warn" style="margin:9px 0 0">Every photo carried a QA '+
               'advisory, so the mean uses all scored photos. Check the capture '+
               'before trusting it.</div>');
      o.push('<div class="note">A single session is one point. §3 interprets the '+
             'fitted trend across sessions, never the gap between two of them.</div>');
      o.push('</div>');
    }
    if(j.log.length){
      var ok = (j.rc===0);
      o.push('<div class="done '+(ok?'good':'bad')+'">'+
             (ok?'\u2713 Finished':'\u2717 Failed (exit '+j.rc+')')+'</div>');
      o.push(details(j.log));
    }
  }
  o.push('</div>');

  // ---- preflight ----
  var pfd = S.preflight || {};
  o.push('<div class="card"><h2>4 · Pre-flight — is this capture usable?</h2>');
  if(!pfd.available){
    o.push(pfd.stale ? '<div class="warn">'+h(pfd.why||'')+'</div>'
                     : '<p class="muted">'+h(pfd.why||'')+'</p>');
  } else {
    o.push('<div class="verdict '+h(pfd.verdict)+'">'+h(pfd.verdict)+' — '+
           h(pfd.verdict_text)+'</div>');
    if(!pfd.findings.length) o.push('<p class="muted">No findings.</p>');
    pfd.findings.forEach(function(f){
      o.push('<div class="find '+h(f.severity)+'">');
      o.push('<div class="what">'+h(f.what)+'</div>');
      o.push('<p><span class="lbl">why</span>'+h(f.why)+'</p>');
      o.push('<p><span class="lbl">do</span>'+h(f.do)+'</p>');
      if(f.frames && f.frames.length)
        o.push('<p class="muted">'+h(f.frames.slice(0,6).join(', '))+
               (f.frames.length>6?', +'+(f.frames.length-6)+' more':'')+'</p>');
      o.push('</div>');});
    o.push('<div class="note">No photograph is ever modified. When a check fails the '+
           'fix is the lighting or the rig, never the file — adjusting exposure in '+
           'software would hide the error instead of removing it.</div>');
  }
  o.push('</div>');

  // ---- chart ----
  o.push('<div class="card"><h2>5 · Series</h2><div class="row">'+
         '<a href="/tracker?person='+encodeURIComponent(S.person)+
         '" target="_blank"><button>Open chart</button></a>'+
         '<span class="muted">trend, CI, exposure trace</span></div></div>');

  a.innerHTML = o.join('');
  wire();
}

function wire(){
  var w = document.getElementById('who');
  if(w) w.onchange = function(){ S.person = w.value; sel = {}; load(); };
  var d = document.getElementById('date');
  if(d) d.onchange = function(){
    S.date = d.value; S.prefill = null; S.reopen = false; sel = {}; load(); };
  document.querySelectorAll('.ib').forEach(function(c){
    c.onchange = function(){ sel[c.value] = c.checked; };});
  var oo = document.getElementById('oneoff');
  if(oo) oo.onchange = function(){ S.oneoff = oo.checked; render(); };
  var det = document.getElementById('logdet');
  if(det) det.addEventListener('toggle', function(){ logOpen = det.open; });
  document.querySelectorAll('button.dir').forEach(function(b){
    b.onclick = function(){
      var base = S.browse.path;
      browseTo(base + (base.charAt(base.length-1)==='/'?'':'/') + b.getAttribute('data-dir'));
    };});
}

function imgs(){ return (S.browse && S.browse.images) || []; }
function pickRecent(){ sel={}; imgs().slice(0,10).forEach(function(f){sel[f.file]=true;}); render(); }
function pickAll(){ sel={}; imgs().forEach(function(f){sel[f.file]=true;}); render(); }
function pickNone(){ sel={}; render(); }
function reopen(){ S.reopen = true; render(); }
function sameAsLast(){
  S.prefill = (S.prev_checklist && S.prev_checklist.answers) || null;
  render();
}
function discard(){
  var scored = S.scored, hasB = S.has_b;
  var msg = 'Throw away session '+S.date+' completely?\\n\\n'+
            'Photos, checklist, score, QA and its rows in the series are moved '+
            'to discarded/ and the session starts over empty.';
  if(scored && hasB)
    msg += '\\n\\nThis session HAS BEEN SCORED and B is set, so this removes a '+
           'point from the study series. The discard is logged with your reason.';
  else if(scored)
    msg += '\\n\\nIt has been scored, but B is not set yet, so nothing has '+
           'entered the study series.';
  if(!confirm(msg)) return;
  var reason = prompt('Why? (recorded in discarded.csv)', '') || '';
  api('/api/discard', {person:S.person, date:S.date, reason:reason})
    .then(function(j){
      S.prefill=null; S.reopen=false; S.movedNote=null; sel={};
      S.discardNote = j.session + ' \u2014 ' + j.removed.join(', ');
      load();
    }).catch(function(e){ err(e.message); });
}

function promote(){
  api('/api/score', {person:S.person, date:S.date, oneoff:false, promote:true})
    .then(function(){ poll(); }).catch(function(e){ err(e.message); });
}

function reshoot(){
  api('/api/reshoot', {person:S.person, date:S.date}).then(function(j){
    S.date = j.session; S.prefill = null; S.reopen = false; sel = {}; load();
  }).catch(function(e){ err(e.message); });
}

function fmtSecs(n){
  if(n==null) return '';
  if(n < 60) return n+'s';
  var m = Math.floor(n/60), r = n%60;
  return m+'m'+(r?' '+r+'s':'');
}

/* render() rebuilds the DOM on every poll, which would slam <details> shut the
   instant it was clicked. The open state lives outside the markup and is
   reapplied, and the toggle writes back to it. */
var logOpen = false;
function details(lines){
  return '<details id="logdet"'+(logOpen?' open':'')+
         '><summary class="muted">Details</summary>'+
         '<pre class="log">'+h(lines.join('\\n'))+'</pre></details>';
}

function browseTo(p){ S.browseDir=p; sel={}; load(); }
function goPath(){ browseTo(document.getElementById('path').value.trim()); }
function goUp(){ if(S.browse && S.browse.parent) browseTo(S.browse.parent); }
function goHome(){ browseTo(S.inbox_path); }

function addPerson(){
  var n = document.getElementById('newname').value.trim();
  if(!n) return;
  api('/api/person', {name:n}).then(function(){ S.person=n; sel={}; load(); })
    .catch(function(e){ err(e.message); });
}

function doImport(){
  var files = Object.keys(sel).filter(function(k){return sel[k];});
  if(!files.length){ err('Select some photos first.'); return; }
  api('/api/import', {person:S.person, date:S.date, files:files,
                      dir:(S.browse&&S.browse.path)||S.inbox_path})
    .then(function(j){
      sel = {};
      if(j.moved_to_new_take){
        S.date = j.session; S.prefill = null; S.reopen = false;
        S.movedNote = j.moved_to_new_take;
      }
      load();
    })
    .catch(function(e){ err(e.message); });
}

function saveChecklist(){
  var a = {};
  document.querySelectorAll('.cl').forEach(function(c){ a[c.value]=c.checked; });
  api('/api/checklist', {person:S.person, date:S.date, answers:a,
                         notes:(document.getElementById('clnotes')||{}).value||''})
    .then(load).catch(function(e){ err(e.message); });
}

function score(){
  api('/api/score', {person:S.person, date:S.date, oneoff:!!S.oneoff})
    .then(function(){ poll(); }).catch(function(e){ err(e.message); });
}

function poll(){
  api('/api/job').then(function(j){
    S.job = j; render();
    if(j.running) setTimeout(poll, 900); else load();
  });
}

load();
</script></body></html>"""


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

ROUTES_POST = {
    '/api/person': do_create_person,
    '/api/import': do_import,
    '/api/checklist': do_checklist,
    '/api/score': do_score,
    '/api/notes': do_notes,
    '/api/reshoot': do_reshoot,
    '/api/discard': do_discard,
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
            if n > 1 << 20:
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
