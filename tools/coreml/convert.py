#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FaceAge regressor: Keras .h5 -> Core ML .mlpackage.

Three decisions here are load-bearing.

1. compute_precision = FLOAT32.
   coremltools defaults to float16. The pipeline's validation tolerance is
   1e-04 and the container currently meets it with 4.72e-05 of headroom;
   float16 carries ~3 decimal digits and blows that immediately. Verified after
   conversion by weight.bin bytes-per-parameter, which must be 4.00 -- a
   silent float16 fallback reads 2.00.

2. Input is a TensorType (MLMultiArray), NOT an ImageType.
   src/faceage_run.py standardizes each crop by its own mean and std:
       mean, std = pat_face.mean(), pat_face.std()
       pat_face = (pat_face - mean) / std
   That is per-image. An ImageType bakes in a FIXED scale/bias, which cannot
   express it. The caller standardizes and passes floats. No loss: the caller
   must compute mean/std anyway, since they are crop_luma_mean/crop_luma_std,
   which feed the +/-5 luma gate in docs/MAC_APP.md.

3. Features are renamed after conversion, not during.
   The TF frontend keys `inputs=`/`outputs=` off real graph node names
   (inception_resnet_v1_input), so a friendly name passed to ct.convert is
   rejected with "not found in given tensorflow graph".

NOT COVERED: MTCNN. Converting the regressor alone does not give a shippable
app -- see tools/coreml/README.md.
"""
from __future__ import print_function

import os
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')

import sys
import argparse
import warnings
warnings.filterwarnings('ignore')

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faceage_keras import load, verify_weights  # noqa: E402

EXPECTED_PARAMS = 22825169


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='models/faceage_model.h5')
    p.add_argument('--out', default='FaceAgeRegressor.mlpackage')
    args = p.parse_args()

    import coremltools as ct

    print('loading %s ...' % args.model)
    model = load(args.model)

    exact, differing, unmatched = verify_weights(model, args.model)
    if differing or unmatched:
        raise SystemExit('ABORT: weight verification failed '
                         '(%d differing, %d unmatched)' % (differing, unmatched))
    print('weights verified: %d tensors bit-identical to the .h5' % exact)
    if model.count_params() != EXPECTED_PARAMS:
        raise SystemExit('ABORT: expected %d params, got %d'
                         % (EXPECTED_PARAMS, model.count_params()))

    print('converting (float32) ...')
    mlmodel = ct.convert(
        model,
        source='tensorflow',
        convert_to='mlprogram',
        inputs=[ct.TensorType(name=model.inputs[0].name.split(':')[0],
                              shape=(1, 160, 160, 3), dtype=np.float32)],
        compute_precision=ct.precision.FLOAT32,
        minimum_deployment_target=ct.target.macOS13,
    )

    spec = mlmodel.get_spec()
    weights_dir = mlmodel.weights_dir
    for have, want in ((spec.description.input[0].name, 'face'),
                       (spec.description.output[0].name, 'faceage')):
        if have != want:
            ct.utils.rename_feature(spec, have, want)
    mlmodel = ct.models.MLModel(spec, weights_dir=weights_dir)

    mlmodel.short_description = (
        'FaceAge regressor (AIM-Harvard/FaceAge). Input: per-crop standardized '
        '(x-mean)/std face crop, 160x160x3 NHWC float32. Output: age in years.')
    mlmodel.input_description['face'] = 'Standardized face crop, (x-mean)/std, NHWC'
    mlmodel.output_description['faceage'] = 'Estimated FaceAge in years'
    mlmodel.save(args.out)
    print('saved %s' % args.out)

    # ---- verify float32 actually survived ----------------------------------
    wbin = os.path.join(args.out, 'Data', 'com.apple.CoreML', 'weights', 'weight.bin')
    size = os.path.getsize(wbin)
    bpp = size / float(EXPECTED_PARAMS)
    spec = mlmodel.get_spec()
    dtypes = {65552: 'FLOAT16', 65568: 'FLOAT32', 65600: 'FLOAT64'}
    print()
    print('spec version      : %d' % spec.specificationVersion)
    print('input             : %s %s %s'
          % (spec.description.input[0].name,
             list(spec.description.input[0].type.multiArrayType.shape),
             dtypes.get(spec.description.input[0].type.multiArrayType.dataType, '?')))
    print('output            : %s %s'
          % (spec.description.output[0].name,
             dtypes.get(spec.description.output[0].type.multiArrayType.dataType, '?')))
    print('weight.bin        : {:,} bytes'.format(size))
    print('bytes / parameter : %.2f  (4.00 = float32, 2.00 = float16)' % bpp)

    if abs(bpp - 4.0) > 0.05:
        raise SystemExit('ABORT: weights are not float32 (%.2f bytes/param). '
                         'The 1e-04 tolerance cannot be met.' % bpp)
    print()
    print('float32 confirmed.')
    print('NOTE: conversion does not prove numerical parity. Run parity.py on '
          'macOS against the container reference.')


if __name__ == '__main__':
    main()
