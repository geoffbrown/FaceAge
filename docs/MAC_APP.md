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

**It is not a second camera.** *(revised 2026-09-12)*

The first draft of this section said "it is not the camera": capture stays on the phone,
because this repo's README measured a webcam-vs-phone gap of **2.2 years** on photos taken
ten minutes apart. That number is still true, and it is still the reason the rule exists.
But it is a gap *between* two instruments, not noise *within* one. The series is a relative
trend line (`docs/PREREGISTRATION.md` §7): any camera that is fixed in place and never
changed contributes a constant offset, and a constant offset cannot move a slope.

So the web app now offers the Mac's built-in camera as an **option beside import**. The
camera step runs a live face detector in the page (pico.js, vendored under `tools/web/`,
MIT) and turns that into plain guidance: *come closer*, *move a little to your left*,
*sit a little lower*, *a bit dark: bring the lamp closer*. The frame turns green and
chimes when the face box is 80–94% of the saved frame height, centred, and the brightness
on the face is within ±5 of that camera's baseline; held for 1.5 s it starts the 3-2-1
countdown and ten shots on its own (a checkbox turns auto-start off; Start always works).
The countdown aborts if the face leaves position. Each shot waits briefly for the face to
settle and records fill, offset and brightness alongside the frame in `capture.json`.

The saved photo is a **fixed centre crop** of the sensor: two thirds of its height, 3:4
portrait, copied 1:1 and unmirrored. That is what the pipeline measures (`face_fill` is
against the saved frame), and fixing it keeps distance comparable between sessions while
letting the person sit at desk distance rather than leaning into a wide lens. The crop is
recorded in the manifest. A Mac on a desk is easier to keep fixed than a propped phone, and
it removes the AirDrop step.

The brightness tolerance (5 by default) is one setting, `settings.json` in the data folder,
read by the result card, the live guide, the pre-flight and the tracker alike. It is meant to
be set from evidence: `faceage sensitivity DATE` scores one session as shot and at four
brightness gains (0.90 to 1.10, the kind of shift a webcam's auto-exposure makes between
sittings) and reports how far FaceAge actually moved, against the session-to-session spread.
`faceage tolerance N "reason"` records the number with its reason and date. The model
standardises every face crop before it sees it, so a uniform brightness shift is largely
removed; the tolerance should reflect the measured sensitivity, not a guess.

When two sittings shot minutes apart disagree, `faceage compare D1 D2 [D3 ...]` puts them
side by side, photo by photo: FaceAge, face size in the frame, brightness and detector
confidence, then says in a sentence what actually differed. Three or more sittings that all
move the same way are called out as drift rather than noise.

The exposure baseline is per camera: the first logged session shot on that camera. A phone
baseline says nothing about how bright the Mac's camera should read, so a Mac session is
never held to one; its first logged session sets the Mac baseline instead.

The tracker is written for the person, not the protocol. It leads with the latest number
and one of six fixed plain-language states (too early to call, early days, holding steady,
trending younger, trending older, two cameras in the mix), each derived from the
pre-registered trend and never invented. One chart: FaceAge per session, camera carried by
shape and hue, sessions before the start line hollow, the fitted trend dashed once three
sessions exist. A consistency strip says whether each session matched the light and passed
every photo. Session rows expand for spread, brightness, flagged photos and notes; column
names explain themselves on hover. The slope, its interval, the formal verdict and the
brightness chart live under "The numbers behind this". The camera per session comes from
`capture.json`; brightness is compared within a camera only.

Discarding a session (start over, shoot again) moves its photos, checklist and result to the
macOS Trash as one folder named for the person and date, where Finder can put it back; the
discard stays logged in `discarded.csv`. Nothing is archived inside the data folder any more.
`faceage tidy` moves anything earlier versions archived there to the Trash; `--all` also moves
session folders that were shot but never added to a tracker.

Deleting is real there: a row's Delete, or Edit and a selection, removes the tracker row
and the photos, results and checklist from disk after a confirmation, and leaves one line in
`deleted.csv`. The app's done card keeps the softer "remove from the tracker" that leaves
the files in place (`removed.csv`). `faceage reset` moves the whole data folder aside.

The rule that survives is **one camera for the whole series, never switched**. The app
records which camera produced each session and warns before a session from a different
camera is added to a tracker built from the other. Cross-person comparison stays out of
scope for the same reason it always was.

## Scope

**Capture or import → QA → score → chart.**

The app is two tabs: **New session** and **Progress**. A new session is camera, analysis,
result. There is no separate conditions step: the capture records camera, light and framing
by measurement, and an "Anything different today?" strip on the camera screen records the
exceptions only the person knows (shave, makeup, light, skin, alcohol, sleep, shower, moved
the Mac, something else) the moment capture starts, before any number exists. Nothing
tapped is the all-clear. The Who step appears only when nobody exists yet; the last person
is remembered. After ten frames the analysis starts by itself. Adding a session switches to
Progress, which is the tracker page embedded in the app.

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

| | Risk | Status |
|---|---|---|
| **FaceAge `.h5` → Core ML** | `coremltools` defaults to float16; **force float32** or the `1e-04` validation tolerance is blown immediately. | **Converted.** float32 verified at 4.00 bytes/parameter. `tools/coreml/` |
| **MTCNN parity** | The subtle one. Vision's `VNDetectFaceRectanglesRequest` is not MTCNN. A different face box means a different 160×160 crop means a different FaceAge value. Swapping the detector produces a *different instrument*, not a port, and breaks comparability with the existing series. All three nets (P/R/O) must be converted. | **Not started.** Now the whole of the remaining risk. |
| **Re-validation** | Non-negotiable. Both implementations over the same 56 UTK images, held to the same tolerance the Docker pipeline already meets (max abs diff 4.72e-05). | **Harness built, not yet run.** Core ML inference is macOS-only, so the number is unmeasured. |

An unbudgeted obstacle turned up and is now solved: the `.h5` cannot be loaded
by any Python newer than 3.6 — 21 `Lambda` layers carry marshalled py3.6
bytecode. That also blocked conversion outright, since coremltools cannot
convert an opaque `Lambda`. Decoded and substituted; see
`tools/coreml/README.md`.

Regressor parity and MTCNN parity are separated: the regressor is checkable
with synthetic tensors (no face photographs, nothing biometric in git), while
MTCNN needs real faces because what is compared is where the box lands.

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
