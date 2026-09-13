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
// a camera that never answers: enough to render the camera card
var navigator = { userAgent: 'harness',
  mediaDevices: { getUserMedia: function(){ return new Promise(function(){}); } } };
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
var UI_DEFAULT = JSON.parse(JSON.stringify(ui));
STATES.forEach(function(st, i){
  try {
    S = st.state;
    sel = {};
    ui = Object.assign({}, UI_DEFAULT, st.ui || {});
    render();
    var out = document.getElementById('app').innerHTML;
    // the wizard renders into three containers; check all of them
  out = document.getElementById('app').innerHTML +
        document.getElementById('tabs').innerHTML +
        document.getElementById('who').innerHTML;
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
        'has_b': False, 'capture': None, 'source': None, 'series_source': None,
        'baseline': None, 'baseline_camera': None, 'fill_calibration': None,
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
         'expect': ['Who are we measuring', 'Add'], 'reject': ['Continue</button>', 'New session', 'Rename']},
        {'name': 'step 0: choosing', 'state': base_state(people=[{'name': 'me', 'sessions': 1, 'logged': 1},
                                                                 {'name': 'jackie', 'sessions': 0, 'logged': 0}]),
         'ui': {'choosing': True},
         'expect': ['Who are we measuring', 'class="people"', '1 in tracker · current', 'Continue</button>',
                    "renamePerson('jackie')", "pickPerson('jackie')", 'Choose</button>',
                    'class="pillbtn" onclick="changePerson()">Change'],
         'reject': ['<select', 'rename</button>']},
        # one person: straight to the camera, no who step, no conditions step
        {'name': 'capture: first ever session',
         'state': base_state(people=[{'name': 'me', 'sessions': 0, 'logged': 0}]),
         'expect': ['Take the photos', 'id="cam"', 'This is your baseline',
                    'Anything different today?', 'data-flag="shave"', 'data-flag="other"',
                    'New session', 'Progress', 'Capture', 'Import them'],
         'reject': ['Same setup as last time', 'id="c_light"', 'Use 0 selected']},
        {'name': 'capture: returning', 'state': base_state(),
         'expect': ['Take the photos', 'Anything different today?'],
         'reject': ['This is your baseline']},
        {'name': 'capture: a flag is on', 'state': base_state(),
         'ui': {'flags': {'shave': True, 'other': True}, 'flagNote': 'new glasses'},
         'expect': ['class="flag on" data-flag="shave"', 'Something else: new glasses']},
        {'name': 'capture: import mode', 'state': base_state(),
         'ui': {'photoMode': 'import'},
         'expect': ['Import photos from your phone', 'Use 0 selected', 'Use this Mac',
                    'Anything different today?', 'How to set up the shot'],
         'reject': ['id="cam"', 'Analyse</button>']},
        {'name': 'capture: camera mode, baseline with live readings taken the same way',
         'state': base_state(baseline_camera={'luma': 173.3, 'live_luma': 101.5, 'live_method': 'box-rgb-2',
                                              'date': '2026-09-10', 'source': 'mac-camera'}),
         'expect': ['Brightness baseline for this camera: <b>102</b>', 'sets its own exposure'],
         'reject': ['<b>173</b>']},
        {'name': 'capture: camera mode, baseline measured the old way',
         'state': base_state(baseline_camera={'luma': 173.3, 'live_luma': 104.0, 'live_method': None,
                                              'date': '2026-09-10', 'source': 'mac-camera'}),
         'expect': ['not checked against it live'],
         'reject': ['<b>104</b>', '<b>173</b>']},
        {'name': 'capture: camera mode, pipeline number only',
         'state': base_state(baseline_camera={'luma': 121.3, 'date': '2026-09-10', 'source': 'mac-camera'}),
         'expect': ['not checked against it live']},
        {'name': 'capture: camera mode, phone baseline only',
         'state': base_state(baseline={'luma': 128.6, 'date': '2026-09-06', 'source': 'import'}),
         'expect': ['No brightness baseline for this camera yet'],
         'reject': ['<b>129</b>', 'Sep 6']},
        {'name': 'capture: camera mode, phone rehearsals, no anchor',
         'state': base_state(series_source='import', has_b=False),
         'expect': ['Those were rehearsals'], 'reject': ['switch for good']},
        {'name': 'capture: camera mode, phone tracker warns',
         'state': base_state(series_source='import', has_b=True),
         'expect': ['built from phone photos', 'switch for good']},
        {'name': 'capture: camera refused', 'state': base_state(),
         'ui': {'camError': 'Camera access was refused.'},
         'expect': ['Camera access was refused', 'try again'], 'reject': ['id="cam"']},
        {'name': 'capture: adding more',
         'state': base_state(staged=['cam_1.jpg', 'cam_2.jpg']),
         'ui': {'forcePhotos': True},
         'expect': ['Continue with 2', 'Take 10 more']},
        {'name': 'capture: empty import folder',
         'state': base_state(browse={'path': '/x', 'parent': None, 'dirs': [], 'images': [], 'n_images': 0}),
         'ui': {'photoMode': 'import'},
         'expect': ['No photos here']},
        {'name': 'analyse: ready (import path)',
         'state': base_state(staged=['a.jpg', 'b.jpg']),
         'expect': ['Ready to analyse', 'Analyse</button>', 'Photos: <b>2 in this session'],
         'reject': ['Add to my tracker']},
        {'name': 'analyse: ready, shot on the Mac',
         'state': base_state(staged=['a.jpg'] * 10, source='mac-camera'),
         'expect': ['Ready to analyse', '10</b> photos in this session, taken with this Mac']},
        {'name': 'analyse: last run failed',
         'state': base_state(staged=['a.jpg'] * 10,
                             job=dict(RUNNING, running=False, rc=1, log=['docker: command not found'])),
         'expect': ['did not finish', 'docker: command not found', 'Analyse</button>'],
         'reject': ['Ready to analyse']},
        {'name': 'analysing', 'state': base_state(staged=['a.jpg'], job=RUNNING),
         'expect': ['Analysing', 'Finding faces', '3 of 10', '30%', 'about 42s left'],
         'reject': ['Add to my tracker']},
        {'name': 'analysing, no counts yet',
         'state': base_state(staged=['a.jpg'],
                             job=dict(RUNNING, log=[], eta=None,
                                      progress={'phase': None, 'done': 0, 'total': 0, 'pct': 0})),
         'expect': ['Analysing', 'indet']},
        {'name': 'result: good capture',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=dict(RESULT, luma_ok=True, fellback=False),
                             preflight=PREFLIGHT_GOOD),
         'expect': ['Good capture', '45.6', 'class="primary" onclick="addToTracker()"',
                    'Shoot again', 'Discard this session', 'Clean session',
                    'Show photos in Finder', 'photos used', 'spread', 'brightness'],
         'reject': ['Analyse</button>', 'Add anyway', '<details class="notes" open>']},
        {'name': 'result: reshoot recommended',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=RESULT, preflight=PREFLIGHT_BAD),
         'expect': ['We would skip this one', 'What to fix', 'exposure 133.7',
                    'class="primary" onclick="shootAgain()"', 'Add anyway'],
         'reject': ['Add to my tracker']},
        {'name': 'result: something was flagged',
         'state': base_state(checklist=dict(CHECKLIST_FAIL, failed=['did not shave']), staged=['a.jpg'],
                             result=dict(RESULT, valid=False, excluded_reason='did not shave'),
                             preflight=PREFLIGHT_GOOD),
         'expect': ['You flagged', 'did not shave', 'stays out of your trend line',
                    '<details class="notes" open>']},
        {'name': 'result: shot on a different camera',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=dict(RESULT, luma_ok=True, fellback=False),
                             preflight=PREFLIGHT_GOOD,
                             source='mac-camera', series_source='import', has_b=True,
                             baseline={'luma': 128.6, 'date': '2026-09-09', 'source': 'import'}),
         'expect': ['compare two cameras', 'Add to my tracker', 'vs baseline (']},
        {'name': 'result: first on this camera',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result=dict(RESULT, luma_delta=None, luma_ok=None, fellback=False),
                             preflight=PREFLIGHT_GOOD, source='mac-camera', series_source='import'),
         'expect': ['sets the baseline for this camera', 'rehearsals', 'this Mac']},
        {'name': 'result: nothing produced',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'],
                             result={'mean': None, 'n': 0},
                             job=dict(RUNNING, running=False, rc=0)),
         'expect': ['did not produce a result', 'Shoot again']},
        {'name': 'done: added to tracker',
         'state': base_state(checklist=CHECKLIST_DONE, staged=['a.jpg'], scored=True,
                             result=dict(RESULT, logged=True, in_series=True)),
         'expect': ['Added to your tracker', 'See your progress', 'Start a new session',
                    'Show photos in Finder', 'Remove it from the tracker'],
         'reject': ['Add to my tracker']},
        {'name': 'done: added but flagged',
         'state': base_state(checklist=CHECKLIST_FAIL, staged=['a.jpg'], scored=True,
                             result=dict(RESULT, logged=True, valid=False)),
         'expect': ['kept out of the trend line']},
        {'name': 'progress tab, nothing logged',
         'state': base_state(people=[{'name': 'me', 'sessions': 0, 'logged': 0}]),
         'ui': {'tab': 'progress'},
         'expect': ['Nothing to show yet', 'Take the first session'], 'reject': ['<iframe']},
        {'name': 'progress tab', 'state': base_state(),
         'ui': {'tab': 'progress'},
         'expect': ['<iframe class="trk"', '/tracker?person=me&amp;embed=1'.replace('&amp;', '&'),
                    'class="on" onclick="setTab(\'progress\')"'],
         'reject': ['id="cam"', 'Take the photos']},
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
