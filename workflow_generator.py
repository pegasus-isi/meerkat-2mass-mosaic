#!/usr/bin/env python3
"""Pegasus workflow generator for the MeerKAT / 2MASS mosaic.

Reproduces $HOME/work/workflows/MeerKAT.ipynb as a Pegasus DAG. See SPEC.md for
the design; the short version:

  * Data acquisition is NOT in the DAG. bin/prefetch.py records every input in
    data/manifest.json; this generator builds the Replica Catalog from that
    manifest and never scans a directory, so DAG generation itself never touches
    the network in either fetch mode.

    Which mode the manifest was written in decides where the raw 2MASS pixels
    come from. Archive mode (bin/prefetch.py's default) holds an IRSA URL per
    image, and those go into the Replica Catalog as remote replicas with input
    staging bypassed per input, so each project_chunk job downloads its own ~32
    images on the worker node. Prefetch mode (--mode prefetch) holds a local
    path per image and registers file:// replicas instead.

    Nothing else about the DAG changes between the two -- in particular the job
    count does not, because chunking is driven by the image *table*, which is
    metadata either way. See bin/prefetch.py's docstring for the trade-offs, and
    DETERMINISM.md for why the reproducibility evidence is all prefetch-mode.

  * Reprojection -- ~85% of per-band compute -- is chunked: the sorted image
    table is split here, at generation time, and one mProjExec job runs per
    chunk. Everything downstream of it operates on the same working set and is
    one job per band.

  * Each chunk's projected images travel as a single tar archive, because
    mProjExec names its own outputs and some inputs legitimately produce none.

Usage:
    ./workflow_generator.py                          # all three bands
    ./workflow_generator.py --subset 20 --bands J    # fast smoke test
    ./workflow_generator.py -e condorpool            # plain HTCondor pool, no site catalog
    ./workflow_generator.py -s unity.yml             # hosted site catalog

Sites follow pegasus-isi/pegasus-gromacs: jobs run on a site named "compute",
defined by a centrally hosted site catalog (-s FILE, or one in ~/.pegasusrc).
The generator writes the workflow and catalogs but no site catalog, and never
plans or submits: it prints the pegasus-plan command. MeerKAT-Mosaic-Workflow.ipynb
drives the same class interactively.
"""

import argparse
import json
import shutil
import logging
import inspect
import os
import re
import sys
from pathlib import Path

from Pegasus.api import (Container, Directory, File, FileServer, Job, Operation,
                         PegasusClientError, Properties, ReplicaCatalog, Site,
                         SiteCatalog, Transformation, TransformationCatalog,
                         Workflow)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))
import mtbl  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("generator")

# Per-step resources.
#
# THESE ARE MEASURED, NOT ESTIMATED, AND THE CEILING IS REAL. The Condor pool
# here advertises slots of 4 CPUs / 6144 MB (x3) and 2 CPUs / 2048 MB (x1), so
# any request above 6 GB matches no slot and the job stays idle forever rather
# than failing visibly. An earlier estimate-based version of this table asked
# for 8-12 GB for mosaic/render/composite/overlay_template; all four would have
# hung. Check `condor_status -af Name Cpus Memory` before raising anything here.
#
# Peak RSS measured in-container on the real data (8-image chunk, full-size
# 10121x10527 mosaic):
#
#   project_chunk     0.09 GB    mProjExec works one image at a time
#   fitplanes         0.35 GB    pairwise diffs; mBgModel holds only the fit table
#   mosaic            0.10 GB    mAdd streams by row -- it never holds a full plane
#   render            2.31 GB    mViewer does hold full planes, plus the decoded PNG
#   composite         2.31 GB    same; mViewer processes the three channels in turn
#   overlay_template  2.31 GB    same, on the 852 MB template
#   blend             1.61 GB    two decoded PNGs plus the output
#
# Requests below carry roughly 2x headroom over measurement while staying under
# the 6 GB slot ceiling. Cores stay at 1 except fitplanes, whose *Exec loops
# can use a second.
TOOL_CONFIGS = {
    "project_chunk":    {"memory": "2 GB", "cores": 1},
    "fitplanes":        {"memory": "4 GB", "cores": 2},
    "mosaic":           {"memory": "2 GB", "cores": 1},
    "render":           {"memory": "5 GB", "cores": 1},
    "composite":        {"memory": "5 GB", "cores": 1},
    "overlay_template": {"memory": "5 GB", "cores": 1},
    "blend":            {"memory": "4 GB", "cores": 1},
    "validate":         {"memory": "4 GB", "cores": 1},
}

# Support modules staged to every job (Replica Catalog, not Transformation
# Catalog -- they are imported by wrappers, not invoked by Pegasus).
SUPPORT_FILES = ["mjob.py", "mtbl.py"]

# Replica Catalog site handle for images that live at the archive rather than on
# disk (bin/prefetch.py's default mode). Deliberately NOT declared in the site
# catalog: the PFN is a fully-qualified https:// URL that pegasus-transfer
# handles on its own, so the handle is only a label marking the replica as
# non-local. montage-workflow-v3 has shipped this pattern for years using the
# handle 'ipac' and no site catalog at all.
ARCHIVE_SITE = "irsa"

# Bypassing the staging site is what makes archive mode worth doing: without it
# Pegasus first pulls every image to the submit host and then ships it out via
# condorio, which is the bottleneck prefetch mode already has plus a re-download.
#
# BYPASS MUST BE PER-INPUT, AND THE GLOBAL PROPERTY IS NOT AN ACCEPTABLE
# FALLBACK. Setting pegasus.transfer.bypass.input.staging=true bypasses
# *everything*, including the stageable wrappers in bin/, which are declared with
# a submit-host pfn. PegasusLite then tries to fetch bin/project_chunk.py from
# /home/.../bin/project_chunk.py on the worker node and fails with "Expected
# local file does not exist" after three attempts and ~450 s of backoff -- the
# wrapper never runs, so it writes no status.json, and the job surfaces as a
# stage-OUT failure that says nothing about the real cause. Observed on
# 2026-09-02: chunk c01 happened to land on a worker sharing the submit host's
# filesystem and passed, c00 landed elsewhere and failed, so the global flag also
# makes correctness depend on placement.
#
# Pegasus 5.1.2 puts per-input bypass on Job.add_inputs(bypass_staging=True) --
# NOT on ReplicaCatalog.add_replica, which has no such parameter. Probed rather
# than assumed, since it has moved across releases.
_JOB_BYPASS_PARAM = "bypass_staging" in inspect.signature(Job.add_inputs).parameters


def normalise_bands(values):
    """Flatten --bands into individual band names.

    argparse's nargs="+" yields a list when invoked from a shell, but a web form
    -- Pegasus AI Studio's generated launcher, for one -- sends the whole field
    as a SINGLE argument: "J H K" or "J,H,K". Both then arrive as one band named
    "J H K", which fails downstream as "manifest has no data for band(s) J H K"
    and reads like a missing download rather than a quoting problem.

    Splitting on commas and whitespace makes every spelling equivalent:
        --bands J H K     --bands "J H K"     --bands J,H,K     --bands "J, H, K"
    """
    bands = []
    for value in values:
        for part in re.split(r"[,\s]+", str(value).strip()):
            if part and part not in bands:
                bands.append(part)
    return bands


class MeerKATMosaicWorkflow:

    def __init__(self, args):
        self.args = args
        self.wf_dir = str(Path(__file__).parent.resolve())
        self.data_dir = os.path.join(self.wf_dir, "data")

        # Generated catalogs live under a directory named after the output DAG,
        # never a shared one.
        #
        # This matters more than it looks. Chunk tables are referenced by LFN
        # ("rimages_J_c01.tbl") but resolved to a path on disk, and Pegasus
        # stages them when each job runs -- not when the DAG is planned. With a
        # shared catalogs/chunks/ directory, generating a second workflow
        # rewrites the first one's tables underneath it: a queued job then
        # stages a table listing images that were never registered as its
        # inputs, and the chunk dies with "N image(s) named in ... were not
        # staged". Observed exactly that with a --subset 16 --proj-chunk-size 8
        # run whose tables were clobbered by a subsequent full-band generation.
        stem = os.path.splitext(os.path.basename(args.output))[0]
        self.catalog_dir = os.path.join(self.wf_dir, "catalogs", stem)
        self.chunk_dir = os.path.join(self.catalog_dir, "chunks")
        self.shared_scratch_dir = os.path.join(self.wf_dir, "scratch")
        self.local_storage_dir = os.path.join(self.wf_dir, "output")
        self.wf_name = "meerkat_2mass_mosaic"

        self.wf = self.sc = self.tc = self.rc = self.props = None
        self.files = {}          # LFN -> File, so producers and consumers share objects
        self.manifest = None
        self.region = None
        self.chunks = {}         # band -> [(chunk_id, table_path, [image names])]
        self.archive_bands = []  # bands whose raw images are fetched at run time

        # Products this run creates, tracked as they are declared. The validate
        # job needs them, and they CANNOT be recovered by filtering self.files
        # on extension: raw 2MASS images and the template are also ".fits", so
        # an extension filter would stage ~1.7 GB of inputs into the comparison
        # job and compare the wrong things.
        self.products = {"mosaics": [], "thumbnails": [], "statuses": [],
                         "tables": []}

    # ------------------------------------------------------------------ inputs

    def load_manifest(self):
        path = os.path.join(self.data_dir, "manifest.json")
        if not os.path.exists(path):
            raise SystemExit(
                "No %s.\n\nRun the pre-workflow fetch first:\n"
                "    apptainer exec Apptainer/Montage_Container.sif bin/prefetch.py"
                % path)
        with open(path) as handle:
            self.manifest = json.load(handle)
        self.region = self.manifest["region"]

        missing = [b for b in self.args.bands if b not in self.manifest["bands"]]
        if missing:
            raise SystemExit(
                "manifest has no data for band(s) %s (it has %s). Fetch them:\n"
                "    apptainer exec Apptainer/Montage_Container.sif "
                "bin/prefetch.py --bands %s"
                % (", ".join(missing), ", ".join(sorted(self.manifest["bands"])),
                   " ".join(missing)))

        logger.info("Manifest created %s, survey %s, MontagePy %s",
                    self.manifest["created"], self.manifest["survey"],
                    self.manifest.get("container_versions", {}).get("MontagePy", "?"))
        logger.info("Region %s, size %.9f deg, thumbnails %dx%d",
                    self.region["location"], self.region["size_deg"],
                    self.region["thumbnail_width"], self.region["thumbnail_height"])

        # The manifest, not a flag, decides where raw images come from: whichever
        # mode bin/prefetch.py ran in is the only mode whose inputs actually
        # exist. Mixed manifests are legal -- one band listed, another
        # downloaded -- and each band is registered according to its own entry.
        self.archive_bands = [band for band in self.args.bands
                              if self.manifest["bands"][band].get("mode")
                              == "archive"]
        if self.archive_bands:
            if not _JOB_BYPASS_PARAM:
                raise SystemExit(
                    "Archive mode needs per-input staging bypass, and this "
                    "Pegasus API's Job.add_inputs() has no bypass_staging "
                    "parameter (5.1.2 does).\n\nThere is deliberately no "
                    "fallback: the global pegasus.transfer.bypass.input.staging "
                    "property also bypasses the stageable wrappers in bin/, "
                    "which have submit-host pfns, and the chunk jobs then fail "
                    "with 'Expected local file does not exist'.\n\nUpgrade "
                    "Pegasus, or switch these bands back to prefetch mode:\n"
                    "    bin/prefetch.py --bands %s"
                    % " ".join(self.archive_bands))
            logger.warning("Archive mode for band(s) %s: their raw images are "
                           "NOT on disk and each project_chunk job will "
                           "download its own from the archive at run time.",
                           " ".join(self.archive_bands))

    def check_unique_filenames(self):
        """Raw image LFNs must be unique across bands.

        Pegasus keys the Replica Catalog by logical filename, so two bands
        sharing a basename would collide. 2MASS Atlas filenames encode the band,
        so this holds in practice -- but it is checked rather than assumed,
        because the failure would be a silently wrong mosaic.
        """
        seen = {}
        for band in self.args.bands:
            for record in self.manifest["bands"][band]["files"]:
                other = seen.get(record["name"])
                if other:
                    raise SystemExit(
                        "raw image name %r appears in both band %s and band %s. "
                        "Pegasus LFNs must be unique; this survey needs "
                        "band-prefixed staging, which project_chunk.py does not "
                        "currently implement." % (record["name"], other, band))
                seen[record["name"]] = band
        logger.info("%d raw image filenames, all unique across bands", len(seen))

    def split_chunks(self):
        """Split each band's sorted image table into per-chunk tables.

        Done here, on the submit host, rather than by a workflow job: the number
        of chunks then follows from data already on disk, so every output
        filename in the DAG is known before planning. A splitting job would
        produce a runtime-determined set of files, which Pegasus cannot declare.
        """
        # Start from an empty directory: re-generating with a smaller band list
        # or chunk size would otherwise leave stale tables behind.
        if os.path.isdir(self.chunk_dir):
            shutil.rmtree(self.chunk_dir)
        os.makedirs(self.chunk_dir, exist_ok=True)
        for band in self.args.bands:
            entry = self.manifest["bands"][band]
            table_path = os.path.join(self.data_dir, entry["table"])
            table = mtbl.read(table_path).sorted_by_filename()

            if self.args.subset:
                total = len(table)
                table = table.subset(table.data_rows[:self.args.subset])
                logger.info("Band %s: --subset %d of %d images",
                            band, len(table), total)

            band_chunks = []
            for index, chunk in enumerate(table.chunks(self.args.proj_chunk_size)):
                chunk_id = "c%02d" % index
                name = "rimages_%s_%s.tbl" % (band, chunk_id)
                path = chunk.write(os.path.join(self.chunk_dir, name))
                band_chunks.append((chunk_id, name, path, chunk.filenames()))
            self.chunks[band] = band_chunks

            logger.info("Band %s: %d images -> %d chunks of <=%d",
                        band, len(table), len(band_chunks),
                        self.args.proj_chunk_size)

    # ---------------------------------------------------------------- catalogs

    def create_pegasus_properties(self, hosted_site_catalog=None):
        self.props = Properties()
        self.props["pegasus.transfer.threads"] = "16"
        if hosted_site_catalog:
            # Use one of Pegasus' centrally hosted site catalogs instead of
            # a locally generated one. pegasus-plan downloads and caches the
            # named file from the catalog repository at plan time.
            # https://pegasus.isi.edu/documentation/reference-guide/catalogs.html#centrally-hosted-site-catalogs
            self.props["pegasus.catalog.site.repo.file"] = hosted_site_catalog
        # Chunk archives are large; keep integrity checking but avoid rechecking
        # every staged file on retry.
        self.props["pegasus.integrity.checking"] = "nosymlink"
        self.props["pegasus.data.configuration"] = "condorio"

        # Deliberately NOT setting pegasus.transfer.bypass.input.staging here.
        # Archive mode bypasses per input, on the raw images only; see
        # _JOB_BYPASS_PARAM for what the global flag breaks.

    # Not used by the CLI — pegasus-plan resolves the site catalog from a
    # centrally hosted one instead (see -s/--hosted-site-catalog). Kept for
    # notebook use, when a self-contained, locally generated HTCondor site
    # catalog is wanted.
    def create_sites_catalog(self, exec_site_name="compute"):
        self.sc = SiteCatalog()
        local = Site("local").add_directories(
            Directory(Directory.SHARED_SCRATCH, self.shared_scratch_dir)
            .add_file_servers(FileServer("file://" + self.shared_scratch_dir,
                                         Operation.ALL)),
            Directory(Directory.LOCAL_STORAGE, self.local_storage_dir)
            .add_file_servers(FileServer("file://" + self.local_storage_dir,
                                         Operation.ALL)),
        )
        exec_site = (
            Site(exec_site_name)
            .add_condor_profile(universe="vanilla")
            .add_pegasus_profile(style="condor")
        )
        self.sc.add_sites(local, exec_site)

    def create_transformation_catalog(self, exec_site_name="compute"):
        self.tc = TransformationCatalog()

        sif = os.path.join(self.wf_dir, "Apptainer", "Montage_Container.sif")
        if not os.path.exists(sif):
            raise SystemExit(
                "Container not built: %s\nBuild it with:\n"
                "    apptainer build --ignore-fakeroot-command \\\n"
                "        Apptainer/Montage_Container.sif "
                "Apptainer/Montage_Container.def" % sif)

        # image_site MUST be passed by keyword: the 4th positional parameter is
        # `arguments`, and a site name passed there leaves image_site unset and
        # yields an unresolvable CONTAINER_SITE at plan time.
        container = Container(
            "montage",
            container_type=Container.SINGULARITY,
            image="file://" + sif,
            image_site="local",
        )

        transformations = []
        for name, config in TOOL_CONFIGS.items():
            wrapper = os.path.join(self.wf_dir, "bin", "%s.py" % name)
            if not os.path.exists(wrapper):
                logger.warning("wrapper missing, skipping transformation %s: %s",
                               name, wrapper)
                continue
            transformations.append(
                Transformation(name,
                               site=exec_site_name,
                               pfn=wrapper,
                               is_stageable=True,
                               container=container)
                .add_pegasus_profile(memory=config["memory"],
                                     cores=config["cores"])
            )

        self.tc.add_containers(container)
        self.tc.add_transformations(*transformations)
        logger.info("Registered %d transformations in container %s",
                    len(transformations), os.path.basename(sif))

    def create_replica_catalog(self):
        self.rc = ReplicaCatalog()

        def add(lfn, path):
            absolute = os.path.abspath(path)
            if not os.path.exists(absolute):
                raise SystemExit("input listed in the manifest is missing: %s\n"
                                 "Re-run bin/prefetch.py --verify" % absolute)
            self.rc.add_replica("local", lfn, "file://" + absolute)
            self.files[lfn] = File(lfn)
            return self.files[lfn]

        def add_url(lfn, url, sha256=None):
            """Register an image that lives at the archive, not on disk.

            Bypass is not set here -- add_replica has no such parameter. It is
            set per input on the consuming job, in add_band_pipeline.
            """
            options = {}
            if sha256:
                # Without this Pegasus has nothing to check an archive download
                # against. prefetch.py carries these over from the bootstrap or an
                # earlier prefetch manifest when one exists.
                options["checksum"] = {"sha256": sha256}
            self.rc.add_replica(ARCHIVE_SITE, lfn, url, **options)
            self.files[lfn] = File(lfn)
            return self.files[lfn]

        # Template header and geometry, produced by prefetch.py so that no job
        # has to stage the ~800 MB template just to read a 38-card header.
        add("region.hdr", os.path.join(self.data_dir, "region.hdr"))
        add("region.json", os.path.join(self.data_dir, "region.json"))

        # Support modules imported by the wrappers (not transformations).
        for name in SUPPORT_FILES:
            add(name, os.path.join(self.wf_dir, "bin", name))

        # The template image itself -- consumed only by overlay_template.
        template_rel = self.manifest["template"]["path"]
        self.template_lfn = os.path.basename(template_rel)
        add(self.template_lfn, os.path.join(self.data_dir, template_rel))

        # Per-chunk image tables written by split_chunks().
        for band, band_chunks in self.chunks.items():
            for _, name, path, _ in band_chunks:
                add(name, path)

        # Raw images, only those the chunk tables actually reference.
        #
        # The chunk tables hold whatever the band's image table held, which for
        # an archive-mode band may be a path rather than a bare basename -- so
        # match on basename, exactly as project_chunk.stage_raw_images() does
        # when it looks for the staged file.
        local_count = archive_count = checksummed = 0
        for band in self.args.bands:
            entry = self.manifest["bands"][band]
            archive_mode = entry.get("mode") == "archive"
            referenced = {os.path.basename(n)
                          for _, _, _, names in self.chunks[band] for n in names}
            for record in entry["files"]:
                if os.path.basename(record["name"]) not in referenced:
                    continue
                if archive_mode:
                    add_url(record["name"], record["url"],
                            sha256=record.get("sha256"))
                    archive_count += 1
                    checksummed += 1 if record.get("sha256") else 0
                else:
                    add(record["name"],
                        os.path.join(self.data_dir, entry["raw_dir"],
                                     record["name"]))
                    local_count += 1

        logger.info("Replica catalog: %d local raw images + %d archive URLs + "
                    "%d chunk tables + %d support/template files",
                    local_count, archive_count,
                    sum(len(c) for c in self.chunks.values()),
                    3 + len(SUPPORT_FILES))

        if archive_count:
            logger.info("Archive replicas on site %r; staging bypassed per "
                        "input on the chunk jobs", ARCHIVE_SITE)
            if checksummed < archive_count:
                logger.warning("%d of %d archive URLs have no checksum, so "
                               "Pegasus cannot verify those downloads",
                               archive_count - checksummed, archive_count)

    # -------------------------------------------------------------------- DAG

    def out(self, lfn):
        """A File object for a workflow-produced file, created once and shared.

        Sharing the object is what lets infer_dependencies build the edges; two
        File("x") instances for the same LFN would leave the edge missing.
        """
        if lfn not in self.files:
            self.files[lfn] = File(lfn)
        return self.files[lfn]

    def support_inputs(self):
        return [self.files[name] for name in SUPPORT_FILES]

    def create_workflow(self):
        self.wf = Workflow(self.wf_name, infer_dependencies=True)
        args = self.args
        survey = self.manifest["survey"]

        region_hdr = self.files["region.hdr"]
        region_json = self.files["region.json"]

        mosaics = {}
        for band in args.bands:
            mosaics[band] = self.add_band_pipeline(band, survey, region_hdr,
                                                  region_json)

        if args.skip_blend:
            logger.info("--skip-blend: stopping after the per-band mosaics")
        elif len(args.bands) != 3:
            logger.warning("composite needs exactly 3 bands, have %d (%s); "
                           "skipping composite, overlay and blend",
                           len(args.bands), " ".join(args.bands))
        else:
            self.add_composite_pipeline(survey, region_json, mosaics)

        if not args.skip_validate:
            self.add_validate(region_json)

    def add_validate(self, region_json):
        """Compare the run's products against the notebook (SPEC.md section 7).

        The reference data travels as one tar rather than as individual files
        because the notebook's thumbnails have exactly the same filenames as the
        workflow's own thumbnails -- staged flat into one working directory they
        would collide, and the comparison would silently compare a file with
        itself.
        """
        reference_tar = self.build_reference_tar()
        if reference_tar is None:
            logger.warning("no reference data; skipping the validate job")
            return

        thumbnails = self.products["thumbnails"]
        statuses = self.products["statuses"]
        mosaics = self.products["mosaics"]
        tables = self.products["tables"]

        report_json = self.out("validation_report.json")
        report_md = self.out("validation_report.md")

        job = (
            Job("validate", _id="validate", node_label="validate")
            .add_args("--results-dir", ".",
                      "--reference-tar", os.path.basename(reference_tar),
                      "--reference-dir", "reference",
                      "--region-json", "region.json",
                      "--report-json", report_json.lfn,
                      "--report-md", report_md.lfn,
                      "--subset", str(self.args.subset),
                      "--min-coadd-images", str(self.args.min_coadd_images))
            .add_inputs(self.files[os.path.basename(reference_tar)],
                        self.files["region.hdr"], region_json,
                        *thumbnails, *statuses, *mosaics, *tables,
                        *self.support_inputs())
            .add_outputs(report_json, report_md, stage_out=True,
                         register_replica=False)
        )
        self.wf.add_jobs(job)
        logger.info("validate job compares %d thumbnails, %d status files, "
                    "%d mosaics", len(thumbnails), len(statuses), len(mosaics))

    def build_reference_tar(self):
        """Archive reference/ (expectations + notebook thumbnails) for staging.

        The Tier 3 baseline is deliberately excluded: it is ~5 GB of FITS and
        Tier 3 is opt-in, run by hand against output/ once a baseline exists.
        """
        import tarfile

        reference_dir = os.path.join(self.wf_dir, "reference")
        expectations = os.path.join(reference_dir, "notebook_expectations.json")
        if not os.path.exists(expectations):
            logger.warning("no %s -- run bin/extract_notebook_refs.py",
                           expectations)
            return None

        os.makedirs(self.catalog_dir, exist_ok=True)
        tar_path = os.path.join(self.catalog_dir, "reference.tar")
        included = 0
        with tarfile.open(tar_path, "w") as archive:
            archive.add(expectations, arcname="notebook_expectations.json")
            included += 1
            thumb_dir = os.path.join(reference_dir, "notebook_thumbnails")
            for name in sorted(os.listdir(thumb_dir)) if os.path.isdir(thumb_dir) else []:
                archive.add(os.path.join(thumb_dir, name),
                            arcname=os.path.join("notebook_thumbnails", name))
                included += 1

        lfn = os.path.basename(tar_path)
        self.rc.add_replica("local", lfn, "file://" + os.path.abspath(tar_path))
        self.files[lfn] = File(lfn)
        logger.info("Reference archive %s: %d files, %.1f MB",
                    lfn, included, os.path.getsize(tar_path) / 1e6)
        return tar_path

    def add_band_pipeline(self, band, survey, region_hdr, region_json):
        """The three-stage per-band sub-DAG: chunks -> fitplanes -> mosaic -> render."""
        args = self.args
        lower = band.lower()

        # --- stage 1: reprojection, one job per chunk (the parallel stage) ---
        tars = []
        for chunk_id, table_name, _, image_names in self.chunks[band]:
            tar = self.out("projected_%s_%s.tar" % (band, chunk_id))
            status = self.out("project_%s_%s.status.json" % (band, chunk_id))
            table = self.files[table_name]
            raws = [self.files[name] for name in image_names]

            job = (
                Job("project_chunk", _id="project_%s_%s" % (band, chunk_id),
                    node_label="project_%s_%s" % (band, chunk_id))
                .add_args("--band", band, "--chunk", chunk_id,
                          "--chunk-table", table_name,
                          "--region-hdr", "region.hdr",
                          "--output-tar", tar.lfn,
                          "--status-json", status.lfn,
                          "--quick-mode" if args.quick_mode else "--no-quick-mode")
            )

            # The chunk table, region.hdr and the support modules are file:// on
            # the submit host: they must keep the normal condorio path, because a
            # worker cannot fetch a submit-host path. Only the raw images have
            # archive URLs a worker can reach, so bypass is set on those alone.
            job.add_inputs(table, region_hdr, *self.support_inputs())
            if band in self.archive_bands:
                job.add_inputs(*raws, bypass_staging=True)
            else:
                job.add_inputs(*raws)

            job.add_outputs(tar, stage_out=False, register_replica=False)
            job.add_outputs(status, stage_out=True, register_replica=False)
            self.wf.add_jobs(job)
            tars.append(tar)
            self.products["statuses"].append(status)

        tar_args = []
        for tar in tars:
            tar_args += ["--tar", tar.lfn]

        # --- stage 2: background modelling (tiny table outputs) ---
        pimages = self.out("pimages_%s.tbl" % band)
        diffs = self.out("diffs_%s.tbl" % band)
        fits_tbl = self.out("fits_%s.tbl" % band)
        corrections = self.out("corrections_%s.tbl" % band)
        fit_status = self.out("fitplanes_%s.status.json" % band)

        fitplanes = (
            Job("fitplanes", _id="fitplanes_%s" % band,
                node_label="fitplanes_%s" % band)
            .add_args("--band", band, "--region-hdr", "region.hdr",
                      *tar_args,
                      "--pimages", pimages.lfn, "--diffs", diffs.lfn,
                      "--fits-table", fits_tbl.lfn,
                      "--corrections", corrections.lfn,
                      "--status-json", fit_status.lfn,
                      "--min-images", str(args.min_images))
            .add_inputs(region_hdr, *tars, *self.support_inputs())
            .add_outputs(pimages, diffs, fits_tbl, corrections, fit_status,
                         stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(fitplanes)
        self.products["statuses"].append(fit_status)
        self.products["tables"] += [pimages, diffs, fits_tbl, corrections]

        # --- stage 3: background correction and coadd ---
        mosaic_fits = self.out("%s%s.fits" % (survey.lower(), lower))
        mosaic_area = self.out("%s%s_area.fits" % (survey.lower(), lower))
        cimages = self.out("cimages_%s.tbl" % band)
        mosaic_status = self.out("mosaic_%s.status.json" % band)

        mosaic_args = ["--band", band, "--region-hdr", "region.hdr",
                       *tar_args,
                       "--pimages", pimages.lfn,
                       "--corrections", corrections.lfn,
                       "--cimages", cimages.lfn,
                       "--output", mosaic_fits.lfn,
                       "--status-json", mosaic_status.lfn]

        # The _area planes are a Montage byproduct: mAdd always writes them, and
        # they must be declared, but nothing downstream reads them and the
        # notebook comparison ignores them. Keeping them in scratch saves ~1.6 GB
        # of stage-out per band (~3.4 GB over a full run + uncorrected).
        area_stage_out = bool(self.args.stage_out_area)
        mosaic_outputs = [mosaic_fits, cimages, mosaic_status]

        # The uncorrected coadd (notebook cell 13) is illustrative and only ever
        # made for the first band, matching the notebook.
        uncorrected = None
        if not args.skip_uncorrected and band == args.bands[0]:
            uncorrected = self.out("uncorrected.fits")
            uncorrected_area = self.out("uncorrected_area.fits")
            mosaic_args += ["--uncorrected-output", uncorrected.lfn]
            mosaic_outputs.append(uncorrected)

        mosaic_job = (
            Job("mosaic", _id="mosaic_%s" % band, node_label="mosaic_%s" % band)
            .add_args(*mosaic_args)
            .add_inputs(region_hdr, pimages, corrections, *tars,
                        *self.support_inputs())
            .add_outputs(*mosaic_outputs, stage_out=True, register_replica=False)
        )
        areas = [mosaic_area] + ([uncorrected_area] if uncorrected is not None else [])
        mosaic_job.add_outputs(*areas, stage_out=area_stage_out,
                               register_replica=False)
        self.wf.add_jobs(mosaic_job)
        self.products["statuses"].append(mosaic_status)
        self.products["tables"].append(cimages)
        self.products["mosaics"].append(mosaic_fits)
        if uncorrected is not None:
            self.products["mosaics"].append(uncorrected)

        # --- stage 4: render ---
        self.add_render(band, mosaic_fits,
                        "%s%s.png" % (survey.lower(), lower),
                        "%s%s_small.png" % (survey.lower(), lower),
                        region_json, label="mosaic_%s" % band)

        if uncorrected is not None:
            # Notebook cell 15: same -2s max gaussian-log stretch, and the source
            # of the graydatamin/graydatamax Tier 1 reference values.
            self.add_render(band, uncorrected, "uncorrected.png",
                            "uncorrected_small.png", region_json,
                            label="uncorrected_%s" % band, suffix="uncorrected")

        return mosaic_fits

    def add_render(self, band, mosaic, png_name, thumb_name, region_json,
                   label, suffix=None):
        png = self.out(png_name)
        thumb = self.out(thumb_name)
        job_id = "render_%s" % (suffix or band)
        status = self.out("render_%s.status.json" % (suffix or band))

        job = (
            Job("render", _id=job_id, node_label=job_id)
            .add_args("--input", mosaic.lfn, "--output", png.lfn,
                      "--thumbnail", thumb.lfn,
                      "--region-json", "region.json",
                      "--status-json", status.lfn,
                      "--label", label)
            .add_inputs(mosaic, region_json, *self.support_inputs())
            .add_outputs(png, thumb, status, stage_out=True,
                         register_replica=False)
        )
        self.wf.add_jobs(job)
        self.products["statuses"].append(status)
        self.products["thumbnails"].append(thumb)
        return png

    def add_composite_pipeline(self, survey, region_json, mosaics):
        """Colour composite, template render, and the final blend."""
        args = self.args
        blue, green, red = (mosaics[b] for b in args.bands)

        composite_png = self.out("%s.png" % survey)
        composite_stretch = self.out("%sStretch.png" % survey.lower())
        composite_thumb = self.out("%s_small.png" % survey)
        composite_status = self.out("composite.status.json")

        composite = (
            Job("composite", _id="composite", node_label="composite")
            .add_args("--blue", blue.lfn, "--green", green.lfn, "--red", red.lfn,
                      "--output", composite_png.lfn,
                      "--stretched", composite_stretch.lfn,
                      "--thumbnail", composite_thumb.lfn,
                      "--region-json", "region.json",
                      "--status-json", composite_status.lfn,
                      "--contrast", str(args.composite_contrast),
                      "--brightness", str(args.composite_brightness))
            .add_inputs(blue, green, red, region_json, *self.support_inputs())
            .add_outputs(composite_png, composite_stretch, composite_thumb,
                         composite_status, stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(composite)
        self.products["statuses"].append(composite_status)
        self.products["thumbnails"].append(composite_thumb)

        overlay = args.overlay_name
        template = self.files[self.template_lfn]
        overlay_png = self.out("%s.png" % overlay)
        overlay_stretch = self.out("%sStretch.png" % overlay.lower())
        overlay_thumb = self.out("%s_small.png" % overlay)
        overlay_status = self.out("overlay_template.status.json")

        overlay_job = (
            Job("overlay_template", _id="overlay_template",
                node_label="overlay_template")
            .add_args("--input", template.lfn, "--output", overlay_png.lfn,
                      "--stretched", overlay_stretch.lfn,
                      "--thumbnail", overlay_thumb.lfn,
                      "--region-json", "region.json",
                      "--status-json", overlay_status.lfn,
                      "--contrast", str(args.overlay_contrast),
                      "--brightness", str(args.overlay_brightness))
            .add_inputs(template, region_json, *self.support_inputs())
            .add_outputs(overlay_png, overlay_stretch, overlay_thumb,
                         overlay_status, stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(overlay_job)
        self.products["statuses"].append(overlay_status)
        self.products["thumbnails"].append(overlay_thumb)

        blend_png = self.out("%s_%s.png" % (overlay, survey))
        blend_thumb = self.out("%s_%s_small.png" % (overlay, survey))
        blend_status = self.out("blend.status.json")

        blend = (
            Job("blend", _id="blend", node_label="blend")
            .add_args("--tmass", composite_stretch.lfn,
                      "--template", overlay_stretch.lfn,
                      "--output", blend_png.lfn,
                      "--thumbnail", blend_thumb.lfn,
                      "--region-json", "region.json",
                      "--status-json", blend_status.lfn,
                      "--alpha", str(args.blend_alpha),
                      "--gamma", str(args.blend_gamma))
            .add_inputs(composite_stretch, overlay_stretch, region_json,
                        *self.support_inputs())
            .add_outputs(blend_png, blend_thumb, blend_status,
                         stage_out=True, register_replica=False)
        )
        self.wf.add_jobs(blend)
        self.products["statuses"].append(blend_status)
        self.products["thumbnails"].append(blend_thumb)

    # ------------------------------------------------------------------ output

    # ------------------------------------------------------------------
    # Plan / run / monitor (thin wrappers over the Pegasus API Workflow
    # object, for interactive use e.g. from a Jupyter notebook)
    # ------------------------------------------------------------------
    def plan_submit(self, exec_site_name="compute", raise_errors=False):
        try:
            # cleanup=inplace matters here: intermediate chunk archives are
            # ~9 GB and are removed as soon as no consumer remains. See
            # SPEC.md 4.6.
            self.wf.plan(
                dir="submit",
                sites=[exec_site_name],
                output_sites=["local"],
                cleanup="inplace",
                verbose=1,
                submit=True,
            )
        except PegasusClientError as e:
            print(e)
            if raise_errors:
                raise

    def status(self):
        try:
            self.wf.status(long=True)
        except PegasusClientError as e:
            print(e)

    def wait(self):
        try:
            self.wf.wait()
        except PegasusClientError as e:
            print(e)

    def statistics(self):
        try:
            self.wf.statistics()
        except PegasusClientError as e:
            print(e)

    def write(self):
        if self.sc is not None:
            self.sc.write()
        self.props.write()
        self.rc.write()
        self.tc.write()
        self.wf.write(file=self.args.output)

    def summarise(self):
        counts = {}
        # Workflow.jobs is a dict keyed by job id, so iterate its values.
        jobs = self.wf.jobs.values() if hasattr(self.wf.jobs, "values") \
            else self.wf.jobs
        for job in jobs:
            counts[job.transformation] = counts.get(job.transformation, 0) + 1
        total = sum(counts.values())
        logger.info("DAG: %d jobs", total)
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            logger.info("  %-18s %d", name, count)
        return total


def build_parser():
    """The CLI's argument parser. The notebook parses its own settings with it,
    so both entry points share one set of options and defaults."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="workflow.yml")
    parser.add_argument("-s", "--hosted-site-catalog", metavar="FILE",
                        default=None,
                        help="Name of a Pegasus centrally hosted site catalog "
                             "to plan against (e.g. access-pegasus.yml), "
                             "instead of a locally generated one. Sets "
                             "pegasus.catalog.site.repo.file; see "
                             "https://pegasus.isi.edu/documentation/"
                             "reference-guide/catalogs.html"
                             "#centrally-hosted-site-catalogs")
    # --execution-site is kept as an alias: the runbooks and reports use it.
    parser.add_argument("-e", "--execution-site-name", "--execution-site",
                        dest="execution_site", metavar="STR", default="compute",
                        help="Execution site name (default: compute). Use "
                             "condorpool on a plain HTCondor pool with no site "
                             "catalog")
    parser.add_argument("--bands", nargs="+", default=["J", "H", "K"],
                        help="bands, in blue/green/red order (default: J H K)")
    parser.add_argument("--proj-chunk-size", type=int, default=32,
                        help="raw images per reprojection job (default: 32); "
                             "the workflow's one parallelism knob")
    parser.add_argument("--subset", type=int, default=0,
                        help="use only the first N images per band -- a full "
                             "end-to-end run in minutes, for development")
    parser.add_argument("--min-coadd-images", type=int, default=460,
                        help="validation floor: fail if a band coadds fewer "
                             "than this many images (ignored under --subset)")
    parser.add_argument("--min-images", type=int, default=0,
                        help="fail a band if fewer than this many images "
                             "reproject (0 disables; try 460 for a full run)")
    parser.add_argument("--quick-mode", dest="quick_mode", action="store_true",
                        default=True, help="mProjExec quickMode (notebook default)")
    parser.add_argument("--no-quick-mode", dest="quick_mode", action="store_false")
    parser.add_argument("--skip-uncorrected", action="store_true",
                        help="skip the illustrative uncorrected coadd")
    parser.add_argument("--skip-blend", action="store_true",
                        help="stop after the per-band mosaics")
    parser.add_argument("--stage-out-area", action="store_true",
                        help="also copy the Montage _area planes to output/ "
                             "(~1.6 GB per band; nothing downstream needs them)")
    parser.add_argument("--skip-validate", action="store_true",
                        help="omit the notebook-comparison job")
    parser.add_argument("--overlay-name", default="MeerKAT",
                        help="basename for the overlay's PNGs (default: MeerKAT)")
    parser.add_argument("--composite-contrast", type=float, default=2.0)
    parser.add_argument("--composite-brightness", type=float, default=-1.0)
    parser.add_argument("--overlay-contrast", type=float, default=3.0)
    parser.add_argument("--overlay-brightness", type=float, default=-128.0)
    parser.add_argument("--blend-alpha", type=float, default=0.6)
    parser.add_argument("--blend-gamma", type=float, default=64.0)
    return parser


def main():
    args = build_parser().parse_args()
    args.bands = normalise_bands(args.bands)

    workflow = MeerKATMosaicWorkflow(args)
    workflow.load_manifest()
    workflow.check_unique_filenames()
    workflow.split_chunks()

    logger.info("Execution site: %s", args.execution_site)
    logger.info("Hosted site catalog: %s", args.hosted_site_catalog
                or "(none — supply your own site catalog)")

    workflow.create_pegasus_properties(
        hosted_site_catalog=args.hosted_site_catalog)
    workflow.create_transformation_catalog(args.execution_site)
    workflow.create_replica_catalog()
    workflow.create_workflow()
    workflow.write()

    logger.info("Wrote %s", args.output)
    workflow.summarise()

    if workflow.archive_bands:
        chunk_jobs = sum(len(workflow.chunks[b]) for b in workflow.archive_bands)
        logger.warning("Band(s) %s fetch from the archive at run time, across %d "
                       "chunk jobs. The job count is unchanged -- chunking "
                       "already batches the images, so there is nothing here for "
                       "Pegasus clustering to collapse -- but every chunk job "
                       "that runs opens its own unthrottled connection to the "
                       "archive. Raise --proj-chunk-size to trade parallelism "
                       "for fewer connections.",
                       " ".join(workflow.archive_bands), chunk_jobs)
        logger.warning("Do not expect a bit-identical match against a prefetch "
                       "baseline until bin/prefetch.py --compare-tables passes.")

    # The generator never plans or submits. cleanup=inplace matters here:
    # intermediate chunk archives are ~9 GB and are removed as soon as no
    # consumer remains (SPEC.md 4.6). --output-dir keeps outputs in output/:
    # the CLI writes no local site, and Pegasus's default one would stage them
    # to wf-output/ instead.
    logger.info("Plan and submit:\n    pegasus-plan --dir submit -s %s -o local "
                "--output-dir %s --cleanup inplace --submit %s",
                args.execution_site, workflow.local_storage_dir, args.output)


if __name__ == "__main__":
    main()
