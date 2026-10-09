# MeerKAT / 2MASS Mosaic — Pegasus Workflow

A Pegasus WMS implementation of `$HOME/work/workflows/MeerKAT.ipynb` —
*"Creating a Merger of a MeerKAT map of the Galactic Center and a 2MASS mosaic
created with the Montage Image Mosaic Engine"* (Good, Moseley & Berriman,
Caltech/IPAC-NExScI).

The workflow builds three background-rectified 2MASS mosaics (J, H, K) on the
MeerKAT pixel grid, combines them into a colour composite, and blends that with
the MeerKAT 1.28 GHz Galactic Centre map. It then **compares its own results
against the notebook's** and writes a pass/fail report.

`SPEC.md` is the design document — read it for why the DAG is shaped this way.
This file is how to run it.

---

## Quick start

```bash
cd $HOME/work/workflows/meerkat-2mass-mosaic

# 1. Build the container (~3 min, 493 MB)
apptainer build --ignore-fakeroot-command \
    Apptainer/Montage_Container.sif Apptainer/Montage_Container.def

# 2. Extract the comparison reference from the notebook (seconds, no network)
python3 bin/extract_notebook_refs.py

# 3. Acquire the inputs. ~4 GB, ~25 min. Bootstraps each band once,
#    then records IRSA URLs so the DAG fetches the pixels itself.
#    Add --mode prefetch to keep the local copy as the DAG's input.
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py

# 4. Check the wrappers work, on 8 images, outside Pegasus (~5 min)
./run_manual.sh

# 5. Generate, then plan and submit (the generator prints this command;
#    -e condorpool: a plain HTCondor pool with no site catalog)
./workflow_generator.py -e condorpool
pegasus-plan --dir submit -s condorpool -o local --output-dir "$PWD/output" --cleanup inplace --submit workflow.yml
```

Steps 1–3 are one-time. Step 5 can then be repeated indefinitely — *planning*
never touches the network in either mode, because the generator reads only
`data/manifest.json`.

Whether *running* touches the network depends on the mode. The default (archive)
has each chunk job fetch its own images from IRSA, so the archive is a runtime
dependency. `bin/prefetch.py --mode prefetch` keeps the checksummed local copy
as the DAG's input and makes runs fully offline. See
[Archive mode](#archive-mode-fetching-in-workflow-the-default) below.

---

## Why data acquisition is a separate step

`bin/prefetch.py` downloads the ~800 MB MeerKAT template and 1437 2MASS Atlas
images, then records every file's size and SHA-256 in `data/manifest.json`. The
generator builds its Replica Catalog from that manifest and never scans a
directory, so:

- a run's inputs are exactly what the manifest says they were,
- planning is always offline, whichever mode wrote the manifest,
- under `--mode prefetch`, re-running costs nothing in downloads and `--verify`
  can prove two runs used identical data.

```bash
# idempotent: adopts what is already on disk
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py

# re-hash everything against the manifest; non-zero exit on any drift
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --verify

# force a re-download of one band
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --force-band K

# adopt a MeerKAT file you already have, instead of downloading
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py \
    --template-path /path/to/MeerKAT_Galactic_Centre_1284MHz-StokesI.fits
```

`region.hdr` — the template header every 2MASS mosaic is built on — is produced
here too, so no workflow job has to stage the 800 MB template merely to read a
38-card header.

### Archive mode: fetching in-workflow (the default)

Archive mode replaces the 2MASS download with an `mArchiveList` query per band —
a catalog lookup returning a download URL per image and no pixels. The manifest
records those URLs, the generator registers them as remote replicas, and **each
`project_chunk` job downloads its own ~32 images on the worker node**, with
staging bypassed per input. This is structurally what
[montage-workflow-v3](https://github.com/pegasus-isi/montage-workflow-v3) does.

```bash
# the default -- bootstraps any band with no table yet, then lists
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py

# gate it before submitting: can the archive still supply every image
# the tables name?
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --compare-tables

# opt out, per band if you like: --bands J
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --mode prefetch
```

**It still needs each band's image table, so every band is downloaded exactly
once.** `mProjExec` requires per-image WCS; MontagePy 2.2.0's `mArchiveList`
returns only `{file, url}` (the *command-line* tool returns a full `fitshdr`
table, but this container is a pip-only install with no Montage executables);
and only `mImgtbl` can build the table, by reading FITS headers off disk.

So archive mode **bootstraps**: a band with no table is downloaded as prefetch
mode would, `mImgtbl` builds the table, every image is hashed, and only then
does the `mArchiveList` query run. A fresh checkout pays the prefetch cost once;
every run after it, and every other machine handed the 263 KB table, pays a
catalog query. The bootstrap's SHA-256s are carried into the URL replicas, which
is what lets Pegasus integrity-check each run-time download — the reason to
bootstrap rather than list blind. Afterwards the pixels are redundant and can be
deleted; nothing deletes them for you, and keeping them leaves `--mode prefetch`
available.

|  | archive (default) | prefetch (`--mode prefetch`) |
|---|---|---|
| Raw pixels needed on the submit host | none after bootstrap (263 KB of tables per band) | ~2.4 GB |
| Downloads per run | every run | once, ever |
| Download parallelism | one connection per running chunk job | serial, through one condorio staging dir |
| Integrity checking | bootstrap checksums, carried into the URL replicas | SHA-256 on every input |
| Archive outage | fails mid-DAG | fails before submit, visibly |
| Job count | identical — see below | identical |
| Output | **byte-identical** (verified, full 3-band, 1437 images) | — |

Mixed manifests are fine: run `--mode prefetch --bands J` and the other bands
stay in archive mode. The manifest is the contract, so the generator needs no
flag of its own — it registers each band according to its own entry.

**Reproducibility is not sacrificed.** Both modes share one sorted table, so
rows, order and chunk boundaries are identical; and `mArchiveDownload` does not
decompress, so its `.fits` files are the same gzip bytes `pegasus-transfer`
fetches from the same URL (cfitsio reads them transparently). Bootstrap-recorded
checksums therefore validate archive downloads unchanged.

Verified at full scale on the `ssh pegasus` cluster, 2026-09-03: a three-band
479-image-per-band archive run (all 1437 images fetched per-job from IRSA, 100
jobs, 0 retries, 15 min 9 s) produced **all four mosaics byte-identical** to the
prefetch baseline, and Tier 3 came back at exactly 0 for both `max |Δ|` and
`RMS Δ` — 109 passed / 0 failed / 0 skipped. It is `DETERMINISM.md`'s fifth
provenance. What you do give up is independence from the archive at run time.

The ~1 GB MeerKAT template is downloaded either way — `region.hdr` and
`region.json` come from `mGetHdr`/`mExamine` on it at generation time, and
`overlay_template` needs the pixels. Archive mode removes the 2MASS raw data,
not the template.

### Archive mode does not enable more clustering

Worth stating because it is the obvious next question. The job count is set by
`--proj-chunk-size`, which is driven by the image **table** — metadata, present
in both modes. Where the bytes come from does not change the shape of the DAG.

And Pegasus horizontal clustering would still be redundant, for the reason in
[SPEC.md](SPEC.md) §4.2: the 45 `project_chunk` jobs are *already* batches of 32
images, so `clusters.size=3` on them is arithmetically identical to
`--proj-chunk-size 96`, just less visible and not settable from the command
line. The one way archive mode could create a clustering *need* is splitting the
fetch into its own per-image job — 1437 tiny download jobs — which is why the
fetch stays inside `project_chunk`.

If anything the pressure runs the other way: unthrottled connections scale with
the number of running chunk jobs, so archive mode argues for **fewer, larger**
chunks.

---

## What the workflow does

Per band, three stages; then four shared stages.

```
project_chunk ×15   mProjExec over 32 images  ──┐
                                                ├─→ fitplanes ──→ mosaic ──→ render
                    (the parallel stage)      ──┘   mOverlaps      mBgExec    mViewer
                                                    mDiffExec      mAdd
                                                    mFitExec
                                                    mBgModel
                                                         │
   J,H,K mosaics ────────────────────────────────────────┴──→ composite ─┐
   MeerKAT template ─────────────────────────────────────────→ overlay ──┴→ blend → validate
```

With the default `--proj-chunk-size 32` that is **≈ 59 jobs**: 45 reprojection
chunks, 3 × (fitplanes, mosaic), 4 renders, composite, overlay, blend, validate.

Only reprojection is chunked, because it is ~85 % of per-band compute (7 of the
notebook's ~10 min per band). Everything after it totals ~2.5 min and needs the
same 3 GB working set, so splitting it further would add a 3 GB re-stage to save
seconds. See `SPEC.md` §4.5.

---

## Arguments

```bash
./workflow_generator.py --help
```

| Flag | Default | Notes |
|---|---|---|
| `--bands J H K` | `J H K` | order sets blue/green/red; composite needs exactly 3 |
| `--proj-chunk-size N` | `32` | the parallelism knob. `--proj-chunk-size 479` reproduces the notebook's single `mProjExec` per band |
| `--subset N` | off | first N images per band — see the runtime note below |
| `--min-images N` | `0` | fail a band if fewer than N images reproject (`460` for a full run) |
| `--min-coadd-images N` | `460` | validation floor |
| `--no-quick-mode` | off | use `mProject`/`mProjectPP` instead of the notebook's `quickMode` |
| `--skip-uncorrected` | off | skip the illustrative uncorrected J coadd |
| `--skip-blend` | off | stop after the per-band mosaics |
| `--skip-validate` | off | omit the comparison job |
| `-s`, `--hosted-site-catalog FILE` | none (`~/.pegasusrc` if set) | [hosted site catalog](https://github.com/pegasushub/pegasus-site-catalogs/tree/main/conf) to plan against, e.g. `access-pegasus.yml`, `unity.yml` |
| `-e`, `--execution-site-name` (alias `--execution-site`) | `compute` | `condorpool` on a plain HTCondor pool with no site catalog |
| `-o`, `--output` | `workflow.yml` | |

Following [pegasus-gromacs](https://github.com/pegasus-isi/pegasus-gromacs), the
generator writes no site catalog — jobs run on `compute`, defined by a hosted
catalog (`-s`, or `pegasus.catalog.site.repo.file` in `~/.pegasusrc`) — and it
never plans or submits: it prints the `pegasus-plan` command. On a plain
HTCondor pool, `-e condorpool` uses the site Pegasus builds by default. The
notebook `MeerKAT-Mosaic-Workflow.ipynb` drives the same
`MeerKATMosaicWorkflow` class interactively (settings parsed by the CLI's own
`build_parser()`; `create_sites_catalog()` writes a local HTCondor `compute`
site for it; submitting is an explicit cell):

```bash
jupyter lab MeerKAT-Mosaic-Workflow.ipynb
```

Stretch and blend constants (`--composite-contrast 2.0`,
`--overlay-brightness -128.0`, `--blend-alpha 0.6`, `--blend-gamma 64.0`, …) all
default to the notebook's values.

### `--subset` does not make a small mosaic

`--subset` reduces how many images are **reprojected**, but the mosaic grid comes
from `region.hdr`, which is the MeerKAT template's own 10121 × 10527 geometry. So
`mAdd` and `mViewer` do full-size work regardless: expect ~1.7 GB mosaics and a
few minutes for the coadd and render even with 8 images, and a largely blank
image. That is correct behaviour. `--subset` is for exercising the plumbing
quickly, not for producing a small picture.

Because a subset mosaic covers only part of the template, `validate.py` is told
about it and relaxes the count and pixel-statistic checks accordingly.

---

## Running

```bash
# generate (it never plans or submits; it prints the command below)
./workflow_generator.py -e condorpool

# then plan and submit — note --cleanup inplace, and --output-dir: with no
# local site in a site catalog, Pegasus would stage outputs to wf-output/
pegasus-plan --dir submit -s condorpool -o local --output-dir "$PWD/output" --cleanup inplace --submit workflow.yml

# monitor
pegasus-status <run-dir>
pegasus-analyzer <run-dir>
pegasus-statistics <run-dir>
```

**`--cleanup inplace` is not optional in practice.** Peak disk is ~30 GB
(~45 GB transient) against the ~64 GB free on this volume; without cleanup, the
9 GB of chunk archives are never reclaimed and a full three-band run can fill
the disk. See `SPEC.md` §4.6 and §10.

The pool this submits to (`studio.pegasus-ai.org`) advertises slots of 4 CPUs /
6 GB (×3) and 2 CPUs / 2 GB (×1) — 14 CPUs, and a hard **6 GB per-job memory
ceiling**. Requests above that match no slot and idle forever rather than
failing, which is why `TOOL_CONFIGS` holds measured values (see the table in
`workflow_generator.py`).

Expect **20–35 min** wall clock against the notebook's ~50 min serial. The gain
is modest at this pool size because 14 CPUs cannot absorb 45 concurrent chunks,
and each job stages the 493 MB container; the DAG is shaped to scale out, and on
a larger pool the reprojection stage collapses toward a single wave.

---

## Validation — does it reproduce the notebook?

Read `output/validation_report.md`. Three tiers (`SPEC.md` §7):

**Tier 1 — metadata.** Every job records the Montage return dictionaries it got
into a `*.status.json`. The notebook's reference values *are* those dictionaries,
parsed out of `MeerKAT.ipynb` into `reference/notebook_expectations.json` by
`bin/extract_notebook_refs.py`. Checks include the region geometry to 1e-9, raw
image counts (479 per band), overlap pairs (2133 for J, ±2 %), images in the
coadd (≥ 460), mosaic dimensions (exactly 10121 × 10527), WCS keywords against
`region.hdr`, `BUNIT = JY/BEAM`, and — the strong ones — the J uncorrected
mosaic's `graydatamin`/`graydatamax` and derived stretch limits to 1e-4 relative.
Those last four are float statistics over 106 million pixels.

**Tier 2 — thumbnails.** `MeerKAT.ipynb` is 19 MB because it embeds its rendered
PNGs. All seven were extracted to `reference/notebook_thumbnails/` (961 × 1000
each) from the actual 2024 run on an Apple M2. Compared by SSIM ≥ 0.98,
PSNR ≥ 35 dB, mean |Δ| ≤ 2/255 — perceptual thresholds, because a different
machine and OpenCV build will not give identical bytes.

**Tier 3 — full-resolution pixels.** Opt-in, ~50 min:

```bash
apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py
apptainer exec Apptainer/Montage_Container.sif bin/validate.py \
    --results-dir output --reference-dir reference
```

This runs the notebook's *unchunked* sequence serially in the same container,
then compares mosaics pixel by pixel (`max|Δ| ≤ 1e-6 × dynamic range`, identical
NaN masks). It is the only tier that can detect a semantic difference between
chunked and whole-band `mProjExec` — the one real risk of this design.

The in-DAG `validate` job runs Tiers 1 and 2. Tier 3 is run by hand because its
reference is ~5 GB of FITS.

---

## Container

`Apptainer/Montage_Container.def` pins **MontagePy 2.2.0** deliberately: 2.2.0
was released 2024-05-05, 2.3.0 on 2025-06-05, and the notebook ran 2024-08-09.
Pinning the version the notebook actually used is what makes the Tier 3 pixel
comparison a like-for-like test rather than a comparison across Montage
releases. `/opt/versions.json` records what the built image contains:

```json
{ "python": "3.10.21", "MontagePy": "2.2.0",
  "opencv-python-headless": "4.10.0.84", "numpy": "1.26.4",
  "astropy": "6.1.2", "scikit-image": "0.24.0" }
```

**The `--ignore-fakeroot-command` flag is required on this host.**
`apptainer build --fakeroot` fails because the host's `libfakeroot.so` needs
`GLIBC_2.38`, newer than the container's glibc; and without fakeroot `apt-get`
cannot do its privilege separation (`setgroups: Operation not permitted`). The
base image is therefore `python:3.10-bookworm` and `%post` uses pip only — no
apt. Python 3.10 also matches MontagePy 2.2.0's `cp310` manylinux wheel, so
nothing is compiled.

---

## Layout

```
workflow_generator.py       DAG + all five catalogs; splits image tables into chunks
run_manual.sh               every step serially on 8 images, outside Pegasus
bin/prefetch.py             pre-workflow acquisition (idempotent, manifest-driven);
                            archive fetch by default, --mode prefetch for a local copy
bin/mtbl.py                 IPAC table sort/split — submit host only
bin/mjob.py                 shared job helpers (staged to every job)
bin/project_chunk.py        mProjExec over one chunk -> tar
bin/fitplanes.py            mImgtbl -> mOverlaps -> mDiffFitExec -> mBgModel
bin/mosaic.py               mBgExec -> mImgtbl -> mAdd
bin/render.py               mViewer -gray + thumbnail
bin/composite.py            three-colour mViewer + contrast stretch
bin/overlay_template.py     MeerKAT render + contrast stretch
bin/blend.py                cv.addWeighted blend — the final product
bin/validate.py             Tier 1/2/3 comparison -> validation_report.{json,md}
bin/extract_notebook_refs.py  pulls the reference out of MeerKAT.ipynb
bin/run_notebook_baseline.py   Tier 3 serial baseline
Apptainer/                  container definition and built .sif
data/                       prefetch output — raws, template, tables, manifest
                            (archive mode: tables + manifest only, no raws)
reference/                  notebook_expectations.json, notebook_thumbnails/, baseline/
catalogs/<dag-stem>/        per-chunk image tables + reference.tar, per DAG
runs/                       Pegasus run directories
output/                     final products
```

---

## Products

`output/` receives: `2massj.fits` `2massh.fits` `2massk.fits` (+ `_area`),
their PNGs and thumbnails, `uncorrected.fits`/`.png`, `2MASS.png` +
`2massStretch.png`, `MeerKAT.png` + `meerkatStretch.png`,
**`MeerKAT_2MASS.png`** (the headline image), every `*_small.png`, every Montage
table (`pimages`, `diffs`, `fits`, `corrections`, `cimages` per band), each job's
`*.status.json`, and `validation_report.json` / `.md`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No data/manifest.json` | acquisition not run | `apptainer exec … bin/prefetch.py` |
| `mArchiveDownload returned an unparseable value` | it returns a *string* holding a dict repr, unlike `MontagePy.main` modules which return real dicts | handled; if it recurs, the archive returned an error document |
| `band X has no image table` from `--compare-tables` | archive mode needs the `mImgtbl` table; `mArchiveList` returns no WCS. `--compare-tables` does not bootstrap one, by design — it changes nothing | run `bin/prefetch.py --bands X` first (it bootstraps), then re-compare |
| `%d images ... absent from the archive listing` | the listing and the table disagree | run `--compare-tables`; use prefetch mode for that band |
| `Expected local file does not exist: .../bin/project_chunk.py`, job then held on **output** transfer for a missing `status.json` | `pegasus.transfer.bypass.input.staging=true` was set globally, so PegasusLite tried to fetch the stageable wrapper from a submit-host path on the worker. Placement-dependent — a job on a worker sharing the submit host's filesystem passes while its sibling fails | never set that property; bypass belongs per input via `Job.add_inputs(bypass_staging=True)`, which is what the generator does. Confirm `pegasus.properties` has no `bypass.input.staging` line |
| In archive mode, many chunk jobs fail on archive timeouts at once | one unthrottled connection per running chunk job | raise `--proj-chunk-size`, throttle with a DAGMan `maxjobs`, or fall back to `--mode prefetch` |
| `chunk produced no projected images at all` | `region.hdr` does not overlap that chunk | check the template and `--subset` selection |
| `expected output(s) not produced` naming `*_area.fits` | a Montage module did not emit its area plane | `mAdd` needs both; re-run that chunk |
| `input geometries differ` in `blend` | the template header did not propagate; the two images are not the same sky | check `region.hdr` came from the same template being blended |
| Disk fills mid-run | planned without cleanup | re-plan with `--cleanup inplace`, or run one band at a time |
| A job sits idle forever, never matched | its memory request exceeds every slot in the pool (6 GB here) | `condor_status -af Name Cpus Memory`, then lower `TOOL_CONFIGS` in `workflow_generator.py`. Requests there are measured peak RSS, not guesses |
| `N image(s) named in rimages_*.tbl were not staged` | a second generator run rewrote a queued workflow's chunk tables | fixed by per-DAG `catalogs/<dag-stem>/`; use a distinct `-o` per configuration |
| Job held on *output* transfer, `*.status.json` missing | the wrapper exited non-zero; the hold is a symptom, not the cause | read `runs/<run>/00/00/<job>.out` — the wrapper's own error is in there |
| `CONTAINER_SITE` unresolved at plan time | `image_site` passed positionally | it must be a keyword — the 4th positional argument is `arguments` |
| Build fails on `setgroups` | `apt-get` without fakeroot | use the pip-only def and `--ignore-fakeroot-command` |

---

## References

- Montage: <http://montage.ipac.caltech.edu/> · <https://github.com/Caltech-IPAC/Montage>
- Heywood, I., Rammala, I., Camilo, F., et al. 2022, *"The 1.28 GHz MeerKAT
  Galactic Center Mosaic"*, ApJ 925, 165
- Berriman & Good 2017, PASP 129, 058006 — Montage and image visualisation
- Jacob et al. 2010, *"Montage: a grid portal and software toolkit for
  science-grade astronomical image mosaicking"*
