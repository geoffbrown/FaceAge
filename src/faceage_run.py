#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FaceAge runner — batch inference over a folder of face photographs.

The numerical core (face localization -> crop -> resize -> standardize ->
regress) is a faithful reimplementation of the reference implementation in
AIM-Harvard/FaceAge `src/test/predict_folder_demo.py`. Do not "improve" the
maths in here: any change breaks agreement with the authors' published
reference outputs (validation/reference/utk_hi-res_qa_res.csv).

Two modes:
  validate  strict replication, writes subj_id,faceage and compares to the
            authors' reference CSV.
  session   same maths plus photo-QA flagging and session-level summary
            statistics, appended to a longitudinal history CSV.
"""

from __future__ import print_function

import os
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')

import gc
import sys
import csv
import json
import time
import math
import hashlib
import argparse
import datetime

import PIL
import PIL.Image
import PIL.ImageOps
import mtcnn
import keras
import numpy as np
import pandas as pd
import tensorflow as tf

# suppress warnings/errors due to migration from TensorFlow 1.x to 2.x
# (identical to the reference implementation)
tf.compat.v1.disable_eager_execution()
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

from skimage.io import imread

IMAGE_EXTS = ('.jpg', '.jpeg', '.png')
MODEL_INPUT_SIZE = (160, 160)


# ----------------------------------------------------------------------------
# numerical core - keep byte-for-byte faithful to predict_folder_demo.py
# ----------------------------------------------------------------------------

def load_image(path_to_image, auto_orient):
    """Read an image. In session mode we honour the EXIF orientation tag first,
    because phone cameras store portrait shots as landscape pixels plus a
    rotation flag, and MTCNN will not find a sideways face. UTK validation
    images carry no orientation tag, so this is a no-op there."""
    if auto_orient:
        with PIL.Image.open(path_to_image) as im:
            oriented = PIL.ImageOps.exif_transpose(im)
            return np.asarray(oriented.convert('RGB'))
    return imread(path_to_image)


def detect_faces(img):
    """Run MTCNN. The reference implementation instantiates a fresh detector per
    image and swallows all errors; replicated here so behaviour matches."""
    try:
        return mtcnn.mtcnn.MTCNN().detect_faces(img)
    except Exception:
        return []


def predict_faceage(model, img, mtcnn_output_dict):
    """Crop, resize, standardize and regress. Mirrors get_model_prediction()."""
    x1, y1, width, height = mtcnn_output_dict['box']
    x1, y1 = abs(x1), abs(y1)
    x2, y2 = x1 + width, y1 + height

    pat_face = img[y1:y2, x1:x2]

    pat_face_pil = PIL.Image.fromarray(np.uint8(pat_face)).convert('RGB')
    pat_face = np.asarray(pat_face_pil.resize(MODEL_INPUT_SIZE))

    mean, std = pat_face.mean(), pat_face.std()
    pat_face = (pat_face - mean) / std
    pat_face_input = pat_face.reshape(1, MODEL_INPUT_SIZE[0], MODEL_INPUT_SIZE[1], 3)

    # mean/std of the crop BEFORE standardization are the exposure and contrast the
    # camera actually delivered. Measured 2026-09-06: a 3x exposure range moved FaceAge
    # by 3.7 years, so these are tracked to catch lighting drift between sessions.
    return float(np.squeeze(model.predict(pat_face_input))), float(mean), float(std)


# ----------------------------------------------------------------------------
# photo QA
# ----------------------------------------------------------------------------

def qa_assess(img, faces, args):
    """Apply the authors' photo exclusion criteria (Supplement Table 1).

    Returns (hard_flags, advisory_flags, metrics). Hard flags mean no usable
    number. Advisories mean the number exists but the photo departs from the
    capture standard."""
    hard, advisory, metrics = [], [], {}

    h, w = img.shape[0], img.shape[1]
    metrics['source_w'], metrics['source_h'] = w, h

    # "Source photograph before face extraction is smaller than 160x160 pixels"
    if w < 160 or h < 160:
        hard.append('SOURCE_TOO_SMALL')

    # "Photograph does not contain a face"
    if not faces:
        hard.append('NO_FACE_DETECTED')
        return hard, advisory, metrics

    # "There is more than one face in the image"
    metrics['n_faces'] = len(faces)
    if len(faces) > 1:
        hard.append('MULTIPLE_FACES')

    face = faces[0]
    conf = float(face.get('confidence', float('nan')))
    metrics['confidence'] = conf

    bx, by, bw, bh = face['box']
    metrics['box'] = [int(bx), int(by), int(bw), int(bh)]
    metrics['crop_w'], metrics['crop_h'] = int(bw), int(bh)

    if bw <= 0 or bh <= 0:
        hard.append('DEGENERATE_CROP')
        return hard, advisory, metrics

    # "Face is not entirely within image borders"
    if bx < 0 or by < 0 or (bx + bw) > w or (by + bh) > h:
        advisory.append('FACE_CLIPPED_AT_BORDER')

    # low-confidence detection
    if not math.isnan(conf) and conf < args.min_confidence:
        advisory.append('LOW_CONFIDENCE')

    # crop upscaled to reach the 160x160 model input
    if bw < 160 or bh < 160:
        advisory.append('CROP_UPSCALED')

    # "The face fills < 80% of the photographic frame".
    # Interpreted as a LINEAR fraction of frame height, not an area fraction.
    # The authors' own worked example of an ACCEPTABLE image (assets/
    # data-acceptable_image.png) has a ~410x520 face box in a ~750x560 frame:
    # 93% of frame height but only ~51% of frame area. An area-based test at
    # 80% would disqualify their own acceptable example, so height is the
    # reading consistent with the published examples.
    fill_h = float(bh) / float(h)
    metrics['face_fill_height_frac'] = round(fill_h, 4)
    metrics['face_fill_area_frac'] = round(float(bw * bh) / float(w * h), 4)
    if fill_h < args.min_face_fill:
        advisory.append('FACE_FILL_BELOW_%d_PCT' % int(round(args.min_face_fill * 100)))

    return hard, advisory, metrics


# ----------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------

def list_images(folder):
    if not os.path.isdir(folder):
        sys.exit("ERROR: input folder does not exist: %s" % folder)
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith(IMAGE_EXTS) and not f.startswith('.'))
    skipped = sorted(f for f in os.listdir(folder)
                     if not f.lower().endswith(IMAGE_EXTS)
                     and not f.startswith('.')
                     and os.path.isfile(os.path.join(folder, f)))
    return files, skipped


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run(args):
    print("Python version     : ", sys.version.split('\n')[0])
    print("TensorFlow version : ", tf.__version__)
    print("Keras version      : ", keras.__version__)
    print("Numpy version      : ", np.__version__)
    print("MTCNN version      : ", getattr(mtcnn, '__version__', 'unknown'))
    print("")

    files, skipped = list_images(args.input)
    if skipped:
        print("NOTE: %d non-.jpg/.png file(s) ignored, e.g. %s"
              % (len(skipped), ', '.join(skipped[:4])))
    if not files:
        sys.exit("ERROR: no .jpg/.png images found in %s" % args.input)

    auto_orient = (args.mode == 'session') and not args.no_auto_orient

    print("Processing %d image(s) from: %s\n" % (len(files), args.input))

    # ---- phase 1: face localization -----------------------------------------
    # kept as a separate pass because the reference implementation calls
    # clear_session() during localization, which would destroy a loaded model.
    records = []
    t = time.time()
    for idx, fname in enumerate(files):
        print('(%d/%d) Running the face localization step for "%s"'
              % (idx + 1, len(files), fname), end='\r')
        sys.stdout.flush()

        path = os.path.join(args.input, fname)
        rec = {'subj_id': os.path.splitext(fname)[0],
               'file': fname,
               'path': path,
               'hard': [], 'advisory': [], 'metrics': {}}

        try:
            img = load_image(path, auto_orient)
            if img.ndim == 2:  # greyscale -> RGB
                img = np.stack([img] * 3, axis=-1)
            if img.shape[-1] == 4:  # drop alpha
                img = img[:, :, :3]
            rec['img'] = img
        except Exception as exc:
            rec['hard'] = ['UNREADABLE_IMAGE']
            rec['error'] = str(exc)
            rec['img'] = None
            records.append(rec)
            continue

        faces = detect_faces(rec['img'])
        rec['faces'] = faces
        hard, advisory, metrics = qa_assess(rec['img'], faces, args)
        rec['hard'], rec['advisory'], rec['metrics'] = hard, advisory, metrics
        records.append(rec)

        # solves known TF memory leaks for the MTCNN pipeline
        if not idx % 5:
            tf.keras.backend.clear_session()
            gc.collect()

    print("\n... Done in %g seconds." % (time.time() - t))

    # ---- phase 2: age regression --------------------------------------------
    model = keras.models.load_model(args.model)
    print("")

    t = time.time()
    predictable = [r for r in records if r.get('faces')
                   and 'DEGENERATE_CROP' not in r['hard']
                   and 'UNREADABLE_IMAGE' not in r['hard']]
    for idx, rec in enumerate(predictable):
        print('(%d/%d) Running the age estimation step for "%s"'
              % (idx + 1, len(predictable), rec['subj_id']), end='\r')
        sys.stdout.flush()
        try:
            age, luma_mean, luma_std = predict_faceage(model, rec['img'], rec['faces'][0])
            rec['faceage'] = age
            rec['metrics']['crop_luma_mean'] = round(luma_mean, 2)
            rec['metrics']['crop_luma_std'] = round(luma_std, 2)
        except Exception as exc:
            rec['hard'].append('PREDICTION_FAILED')
            rec['error'] = str(exc)

    print("\n... Done in %g seconds.\n" % (time.time() - t))

    for rec in records:
        rec.pop('img', None)
        rec.pop('faces', None)

    return records


# ----------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------

def write_per_image(records, path, strict):
    """strict=True writes exactly the reference schema (subj_id,faceage)."""
    if strict:
        rows = [{'subj_id': r['subj_id'], 'faceage': r.get('faceage')}
                for r in records]
        pd.DataFrame(rows, columns=['subj_id', 'faceage']).to_csv(path, index=False)
        return

    rows = []
    for r in records:
        m = r['metrics']
        rows.append({
            'subj_id': r['subj_id'],
            'file': r['file'],
            'faceage': r.get('faceage'),
            'status': 'FAILED' if r['hard'] else ('FLAGGED' if r['advisory'] else 'OK'),
            'hard_flags': ';'.join(r['hard']),
            'advisory_flags': ';'.join(r['advisory']),
            'confidence': m.get('confidence'),
            'n_faces': m.get('n_faces'),
            'source_w': m.get('source_w'),
            'source_h': m.get('source_h'),
            'crop_w': m.get('crop_w'),
            'crop_h': m.get('crop_h'),
            'crop_luma_mean': m.get('crop_luma_mean'),
            'crop_luma_std': m.get('crop_luma_std'),
            'face_fill_height_frac': m.get('face_fill_height_frac'),
            'face_fill_area_frac': m.get('face_fill_area_frac'),
            'error': r.get('error', ''),
        })
    cols = ['subj_id', 'file', 'faceage', 'status', 'hard_flags', 'advisory_flags',
            'confidence', 'n_faces', 'source_w', 'source_h', 'crop_w', 'crop_h',
            'crop_luma_mean', 'crop_luma_std',
            'face_fill_height_frac', 'face_fill_area_frac', 'error']
    pd.DataFrame(rows, columns=cols).to_csv(path, index=False)


def summarise(records, args):
    usable = [r for r in records if r.get('faceage') is not None and not r['hard']]
    if not args.include_flagged:
        primary = [r for r in usable if not r['advisory']]
        # if every photo carries an advisory, fall back to all usable photos
        # rather than reporting an empty session
        fellback = False
        if not primary:
            primary, fellback = usable, True
    else:
        primary, fellback = usable, False

    vals = np.array([r['faceage'] for r in primary], dtype=float)
    s = {
        'n': int(len(vals)),
        'n_total_images': len(records),
        'n_failed': sum(1 for r in records if r['hard']),
        'n_flagged': sum(1 for r in records if r['advisory'] and not r['hard']),
        'fellback_to_flagged': fellback,
    }
    if len(vals):
        s['mean'] = float(np.mean(vals))
        s['median'] = float(np.median(vals))
        # sample standard deviation (ddof=1); undefined for a single photo
        s['std'] = float(np.std(vals, ddof=1)) if len(vals) > 1 else float('nan')
        s['min'] = float(np.min(vals))
        s['max'] = float(np.max(vals))
        lums = [r['metrics'].get('crop_luma_mean') for r in primary
                if r['metrics'].get('crop_luma_mean') is not None]
        s['luma'] = float(np.mean(lums)) if lums else float('nan')
    else:
        for k in ('mean', 'median', 'std', 'min', 'max', 'luma'):
            s[k] = float('nan')
    return s


def append_history(history_path, row):
    cols = ['session_date', 'run_timestamp', 'n', 'n_total_images', 'n_failed',
            'n_flagged', 'mean', 'median', 'std', 'min', 'max', 'mean_luma',
            'model_sha256', 'image_dir', 'notes']
    if os.path.exists(history_path):
        hist = pd.read_csv(history_path)
        for c in cols:
            if c not in hist.columns:
                hist[c] = None
        hist = hist[hist['session_date'].astype(str) != str(row['session_date'])]
        hist = pd.concat([hist, pd.DataFrame([row], columns=cols)], ignore_index=True)
    else:
        hist = pd.DataFrame([row], columns=cols)
    hist = hist.sort_values('session_date').reset_index(drop=True)
    hist.to_csv(history_path, index=False)
    return len(hist)


# ----------------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------------

def compare_to_reference(records, reference_csv, tol):
    ref = pd.read_csv(reference_csv)
    ref_map = dict(zip(ref['subj_id'].astype(str), ref['faceage'].astype(float)))

    matched, missing, diffs = [], [], []
    for r in records:
        sid = str(r['subj_id'])
        if sid not in ref_map:
            missing.append(sid)
            continue
        if r.get('faceage') is None:
            diffs.append((sid, ref_map[sid], None, float('nan')))
            continue
        d = abs(r['faceage'] - ref_map[sid])
        matched.append((sid, ref_map[sid], r['faceage'], d))
        diffs.append((sid, ref_map[sid], r['faceage'], d))

    print("=" * 78)
    print("VALIDATION AGAINST AUTHORS' REFERENCE CSV")
    print("=" * 78)
    print("Reference file : %s" % reference_csv)
    print("Reference rows : %d" % len(ref))
    print("Images scored  : %d" % len(records))
    print("Matched to ref : %d" % len(matched))
    if missing:
        print("NOT IN REF     : %d (%s)" % (len(missing), ', '.join(missing[:5])))
    print("")

    if not matched:
        print("RESULT: FAIL — nothing could be compared.")
        return False

    ds = np.array([m[3] for m in matched])
    print("Absolute difference vs reference:")
    print("  max    : %.10g" % ds.max())
    print("  mean   : %.10g" % ds.mean())
    print("  median : %.10g" % np.median(ds))
    print("  n <= %.0e : %d / %d" % (tol, int((ds <= tol).sum()), len(ds)))
    print("")

    worst = sorted(matched, key=lambda m: -m[3])[:10]
    print("%-32s %14s %14s %12s" % ('subj_id', 'reference', 'reproduced', 'abs_diff'))
    for sid, rv, mv, d in worst:
        print("%-32s %14.6f %14.6f %12.3e" % (sid, rv, mv, d))
    print("")

    ok = bool(ds.max() <= tol)
    print("RESULT: %s (tolerance %.0e)" % ('PASS' if ok else 'FAIL', tol))
    print("=" * 78)
    return ok


# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description='FaceAge batch runner')
    p.add_argument('--mode', choices=['session', 'validate'], default='session')
    p.add_argument('--input', required=True, help='folder of .jpg/.png images')
    p.add_argument('--model', default='/opt/faceage/models/faceage_model.h5')
    p.add_argument('--out-per-image', default=None)
    p.add_argument('--out-summary', default=None)
    p.add_argument('--history', default=None)
    p.add_argument('--session-date', default=None, help='YYYY-MM-DD')
    p.add_argument('--reference', default=None, help='reference CSV for validate mode')
    p.add_argument('--tolerance', type=float, default=1e-4)
    p.add_argument('--min-confidence', type=float, default=0.95)
    p.add_argument('--min-face-fill', type=float, default=0.80)
    p.add_argument('--include-flagged', action='store_true',
                   help='include QA-flagged photos in the session statistics')
    p.add_argument('--no-auto-orient', action='store_true',
                   help='do not apply EXIF orientation before detection')
    p.add_argument('--notes', default='')
    args = p.parse_args()

    if not os.path.exists(args.model):
        sys.exit("ERROR: model weights not found at %s" % args.model)

    records = run(args)

    strict = (args.mode == 'validate')
    if args.out_per_image:
        write_per_image(records, args.out_per_image, strict)
        print("Per-image results written to: %s" % args.out_per_image)

    if args.mode == 'validate':
        if not args.reference:
            sys.exit("ERROR: --reference is required in validate mode")
        ok = compare_to_reference(records, args.reference, args.tolerance)
        sys.exit(0 if ok else 2)

    # ---- session mode --------------------------------------------------------
    s = summarise(records, args)
    session_date = args.session_date or datetime.date.today().isoformat()

    print("=" * 78)
    print("SESSION SUMMARY  %s" % session_date)
    print("=" * 78)
    print("Photos supplied        : %d" % s['n_total_images'])
    print("Scored and used        : %d" % s['n'])
    print("QA-flagged             : %d" % s['n_flagged'])
    print("Failed (no usable face): %d" % s['n_failed'])
    print("")
    if s['n']:
        print("Mean FaceAge   : %.3f   <- the number to track" % s['mean'])
        print("Median FaceAge : %.3f" % s['median'])
        print("Std dev (n-1)  : %s" % ('n/a (single photo)' if math.isnan(s['std'])
                                       else '%.3f' % s['std']))
        print("Range          : %.3f - %.3f" % (s['min'], s['max']))
        if not math.isnan(s.get('luma', float('nan'))):
            print("Face exposure  : %.1f  <- keep within ~5 of prior sessions" % s['luma'])
    else:
        print("No usable photographs in this session.")
    print("")

    if s['fellback_to_flagged']:
        print("WARNING: every photo carried a QA advisory, so the statistics above")
        print("         use all scored photos. Check the advisory flags and review")
        print("         your capture setup before trusting this session.")
        print("")

    failed = [r for r in records if r['hard']]
    if failed:
        print("FAILED PHOTOS (excluded, no FaceAge value):")
        for r in failed:
            print("  %-40s %s" % (r['file'], ';'.join(r['hard'])))
        print("")
    flagged = [r for r in records if r['advisory'] and not r['hard']]
    if flagged:
        print("QA-FLAGGED PHOTOS (scored, excluded from the mean by default):")
        for r in flagged:
            print("  %-40s %s" % (r['file'], ';'.join(r['advisory'])))
        print("")

    if args.out_summary:
        with open(args.out_summary, 'w') as fh:
            json.dump(dict(s, session_date=session_date), fh, indent=2)
        print("Session summary written to: %s" % args.out_summary)

    if args.history:
        row = {
            'session_date': session_date,
            'run_timestamp': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            'n': s['n'], 'n_total_images': s['n_total_images'],
            'n_failed': s['n_failed'], 'n_flagged': s['n_flagged'],
            'mean': round(s['mean'], 4) if s['n'] else '',
            'median': round(s['median'], 4) if s['n'] else '',
            'std': ('' if (not s['n'] or math.isnan(s['std'])) else round(s['std'], 4)),
            'min': round(s['min'], 4) if s['n'] else '',
            'max': round(s['max'], 4) if s['n'] else '',
            'mean_luma': (round(s['luma'], 1)
                          if (s['n'] and not math.isnan(s.get('luma', float('nan')))) else ''),
            'model_sha256': sha256_of(args.model)[:16],
            'image_dir': args.input,
            'notes': args.notes,
        }
        n = append_history(args.history, row)
        print("Longitudinal history updated: %s (%d session rows)" % (args.history, n))


if __name__ == '__main__':
    main()
