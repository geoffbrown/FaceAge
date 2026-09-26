# FaceAge

Track your FaceAge, the face-based biological age estimate from Harvard's AIM Lab,
session by session on your own Mac. You take ten photos, the app checks them, scores
them with the published model and adds the result to a chart of your trend. Photos
and results never leave the Mac.

- Official FaceAge code and weights: <https://github.com/AIM-Harvard/FaceAge>
- Paper: Bontempi et al., *Lancet Digital Health*, 2025,
  <https://www.thelancet.com/journals/landig/article/PIIS2589-7500(25)00042-1/fulltext>

This is an independent app built on that release. It runs the authors' model and
weights unmodified, in a container that reproduces their environment exactly
(see [Does it match the authors?](#does-it-match-the-authors)). It is not
affiliated with or endorsed by the FaceAge authors, and it is a research model,
not a medical test.

## Quick start

You need an Apple Silicon Mac on macOS 13 or newer, [Homebrew](https://brew.sh),
about 10 GB of free disk, and 15 minutes for the first install.

```bash
git clone https://github.com/geoffbrown/FaceAge.git ~/Documents/GitHub/FaceAge
~/Documents/GitHub/FaceAge/tools/install.sh
```

Then, in a new terminal window:

```bash
faceage app
```

The app opens in your browser. Leave that terminal open while you use it, and press
Ctrl-C there to stop it.

The installer sets up the container runtime, Rosetta 2, the model weights (checked
against the authors' release) and the `faceage` command, and says what it is doing
at each step. Re-running it is safe; anything already done is skipped. If something
seems wrong later, `faceage doctor` checks the whole setup.

## Using the app

**1. Who are we measuring?** Add yourself, with your birthday so the chart can
show FaceAge against your real age. Each person gets a separate tracker.

**2. Take the photos.** Either let the app use the Mac's camera (it frames you,
waits until you are still, and takes ten shots), or import photos you took on
your phone. HEIC is converted on import. The first time, open *How to set up the
shot*: it is short, and it matters more than anything else here (see below). Tick
anything that is different today, such as different light or no shave, so it is
recorded with the session.

**3. Analyse.** The app checks every photo (one face, framed large enough,
brightness close to your earlier sessions with this camera) and scores the clean
ones. The first run after a restart takes a minute while the model wakes up.

**4. Result.** You get the session's FaceAge (the average of its photos), how
much the photos disagreed, and a verdict: *Good capture*, *Usable, with notes*, or
*Shoot again*, with what to fix. Add it to your tracker, or discard it
and shoot again.

**5. Progress.** The trend over time against your real age, with a trend line
once there are enough sessions, a table of every session, and notes.

The footer shows where your data is and whether it is backed up. Set up a backup
there once and the app keeps it current (see [Backup](#backup-and-moving-to-a-new-mac)).

## Getting a number you can trust

One photo means little. The model reads fine skin texture, so how the photo is
taken moves the number far more than a few months of real change:

- **Brightness.** The same photo made brighter or darker moved FaceAge by 3.7
  years (darker reads older; the measurements are in the technical reference).
- **Camera and framing.** Two sets of ten photos taken ten minutes apart, one
  with a wide webcam shot and one with a tight phone shot, differed by 2.2 years,
  seven times the measurement noise.

So keep everything the same every session: one fixed lamp in a room without
daylight, the same camera in the same place, the same framing, a plain
background, a neutral face. Take ten photos; the session average is the number,
and the app flags a session whose brightness has drifted from your baseline.
Pick one camera and stay with it, because sessions from two cameras cannot be
compared. The full standard is in [docs/CAPTURE_STANDARD.md](docs/CAPTURE_STANDARD.md).

## Your data

Everything lives in `~/FaceAgeData`, outside this repo, one folder per person:

| What | Where, under `~/FaceAgeData/subjects/<name>/` |
|---|---|
| Photos, one folder per session | `sessions/YYYY-MM-DD/` |
| Your series, one row per session | `results/faceage_history.csv` |
| Per-photo results | `results/YYYY-MM-DD_per_image.csv` |

Nothing is uploaded, and `.gitignore` excludes every image type, the model weights
and all result files, so none of it can be committed by accident. Photos of
someone else are their biometric data: ask first, and delete their folder when
you are done.

## Backup and moving to a new Mac

Everything except `~/FaceAgeData` is rebuilt by the installer, so that folder is
what the backup copies. Set it up from the app's footer, or with:

```bash
faceage backup              # to iCloud Drive; or: faceage backup /Volumes/Disk/FaceAge
```

From then on the app updates the copy after every change to your tracker. It is a
plain folder of your files plus a checksum for each, so `faceage backup --verify`
can prove the copy is intact. Earlier versions of changed files are kept, never
deleted. `faceage backup --archive file.zip` makes a single file instead.

On a new Mac, install as in the quick start, then:

```bash
faceage restore "~/Library/Mobile Documents/com~apple~CloudDocs/FaceAge Backup"
```

The installer spots a backup in iCloud Drive and prints this line for you.
`restore` checks the copy before using it. If the new Mac already has data, add
`--merge` to fill in only what is missing, or `--replace` to move the existing
folder aside first; nothing is deleted either way.

## Command line

Everything the app does is also a command, for scripting or if you prefer a
terminal. `faceage help` lists them all; the useful ones:

| Command | What it does |
|---|---|
| `faceage run [FOLDER or photos]` | score a session (today's by default) and add it to the series |
| `faceage chart` | open the progress chart |
| `faceage history` | print the series |
| `faceage preflight [DATE]` | what is wrong with a session's photos, and what to do |
| `faceage compare D1 D2` | two sessions side by side, photo by photo |
| `faceage sensitivity [DATE]` | how much brightness moves FaceAge on your own face |
| `faceage tidy` | move discarded sessions to the Trash |
| `faceage backup` / `restore` | see above |
| `faceage doctor` | check the install |

Add `--subject NAME` to work with someone else's series, or `--no-log` to score
photos without recording anything. `docs/PREREGISTRATION.md` describes the
pre-registered analysis (`faceage analyse`, `gate`, `trend`, `anchor`,
`invalidate`).

## Does it match the authors?

Yes, to the precision they published. Scoring 56 images from the authors' own
validation set reproduces their published values with a largest difference of
0.00005 years, which is rounding in their CSV. Run `faceage validate` to repeat it
(it needs those images; see the technical reference).

The container matches their Python 3.6 / TensorFlow 2.6 environment package for
package and runs under Rosetta on Apple Silicon. Where it had to differ, and why
none of it moves a number, is in [docs/TECHNICAL.md](docs/TECHNICAL.md), along
with how photos are checked and flagged, the columns of the history file, and
how to install without the installer.

One thing worth knowing about the model: the authors deliberately did not tune it
to guess chronological age, because that erased what makes FaceAge predictive of
health. It is noisy by design, which is why the session average, never a single
photo, is the number to watch.

## License

The code written for this repo is MIT licensed ([LICENSE](LICENSE)). The FaceAge
model, its weights and the scoring core adapted from the authors' code are theirs:
their release has no open-source license and is provided for reproducible research,
not clinical care or commercial use, and those terms carry over to this app. The
details are in [NOTICE](NOTICE).
