# Tier 3 runbook — proving full-resolution pixel equality

Tier 3 needs a serial whole-band baseline to compare against. This is how to
produce it, and the exact options to launch it in Pegasus AI Studio.

> **Status: Tier 3 passes.** Run on the `ssh pegasus` cluster on 2026-08-26 —
> **109 checks passed, 0 failed, 0 skipped**, with `max |Δ| = 0` and `RMS Δ = 0`
> on all four full-resolution mosaics. Full table in `TIER3_REPORT.md`; the
> DAG that produced it is in `runs/tier3-pegasus/`. The steps below are what
> was actually executed, not a proposal.

> **Read this first.** `bin/run_notebook_baseline.py` could not run before
> 2026-08-26. It called `mDiffFitExec(...)` and `mBgModel(a, b, c)` — the
> notebook's literal calls — both of which fail on MontagePy 2.2.0
> (`SPEC.md` §9.1). It would have crashed ~7 minutes into band J, which is why
> Tier 3 read `skipped` in every earlier report. It now takes the same
> `mDiffExec` + `mFitExec` path as `bin/fitplanes.py`. **You need that fix
> present**, or Tier 3 still cannot run — and the file needs its exec bit,
> which `rsync` and `git` both drop (it shipped as mode 644, another sign it had
> never been run):
>
> ```bash
> # positive test: the fixed version calls mFitExec, the broken one never does.
> # (do NOT grep for mDiffFitExec -- the fixed file still names it five times,
> #  in the comments explaining why it is not used.)
> grep -q 'mFitExec(' bin/run_notebook_baseline.py && echo "fix present" || echo "FIX MISSING"
> chmod +x bin/*.py     # or apptainer exits 255 with "FATAL: permission denied"
> ```

---

## What Tier 3 actually measures

Tiers 1 and 2 compare against the notebook's *reported numbers* and *8-bit
thumbnails*. Neither can distinguish "the chunked DAG reproduced the notebook"
from "the chunked DAG differs in a way that survives rounding to PNG". Tier 3
runs the whole-band sequence unchunked, in the same container, on the same
inputs, and diffs the 10121×10527 float planes.

It isolates exactly one variable: **does splitting `mProjExec` across 15 chunks
change the result?** That is the residual risk of the entire chunked design.

Two things are held constant on purpose, so that chunking is the *only*
difference:

| Held constant | Why |
|---|---|
| `pimages.tbl` / `cimages.tbl` sorted by filename on both sides | `mBgModel`'s global fit is order-sensitive; directory-listing order would inject a difference unrelated to chunking |
| `mDiffExec` + `mFitExec` on both sides | the notebook's `mDiffFitExec` is unusable in 2.2.0, so *neither* side can run it; using different diff/fit paths would confound the measurement |

Pass criterion (`bin/validate.py` `tier3`): identical shape, identical NaN mask,
and `max |Δ|` and `RMS Δ` within threshold, per mosaic.

---

## Sequence

Four steps. Steps 1–2 are one-time per host.

```bash
cd meerkat-2mass-mosaic

# 1. container  (~3 min, 493 MB)
apptainer build --ignore-fakeroot-command \
    Apptainer/Montage_Container.sif Apptainer/Montage_Container.def

# 2. inputs  (~4 GB, ~20-25 min, needs network)
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py
apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --verify

# 3. the workflow — produces output/2mass{j,h,k}.fits to be compared
./workflow_generator.py --submit --min-images 460

# 4. the serial baseline — produces reference/baseline/*.fits
apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py

# 5. compare all three tiers  (see the invocation traps at the end)
cp data/region.hdr output/
apptainer exec Apptainer/Montage_Container.sif bin/validate.py \
    --results-dir output --reference-dir reference \
    --region-json data/region.json --min-coadd-images 460
```

Steps 3 and 4 are independent — the baseline reads only `data/`, the workflow
writes only `output/`. Run them concurrently if you have the cores and the disk;
run them in sequence if you want a clean parallelism measurement from step 3.

---

## Options you actually need

### Step 3 — the workflow

```bash
./workflow_generator.py --submit --min-images 460
```

That is the whole full-field invocation. Everything else already defaults to the
notebook's behaviour:

| Flag | Default | Leave it alone because |
|---|---|---|
| `--bands J H K` | `J H K` | order is blue/green/red for the composite; the composite needs exactly 3 |
| `--proj-chunk-size 32` | `32` | 479 images → 15 chunks/band → 45 parallel reprojection jobs |
| `--quick-mode` | on | this is the notebook's `quickMode=True`; `--no-quick-mode` changes the pixels and Tier 1/2 will fail |
| `--min-coadd-images 460` | `460` | validation floor; the notebook coadds 471 |
| `--execution-site condorpool` | `condorpool` | |

The two you may want to add:

| Flag | When |
|---|---|
| `--min-images 460` | **use it for any full run.** Fails a band whose reprojection lost more than ~19 of 479 images, instead of silently coadding fewer |
| `--stage-out-area` | only if you want the Montage `_area` planes in `output/` (+1.6 GB per band; nothing downstream reads them) |

Do **not** pass for a Tier 3 run:

- `--subset N` — Tier 3 compares full mosaics; a subset makes both sides small
  and the comparison meaningless (and `--min-coadd-images` is ignored under it).
- `--skip-uncorrected` — the uncorrected J coadd is one of the Tier 1 checks
  (data min/max and stretch limits).
- `--skip-blend` / `--skip-validate` — you need the validate job.

The generator plans with `cleanup=inplace`, which matters: the intermediate chunk
archives are ~9 GB and without it the volume can fill. To plan into a named run
directory instead of the default submit dir:

```bash
./workflow_generator.py --min-images 460          # generate only
pegasus-plan --submit -s condorpool -o local --cleanup inplace \
    --dir runs --relative-dir tier3 workflow.yml
```

### Step 4 — the baseline

```bash
apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py
```

| Flag | Default | Notes |
|---|---|---|
| `--bands J H K` | all three | **`--bands J` if disk is tight** — one band ≈ 7 GB transient, and one band is enough to answer the chunking question |
| `--keep-work` | off | retains the per-band work trees, ~7 GB each. Leave off |
| `--quick-mode` | on | must match the workflow, or you are comparing two different algorithms |
| `--subset N` | off | leave off, same reason as above |
| `--out-dir` | `reference/baseline` | where `validate.py` looks; change both or neither |

Cost: **~50 min and ~20 GB for three bands** on a studio-class pod. Measured on
the `ssh pegasus` cluster: **14 min** for all three bands, 6.8 GB of baseline
products retained (the per-band work trees are deleted unless `--keep-work`).

---

## Sizing — this is where it bites

`SPEC.md` §4.2 documents a silent failure mode worth repeating: **a memory
request above the largest slot matches nothing and the job sits idle forever
rather than failing.** Check before you launch:

```bash
condor_status -af Name Cpus Memory
```

| Environment | Slots | Verdict |
|---|---|---|
| Studio pod (the archived run) | 3 × (4 CPU / 6144 MB) + 1 × (2 CPU / 2048 MB) | works — every request is ≤ 5 GB. But a 45-wide level runs ~2 at a time, so expect the measured 74 min |
| `ssh pegasus` cluster | 5 × (8 CPU / 15991 MB) across ATLA, GPN, NCSA, NEWY | 40 cores. **Measured: 22.5 min** for the DAG vs 74.2 on the pod; the 45-wide reprojection level went 43.5 → 2.9 min. No shared filesystem, but the 516 MB container stages in ~3 s per job |

Disk, per `SPEC.md` §4.6: peak ≈ 30 GB (≈ 45 GB transient) for the workflow,
plus 4 GB of inputs, plus ~20 GB for the baseline. **Budget ~70 GB.** If the
volume is smaller, run bands one at a time:

```bash
./workflow_generator.py --submit --bands J --min-images 460
apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py --bands J
```

---

## Launching in Pegasus AI Studio

Studio runs the same commands; the differences are environmental.

1. **Working directory must be the project root.** The generator derives every
   catalog path from its own location, and `sites.yml` hard-codes
   `scratch/` and `output/` under it. Launch from `meerkat-2mass-mosaic/`.

2. **Regenerate the catalogs on the host you plan on.** `sites.yml`,
   `transformations.yml` and `replicas.yml` in the repo carry absolute paths from
   whichever host wrote them (`/home/kthare10/work/...`). Running
   `./workflow_generator.py` rewrites all three. Don't hand-edit them.

3. **`catalogs/<dag-stem>/chunks/` is per-DAG and cleared on each generation**
   (`SPEC.md` §9.3). Generating a second workflow while a first is still queued
   clobbers the first one's chunk tables — observed, and it surfaces as a Condor
   hold on *output* transfer, not as an obvious input error. **Let one run finish
   before generating another**, or use distinct `-o` names.

4. **The container must be readable by the execution site.** It is declared in
   the transformation catalog as `Apptainer/Montage_Container.sif` relative to
   the project; on a distributed pool it is staged per job (516 MB), so the first
   level is slow.

5. **Network is needed only for step 2.** Steps 3–5 are fully offline — that is
   the point of keeping acquisition out of the DAG.

6. **Expect ~59 compute jobs / ~100 DAG nodes** and, on the studio pod,
   ~74 min. Monitor with:

   ```bash
   pegasus-status -l <run-dir>
   pegasus-analyzer <run-dir>          # on failure
   pegasus-statistics -s all <run-dir> # after
   ```

---

## Reading the result

```bash
cat output/validation_report.md
```

Tier 3 rows appear as `baseline 2massj.fits shape` / `NaN mask` / `max |Δ|` /
`RMS Δ`, one group per mosaic. Three outcomes:

- **Tier 3 passes** — chunked reprojection is proven equivalent to the
  notebook's single `mProjExec`. *This is what happened:* all four mosaics came
  back at `max |Δ| = 0`, against a permitted `1e-6 × dynamic range` ≈ 0.01.
- **`max |Δ|` slightly over threshold** — likely float non-associativity in
  `mAdd`'s accumulation order, not a design fault. Report the actual number
  rather than raising the threshold to make it green.
- **NaN mask mismatch** — real. It means a different set of pixels got coverage,
  i.e. chunking changed which images contributed. That would be the one result
  that invalidates the design.

### Two invocation traps that cost me a re-run

`bin/validate.py` **silently produces fewer checks** when its inputs are
incomplete — it does not warn that a whole family of assertions was never
generated. Both of these matter if you run it by hand rather than in the DAG:

| Omission | Symptom |
|---|---|
| no `--region-json` | 5 Tier 1 checks **fail** with `actual=None` (region centre, size, thumbnail dims) |
| no `region.hdr` in `--results-dir` | 32 Tier 1 WCS checks **silently vanish** — 77 total instead of 109, still reporting "0 skipped" |

So the complete manual invocation is:

```bash
cp data/region.hdr output/          # the DAG stages this in; a manual run must too
apptainer exec Apptainer/Montage_Container.sif bin/validate.py \
    --results-dir output --reference-dir reference \
    --region-json data/region.json --min-coadd-images 460
```

Sanity check the total: **109 = 65 Tier 1 + 28 Tier 2 + 16 Tier 3.** If you see
77, `region.hdr` is missing; if you see 93 with one skip, the baseline is.
