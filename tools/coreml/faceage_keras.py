#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Load the FaceAge .h5 outside the reference container, and share the
preprocessing arithmetic with the parity tooling.

WHY THIS EXISTS
---------------
`keras.models.load_model('faceage_model.h5')` fails on any Python newer than
3.6 with:

    EOFError: EOF read where object expected   (marshal.loads)

The model is a Sequential of [inception_resnet_v1, dense_1,
classifier_1_BatchNorm, dense_2], saved by Keras 2.2.5. The nested
inception_resnet_v1 contains 21 `*_ScaleSum` Lambda layers whose Python
function is stored as marshalled Python 3.6 bytecode. Python 3.7+ cannot
unmarshal it, so the load dies before any weight is read.

The bytecode was decoded rather than guessed. All 21 Lambdas carry a
byte-identical body:

    LOAD_FAST 0 (inputs) / LOAD_CONST 1 (0) / BINARY_SUBSCR
    LOAD_FAST 0 (inputs) / LOAD_CONST 2 (1) / BINARY_SUBSCR
    LOAD_FAST 1 (scale)  / BINARY_MULTIPLY  / BINARY_ADD / RETURN_VALUE

    => lambda inputs, scale: inputs[0] + inputs[1] * scale

with the scale carried separately in each layer's `arguments` dict:
0.17 x5 (Block35), 0.1 x10 (Block17), 0.2 x5 (Block8), 1.0 x1 (final Block8) --
the canonical Inception-ResNet v1 residual ladder.

Order matters: `inputs[0]*scale + inputs[1]` would also run, also produce
plausible ages, and be silently wrong in all 21 residual blocks. That is why it
was read out of the bytecode instead of recalled from the reference
implementation.

Substituting a named, serializable layer both fixes the load and is *required*
for conversion -- coremltools cannot convert an opaque Python Lambda.

This module does NOT replace the container. docker/Dockerfile remains the
numerical reference; see tools/coreml/README.md.
"""
from __future__ import print_function

import os
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')

import warnings
warnings.filterwarnings('ignore')

import numpy as np

MODEL_INPUT_SIZE = (160, 160)


# ----------------------------------------------------------------------------
# preprocessing -- must stay identical to src/faceage_run.py predict_faceage()
# ----------------------------------------------------------------------------

def standardize(crop_uint8):
    """(x - mean) / std, computed per image from the crop itself.

    Mirrors src/faceage_run.py:90-92. This is per-image, not a fixed dataset
    mean/std, which is why the Core ML model takes a plain float tensor rather
    than an ImageType with a baked-in scale/bias (see convert.py).

    Returns (standardized_float32, mean, std). mean and std are the pipeline's
    crop_luma_mean / crop_luma_std, which feed the +/-5 luma gate.
    """
    crop = np.asarray(crop_uint8)
    mean, std = crop.mean(), crop.std()
    x = (crop - mean) / std
    return x.astype(np.float32), float(mean), float(std)


# ----------------------------------------------------------------------------
# model loading
# ----------------------------------------------------------------------------

def _keras():
    import keras
    return keras


def make_scale_sum_class():
    keras = _keras()

    class ScaleSum(keras.layers.Layer):
        """Serializable stand-in for the original marshalled Lambda."""

        def __init__(self, scale, **kwargs):
            super(ScaleSum, self).__init__(**kwargs)
            self.scale = float(scale)

        def call(self, inputs):
            return inputs[0] + inputs[1] * self.scale

        def get_config(self):
            cfg = super(ScaleSum, self).get_config()
            cfg['scale'] = self.scale
            return cfg

    return ScaleSum


def load(path):
    """Load the FaceAge model with the 21 Lambdas substituted."""
    keras = _keras()
    ScaleSum = make_scale_sum_class()

    def from_config(cls, config, custom_objects=None):
        return ScaleSum(scale=config['arguments']['scale'], name=config['name'])

    original = keras.layers.Lambda.from_config
    keras.layers.Lambda.from_config = classmethod(from_config)
    try:
        return keras.models.load_model(path, compile=False)
    finally:
        keras.layers.Lambda.from_config = original


def verify_weights(model, h5_path):
    """Confirm every weight tensor in the rebuilt graph is bit-identical to the
    .h5. Guards against the substitution silently shifting weight assignment.

    Returns (n_exact, n_differing, n_unmatched).
    """
    import h5py
    from collections import defaultdict

    f = h5py.File(h5_path, 'r')
    h5w = {}

    def visit(name, obj):
        if isinstance(obj, h5py.Dataset):
            h5w[name] = np.asarray(obj)

    f['model_weights'].visititems(visit)

    by_key = defaultdict(list)
    for k, v in h5w.items():
        by_key[(k.split('/')[-1], v.shape)].append((k, v))

    mw = {}

    def collect(layer):
        for sub in getattr(layer, 'layers', []):
            collect(sub)
        for w in layer.weights:
            mw[w.name] = w.numpy()

    for l in model.layers:
        collect(l)

    exact = differing = unmatched = 0
    for name, v in mw.items():
        cands = by_key.get((name.split('/')[-1], v.shape))
        if not cands:
            unmatched += 1
            continue
        base = name.split('/')[0]
        pick = next((c for c in cands if base in c[0]), cands[0])
        if np.abs(pick[1].astype(np.float64) - v.astype(np.float64)).max() == 0.0:
            exact += 1
        else:
            differing += 1
    return exact, differing, unmatched


if __name__ == '__main__':
    import sys
    h5 = sys.argv[1] if len(sys.argv) > 1 else 'models/faceage_model.h5'
    m = load(h5)
    print('LOADED OK')
    print('  input  :', m.inputs[0].shape.as_list(), m.inputs[0].dtype.name)
    print('  output :', m.outputs[0].shape.as_list(), m.outputs[0].dtype.name)
    print('  params : {:,}'.format(m.count_params()))
    e, d, u = verify_weights(m, h5)
    print('  weights: %d bit-identical, %d differing, %d unmatched' % (e, d, u))
    sys.exit(0 if (d == 0 and u == 0) else 2)
