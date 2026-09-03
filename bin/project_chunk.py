#!/usr/bin/env python3
"""Reproject one chunk of a band's raw images onto the template grid.

This is the workflow's parallel stage: notebook cell 11 runs a single
mProjExec over all 479 images of a band; here the sorted image table has been
split into chunks at DAG-generation time and one job runs mProjExec over each.
Montage still chooses the reprojection module per image and still absorbs
per-image failures, so the per-image behaviour is exactly the notebook's.

quickMode=True is retained from the notebook (it routes to mProjectQL, ~20x
faster via Lanczos interpolation).

Output is a single tar archive rather than individual FITS files, because
mProjExec names its outputs itself and roughly 8 of 479 images per band produce
no output at all. One deterministic archive that always exists avoids both
predicting Montage's naming and declaring ~958 optional Pegasus outputs. Image
and _area planes travel together. See SPEC.md sections 4.3 and 6.1.

Invoked by Pegasus with everything staged flat into the working directory.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402  (support file staged by Pegasus)
import mtbl  # noqa: E402

log = mjob.setup_logging("project_chunk")


def stage_raw_images(chunk_table, raw_dir, log):
    """Lay out raw/ so that mProjExec finds each image where the table says.

    Pegasus stages inputs flat into the working directory, but mProjExec joins
    its `path` argument with the table's filename column. The filename column
    may hold a bare basename or a relative path depending on what mImgtbl was
    pointed at, so the layout is driven by the table rather than assumed.
    """
    table = mtbl.read(chunk_table)
    names = table.filenames()
    log.info("Chunk table %s lists %d images (filename column %r)",
             chunk_table, len(names), table.filename_column())

    os.makedirs(raw_dir, exist_ok=True)
    missing = []
    for name in names:
        target = os.path.join(raw_dir, name)
        os.makedirs(os.path.dirname(target) or raw_dir, exist_ok=True)
        source = os.path.abspath(os.path.basename(name))
        if not os.path.exists(source):
            missing.append(name)
            continue
        if not os.path.lexists(target):
            os.symlink(source, target)

    if missing:
        raise FileNotFoundError(
            "%d image(s) named in %s were not staged into the working "
            "directory: %s" % (len(missing), chunk_table, ", ".join(missing[:5])))

    log.info("Linked %d raw images into %s/", len(names), raw_dir)
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--band", required=True)
    parser.add_argument("--chunk", required=True, help="chunk id, for labelling")
    parser.add_argument("--chunk-table", required=True,
                        help="IPAC table listing this chunk's raw images")
    parser.add_argument("--region-hdr", required=True,
                        help="template header (region.hdr)")
    parser.add_argument("--output-tar", required=True,
                        help="archive of the projected images this chunk produced")
    parser.add_argument("--chunk-image-table", default=None,
                        help="optional mImgtbl of this chunk's projected images "
                             "(diagnostic only; the authoritative table is "
                             "rebuilt by fitplanes.py)")
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--quick-mode", dest="quick_mode", action="store_true",
                        default=True, help="notebook default: quickMode=True")
    parser.add_argument("--no-quick-mode", dest="quick_mode", action="store_false")
    args = parser.parse_args()

    from MontagePy.main import mProjExec, mImgtbl

    raw_dir, proj_dir = "raw", "projected"
    status = mjob.Status("project_chunk", log, band=args.band, chunk=args.chunk,
                         quick_mode=args.quick_mode)

    mjob.ensure_dirs(raw_dir, proj_dir)
    names = stage_raw_images(args.chunk_table, raw_dir, log)
    status.set("input_images", len(names))
    log.info("Disk free before reprojection: %.1f GB", mjob.disk_free_gb("."))

    # Notebook cell 11, restricted to this chunk's table.
    status.run("mProjExec", mProjExec, raw_dir, args.chunk_table, args.region_hdr,
               projdir=proj_dir, quickMode=args.quick_mode)

    produced = mjob.count_fits(proj_dir)
    status.set("projected_images", produced)
    log.info("Reprojected %d of %d input images", produced, len(names))
    if produced < len(names):
        # Normal: images that fall outside or barely graze the template produce
        # nothing. The notebook's own 479 -> 471 attrition is this effect.
        log.info("%d image(s) yielded no projection (outside template coverage) "
                 "-- expected, not an error", len(names) - produced)
    if produced == 0:
        raise RuntimeError("chunk produced no projected images at all; "
                           "check that region.hdr overlaps this chunk")

    if args.chunk_image_table:
        status.run("mImgtbl", mImgtbl, proj_dir, args.chunk_image_table)

    files, size = mjob.make_tar(args.output_tar, proj_dir, log)
    status.set("archived_files", files)
    status.set("archive_bytes", size)

    mjob.require_files(args.output_tar)
    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
