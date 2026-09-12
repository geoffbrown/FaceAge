#!/usr/bin/env python3
"""Actually run the page's JavaScript.

node --check only PARSES. It cannot see an undefined variable, which is how
`Can't find variable: prefill` reached the browser: a patch added two uses of a
local while the line declaring it silently failed to match, and every existing
test still passed.

This executes the script in node against stub DOM/fetch objects and calls
render() over a range of states. Any reference error, any thrown exception,
fails the build. Skipped when node is unavailable.

    python3 tools/test_faceage_render.py
"""
import os
import re
import sys
import json
import shutil
import tempfile
import subprocess
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
// --- minimal DOM, enough for render() and wire() ---------------------------
function El(tag){
  this.tag = tag; this.children = []; this._html = '';
  this.style = {}; this.attrs = {}; this.checked = false; this.value = '';
  this.open = false; this.textContent = '';
}
El.prototype.appendChild = function(c){ this.children.push(c); return c; };
El.prototype.addEventListener = function(){};
El.prototype.setAttribute = function(k,v){ this.attrs[k]=v; };
El.prototype.getAttribute = function(k){ return this.attrs[k]; };
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function(){ return this._html; },
  set: function(v){ this._html = String(v); }
});

var ELS = {};
var document = {
  createElement: function(t){ return new El(t); },
  getElementById: function(id){
    if(!ELS[id]) ELS[id] = new El('div');
    return ELS[id];
  },
  querySelectorAll: function(){ return []; },
  querySelector: function(){ return null; }
};
var location = { protocol: 'http:' };
var window = {};
var alert = function(){};
var confirm = function(){ return false; };
var prompt = function(){ return ''; };
var setTimeout = function(){};
var fetch = function(){
  return Promise.resolve({ ok: true, json: function(){ return Promise.resolve({}); } });
};

__SCRIPT__

// --- drive it --------------------------------------------------------------
var STATES = __STATES__;
var failures = [];
STATES.forEach(function(st, i){
  try {
    S = st.state;
    sel = {};
    render();
    var out = document.getElementById('app').innerHTML;
    // the wizard renders into three containers; check all of them
  out = document.getElementById('app').innerHTML +
        document.getElementById('rail').innerHTML +
        document.getElementById('who').innerHTML +
        document.getElementById('sess').innerHTML;
  if(typeof out !== 'string' || out.length < 20)
      failures.push(st.name + ': rendered almost nothing (' +
                    (out ? out.length : 0) + ' chars)');
    (st.expect || []).forEach(function(sub){
      if(out.indexOf(sub) === -1)
        failures.push(st.name + ': expected to contain ' + JSON.stringify(sub));
    });
    (st.reject || []).forEach(function(sub){
      if(out.indexOf(sub) !== -1)
        failures.push(st.name + ': should NOT contain ' + JSON.stringify(sub));
    });
  } catch (e) {
    failures.push(st.name + ': ' + (e && e.message ? e.message : String(e)));
  }
});
if(failures.length){ console.log('FAIL\n' + failures.join('\n')); process.exit(1); }
console.log('OK');
"""


def base_state(**kw):
    s = {
        'people': [{'name': 'me', 'sessions': 1, 'logged': 1}],
        'person': 'me', 'date': '2026-09-12', 'today': '2026-09-12',
        'inbox_path': '/Users/x/Downloads', 'data_path': '/Users/x/FaceAgeData',
        'browse': {'path': '/Users/x/Downloads', 'parent': '/Users/x',
                   'dirs': ['pics'],
                   'images': [{'file': 'IMG_1.jpg', 'when': '12 Sep 10:00',
                               'heic': False, 'size': 10, 'mtime': 1}],
                   'n_images': 1},
        'inbox': [], 'staged': [],
        'checklist_items': [{'key': 'light', 'label': 'Frontal light'},
                            {'key': 'pose', 'label': 'Neutral expression'}],
        'checklist': None, 'checklist_stale': False, 'prev_checklist': None,
        'scored': False, 'result': None, 'baseline_luma': 121.3,
        'has_b': False,
        'preflight': {'available': False, 'why': 'not yet'},
        'job': {'running': False, 'label': '', 'rc': None, 'log': [],
                'progress': {'phase': None, 'done': 0, 'total': 0, 'pct': 0},
                'eta': None, 'elapsed': 0},
    }
    s.update(kw)
    return s


CHECKLIST_DONE = {'session_date': '2026-09-12', 'recorded_at': '2026-09-12T10:00',
                  'valid': True, 'failed': [], 'answers': {'light': True}}
CHECKLIST_FAIL = {'session_date': '2026-09-12', 'recorded_at': '2026-09-12T10:00',
                  'valid': False, 'failed': ['Frontal light'],
                  'answers': {'light': False}}
RESULT = {'mean': 45.62, 'median': 45.5, 'std': 1.39, 'n': 14, 'n_total': 14,
          'n_failed': 0, 'n_flagged': 14, 'min': 43.3, 'max': 48.4,
          'luma': 133.7, 'baseline_luma': 128.6, 'luma_delta': 5.1,
          'luma_ok': False, 'fellback': True, 'valid': True, 'logged': False,
          'in_series': False, 'has_checklist': True, 'excluded_reason': None}
RUNNING = {'running': True, 'label': 'Analysing', 'rc': None,
           'log': ['(3/10) Running the face localization step'],
           'progress': {'phase': 'Finding faces', 'done': 3, 'total': 10, 'pct': 30.0},
           'eta': 42, 'elapsed': 18}
PREFLIGHT_BAD = {'available': True, 'n_frames': 14, 'verdict': 'RESHOOT',
                 'verdict_text': 'Reshoot before scoring.',
                 'findings': [{'severity': 'RESHOOT', 'code': 'LUMA_DRIFT',
                               'what': 'exposure 133.7', 'why': 'lighting',
                               'do': 'reshoot', 'frames': ['a.jpg', 'b.jpg']}]}
PREFLIGHT_GOOD = {'available': True, 'n_frames': 10, 'verdict': 'GOOD',
                  'verdict_text': 'Capture looks good.', 'findings': []}


def states():
    """One entry per wizard step and per branch that has bitten, with
    assertions on what the step must and must not show."""
    return [
        {'name': 'step 0: no people', 'state': base_state(people=[], person=None),
         'expect': ['Who are we measuring', 'Add'], 'reject': ['Continue</button>']},
        {'name': 'step 0: pick a person', 'state': base_state(person=None),
         'expect': ['Who are we measuring', 'Continue']},
        # first-ever session: the shooting guide is open by default
        {'name': 'step 1: conditions', 'state': base_state(),
         'expect': ['Same setup as last time', 'id="c_light"', 'Continue',
                    '<details class="guide" open>', 'How to set up the shot',
                    'Selfie camera is fine', 'AE/AF LOCK'],
         'reject': ['Pick the photos', 'Analyse</button>']},
        # later sessions: still there, collapsed
        {'name': 'step 1: conditions, returning',
         'state': base_state(prev_checklist=CHECKLIST_DONE),
         'expect': ['<details class="guide">'],
         'reject': ['<details class="guide" open>']},
        {'name': 'step 1: with prefill offered',
         'state': base_state(prev_checklist=CHECKLIST_DONE),
         'expect': ['Same as ']},
        {'name': 'step 2: photos', 'state': base_state(checklist=CHECKLIST_DONE),
         'expect': ['Pick the photos', 'Use 0 selected', 'Conditions: <b>all good',
                    'How to set up the shot'],
         'reject': ['Analyse</button>']},
        {'name': 'step 2: photos, conditions flagged',
         'state': base_state(checklist=CHECKLIST_FAIL),
         'expect': ['Conditions: <b>problem noted']},
        {'name': 'step 2: empty folder',
         'state': base_state(checklist=CHECKLIST_DONE,
                             browse={'path': '/x', 'parent': None, 'dirs': [],
                                     'images': [], 'n_images': 0}),
         'expect': ['No photos here']},
        {'name': 'step 3: ready to analyse',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg', 'b.jpg']),
         'expect': ['Ready to analyse', 'Analyse</button>', 'Photos: <b>2 selected'],
         'reject': ['Add to my tracker']},
        {'name': 'step 4: running',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'], job=RUNNING),
         'expect': ['Analysing', 'Finding faces', '3 of 10', '30%', 'about 42s left'],
         'reject': ['Add to my tracker']},
        {'name': 'step 4: running, no counts yet',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             job=dict(RUNNING, log=[], eta=None,
                                      progress={'phase': None, 'done': 0,
                                                'total': 0, 'pct': 0})),
         'expect': ['Analysing', 'indet']},
        {'name': 'step 5: result, good capture',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=dict(RESULT, luma_ok=True, fellback=False),
                             preflight=PREFLIGHT_GOOD),
         'expect': ['Good capture', '45.6', 'Add to my tracker', 'Discard this session',
                    'clean session'],
         'reject': ['Analyse</button>', 'Reshoot recommended']},
        {'name': 'step 5: result, reshoot recommended',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=RESULT, preflight=PREFLIGHT_BAD),
         'expect': ['Reshoot recommended', 'We would skip this one',
                    'exposure 133.7', 'Add to my tracker']},
        {'name': 'step 5: result with flagged conditions',
         'state': base_state(checklist=CHECKLIST_FAIL, staged=['a.jpg'],
                             result=dict(RESULT, valid=False,
                                         excluded_reason='Frontal light'),
                             preflight=PREFLIGHT_GOOD),
         'expect': ['stay out of your trend line']},
        {'name': 'step 5: no result produced',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result={'mean': None, 'n': 0},
                             job=dict(RUNNING, running=False, rc=0)),
         'expect': ['did not produce a result', 'Start over']},
        {'name': 'done: added to tracker',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'], scored=True,
                             result=dict(RESULT, logged=True, in_series=True)),
         'expect': ['Added to your tracker', 'View tracker', 'Start a new session'],
         'reject': ['Add to my tracker']},
        {'name': 'done: added but flagged',
         'state': base_state(checklist=CHECKLIST_FAIL, staged=['a.jpg'], scored=True,
                             result=dict(RESULT, logged=True, valid=False)),
         'expect': ['kept out of the trend line']},
        {'name': 'take letter in header', 'state': base_state(date='2026-09-12b'),
         'expect': ['take b']},
    ]


class TestCopy(unittest.TestCase):
    """UI chrome should read like instructions, not like the protocol document.

    The findings panel is exempt: its why/do text comes from the server and
    earns its detail. This covers the labels and explanations around it.
    """

    def script(self):
        import faceage_web
        return re.search(r'<script>(.*?)</script>', faceage_web.PAGE, re.S).group(1)

    def test_no_section_references(self):
        """A user should never have to know what §1 is to use the page."""
        js = self.script()
        bad = [l.strip()[:80] for l in js.split('\n') if '\u00a7' in l]
        self.assertEqual(bad, [], 'section references in UI copy: %s' % bad)

    def test_no_methodology_jargon(self):
        js = self.script()
        for term in ('pre-registered', 'protocol failure', 'validity recorded',
                     'one-way door', 'the study series', 'in the series'):
            hits = [l.strip()[:80] for l in js.split('\n') if term in l]
            self.assertEqual(hits, [], '%r in UI copy: %s' % (term, hits))


class TestRender(unittest.TestCase):
    def test_render_runs_for_every_state(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not available')
        import faceage_web
        js = re.search(r'<script>(.*?)</script>', faceage_web.PAGE, re.S)
        self.assertIsNotNone(js, 'no script block in PAGE')
        script = js.group(1)
        # load() runs on parse and would fire a fetch; the harness drives
        # render() directly instead.
        script = script.replace('\nload();', '\n')

        harness = (HARNESS
                   .replace('__SCRIPT__', script)
                   .replace('__STATES__', json.dumps(states())))
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
            fh.write(harness)
            path = fh.name
        try:
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(r.returncode, 0,
                         'render() failed:\n%s\n%s' % (r.stdout, r.stderr))


if __name__ == '__main__':
    unittest.main(verbosity=2)
