#!/usr/bin/env python3
"""Produce the Tier 3 reference by running the notebook's sequence serially.

This is the notebook, unchunked, in the same container the workflow uses: one
mProjExec over each band's whole image table, exactly as MeerKAT.ipynb does it.
Its outputs land in reference/baseline/ and bin/validate.py compares the
workflow's full-resolution mosaics against them.

Why this tier exists: Tiers 1 and 2 compare against the notebook's *reported*
numbers and *rendered thumbnails*, which cannot distinguish "the chunked DAG
reproduced the notebook" from "the chunked DAG differs in a way that survives
rounding to 8-bit PNG and 1e-4 relative statistics". Running the whole-band
sequence here, with the same MontagePy build and the same inputs, isolates
exactly one variable: whether splitting mProjExec across chunks changes the
result. That is the residual risk of the chunked design, and this is the only
thing that can measure it.

Two deliberate departures from the notebook, both for the same reason -- this
tier must vary chunking and nothing else:

1. pimages.tbl and cimages.tbl are sorted by filename, as the workflow sorts
   them. mBgModel's global fit is order-sensitive, so leaving one side in
   directory-listing order would inject a difference that has nothing to do
   with chunking.

2. mDiffExec + mFitExec replace the notebook's mDiffFitExec, which is broken in
   MontagePy 2.2.0 (SPEC.md 9.1) and is what bin/fitplanes.py already does.
   Both sides therefore take the identical diff/fit path.

Neither side runs the notebook's literal mDiffFitExec call, because in this
MontagePy build no side can.

Cost: about 50 minutes and ~20 GB for all three bands. Run one band at a time
if disk is tight.

    apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py
    apptainer exec Apptainer/Montage_Container.sif bin/run_notebook_baseline.py --bands J
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "bin"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("baseline")


def normalise_bands(values):
    """Flatten --bands: web forms send "J H K" / "J,H,K" as ONE argument.

    Same trap as workflow_generator.normalise_bands -- see its docstring.
    """
    import re
    bands = []
    for value in values:
        for part in re.split(r"[,\s]+", str(value).strip()):
            if part and part not in bands:
                bands.append(part)
    return bands


def check(result, what):
    import mjob
    return mjob.check(result, what, logger)


def run_band(band, survey, data_dir, work_root, out_dir, region, args):
    """The notebook's per-band sequence: cells 9-21, unchunked."""
    import mtbl
    from MontagePy.main import (mProjExec, mImgtbl, mOverlaps, mDiffExec,
                                mFitExec, mBgModel, mBgExec, mAdd, mViewer)

    lower = band.lower()
    work = os.path.join(work_root, "%s%s" % (survey, band))
    if os.path.exists(work):
        shutil.rmtree(work)
    for name in ("projected", "diffs", "corrected"):
        os.makedirs(os.path.join(work, name), exist_ok=True)

    with open(os.path.join(data_dir, "manifest.json")) as handle:
        manifest = json.load(handle)
    entry = manifest["bands"][band]

    raw_dir = os.path.abspath(os.path.join(data_dir, entry["raw_dir"]))
    region_hdr = os.path.abspath(os.path.join(data_dir, "region.hdr"))
    table = os.path.abspath(os.path.join(data_dir, entry["table"]))

    timings, results = {}, {}
    previous = os.getcwd()
    os.chdir(work)
    try:
        if args.subset:
            source = mtbl.read(table).sorted_by_filename()
            source = source.subset(source.data_rows[:args.subset])
            table = os.path.abspath("rimages_subset.tbl")
            source.write(table)
            logger.info("Band %s: --subset %d images", band, len(source))

        started = time.time()
        # Notebook cell 11 -- ONE mProjExec over the whole band.
        results["mProjExec"] = check(
            mProjExec(raw_dir, table, region_hdr, projdir="projected",
                      quickMode=args.quick_mode), "mProjExec")
        timings["mProjExec"] = round(time.time() - started, 1)

        results["mImgtbl_projected"] = check(
            mImgtbl("projected", "pimages.tbl"), "mImgtbl (projected)")
        rows = mtbl.sort_in_place("pimages.tbl")
        logger.info("pimages.tbl: %d rows (sorted)", rows)
        results["pimages_rows"] = rows

        # Notebook cell 13 -- the illustrative uncorrected coadd.
        if not args.skip_uncorrected and band == args.bands[0]:
            started = time.time()
            results["mAdd_uncorrected"] = check(
                mAdd("projected", "pimages.tbl", region_hdr, "uncorrected.fits"),
                "mAdd (uncorrected)")
            timings["mAdd_uncorrected"] = round(time.time() - started, 1)
            results["mViewer_uncorrected"] = check(
                mViewer("-ct 1 -gray uncorrected.fits -2s max gaussian-log "
                        "-out uncorrected.png", "", mode=2),
                "mViewer (uncorrected)")

        # Notebook cell 17.
        started = time.time()
        results["mOverlaps"] = check(mOverlaps("pimages.tbl", "diffs.tbl"),
                                     "mOverlaps")

        # mDiffExec + mFitExec, not the notebook's mDiffFitExec, which is broken
        # in MontagePy 2.2.0 -- every documented call form raises "TypeError: an
        # integer is required" from MontagePy/_wrappers.pyx. See SPEC.md 9.1.
        #
        # This is not merely a workaround, it is required for the tier to mean
        # anything: bin/fitplanes.py takes the same two-call path, and Tier 3
        # exists to isolate ONE variable -- whether splitting mProjExec across
        # chunks changes the result. Leaving the baseline on a different
        # diff/fit code path would confound exactly what it is meant to measure,
        # the same reasoning that makes both sides sort pimages.tbl.
        results["mDiffExec"] = check(
            mDiffExec("projected", "diffs.tbl", region_hdr, "diffs"),
            "mDiffExec")
        results["mFitExec"] = check(
            mFitExec("diffs.tbl", "fits.tbl", "diffs"), "mFitExec")

        # Recorded under the notebook's own key names so the reported attrition
        # stays comparable with the notebook's mDiffFitExec dict (J: 1 and 6).
        results["diff_failed"] = results["mDiffExec"].get("failed")
        results["fit_failed"] = results["mFitExec"].get("failed")

        # gap_dir is required in 2.2.0; the notebook's cell 17 passes three
        # arguments and would fail, its cell 26 passes "". So does the workflow.
        results["mBgModel"] = check(
            mBgModel("pimages.tbl", "fits.tbl", "corrections.tbl", ""),
            "mBgModel")
        timings["background_model"] = round(time.time() - started, 1)

        # mDiffFitExec(keepAll=0) deleted each difference image as it fitted it;
        # mDiffExec had to write all ~2133 of them. Reclaim before the coadd.
        shutil.rmtree("diffs", ignore_errors=True)
        os.makedirs("diffs", exist_ok=True)

        # Notebook cell 19.
        started = time.time()
        results["mBgExec"] = check(
            mBgExec("projected", "pimages.tbl", "corrections.tbl", "corrected"),
            "mBgExec")
        results["mImgtbl_corrected"] = check(
            mImgtbl("corrected", "cimages.tbl"), "mImgtbl (corrected)")
        mtbl.sort_in_place("cimages.tbl")

        mosaic = "%s%s.fits" % (survey.lower(), lower)
        results["mAdd"] = check(
            mAdd("corrected", "cimages.tbl", region_hdr, mosaic), "mAdd")
        timings["coadd"] = round(time.time() - started, 1)

        # Notebook cell 21.
        results["mViewer"] = check(
            mViewer("-ct 1 -gray %s -2s max gaussian-log -out %s%s.png"
                    % (mosaic, survey.lower(), lower), "", mode=2), "mViewer")

        # Copy the products worth comparing; the intermediates stay behind.
        os.makedirs(out_dir, exist_ok=True)
        kept = []
        for name in os.listdir("."):
            if name.endswith(".fits") or name.endswith(".png") or \
                    name.endswith(".tbl"):
                if os.path.isfile(name):
                    shutil.copy2(name, os.path.join(out_dir, name))
                    kept.append(name)
        logger.info("Band %s: copied %d products into %s", band, len(kept), out_dir)
    finally:
        os.chdir(previous)

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)

    return {"results": results, "timings": timings}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=os.path.join(PROJECT_DIR, "data"))
    parser.add_argument("--out-dir",
                        default=os.path.join(PROJECT_DIR, "reference", "baseline"))
    parser.add_argument("--work-dir",
                        default=os.path.join(PROJECT_DIR, "baseline_work"))
    parser.add_argument("--bands", nargs="+", default=["J", "H", "K"])
    parser.add_argument("--subset", type=int, default=0)
    parser.add_argument("--quick-mode", dest="quick_mode", action="store_true",
                        default=True)
    parser.add_argument("--no-quick-mode", dest="quick_mode", action="store_false")
    parser.add_argument("--skip-uncorrected", action="store_true")
    parser.add_argument("--keep-work", action="store_true",
                        help="retain the per-band work trees (~7 GB each)")
    args = parser.parse_args()
    args.bands = normalise_bands(args.bands)

    try:
        import mjob  # noqa: F401
        from MontagePy.main import mProjExec  # noqa: F401
    except ImportError as error:
        logger.error("Run inside the container: %s", error)
        logger.error("  apptainer exec Apptainer/Montage_Container.sif "
                     "bin/run_notebook_baseline.py")
        sys.exit(1)

    import mjob

    manifest_path = os.path.join(args.data_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        logger.error("No %s -- run bin/prefetch.py first", manifest_path)
        sys.exit(1)
    with open(manifest_path) as handle:
        manifest = json.load(handle)
    region = manifest["region"]
    survey = manifest["survey"]

    logger.info("Baseline: survey %s, bands %s, MontagePy %s",
                survey, " ".join(args.bands),
                manifest.get("container_versions", {}).get("MontagePy", "?"))
    logger.info("Disk free: %.1f GB", mjob.disk_free_gb(args.data_dir))

    os.makedirs(args.work_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)

    summary = {"survey": survey, "bands": {},
               "container_versions": manifest.get("container_versions", {}),
               "quick_mode": args.quick_mode, "subset": args.subset,
               "note": "Serial whole-band reference for Tier 3. pimages/cimages "
                       "sorted by filename, matching the workflow, so that "
                       "mBgModel ordering is not a confounding variable."}

    overall = time.time()
    for band in args.bands:
        logger.info("=== Band %s ===", band)
        started = time.time()
        summary["bands"][band] = run_band(band, survey, args.data_dir,
                                          args.work_dir, args.out_dir, region,
                                          args)
        logger.info("Band %s finished in %.1f min", band,
                    (time.time() - started) / 60)

    summary["total_minutes"] = round((time.time() - overall) / 60, 1)

    # The composite/overlay/blend stages are notebook cells 32-36 and are pure
    # image arithmetic on the band mosaics; the workflow's own wrappers cover
    # them, and Tier 2 compares their thumbnails. Only the FITS mosaics -- the
    # thing chunking could plausibly change -- are produced here.
    path = os.path.join(args.out_dir, "baseline_summary.json")
    with open(path, "w") as handle:
        json.dump(mjob.jsonable(summary), handle, indent=2)
        handle.write("\n")

    logger.info("Baseline complete in %.1f min -> %s",
                summary["total_minutes"], args.out_dir)
    logger.info("Now compare:\n    apptainer exec Apptainer/Montage_Container.sif "
                "bin/validate.py --results-dir output --reference-dir reference")


if __name__ == "__main__":
    main()
