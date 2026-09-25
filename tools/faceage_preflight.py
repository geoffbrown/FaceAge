#!/usr/bin/env python3
"""Pre-flight diagnosis: what is wrong with this session, and what to do about it.

Runs BEFORE the session is scored. That ordering is the point. You learn whether
the capture was good without learning what the number was, so the decision to
reshoot cannot be influenced by whether you liked the result -- which is the
same principle docs/PREREGISTRATION.md §1 applies to protocol failures.

A flag is not the product. "mean_luma 127.4" tells you nothing you can act on.
Every finding here carries a specific remedy tied to the capture standard in
README §"Keep the capture setup identical" and docs/CAPTURE_STANDARD.md §3.

Nothing in here modifies a photograph. If a check fails the answer is always to
reshoot -- fix the light, not the file. Software-normalising exposure would hide
the error rather than remove it, and would flatten the mean_luma trace that the
§6 falsification check depends on.

Stdlib only. Reads the per-image QA CSV the pipeline already produces.
"""
from __future__ import print_function

import os
import csv
import sys
import json
import math
import argparse

# The capture standard's tolerance. README: "Keep it within roughly +/-5 of
# prior sessions. If it drifts, treat that session's change as suspect."
LUMA_TOL = float(os.environ.get('FACEAGE_LUMA_TOL') or 5.0)   # set with `faceage tolerance`
LUMA_WARN = LUMA_TOL / 2  # early warning at half the tolerance, before it becomes a reshoot
LUMA_SPREAD_TOL = 5.0     # variation WITHIN one session
MIN_FACE_FILL = 0.80      # authors' Supplement Table 1, as linear frame height
MIN_CONFIDENCE = 0.95
EXPECT_FRAMES = 10        # docs/CAPTURE_STANDARD.md §3: "10 frames per session"

RESHOOT, CHECK, INFO = 'RESHOOT', 'CHECK', 'INFO'
_RANK = {RESHOOT: 0, CHECK: 1, INFO: 2}


class Finding(object):
    def __init__(self, severity, code, what, why, do, frames=None):
        self.severity = severity
        self.code = code
        self.what = what        # what was measured
        self.why = why          # why it matters for this specific model
        self.do = do            # the action, concrete enough to perform
        self.frames = frames or []

    def as_dict(self):
        return {'severity': self.severity, 'code': self.code, 'what': self.what,
                'why': self.why, 'do': self.do, 'frames': self.frames}


# ----------------------------------------------------------------------------
# reading the pipeline's per-image QA output
# ----------------------------------------------------------------------------

def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def load_per_image(path):
    if not os.path.exists(path):
        raise SystemExit('no per-image QA at %s' % path)
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            out.append({
                'file': r.get('file') or r.get('subj_id') or '?',
                'luma': _num(r.get('crop_luma_mean')),
                'luma_std': _num(r.get('crop_luma_std')),
                'confidence': _num(r.get('confidence')),
                'fill': _num(r.get('face_fill_height_frac')),
                'crop_w': _num(r.get('crop_w')),
                'crop_h': _num(r.get('crop_h')),
                'hard': [f for f in (r.get('hard_flags') or '').split(';') if f],
                'adv': [f for f in (r.get('advisory_flags') or '').split(';') if f],
            })
    return out


def _with(images, flag):
    return [i['file'] for i in images if flag in i['hard'] or flag in i['adv']]


# ----------------------------------------------------------------------------
# the diagnosis
# ----------------------------------------------------------------------------

def diagnose(images, baseline_luma=None, expect_frames=EXPECT_FRAMES):
    """Return findings, worst first. `baseline_luma` is session one's mean_luma."""
    f = []
    usable = [i for i in images if not i['hard']]
    lumas = [i['luma'] for i in usable if i['luma'] is not None]

    # ---- hard failures: these frames produced no number at all --------------
    for flag, what, why, do in (
        ('NO_FACE_DETECTED',
         'no face found',
         'The frame contributes nothing, so the session mean rests on fewer '
         'photos than you think.',
         'Usually the image is sideways (rotation tag lost), too dark, or the '
         'face is out of frame. Check the tripod aim and reshoot those frames.'),
        ('MULTIPLE_FACES',
         'more than one face in the frame',
         'The pipeline takes the detector\'s first face, which may not be '
         'yours -- so the number could be measuring someone else.',
         'Clear the background: another person, a portrait on the wall, or a '
         'reflection in a mirror or screen. Reshoot.'),
        ('SOURCE_TOO_SMALL',
         'source image under 160x160',
         'Below the model input size, so there is nothing to crop.',
         'Check the phone is not saving a downscaled copy; shoot at full '
         'resolution.'),
        ('UNREADABLE_IMAGE',
         'file could not be read',
         'Truncated or not an image.',
         'Re-transfer from the phone; an interrupted AirDrop is the usual '
         'cause.'),
        ('DEGENERATE_CROP',
         'face box had zero width or height',
         'Detection succeeded but produced an unusable crop.',
         'Reshoot the frame.'),
        ('PREDICTION_FAILED',
         'the model raised an error on this frame',
         'No value produced.',
         'Re-run; if it repeats, the frame is malformed -- reshoot it.'),
    ):
        hits = _with(images, flag)
        if hits:
            f.append(Finding(RESHOOT, flag, '%d frame(s) %s' % (len(hits), what),
                             why, do, hits))

    if not usable:
        f.append(Finding(RESHOOT, 'SESSION_EMPTY', 'no usable frames',
                         'There is no session to score.',
                         'Reshoot the whole session.'))
        return sorted(f, key=lambda x: _RANK[x.severity])

    # ---- frame count --------------------------------------------------------
    if len(usable) < expect_frames:
        sev = RESHOOT if len(usable) < expect_frames / 2.0 else CHECK
        f.append(Finding(
            sev, 'FRAMES_SHORT',
            '%d usable frame(s), expected %d' % (len(usable), expect_frames),
            'The session mean averages out per-photo noise, and that noise is '
            'large -- expression alone moves FaceAge ~2.2 years. Fewer frames '
            'means a noisier session mean.',
            'Shoot %d more frame(s), discarding blinks and expression drift.'
            % (expect_frames - len(usable))))

    # ---- exposure vs baseline: the dominant photographic confound -----------
    if lumas:
        session_luma = sum(lumas) / len(lumas)
        if baseline_luma is None:
            f.append(Finding(
                INFO, 'LUMA_BASELINE',
                'session exposure %.1f (no baseline yet)' % session_luma,
                'This session sets the exposure baseline every later session is '
                'held to.',
                'Record how the light is set up now -- lamp, position, output, '
                'room -- so it can be reproduced exactly.'))
        else:
            d = session_luma - baseline_luma
            if abs(d) > LUMA_TOL:
                f.append(Finding(
                    RESHOOT, 'LUMA_DRIFT',
                    'exposure %.1f vs baseline %.1f (%+.1f, tolerance +/-%.0f)'
                    % (session_luma, baseline_luma, d, LUMA_TOL),
                    'Lighting is the dominant photographic confound: a 3x '
                    'exposure range moved FaceAge by 3.7 years in this repo\'s '
                    'own measurement. A drift this size can exceed anything '
                    'real the intervention will produce.',
                    'The light is %s than baseline. Shoot under fixed '
                    'artificial light in a room where daylight is excluded -- '
                    'more reliable than any time-of-day rule. Restore the lamp '
                    'position and output, and never let it come from overhead: '
                    'overhead light casts shadow into the nasolabial and '
                    'periorbital regions, which reads as older. Then reshoot.'
                    % ('brighter' if d > 0 else 'darker')))
            elif abs(d) > LUMA_WARN:
                f.append(Finding(
                    CHECK, 'LUMA_SHIFT',
                    'exposure %.1f vs baseline %.1f (%+.1f)'
                    % (session_luma, baseline_luma, d),
                    'Inside tolerance, but drifting. Exposure creep across a '
                    'study looks exactly like a slow trend.',
                    'Check the lamp and the room before the next session; if it '
                    'keeps moving in one direction, find the cause now rather '
                    'than after six months of series.'))

        # ---- exposure stability WITHIN the session --------------------------
        spread = max(lumas) - min(lumas)
        if spread > LUMA_SPREAD_TOL:
            worst = [i['file'] for i in usable if i['luma'] is not None
                     and abs(i['luma'] - session_luma) > LUMA_SPREAD_TOL / 2.0]
            f.append(Finding(
                RESHOOT, 'LUMA_UNSTABLE',
                'brightness varied %.1f across the session (%.1f to %.1f)'
                % (spread, min(lumas), max(lumas)),
                'The light changed while you were shooting, or the camera '
                're-metered between frames. Averaging frames taken under '
                'different light does not cancel out -- it just widens the '
                'session.',
                'Lock exposure at capture: on iPhone, tap and hold for AE/AF '
                'lock before the first frame. Exclude daylight so passing cloud '
                'cannot change the room. Then reshoot.', worst))

    # ---- framing ------------------------------------------------------------
    low_fill = [i for i in usable
                if i['fill'] is not None and i['fill'] < MIN_FACE_FILL]
    if low_fill:
        avg = sum(i['fill'] for i in low_fill) / len(low_fill)
        f.append(Finding(
            CHECK if len(low_fill) < len(usable) else RESHOOT,
            'FACE_FILL_LOW',
            '%d frame(s) with the face filling %.0f%% of frame height, '
            'below the %.0f%% bar' % (len(low_fill), avg * 100, MIN_FACE_FILL * 100),
            'A smaller face means a smaller crop upscaled to 160x160, so the '
            'model reads a softer image. It also means you were further from '
            'the camera than at baseline, which is a capture-setup change.',
            'Restore the marked tripod position and distance, and check the '
            'frame is filled the way it was at baseline. Reshoot.',
            [i['file'] for i in low_fill]))

    upscaled = _with(images, 'CROP_UPSCALED')
    if upscaled and not low_fill:
        f.append(Finding(
            CHECK, 'CROP_UPSCALED',
            '%d frame(s) had a face box under 160px' % len(upscaled),
            'The crop is enlarged to reach the model input, which invents '
            'detail that was never captured.',
            'Move the tripod closer, or check the phone is shooting at full '
            'resolution.', upscaled))

    clipped = _with(images, 'FACE_CLIPPED_AT_BORDER')
    if clipped:
        f.append(Finding(
            CHECK, 'FACE_CLIPPED',
            '%d frame(s) with the face touching the frame edge' % len(clipped),
            'Part of the face is outside the image, so the crop is incomplete '
            'and not comparable with a full one.',
            'Recentre on the tripod mark and leave margin on all four sides. '
            'Reshoot those frames.', clipped))

    lowconf = [i for i in usable if i['confidence'] is not None
               and i['confidence'] < MIN_CONFIDENCE]
    if lowconf:
        f.append(Finding(
            CHECK, 'LOW_CONFIDENCE',
            '%d frame(s) detected below %.2f confidence' % (len(lowconf),
                                                            MIN_CONFIDENCE),
            'The detector was unsure where the face was, so the crop may be '
            'placed differently from other frames -- and a different crop is a '
            'different number.',
            'Usual causes: hair over the forehead or temples, glasses, head not '
            'level, or soft focus. The model attends to the forehead and '
            'temples specifically, so keep both clear. Reshoot those frames.',
            [i['file'] for i in lowconf]))

    return sorted(f, key=lambda x: _RANK[x.severity])


def verdict(findings):
    if any(x.severity == RESHOOT for x in findings):
        return RESHOOT
    if any(x.severity == CHECK for x in findings):
        return CHECK
    return 'GOOD'


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

VERDICT_TEXT = {
    'GOOD': 'Capture looks good. Score it.',
    CHECK: 'Usable, but something drifted. Read the items below before scoring.',
    RESHOOT: 'Reshoot before scoring. Scoring this session would put a number '
             'you cannot trust into the series.',
}


def render(findings, images):
    out = ['=' * 74, 'PRE-FLIGHT -- %d frame(s)' % len(images), '=' * 74]
    v = verdict(findings)
    out.append('VERDICT: %s' % v)
    out.append(VERDICT_TEXT[v])
    out.append('')
    if not findings:
        out.append('No findings.')
    for x in findings:
        out.append('[%s] %s' % (x.severity, x.what))
        out.append('   why : %s' % x.why)
        out.append('   do  : %s' % x.do)
        if x.frames:
            shown = ', '.join(x.frames[:4])
            if len(x.frames) > 4:
                shown += ', +%d more' % (len(x.frames) - 4)
            out.append('   files: %s' % shown)
        out.append('')
    out.append('=' * 74)
    out.append('No photograph was modified. If a check fails the fix is the')
    out.append('lighting or the rig, never the file.')
    return '\n'.join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description='FaceAge session pre-flight')
    p.add_argument('--per-image', required=True,
                   help='the session\'s *_per_image.csv')
    p.add_argument('--baseline-luma', type=float, default=None)
    p.add_argument('--expect-frames', type=int, default=EXPECT_FRAMES)
    p.add_argument('--json', action='store_true')
    args = p.parse_args(argv)

    images = load_per_image(args.per_image)
    findings = diagnose(images, args.baseline_luma, args.expect_frames)

    if args.json:
        print(json.dumps({'verdict': verdict(findings),
                          'n_frames': len(images),
                          'findings': [x.as_dict() for x in findings]}, indent=2))
    else:
        print(render(findings, images))
    return 2 if verdict(findings) == RESHOOT else 0


if __name__ == '__main__':
    sys.exit(main())
