#!/usr/bin/env python3
"""Apply the background corrections and coadd one band into a mosaic.

Notebook cell 19, optionally plus cell 13:

    mBgExec(projected/, pimages.tbl, corrections.tbl) -> corrected/
    mImgtbl(corrected/)                               -> cimages.tbl
    mAdd(corrected/, cimages.tbl, region.hdr)         -> 2massb.fits + _area

With --uncorrected-output this job also reproduces cell 13, coadding the
*uncorrected* projected images. That mosaic exists in the notebook only to
motivate background rectification (its vertical striping is the illustration),
but it is cheap here -- this job already holds the projected images -- and it
yields the two strongest Tier 1 checks, the exact graydatamin/graydatamax of
the J-band render. See SPEC.md section 7.

cimages.tbl is built here rather than passed in: this job has to stage every
projected image anyway because mAdd needs the pixels, so running mImgtbl
locally costs nothing and inherits its badfits/badwcs skipping, which is how
the notebook filters unusable images.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402
import mtbl  # noqa: E402

log = mjob.setup_logging("mosaic")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--band", required=True)
    parser.add_argument("--region-hdr", required=True)
    parser.add_argument("--tar", action="append", required=True,
                        help="chunk archive of projected images (repeatable)")
    parser.add_argument("--pimages", required=True)
    parser.add_argument("--corrections", required=True)
    parser.add_argument("--cimages", required=True, help="output corrected table")
    parser.add_argument("--output", required=True, help="mosaic FITS to produce")
    parser.add_argument("--uncorrected-output", default=None,
                        help="also coadd the uncorrected projected images "
                             "(notebook cell 13)")
    parser.add_argument("--status-json", required=True)
    args = parser.parse_args()

    from MontagePy.main import mBgExec, mImgtbl, mAdd

    proj_dir, corr_dir = "projected", "corrected"
    status = mjob.Status("mosaic", log, band=args.band, chunks=len(args.tar))

    mjob.ensure_dirs(proj_dir, corr_dir)
    mjob.extract_tars(args.tar, proj_dir, log)
    status.set("projected_images", mjob.count_fits(proj_dir))
    log.info("Disk free: %.1f GB", mjob.disk_free_gb("."))

    # The uncorrected coadd first, while projected/ is untouched (notebook
    # cell 13 -- illustrative, and the source of two Tier 1 reference values).
    if args.uncorrected_output:
        log.info("Coadding uncorrected images (notebook cell 13)")
        status.run("mAdd_uncorrected", mAdd,
                   proj_dir, args.pimages, args.region_hdr, args.uncorrected_output)
        mjob.require_files(args.uncorrected_output)

    status.run("mBgExec", mBgExec, proj_dir, args.pimages, args.corrections, corr_dir)

    bgexec = status.data["montage"]["mBgExec"]
    corrected = mjob.count_fits(corr_dir)
    status.set("corrected_images", corrected)
    log.info("Background-corrected %d images (mBgExec count=%s, nocorrection=%s)",
             corrected, bgexec.get("count"), bgexec.get("nocorrection"))
    if corrected == 0:
        raise RuntimeError("mBgExec produced no corrected images")

    status.run("mImgtbl", mImgtbl, corr_dir, args.cimages)
    rows = mtbl.sort_in_place(args.cimages)
    status.set("cimages_rows", rows)
    log.info("%s has %d rows (sorted by filename)", args.cimages, rows)

    status.run("mAdd", mAdd, corr_dir, args.cimages, args.region_hdr, args.output)

    # mAdd always writes a companion area plane; both are declared outputs.
    area = args.output.replace(".fits", "_area.fits")
    mjob.require_files(args.output, area)
    for path in (args.output, area):
        log.info("%s is %.1f MB", path, os.path.getsize(path) / 1e6)

    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
