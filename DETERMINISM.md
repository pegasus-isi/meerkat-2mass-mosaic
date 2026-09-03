# Bit-level determinism — the five-way result

The headline reproducibility claim for this workflow, and the evidence for it.

## Claim

The three 2MASS mosaics and the uncorrected J coadd are **byte-for-byte
identical** across two clusters, two Pegasus versions, both execution shapes
(45-way parallel and fully serial), and both data-staging modes (a checksummed
local copy and per-job fetches from IRSA at run time).

## Evidence

3.4 GB of float64 pixel data, five independent provenances, one MD5 per mosaic:

| Mosaic | MD5 — identical in all five rows below |
|---|---|
| `2massj.fits` | `d8dafd5f65caa750dc6e6c621c4bb6db` |
| `2massh.fits` | `aa16941b2b73e10c5e9c75674759b45c` |
| `2massk.fits` | `43962c383fc74c5341e333981830ae31` |
| `uncorrected.fits` | `865615b44bacd2a4820ce40353434ff8` |

The five provenances:

| # | Path | Shape | Inputs | Host | Pegasus | Date |
|---|---|---|---|---|---|---|
| 1 | studio `output/` | chunked DAG, 45 parallel `mProjExec` jobs | prefetch | Jetstream2 pod, 8 cores | 6.0.0-dev.0 | 2026-08-20 |
| 2 | studio `reference/baseline/` | serial, one `mProjExec` per band | prefetch | same pod | — (no Pegasus) | 2026-08-26 |
| 3 | cluster `output/` | chunked DAG, 45 parallel `mProjExec` jobs | prefetch | 4 sites, 40 cores | 5.1.2 | 2026-08-26 |
| 4 | cluster `reference/baseline/` | serial, one `mProjExec` per band | prefetch | `pegasus-submit` | — (no Pegasus) | 2026-08-26 |
| 5 | cluster `runs/archive-full` | chunked DAG, 45 parallel `mProjExec` jobs | **archive** — 1437 images fetched per-job from IRSA | 4 sites, 8 cores each | 5.1.2 | 2026-09-03 |

So the identity holds across every axis that was varied:

- **execution shape** — 1 vs 3 (and 2 vs 4): 45 parallel jobs against one serial
  loop produce the same bytes. This is the chunked design's central risk, retired.
- **data staging** — 3 vs 5: raw pixels read from a checksummed local copy
  against the same 1437 images pulled from IRSA by the chunk jobs themselves,
  with input staging bypassed. Retires the risk that moving the fetch into the
  DAG perturbs the result.
- **hardware and site** — 1 vs 3: a shared Jetstream2 pod against a 4-site
  cluster spanning ATLA, GPN, NCSA and NEWY.
- **Pegasus version** — 1 vs 3: 6.0.0-dev.0 against 5.1.2.
- **time** — 1 vs 5: fourteen days apart, from a re-derived Replica Catalog.

## Why this is stronger than the Tier 3 threshold

`bin/validate.py` Tier 3 passes at `max |Δ| ≤ 1e-6 × dynamic range` — about 0.01
per pixel here. All three Tier 3 runs came back at **exactly 0** for both
`max |Δ|` and `RMS Δ` on all four mosaics, which the checksums then explain: the
agreement is not "inside tolerance", it is identical bytes, FITS headers
included. Montage's `mAdd` writes no timestamp card, so there is nothing in the
file that could differ without the pixels differing.

The archive-mode run (provenance 5) reports **109 passed / 0 failed / 0 skipped**
— the same count as the two prefetch Tier 3 runs. Note that the `validate` job
*inside* the DAG reports 93 passed / 1 skipped instead: the 16 baseline pixel
checks need the 3.4 GB `reference/baseline/` mosaics, which are deliberately not
staged into a worker-node job (`reference.tar` carries only the notebook
expectations and seven thumbnails). That check is non-gating in-DAG and is run
on the submit host afterwards, where the baselines live:

```bash
apptainer exec Apptainer/Montage_Container.sif python3 bin/validate.py \
    --results-dir output --reference-dir reference --region-json data/region.json
```

Omitting `--region-json` makes five Tier 1 region assertions read `None` and
fail spuriously — the in-DAG job gets `region.json` staged, a manual run does not.

## Reproducing the check

```bash
md5sum output/2mass{j,h,k}.fits output/uncorrected.fits \
       reference/baseline/2mass{j,h,k}.fits reference/baseline/uncorrected.fits
```

All eight lines on one host must show the four values in the table above. This
works whichever mode wrote `output/` — that is the point of provenance 5.

## What this does not prove

- **Not equality with the notebook's own 2024 output.** Those FITS files were
  never archived. What is proven is that this pipeline is deterministic and that
  chunking does not perturb it — see `NOTEBOOK_COMPARISON.md` §1 for what *is*
  compared against the notebook (109 assertions, including its reported counts
  and its rendered thumbnails).
- **Not determinism across MontagePy versions.** Both clusters run the pinned
  2.2.0 from the same container image.
- **Not determinism across chunk sizes.** Every chunked run above used
  `--proj-chunk-size 32`. The serial baselines are effectively chunk size 479,
  so 32 ≡ 479 is proven; intermediate values are untested.
- **Not a claim about `mDiffFitExec`.** All five runs take the
  `mDiffExec` + `mFitExec` path, because `mDiffFitExec` is broken in MontagePy
  2.2.0 (`SPEC.md` §9.1).
- **Not independence from the archive at run time.** Provenance 5 proves
  archive mode produces identical bytes; it does not make the archive optional.
  Every archive-mode run re-downloads 1437 images, so IRSA is a runtime
  dependency of the DAG and an outage fails a run mid-flight rather than before
  submit. `--mode prefetch` is what removes that dependency.

  Two mechanisms keep the mode honest rather than merely lucky:
  `--compare-tables` confirms the archive still offers a URL for every image
  the tables name (479/479 per band on 2026-09-03), and the SHA-256s recorded
  when each band was bootstrapped ride along in the Replica Catalog so Pegasus
  verifies every download — `pegasus.integrity.checking = nosymlink` covers
  these transfers, and each chunk's `.in.meta` carries a `checksum.value` per
  image. Discard the bootstrap checksums and that second mechanism is gone: the
  URLs go in unchecksummed and nothing validates what arrives.

- **Not determinism across archive *content* changes.** Both modes read the
  same `mImgtbl` table, so rows, order and chunk boundaries are pinned. What is
  not pinned is IRSA re-issuing an image under the same name with different
  bytes; the carried checksums would catch it as a transfer failure, not
  silently absorb it.

## Provenance of this file

Provenances 1–4: checksums computed 2026-08-26 on both hosts directly, after the
studio Tier 3 run completed at 19:56 UTC. Reports: `TIER3_REPORT.md` (cluster),
`TIER3_REPORT_STUDIO.md` (studio) — both 109 passed / 0 failed / 0 skipped.

Provenance 5: cluster run `runs/archive-full`, planned and submitted 2026-09-03
14:00:38 UTC, all 100 jobs succeeded with 0 retries in 15 min 9 s (36 min 51 s
cumulative job time). Gated beforehand by `--compare-tables` (479/479 images per
band resolve to a URL). Report: `TIER3_REPORT_ARCHIVE.md` /
`tier3_report_archive.json` — 109 passed / 0 failed / 0 skipped. Output mtimes
14:10:46–14:10:57 UTC confirm the four mosaics were written by this run rather
than inherited from the prefetch baseline that previously occupied `output/`.

For reference, the prefetch-mode full run this is compared against
(`runs/full-verify`, 2026-09-02) took 20 min 11 s over the same 4-site pool.
Archive mode came in 5 minutes faster: the 1437 downloads run in parallel on the
workers instead of serially through one condorio staging directory.
