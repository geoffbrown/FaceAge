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
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.heic', '.heif')

sys.path.insert(0, HERE)
import faceage_preflight as pf          # noqa: E402

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
    if not d or not DATE_RE.match(d):
        raise ValueError('invalid session date (YYYY-MM-DD)')
    try:
        datetime.date.fromisoformat(d)
    except ValueError:
        raise ValueError('invalid session date')
    return d


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

class Job(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.label = ''
        self.log = []
        self.rc = None
        self.started = None

    def snapshot(self):
        with self.lock:
            return {'running': self.running, 'label': self.label,
                    'rc': self.rc, 'log': self.log[-400:],
                    'elapsed': (time.time() - self.started) if self.started else 0}

    def start(self, label, argv, cwd=None):
        with self.lock:
            if self.running:
                raise RuntimeError('a job is already running')
            self.running, self.label, self.log, self.rc = True, label, [], None
            self.started = time.time()

        def run():
            try:
                p = subprocess.Popen(argv, cwd=cwd or REPO, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     bufsize=1, env=dict(os.environ,
                                                         FACEAGE_REPO=REPO,
                                                         FACEAGE_DATA=DATA))
                for line in p.stdout:
                    with self.lock:
                        self.log.append(line.rstrip('\n'))
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
    if not files:
        raise ValueError('no photos selected')

    dest = session_dir(name, date)
    os.makedirs(dest, exist_ok=True)
    copied, skipped = [], []
    for f in files:
        if os.path.basename(f) != f or f.startswith('.'):
            raise ValueError('bad filename: %s' % f)
        if not f.lower().endswith(IMAGE_EXTS):
            raise ValueError('not an image: %s' % f)
        src = os.path.join(INBOX, f)
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

    failed = [label for key, label in CHECKLIST if not answers.get(key)]
    doc = {'session_date': date, 'person': name,
           'recorded_at': datetime.datetime.now().replace(microsecond=0).isoformat(),
           'answers': {k: bool(answers.get(k)) for k, _ in CHECKLIST},
           'failed': failed, 'notes': notes,
           'valid': not failed}

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
    return {'ok': True, 'checklist': doc}


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

    argv = [FACEAGE, 'run', date, '--subject', name]
    if oneoff:
        argv.append('--no-log')
    JOB.start('Scoring %s — %s%s' % (name, date, ' (one-off)' if oneoff else ''),
              argv)
    return {'ok': True, 'started': True}


def do_preflight(person, date):
    per_image = os.path.join(results_dir(person), '%s_per_image.csv' % safe_date(date))
    if not os.path.exists(per_image):
        return {'available': False,
                'why': 'Pre-flight reads the per-image QA the pipeline writes, '
                       'so it becomes available once this session has been run.'}
    images = pf.load_per_image(per_image)
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


def state(person=None, date=None):
    people = list_people()
    if person is None and people:
        person = people[0]['name']
    date = date or datetime.date.today().isoformat()
    s = {'people': people, 'person': person, 'date': date,
         'inbox_path': INBOX, 'inbox': scan_inbox(), 'data_path': DATA,
         'checklist_items': [{'key': k, 'label': l} for k, l in CHECKLIST],
         'job': JOB.snapshot()}
    if person:
        s.update({'staged': staged(person, date),
                  'checklist': read_checklist(person, date),
                  'scored': session_scored(person, date),
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
button:disabled{opacity:.45;cursor:not-allowed}
input,select{font:inherit;padding:6px 9px;border-radius:7px;
  border:1px solid var(--bd);background:var(--s1);color:var(--tx)}
.muted{color:var(--t2);font-size:12.5px}
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
           '&date='+encodeURIComponent(S&&S.date||'');
  return api('/api/state'+qs).then(function(j){S=j; render();});
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
  o.push('<span style="flex:1"></span>');
  o.push('<label class="muted"><input type="checkbox" id="oneoff"> one-off (score only, no series)</label>');
  o.push('</div>');
  o.push('<div class="note">Each person has their own series, exposure baseline and chart. '+
         'There is no combined view: different cameras and rooms move FaceAge by more than any '+
         'real difference between two people would. Other people’s photos are their '+
         'biometric data — get their agreement, and delete their folder when done.</div>');
  o.push('</div>');

  if(!S.person){ a.innerHTML = o.join(''); wire(); return; }

  o.push('<div class="step"><b>'+h(S.person)+'</b> · session <input id="date" type="date" value="'+
         h(S.date)+'" style="padding:3px 6px"> · '+
         S.staged.length+' photo'+(S.staged.length===1?'':'s')+' staged'+
         (S.scored?' · <span class="pill">scored</span>':'')+'</div>');

  // ---- import ----
  o.push('<div class="card"><h2>1 · Import from '+h(S.inbox_path)+'</h2>');
  if(!S.inbox.length){
    o.push('<p class="muted">No images found. AirDrop from your phone, then reload.</p>');
  } else {
    o.push('<ul class="files">');
    S.inbox.forEach(function(f){
      o.push('<li><input type="checkbox" class="ib" value="'+h(f.file)+'"'+
             (sel[f.file]?' checked':'')+'>'+
             '<span class="fname">'+h(f.file)+'</span>'+
             (f.heic?'<span class="pill">HEIC</span>':'')+
             '<span class="muted">'+h(f.when)+'</span></li>');});
    o.push('</ul>');
    o.push('<div class="row" style="margin-top:9px">');
    o.push('<button onclick="pickRecent()">Select 10 most recent</button>');
    o.push('<button class="primary" onclick="doImport()">Import selected</button>');
    o.push('</div>');
    if(S.inbox.some(function(f){return f.heic;}))
      o.push('<div class="note">HEIC files convert to JPEG on scoring, originals kept. '+
             'Simpler is Settings › Camera › Formats › Most Compatible, so the '+
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
  } else if(S.scored){
    o.push('<p class="muted">This session was already scored without a checklist. '+
           'Recording one now would be deciding after seeing the number.</p>');
  } else {
    o.push('<p class="muted">Answer before scoring. Unchecking an item records a '+
           'protocol failure with that reason — the session still gets scored, '+
           'but is excluded from the analysis.</p>');
    S.checklist_items.forEach(function(it){
      o.push('<div class="chk"><input type="checkbox" class="cl" id="cl_'+h(it.key)+
             '" value="'+h(it.key)+'"><label for="cl_'+h(it.key)+'">'+h(it.label)+'</label></div>');});
    o.push('<div class="row" style="margin-top:9px">');
    o.push('<input id="clnotes" placeholder="notes (optional)" style="flex:1">');
    o.push('<button class="primary" onclick="saveChecklist()">Record</button></div>');
  }
  o.push('</div>');

  // ---- score ----
  o.push('<div class="card"><h2>3 · Score</h2>');
  var j = S.job, blocked = !S.staged.length;
  if(j.running){
    o.push('<p class="muted">'+h(j.label)+' — '+Math.round(j.elapsed)+'s</p>');
    o.push('<pre class="log">'+h(j.log.join('\n'))+'</pre>');
  } else {
    o.push('<div class="row"><button class="primary" id="scorebtn"'+
           (blocked?' disabled':'')+' onclick="score()">Score session</button>');
    if(blocked) o.push('<span class="muted">Import photos first.</span>');
    else if(!cl) o.push('<span class="muted">Needs the checklist, unless one-off.</span>');
    o.push('</div>');
    if(j.log.length) o.push('<pre class="log">'+h(j.log.join('\n'))+'</pre>');
  }
  o.push('</div>');

  // ---- preflight ----
  var pfd = S.preflight || {};
  o.push('<div class="card"><h2>4 · Pre-flight — is this capture usable?</h2>');
  if(!pfd.available){
    o.push('<p class="muted">'+h(pfd.why||'')+'</p>');
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
  if(d) d.onchange = function(){ S.date = d.value; sel = {}; load(); };
  document.querySelectorAll('.ib').forEach(function(c){
    c.onchange = function(){ sel[c.value] = c.checked; };});
}

function pickRecent(){
  sel = {};
  S.inbox.slice(0,10).forEach(function(f){ sel[f.file]=true; });
  render();
}

function addPerson(){
  var n = document.getElementById('newname').value.trim();
  if(!n) return;
  api('/api/person', {name:n}).then(function(){ S.person=n; sel={}; load(); })
    .catch(function(e){ err(e.message); });
}

function doImport(){
  var files = Object.keys(sel).filter(function(k){return sel[k];});
  if(!files.length){ err('Select some photos first.'); return; }
  api('/api/import', {person:S.person, date:S.date, files:files})
    .then(function(){ sel={}; load(); })
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
  var one = (document.getElementById('oneoff')||{}).checked || false;
  api('/api/score', {person:S.person, date:S.date, oneoff:one})
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
}


class Handler(BaseHTTPRequestHandler):
    server_version = 'faceage'

    def log_message(self, fmt, *args):        # quiet; this is a local tool
        pass

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
                                        q.get('date') or None))
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
