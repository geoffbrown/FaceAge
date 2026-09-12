#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate deterministic parity fixtures for the FaceAge regressor.

The port has two independent parity problems and they should not be tested
together:

  1. the REGRESSOR   .h5 -> Core ML. Input is a standardized 160x160x3 float
                     tensor, so it can be exercised with synthetic tensors.
  2. MTCNN           needs real faces, because what is being compared is where
                     the box lands. Not covered here.

Conflating them means a parity failure tells you nothing about which half
broke. This file covers (1) only, and needs no face photographs at all -- no
UTK images, no biometric data, nothing that cannot sit in git.

WHAT IS STORED: uint8 crops, not float tensors. Standardization is exact
float64 arithmetic from uint8, so both sides derive bit-identical inputs from
the same stored bytes. Storing floats generated from sin/cos would make the
fixtures depend on the platform's libm at the ULP level.
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
from faceage_keras import load, standardize, verify_weights  # noqa: E402

SEED = 20260912
N = 24
HERE = os.path.dirname(os.path.abspath(__file__))
FIXDIR = os.path.join(HERE, 'fixtures')
CROPS = os.path.join(FIXDIR, 'crops_uint8.npz')


def build_crops():
    """Synthetic uint8 crops with spatial structure and a swept exposure range,
    so standardization sees the mean/std spread real photos produce."""
    rng = np.random.default_rng(SEED)
    yy, xx = np.mgrid[0:160, 0:160].astype(np.float64)
    out = []
    for i in range(N):
        base = (128
                + 40 * np.sin(2 * np.pi * (xx / 160.0) * (1 + i % 3))
                * np.cos(2 * np.pi * (yy / 160.0) * (1 + i % 5))
                + 25 * np.sin(2 * np.pi * ((xx + yy) / 160.0) * (1 + i % 7)))
        img = np.stack([base, base * 0.94 + 6, base * 0.88 + 12], axis=-1)
        img += rng.normal(0, 8, img.shape)
        gain = 0.45 + 1.25 * (i / float(max(N - 1, 1)))
        out.append(np.clip(img * gain, 0, 255).astype(np.uint8))
    return np.stack(out)


def load_crops():
    if not os.path.exists(CROPS):
        raise SystemExit('missing %s -- run with --write-crops first' % CROPS)
    return np.load(CROPS)['crops']


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='models/faceage_model.h5')
    p.add_argument('--write-crops', action='store_true',
                   help='regenerate the crop fixtures (normally committed)')
    p.add_argument('--out', default=None,
                   help='reference JSON to write (default: fixtures/keras_<tf>.json)')
    p.add_argument('--label', default=None,
                   help='label for the environment that produced these values')
    args = p.parse_args()

    if not os.path.isdir(FIXDIR):
        os.makedirs(FIXDIR)

    if args.write_crops:
        crops = build_crops()
        np.savez_compressed(CROPS, crops=crops)
        print('wrote %s %s' % (CROPS, crops.shape))
    crops = load_crops()

    import tensorflow as tf
    label = args.label or ('keras-tf%s' % tf.__version__)
    out = args.out or os.path.join(FIXDIR, 'keras_tf%s.json' % tf.__version__)

    model = load(args.model)
    exact, differing, unmatched = verify_weights(model, args.model)
    if differing or unmatched:
        raise SystemExit('ABORT: weight verification failed '
                         '(%d differing, %d unmatched)' % (differing, unmatched))
    print('weights verified: %d tensors bit-identical to the .h5' % exact)

    records = []
    for i in range(len(crops)):
        x, mean, std = standardize(crops[i])
        y = model.predict(x[None, ...], verbose=0)
        records.append({'i': i,
                        'crop_luma_mean': round(mean, 6),
                        'crop_luma_std': round(std, 6),
                        'faceage': float(np.squeeze(y))})

    doc = {
        'seed': SEED,
        'n': int(len(crops)),
        'crops_file': os.path.relpath(CROPS, HERE),
        'crops_sha256': sha256_file(CROPS),
        'model_sha256': sha256_file(args.model),
        'produced_by': label,
        'tensorflow': tf.__version__,
        'records': records,
    }
    with open(out, 'w') as fh:
        json.dump(doc, fh, indent=2)
    print('wrote %s' % out)

    vals = np.array([r['faceage'] for r in records])
    print()
    print('%-4s %14s %14s %12s' % ('i', 'luma_mean', 'luma_std', 'faceage'))
    for r in records:
        print('%-4d %14.4f %14.4f %12.6f'
              % (r['i'], r['crop_luma_mean'], r['crop_luma_std'], r['faceage']))
    print()
    print('range %.4f .. %.4f   spread %.4f' % (vals.min(), vals.max(), vals.ptp()))


if __name__ == '__main__':
    main()
