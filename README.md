# FaceAge — local monthly scoring pipeline

Runs the Harvard **FaceAge** model (Bontempi et al., *Lancet Digital Health*, 2025) locally
on Apple Silicon to score face photographs and build a longitudinal series.

Everything runs on this machine. No image ever leaves it.

- Upstream code: <https://github.com/AIM-Harvard/FaceAge>
- Paper: <https://www.thelancet.com/journals/landig/article/PIIS2589-7500(25)00042-1/fulltext>

---

## 1. Monthly run

Drop this session's photos into a dated folder and run one command:

```bash
mkdir -p ~/FaceAgeData/sessions/$(date +%F)
# copy this session's .jpg / .png photos into that folder, then:
faceage run
```

`faceage run` with no argument uses today's session folder. It also accepts photos
or a folder directly, which saves the copy step:

```bash
faceage run 2026-08-01                  # an earlier session folder
faceage run ~/Desktop/pics              # score a folder in place
faceage run ~/Desktop/pics/*.jpg        # stage those photos into today's session
faceage run --replace ~/Desktop/*.jpg   # ...archiving any photos already there
```

Re-running the same session date replaces that row in the history rather than
duplicating it.

### Lighting is the dominant confound

Measured on this pipeline, 2026-09-06: the same photograph re-exposed across a 3x
brightness range moved FaceAge by **3.69 years**, monotonically — darker reads older,
brighter reads younger.

| brightness | 0.5x | 0.7x | 0.85x | 1.0x | 1.2x | 1.5x |
|---|---|---|---|---|---|---|
| FaceAge | 43.25 | 43.65 | 42.89 | 42.18 | 42.11 | 39.96 |

The per-image standardisation does *not* cancel this: brightening a JPEG compresses and
clips highlights, altering the skin-texture contrast the model reads.

For a year-long series this is the main threat to validity — seasonal daylight change
alone could manufacture a multi-year "improvement" that is purely photographic. So:

- Shoot under **fixed artificial light** in a room where you can exclude daylight.
  Same lamp, same position, same output. This is more reliable than any time-of-day rule.
- Lock exposure at capture (on iPhone, tap and hold for AE/AF lock).
- Watch the **`mean_luma`** column in the history CSV. It is the mean pixel value of the
  face crop before normalisation, on a 0-255 scale. Keep it within roughly ±5 of prior
  sessions. If it drifts, treat that session's change as suspect until you reshoot.

> **Keep the capture setup identical between sessions.** Measured on 2026-09-06, two
> sets of ten photos taken ten minutes apart — a wide webcam shot versus a tight phone
> shot — differed by **2.2 years** of FaceAge. Within-session precision was ±0.32
> (standard error), so capture setup moved the number seven times more than the
> measurement noise, and comfortably more than a year of real change would. Same
> camera, distance, height, lighting and time of day, every month.

Other commands:

```bash
faceage chart        # build + open the visual tracker dashboard
faceage history      # print the longitudinal series
faceage doctor       # check VM, image, weights hash, session count
faceage validate     # re-run validation against the authors' published CSV
faceage build        # rebuild the container image
```

### Installing the `faceage` command

On a machine that has never run this before, use the installer — it sets up the
container runtime, weights and `PATH` in one step. See section 5.

```bash
./tools/install.sh
```

---

## 2. Where things live

Data is kept **per subject**, so scoring someone else never touches your series.
Your own subject is `me`; everything below is under `~/FaceAgeData/subjects/<subject>/`.

| What | Path (relative to `~/FaceAgeData/subjects/me/`) |
|---|---|
| Photos, by session | `sessions/YYYY-MM-DD/` |
| Per-image results | `results/YYYY-MM-DD_per_image.csv` |
| Session summary | `results/YYYY-MM-DD_summary.json` |
| **Longitudinal series** | `results/faceage_history.csv` |
| Tracker dashboard | `results/tracker.html` (built by `faceage chart`) |

### Scoring someone else

```bash
faceage run --subject jackie ~/Desktop/jackie/*.jpg   # her own series
faceage chart --subject jackie                        # her own tracker
faceage subjects                                      # who has data here
faceage run --no-log ~/Desktop/oneoff.jpg             # score, log nothing
```

Each subject gets an independent `sessions/`, `results/`, history CSV and tracker;
the tracker page is badged with the subject name so two dashboards can't be confused.
`--no-log` scores and writes the per-image CSV but appends to no series — use it for
one-off curiosity runs.

Other people's photographs are their biometric data. They are gitignored like yours,
but get the person's agreement before scoring them, and delete
`~/FaceAgeData/subjects/<name>/` when you're done.
| Model weights (92 MB) | `models/faceage_model.h5` (gitignored) |
| Code | this repo |

Photos and results live **outside the git repo** by design. `.gitignore` additionally
excludes every common image extension anywhere in the tree, the `models/` directory,
and all result CSVs, so nothing sensitive can be staged even if a copy lands here.

### What you get back

`faceage_history.csv` gains one row per session:

```
session_date, run_timestamp, n, n_total_images, n_failed, n_flagged,
mean, median, std, min, max, mean_luma, model_sha256, image_dir, notes
```

**`mean` is the number to track.** Per-photo noise on this model is large, so a single
photograph is close to meaningless; the session mean is the signal. `std` is the sample
standard deviation (ddof=1) across the session — it tells you how noisy that session was.
Re-running the same session date replaces that row rather than duplicating it.

The per-image CSV additionally records detection confidence, face-box geometry, and QA
flags for every photo.

---

## 3. Photo QA criteria

These come from the authors' Supplement Table 1 (`data/README.md` upstream), which is
what the clinical images were culled against. Use them as your capture standard.

**Exclusion criteria — a photograph is disqualified if:**

1. The source photograph is **smaller than 160×160 pixels** before face extraction.
2. The photograph **does not contain a face**.
3. Objects **obscure part or all of the face** (mask, hat, sunglasses, hand).
4. The face is **not entirely within the image borders**.
5. There is **more than one face** in the image.
6. The face **fills less than 80% of the photographic frame**.
7. The photograph is **poor quality**, specifically:
   - facial features not clearly defined due to poor resolution or pixelation;
   - photographic artefacts that distort or obscure the subject;
   - contrast ratio prevents clear visualisation of facial features (note: a standard
     per-image normalisation is applied to all photos before processing, which already
     maximises contrast);
   - obvious colour distortion (something normally green appears red).

The authors' worked example of an *acceptable* image is a front-facing, evenly lit,
neutral-expression head shot where the face spans essentially the full frame height.
Their worked example of a *disqualified* image was rejected for containing more than one face.

### Practical capture standard

- One person in frame. No one in the background.
- Tight head-and-shoulders crop: the face should span **≥80% of the frame height**.
- Face square to the camera, neutral expression, eyes open, mouth closed.
- No glasses, hat, or hair across the face.
- Even, diffuse, front lighting. No hard side light, no strong backlight, no colour cast.
- **Lighting is the single most important variable to hold constant — see below.**
- Plain background.
- Same spot, same time of day, same camera each month — consistency matters more than
  absolute quality for a trend.
- Shoot **JPEG or PNG**. HEIC is not readable by this pipeline; set the iPhone to
  "Most Compatible", or export to JPEG first.
- Take 8–12 photos per session, not one. The mean is what you track.

### How the pipeline flags photos

**Hard failures** — no FaceAge value, excluded from statistics, listed by name:

| Flag | Meaning |
|---|---|
| `NO_FACE_DETECTED` | MTCNN found no face |
| `MULTIPLE_FACES` | more than one face (exclusion criterion 5) |
| `SOURCE_TOO_SMALL` | source below 160×160 (criterion 1) |
| `DEGENERATE_CROP` | face box had zero area |
| `UNREADABLE_IMAGE` | file could not be decoded (e.g. HEIC) |
| `PREDICTION_FAILED` | model raised during inference |

**Advisories** — a value is produced but the photo departs from the capture standard.
These are excluded from the session mean by default and listed by name:

| Flag | Meaning |
|---|---|
| `FACE_FILL_BELOW_80_PCT` | face spans <80% of frame height (criterion 6) |
| `LOW_CONFIDENCE` | MTCNN confidence below 0.95 |
| `CROP_UPSCALED` | face box smaller than 160×160, so the crop was upscaled |
| `FACE_CLIPPED_AT_BORDER` | face box touches the image edge (criterion 4) |

If *every* photo in a session carries an advisory, the pipeline falls back to using all
scored photos and prints a warning rather than reporting an empty session.

Override thresholds per run:

```bash
faceage run 2026-09-06 --min-face-fill 0.6      # looser framing requirement
faceage run 2026-09-06 --include-flagged        # count flagged photos in the mean
```

Criterion 3 (obscuring objects) and criterion 7 (quality, contrast, colour) are **not**
machine-checked — the model has no way to detect them. Those remain your responsibility
at capture time.

---

## 4. Validation

The pipeline was validated against the authors' own published output
(`validation/reference/utk_hi-res_qa_res.csv`, 2,547 rows) before ever being pointed at
personal photos.

```
Images scored  : 56
Matched to ref : 56
max abs diff   : 4.72e-05
mean abs diff  : 6.98e-06
RESULT: PASS (tolerance 1e-04)
```

The residual difference is **serialisation, not computation**. The authors' CSV stores
values at ~6 significant figures (e.g. `94.1724`); this pipeline reproduces
`94.172447`. Agreement is exact to the precision they published.

Reproduce it with `faceage validate`.

**On the validation set size:** the authors' 2,547 curated UTK images were never released
on Zenodo as the repo promised. They survive in a public Google Drive folder linked from
the upstream notebook (`utk_hi-res_qa`), but Drive rate-limits bulk downloads, so 56
images were retrieved — spanning chronological ages 20 to 100, every one cross-checked
against the reference CSV. That is ample to prove environment fidelity, which is a
floating-point question, not a statistical one. To extend the set, download more files
from that folder into `validation/images/` and re-run `faceage validate`.

---

## 5. Installing on a new Mac

One command, from a fresh clone:

```bash
git clone https://github.com/geoffbrown/FaceAge.git ~/Documents/GitHub/FaceAge
cd ~/Documents/GitHub/FaceAge
./tools/install.sh
```

Then open a new terminal and confirm:

```bash
faceage doctor
```

`install.sh` is idempotent — re-run it any time; every step that is already done
is skipped. It:

1. Checks macOS version, architecture and free disk. Apple Virtualization +
   Rosetta needs macOS 13 or newer on Apple Silicon.
2. Installs `colima` and the `docker` CLI via Homebrew if they are missing —
   no Docker Desktop, no licence, no admin password.
3. Installs Rosetta 2 if the `linux/amd64` image will need it.
4. Starts the Colima VM with the exact settings `tools/faceage` expects.
5. Downloads the 92 MB model weights and **refuses to continue unless the
   sha256 matches** the AIM-Harvard release.
6. Builds the container image (~10 min under emulation).
7. Puts `faceage` on your `PATH` and pins `FACEAGE_REPO` to this clone, so the
   repo works wherever you put it — not only `~/Documents/GitHub/FaceAge`.

The installer reads the weights URL and hash, the image name and the VM flags
out of `tools/faceage` rather than restating them, so the two cannot drift apart.

Flags: `--rebuild` forces the image to rebuild; `--skip-build` does everything
except the 10-minute build.

`colima stop` shuts the VM down; `faceage run` restarts it automatically when needed.

### What is not in this repo, and why

| Item | Why it isn't tracked | How you get it on a new machine |
|---|---|---|
| **Model weights** (92 MB) | Redistributable only from the authors' release, and too large for git | `install.sh` downloads and hash-verifies it automatically |
| **Validation images** (`validation/images/`) | Other people's faces; the authors never published the curated set to Zenodo | **Manual** — see section 4 |
| **Your photos and results** | Biometric data, deliberately kept outside git | Stay in `~/FaceAgeData/` on each machine, per-machine |

Only the validation images need a manual step, and only if you want to re-prove
environment fidelity on the new machine — `faceage validate` exits with
"no validation images" without them. Scoring does not need them.

The authors' reference CSV (`validation/reference/utk_hi-res_qa_res.csv`, 2,547
rows) *is* tracked, so all you need to supply is the matching images.

### Doing it by hand

If you would rather not run the installer, it is equivalent to:

```bash
# 1. Container runtime (no admin password, no Docker Desktop licence)
brew install colima docker
colima start --vm-type=vz --vz-rosetta --cpu 4 --memory 8 --disk 60

# 2. This repo
git clone https://github.com/geoffbrown/FaceAge.git ~/Documents/GitHub/FaceAge
cd ~/Documents/GitHub/FaceAge

# 3. Model weights (92 MB, not in git)
mkdir -p models
curl -L -o models/faceage_model.h5 \
  https://github.com/AIM-Harvard/FaceAge/releases/download/v1/faceage_model.h5
shasum -a 256 models/faceage_model.h5
# expect: dd43eb66616558a70f86d55d405c2e5be1e49569654b7eb852c206393fd7a85d

# 4. Build the image (~10 min under emulation)
./tools/faceage build

# 5. Put `faceage` on PATH (skip if you always call ./tools/faceage)
echo 'export FACEAGE_REPO="$HOME/Documents/GitHub/FaceAge"' >> ~/.zshrc
echo 'export PATH="$FACEAGE_REPO/tools:$PATH"'              >> ~/.zshrc
source ~/.zshrc

# 6. Confirm
faceage doctor
faceage validate    # needs validation/images/ — see section 4
```

---

## 6. Deviations from the authors' environment

The pipeline reproduces the authors' published numbers exactly, so none of the following
shifted the results. Recorded for completeness.

| # | Deviation | Could it shift the numbers? |
|---|---|---|
| 1 | **conda → pip.** `environment-cpu.yaml` pins linux-64 conda build strings (`h06a4308`, `h7b6447c`), which only resolve on linux-64. The container installs the identical pip section verbatim and matches the conda section's versions via pip on a `python:3.6.13-slim-buster` base. | No. Every numerically relevant library is byte-identical in version: Python 3.6.13, TensorFlow 2.6.2, Keras 2.6.0, NumPy 1.19.5, MTCNN 0.1.1, OpenCV 4.5.4.60, Pandas 1.1.5. The conda-only packages (`zlib`, `yaml`, `sip`, `setuptools`) do not touch the arithmetic. Confirmed by the validation result. |
| 2 | **`six` installed with `--no-deps`.** The authors' file pins `six=1.16.0` via conda, but TensorFlow 2.6.2's pip metadata demands `six~=1.15.0`. conda and pip resolve independently, so the authors never hit this conflict; pip does. Installing `six==1.16.0` with `--no-deps` reproduces their actual end state rather than silently downgrading. | No. `six` is a pure-Python 2/3 compatibility shim with no numerical role. Their runtime had 1.16.0; so does this one. |
| 3 | **Rosetta 2 emulation.** `linux/amd64` x86-64 containers run on Apple Silicon under Apple Virtualization + Rosetta. | No, and this was the main risk — TensorFlow's official builds require AVX, which older Rosetta lacked. On this macOS version AVX works, TF 2.6.2 executes, and the validation confirms bit-level agreement. Had it not, the failure would have been a crash, not silently wrong numbers. |
| 4 | **Debian buster apt repos redirected** to `archive.debian.org` (buster is EOL). Affects only the install-time system libraries for OpenCV. | No. |
| 5 | **Unpinned transitive dependencies.** `environment-cpu.yaml` leaves `scikit-image` unpinned and never mentions Pillow. Python 3.6 constrains resolution to the same last-compatible versions the authors would have received: scikit-image 0.17.2, Pillow 8.4.0. | No, and this one genuinely mattered — Pillow performs the 160×160 resize and scikit-image the file read, so a different version *could* have shifted values. It did not; validation is exact. The Dockerfile does not pin them, so a future rebuild could in principle drift. Re-run `faceage validate` after any rebuild. |
| 6 | **EXIF auto-orientation added** in session mode only. Phone cameras store portrait shots as landscape pixels plus a rotation flag; MTCNN cannot find a sideways face. | Not for validation — it is disabled in validate mode, and UTK images carry no orientation tag. For your own photos it is corrective: it makes the pipeline see the image the right way up, as if the camera had written the pixels in that order. |
| 7 | **Face-fill criterion interpreted as frame height**, not frame area. The authors' acceptable worked example has a face box covering ~93% of frame height but only ~51% of frame area; an area-based test at 80% would disqualify their own example. | Does not affect any FaceAge value — it only decides which photos get an advisory flag. |

### One thing to know about the model itself

The authors deliberately did **not** fine-tune FaceAge on chronological age. A fine-tuned
version predicts chronological age more accurately (MAE < 3 years) but loses the
prognostic signal that makes FaceAge interesting. The released model is therefore
"noisier" by design, with wide per-image dispersion — which is exactly why the session
mean, not any single photo, is the number worth tracking.
