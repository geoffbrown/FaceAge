#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Regressor parity: Core ML vs the reference Keras implementation.

Both sides read the SAME committed uint8 crops and derive the standardized
input identically, so a difference in the output is a difference in the model
and nothing else.

Backends
--------
  --backend keras   run the .h5 through Keras. Inside docker/Dockerfile this
                    produces the AUTHORITATIVE reference (TF 2.6.2 / py3.6).
                    Outside it, a useful but non-authoritative cross-check.
  --backend coreml  run the .mlpackage. macOS only -- coremltools cannot
                    execute a Core ML model on Linux (libcoremlpython is not
                    built there), so this step cannot be done anywhere but the
                    Mac.

Typical use:

    # 1. reference, inside the container (authoritative)
    faceage shell -- python tools/coreml/parity.py --backend keras \
        --emit fixtures/keras_tf2.6.2.json --label container-tf2.6.2

    # 2. candidate, on the Mac
    python tools/coreml/parity.py --backend coreml \
        --model FaceAgeRegressor.mlpackage \
        --reference tools/coreml/fixtures/keras_tf2.6.2.json

Exit status is 0 only if every fixture is within tolerance.
"""
from __future__ import print_function

import os
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')

import sys
import json
import hashlib
import argparse
import warnings
warnings.filterwarnings('ignore')

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faceage_keras import load, standardize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CROPS = os.path.join(HERE, 'fixtures', 'crops_uint8.npz')
DEFAULT_TOL = 1e-4


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run_keras(model_path, crops):
    model = load(model_path)
    out = []
    for i in range(len(crops)):
        x, _, _ = standardize(crops[i])
        out.append(float(np.squeeze(model.predict(x[None, ...], verbose=0))))
    return out


def run_coreml(model_path, crops):
    import platform
    if platform.system() != 'Darwin':
        raise SystemExit(
            'Core ML inference requires macOS (running on %s).\n'
            'coremltools can CONVERT on Linux but cannot PREDICT there -- '
            'libcoremlpython is not built for it. Run this step on the Mac.'
            % platform.system())

    import coremltools as ct
    m = ct.models.MLModel(model_path)
    in_name = m.get_spec().description.input[0].name
    out_name = m.get_spec().description.output[0].name
    out = []
    for i in range(len(crops)):
        x, _, _ = standardize(crops[i])
        y = m.predict({in_name: x[None, ...].astype(np.float32)})[out_name]
        out.append(float(np.squeeze(np.asarray(y))))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--backend', choices=['keras', 'coreml'], required=True)
    p.add_argument('--model', default=None,
                   help='.h5 for keras, .mlpackage for coreml')
    p.add_argument('--reference', default=None,
                   help='reference JSON to compare against')
    p.add_argument('--emit', default=None,
                   help='write results as a new reference JSON instead of comparing')
    p.add_argument('--label', default=None)
    p.add_argument('--tolerance', type=float, default=DEFAULT_TOL)
    args = p.parse_args()

    model_path = args.model or ('models/faceage_model.h5' if args.backend == 'keras'
                                else 'FaceAgeRegressor.mlpackage')
    if not os.path.exists(CROPS):
        raise SystemExit('missing fixtures: %s' % CROPS)
    crops = np.load(CROPS)['crops']

    vals = (run_keras if args.backend == 'keras' else run_coreml)(model_path, crops)

    if args.emit:
        doc = {'n': int(len(crops)),
               'crops_sha256': sha256_file(CROPS),
               'produced_by': args.label or args.backend,
               'backend': args.backend,
               'records': [{'i': i, 'faceage': v} for i, v in enumerate(vals)]}
        with open(args.emit, 'w') as fh:
            json.dump(doc, fh, indent=2)
        print('wrote %s (%d records)' % (args.emit, len(vals)))
        return 0

    if not args.reference:
        raise SystemExit('--reference is required unless --emit is given')
    with open(args.reference) as fh:
        ref = json.load(fh)

    if ref.get('crops_sha256') and ref['crops_sha256'] != sha256_file(CROPS):
        raise SystemExit('ABORT: fixture crops differ from those used for the '
                         'reference. The comparison would be meaningless.')

    rv = [r['faceage'] for r in ref['records']]
    if len(rv) != len(vals):
        raise SystemExit('ABORT: reference has %d records, got %d'
                         % (len(rv), len(vals)))

    d = np.abs(np.array(vals) - np.array(rv))

    print('=' * 72)
    print('REGRESSOR PARITY  %s  vs  %s' % (args.backend, ref.get('produced_by', '?')))
    print('=' * 72)
    print('model      : %s' % model_path)
    print('reference  : %s' % args.reference)
    print('fixtures   : %d' % len(vals))
    print('')
    print('%-4s %16s %16s %14s' % ('i', 'reference', args.backend, 'abs_diff'))
    for i in np.argsort(-d)[:10]:
        print('%-4d %16.6f %16.6f %14.3e' % (i, rv[i], vals[i], d[i]))
    print('')
    print('max abs diff    : %.6e' % d.max())
    print('mean abs diff   : %.6e' % d.mean())
    print('within %.0e     : %d / %d' % (args.tolerance,
                                         int((d <= args.tolerance).sum()), len(d)))
    ok = bool(d.max() <= args.tolerance)
    print('')
    print('RESULT: %s (tolerance %.0e)' % ('PASS' if ok else 'FAIL', args.tolerance))
    print('=' * 72)
    if not ok:
        print('A failure here means the converted model is a DIFFERENT '
              'INSTRUMENT, not a port. Do not score a series with it.')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
