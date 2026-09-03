#!/usr/bin/env python3
"""Render a single-band mosaic to PNG and shrink it to a thumbnail.

Notebook cells 15 and 21:

    mViewer("-ct 1 -gray <in> -2s max gaussian-log -out <out>", "", mode=2)
    cv2.resize(cv2.imread(out), shape) -> <in>_small.png

The stretch is Montage's gaussian-log histogram equalisation from -2 sigma of
the low-level noise distribution to the image maximum, which is what makes both
faint structure and bright sources visible at once.

The mViewer return dictionary is captured into the status file: it carries nx,
ny, graydatamin/max and the derived stretch limits, which are four of the Tier 1
reference values (SPEC.md section 7).

The thumbnail is what Tier 2 compares against the PNGs embedded in the notebook,
so it must use the same geometry -- taken from region.json, computed by
bin/prefetch.py exactly as the notebook computes its `shape`.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402

log = mjob.setup_logging("render")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="mosaic FITS to render")
    parser.add_argument("--output", required=True, help="full-resolution PNG")
    parser.add_argument("--thumbnail", required=True, help="shrunken PNG")
    parser.add_argument("--region-json", required=True,
                        help="region.json, for the thumbnail geometry")
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--label", default="render", help="label for logging")
    parser.add_argument("--color-table", default="1",
                        help="mViewer -ct value (notebook: 1)")
    parser.add_argument("--stretch-min", default="-2s",
                        help="lower stretch limit (notebook: -2s for mosaics)")
    parser.add_argument("--stretch-max", default="max")
    parser.add_argument("--stretch-mode", default="gaussian-log")
    args = parser.parse_args()

    from MontagePy.main import mViewer

    status = mjob.Status("render", log, label=args.label, input=args.input)
    region = mjob.load_region(args.region_json)

    params = "-ct %s -gray %s %s %s %s -out %s" % (
        args.color_table, args.input, args.stretch_min, args.stretch_max,
        args.stretch_mode, args.output)
    log.info("mViewer %s", params)
    status.set("mviewer_params", params)
    status.run("mViewer", mViewer, params, "", mode=2)

    mjob.require_files(args.output)
    log.info("%s is %.1f MB", args.output, os.path.getsize(args.output) / 1e6)

    mjob.thumbnail(args.output, args.thumbnail,
                   region["thumbnail_width"], region["thumbnail_height"], log)

    mjob.require_files(args.thumbnail)
    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
