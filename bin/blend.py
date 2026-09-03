#!/usr/bin/env python3
"""Blend the 2MASS colour composite with the MeerKAT render -- the final product.

Notebook cell 36:

    cv2.addWeighted(tmassStretch, 0.6, meerkatStretch, 0.4, 64.0)
        -> MeerKAT_2MASS.png
    cv2.resize(blend, shape) -> MeerKAT_2MASS_small.png

The notebook's cell 36 re-derives both stretched images from 2MASS.png and
MeerKAT.png so that the cell can stand alone, repeating work already done in
cells 32 and 34. Here the stretched PNGs are inputs produced by
bin/composite.py and bin/overlay_template.py, which is arithmetically identical
and avoids decoding two ~100 MB PNGs twice.

Both inputs must have identical dimensions -- cv2.addWeighted raises otherwise.
They do by construction, because region.hdr was extracted from the MeerKAT image
so the 2MASS mosaics were built on the MeerKAT pixel grid. The shape check below
is therefore not defensive padding: if it ever fires, the template header did not
propagate and the two images are not of the same sky.
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import mjob  # noqa: E402

log = mjob.setup_logging("blend")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tmass", required=True,
                        help="stretched 2MASS composite PNG")
    parser.add_argument("--template", required=True,
                        help="stretched template (MeerKAT) PNG")
    parser.add_argument("--output", required=True)
    parser.add_argument("--thumbnail", required=True)
    parser.add_argument("--region-json", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--alpha", type=float, default=0.6,
                        help="weight of the 2MASS composite (notebook: 0.6)")
    parser.add_argument("--gamma", type=float, default=64.0,
                        help="constant added to the blend (notebook: 64.0)")
    args = parser.parse_args()

    import cv2

    beta = 1.0 - args.alpha
    status = mjob.Status("blend", log, alpha=args.alpha, beta=beta,
                         gamma=args.gamma)
    region = mjob.load_region(args.region_json)

    tmass = cv2.imread(args.tmass)
    if tmass is None:
        raise RuntimeError("could not read %s" % args.tmass)
    template = cv2.imread(args.template)
    if template is None:
        raise RuntimeError("could not read %s" % args.template)

    log.info("2MASS composite %s: %dx%d", args.tmass, tmass.shape[1], tmass.shape[0])
    log.info("Template       %s: %dx%d", args.template,
             template.shape[1], template.shape[0])

    if tmass.shape != template.shape:
        raise RuntimeError(
            "input geometries differ (%s vs %s). The 2MASS mosaics are built on "
            "region.hdr extracted from the template, so these must match; a "
            "mismatch means the template header did not propagate."
            % (tmass.shape, template.shape))

    status.set("geometry", {"width": tmass.shape[1], "height": tmass.shape[0]})

    blended = cv2.addWeighted(tmass, args.alpha, template, beta, args.gamma)
    if not cv2.imwrite(args.output, blended):
        raise RuntimeError("could not write %s" % args.output)
    log.info("Blended %.1f%% 2MASS + %.1f%% template + %.0f -> %s (%.1f MB)",
             100 * args.alpha, 100 * beta, args.gamma, args.output,
             os.path.getsize(args.output) / 1e6)

    mjob.thumbnail(args.output, args.thumbnail,
                   region["thumbnail_width"], region["thumbnail_height"], log)

    mjob.require_files(args.output, args.thumbnail)
    status.write(args.status_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:                      # noqa: BLE001
        log.error("%s: %s", type(error).__name__, error)
        sys.exit(1)
