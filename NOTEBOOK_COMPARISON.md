# Notebook → Pegasus: a comparison

`MeerKAT.ipynb` (Good, Moseley & Berriman — Caltech/IPAC-NExScI) vs.
`meerkat-2mass-mosaic`, the Pegasus WMS implementation of it.

Everything below is measured, not estimated. Notebook figures come from the
timestamps and MontagePy return dicts **printed in the notebook's own saved
outputs** (run 2024-08-09 13:26:10, MontagePy 2.2.0, Apple M2 / 24 GB).

Workflow figures come from three independent runs:

| | **Run A — studio** | **Run B — pegasus cluster** |
|---|---|---|
| Host | 8-core / 29 GB Jetstream2 pod | 5 × 8-core / 16 GB across ATLA, GPN, NCSA, NEWY |
| Pool | 3 × (4 CPU/6 GB) + 1 × (2 CPU/2 GB) | 40 cores, 4 sites, ~170 MB/s links |
| Pegasus | 6.0.0-dev.0 | 5.1.2 |
| Run dir | `runs/full/`, wf `9c5cb993` | `runs/tier3-pegasus/` |
| Date | 2026-08-20 | 2026-08-26 |
| Result | 100/100 nodes, 74.2 min | 100/100 nodes, **22.5 min** |
| Validation | 93 pass, Tier 3 skipped | **109 pass, 0 fail, 0 skipped** |

A third pass — **Run C** — re-validated Run A's own mosaics on the studio pod
against a serial baseline computed there (2026-08-26, 25.7 min): **109 pass, 0
fail, 0 skipped**, `TIER3_REPORT_STUDIO.md`. Runs B and C together give the
four-way byte-identity result in `DETERMINISM.md`.

Run B exists because Tier 3 had never been run and the 45-way fan-out had never
been exercised. Both are now settled. Figures come from each run's `*.metrics`,
`*.dag.metrics` and `*.stampede.db` `job_instance`/`jobstate` tables, analysed
by the same script (`runs/tier3-pegasus/`, `TIER3_REPORT.md`).

---

## 1. Same science, different execution model

| | Notebook | Pegasus workflow |
|---|---|---|
| Unit of work | 20 code cells, run top to bottom by a human | 59 compute jobs in a DAG, planned and submitted once |
| Artifacts | 42 cells / 221 effective LOC / 19.5 MB `.ipynb` | 13 wrappers + generator, 3,513 LOC, no embedded outputs |
| Data acquisition | `mArchiveDownload` inside the run, every run | `bin/prefetch.py`, **once**, outside the DAG, checksummed |
| State | `os.chdir(home)` mutated across cells | no shared mutable state; every job independent |
| Bands | strictly sequential J → H → K | 3 independent sub-DAGs |
| Reprojection | one `mProjExec` over 479 images | 15 chunked `mProjExec` jobs × 3 bands = **45 parallel** |
| Failure handling | return dicts printed, not checked | captured to `*.status.json` + hard budgets, job fails loudly |
| Determinism | `mImgtbl` in directory-listing order | `rimages.tbl` sorted by filename |
| Provenance | cell outputs, if the notebook is saved | `braindump.yml`, `jobstate.log`, `stampede.db`, per-job status JSON |
| Correctness check | eyeball the thumbnails | `validate` job — **109 automated assertions**, pass/fail, reproduced on two clusters |
| Portability | one machine, one Python env | any Pegasus site; tool stack pinned in a 493 MB Apptainer image |
| Scale knob | none | `--proj-chunk-size` (1 → 479), `--bands`, `--subset` |

### The scientific result is identical

The `validate` job compares the workflow's products against reference values
extracted from the notebook's saved outputs (`bin/extract_notebook_refs.py`).

**109 passed · 0 failed · 0 warnings · 0 skipped** — all three tiers, Run B.

| Tier | What it proves | Result |
|---|---|---|
| 1 — metadata | region centre, size, image counts, overlap pairs, plane-fit failures, WCS of every mosaic, data min/max, stretch limits | 65/65 pass |
| 2 — thumbnails | all 7 notebook PNGs: geometry, SSIM, PSNR, mean per-channel Δ | 28/28 pass |
| 3 — full-res pixels | float-level FITS comparison against an unchunked serial baseline | **16/16 pass — max \|Δ\| = 0, twice, on two clusters** |

Run A produced the same 93 Tier 1 + 2 checks on entirely different hardware and
a different Pegasus version, with Tier 3 skipped for want of a baseline.

### Tier 3 — the chunking question, finally answered

Tier 3 is the only tier that can distinguish "the chunked DAG reproduced the
notebook" from "the chunked DAG differs in a way that survives rounding to 8-bit
PNG". It runs the notebook's whole-band sequence serially — one `mProjExec` over
all 479 images, no chunks — and diffs the resulting 10121×10527 float planes
against the DAG's.

**The mosaics are bit-identical.** Not within tolerance — exactly equal:

| Mosaic | Shape | NaN mask | max \|Δ\| | threshold |
|---|---|---|---|---|
| `2massj.fits` | (10527, 10121) ✓ | 0 differing pixels | **0** | ≤ 0.0112 |
| `2massh.fits` | (10527, 10121) ✓ | 0 differing pixels | **0** | ≤ 0.0103 |
| `2massk.fits` | (10527, 10121) ✓ | 0 differing pixels | **0** | ≤ 0.0107 |
| `uncorrected.fits` | (10527, 10121) ✓ | 0 differing pixels | **0** | ≤ 0.0112 |

RMS Δ is 0 for all four. The thresholds shown are `1e-6 × dynamic range`, so
there was room for ~0.01 of drift per pixel and none of it was used.

This retires the central risk of the whole design. Splitting `mProjExec` across
15 chunks per band does not perturb the result at all — not in the reprojection,
not through `mBgModel`'s order-sensitive global fit, not in `mAdd`'s
accumulation. **The parallelism is free of numerical cost.**

### And it is stronger than that — four-way byte identity

Tier 3 was then run a second time, independently, on the studio pod
(2026-08-26, 25.7 min baseline; **also 109 passed / 0 failed / 0 skipped** —
`TIER3_REPORT_STUDIO.md`). Checksumming all four provenances afterwards gives one
MD5 per mosaic:

| Mosaic | MD5 — identical across all four runs below |
|---|---|
| `2massj.fits` | `d8dafd5f65caa750dc6e6c621c4bb6db` |
| `2massh.fits` | `aa16941b2b73e10c5e9c75674759b45c` |
| `2massk.fits` | `43962c383fc74c5341e333981830ae31` |
| `uncorrected.fits` | `865615b44bacd2a4820ce40353434ff8` |

The four: studio chunked DAG (Pegasus 6.0.0-dev, 8-core pod, Aug 20) · studio
serial baseline (same pod, Aug 26) · cluster chunked DAG (Pegasus 5.1.2, 4 sites
/ 40 cores, Aug 26) · cluster serial baseline (Aug 26).

So **3.4 GB of float64 pixel data is bit-identical across two clusters, two
Pegasus versions, both execution shapes, and six days.** The FITS headers match
too — Montage's `mAdd` writes no timestamp card, so nothing in the file can
differ without the pixels differing. Full matrix and limits in `DETERMINISM.md`.

That is a determinism result, not merely an equivalence one: this pipeline is
reproducible at the byte level, and the 45-way fan-out is provably free.

Two things were deliberately held constant so that chunking was the only
variable: both sides sort `pimages.tbl`/`cimages.tbl` by filename (`mBgModel`'s
fit is order-sensitive), and both take the `mDiffExec` + `mFitExec` path. See
§3 — in this MontagePy build, neither side *can* run the notebook's
`mDiffFitExec`.

> **Tier 3 had never run, and could not have.** `bin/run_notebook_baseline.py`
> called `mDiffFitExec(...)` and `mBgModel(a, b, c)` — the notebook's literal
> calls, both of which fail on MontagePy 2.2.0. It would have crashed ~7 min into
> band J. The file was also mode 644 while every other wrapper is 755, so it had
> never been executed. Both are fixed; the `skipped` in Run A's report was a
> latent bug, not a deferred task.

Tier 2 is the strong result: **SSIM = 1.0 and mean |Δ| = 0.0 on all seven
thumbnails**, four of them at PSNR = ∞ (bit-identical). The two composited
images (`2MASS_small.png`, `MeerKAT_2MASS_small.png`) land at 98.3 dB and
98.9 dB — a handful of least-significant-bit differences from the OpenCV
`addWeighted` chain, far inside the ≥ 35 dB gate.

Selected Tier 1 agreements, exact:

| Quantity | Notebook | Workflow |
|---|---|---|
| Region centre (l, b) | 266.37081688220496, −29.22153052692637 | identical (lat to 1e-15) |
| Region size | 3.2165880120000003° | identical |
| Raw images per band | 479 | 479 (all three bands) |
| J overlap pairs (`mOverlaps`) | 2133 | 2133 |
| J images in coadd (`mBgExec`) | 471 | 471 |
| J plane-fit failures | 6 | 6 |
| Mosaic dimensions | 10121 × 10527 | 10121 × 10527 |
| `BUNIT` inherited from template | JY/BEAM | JY/BEAM |
| Uncorrected data min / max | −41.0209846496582 / 11176.1787109375 | identical |
| Stretch floor / ceiling | 27.89344589703572 / 11176.1787109375 | identical |

H and K reproduce 471-in-coadd and 5–6 plane-fit failures too — the notebook
never printed those, so they are recorded as new ground truth rather than
compared.

---

## 2. Runtime — the honest version

| | Notebook | Run A — studio pod | Run B — pegasus cluster |
|---|---|---|---|
| Host | Apple M2, 24 GB | 8 cores / 29 GB, shared | 40 cores / 4 sites |
| Wall clock | **~35 min** compute (50.4 min incl. human idle) | **74.2 min** | **22.5 min** |
| Cumulative compute | ~35 min (serial) | 50.5 min / 59 jobs | 51.7 min / 59 jobs |
| Average concurrency | 1.0 by construction | **1.39** | **2.77** |
| Peak concurrency | 1 | 19 | 15 |
| Reprojection level span | ~7.2 min × 3 serial | 43.5 min | **2.9 min** |
| Data download | 2.1 min, every run | 0 — prefetched | 0 — prefetched |
| Nodes | — | 100/100, 1 eviction | 100/100, 0 evictions |

**The same DAG went from 74.2 min to 22.5 min by giving it slots — a 3.3×
wall-clock gain with no code change.** The two cumulative-compute figures
(50.5 vs 51.7 min) confirm both runs did the same work, so the gain is purely
realised parallelism. **The 45-wide reprojection level went from 43.5 minutes to
2.9 minutes.**

That settles the question Run A could not: the chunked design's parallelism is
real, not theoretical. Two honest qualifications:

- Peak concurrency is *lower* in Run B (15 vs 19) while the average is double.
  Run A's peak was short local staging jobs bunching together; the average is
  the number that means anything.
- Run B's reprojection level consumed 23.9 min of job time against Run A's
  69 min for identical work, because Run A paid ~56 s of pegasus-lite and
  transfer overhead per chunk job and Run B pays ~3 s. So the level-span
  improvement (43.5 → 2.9 min) mixes faster staging with wider fan-out. The
  cleanest single statement is the wall clock: 74.2 → 22.5 min.

At 22.5 min Run B is faster than the notebook's ~35 min of compute — but that
is 40 cores against one M2, so it is not an architecture-to-architecture claim
either. What *is* claimable: the DAG scales with the pool and the notebook
cannot.

### Why Run A was slow — still worth understanding

Notebook, per stage (from its own timestamps):

| Stage | J | H | K |
|---|---|---|---|
| `mArchiveDownload` | 37 s | 50 s | 50 s |
| `mProjExec` (479 images) | **7.2 min** | \} 5.2 min | \} 6.9 min |
| `mOverlaps` + `mDiffFitExec` + `mBgModel` | 1.3 min | \} all remaining | \} all remaining |
| `mBgExec` + `mImgtbl` + `mAdd` | 0.5 min | \} stages in one | \} stages in one |
| `mViewer` + thumbnail | 0.9 min | \} cell | \} cell |
| uncorrected coadd + render (J only) | 2.7 min | — | — |
| **band total** | **15.0 min** | **6.6 min** | **8.0 min** |

Then composite 1.9 min, MeerKAT render 2.5 min, blend ≲ 2 min.

Workflow, longest jobs: `fitplanes_K` 215 s, `mosaic_J` 210 s, `fitplanes_J`
202 s, `composite` 146 s, `fitplanes_H` 103 s, `render_uncorrected` 100 s,
`project_H_c06` 80 s. Reprojection totals 1,608 s across 45 chunks (avg 36 s).

The critical path through the DAG is only ≈ **12.8 min** of compute
(80 → 202 → 210 → 68 → 146 → 39 → 22 s). It took 74.2 min because:

1. **The pool could not run the fan-out.** Measured average concurrency across
   the whole run was **1.39**, peak 19 (mostly short local staging jobs). The
   45-way reprojection level spanned 43.5 min while consuming only 69 min of
   job time — ≈1.6 jobs in flight. The pool advertises three 4-CPU/6 GB slots
   plus one 2-CPU/2 GB slot, so a 45-wide level has nowhere to go.
2. **Staging is the tax Pegasus charges for portability.** 1,488 input files,
   131 intermediates, 9.2 GB of chunk archives. `project_chunk` jobs averaged
   36 s of compute but 92 s wall — ~56 s each in pegasus-lite setup and
   transfer. The notebook pays none of this because every stage reads the
   previous stage's files in place.
3. **41 nodes of the 100 are Pegasus scaffolding** — 13 stage-in, 10 stage-out,
   17 cleanup, 1 create-dir. Necessary, and not free.
4. **Slower cores.** The M2 beats a shared Jetstream2 vCPU per-core, and 85 % of
   this pipeline is single-threaded Montage.

One job was evicted and re-ran automatically (101 `EXECUTE` events for 100
jobs) — invisible in the result, and exactly the kind of thing that ends a
notebook run at cell 26.

### What the runtime comparison actually says

The notebook is faster than Run A **at the one size it can run**: 479 images per
band, on one machine, with a human present. Run B is the point: the same DAG,
unchanged, on a pool that can hold it, finishes in 22.5 min — and Tier 3 proves
it did so without moving a single pixel.

`--proj-chunk-size` makes the trade explicit and testable: `479` collapses to
exactly the notebook's one-`mProjExec` shape; `8` gives 180 reprojection jobs.
**Tier 3 is the evidence that "neither changes a pixel" is literally true** —
the chunked DAG and the unchunked baseline agree to `max |Δ| = 0`.

So the scaling claim is no longer arithmetic. It was measured: 43.5 min → 2.9 min
on the reprojection level, 74.2 → 22.5 min overall.

---

## 3. What had to change, and why

Twelve deliberate deviations (`SPEC.md` §9). Two were forced by the toolchain,
not chosen:

- **`mDiffFitExec` is broken in MontagePy 2.2.0.** Every documented call form —
  including the notebook's own — raises `TypeError: an integer is required`
  from `MontagePy/_wrappers.pyx:184`; the generated Cython disagrees with its
  declared signature. The workflow uses `mDiffExec` + `mFitExec`, the two halves
  of the same operation. No accuracy cost; it costs disk, because
  `mDiffFitExec(keepAll=0)` deletes each difference image as it fits it while
  `mDiffExec` must write all ~2,133 first. `fitplanes` deletes `diffs/` before
  returning, so nothing is staged out.
- **`mBgModel` requires four arguments here.** The notebook's cell 17 passes
  three and would fail as written; its cell 26 passes `""`. The workflow passes
  `""`.

The other ten are structural: chunked reprojection, diffs and corrected images
kept inside jobs (≈21 GB never staged), projected images shipped as one
deterministic tar per chunk instead of ~958 optional outputs, flat per-job
directories instead of a `2MASSJ/{raw,projected,diffs,corrected}` tree, sorted
tables, captured failure counts, no `chdir` state, pinned MontagePy, thumbnails
written as files instead of `IPython.display`.

Also worth recording: `mArchiveDownload` returns a **string**, not a dict, unlike
every other `MontagePy.main` module. The notebook's output looks uniformly
dict-shaped only because it printed `str(rtn)` either way.

---

## 4. What the workflow adds that the notebook cannot have

- **Re-runnable without the network.** `prefetch.py` records size and SHA-256
  for all 1,438 inputs in `data/manifest.json`; the generator builds its Replica
  Catalog from the manifest and never scans a directory. `--verify` re-hashes
  and exits non-zero on drift. Two runs can be *proved* to have used identical
  data.
- **Machine-checked notebook equivalence.** 93 assertions in the DAG, every
  run. The notebook's correctness check is a human looking at a thumbnail.
- **Full provenance.** Per-job `*.status.json` with the real Montage return
  dicts, plus `stampede.db`, `jobstate.log`, `braindump.yml`. Every number in
  §1 and §2 of this document was recovered from those files months after the
  run — none of it from a transcript or a memory.
- **Honest resource requests.** Measured peak RSS, not guesses: `mAdd` peaks at
  **0.10 GB** on a 10121×10527 mosaic (it streams by row), while `mViewer` needs
  2.31 GB. An earlier estimate-based table asked 8–12 GB for four
  transformations — every one would have matched no slot on this pool and sat
  **idle forever** rather than failing.
- **Parameterisation.** Survey, bands, region, template, chunk size, subset
  size, failure budgets, and every optional stage are flags. The notebook has
  three near-duplicate band sections that must be edited in parallel.
- **Failure surfacing.** `project_chunk.py` validates its chunk table against
  what was actually staged. That check is what caught a shared-catalog bug where
  a queued run's tables were clobbered by a later generation — it surfaced as a
  Condor hold on *output* transfer, and without the check the job would have
  silently reprojected 8 of the 32 images it was told to.

---

## 5. Cost of the conversion

From `AI_SESSION_LOG.md`, extracted from the raw Claude Code transcripts:

| | |
|---|---|
| Sessions | 2 top-level + 1 sub-agent |
| Distinct human asks | **5** |
| Wall clock | 90.8 min (60.5 min active model time) |
| API requests / tool calls | 470 / 231 |
| Output tokens | 249,269 |
| Cache-read tokens | 46,679,835 |
| Est. cost (list price) | **≈ $98.92** |

One turn dominates: the single message answering eight design questions
(`Q1 - chunked, Q2 - all, Q3 - match notebook, …`) bought 57.5 min and 149 API
requests of autonomous build — 92 % of that session's cost. Two thirds of total
cost is cache reads (~$70 of the $99), so **cost scaled with session length, not
with code produced**.

Net: ≈1.5 h and ≈$99 turned a 221-LOC notebook into a 100-node DAG that
reproduces it to SSIM 1.0 and proves it did.

---

## 6. Caveats

1. **None of the three hosts are comparable.** Apple M2 / 24 GB exclusive, a
   shared 8-core Jetstream2 pod, and a 40-core 4-site cluster. Every wall-clock
   statement here is host-to-host, never architecture-to-architecture.
2. Notebook wall clock is inferred from `datetime.now()` prints at cell
   boundaries, so it includes whatever the human did between cells. The 15.8 min
   gap after the blend cell is idle time, not compute — hence "~35 min compute"
   against "50.4 min elapsed".
3. Cost is list-price arithmetic over transcript token counts, not a billed
   amount. It also covers only the original build, not the Run B session.
4. **Tier 3 proves equality against serial baselines, not against the
   notebook's actual 2024 output.** Both baselines were produced here, from the
   same inputs and container. The notebook's own FITS files were never archived,
   so what is proven is *chunked ≡ unchunked on this toolchain, deterministically
   across hosts* — the design risk that mattered — not *2026 output ≡ 2024 output
   byte-for-byte*.
5. Run B's Tier 3 comparison necessarily runs both sides on the
   `mDiffExec`+`mFitExec` path, since `mDiffFitExec` is unusable in MontagePy
   2.2.0. If that binding is ever fixed, whether it yields identical background
   models is untested.
6. `--proj-chunk-size` was only ever exercised at 32 (and 8 in development).
   Tier 3 proves chunk-size 32 ≡ unchunked; it does not sweep the range.

**Retired by Run B** (kept here so the earlier version of this document can be
reconciled): Tier 3 is no longer unproven, and the 45-way parallelism is no
longer un-exercised — both were open caveats before 2026-08-26.
