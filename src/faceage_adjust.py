#!/usr/bin/env python
"""Make brightness-shifted copies of a session's photos, for measuring how much
exposure actually moves FaceAge on this person's own face.

Runs inside the scoring container (it needs PIL and numpy). Each output photo
is the input with every pixel multiplied by GAIN and clipped to 0-255: the
same kind of shift a camera's auto-exposure makes between two sittings.

    python faceage_adjust.py --input DIR --output DIR --gain 1.05
"""
from __future__ import print_function
import os
import sys
import argparse
import numpy as np
import PIL.Image
import PIL.ImageOps

EXTS = ('.jpg', '.jpeg', '.png')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--gain', type=float, required=True)
    a = p.parse_args()
    if not os.path.isdir(a.output):
        os.makedirs(a.output)
    n = 0
    for f in sorted(os.listdir(a.input)):
        if not f.lower().endswith(EXTS) or f.startswith('.'):
            continue
        img = PIL.Image.open(os.path.join(a.input, f))
        img = PIL.ImageOps.exif_transpose(img).convert('RGB')
        arr = np.asarray(img).astype(np.float32) * a.gain
        out = PIL.Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        out.save(os.path.join(a.output, os.path.splitext(f)[0] + '.jpg'), quality=95)
        n += 1
    print('wrote %d photo(s) at gain %.3f to %s' % (n, a.gain, a.output))
    return 0 if n else 1


if __name__ == '__main__':
    sys.exit(main())
