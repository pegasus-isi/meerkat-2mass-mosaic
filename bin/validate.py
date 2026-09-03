#!/usr/bin/env python3
"""Compare the workflow's results against the reference notebook.

Three tiers, in increasing strength (SPEC.md section 7):

  Tier 1  Metadata assertions. The notebook's reference values ARE the Montage
          return dictionaries it printed, so each job captures its own return
          dicts into a .status.json and this script compares them against
          reference/notebook_expectations.json.

  Tier 2  Thumbnails. MeerKAT.ipynb embeds its rendered PNGs; those were
          extracted to reference/notebook_thumbnails/ by
          bin/extract_notebook_refs.py. Compared by SSIM/PSNR, because a
          different machine and OpenCV build will not give identical bytes.

  Tier 3  Full-resolution pixels, against a serial baseline produced by
          bin/run_notebook_baseline.py in the same container. Only this tier can
          detect a semantic difference between chunked and whole-band mProjExec,
          which is the residual risk of the chunked design.

Exit status reflects Tier 1 and the failure budget. Tiers 2 and 3 gate only when
their reference data is present, so the script is useful before the baseline has
ever been run.

Usage (in a job, everything staged flat into the working directory):
    validate.py --results-dir . --reference-dir reference \\
                --report-json validation_report.json \\
                --report-md validation_report.md

Usage (by hand, against the staged-out products):
    apptainer exec Apptainer/Montage_Container.sif bin/validate.py \\
        --results-dir output --reference-dir reference
"""

import argparse
import glob
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("validate")


class Report:
    """Accumulates checks. A check is a dict, not prose, so CI can read it."""

    def __init__(self):
        self.checks = []

    def add(self, name, tier, passed, expected=None, actual=None,
            tolerance=None, gating=True, detail=None):
        self.checks.append({
            "name": name, "tier": tier, "pass": bool(passed),
            "expected": expected, "actual": actual, "tolerance": tolerance,
            "gating": gating, "detail": detail,
        })
        mark = "PASS" if passed else ("FAIL" if gating else "WARN")
        message = "[%s] tier %s  %s" % (mark, tier, name)
        if expected is not None or actual is not None:
            message += "  expected=%s actual=%s" % (expected, actual)
        (logger.info if passed else (logger.error if gating else logger.warning))(message)
        return passed

    def skip(self, name, tier, reason):
        self.checks.append({"name": name, "tier": tier, "pass": None,
                            "skipped": reason, "gating": False})
        logger.info("[SKIP] tier %s  %s  (%s)", tier, name, reason)

    def failures(self):
        return [c for c in self.checks if c.get("gating") and c["pass"] is False]

    def counts(self):
        """(passed, gating failures, skipped, non-gating warnings).

        Warnings are kept separate from failures: reporting "3 failed" next to
        "VALIDATION PASSED" reads as a contradiction, when what happened is
        that three non-gating checks warned.
        """
        passed = sum(1 for c in self.checks if c["pass"] is True)
        failed = sum(1 for c in self.checks
                     if c["pass"] is False and c.get("gating"))
        warned = sum(1 for c in self.checks
                     if c["pass"] is False and not c.get("gating"))
        skipped = sum(1 for c in self.checks if c["pass"] is None)
        return passed, failed, skipped, warned


def close(actual, expected, tolerance, relative=False):
    if actual is None or expected is None:
        return False
    if relative:
        scale = abs(expected) if expected else 1.0
        return abs(actual - expected) <= tolerance * scale
    return abs(actual - expected) <= tolerance


def load_statuses(results_dir):
    """Every *.status.json a job wrote, keyed by filename."""
    statuses = {}
    for path in sorted(glob.glob(os.path.join(results_dir, "*.status.json"))):
        with open(path) as handle:
            statuses[os.path.basename(path)] = json.load(handle)
    return statuses


def montage(status, module):
    return (status or {}).get("montage", {}).get(module, {})


# ------------------------------------------------------------------- tier 1

def tier1(report, expectations, statuses, results_dir, region, args):
    exp_region = expectations.get("region", {})

    # --- geometry -----------------------------------------------------------
    for key, label, tol in [("lon_centre_deg", "region centre longitude", 1e-9),
                            ("lat_centre_deg", "region centre latitude", 1e-9),
                            ("size_deg", "region size (deg)", 1e-9)]:
        report.add(label, 1, close(region.get(key), exp_region.get(key), tol),
                   exp_region.get(key), region.get(key), tol)

    for key, label in [("thumbnail_width", "thumbnail width"),
                       ("thumbnail_height", "thumbnail height")]:
        report.add(label, 1, region.get(key) == exp_region.get(key),
                   exp_region.get(key), region.get(key), "exact")

    # --- per-band counts ----------------------------------------------------
    for band, exp_band in sorted(expectations.get("bands", {}).items()):
        chunk_statuses = [s for name, s in statuses.items()
                          if name.startswith("project_%s_" % band)]
        if not chunk_statuses:
            report.skip("band %s reprojection counts" % band, 1,
                        "band not in this run")
            continue

        inputs = sum(s.get("input_images", 0) for s in chunk_statuses)
        projected = sum(s.get("projected_images", 0) for s in chunk_statuses)

        if args.subset:
            report.skip("band %s raw image count" % band, 1,
                        "--subset %d in effect" % args.subset)
        else:
            report.add("band %s raw image count" % band, 1,
                       inputs == exp_band.get("raw_in_table"),
                       exp_band.get("raw_in_table"), inputs, "exact")

        report.add("band %s chunks reported" % band, 1, len(chunk_statuses) > 0,
                   ">0", len(chunk_statuses), None, gating=False)

        fitplanes_status = statuses.get("fitplanes_%s.status.json" % band, {})
        overlaps = montage(fitplanes_status, "mOverlaps")
        bgexec = montage(statuses.get("mosaic_%s.status.json" % band), "mBgExec")

        if not args.subset and exp_band.get("overlap_pairs"):
            expected_pairs = exp_band["overlap_pairs"]
            actual_pairs = overlaps.get("count")
            report.add("band %s overlap pairs" % band, 1,
                       close(actual_pairs, expected_pairs,
                             args.overlap_tolerance, relative=True),
                       expected_pairs, actual_pairs,
                       "%.0f%% relative" % (100 * args.overlap_tolerance))

        if not args.subset:
            coadd = bgexec.get("count")
            report.add("band %s images in coadd" % band, 1,
                       coadd is not None and coadd >= args.min_coadd_images,
                       ">= %d (notebook: %s)" % (args.min_coadd_images,
                                                 exp_band.get("images_in_coadd")),
                       coadd, "floor")

        # Failure budget -- the notebook's own attrition made visible.
        # fitplanes.py records these under the notebook's key names even though
        # it uses mDiffExec/mFitExec rather than the broken mDiffFitExec binding.
        fit_failed = fitplanes_status.get("fit_failed")
        if fit_failed is not None:
            report.add("band %s plane-fit failures within budget" % band, 1,
                       fit_failed <= args.max_fit_failed,
                       "<= %d (notebook J: %s)" % (args.max_fit_failed,
                                                   exp_band.get("fit_failed")),
                       fit_failed, "budget")

        report.add("band %s reprojection attrition" % band, 1,
                   projected > 0, ">0", "%d of %d" % (projected, inputs),
                   None, gating=False)

    # --- mosaic geometry, WCS and BUNIT -------------------------------------
    exp_mosaic = expectations.get("mosaic", {})
    try:
        from astropy.io import fits
    except ImportError:
        report.skip("mosaic WCS and dimensions", 1, "astropy unavailable")
        return

    hdr_path = os.path.join(results_dir, "region.hdr")
    template_keys = {}
    if os.path.exists(hdr_path):
        with open(hdr_path) as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.partition("=")
                    template_keys[key.strip()] = value.split("/")[0].strip()

    for path in sorted(glob.glob(os.path.join(results_dir, "*.fits"))):
        name = os.path.basename(path)
        if name.endswith("_area.fits"):
            continue
        with fits.open(path) as hdul:
            header = hdul[0].header
            nx, ny = header.get("NAXIS1"), header.get("NAXIS2")

        if exp_mosaic.get("nx"):
            report.add("%s dimensions" % name, 1,
                       (nx, ny) == (exp_mosaic["nx"], exp_mosaic["ny"]),
                       "%sx%s" % (exp_mosaic["nx"], exp_mosaic["ny"]),
                       "%sx%s" % (nx, ny), "exact")

        if expectations.get("j_uncorrected_viewer", {}).get("bunit"):
            expected_bunit = expectations["j_uncorrected_viewer"]["bunit"]
            actual_bunit = str(header.get("BUNIT", "")).strip()
            report.add("%s BUNIT inherited from template" % name, 1,
                       actual_bunit == expected_bunit,
                       expected_bunit, actual_bunit, "exact")

        for key in ("CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CDELT1", "CDELT2",
                    "CTYPE1", "CTYPE2"):
            if key not in template_keys:
                continue
            expected = template_keys[key].strip("'\" ")
            actual = header.get(key)
            try:
                agrees = close(float(actual), float(expected), 1e-9, relative=True)
            except (TypeError, ValueError):
                agrees = str(actual).strip() == expected
            report.add("%s %s matches region.hdr" % (name, key), 1, agrees,
                       expected, actual, "1e-9 relative")

    # --- the strong checks: J uncorrected pixel statistics ------------------
    exp_viewer = expectations.get("j_uncorrected_viewer", {})
    render = montage(statuses.get("render_uncorrected.status.json"), "mViewer")
    if not render:
        report.skip("uncorrected render pixel statistics", 1,
                    "no uncorrected render in this run")
    elif args.subset:
        report.skip("uncorrected render pixel statistics", 1,
                    "--subset %d in effect: the mosaic covers only part of the "
                    "template, so pixel statistics cannot match" % args.subset)
    else:
        for key, label in [("graydatamin", "uncorrected data minimum"),
                           ("graydatamax", "uncorrected data maximum"),
                           ("grayminval", "uncorrected stretch floor"),
                           ("graymaxval", "uncorrected stretch ceiling")]:
            report.add(label, 1,
                       close(render.get(key), exp_viewer.get(key),
                             args.pixel_tolerance, relative=True),
                       exp_viewer.get(key), render.get(key),
                       "%g relative" % args.pixel_tolerance)


# ------------------------------------------------------------------- tier 2

def tier2(report, results_dir, reference_dir, args):
    thumb_dir = os.path.join(reference_dir, "notebook_thumbnails")
    if not os.path.isdir(thumb_dir):
        report.skip("notebook thumbnail comparison", 2,
                    "no %s -- run bin/extract_notebook_refs.py" % thumb_dir)
        return

    try:
        import cv2
        import numpy as np
        from skimage.metrics import structural_similarity, peak_signal_noise_ratio
    except ImportError as error:
        report.skip("notebook thumbnail comparison", 2, "missing %s" % error)
        return

    references = sorted(glob.glob(os.path.join(thumb_dir, "*.png")))
    compared = 0
    for reference_path in references:
        name = os.path.basename(reference_path)
        candidate = os.path.join(results_dir, name)
        if not os.path.exists(candidate):
            report.skip("thumbnail %s" % name, 2, "not produced by this run")
            continue

        reference = cv2.imread(reference_path)
        actual = cv2.imread(candidate)
        if reference is None or actual is None:
            report.add("thumbnail %s readable" % name, 2, False,
                       "readable PNG", "unreadable")
            continue

        if reference.shape != actual.shape:
            report.add("thumbnail %s geometry" % name, 2, False,
                       str(reference.shape), str(actual.shape), "exact")
            continue
        report.add("thumbnail %s geometry" % name, 2, True,
                   str(reference.shape), str(actual.shape), "exact")

        ssim = float(structural_similarity(reference, actual, channel_axis=-1,
                                           data_range=255))
        psnr = float(peak_signal_noise_ratio(reference, actual, data_range=255))
        mean_abs = float(np.mean(np.abs(reference.astype(np.int16) -
                                        actual.astype(np.int16))))

        gating = not args.subset
        report.add("thumbnail %s SSIM" % name, 2, ssim >= args.ssim_min,
                   ">= %.3f" % args.ssim_min, round(ssim, 5), "similarity",
                   gating=gating)
        report.add("thumbnail %s PSNR (dB)" % name, 2, psnr >= args.psnr_min,
                   ">= %.1f" % args.psnr_min, round(psnr, 2), "dB",
                   gating=gating)
        report.add("thumbnail %s mean |Δ| per channel" % name, 2,
                   mean_abs <= args.max_mean_abs,
                   "<= %.2f" % args.max_mean_abs, round(mean_abs, 4), "levels",
                   gating=gating)
        compared += 1

    logger.info("Tier 2 compared %d of %d reference thumbnails",
                compared, len(references))


# ------------------------------------------------------------------- tier 3

def tier3(report, results_dir, reference_dir, args):
    baseline_dir = os.path.join(reference_dir, "baseline")
    baselines = sorted(glob.glob(os.path.join(baseline_dir, "*.fits")))
    if not baselines:
        report.skip("full-resolution pixel comparison", 3,
                    "no baseline in %s -- run bin/run_notebook_baseline.py"
                    % baseline_dir)
        return

    try:
        import numpy as np
        from astropy.io import fits
    except ImportError as error:
        report.skip("full-resolution pixel comparison", 3, "missing %s" % error)
        return

    for baseline_path in baselines:
        name = os.path.basename(baseline_path)
        if name.endswith("_area.fits"):
            continue
        candidate = os.path.join(results_dir, name)
        if not os.path.exists(candidate):
            report.skip("baseline %s" % name, 3, "not produced by this run")
            continue

        with fits.open(baseline_path) as hdul:
            expected = hdul[0].data.astype(np.float64)
        with fits.open(candidate) as hdul:
            actual = hdul[0].data.astype(np.float64)

        if expected.shape != actual.shape:
            report.add("baseline %s shape" % name, 3, False,
                       str(expected.shape), str(actual.shape), "exact")
            continue
        report.add("baseline %s shape" % name, 3, True,
                   str(expected.shape), str(actual.shape), "exact")

        expected_nan = np.isnan(expected)
        actual_nan = np.isnan(actual)
        nan_mismatch = int(np.sum(expected_nan != actual_nan))
        report.add("baseline %s NaN mask" % name, 3, nan_mismatch == 0,
                   "0 differing pixels", nan_mismatch, "exact")

        both = ~expected_nan & ~actual_nan
        if not np.any(both):
            report.add("baseline %s has comparable pixels" % name, 3, False,
                       ">0", 0)
            continue

        difference = np.abs(expected[both] - actual[both])
        max_abs = float(np.max(difference))
        rms = float(np.sqrt(np.mean(difference ** 2)))
        dynamic = float(np.nanmax(expected) - np.nanmin(expected))
        threshold = args.fits_tolerance * dynamic

        report.add("baseline %s max |Δ|" % name, 3, max_abs <= threshold,
                   "<= %.3g (%g x dynamic range %.3g)"
                   % (threshold, args.fits_tolerance, dynamic),
                   "%.3g" % max_abs, "relative to range")
        report.add("baseline %s RMS Δ" % name, 3, rms <= threshold,
                   "<= %.3g" % threshold, "%.3g" % rms, "relative to range",
                   gating=False)


# -------------------------------------------------------------------- output

def write_markdown(path, report, expectations, args):
    passed, failed, skipped, warned = report.counts()
    verdict = "**PASS**" if not report.failures() else "**FAIL**"

    lines = [
        "# Validation report — MeerKAT / 2MASS mosaic workflow",
        "",
        "Comparison against `MeerKAT.ipynb` (run %s, MontagePy 2.2.0)."
        % expectations.get("notebook_run", {}).get("first_timestamp", "2024-08-09"),
        "",
        "| | |",
        "|---|---|",
        "| Verdict | %s |" % verdict,
        "| Checks passed | %d |" % passed,
        "| Checks failed | %d |" % failed,
        "| Warnings (non-gating) | %d |" % warned,
        "| Checks skipped | %d |" % skipped,
    ]
    if args.subset:
        lines.append("| Mode | `--subset %d` — count and pixel checks relaxed |"
                     % args.subset)
    lines.append("")

    if report.failures():
        lines += ["## Failures", ""]
        for check in report.failures():
            lines.append("- **%s** (tier %s): expected `%s`, got `%s`"
                         % (check["name"], check["tier"],
                            check["expected"], check["actual"]))
        lines.append("")

    for tier, title in [(1, "Tier 1 — metadata assertions"),
                        (2, "Tier 2 — notebook thumbnails"),
                        (3, "Tier 3 — full-resolution pixels")]:
        tier_checks = [c for c in report.checks if c["tier"] == tier]
        if not tier_checks:
            continue
        lines += ["## %s" % title, "",
                  "| Check | Expected | Actual | Result |",
                  "|---|---|---|---|"]
        for check in tier_checks:
            if check["pass"] is None:
                result = "skipped — %s" % check.get("skipped", "")
                expected = actual = "—"
            else:
                result = "pass" if check["pass"] else (
                    "**fail**" if check.get("gating") else "warn")
                expected = "`%s`" % check.get("expected")
                actual = "`%s`" % check.get("actual")
            lines.append("| %s | %s | %s | %s |"
                         % (check["name"], expected, actual, result))
        lines.append("")

    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", default=".",
                        help="directory holding the workflow's products")
    parser.add_argument("--reference-dir", default="reference")
    parser.add_argument("--reference-tar", default=None,
                        help="archive of the reference directory, unpacked to "
                             "--reference-dir before comparing. Used when run "
                             "as a Pegasus job: the reference thumbnails have "
                             "the SAME filenames as the workflow's own outputs, "
                             "so they cannot be staged flat alongside them.")
    parser.add_argument("--region-json", default=None,
                        help="default: <results-dir>/region.json")
    parser.add_argument("--report-json", default="validation_report.json")
    parser.add_argument("--report-md", default="validation_report.md")
    parser.add_argument("--subset", type=int, default=0,
                        help="declare that the run used --subset N, which "
                             "relaxes count and pixel checks")
    parser.add_argument("--min-coadd-images", type=int, default=460)
    parser.add_argument("--max-fit-failed", type=int, default=25)
    parser.add_argument("--overlap-tolerance", type=float, default=0.02)
    parser.add_argument("--pixel-tolerance", type=float, default=1e-4)
    parser.add_argument("--ssim-min", type=float, default=0.98)
    parser.add_argument("--psnr-min", type=float, default=35.0)
    parser.add_argument("--max-mean-abs", type=float, default=2.0)
    parser.add_argument("--fits-tolerance", type=float, default=1e-6)
    args = parser.parse_args()

    if args.reference_tar:
        import tarfile
        os.makedirs(args.reference_dir, exist_ok=True)
        with tarfile.open(args.reference_tar, "r") as archive:
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise ValueError("unsafe member %r in %s"
                                     % (member.name, args.reference_tar))
            archive.extractall(args.reference_dir)
        logger.info("Unpacked %s into %s/", args.reference_tar, args.reference_dir)

    expectations_path = os.path.join(args.reference_dir,
                                     "notebook_expectations.json")
    if not os.path.exists(expectations_path):
        logger.error("No %s -- run bin/extract_notebook_refs.py first",
                     expectations_path)
        sys.exit(1)
    with open(expectations_path) as handle:
        expectations = json.load(handle)

    region_path = args.region_json or os.path.join(args.results_dir, "region.json")
    region = {}
    if os.path.exists(region_path):
        with open(region_path) as handle:
            region = json.load(handle)
    else:
        logger.warning("No region.json at %s; geometry checks will fail",
                       region_path)

    statuses = load_statuses(args.results_dir)
    logger.info("Found %d status files in %s", len(statuses), args.results_dir)

    report = Report()
    tier1(report, expectations, statuses, args.results_dir, region, args)
    tier2(report, args.results_dir, args.reference_dir, args)
    tier3(report, args.results_dir, args.reference_dir, args)

    passed, failed, skipped, warned = report.counts()
    payload = {
        "verdict": "pass" if not report.failures() else "fail",
        "passed": passed, "failed": failed, "skipped": skipped,
        "warnings": warned,
        "subset": args.subset,
        "tolerances": {
            "pixel_relative": args.pixel_tolerance, "ssim_min": args.ssim_min,
            "psnr_min": args.psnr_min, "max_mean_abs": args.max_mean_abs,
            "fits_relative": args.fits_tolerance,
            "min_coadd_images": args.min_coadd_images,
            "max_fit_failed": args.max_fit_failed,
        },
        "checks": report.checks,
    }
    with open(args.report_json, "w") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    write_markdown(args.report_md, report, expectations, args)

    logger.info("%d passed, %d failed, %d warned, %d skipped",
                passed, failed, warned, skipped)
    logger.info("Reports: %s, %s", args.report_json, args.report_md)

    if report.failures():
        logger.error("VALIDATION FAILED: %d gating check(s)", len(report.failures()))
        sys.exit(1)
    logger.info("VALIDATION PASSED")


if __name__ == "__main__":
    main()
