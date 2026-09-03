#!/usr/bin/env python3
"""Shared helpers for the workflow's job wrappers.

This is a support module, not a transformation: it is registered in the Replica
Catalog and added as an input to every job, so it arrives in the job's working
directory and is imported with

    sys.path.insert(0, os.getcwd())
    import mjob

Never import it by a path relative to __file__ -- Pegasus stages the wrapper
script and its inputs independently, so __file__ says nothing about where the
support files landed.
"""

import json
import logging
import os
import subprocess
import sys
import tarfile
import time

logger = logging.getLogger("mjob")


def setup_logging(name):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - " + name + " - %(levelname)s - %(message)s")
    return logging.getLogger(name)


class MontageError(RuntimeError):
    """A Montage module returned a non-zero status."""


def check(result, what, log=logger):
    """Validate a Montage return dict.

    Every MontagePy module returns {'status': '0', ...} on success. A non-zero
    status is a real failure and must fail the job; per-item attrition inside
    the *Exec wrappers (failed/nooverlap/diff_failed/fit_failed counts) is
    normal and is recorded rather than raised -- see SPEC.md section 6.1.
    """
    if isinstance(result, (bytes, str)):
        # Not all of Montage's Python bindings agree on this: MontagePy.main
        # modules return a real dict, but MontagePy.archive.mArchiveDownload
        # returns a *string* holding a dict repr. Accept both rather than
        # discovering the difference mid-DAG.
        import ast
        text = result.decode() if isinstance(result, bytes) else result
        try:
            result = ast.literal_eval(text.strip())
        except (ValueError, SyntaxError):
            raise MontageError("%s returned an unparseable value: %r"
                               % (what, result))

    if not isinstance(result, dict):
        raise MontageError("%s returned %r, not a status dict" % (what, result))
    if str(result.get("status")) != "0":
        raise MontageError("%s failed: %s" % (what, result))
    log.info("%-18s %s", what + ":", _printable(result))
    return result


def _printable(result):
    """Compact rendering of a Montage return dict, bytes decoded."""
    items = []
    for key, value in result.items():
        if isinstance(value, bytes):
            value = value.decode()
        items.append("%s=%s" % (key, value))
    return " ".join(items)


def jsonable(value):
    """Make a Montage return dict JSON-serialisable (mViewer returns bytes)."""
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


class Status:
    """Accumulates a job's Montage return dicts into a status JSON file.

    These files are the workflow's Tier 1 evidence: the notebook's reference
    values ARE the Montage return dictionaries, so capturing them verbatim is
    what makes the comparison in bin/validate.py possible.
    """

    def __init__(self, step, log=logger, **fields):
        self.data = {"step": step, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.data.update(jsonable(fields))
        self.log = log
        self._start = time.time()

    def record(self, name, result):
        self.data.setdefault("montage", {})[name] = jsonable(result)
        return result

    def run(self, name, callable_, *args, **kwargs):
        """Call a Montage module, validate its status, and record the result."""
        started = time.time()
        result = callable_(*args, **kwargs)
        check(result, name, self.log)
        self.record(name, result)
        self.data.setdefault("seconds", {})[name] = round(time.time() - started, 2)
        return result

    def set(self, key, value):
        self.data[key] = jsonable(value)

    def write(self, path):
        self.data["elapsed_seconds"] = round(time.time() - self._start, 2)
        self.data["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(self.data, handle, indent=2)
            handle.write("\n")
        self.log.info("Wrote status %s (%.1f s elapsed)", path,
                      self.data["elapsed_seconds"])
        return path


# --------------------------------------------------------------- filesystem

def ensure_dirs(*paths):
    for path in paths:
        os.makedirs(path, exist_ok=True)
    return paths


def make_tar(tar_path, source_dir, log=logger):
    """Archive `source_dir`'s contents (not the directory itself) into tar_path.

    Chunk jobs ship their projected images this way: one deterministic declared
    output that exists regardless of how many images mProjExec managed to
    produce. See SPEC.md section 4.3.
    """
    names = sorted(os.listdir(source_dir)) if os.path.isdir(source_dir) else []
    with tarfile.open(tar_path, "w") as archive:
        for name in names:
            archive.add(os.path.join(source_dir, name), arcname=name)
    size = os.path.getsize(tar_path)
    log.info("Archived %d files from %s/ into %s (%.1f MB)",
             len(names), source_dir, tar_path, size / 1e6)
    return len(names), size


def extract_tars(tar_paths, dest_dir, log=logger):
    """Unpack chunk archives into a single directory, as Montage expects."""
    os.makedirs(dest_dir, exist_ok=True)
    total = 0
    for tar_path in tar_paths:
        if not os.path.exists(tar_path):
            raise FileNotFoundError("chunk archive missing: %s" % tar_path)
        with tarfile.open(tar_path, "r") as archive:
            members = archive.getmembers()
            # Refuse absolute or parent-relative member names.
            for member in members:
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise ValueError("unsafe member %r in %s" % (member.name, tar_path))
            archive.extractall(dest_dir)
            total += len(members)
    files = len([n for n in os.listdir(dest_dir)])
    log.info("Extracted %d members from %d archives into %s/ (%d files present)",
             total, len(tar_paths), dest_dir, files)
    return files


def count_fits(directory, suffix=".fits", exclude_area=True):
    """Count FITS files in a directory, optionally ignoring Montage area planes."""
    if not os.path.isdir(directory):
        return 0
    names = [n for n in os.listdir(directory) if n.endswith(suffix)]
    if exclude_area:
        names = [n for n in names if not n.endswith("_area" + suffix)]
    return len(names)


def disk_free_gb(path="."):
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize / 1e9


# ------------------------------------------------------------------- images

def load_region(path):
    """Read region.json, produced by bin/prefetch.py from mExamine."""
    with open(path) as handle:
        return json.load(handle)


def thumbnail(source_png, dest_png, width, height, log=logger):
    """Shrink a full-resolution PNG to the notebook's thumbnail geometry.

    cv2.resize takes (width, height); region.json stores the notebook's own
    `shape` tuple computed the same way, so the result is directly comparable
    to the thumbnails extracted from the notebook (Tier 2).
    """
    import cv2

    image = cv2.imread(source_png)
    if image is None:
        raise RuntimeError("could not read %s (mViewer produced nothing?)" % source_png)
    log.info("%s is %dx%d, resizing to %dx%d",
             source_png, image.shape[1], image.shape[0], width, height)
    resized = cv2.resize(image, (width, height))
    if not cv2.imwrite(dest_png, resized):
        raise RuntimeError("could not write %s" % dest_png)
    log.info("Wrote %s (%.1f KB)", dest_png, os.path.getsize(dest_png) / 1024)
    return dest_png


def stretch(source_png, dest_png, contrast, brightness, log=logger):
    """The notebook's cv.addWeighted(img, contrast, img, 0, brightness) stretch."""
    import cv2

    image = cv2.imread(source_png)
    if image is None:
        raise RuntimeError("could not read %s" % source_png)
    stretched = cv2.addWeighted(image, contrast, image, 0, brightness)
    if not cv2.imwrite(dest_png, stretched):
        raise RuntimeError("could not write %s" % dest_png)
    log.info("Stretched %s -> %s (contrast=%s brightness=%s)",
             source_png, dest_png, contrast, brightness)
    return dest_png


def require_files(*paths):
    """Fail with a clear message if an expected output is missing."""
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError("expected output(s) not produced: %s" % ", ".join(missing))
    return paths
