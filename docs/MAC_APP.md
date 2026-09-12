# Mac app — specification

**Status:** spec only, not started. Build is gated on the repeatability study (§Gate).
**Drafted:** 2026-09-12

---

## Purpose

Not "score your face" — `faceage run` already does that. The app exists for one reason:

> **Reduce capture and scoring friction enough that session count gets large enough for the
> trend slope to be estimable.**

Weekly sampling roughly halves the minimum detectable change versus monthly, in the same
window, for the same rigor (`docs/PREREGISTRATION.md` §3). The CLI has enough friction that
monthly is what realistically happens. That gap is the whole product.

## What the app is not

**It is not the camera.**

Capture stays on the phone, on a tripod, per `Younger 2027 — FaceAge Improvement Pro.md`
§3: same phone, **rear main camera — never ultrawide**, since wide-angle barrel distortion
alters apparent midface geometry. A Mac webcam is a worse instrument, and this repo's own
README measured a webcam-vs-phone gap of **2.2 years** on photos taken ten minutes apart.

An earlier draft of this spec had the app driving the Mac's camera with an AVFoundation
exposure lock and a live framing overlay. That is discarded. It would have produced a
worse number with more confidence attached to it.

## Scope

**Import → QA → score → chart.**

1. **Import** a session's photos from the phone (AirDrop, Photos, Image Capture, or a
   watched folder).
2. **Stage** them into `$FACEAGE_DATA/subjects/<name>/sessions/YYYY-MM-DD/`.
3. **Pre-flight QA before scoring** — the checks worth having a UI for:
   - `mean_luma` of each face crop against the session-one baseline, with a **±5 gate**.
     Out of range is the early warning that lighting drifted, and lighting is the dominant
     photographic confound.
   - face fill, head level, detection confidence — surfaced as the pipeline's existing
     advisory flags, shown before the run rather than discovered in a CSV afterward.
   - a **session-validity checklist** from `docs/PREREGISTRATION.md` §1 (makeup, photo-day
     controls, lighting geometry, camera/lens). Answered **before** the score is computed,
     because a session invalidated after its number is known is a result being discarded
     for being inconvenient.
4. **Score** by invoking the existing pipeline. No reimplementation.
5. **Chart** — session means with error bars, fitted slope with CI, `mean_luma` on a shared
   x-axis beneath, B and R anchors marked.

## Data

- **Photos are saved**, not discarded. Re-scoring matters: the model may change, thresholds
  may change, and a weird session needs to be inspectable.
- **Location is user-set**, defaulting to `~/FaceAgeData` — the same root the CLI uses, via
  `FACEAGE_DATA` (`tools/faceage:8`). One data root, two front ends. If the app's location
  differs from the shell's, `faceage history` silently comes up empty, so changing it in
  the app offers to update the shell rc too.
- **Subjects already exist** in the CLI (`--subject`), with independent sessions, results,
  history and tracker. The app is a front end on that model, not a new one.

### iCloud

Syncing the tree is a net win — capture on whichever machine, one series. Two real failure
modes, neither of them privacy:

- **Eviction.** With Optimize Mac Storage on, evicted photos become `.name.jpg.icloud`
  stubs, which do not match the `*.jpg` glob in `tools/faceage:89`. They do not error —
  they silently vanish from the session and shrink `n`. Partial eviction is the bad case:
  a re-scored session reports a mean over 4 of 12 photos with no warning.
  → **The app must count stubs and refuse to score, or `brctl download` first.**
- **Conflicted history.** Two machines appending to a synced `faceage_history.csv` produces
  `faceage_history 2.csv` and a silently forked series. Photos are safe (written once);
  the append-only CSVs are not.
  → **One machine owns scoring.**

## Accounts and sync

- **No account is the default.** Local-only, fully functional.
- Optional sync for a handful of friends: per-person UUID, magic link if being polite.
  Supabase + RLS.
- **Sync rows, never images.** `session_date, n, mean, std, mean_luma, model_sha256`. Tiny,
  and it preserves the property the repo is built on — no image leaves the machine.
- **Per-person trends only. No shared leaderboard.** Different Macs, cameras, rooms. The
  webcam-vs-phone gap alone (2.2 yr) dwarfs any real difference between two similar-aged
  people, so every individual series can be valid while every cross-person comparison is
  noise. Two numbers side by side is an invitation to compare them.

## The hard part: getting the model out of Docker

Required only for a standalone `.app` others can install. Friends cannot be asked to run
Homebrew, Colima and a 15-minute Rosetta build.

| | Risk |
|---|---|
| **FaceAge `.h5` → Core ML** | `coremltools` defaults to float16; **force float32** or the `1e-04` validation tolerance is blown immediately. |
| **MTCNN parity** | The subtle one. Vision's `VNDetectFaceRectanglesRequest` is not MTCNN. A different face box means a different 160×160 crop means a different FaceAge value. Swapping the detector produces a *different instrument*, not a port, and breaks comparability with the existing series. All three nets (P/R/O) must be converted. |
| **Re-validation** | Non-negotiable. Both implementations over the same 56 UTK images, held to the same tolerance the Docker pipeline already meets (max abs diff 4.72e-05). |

The Docker pipeline becomes the **reference implementation** rather than the shipped one,
and `faceage validate` plus `validation/reference/utk_hi-res_qa_res.csv` are already the
oracle. Better still: with photos retained, the converted model can re-score the entire
existing history and be diffed against the Docker numbers — a far stronger check than 56
stock images, because it is this face under this lighting.

Note Vision *is* appropriate for non-scoring UI work (fast, on-device, free). It must never
touch the number.

## Gate

**Do not start building until σ — between-session measurement noise — is known.**

The repeatability study (`docs/PREREGISTRATION.md` §4) measures it: five sessions in seven
days, identical rig, where real change is ≈ 0.

| σ (yr) | Decision |
|---|---|
| ≤ 0.5 | Build |
| 0.5 – 1.0 | Fix the rig, re-measure first |
| > 1.0 | **Do not build.** The instrument cannot resolve the effect and the app would be polish on noise. |

## Build order

1. **Import + QA + score + chart, local only, me only.** No Core ML — shells out to
   `faceage run` against what is already on `main`. Testable against the existing series
   from day one.
2. **Core ML conversion**, validated as above. The real work.
3. **Standalone `.app` + row-only sync.** Only if friends are actually in scope.

Step 1 answers whether reduced friction actually raises session count and tightens variance.
If it does not, steps 2 and 3 are not worth doing.

## Open questions

- Does the contest kit prescribe a capture app or photo format that constrains any of this?
- Is a watched-folder import enough, or is a Photos-library picker needed in practice?
- Which machine owns scoring, given the two-Mac setup?
- `Younger 2027 — FaceAge Protocol.md` (capture/measurement) is referenced in §1 of the
  Improvement Protocol but is not in this repo. It may constrain the QA checklist above.
