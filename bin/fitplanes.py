#!/usr/bin/env python3
"""Model the sky background across one band's reprojected images.

Notebook cell 17, plus the mImgtbl of cell 11 that the notebook prints but
discards. Stages that all operate on the same assembled directory of projected
images:

    mImgtbl(projected/)             -> pimages.tbl
    mOverlaps(pimages.tbl)          -> diffs.tbl        (2133 pairs for J)
    mDiffExec(projected/, diffs...) -> diffs/*.fits
    mFitExec(diffs.tbl, diffs/)     -> fits.tbl
    mBgModel(pimages.tbl, fits.tbl) -> corrections.tbl

They are one job because each needs the full ~3 GB working set and together
they take about 100 seconds; splitting them would add a 3 GB re-stage per split
to save seconds (SPEC.md section 4.5). The outputs are all tiny tables, which
makes this a cheap checkpoint boundary before the coadd.

TWO DEPARTURES FROM THE NOTEBOOK, both forced by MontagePy 2.2.0:

1. The notebook calls mDiffFitExec, which computes and fits each difference in
   one pass. In MontagePy 2.2.0 that binding is broken -- every documented call
   form raises "TypeError: an integer is required" from inside
   MontagePy/_wrappers.pyx, because the generated code disagrees with its own
   declared signature. mDiffExec + mFitExec are the two halves of the same
   operation and work correctly, so they are used instead.

   The cost is disk, not accuracy: mDiffFitExec with keepAll=0 deletes each
   difference image right after fitting it, whereas mDiffExec must write all of
   them before mFitExec runs. For a full band that is ~2133 pairs x ~2 MB x 2
   planes, so budget several GB of scratch inside this job. The difference
   images are transient either way -- they are never staged out.

2. mBgModel requires a fourth argument (gap_dir) in this version; the notebook
   calls it with three. An empty string is passed, matching notebook cell 26.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402
import mtbl  # noqa: E402

log = mjob.setup_logging("fitplanes")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--band", required=True)
    parser.add_argument("--region-hdr", required=True)
    parser.add_argument("--tar", action="append", required=True,
                        help="chunk archive of projected images (repeatable)")
    parser.add_argument("--pimages", required=True, help="output image table")
    parser.add_argument("--diffs", required=True, help="output overlap table")
    parser.add_argument("--fits-table", required=True, help="output plane-fit table")
    parser.add_argument("--corrections", required=True, help="output corrections table")
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--min-images", type=int, default=0,
                        help="fail if fewer than this many projected images "
                             "were assembled (0 disables the check)")
    args = parser.parse_args()

    from MontagePy.main import (mImgtbl, mOverlaps, mDiffExec, mFitExec,
                                mBgModel)

    proj_dir, diff_dir = "projected", "diffs"
    status = mjob.Status("fitplanes", log, band=args.band, chunks=len(args.tar))

    mjob.ensure_dirs(proj_dir, diff_dir)
    mjob.extract_tars(args.tar, proj_dir, log)

    images = mjob.count_fits(proj_dir)
    status.set("projected_images", images)
    log.info("Assembled %d projected images from %d chunk archives",
             images, len(args.tar))
    if args.min_images and images < args.min_images:
        raise RuntimeError("only %d projected images assembled, expected at "
                           "least %d" % (images, args.min_images))
    log.info("Disk free: %.1f GB", mjob.disk_free_gb("."))

    # The authoritative image table for this band. The notebook builds it too
    # (cell 11) but prints the wrong variable, which is why its projected count
    # appears to be 479 rather than the real 471 -- see SPEC.md section 1.2.
    status.run("mImgtbl", mImgtbl, proj_dir, args.pimages)

    # Deterministic row order before anything order-sensitive consumes it:
    # mBgModel's global fit iterates over these rows. See bin/mtbl.py.
    rows = mtbl.sort_in_place(args.pimages)
    status.set("pimages_rows", rows)
    log.info("%s has %d rows (sorted by filename)", args.pimages, rows)

    status.run("mOverlaps", mOverlaps, args.pimages, args.diffs)

    # mDiffExec then mFitExec, standing in for the notebook's mDiffFitExec --
    # see the module docstring for why. All difference images coexist between
    # the two calls, so this is the job's disk high-water mark.
    status.run("mDiffExec", mDiffExec, proj_dir, args.diffs, args.region_hdr,
               diff_dir)
    log.info("Difference images written: %d (%.1f GB free)",
             mjob.count_fits(diff_dir, exclude_area=False),
             mjob.disk_free_gb("."))

    status.run("mFitExec", mFitExec, args.diffs, args.fits_table, diff_dir)

    # Per-pair attrition is normal: the notebook reported diff_failed=1 and
    # fit_failed=6 for J. Recorded under the notebook's own key names so
    # bin/validate.py can budget against the reference values.
    diff_failed = status.data["montage"]["mDiffExec"].get("failed")
    fit_failed = status.data["montage"]["mFitExec"].get("failed")
    status.set("diff_failed", diff_failed)
    status.set("fit_failed", fit_failed)
    if diff_failed or fit_failed:
        log.info("Per-pair attrition: diff_failed=%s fit_failed=%s of %s pairs "
                 "-- expected, not an error", diff_failed, fit_failed,
                 status.data["montage"]["mOverlaps"].get("count"))

    # The difference images are no longer needed; free the space before mosaic
    # stages the same chunk archives into its own working directory.
    import shutil
    shutil.rmtree(diff_dir, ignore_errors=True)
    log.info("Removed %s/ (%.1f GB free)", diff_dir, mjob.disk_free_gb("."))

    # gap_dir is required in MontagePy 2.2.0; notebook cell 26 passes "".
    status.run("mBgModel", mBgModel, args.pimages, args.fits_table,
               args.corrections, "")

    mjob.require_files(args.pimages, args.diffs, args.fits_table, args.corrections)
    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
