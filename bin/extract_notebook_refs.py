#!/usr/bin/env python3
"""Extract the comparison reference for Tiers 1 and 2 out of MeerKAT.ipynb.

The notebook is ~19 MB because it embeds its own rendered PNG outputs and the
printed return dictionaries of every Montage call from its 2024-08-09 run. That
makes the notebook itself a usable reference: no re-run is required to obtain a
cross-machine comparison target.

Two artifacts are produced:

  reference/notebook_thumbnails/*.png    Tier 2 -- the 961x1000 thumbnails the
                                         notebook displayed, byte-for-byte as
                                         written by cv2.imwrite in 2024.

  reference/notebook_expectations.json   Tier 1 -- counts, region geometry,
                                         mosaic dimensions and mViewer pixel
                                         statistics, parsed from the notebook's
                                         stdout rather than hand-transcribed.

Everything here is derived from the notebook by parsing, so the reference cannot
silently drift from the document it claims to represent.

Usage:
    ./extract_notebook_refs.py [--notebook PATH] [--outdir PATH]
"""

import argparse
import ast
import base64
import hashlib
import json
import logging
import os
import re
import struct
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Which notebook cell rendered which thumbnail, and what the workflow's
# corresponding output file is called. Cell indices are 0-based into
# nb["cells"] and were established by reading the notebook source; the script
# verifies each mapping against the cell's source text before trusting it.
THUMBNAIL_MAP = [
    # (cell index, reference filename, substring that must appear in the cell source)
    (15, "uncorrected_small.png",   "uncorrected_small.png"),
    (21, "2massj_small.png",        "2massj_small.png"),
    (26, "2massh_small.png",        "2massh_small.png"),
    (30, "2massk_small.png",        "2massk_small.png"),
    (32, "2MASS_small.png",         "2MASS_small.png"),
    (34, "MeerKAT_small.png",       "MeerKAT_small.png"),
    (36, "MeerKAT_2MASS_small.png", "MeerKAT_2MASS_small.png"),
]

# Which printed dictionary is which.
#
# Parsing by module name does not work: cells 25 and 29 label their mImgtbl
# result "Raw images:", and cell 19 prints THREE dicts of which the middle one
# (mImgtbl over corrected/) has exactly the same keys as cell 9's mImgtbl over
# raw/ -- a name-keyed scan silently overwrites the raw count (479) with the
# corrected count (471). So each dict is addressed by (cell, position) and
# validated against the keys it is expected to carry.
#
# Format: (cell, index-within-cell, expected-module, {required keys}, description)
DICTS = [
    (5,  0, "mGetHdr",         {"status", "ncard"},                        "MeerKAT header"),
    (9,  0, "mArchiveDownload", {"status", "count"},                       "J raw downloaded"),
    (9,  1, "mImgtbl",          {"status", "count", "badfits", "badwcs"},  "J raw image table"),
    (11, 0, "mProjExec",        {"status", "count", "failed", "nooverlap"}, "J reprojection"),
    (13, 0, "mAdd",             {"status", "time"},                        "J uncorrected coadd"),
    (15, 0, "mViewer",          {"status", "nx", "ny"},                    "J uncorrected render"),
    (17, 0, "mOverlaps",        {"status", "count"},                       "J overlaps"),
    (17, 1, "mDiffFitExec",     {"status", "count", "diff_failed", "fit_failed"}, "J diff/fit"),
    (19, 0, "mBgExec",          {"status", "count", "nocorrection", "failed"}, "J background"),
    (19, 1, "mImgtbl",          {"status", "count", "badfits", "badwcs"},  "J corrected table"),
    (19, 2, "mAdd",             {"status", "time"},                        "J corrected coadd"),
    (25, 0, "mImgtbl",          {"status", "count", "badfits", "badwcs"},  "H raw image table"),
    (29, 0, "mImgtbl",          {"status", "count", "badfits", "badwcs"},  "K raw image table"),
]


def png_size(data):
    """Return (width, height) from a PNG byte string without needing PIL."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    # IHDR is always the first chunk: 8-byte signature, 4-byte length,
    # 4-byte type, then width/height as big-endian uint32.
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def cell_text_outputs(cell):
    """All stdout/plain-text output of a cell, concatenated."""
    chunks = []
    for out in cell.get("outputs", []):
        if "text" in out:
            chunks.append("".join(out["text"]))
        data = out.get("data", {})
        if "text/plain" in data:
            chunks.append("".join(data["text/plain"]))
    return "\n".join(chunks)


def cell_pngs(cell):
    """All image/png payloads of a cell, decoded, in order."""
    images = []
    for out in cell.get("outputs", []):
        payload = out.get("data", {}).get("image/png")
        if payload:
            if isinstance(payload, list):
                payload = "".join(payload)
            images.append(base64.b64decode(payload))
    return images


def parse_dicts(text):
    """Every dict literal printed in `text`, in order, with its label.

    Returns a list of (label, dict). The label is whatever preceded the colon
    ("mImgtbl (raw)", "Raw images", ...) and is used only for validation --
    position, not label, identifies a value. mViewer's repr contains bytes
    literals (b'grayscale'), so ast.literal_eval is used, not json.loads.
    """
    found = []
    for match in re.finditer(r"^\s*([^:{}\n]*?)\s*:\s*(\{.*\})\s*$", text, re.MULTILINE):
        label, literal = match.group(1).strip(), match.group(2)
        try:
            value = ast.literal_eval(literal)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(value, dict):
            continue
        # Decode bytes values so the result is JSON-serialisable.
        value = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in value.items()}
        found.append((label, value))
    return found


def parse_timestamps(nb):
    """Every 'YYYY-MM-DD HH:MM:SS' the notebook printed, in cell order."""
    stamps = []
    for i, cell in enumerate(nb["cells"]):
        for match in re.finditer(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                                 cell_text_outputs(cell), re.MULTILINE):
            stamps.append((i, match.group(0)))
    return stamps


def build_expectations(nb, thumb_info):
    """Assemble the Tier 1 expectation set from parsed notebook output."""
    exp = {
        "_comment": "Derived from MeerKAT.ipynb's saved outputs by "
                    "bin/extract_notebook_refs.py. Do not edit by hand.",
        "source_notebook": "MeerKAT.ipynb",
        "bands": {},
        "notes": [],
    }

    per_cell = {i: parse_dicts(cell_text_outputs(c)) for i, c in enumerate(nb["cells"])}

    # Resolve every entry of DICTS, validating position and keys so that a
    # changed notebook produces a loud failure rather than a wrong reference.
    resolved = {}
    problems = []
    for cell, index, module, required, description in DICTS:
        entries = per_cell.get(cell, [])
        if index >= len(entries):
            problems.append("cell %d has %d printed dicts, need index %d (%s)"
                            % (cell, len(entries), index, description))
            continue
        label, value = entries[index]
        missing = required - set(value)
        if missing:
            problems.append("cell %d dict %d (%s, labelled %r) missing keys %s"
                            % (cell, index, description, label, sorted(missing)))
            continue
        if module.lower() not in label.lower().replace(" ", "") and \
                module not in ("mImgtbl", "mGetHdr"):
            # Labels are free text in the notebook; only warn on a mismatch for
            # modules whose label is expected to name them.
            logger.warning("cell %d dict %d labelled %r, expected %s",
                           cell, index, label, module)
        resolved[(cell, index)] = value

    if problems:
        for message in problems:
            logger.error("  %s", message)
        raise SystemExit("notebook output does not match the expected layout; "
                         "DICTS in this script needs updating")

    def get(cell, index, key):
        return resolved[(cell, index)].get(key)

    # --- Region geometry, from cell 5's mExamine/mGetHdr and printed scalars ---
    text5 = cell_text_outputs(nb["cells"][5])
    loc = re.search(r"^location:\s+(\S+)\s+(\S+)\s*$", text5, re.MULTILINE)
    size = re.search(r"^size:\s+(\S+)\s*$", text5, re.MULTILINE)
    shape = re.search(r"^shape:\s+\((\d+),\s*(\d+)\)\s*$", text5, re.MULTILINE)
    if loc and size and shape:
        exp["region"] = {
            "lon_centre_deg": float(loc.group(1)),
            "lat_centre_deg": float(loc.group(2)),
            "size_deg": float(size.group(1)),
            "thumbnail_width": int(shape.group(1)),
            "thumbnail_height": int(shape.group(2)),
        }
    exp["meerkat_header_ncard"] = get(5, 0, "ncard")

    # --- Per-band counts (J is fully instrumented; H and K print only raw counts) ---
    exp["bands"]["J"] = {
        "raw_downloaded": get(9, 0, "count"),
        "raw_in_table": get(9, 1, "count"),
        "raw_badfits": get(9, 1, "badfits"),
        "raw_badwcs": get(9, 1, "badwcs"),
        "projexec_attempted": get(11, 0, "count"),
        "projexec_failed": get(11, 0, "failed"),
        "projexec_nooverlap": get(11, 0, "nooverlap"),
        "overlap_pairs": get(17, 0, "count"),
        "difffit_pairs": get(17, 1, "count"),
        "diff_failed": get(17, 1, "diff_failed"),
        "fit_failed": get(17, 1, "fit_failed"),
        "images_in_coadd": get(19, 0, "count"),
        "nocorrection": get(19, 0, "nocorrection"),
        "bgexec_failed": get(19, 0, "failed"),
        "corrected_in_table": get(19, 1, "count"),
        "uncorrected_add_seconds": get(13, 0, "time"),
        "corrected_add_seconds": get(19, 2, "time"),
    }
    exp["bands"]["H"] = {"raw_in_table": get(25, 0, "count"),
                         "raw_badfits": get(25, 0, "badfits"),
                         "raw_badwcs": get(25, 0, "badwcs")}
    exp["bands"]["K"] = {"raw_in_table": get(29, 0, "count"),
                         "raw_badfits": get(29, 0, "badfits"),
                         "raw_badwcs": get(29, 0, "badwcs")}

    # --- Mosaic dimensions and pixel statistics, from the J uncorrected mViewer ---
    viewer = resolved[(15, 0)]
    exp["mosaic"] = {"nx": viewer.get("nx"), "ny": viewer.get("ny")}
    exp["j_uncorrected_viewer"] = {
        key: viewer.get(key) for key in
        ("type", "bunit", "graydatamin", "graydatamax", "grayminval",
         "graymaxval", "grayminsigma", "graymaxsigma", "colortable", "flipY")
    }

    # --- Wall clock ---
    stamps = parse_timestamps(nb)
    if stamps:
        exp["notebook_run"] = {
            "first_timestamp": stamps[0][1],
            "last_timestamp": stamps[-1][1],
            "timestamp_count": len(stamps),
        }
        exp["notes"].append(
            "Notebook wall clock %s -> %s. MontagePy 2.2.0 was released "
            "2024-05-05 and 2.3.0 on 2025-06-05, so this run used 2.2.0; the "
            "container pins that version." % (stamps[0][1], stamps[-1][1]))

    exp["thumbnails"] = thumb_info

    # --- Caveats that cannot be parsed, but must travel with the numbers ---
    exp["notes"].extend([
        "Cell 11 prints 'mImgtbl (projected)' using the mProjExec return value "
        "(rtn is never reassigned from the mImgtbl call), so the projected-image "
        "count is NOT reported by the notebook. The real number of images "
        "reaching the coadd is mBgExec's count (471 for J), meaning roughly 8 of "
        "479 raw images per band yield no usable projection.",
        "Only the J band prints full diagnostics. H and K print just the raw "
        "image count, so overlap/coadd expectations for H and K are unknown from "
        "the notebook alone and are checked against the Tier 3 local baseline.",
        "mDiffFitExec reported diff_failed=1 and fit_failed=6 for J: per-pair "
        "failures are normal and must not fail the workflow.",
    ])
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_nb = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), os.pardir, "MeerKAT.ipynb")
    parser.add_argument("--notebook", default=os.path.normpath(default_nb),
                        help="path to MeerKAT.ipynb (default: ../../MeerKAT.ipynb)")
    parser.add_argument("--outdir", default=None,
                        help="reference directory (default: <project>/reference)")
    args = parser.parse_args()

    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = args.outdir or os.path.join(project, "reference")
    thumbdir = os.path.join(outdir, "notebook_thumbnails")
    os.makedirs(thumbdir, exist_ok=True)

    if not os.path.exists(args.notebook):
        logger.error("Notebook not found: %s", args.notebook)
        sys.exit(1)

    logger.info("Reading %s (%.1f MB)", args.notebook,
                os.path.getsize(args.notebook) / 1e6)
    with open(args.notebook) as fh:
        nb = json.load(fh)
    logger.info("Notebook has %d cells", len(nb["cells"]))

    # --- Tier 2: thumbnails ---
    thumb_info = {}
    failures = []
    for cell_idx, name, marker in THUMBNAIL_MAP:
        if cell_idx >= len(nb["cells"]):
            failures.append("%s: cell %d out of range" % (name, cell_idx))
            continue
        cell = nb["cells"][cell_idx]
        source = "".join(cell["source"])
        if marker not in source:
            failures.append("%s: cell %d source does not mention %r "
                            "(notebook changed?)" % (name, cell_idx, marker))
            continue
        images = cell_pngs(cell)
        if not images:
            failures.append("%s: cell %d has no image/png output" % (name, cell_idx))
            continue
        if len(images) > 1:
            logger.warning("%s: cell %d has %d images, taking the last",
                           name, cell_idx, len(images))
        data = images[-1]
        width, height = png_size(data)
        path = os.path.join(thumbdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        digest = hashlib.sha256(data).hexdigest()
        thumb_info[name] = {"cell": cell_idx, "width": width, "height": height,
                            "bytes": len(data), "sha256": digest}
        logger.info("  %-26s cell %-3d %4dx%-5d %8.1f KB  %s",
                    name, cell_idx, width, height, len(data) / 1024, digest[:12])

    if failures:
        for message in failures:
            logger.error("  %s", message)
        logger.error("%d of %d thumbnails could not be extracted",
                     len(failures), len(THUMBNAIL_MAP))
        sys.exit(1)

    # Every thumbnail should share the geometry the notebook computed.
    shapes = {(info["width"], info["height"]) for info in thumb_info.values()}
    if len(shapes) != 1:
        logger.warning("Thumbnails disagree on geometry: %s", shapes)
    else:
        logger.info("All %d thumbnails are %dx%d", len(thumb_info), *shapes.pop())

    # --- Tier 1: expectations ---
    expectations = build_expectations(nb, thumb_info)
    exp_path = os.path.join(outdir, "notebook_expectations.json")
    with open(exp_path, "w") as fh:
        json.dump(expectations, fh, indent=2, sort_keys=False)
        fh.write("\n")
    logger.info("Wrote %s", exp_path)

    region = expectations.get("region", {})
    mosaic = expectations.get("mosaic", {})
    logger.info("Region:  %.10f %.10f  size %.9f deg  thumb %dx%d",
                region.get("lon_centre_deg", float("nan")),
                region.get("lat_centre_deg", float("nan")),
                region.get("size_deg", float("nan")),
                region.get("thumbnail_width", 0), region.get("thumbnail_height", 0))
    logger.info("Mosaic:  %sx%s", mosaic.get("nx"), mosaic.get("ny"))
    for band, values in sorted(expectations["bands"].items()):
        logger.info("Band %s:  %s", band,
                    ", ".join("%s=%s" % kv for kv in sorted(values.items())))

    logger.info("Reference extraction complete: %d thumbnails + expectations",
                len(thumb_info))


if __name__ == "__main__":
    main()
