# Pre-registration — Younger 2027 FaceAge study

**Drafted:** 2026-09-09
**Amended:** 2026-09-12 — see [Amendments](#amendments)
**Capture standard:** defers to `Younger 2027 — FaceAge Improvement Pro.md` §3
**Baseline (B):** not yet set — see [Calendar](#calendar). This decision is time-critical.
**Retest (R):** chosen by me, ≥6 months after B, no later than 2027-08-01

---

## Why this document exists

The Younger 2027 contest takes one baseline and one retest. The scored quantity is the
**change in deviation**:

> (FaceAge − chronological age at B) − (FaceAge − chronological age at R)

This repo runs the same published model locally between those two points, to see whether
the number is moving, when it started, and whether it is still moving.

Three things are true at once:

- the effect is small — a realistic six-month gain is "a small number of years at best,
  and potentially within the noise band of a single photo pair" (Improvement Protocol §9)
- the instrument is noisy — see [the confound table](#what-this-rests-on) below
- the person running the study is the person hoping for a particular answer

That combination is how self-experiments produce results that feel meaningful and aren't.

So the exclusion rules, the analysis, and the stopping rule are written **before the data
exists** and committed to git, where the timestamp is not something I can quietly revise
once I can see which way the line is going.

This document does not forbid changing the plan. It forbids changing it *invisibly*.

## Scope — what this document does and does not cover

It deliberately does **not** restate the capture standard or the intervention plan. Those
live in `Younger 2027 — FaceAge Improvement Pro.md` §3 and §4, and duplicating them here
would create a third, drifting copy.

This document commits only to the things that decide whether the resulting numbers can be
trusted: **what gets excluded, how it is analysed, when it stops, and what would falsify
it.**

Where the two disagree: **§3 governs capture, this document governs analysis.**

---

## What this rests on

Harvard's paired-photo robustness tests (Improvement Protocol §3):

| Variable | Mean absolute swing |
|---|---|
| **Makeup vs. none** | **4.37 yr** |
| Illumination (light vs. dark) | 2.35 yr |
| Expression (neutral vs. smiling) | 2.19 yr |
| Head angle (22.5° vs. frontal) | ~1.1–1.2 yr |

Measured on this pipeline (README §3):

| Variable | Swing |
|---|---|
| Lighting, 0.5× → 1.5× brightness | 3.69 yr, monotonic — darker reads older |
| Capture setup change (webcam vs. phone, 10 min apart) | 2.2 yr |
| Within-session precision (SE across 8–12 photos) | ±0.32 yr |

**Every single-variable confound above is larger than the effect being measured.** Makeup
alone outweighs any realistic six-month intervention result.

Two structural risks follow:

1. **Seasonality.** A window running from autumn into summer is the worst case for daylight
   drift, producing exactly the smooth monotonic trend a trend-fitting analysis is built to
   detect. §3's fixed-light requirement is therefore load-bearing, not best practice.
2. **Where the model looks.** Saliency concentrates on nasolabial folds, forehead and the
   temporalis area (§2). Overhead light casts shadow into precisely those regions and reads
   as older — so lighting geometry is not a general quality concern but a direct attack on
   the measured signal.

The model is additionally not fine-tuned on chronological age, by the authors' deliberate
choice; per-image dispersion is wide by design. **The session mean is the unit of analysis.
A single photograph is not interpretable.**

---

## 1. Capture — deferred, with pre-committed validity conditions

The capture standard is `Younger 2027 — FaceAge Improvement Pro.md` §3, fixed at the
rehearsal sessions and never changed thereafter.

What is pre-committed **here** is when a session does not count. A session is a **protocol
failure** if any of the following was true:

- any makeup, tinted product, or unusual grooming state differing from baseline
- overhead or non-frontal primary light; any light source other than the fixed rig
- a camera, lens, distance, height, or framing differing from baseline
  (one camera for the whole series — a propped phone or the Mac's built-in camera via the app —
  at a marked distance and height; never hand-held, never ultrawide, never switched)
- non-neutral expression, or head not level
- any §3 photo-day control violated: alcohol within 48h, sodium above cap the prior day,
  under 7h sleep, wrong wake-to-photo interval, hot shower/sauna/hard training within 2h,
  facial treatment within 2 weeks
- sunburn, allergy flare, illness, or a visible blemish in an attention region

**A protocol failure must be recorded, with its reason, before the session's score is
looked at.** A session invalidated after its number is known is not an exclusion; it is a
result being discarded for being inconvenient.

## 2. Exclusion rules

Within a valid session, a photograph is excluded **only** by the pipeline's existing
automated flags:

`NO_FACE_DETECTED` · `MULTIPLE_FACES` · `SOURCE_TOO_SMALL` · `DEGENERATE_CROP` ·
`UNREADABLE_IMAGE` · `PREDICTION_FAILED` · `FACE_FILL_BELOW_80_PCT` · `LOW_CONFIDENCE` ·
`CROP_UPSCALED` · `FACE_CLIPPED_AT_BORDER`

**No photograph and no session is ever excluded on the basis of its FaceAge value.**

Thresholds (`--min-face-fill`, `--include-flagged`) are set here, at their defaults, and are
not tuned mid-study.

## 3. Analysis

Specified before any data is collected.

- **Primary estimate:** OLS slope of session mean against date, across all valid,
  non-excluded sessions.
- Reported in FaceAge-years per month, **with a 95% confidence interval**.
- **If the CI includes zero, the result is "not detected."** Not "trending," not "early
  signs," not "directionally encouraging."
- **Secondary:** total modelled change across the window, with CI.
- No subgroup analysis. No window trimming. No switching to a different model form because
  a different one fits better.

Chronological-age drift across the window is known and linear, so the slope of raw FaceAge
and the slope of the deviation differ by a known constant. **Raw FaceAge is analysed; the
deviation is reported alongside it** for comparability with the contest's scored quantity.

### Sampling rationale

A realistic six-month result is on the order of 0.2–0.3 yr/month — at or below the noise
floor for any single session-to-session comparison. **Pairwise deltas are not interpreted.
Only the fitted trend is.**

Approximate SE of the total modelled change over a 26-week window, for between-session
noise σ:

| Cadence | n | SE of total change | Detectable at 2 SE |
|---|---|---|---|
| Monthly | 6 | ≈ 1.31 σ | ≈ 2.6 σ |
| Weekly | 26 | ≈ 0.68 σ | ≈ 1.4 σ |

Assumes independent errors and a linear trend. Weekly sampling roughly halves the minimum
detectable change for the same window and the same rigor. Improvement Protocol §6 specifies
monthly reference sets; **more frequent sampling is strictly better for the trend estimate
and costs nothing but time.**

## 4. Repeatability study and decision gate

Runs during the rehearsal window (§6: "B−2 weeks — three practice sessions"), before the
baseline set. **Five sessions in seven days, identical rig**: 2026-09-11, 09-13, 09-14,
09-16, 09-17.

Real facial change over seven days is approximately zero, so the standard deviation of
those five session means estimates σ, the between-session measurement noise. This quantity
is currently unknown and governs whether the local series is worth running at all.

| σ (yr) | Detectable over 6 mo, weekly | Decision |
|---|---|---|
| ≤ 0.5 | ~0.7 yr | Proceed |
| 0.5 – 1.0 | ~0.7 – 1.4 yr | Improve the rig, re-run the repeatability study |
| > 1.0 | > 1.4 yr | Local series cannot resolve the effect — do not build tooling on it |

The gate is evaluated **before the direction of any real trend is known**, so the decision
to proceed cannot be motivated by the result.

These sessions are rehearsal. **They are not submitted and do not enter the series.**

## 5. Stopping rule

The local series runs to the retest. **It does not stop early because the number looks
good, and it does not stop early because it looks bad.**

## 6. Falsification check

`mean_luma` — the mean pixel value of the face crop before normalisation — is plotted on a
shared x-axis directly beneath the FaceAge series, every time the series is reviewed.

**If the two trends share a shape, the study has measured photography, not biology.**

Run *before* the result is interpreted, not after it is challenged.

## 7. Relationship to the official contest numbers

The local pipeline and the contest's measurement are different instruments. The contest's
model version may differ from the public release, and its preprocessing and crop almost
certainly differ from this one (Improvement Protocol §8, item 3).

- Absolute agreement is **not** expected, and its absence is not a finding.
- The local series is a **relative trend line**, never the official score.
- What is compared is **shape**: whether the modelled local change agrees in direction and
  rough magnitude with the official B→R delta.
- Disagreement in *direction* means one instrument is wrong, and the local series will have
  many points plus a luma trace available to interrogate, against the contest's two.

---

## Calendar

Anchored on **B** (baseline submission) and **R** (retest). All study dates count from B;
see Improvement Protocol §6 for the full intervention calendar.

| When | This document's concern |
|---|---|
| B−2 wk | Rehearsal + repeatability study (§4). Rig fixed at the end of this window and not changed again. |
| B−2 wk | **Decision gate** (§4). Proceed / improve rig / abandon local series. |
| **B** | Baseline set submitted. Local baseline session captured the same day, same rig. |
| B → R | Reference sessions, weekly where practical (§3 sampling rationale), monthly minimum per §6. |
| **R** | Retest set. Local closing session same day, same rig. |
| after R | Pre-registered analysis run exactly as specified in §3. |

**B is not yet set, and the choice is consequential.** The six-month clock starts at
baseline submission and R is due no later than 2027-08-01, so an early B buys intervention
time: a late-September 2026 baseline yields ~10 months, a 2027-02-01 baseline exactly six
(Improvement Protocol §8, item 4). Counterweight: a later R sits deeper into summer sun
exposure, and it is unclear whether the composite normalises by elapsed time.

---

## Amendments

Any change to this document after the baseline session must be:

1. A **new commit**, not an edit that hides what it replaced.
2. **Dated**, with the reason stated.
3. Recorded in the table below.

Where an amendment changes the analysis, the result under the **original** specification is
reported alongside the amended one.

| Date | Section | Change | Reason |
|---|---|---|---|
| 2026-09-12 | Throughout | Capture standard now defers to `Younger 2027 — FaceAge Improvement Pro.md` §3 rather than restating it. Added the Harvard paired-photo confound table, makeup and photo-day controls as session-validity conditions, the saliency/overhead-light rationale, the deviation change score as the scored quantity, and a B-anchored calendar replacing fixed 2026 dates. Repeatability study reframed as rehearsal-window prep. | Original draft was written without sight of the Improvement Protocol, which was committed 2026-09-11. It omitted makeup — the single largest confound at 4.37 yr — and all photo-day controls, and assumed a fixed baseline date when B is in fact a live decision. Amended before any study data exists. |
| 2026-09-12 | §1 | Camera condition reworded from "rear main camera only" to "same camera every time, phone propped at a marked distance — never hand-held, never ultrawide". | The front camera is the only practical option for solo capture. The concern behind "rear main" was perspective distortion of the midface at close range; with the phone propped at a fixed distance that distortion is a constant offset, and the series is a relative trend line (§7), so a constant offset cannot move it. What would move it is distance varying between sessions, which is what the reworded condition rules out. Amended before B is set. |
| 2026-09-12 | §1 | Camera condition extended: the Mac's built-in camera, driven by the app with a fixed framing overlay, is an acceptable instrument alongside a propped phone. The condition now reads "one camera for the whole series … never switched". | The 2.2-year webcam-vs-phone gap in the README is a gap *between* two instruments, not noise *within* one. The series is a relative trend (§7), so any instrument that is fixed in place and never changed gives a constant offset that cannot move the slope. A Mac on a desk is, if anything, easier to keep fixed than a propped phone. What the condition must rule out is switching cameras mid-series: a session on a different camera from the rest is a §1 failure. The app records which camera produced each session (`capture.json`) and warns before a mixed session is added. Amended before B is set. |
