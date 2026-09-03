#!/usr/bin/env python3
"""Combine the three band mosaics into a colour composite PNG.

Notebook cell 32:

    mViewer("-blue  <J> -1s max gaussian-log "
            "-green <H> -1s max gaussian-log "
            "-red   <K> -1s max gaussian-log -out 2MASS.png", "", mode=2)
    cv2.addWeighted(img, 2.0, img, 0, -1.0) -> 2massStretch.png
    cv2.resize(stretched, shape)            -> 2MASS_small.png

Note the stretch floor is -1s here, not the -2s used for the single-band
renders, and that the thumbnail is made from the *stretched* image, not the raw
mViewer output. Both follow the notebook.

Band-to-channel assignment follows the order the bands are given, so
--bands J H K maps blue, green, red as the notebook does (longest wavelength
red). Exactly three inputs are required.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402

log = mjob.setup_logging("composite")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blue", required=True, help="mosaic for the blue channel")
    parser.add_argument("--green", required=True)
    parser.add_argument("--red", required=True)
    parser.add_argument("--output", required=True, help="composite PNG")
    parser.add_argument("--stretched", required=True, help="contrast-stretched PNG")
    parser.add_argument("--thumbnail", required=True)
    parser.add_argument("--region-json", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--contrast", type=float, default=2.0,
                        help="notebook default: 2.0")
    parser.add_argument("--brightness", type=float, default=-1.0,
                        help="notebook default: -1.0")
    parser.add_argument("--stretch-min", default="-1s",
                        help="notebook uses -1s for the composite, not -2s")
    parser.add_argument("--stretch-max", default="max")
    parser.add_argument("--stretch-mode", default="gaussian-log")
    args = parser.parse_args()

    from MontagePy.main import mViewer

    status = mjob.Status("composite", log, blue=args.blue, green=args.green,
                         red=args.red, contrast=args.contrast,
                         brightness=args.brightness)
    region = mjob.load_region(args.region_json)

    limits = "%s %s %s" % (args.stretch_min, args.stretch_max, args.stretch_mode)
    params = ("-blue %s %s -green %s %s -red %s %s -out %s"
              % (args.blue, limits, args.green, limits, args.red, limits,
                 args.output))
    log.info("mViewer %s", params)
    status.set("mviewer_params", params)
    status.run("mViewer", mViewer, params, "", mode=2)

    mjob.require_files(args.output)
    log.info("%s is %.1f MB", args.output, os.path.getsize(args.output) / 1e6)

    mjob.stretch(args.output, args.stretched, args.contrast, args.brightness, log)
    # The notebook shrinks the stretched image, not the raw render.
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
