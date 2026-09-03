#!/usr/bin/env python3
"""Render the MeerKAT template image to a PNG matching the mosaic grid.

Notebook cell 34:

    mViewer("-gray <meerkat.fits> -1s max gaussian-log -out MeerKAT.png", "", mode=2)
    cv2.addWeighted(img, 3.0, img, 0, -128.0) -> meerkatStretch.png
    cv2.resize(stretched, shape)              -> MeerKAT_small.png

Two things differ from bin/render.py and are deliberate, both from the notebook:
no -ct colour table is applied, and the stretch floor is -1s.

The heavy contrast (3.0) with a large negative offset (-128) pushes the radio
continuum's faint extended emission down so that only the bright filaments
survive the blend in bin/blend.py.

This is the only job that stages the ~800 MB template FITS. Its output shares
the mosaics' pixel grid because region.hdr was extracted from this very file,
which is what makes the final blend pixel-aligned without any resampling.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402

log = mjob.setup_logging("overlay_template")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="template FITS (MeerKAT)")
    parser.add_argument("--output", required=True, help="full-resolution PNG")
    parser.add_argument("--stretched", required=True)
    parser.add_argument("--thumbnail", required=True)
    parser.add_argument("--region-json", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--contrast", type=float, default=3.0,
                        help="notebook default: 3.0")
    parser.add_argument("--brightness", type=float, default=-128.0,
                        help="notebook default: -128.0")
    parser.add_argument("--stretch-min", default="-1s")
    parser.add_argument("--stretch-max", default="max")
    parser.add_argument("--stretch-mode", default="gaussian-log")
    args = parser.parse_args()

    from MontagePy.main import mViewer

    status = mjob.Status("overlay_template", log, input=args.input,
                         contrast=args.contrast, brightness=args.brightness)
    region = mjob.load_region(args.region_json)

    log.info("Template is %.1f MB", os.path.getsize(args.input) / 1e6)

    # No -ct here: the notebook renders the radio map without a colour table.
    params = "-gray %s %s %s %s -out %s" % (
        args.input, args.stretch_min, args.stretch_max, args.stretch_mode,
        args.output)
    log.info("mViewer %s", params)
    status.set("mviewer_params", params)
    status.run("mViewer", mViewer, params, "", mode=2)

    mjob.require_files(args.output)
    log.info("%s is %.1f MB", args.output, os.path.getsize(args.output) / 1e6)

    mjob.stretch(args.output, args.stretched, args.contrast, args.brightness, log)
    mjob.thumbnail(args.stretched, args.thumbnail,
                   region["thumbnail_width"], region["thumbnail_height"], log)

    mjob.require_files(args.stretched, args.thumbnail)
    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
