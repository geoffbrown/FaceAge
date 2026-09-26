# Capture standard

How to take the photos so that sessions can be compared. The app's checklist and
`tools/faceage_preflight.py` implement it.

## §3 Noise control

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

