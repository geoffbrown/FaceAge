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
  querySelectorAll: function(){ return []; }
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
        'person': 'me', 'date': '2026-09-12',
        'inbox_path': '/Users/x/Downloads', 'data_path': '/Users/x/FaceAgeData',
        'browse': {'path': '/Users/x/Downloads', 'parent': '/Users/x',
                   'dirs': ['pics'],
                   'images': [{'file': 'IMG_1.jpg', 'when': '12 Sep 10:00',
                               'heic': False, 'size': 10, 'mtime': 1}],
                   'n_images': 1},
        'inbox': [], 'staged': ['IMG_1.jpg'],
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


def states():
    """Every branch of render() that has bitten so far, plus the empty cases."""
    return [
        {'name': 'no people', 'state': base_state(people=[], person=None)},
        {'name': 'fresh session', 'state': base_state()},
        # The Score button must be disabled for every reason the server would
        # refuse. An enabled button that says "Needs the checklist" offers an
        # action that returns 400.
        {'name': 'nothing staged', 'state': base_state(staged=[]),
         'expect': ['id="scorebtn" disabled', 'Import photos first']},
        {'name': 'staged but no checklist', 'state': base_state(),
         'expect': ['id="scorebtn" disabled', 'Record the checklist above first']},
        {'name': 'no checklist but one-off ticked',
         'state': base_state(oneoff=True),
         'expect': ['id="scorebtn"', 'Score only'],
         'reject': ['id="scorebtn" disabled']},
        {'name': 'checklist recorded', 'state': base_state(checklist=CHECKLIST_DONE),
         'expect': ['Score and add to series'],
         'reject': ['id="scorebtn" disabled']},
        {'name': 'checklist failed', 'state': base_state(checklist=CHECKLIST_FAIL)},
        {'name': 'checklist stale, unscored',
         'state': base_state(checklist=CHECKLIST_DONE, checklist_stale=True)},
        {'name': 'checklist stale, scored',
         'state': base_state(checklist=CHECKLIST_DONE, checklist_stale=True,
                             scored=True)},
        {'name': 'reopen with prefill',
         'state': base_state(checklist=CHECKLIST_FAIL, checklist_stale=True,
                             reopen=True, prefill={'light': True},
                             prev_checklist=CHECKLIST_DONE)},
        {'name': 'prefill offered',
         'state': base_state(prev_checklist=CHECKLIST_DONE)},
        {'name': 'job running',
         'state': base_state(job={'running': True, 'label': 'Scoring',
                                  'rc': None, 'log': ['(3/10) Running'],
                                  'progress': {'phase': 'Finding faces',
                                               'done': 3, 'total': 10,
                                               'pct': 30.0},
                                  'eta': 42, 'elapsed': 18})},
        {'name': 'job running, no counts',
         'state': base_state(job={'running': True, 'label': 'Scoring',
                                  'rc': None, 'log': [],
                                  'progress': {'phase': None, 'done': 0,
                                               'total': 0, 'pct': 0},
                                  'eta': None, 'elapsed': 3})},
        {'name': 'one-off result, addable',
         'state': base_state(checklist=CHECKLIST_DONE, result=RESULT,
                             job={'running': False, 'label': '', 'rc': 0,
                                  'log': ['done'],
                                  'progress': {'phase': None, 'done': 0,
                                               'total': 0, 'pct': 0},
                                  'eta': None, 'elapsed': 20})},
        {'name': 'one-off result, no checklist',
         'state': base_state(result=dict(RESULT, has_checklist=False))},
        {'name': 'logged and in series',
         'state': base_state(checklist=CHECKLIST_DONE, scored=True,
                             result=dict(RESULT, logged=True, in_series=True))},
        {'name': 'logged but invalid',
         'state': base_state(checklist=CHECKLIST_FAIL, scored=True,
                             result=dict(RESULT, logged=True, valid=False,
                                         in_series=False,
                                         excluded_reason='Frontal light'))},
        {'name': 'preflight reshoot',
         'state': base_state(preflight={
             'available': True, 'n_frames': 14, 'verdict': 'RESHOOT',
             'verdict_text': 'Reshoot before scoring.',
             'findings': [{'severity': 'RESHOOT', 'code': 'LUMA_DRIFT',
                           'what': 'exposure 133.7', 'why': 'lighting',
                           'do': 'reshoot', 'frames': ['a.jpg', 'b.jpg']}]})},
        {'name': 'preflight stale',
         'state': base_state(preflight={'available': False, 'stale': True,
                                        'why': '2 added'})},
        {'name': 'take letter', 'state': base_state(date='2026-09-12b')},
        {'name': 'discard note', 'state': base_state(discardNote='2026-09-12 — photos')},
        {'name': 'moved note', 'state': base_state(movedNote='2026-09-12b')},
        {'name': 'one-off checked', 'state': base_state(oneoff=True)},
        {'name': 'empty browse',
         'state': base_state(browse={'path': '/x', 'parent': None,
                                     'dirs': [], 'images': [], 'n_images': 0})},
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
                     'one-way door', 'the study series'):
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
