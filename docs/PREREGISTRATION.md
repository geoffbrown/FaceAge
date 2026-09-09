# Pre-registration — Younger interim tracking study

**Drafted:** 2026-09-09
**Repeatability study begins:** 2026-09-11
**Baseline anchor:** to be set, alongside the Younger benchmark session
**Closing anchor:** ~6 months after baseline

---

## Why this document exists

Younger takes one baseline benchmark and one revisit six months later. Two data points.
This study runs the same underlying model locally to fill in the gap — to see whether the
number is moving, when it started, and whether it is still moving.

The value being estimated is small, the instrument is noisy, and the person running the
study is the person hoping for a particular answer. Those three facts together are how
self-experiments produce results that feel meaningful and aren't.

So the protocol, the exclusion rules, the analysis, and the stopping rule are written
**before the data exists** and committed to git, where the timestamp is not something I can
quietly revise once I can see which way the line is going.

This document does not forbid changing the plan. It forbids changing it *invisibly*. See
[Amendments](#amendments).

---

## What this rests on

Measured on this pipeline, documented in the README:

| Source of variation | Magnitude |
|---|---|
| Within-session precision (SE across 8–12 photos) | ±0.32 yr |
| Lighting, 0.5× → 1.5× brightness | 3.69 yr, monotonic — darker reads older |
| Capture setup change (webcam vs. phone, 10 min apart) | 2.2 yr |
| Plausible real 6-month change from a strong intervention | ~1–2 yr |

**The dominant confound is larger than the effect being measured.** A six-month window is
also the worst possible length for seasonal daylight drift, because it begins in one season
and ends in the opposite one — producing exactly the smooth monotonic trend that a
trend-fitting analysis is designed to detect.

The model is additionally not fine-tuned on chronological age (README §6), by the authors'
deliberate choice. Per-image dispersion is wide by design. The session mean is the unit of
analysis; a single photograph is not interpretable.

---

## 1. Capture protocol

Fixed for every session, including both anchors.

- Same room, **after dark**, blinds closed. Daylight contribution structurally zero, not
  merely reduced.
- Same lamp, same position, same output. No other light source.
- Floor position and camera height **physically marked**, not remembered.
- Same camera, same lens, same distance, every session.
- **One machine owns the camera** and performs all scoring.
- Exposure, ISO and white balance **locked to session-one values** — not auto-exposed per
  session.
- Neutral expression, eyes open, mouth closed. No glasses, hat, or hair across the face.
- 8–12 photos per session; **20–30 for the two anchor sessions**.

The locked capture parameters — exposure, ISO, white balance, distance, height — are the
record of the setup. They are what reproduces it, not a reference photograph.

## 2. Exclusion rules

A photograph is excluded **only** by the pipeline's existing automated flags:

`NO_FACE_DETECTED` · `MULTIPLE_FACES` · `SOURCE_TOO_SMALL` · `DEGENERATE_CROP` ·
`UNREADABLE_IMAGE` · `PREDICTION_FAILED` · `FACE_FILL_BELOW_80_PCT` · `LOW_CONFIDENCE` ·
`CROP_UPSCALED` · `FACE_CLIPPED_AT_BORDER`

**No photograph and no session is excluded on the basis of its FaceAge value.** A session
that looks wrong is data about the protocol, not grounds for deletion.

A session may be excluded for a *protocol failure* (wrong room, lamp moved, exposure not
locked) only if the failure is logged, with its reason, **before the session's score is
looked at**.

Thresholds (`--min-face-fill`, `--include-flagged`) are set here, at their defaults, and are
not tuned mid-study.

## 3. Analysis

Specified before any data is collected.

- **Primary estimate:** OLS slope of session mean against date, across all non-excluded
  sessions.
- Reported in FaceAge-years per month, **with a 95% confidence interval**.
- **If the CI includes zero, the result is "not detected."** Not "trending," not "early
  signs," not "directionally encouraging."
- **Secondary:** total modelled change across the window, with CI.
- No subgroup analysis. No window trimming. No switching to a different model form because
  a different one fits better.

### Sampling rationale

A strong six-month result is on the order of 0.2–0.3 yr/month, at or below the noise floor
for any single session-to-session comparison. **Pairwise deltas are not interpreted.** Only
the fitted trend is.

Approximate SE of the six-month total change over a 26-week window, for between-session
noise σ:

| Cadence | n | SE of total change | Detectable at 2 SE |
|---|---|---|---|
| Monthly | 6 | ≈ 1.31 σ | ≈ 2.6 σ |
| Weekly | 26 | ≈ 0.68 σ | ≈ 1.4 σ |

Assumes independent errors and a linear trend. Weekly sampling roughly halves the minimum
detectable change for the same window and the same rigor.

## 4. Repeatability study and decision gate

**Before the baseline anchor:** five sessions in seven days (2026-09-11, 09-13, 09-14,
09-16, 09-17), identical setup.

Real facial change over seven days is approximately zero, so the standard deviation of
those five session means estimates σ, the between-session measurement noise. This quantity
is currently unknown and governs whether the study is worth running at all.

| σ (yr) | Detectable over 6 mo, weekly | Decision |
|---|---|---|
| ≤ 0.5 | ~0.7 yr | Proceed |
| 0.5 – 1.0 | ~0.7 – 1.4 yr | Improve the rig, re-run the repeatability study |
| > 1.0 | > 1.4 yr | Stop — the instrument cannot resolve the effect |

The gate is evaluated **before the direction of any real trend is known**, so the decision
to proceed cannot be motivated by the result.

## 5. Stopping rule

The study runs to the Younger revisit date.

**It does not stop early because the number looks good, and it does not stop early because
it looks bad.**

## 6. Falsification check

`mean_luma` — the mean pixel value of the face crop before normalisation — is plotted on a
shared x-axis directly beneath the FaceAge series, every time the series is reviewed.

**If the two trends share a shape, the study has measured photography, not biology.**

This check is run *before* the result is interpreted, not after it is challenged.

## 7. Cross-instrument comparison

The local series and Younger's two measurements are different instruments on different
scales — different camera, lighting, and possibly model version or preprocessing.

- Absolute agreement is **not** expected and its absence is not a finding.
- What is compared is **shape**: whether the modelled total change over the window agrees
  in direction and rough magnitude with the delta Younger reports.
- Disagreement in *direction* means one instrument is wrong, and the local series has ~26
  points plus a luma trace available to interrogate.

---

## Amendments

Any change to this document after 2026-09-11 must be:

1. A **new commit**, not an edit that hides what it replaced.
2. **Dated**, with the reason stated.
3. Recorded in the table below.

Where an amendment changes the analysis, the result under the **original** specification is
reported alongside the amended one.

| Date | Section | Change | Reason |
|---|---|---|---|
| — | — | — | — |
