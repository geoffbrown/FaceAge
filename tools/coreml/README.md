# Core ML port — first pass

Step 2 of `docs/MAC_APP.md` ("getting the model out of Docker"). This is the
regressor half only, and parity is **not yet demonstrated**. Read the status
table before using any of it.

> The app build itself is gated on the repeatability study
> (`docs/PREREGISTRATION.md` §4, σ unknown until the 2026-09-17 session). This
> work is upstream of that gate: it establishes whether the port is *feasible*
> and what it costs. Nothing here commits to building the app.

## Status

| | |
|---|---|
| `.h5` loads outside the container | **Done** — needed a fix, see below |
| All 497 weight tensors bit-identical to the `.h5` | **Verified** |
| Keras → Core ML conversion | **Done** — `FaceAgeRegressor.mlpackage`, 1125 ops |
| float32 (not float16) | **Verified** — 4.00 bytes/parameter |
| Deterministic parity fixtures | **Done** — 24 crops, FaceAge 29.9–84.0 |
| Parity harness | **Done**, self-consistency 0.0 |
| **Core ML vs Keras numerical parity** | **NOT DONE — requires macOS** |
| **MTCNN port** | **NOT STARTED** — the harder half |

Everything above was produced and checked on Linux. Core ML *inference* cannot
run there at all (`libcoremlpython` is not built for it), so the one number
that decides whether this is a port or a different instrument has not been
measured yet. That step is on the Mac.

## What had to be solved

### The `.h5` will not load on any modern Python

    EOFError: EOF read where object expected   (marshal.loads)

The model is a Keras 2.2.5 `Sequential` of `[inception_resnet_v1, dense_1,
classifier_1_BatchNorm, dense_2]`. The nested `inception_resnet_v1` (426
layers) holds 21 `*_ScaleSum` `Lambda` layers whose function is stored as
**marshalled Python 3.6 bytecode**. Python 3.7+ cannot unmarshal it, and the
load dies before reading a single weight.

This is not incidental. coremltools cannot convert an opaque Python `Lambda`
either, so the substitution below is required for the port regardless.

### The Lambda was decoded, not guessed

All 21 bodies are byte-identical:

```
LOAD_FAST 0 (inputs) / LOAD_CONST 1 (0) / BINARY_SUBSCR
LOAD_FAST 0 (inputs) / LOAD_CONST 2 (1) / BINARY_SUBSCR
LOAD_FAST 1 (scale)  / BINARY_MULTIPLY  / BINARY_ADD / RETURN_VALUE

=> lambda inputs, scale: inputs[0] + inputs[1] * scale
```

with `scale` carried in each layer's `arguments`: 0.17 ×5 (Block35), 0.1 ×10
(Block17), 0.2 ×5 (Block8), 1.0 ×1 (final Block8) — the canonical
Inception-ResNet v1 ladder.

Worth being explicit about why this was read out of the bytecode rather than
recalled from the reference implementation: `inputs[0]*scale + inputs[1]` also
runs, also yields plausible ages, and would be **silently wrong in all 21
residual blocks**. There is no face photograph in this repo to catch that, so
the argument order had to come from the file itself.

### float32 is verified structurally, not assumed

`compute_precision=FLOAT32` is passed, and then checked: `weight.bin` is
91,202,816 bytes over 22,825,169 parameters = **4.00 bytes/parameter**. A
silent float16 fallback reads 2.00. `convert.py` aborts if it is not 4.00.

### The input is a tensor, not an image

`src/faceage_run.py` standardizes each crop by its *own* mean and std. That is
per-image, so it cannot be expressed as an `ImageType`'s fixed scale/bias. The
Core ML model therefore takes a plain `(1,160,160,3)` float32 `MLMultiArray`
and the caller standardizes. No loss — the caller needs `mean`/`std` anyway,
they are `crop_luma_mean`/`crop_luma_std` behind the ±5 luma gate.

## Fixtures

Regressor parity and MTCNN parity are **separated on purpose**. Conflating them
means a failure tells you nothing about which half broke.

The regressor's input is just a standardized float tensor, so it is exercised
with 24 synthetic crops — no face photographs, no UTK images, nothing
biometric in git. Stored as **uint8** (`fixtures/crops_uint8.npz`, 1.5 MB);
standardization from uint8 is exact float64 arithmetic, so both sides derive
bit-identical inputs. Storing floats built from `sin`/`cos` would have made the
fixtures depend on the platform's libm at the ULP level.

Exposure is swept across the set (luma mean 56 → 210), producing FaceAge 29.9 –
84.0. A wide spread makes the comparison sensitive.

`fixtures/keras_tf2.15.1.json` is a **cross-check, not the reference.** The
authoritative values come from TF 2.6.2 in `docker/Dockerfile`.

## Running it

Dependencies are for this tooling only; the container is untouched.

```bash
pip install "tensorflow-cpu==2.15.1" "numpy<2" coremltools
```

**1. Reference values, inside the container (authoritative):**

```bash
python tools/coreml/parity.py --backend keras \
  --emit tools/coreml/fixtures/keras_container.json --label container-tf2.6.2
```

**2. Convert:**

```bash
python tools/coreml/convert.py --model models/faceage_model.h5
```

**3. The step that actually decides it — on the Mac:**

```bash
python tools/coreml/parity.py --backend coreml \
  --model FaceAgeRegressor.mlpackage \
  --reference tools/coreml/fixtures/keras_container.json
```

Exit 0 only if every fixture is within 1e-04. The container currently meets
that against the authors' own CSV with 4.72e-05, so 1e-04 is the bar to hold.

If step 1 and the committed TF 2.15 JSON disagree materially, that is its own
finding — it would mean the numbers drift with TensorFlow version, which
matters for the series' comparability over time.

## What is left

**MTCNN is the real work and none of it is done.** `docs/MAC_APP.md` already
names it as the subtle risk and that still stands: Vision's
`VNDetectFaceRectanglesRequest` is not MTCNN, a different box is a different
160×160 crop, and swapping the detector yields a different instrument rather
than a port. All three nets (P/R/O) need converting, and unlike the regressor,
**testing them needs real faces** — the fixture trick does not apply, because
what is being compared is where the box lands.

Two pipeline quirks any MTCNN port has to reproduce exactly
(`src/faceage_run.py:81-88`):

- `x1, y1 = abs(x1), abs(y1)` — negative coordinates are **flipped, not
  clamped**, and there is no clipping at the right/bottom edge; numpy silently
  truncates instead.
- The crop is resized by `PIL.Image.resize` with its **default resampling,
  which is BICUBIC** (confirmed empirically — the default is unnamed in the
  call, so it is easy to reimplement as bilinear by accident). Core Image and
  vImage default to other kernels, and a different resampling kernel moves the
  number.

Also unresolved: `faceage_run.py` takes `faces[0]`, the detector's first
result, not the highest-confidence one. Any reimplementation must match that
ordering, not improve on it.
