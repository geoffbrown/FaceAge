# Younger 2027 — FaceAge Improvement Protocol

*Companion to `Younger 2027 — FaceAge Protocol.md` (capture/measurement). This note covers the intervention half: what to actually do between baseline and retest.*

---

## 1. What is being measured

The contest uses **Harvard FaceAge** (Bontempi et al., *Lancet Digital Health*, May 2025), developed at Mass General Brigham / Harvard Medical School. Dr. Ray Mak, its co-creator, is a scientific partner on the contest.

Mechanically: a face-detection stage crops the face, then a convolutional network (Inception-ResNet v1, pretrained on face recognition) produces a 128-dimensional embedding, which a regression layer converts to a single continuous age estimate.

The scored quantity is the **deviation** — FaceAge minus chronological age. Because the contest composite is a change score, what matters is:

> (deviation at baseline) − (deviation at retest)

Same-day chronological age drift over six months is negligible, so in practice you are trying to lower the raw FaceAge estimate from January to your retest week.

**Confirmed: they run the original FaceAge, not the successor.** The contest page specifies the model trained on face photos from 58,851 individuals, published in *Lancet Digital Health* 2025 — that is the original Bontempi/Mak model, not the newer FAHR-FaceAge (40M-image foundation model). Two consequences:

- Accuracy detail from the repo: overall MAE is 5.87 years, improving to 4.09 years for the manually curated 60+ range, and **mean age difference is approximately zero for ages above 40** — so no systematic age bias in your range, just wider dispersion. Under 40 the model fits poorly by design; the authors deliberately traded younger-age accuracy for oncology-relevant accuracy. The team also deliberately did *not* fine-tune the model, because fine-tuning made it better at guessing chronological age while destroying its prognostic signal. The "noise" is partly the biology.
- The public GitHub repo publishes **trained model weights** for this exact model. See §8, item 3 — this changes the game.

They also cite a 2026 *JAMA Network Open* validation in early-stage NSCLC patients: facial biological age predicted overall survival better than chronological age (adjusted HR 1.39 per decade), with a face age ≥10 years above chronological linked to higher 2-year mortality.

---

## 2. Where the model looks

This is the most actionable finding. Saliency mapping on FAHR-FaceAge shows attention concentrated on:

- **Nasolabial folds** (nose-to-mouth lines)
- **Forehead**
- **Temporalis area** (temples) — weighting here *increases* from age 40 onward

The companion survival model attends to a different region: under the eyes and the nasal bridge.

**Implication:** midface fold depth, forehead skin texture, and temporal fullness are on-target. Jawline, neck, and lower-face work are off-target for this metric. Anything covering the temples or forehead is a confound.

---

## 3. Noise control — your single biggest lever

The Harvard group tested robustness by feeding paired photos of the same person. Averages were unbiased, but **per-photo variation was large**:

| Variable | Mean absolute swing |
|---|---|
| Makeup vs none | 4.37 years |
| Illumination (light vs dark) | 2.35 years |
| Expression (neutral vs smiling) | 2.19 years |
| Head angle (22.5° vs frontal) | ~1.1–1.2 years |

Read that against the size of the effect you are trying to produce. A single uncontrolled photo can swing the estimate by more than any realistic six-month intervention will earn you. **Standardization and averaging are worth more than the entire intervention stack.**

The contest describes FaceAge as an estimate "from a series of standardized photos," self-captured at baseline and re-captured at retest — so multiple frames are expected. Their kit instructions will define the series; the standard below is what you control on top of it.

### Capture standard — fix at baseline, never change

- Same phone, same lens (**rear main camera, not ultrawide** — wide-angle barrel distortion alters apparent midface geometry), same app, HDR setting locked
- Tripod at eye level, marked floor position, fixed distance; face occupies a consistent fraction of the frame
- **Flat, diffuse, frontal light.** A north-facing window at a fixed time of day, or a ring/softbox at fixed distance and color temperature. Never overhead — overhead light casts shadow into exactly the nasolabial and periorbital regions and reads as older
- Plain neutral background
- Neutral expression, mouth closed, jaw relaxed, eyes open normally, head level (camera grid + level app)
- No glasses; hair off the forehead and temples; identical grooming state
- **10 frames per session**, discard blinks and expression drift
- Log every parameter in a fixed template each time

### Photo-day controls

- No alcohol for 48h (facial edema, flushing, telangiectasia)
- Sodium under a fixed cap the day before
- 7h+ sleep; same wake-to-photo interval every time
- Shoot 2+ hours after waking — periorbital edema peaks immediately on waking and redistributes
- No hot shower, sauna, or hard training within 2h (erythema)
- No facial treatment of any kind within 2 weeks
- Reschedule for sunburn, allergy flare, illness, or a visible blemish in an attention region

---

## 4. Intervention stack

### Tier 1 — start the day after the baseline photo

**1. Daily broad-spectrum SPF 30+, every morning, year-round.**
UV is the dominant driver of extrinsic facial aging. The Nambour randomized trial (Hughes et al., *Annals of Internal Medicine*, 2013) found daily sunscreen use over 4.5 years prevented detectable skin aging relative to discretionary use. Your intervention window runs through spring and into summer and you train outdoors on the bike — this is your largest exposure risk. Add a cycling cap or visor, UV-blocking eyewear, and reapplication on long rides. This is prevention rather than reversal, but preventing a training summer's worth of photoaging is worth more than most of the reversal options combined.

**2. Topical retinoid — start immediately, it is the slowest-acting item.**
Best-evidenced topical for fine lines, texture, and pigment. Prescription tretinoin 0.025–0.05% nightly via your physician, or OTC retinaldehyde/adapalene if tolerance is a problem. Expect 8–12 weeks of retinization and measurable change at 12–24 weeks. Titrate: 2 nights/week → alternate nights → nightly over 6–8 weeks. Pair with a bland moisturizer and the morning SPF (photosensitivity). Apply across the **forehead and midface** — the attention regions.

**3. Nicotine: zero.** Smoking is the strongest lifestyle association in both papers — current smokers estimated ~2.8 years older on the original FaceAge, with a dose-dependent relationship in FAHR. Includes vapes and pouches; the mechanism is partly vasoconstriction.

**4. Alcohol: minimal, and zero in the two weeks before either photo.** Heavy use was significantly associated with a higher age deviation in FAHR. Short-term it drives facial edema and flushing; long-term, telangiectasia and sleep fragmentation.

**5. Sleep.** You already track this. Consistent 7.5h+ with a stable schedule. Sleep restriction reliably produces periorbital edema, dark circles, and lid droop.

### Tier 2 — moderate confidence, low cost

**6. Sleep position.** Chronic side or prone sleeping produces asymmetric compression lines through the midface and periorbital region. Supine if you can manage it; otherwise a silk or satin pillowcase to reduce shear.

**7. Daily moisturizer** (humectant + occlusive at night). Doesn't change collagen, but it changes surface light scatter and fine-line visibility — which is what the model reads as texture.

**8. Day-to-day sodium and hydration consistency**, not just before photos.

**9. Adequate protein and energy** — already covered by JP2.

**10. Glycemic control / limited added sugar.** Advanced glycation end-product cross-linking is a plausible mechanism for dermal stiffening, but the evidence for a visible six-month effect is thin. Keep it, don't count on it.

### Tier 3 — procedures. Confirm the rules first (§8).

**Evidence note that should change your priors:** FAHR-FaceAge robustness testing on before/after surgical pairs found **no significant change** in the age estimate after eyebrow correction, eyelid correction, facelift, or facial bone correction. Nose correction actually *raised* the estimate by ~1.1 years. Cosmetic surgery appears to be a poor return on this specific metric.

If procedures are permitted, the plausible return is in **pigment and texture, not volume**:
- IPL/BBL for solar lentigines and diffuse redness
- Non-ablative fractional resurfacing or microneedling for texture
- Complete the final session **≥6 weeks before the retest photo** so erythema and post-inflammatory pigment have fully resolved. Never within 2 weeks of a photo.

Fillers and neuromodulators are untested against this model, and they target precisely the attention regions — which cuts both ways. They also carry the highest disclosure risk of anything on this list.

---

## 5. The conflict to decide now: body composition vs FaceAge

This is the important strategic point and it is not obvious.

Your DEXA and body-composition goals push toward a fat-loss block. But the model attends to the **nasolabial folds and the temporalis region**, and both are volume-dependent. Midface fat pad loss deepens the nasolabial fold; temporal fat and muscle loss produces hollowing. Past your early forties, a meaningful weight drop can raise apparent facial age even while body composition improves.

The original FaceAge paper found only a weak relationship between BMI and the age deviation across a large cohort — but that is a cross-sectional population correlation, not the effect of *you* losing weight over six months, and it doesn't capture the regional volume effect.

**Decision rules:**

- No aggressive deficit in the final **10–12 weeks** before the retest photo
- If a cut is warranted, **front-load it.** Run it early in the window; spend the last 8–10 weeks at maintenance so facial volume refills
- Cap the rate of loss. Slow loss preserves more facial volume than fast loss
- **Instrument it.** Add a face check to the monthly reference series. If you see the nasolabial shadow deepening or the temples hollowing month over month, the cut is costing you more on FaceAge than it is earning on DEXA
- Before optimizing either, find out how the composite weights the domains (§8). One DEXA point is not automatically worth one FaceAge point

---

## 6. Calendar

Anchored on **B** = baseline photo, **R** = retest photo. Note that **R is yours to choose** — any time from six months after B up to August 1, 2027. Decide the target retest week early, because everything below counts backward from it. Prefer a deload week, as with your other domains.

| When | Action |
|---|---|
| B−2 weeks | Build and rehearse the capture rig. Three practice sessions to confirm you can reproduce framing and lighting. Do not submit these. |
| B−2 weeks | Start SPF and moisturizer only (no downside, no confound) |
| **B** | Baseline photo set. **Do not start the retinoid before this.** The baseline is meant to measure your untreated state — starting the intervention before the pre-intervention measurement just spends your own delta. |
| B+0 to B+2 wk | Begin retinoid titration. Nicotine zero, alcohol cap, sleep target locked. |
| Monthly | Reference photo set, identical rig. Log conditions. Face check per §5. **Score the set through the local FaceAge model (§8, item 3) and chart the trend.** |
| R−16 weeks | Last window to *start* any Tier 3 course |
| R−12 weeks | Any fat-loss block ends. Move to maintenance. |
| R−6 weeks | Final procedure session of any kind |
| R−4 weeks | Retinoid continues at current strength — no increases (avoid erythema at photo) |
| R−2 weeks | Zero alcohol. No new products, no facial treatments, no new sun exposure. Sodium controlled. |
| R−48h | Photo-day controls per §3 |
| **R** | Retest photo set. Identical rig, identical time of day, identical grooming state as B. |

---

## 7. Hold constant between B and R

- **Facial hair state.** The biggest one. A beard obscures the nasolabial region the model weights most heavily. Whatever state you are in at baseline, replicate it exactly at retest — same length, same shape.
- **Hair at the temples and forehead.** The model attends to both; hair covering them is a confound.
- Glasses on/off
- Tan level, as far as you can control it
- Body weight, roughly — see §5
- Camera, lens, lighting, distance, framing, time of day

---

## 8. What the contest site answers — and what it doesn't

### Answered

**1. Which model.** Original FaceAge (58,851 training subjects, *Lancet Digital Health* 2025). See §1.

**2. You get your baseline number.** This is the biggest find. Per the FAQ: results flow into the participant dashboard on a rolling basis as each lab finishes; they are not held for a January reveal. The **only** thing withheld until the November finale is the overall ranking and the final biological-age delta. Every individual test result is visible throughout.

So you will see your baseline FaceAge estimate. You are not flying blind — you can steer. Combined with item 3 below, this converts FaceAge from a one-shot gamble into a closed-loop, measurable target.

**3. You can probably run the model yourself.** The Harvard AIM Lab publishes the full FaceAge code on GitHub (`AIM-Harvard/FaceAge`), including a ready-made inference script that scores every image in a folder and writes a CSV.

~~Caveat on the weights~~ — **resolved, 2026-09.** The weights are published in the AIM-Harvard release and download cleanly:

```
https://github.com/AIM-Harvard/FaceAge/releases/download/v1/faceage_model.h5
sha256 dd43eb66616558a70f86d55d405c2e5be1e49569654b7eb852c206393fd7a85d
```

`tools/install.sh` fetches and hash-verifies them automatically and refuses to proceed on a mismatch. The pipeline has since been validated against the authors' own published output (`validation/reference/utk_hi-res_qa_res.csv`): max abs diff **4.72e-05** across 56 images, which is serialisation precision, not computation. Setup in §10.

Practical consequence: run your monthly reference photos through the model locally. Calibrate the local output against the official baseline number the dashboard returns, then use the local pipeline as your monthly tracker. Two cautions — the contest's version may differ from the public release, and their preprocessing/crop may differ from yours, so treat local numbers as a **relative** trend line, not the official score. That is still enough to answer the questions that matter: is the retinoid working, and is my cut costing me facial volume (§5)?

**4. Timing leverage you may not have noticed.** The six-month clock starts *when you submit your baseline*, and the retest is due no later than August 1, 2027 regardless. So:

- Baseline late September 2026 → retest window opens ~late March 2027, closes August 1. That is roughly **ten months** of intervention time before the deadline.
- Baseline February 1, 2027 (the deadline) → retest August 1. Exactly six months.

Baselining early buys you up to four extra months. That disproportionately favours the slow-acting items here — the retinoid needs 12–24 weeks, and pigment/texture work needs a full course plus a six-week clearance. Two counterweights: a later retest sits deeper into summer sun exposure, and it is unclear whether the composite normalises the delta by elapsed time. Ask.

### Still unanswered — ask `hello@neuroagetx.com`

**5. Scoring methodology and domain weighting.** Not published. The site states twice that final scoring methodology will be released to competitors before the contest begins. Until then you cannot rationally trade FaceAge against DEXA (§5). Ask specifically: how is FaceAge weighted, and is the delta time-normalised?

**6. Cosmetic and dermatologic procedures.** Nothing on the site addresses this either way. Worth noting that one of the official clinic teams (MOOV) lists aesthetics among its services — which suggests no blanket prohibition, but is not permission. Ask directly, and ask whether disclosure is required.

**7. Exact photo capture spec** — frame count, resolution, lighting or expression requirements. Will be in the kit. Their spec overrides §3 wherever they conflict.

### Also worth knowing from the site

- **Body composition is an optional add-on** for standard Competitor entry; two DEXA scans are bundled with Ultra. Confirm which you have.
- VO2max is taken from your wearable — Oura, Apple Watch, Garmin, and WHOOP are all supported, so both your devices qualify.
- Grip uses the dynamometer shipped in the kit.
- Prize structure per the FAQ: **Grand Prize** for the largest reduction between baseline and finale; **Youngest Finisher** for the lowest biological age at the finale.

---

## 9. Expectation setting

No intervention trial has been run against this model. The realistic six-month gain from the Tier 1 stack is modest — likely a small number of years at best, and potentially within the noise band of a single photo pair.

The three things most likely to actually move your scored delta:

1. **Noise control.** Standardized capture and multi-frame averaging. Free, entirely within your control, and larger in magnitude than any intervention here.
2. **Not making it worse.** A training summer of sun exposure, an aggressive cut in the wrong twelve weeks, or a bad sleep block before the retest photo can each cost you more than the whole stack earns.
3. **Closing the loop.** Because you get your baseline number and the model weights are public, you can measure whether any of this is working instead of guessing. Almost no competitor will do this.

Play defense first, then instrument everything.

---

## 10. Running FaceAge locally on the Mac

**Status: built and validated.** This section described an open problem; it is now solved. Option A below was taken, and this repo *is* the result.

### Install

```bash
git clone https://github.com/geoffbrown/FaceAge.git ~/Documents/GitHub/FaceAge
cd ~/Documents/GitHub/FaceAge
./tools/install.sh
```

Idempotent and safe to re-run. It handles colima + docker via Homebrew, Rosetta 2, the VM, hash-verified weights, the container build (~10 min under emulation) and `PATH`. Then, in a new terminal:

```bash
faceage run          # score today's session folder
faceage chart        # build + open the tracker
faceage history      # print the longitudinal series
faceage doctor       # check VM, image, weights hash, session count
faceage validate     # re-run validation against the authors' published CSV
```

Photos live in `~/FaceAgeData/subjects/<name>/`, outside the repo. `.gitignore` excludes every image extension anywhere in the tree, plus weights and all result CSVs.

### Why option A

**A. Docker with `--platform linux/amd64`** — taken. The repo's environment is period-accurate to 2021 (Python 3.6.13, TensorFlow 2.6.2, Keras 2.6.0, NumPy 1.19.5, MTCNN), none of which has an Apple Silicon build, so `conda env create --file environment-cpu.yaml` fails on an M-series Mac. A Linux container under Apple Virtualization + Rosetta reproduces the authors' environment exactly. **Validated at max abs diff 4.72e-05** against their published output — agreement is exact to the precision they published. Deviations from their environment are documented in the repo README.

Compute is a non-issue: the authors' benchmark ran 2,547 images on CPU in roughly the same time as on a TITAN RTX. Ten photos is seconds, even emulated.

**B. Rosetta conda environment** — not needed. Faster to build, more fragile; resolution of old `osx-64` TF 2.6 builds can break.

**C. Modernise the environment** — not needed, and now actively undesirable: a current TensorFlow would change the arithmetic and break comparability with the validated numbers above.

**D. Google Colab** — rejected. Uploading photos of your face to Google's infrastructure, for a pipeline whose entire design premise is that no image leaves the machine.

### Operating notes

1. ~~Confirm the weights download~~ — done, hash-verified by the installer (§8, item 3).
2. **Take 8–12 photos per session, not one.** Per-photo dispersion is wide by design; the session mean is the signal and `std` tells you how noisy that session was. Re-running a session date replaces its row rather than duplicating it.
3. **Watch the `mean_luma` column.** It is the mean pixel value of the face crop before normalisation, 0–255. Keep it within roughly ±5 of prior sessions — it is the early warning that lighting has drifted, and per §3 lighting is the dominant photographic confound.
4. The photo QA criteria from the authors' Supplement Table 1 are implemented as automated flags (hard failures and advisories) and documented in the repo README §3. Advisory-flagged photos are excluded from the session mean by default.
5. Measurement commitments — exclusion rules, analysis, stopping rule, falsification check — are pre-registered in `docs/PREREGISTRATION.md`, committed before any study data exists. §3 of this note governs capture; that document governs analysis.

---

## 11. Product stack

Principle: fragrance-free, dye-free, short ingredient lists, mineral UV filters. The "clean beauty" category more broadly is marketing — botanical extracts and essential oils are among the more irritating things you can apply. Irritation matters here specifically, because redness reads as older to the model and you'll be running a retinoid.

### What you already have

- **CeraVe cleanser — keep it.** Ceramide-based, fragrance-free, no reason to replace. If it's the Foaming version, switch to Hydrating once the retinoid starts; foaming is too stripping alongside it.
- **CeraVe SPF 30 — keep as a backup, don't make it the primary.** Two gaps. SPF 30 is thin cover for the outdoor training volume you'll be doing through spring and summer. And most CeraVe facial sunscreens use chemical filters (avobenzone, homosalate, octisalate, octocrylene) — efficacy is fine, but they sting badly when you sweat, which means you won't reapply on the bike. The exception is their Hydrating Mineral SPF 30; check which one you have.

### To order

| Slot | Top pick | Backup |
|---|---|---|
| **Sunscreen** (highest value item) | **ISDIN Eryfotona Actinica SPF 50+** — 100% zinc, elegant texture, doesn't run into eyes when sweating | **La Roche-Posay Anthelios Mineral SPF 50** — same filter class, cheaper, slightly heavier finish |
| **Retinoid** (non-Rx) | **Medik8 Crystal Retinal 3** — retinaldehyde, one conversion step from tretinoin, better tolerated; a clear 3 → 6 → 10 → 20 ladder to climb as your skin adapts | **Differin (adapalene 0.1%)** — OTC at prescription strength, extremely stable, about a tenth the price |
| **Night moisturizer** | **La Roche-Posay Toleriane Double Repair** — ceramides plus niacinamide, built for retinoid nights | **Vanicream Daily Facial Moisturizer** — about as short an ingredient list as exists. Or your CeraVe PM if you already have it |
| **Morning antioxidant** (optional) | **Timeless 20% Vitamin C + E + Ferulic** — near-identical to the reference formula at a fraction of the price | **SkinCeuticals C E Ferulic** — the original, if you want the version with the actual trial data |
| **Sleep** (optional, §4 item 6) | Silk or satin pillowcase, any brand | — |

### Skip

Anything scented. Anything with essential oils. Augustinus Bader — the price is not matched by published evidence.

### Sequencing

Order the sunscreen now and start using it immediately; it's the only item with no downside and no confound, and it's the one your training pattern most exposes you on. Hold the retinoid until the day after your baseline photo (§6).

---

## References

- Bontempi D, et al. FaceAge, a deep learning system to estimate biological age from face photographs to improve prognostication. *Lancet Digit Health* 2025;7(6):100870.
- Haugg F, Lee G, et al. Foundation Artificial Intelligence Models for Health Recognition Using Face Photographs (FAHR-Face). arXiv:2506.14909, 2025.
- Hughes MCB, et al. Sunscreen and prevention of skin aging: a randomized trial. *Ann Intern Med* 2013;158(11):781–90.
- AIM-Harvard/FaceAge repository, github.com/AIM-Harvard/FaceAge