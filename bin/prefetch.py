#!/usr/bin/env python3
"""Pre-workflow data acquisition for the MeerKAT/2MASS mosaic workflow.

Everything this script does is deliberately OUTSIDE the Pegasus DAG, so the
workflow can be *planned* any number of times without touching the network. It
is idempotent: a second invocation verifies what is already on disk and
downloads nothing.

Steps (mirroring notebook cells 5, 7 and 9):

  1. Fetch the MeerKAT Galactic Centre mosaic (~1 GB), resuming a partial file.
  2. mGetHdr on it  -> region.hdr, the template every 2MASS mosaic is built on.
  3. mExamine on it -> region.json (centre, size, thumbnail shape).
  4. mArchiveDownload per band -> data/raw/<BAND>/
  5. mImgtbl per band, rows sorted by filename -> data/tables/rimages_<BAND>.tbl
  6. data/manifest.json recording every input with size and SHA-256.

The manifest is the contract with workflow_generator.py: the generator populates
the Replica Catalog from it and never scans a directory, so a run's inputs are
exactly what was recorded here.

Note "planned", not "run". In the default archive mode below, step 4 runs once
to bootstrap and the DAG then fetches the raw pixels itself on every execution,
so the workflow does depend on the network at RUN time. --mode prefetch is what
removes that dependency entirely.

TWO FETCH MODES
---------------
`--mode archive` is the DEFAULT. Step 4's mArchiveDownload is replaced by an
mArchiveList call -- a catalog *query* returning a download URL per image and no
pixels. The manifest records those URLs, the generator registers them as remote
replicas with input staging bypassed, and each project_chunk job pulls its own
~32 images straight from IRSA. Structurally this is what montage-workflow-v3
does.

`--mode prefetch` is the opt-out: the raw 2MASS images land on the submit host
and the generator registers them as file:// replicas, checksummed and reused
byte-for-byte across runs. Use it when reproducibility matters more than a light
submit host -- see DETERMINISM.md, whose four provenances are all prefetch runs.

ARCHIVE MODE STILL NEEDS THE BAND'S IMAGE TABLE, and that is not a detail.
mProjExec requires per-image WCS; MontagePy 2.2.0's mArchiveList returns only
{file, url} (the *command-line* mArchiveList returns a full fitshdr table, but
this container is a pip-only install with no Montage executables); and the only
tool here that can produce WCS is mImgtbl, which reads FITS headers off disk.

So the table has to exist, which means the band must be downloaded ONCE. Archive
mode therefore BOOTSTRAPS: a band with no table is downloaded exactly as
prefetch mode would, mImgtbl builds the table, every image is hashed, and only
then does the mArchiveList query run. The first invocation on a fresh checkout
costs what prefetch costs. Every one after it -- and every other machine handed
the 263 KB table -- costs a catalog query.

Two things follow, both good:

  * The bootstrap's SHA-256s are carried into the URL replicas, so Pegasus
    integrity-checks each run-time download against the bytes we actually saw.
    This is the reason to bootstrap rather than list blind.
  * Once the table exists the pixels are redundant: ~800 MB per band can be
    deleted and the table carried between machines on its own. Nothing here
    deletes them for you.

Because both modes share one table, archive mode differs from prefetch mode in
exactly one respect -- where the pixels come from. Same rows, same order, same
chunk boundaries. Checked on the cluster 2026-09-02: a 16-image band-J run in
each mode produced byte-identical outputs. That is one band at subset scale, not
the full three-band mosaics DETERMINISM.md checksums -- see the archive-mode
caveat there for what is and is not proven.

What archive mode costs:

  * Every run re-downloads. The archive is a runtime dependency of the DAG
    rather than a one-time setup step, so a run can fail mid-DAG on something
    prefetch mode would have caught before submit.
  * `--verify` can only check the table for an archive band -- the pixels are
    not there to re-hash. It says so rather than reporting a hollow pass.
  * IRSA sees one connection per running chunk job, unthrottled. See
    `pegasus.transfer.bypass.input.staging` in the Pegasus docs.
  * The ~1 GB MeerKAT template is downloaded either way: region.hdr and
    region.json come from mGetHdr/mExamine on it at generation time, and the
    overlay_template job needs the pixels. Archive mode removes the 2MASS raw
    data, not the template.

Must run inside the Montage container:

    apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py
    apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --mode prefetch
    apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --verify
    apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py --compare-tables
"""

import argparse
import ast
import contextlib
import datetime
import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.request
import warnings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("prefetch")

# mArchiveDownload queries IRSA without certificate verification and urllib3
# warns once per request, which buries the progress output. Not our call to
# make from here, so just stop it repeating.
warnings.filterwarnings("ignore", message=".*Unverified HTTPS request.*")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "bin"))

MEERKAT_URL = ("https://archive-gw-1.kat.ac.za/public/repository/"
               "10.48479/fyst-hj47/data/"
               "MeerKAT_Galactic_Centre_1284MHz-StokesI.fits")
MEERKAT_NAME = "MeerKAT_Galactic_Centre_1284MHz-StokesI.fits"

# Minimum plausible size for the MeerKAT mosaic; guards against saving an error
# page or a truncated transfer as if it were data.
MEERKAT_MIN_BYTES = 500 * 1024 * 1024


@contextlib.contextmanager
def in_dir(path):
    """Run a block with `path` as the working directory.

    Montage modules are given relative paths here, exactly as the notebook does
    (it chdir's into each work tree and passes "raw"), because some modules
    treat their directory arguments as relative to the process cwd.
    """
    previous = os.getcwd()
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)


def sha256_file(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(count):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return "%.1f %s" % (count, unit)
        count /= 1024.0


def download_with_resume(url, path, min_bytes=0, timeout=120):
    """Download `url` to `path`, resuming a partial file via a Range request."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    partial = path + ".part"
    have = os.path.getsize(partial) if os.path.exists(partial) else 0

    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", "bytes=%d-" % have)
        logger.info("Resuming from %s", human(have))

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code == 416 and have:
            # Range not satisfiable: the partial file is already complete.
            logger.info("Server reports nothing further to send; treating as complete")
            os.replace(partial, path)
            return path
        raise

    with response:
        if have and response.status != 206:
            logger.warning("Server ignored Range (status %s); restarting download",
                           response.status)
            have = 0
            mode = "wb"
        else:
            mode = "ab" if have else "wb"

        remaining = response.headers.get("Content-Length")
        total = (int(remaining) + have) if remaining else None
        logger.info("Downloading %s -> %s%s", url, path,
                    " (%s)" % human(total) if total else "")

        written = have
        next_report = written + (64 << 20)
        with open(partial, mode) as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if written >= next_report:
                    if total:
                        logger.info("  %s / %s (%.0f%%)", human(written),
                                    human(total), 100.0 * written / total)
                    else:
                        logger.info("  %s", human(written))
                    next_report = written + (64 << 20)

    size = os.path.getsize(partial)
    if size < min_bytes:
        raise RuntimeError("%s is only %s, expected at least %s -- the transfer "
                           "failed or the URL returned an error document"
                           % (partial, human(size), human(min_bytes)))
    os.replace(partial, path)
    logger.info("Downloaded %s (%s)", path, human(size))
    return path


def container_versions():
    """Versions recorded when the container was built, if we are inside it."""
    try:
        with open("/opt/versions.json") as handle:
            return json.load(handle)
    except OSError:
        return {}


def require_montage():
    """Both modules, for both modes: archive mode bootstraps via a download."""
    try:
        from MontagePy.main import mGetHdr, mExamine, mImgtbl              # noqa: F401
        from MontagePy.archive import mArchiveDownload, mArchiveList       # noqa: F401
    except ImportError as error:
        logger.error("MontagePy is not importable: %s", error)
        logger.error("Run this script inside the container:")
        logger.error("  apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py")
        sys.exit(1)


def check_status(result, what):
    """Validate a Montage return value, which is not always a dict.

    MontagePy.main modules return a real dict, {'status': '0', ...}. But
    MontagePy.archive.mArchiveDownload returns a *string* holding a dict repr,
    "{'status': '0', 'count': 479}". That inconsistency is also why the
    notebook's output looks like a dict for every module: it printed
    str(rtn) either way. Both forms are accepted here.
    """
    if isinstance(result, (bytes, str)):
        text = result.decode() if isinstance(result, bytes) else result
        try:
            parsed = ast.literal_eval(text.strip())
        except (ValueError, SyntaxError):
            raise RuntimeError("%s returned an unparseable value: %r"
                               % (what, result))
        if not isinstance(parsed, dict):
            raise RuntimeError("%s returned %r, not a status dict" % (what, result))
        result = parsed

    if not isinstance(result, dict):
        raise RuntimeError("%s returned %r, not a status dict" % (what, result))
    if str(result.get("status")) != "0":
        raise RuntimeError("%s failed: %s" % (what, result))
    logger.info("%-22s %s", what + ":", result)
    return result


def build_region(meerkat_path, data_dir):
    """Notebook cell 5: extract the template header and examine the geometry."""
    from MontagePy.main import mGetHdr, mExamine

    with in_dir(data_dir):
        result = check_status(mGetHdr(meerkat_path, "region.hdr"), "mGetHdr")
        ncard = result.get("ncard")

        info = mExamine(meerkat_path)
        check_status(info, "mExamine")

    size = max(info["ximgsize"], info["yimgsize"])
    ratio = 1000.0 / size
    # cv2.resize takes (width, height); the notebook's `shape` is built the same
    # way, so thumbnails produced here are directly comparable to the notebook's.
    shape = (int(info["ximgsize"] * ratio), int(info["yimgsize"] * ratio))

    region = {
        "location": "%s %s" % (info["lonc"], info["latc"]),
        "lon_centre_deg": info["lonc"],
        "lat_centre_deg": info["latc"],
        "size_deg": size,
        "ximgsize_deg": info["ximgsize"],
        "yimgsize_deg": info["yimgsize"],
        "thumbnail_width": shape[0],
        "thumbnail_height": shape[1],
        "header_ncard": ncard,
        "header_sha256": sha256_file(os.path.join(data_dir, "region.hdr")),
    }
    with open(os.path.join(data_dir, "region.json"), "w") as handle:
        json.dump(region, handle, indent=2)
        handle.write("\n")

    logger.info("Region: %s  size %.9f deg  thumbnail %dx%d",
                region["location"], region["size_deg"],
                region["thumbnail_width"], region["thumbnail_height"])
    return region


def band_is_current(entry, data_dir, verify=False):
    """True when a manifest band entry still matches what is on disk."""
    if not entry:
        return False
    table = os.path.join(data_dir, entry["table"])
    if not os.path.exists(table):
        logger.info("  table %s missing", entry["table"])
        return False

    # Archive-mode entries have no local pixels by design, so the table is the
    # only thing there is to check. Deliberately NOT re-querying IRSA to confirm
    # the URLs still resolve: that would make DAG generation depend on the
    # network, which is the one property both modes are supposed to keep.
    if entry.get("mode") == "archive":
        return True

    for record in entry["files"]:
        path = os.path.join(data_dir, entry["raw_dir"], record["name"])
        if not os.path.exists(path):
            logger.info("  %s missing", record["name"])
            return False
        if os.path.getsize(path) != record["bytes"]:
            logger.info("  %s wrong size", record["name"])
            return False
        if verify and sha256_file(path) != record["sha256"]:
            logger.error("  %s CHECKSUM MISMATCH", record["name"])
            return False
    return True


def fetch_band(survey, band, region, data_dir, force_download=False):
    """Notebook cell 9: download a band and build its sorted image table."""
    from MontagePy.main import mImgtbl
    from MontagePy.archive import mArchiveDownload
    import mtbl

    dataset = "%s %s" % (survey, band)
    raw_rel = os.path.join("raw", band)
    table_rel = os.path.join("tables", "rimages_%s.tbl" % band)

    with in_dir(data_dir):
        os.makedirs(raw_rel, exist_ok=True)
        os.makedirs("tables", exist_ok=True)

        # Adopt an existing download rather than repeating it. This is the
        # recovery path when a previous run fetched the images but died before
        # writing the manifest -- re-downloading ~800 MB to learn what is
        # already on disk would be wasteful. The count is recorded as
        # unverified so the manifest shows the images were not re-checked
        # against the archive; --force-band redoes the download properly.
        existing = [n for n in os.listdir(raw_rel) if n.endswith(".fits")]
        if existing and not force_download:
            logger.warning("Band %s already has %d FITS files in %s -- adopting "
                           "them instead of downloading again "
                           "(use --force-band %s to re-download)",
                           band, len(existing), raw_rel, band)
            downloaded = {"status": "0", "count": len(existing),
                          "unverified_adopted": True}
        else:
            logger.info("mArchiveDownload %r into %s", dataset, raw_rel)
            downloaded = check_status(
                mArchiveDownload(dataset, region["location"], region["size_deg"],
                                 raw_rel),
                "mArchiveDownload")

        tabled = check_status(mImgtbl(raw_rel, table_rel), "mImgtbl")

        # Deterministic row order: mImgtbl emits directory-listing order, and
        # mBgModel's global fit is order-sensitive. See bin/mtbl.py.
        rows = mtbl.sort_in_place(table_rel)
        logger.info("Sorted %s by filename (%d rows)", table_rel, rows)

        names = sorted(os.listdir(raw_rel))
        files = [{"name": name,
                  "bytes": os.path.getsize(os.path.join(raw_rel, name)),
                  "sha256": sha256_file(os.path.join(raw_rel, name))}
                 for name in names]
        table_sha = sha256_file(table_rel)

    total = sum(record["bytes"] for record in files)
    logger.info("Band %s: %d files, %s, %d table rows",
                band, len(files), human(total), rows)

    if tabled.get("badfits") or tabled.get("badwcs"):
        logger.warning("Band %s: mImgtbl reported badfits=%s badwcs=%s",
                       band, tabled.get("badfits"), tabled.get("badwcs"))

    return {
        "mode": "prefetch",
        "dataset": dataset,
        "raw_dir": raw_rel,
        "table": table_rel,
        "table_sha256": table_sha,
        "downloaded_count": downloaded.get("count"),
        "table_rows": rows,
        "badfits": tabled.get("badfits"),
        "badwcs": tabled.get("badwcs"),
        "total_bytes": total,
        "files": files,
    }


# --------------------------------------------------------------- archive mode

def archive_urls(survey, band, region, data_dir, out_rel):
    """Query the archive for a band's name -> download URL map. No pixels.

    WHAT MontagePy's mArchiveList ACTUALLY RETURNS, because it is not what the
    command-line tool of the same name returns and the difference decides the
    whole shape of archive mode: the binding writes JSON,

        [{"file": "2mass-atlas-000908s-j0150009.fits",
          "url": "http://irsa.ipac.caltech.edu/ibe/data/.../ji0150009.fits.gz"},
         ...]

    -- two fields, no WCS. The CLI writes a full `fitshdr` IPAC table with
    cntr/ra/dec/crpix/crval/cdelt/crota2/naxis, which is what montage-workflow-v3
    feeds to mDAGTbls. This container is a pip-only MontagePy 2.2.0 install with
    no Montage executables, so that route is unavailable: there is NO way here to
    obtain image WCS without the pixels, and mProjExec needs the WCS.

    Hence archive mode fetches pixels at run time but still needs the band's
    image table, which only mImgtbl (reading real FITS headers) can produce. See
    list_band.

    Signature note: mArchiveList(dataset, location, size, output) -- the same
    shape as mArchiveDownload, NOT the CLI's
    (survey band centre width height output).
    """
    from MontagePy.archive import mArchiveList

    dataset = "%s %s" % (survey, band)

    with in_dir(data_dir):
        os.makedirs(os.path.dirname(out_rel) or ".", exist_ok=True)
        logger.info("mArchiveList %r at %s, %.9f deg -> %s",
                    dataset, region["location"], region["size_deg"], out_rel)
        # region["size_deg"] verbatim: the same extent mArchiveDownload is given
        # in prefetch mode, so the two modes select the same images. (v3 inflates
        # its datafind box by 1.42x; matching that would break the join below.)
        listed = check_status(
            mArchiveList(dataset, region["location"], region["size_deg"],
                         out_rel),
            "mArchiveList")

        with open(out_rel) as handle:
            payload = json.load(handle)

    if not isinstance(payload, list):
        raise SystemExit("mArchiveList wrote %s, expected a JSON list of "
                         "{file, url} objects" % type(payload).__name__)

    urls = {}
    for record in payload:
        name = record.get("file")
        url = record.get("url")
        if not name or not url:
            raise SystemExit("mArchiveList entry %r has no file/url pair"
                             % (record,))
        urls[os.path.basename(name)] = url

    logger.info("Band %s: archive lists %d images (%d unique names)",
                band, len(payload), len(urls))
    return listed, urls


def band_table_rel(band, manifest):
    """Where a band's image table lives, per the manifest or by convention."""
    entry = (manifest.get("bands") or {}).get(band) or {}
    return entry.get("table") or os.path.join("tables", "rimages_%s.tbl" % band)


def has_band_table(band, data_dir, manifest):
    """True when the band's mImgtbl table is already on disk.

    Archive mode's bootstrap decision hinges on this, so it answers rather than
    raising: a missing table is a thing to fix by downloading the band once, not
    an error.
    """
    return os.path.exists(os.path.join(data_dir,
                                       band_table_rel(band, manifest)))


def band_table(band, data_dir, manifest):
    """The band's image table -- the one thing archive mode cannot do without.

    mProjExec needs per-image WCS, and in this container only mImgtbl can produce
    it, by reading real FITS headers off disk (see archive_urls). So archive mode
    reuses the table a download already built: 263 KB per band, against the
    ~800 MB of pixels it lets us stop storing.

    The upside of reusing it rather than deriving a new one is that archive mode
    then differs from prefetch mode in exactly one respect -- where the pixels
    come from. Same table, same order, same chunk boundaries, so a bit-identical
    result is a reasonable expectation rather than a hope.

    Reaching the raise means the caller skipped the bootstrap in main(); it is a
    guard, not the normal path for a missing table.
    """
    table_rel = band_table_rel(band, manifest)
    if not os.path.exists(os.path.join(data_dir, table_rel)):
        raise SystemExit(
            "band %s has no image table at %s.\n\n"
            "Archive mode still needs one: mProjExec requires per-image WCS, and "
            "MontagePy's mArchiveList returns only {file, url} -- no WCS. Only "
            "mImgtbl can build the table, and it reads the FITS headers, so the "
            "band has to have been downloaded once:\n"
            "    bin/prefetch.py --bands %s\n"
            "The table is small enough to keep (263 KB) after deleting the raw "
            "images, and can be carried between machines on its own."
            % (band, table_rel, band))
    return table_rel


def list_band(survey, band, region, data_dir, manifest, checksums=None):
    """Archive-mode analogue of fetch_band: keep the table, PFN the pixels.

    `checksums` maps image name -> sha256 from an earlier prefetch-mode manifest.
    Carrying them into the URL replicas is what lets Pegasus integrity-check the
    downloads; without them the replicas go in unchecksummed.
    """
    import mtbl

    table_rel = band_table(band, data_dir, manifest)
    listed, urls = archive_urls(survey, band, region, data_dir,
                                os.path.join("tables", "aurls_%s.json" % band))

    table = mtbl.read(os.path.join(data_dir, table_rel))
    names = [os.path.basename(n) for n in table.filenames()]

    # Every image the table names must have a URL, or its chunk job will fail at
    # stage-in. Fail here instead: a missing replica is a generation-time error,
    # not something to discover on a worker node.
    missing = [name for name in names if name not in urls]
    if missing:
        raise SystemExit(
            "%d of %d images in %s are absent from the archive listing, e.g. "
            "%s.\nThe listing and the table disagree, so archive mode would "
            "produce chunk jobs with unresolvable inputs. Use prefetch mode for "
            "band %s."
            % (len(missing), len(names), table_rel, ", ".join(missing[:5]), band))

    files = []
    for name in names:
        record = {"name": name, "url": urls[name]}
        if checksums and name in checksums:
            record["sha256"] = checksums[name]
        files.append(record)

    extra = len(urls) - len(names)
    carried = sum(1 for record in files if "sha256" in record)
    logger.info("Band %s: %d images from %s, all resolved to URLs%s; "
                "%d checksums carried over", band, len(files), table_rel,
                " (%d listed images unused)" % extra if extra > 0 else "",
                carried)
    if not carried:
        logger.warning("Band %s: no checksums available, so Pegasus cannot "
                       "integrity-check these downloads", band)

    with in_dir(data_dir):
        table_sha = sha256_file(table_rel)

    return {
        "mode": "archive",
        "dataset": "%s %s" % (survey, band),
        "raw_dir": None,
        "table": table_rel,
        "table_sha256": table_sha,
        "listed_count": listed.get("count") or len(urls),
        "table_rows": len(names),
        "total_bytes": 0,
        "files": files,
    }


def compare_tables(survey, bands, region, data_dir, manifest):
    """Check that the archive listing covers every image in each band's table.

    This is the gate on archive mode. The table is shared between the two modes,
    so row order is not in question -- what is, is whether the archive still
    offers a URL for every image the table names. A miss means a chunk job with
    an unresolvable input.

    Non-destructive: writes tables/compare_<BAND>.json and touches no manifest.
    """
    import mtbl

    ok = True
    for band in bands:
        try:
            table_rel = band_table(band, data_dir, manifest)
        except SystemExit as error:
            logger.error("band %s: %s", band, str(error).split("\n")[0])
            ok = False
            continue

        _, urls = archive_urls(survey, band, region, data_dir,
                               os.path.join("tables", "compare_%s.json" % band))
        table = mtbl.read(os.path.join(data_dir, table_rel))
        names = [os.path.basename(n) for n in table.filenames()]

        missing = [name for name in names if name not in urls]
        unused = [name for name in urls if name not in set(names)]

        logger.info("band %s: table %d images, archive listing %d images",
                    band, len(names), len(urls))
        if missing:
            logger.error("  %d table images have NO archive URL: %s",
                         len(missing), ", ".join(missing[:5]))
            ok = False
        else:
            logger.info("  all %d table images resolve to a URL", len(names))
        if unused:
            # Harmless -- the listing covers a slightly wider box than the table
            # kept -- but worth seeing, since a large number means the two are
            # describing different regions.
            logger.info("  %d listed images are not in the table (ignored)",
                        len(unused))

    logger.info("Archive coverage %s", "passed" if ok else "FAILED")
    if ok:
        logger.info("Archive mode is safe for these bands: every image the "
                    "chunk tables name can be fetched at run time.")
    else:
        logger.info("Do not use archive mode for the failing bands: their chunk "
                    "jobs would have inputs Pegasus cannot resolve.")
    return ok


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=os.path.join(PROJECT_DIR, "data"),
                        help="where inputs are stored (default: <project>/data)")
    parser.add_argument("--survey", default="2MASS",
                        help="archive survey name (default: 2MASS)")
    parser.add_argument("--bands", nargs="+", default=["J", "H", "K"],
                        help="bands to fetch (default: J H K)")
    parser.add_argument("--template-url", default=MEERKAT_URL,
                        help="URL of the template/overlay image")
    parser.add_argument("--template-path", default=None,
                        help="use an already-downloaded template instead of "
                             "downloading (symlinked into data/meerkat/)")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch everything, ignoring what is on disk")
    parser.add_argument("--force-band", action="append", default=[],
                        help="re-fetch one band (repeatable)")
    parser.add_argument("--verify", action="store_true",
                        help="re-hash every input against the manifest and exit "
                             "non-zero on any mismatch; downloads nothing")
    # --mode must be registered before its aliases: argparse takes the default
    # for a dest from the FIRST action declaring it, and the store_const aliases
    # below carry an implicit default of None.
    parser.add_argument("--mode", choices=["archive", "prefetch"],
                        default="archive",
                        help="where chunk jobs get their raw pixels. 'archive' "
                             "(default) records an IRSA URL per image so each "
                             "job pulls its own ~32 at run time; 'prefetch' "
                             "keeps a checksummed local copy and registers "
                             "file:// replicas. A band with no image table is "
                             "downloaded once either way -- only mImgtbl can "
                             "produce the WCS mProjExec needs")
    parser.add_argument("--list-only", dest="mode", action="store_const",
                        const="archive",
                        help="deprecated alias for --mode archive (now default)")
    parser.add_argument("--prefetch", dest="mode", action="store_const",
                        const="prefetch", help="alias for --mode prefetch")
    parser.add_argument("--compare-tables", action="store_true",
                        help="check an mArchiveList query against the existing "
                             "prefetch tables, row for row, and exit. The gate "
                             "on archive mode; changes nothing")
    args = parser.parse_args()

    if args.compare_tables and args.verify:
        parser.error("--compare-tables runs on its own")

    require_montage()

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    manifest_path = os.path.join(data_dir, "manifest.json")

    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as handle:
            manifest = json.load(handle)

    # ------------------------------------------------------------- verify only
    if args.verify:
        if not manifest:
            logger.error("No manifest at %s -- nothing to verify", manifest_path)
            sys.exit(1)
        logger.info("Verifying inputs against %s", manifest_path)
        ok = True

        template = manifest.get("template", {})
        path = os.path.join(data_dir, template.get("path", ""))
        if not os.path.exists(path):
            logger.error("template missing: %s", path)
            ok = False
        elif sha256_file(path) != template.get("sha256"):
            logger.error("template CHECKSUM MISMATCH: %s", path)
            ok = False
        else:
            logger.info("template OK (%s)", human(template["bytes"]))

        for band, entry in sorted(manifest.get("bands", {}).items()):
            if not band_is_current(entry, data_dir, verify=True):
                logger.error("band %s FAILED verification", band)
                ok = False
            elif entry.get("mode") == "archive":
                # Say this plainly rather than reporting a pass that checked
                # nothing but a table: the images live at IRSA and will not be
                # seen until the workflow runs.
                logger.warning("band %s: archive mode -- table OK (%d rows), but "
                               "the %d images were NOT checked; they are fetched "
                               "at run time", band, entry["table_rows"],
                               len(entry["files"]))
            else:
                logger.info("band %s OK (%d files, %s)", band,
                            len(entry["files"]), human(entry["total_bytes"]))

        logger.info("Verification %s", "passed" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------ compare tables only
    if args.compare_tables:
        region_path = os.path.join(data_dir, "region.json")
        if not os.path.exists(region_path):
            logger.error("No %s -- run prefetch mode first so there is a "
                         "baseline to compare against", region_path)
            sys.exit(1)
        with open(region_path) as handle:
            region = json.load(handle)
        ok = compare_tables(args.survey, args.bands, region, data_dir, manifest)
        sys.exit(0 if ok else 1)

    # -------------------------------------------------------------- template
    meerkat_dir = os.path.join(data_dir, "meerkat")
    meerkat_path = os.path.join(meerkat_dir, MEERKAT_NAME)
    template_entry = manifest.get("template", {})

    if args.template_path:
        source = os.path.abspath(args.template_path)
        if not os.path.exists(source):
            logger.error("--template-path does not exist: %s", source)
            sys.exit(1)
        os.makedirs(meerkat_dir, exist_ok=True)
        if os.path.realpath(source) != os.path.realpath(meerkat_path):
            if os.path.lexists(meerkat_path):
                os.remove(meerkat_path)
            os.symlink(source, meerkat_path)
        logger.info("Using existing template %s", source)
    elif args.force or not os.path.exists(meerkat_path) or \
            os.path.getsize(meerkat_path) < MEERKAT_MIN_BYTES:
        download_with_resume(args.template_url, meerkat_path,
                             min_bytes=MEERKAT_MIN_BYTES)
    else:
        logger.info("Template already present: %s (%s)", meerkat_path,
                    human(os.path.getsize(meerkat_path)))

    size = os.path.getsize(meerkat_path)
    if template_entry.get("bytes") == size and template_entry.get("sha256") \
            and not args.force:
        template_sha = template_entry["sha256"]
        logger.info("Reusing recorded template checksum")
    else:
        logger.info("Hashing template (%s)", human(size))
        template_sha = sha256_file(meerkat_path)

    # -------------------------------------------------------------- region.hdr
    region_path = os.path.join(data_dir, "region.json")
    hdr_path = os.path.join(data_dir, "region.hdr")
    if args.force or not (os.path.exists(region_path) and os.path.exists(hdr_path)):
        region = build_region(meerkat_path, data_dir)
    else:
        with open(region_path) as handle:
            region = json.load(handle)
        logger.info("Reusing region.hdr / region.json (%s, size %.9f deg)",
                    region["location"], region["size_deg"])

    # ------------------------------------------------------------------ bands
    bands = manifest.get("bands", {})
    for band in args.bands:
        entry = bands.get(band)
        forced = args.force or band in args.force_band
        wanted_mode = args.mode

        # A mode change always redoes the band: the manifest entry shapes differ
        # (raw_dir + checksums vs urls), and silently keeping the old one would
        # hand the generator the wrong kind of replica.
        if entry and entry.get("mode", "prefetch") != wanted_mode:
            logger.info("Band %s switching %s -> %s", band,
                        entry.get("mode", "prefetch"), wanted_mode)
        elif not forced and band_is_current(entry, data_dir):
            logger.info("Band %s already complete in %s mode (%d images) -- "
                        "skipping", band, wanted_mode, len(entry["files"]))
            continue

        if wanted_mode == "archive":
            # Bootstrap. Archive mode cannot invent the band's WCS table -- see
            # archive_urls -- so a band without one is downloaded exactly as
            # prefetch mode would, which also gives us the SHA-256s that make the
            # URL replicas integrity-checkable. Costs one download, once.
            if not has_band_table(band, data_dir, manifest):
                logger.info("=== Band %s: no image table yet, bootstrapping "
                            "with a one-time download ===", band)
                entry = bands[band] = fetch_band(args.survey, band, region,
                                                 data_dir,
                                                 force_download=forced)

            logger.info("=== Listing band %s (no download) ===", band)
            # Checksums come from a prefetch-shaped entry -- either one already in
            # the manifest or the bootstrap just above. Carrying them into the URL
            # replicas is what lets Pegasus verify each run-time download.
            previous = entry if entry and entry.get("mode") != "archive" else None
            checksums = {record["name"]: record["sha256"]
                         for record in (previous or {}).get("files", [])
                         if record.get("sha256")}
            bands[band] = list_band(args.survey, band, region, data_dir,
                                    manifest, checksums=checksums)
        else:
            logger.info("=== Fetching band %s ===", band)
            bands[band] = fetch_band(args.survey, band, region, data_dir,
                                     force_download=forced)

    # --------------------------------------------------------------- manifest
    manifest = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "survey": args.survey,
        "bands_requested": args.bands,
        "container_versions": container_versions(),
        "template": {
            "path": os.path.relpath(meerkat_path, data_dir),
            "url": args.template_url if not args.template_path else None,
            "local_source": args.template_path,
            "bytes": size,
            "sha256": template_sha,
        },
        "region": region,
        "region_hdr": {
            "path": "region.hdr",
            "sha256": sha256_file(hdr_path),
        },
        "bands": bands,
    }
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    total = size + sum(entry.get("total_bytes") or 0 for entry in bands.values())
    archive_bands = sorted(b for b, e in bands.items()
                           if e.get("mode") == "archive")
    logger.info("Wrote %s", manifest_path)
    logger.info("Inputs on disk: %s across %d bands + template",
                human(total), len(bands))

    if archive_bands:
        listed = sum(len(bands[b]["files"]) for b in archive_bands)
        logger.warning("Archive mode for band(s) %s: the chunk jobs will "
                       "download %d images from IRSA on every run.",
                       " ".join(archive_bands), listed)
        logger.warning("Confirm the image set before submitting:  "
                       "bin/prefetch.py --compare-tables")

        # A bootstrap leaves the pixels behind, so say what is reclaimable
        # rather than claiming nothing is on disk. Deliberately NOT deleting
        # them: they are the only local copy, and prefetch mode wants them.
        stale = []
        for band in archive_bands:
            raw = os.path.join(data_dir, "raw", band)
            if os.path.isdir(raw):
                size = sum(os.path.getsize(os.path.join(raw, n))
                           for n in os.listdir(raw)
                           if os.path.isfile(os.path.join(raw, n)))
                if size:
                    stale.append((raw, size))
        if stale:
            logger.info("%s of raw pixels remain on disk and are no longer "
                        "referenced by the DAG: %s. The image tables are what "
                        "archive mode needs; the pixels can be deleted, or kept "
                        "so --mode prefetch stays available.",
                        human(sum(size for _, size in stale)),
                        ", ".join(path for path, _ in stale))

    if len(archive_bands) < len(bands):
        logger.info("The DAG can be generated repeatedly with no network access "
                    "for the prefetch-mode bands.")


if __name__ == "__main__":
    main()
