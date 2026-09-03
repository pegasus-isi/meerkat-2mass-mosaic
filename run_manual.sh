#!/bin/bash
#
# Run every pipeline step serially, outside Pegasus, on a handful of images.
#
# This exists to catch the mistakes that are expensive to find inside a DAG:
# argument names that disagree between workflow_generator.py and a wrapper,
# support files that are not where a wrapper looks for them, a directory layout
# mProjExec does not accept, a missing _area plane. Those surface here in
# minutes instead of after a plan-submit-fail cycle.
#
# Each step runs in a working directory populated the way Pegasus populates one:
# every input staged FLAT, no source tree on the path. If a wrapper works here,
# it will work in a job.
#
# NOTE ON RUNTIME: --images only shrinks the reprojection work. The mosaic grid
# comes from region.hdr (10121 x 10527, taken from the MeerKAT template), so
# mAdd and mViewer do full-size work regardless -- expect ~1.7 GB mosaics and a
# few minutes for the coadd and render even with 8 images. That is correct
# behaviour, not a bug.
#
# Usage:
#   ./run_manual.sh                        # 8 J-band images, no overlay
#   ./run_manual.sh --band H --images 16
#   ./run_manual.sh --with-overlay         # also render the 800 MB template
#   ./run_manual.sh --keep                 # do not delete the working directory

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIF="${PROJECT_DIR}/Apptainer/Montage_Container.sif"
DATA_DIR="${PROJECT_DIR}/data"
WORKDIR="${PROJECT_DIR}/manual_test"

BAND="J"
IMAGES=8
WITH_OVERLAY=0
KEEP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --band)         BAND="$2"; shift 2 ;;
        --images)       IMAGES="$2"; shift 2 ;;
        --workdir)      WORKDIR="$2"; shift 2 ;;
        --with-overlay) WITH_OVERLAY=1; shift ;;
        --keep)         KEEP=1; shift ;;
        -h|--help)      sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n=== %s ===\n' "$*"; }
run() { echo "+ $*"; "$@"; }

# ---------------------------------------------------------------- preflight

[[ -f "$SIF" ]] || {
    echo "Container not built: $SIF" >&2
    echo "  apptainer build --ignore-fakeroot-command \\" >&2
    echo "      Apptainer/Montage_Container.sif Apptainer/Montage_Container.def" >&2
    exit 1
}
[[ -f "${DATA_DIR}/manifest.json" ]] || {
    echo "No ${DATA_DIR}/manifest.json -- run the pre-workflow fetch first:" >&2
    echo "  apptainer exec ${SIF} bin/prefetch.py" >&2
    exit 1
}

# In-container invocation. --pwd puts us in the staging directory, so wrappers
# see exactly the flat working directory a Pegasus job sees.
inbox() { apptainer exec --pwd "$WORKDIR" "$SIF" python3 "$@"; }

say "Preparing working directory ${WORKDIR}"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"

# Build a chunk table holding the first N images of the band, exactly as
# workflow_generator.py does at DAG-generation time.
CHUNK_TABLE="rimages_${BAND}_c00.tbl"
apptainer exec "$SIF" python3 - "$PROJECT_DIR" "$DATA_DIR" "$BAND" "$IMAGES" \
                                "$WORKDIR/$CHUNK_TABLE" <<'PYTHON'
import json, os, sys
project, data_dir, band, count, out = sys.argv[1:6]
sys.path.insert(0, os.path.join(project, "bin"))
import mtbl

with open(os.path.join(data_dir, "manifest.json")) as fh:
    manifest = json.load(fh)
entry = manifest["bands"][band]
table = mtbl.read(os.path.join(data_dir, entry["table"])).sorted_by_filename()
chunk = table.subset(table.data_rows[:int(count)])
chunk.write(out)
print("chunk table: %d of %d images" % (len(chunk), len(table)))
for name in chunk.filenames():
    print("  " + name)
PYTHON

# Stage inputs FLAT, the way Pegasus does.
say "Staging inputs"
cp "${DATA_DIR}/region.hdr" "${DATA_DIR}/region.json" "$WORKDIR/"
cp "${PROJECT_DIR}/bin/mjob.py" "${PROJECT_DIR}/bin/mtbl.py" "$WORKDIR/"

RAW_DIR="${DATA_DIR}/$(apptainer exec "$SIF" python3 -c "
import json,sys
print(json.load(open('${DATA_DIR}/manifest.json'))['bands']['${BAND}']['raw_dir'])")"
apptainer exec "$SIF" python3 - "$WORKDIR/$CHUNK_TABLE" "$RAW_DIR" "$WORKDIR" \
                                "$PROJECT_DIR" <<'PYTHON'
import os, shutil, sys
table_path, raw_dir, workdir, project = sys.argv[1:5]
sys.path.insert(0, os.path.join(project, "bin"))
import mtbl
names = mtbl.read(table_path).filenames()
for name in names:
    base = os.path.basename(name)
    shutil.copy(os.path.join(raw_dir, base), os.path.join(workdir, base))
print("staged %d raw images flat into the working directory" % len(names))
PYTHON

ls -1 "$WORKDIR" | head -20
echo "... $(ls -1 "$WORKDIR" | wc -l) files staged"

# ------------------------------------------------------------------- steps

say "Step 1/5: project_chunk (mProjExec + mImgtbl + tar)"
run inbox "${PROJECT_DIR}/bin/project_chunk.py" \
    --band "$BAND" --chunk c00 \
    --chunk-table "$CHUNK_TABLE" \
    --region-hdr region.hdr \
    --output-tar "projected_${BAND}_c00.tar" \
    --chunk-image-table "pimages_${BAND}_c00.tbl" \
    --status-json "project_${BAND}_c00.status.json"

say "Step 2/5: fitplanes (mImgtbl + mOverlaps + mDiffFitExec + mBgModel)"
run inbox "${PROJECT_DIR}/bin/fitplanes.py" \
    --band "$BAND" --region-hdr region.hdr \
    --tar "projected_${BAND}_c00.tar" \
    --pimages "pimages_${BAND}.tbl" \
    --diffs "diffs_${BAND}.tbl" \
    --fits-table "fits_${BAND}.tbl" \
    --corrections "corrections_${BAND}.tbl" \
    --status-json "fitplanes_${BAND}.status.json"

say "Step 3/5: mosaic (mBgExec + mImgtbl + mAdd, plus the uncorrected coadd)"
run inbox "${PROJECT_DIR}/bin/mosaic.py" \
    --band "$BAND" --region-hdr region.hdr \
    --tar "projected_${BAND}_c00.tar" \
    --pimages "pimages_${BAND}.tbl" \
    --corrections "corrections_${BAND}.tbl" \
    --cimages "cimages_${BAND}.tbl" \
    --output "2mass$(echo "$BAND" | tr 'A-Z' 'a-z').fits" \
    --uncorrected-output uncorrected.fits \
    --status-json "mosaic_${BAND}.status.json"

MOSAIC="2mass$(echo "$BAND" | tr 'A-Z' 'a-z').fits"
PNG="${MOSAIC%.fits}.png"

say "Step 4/5: render (mViewer + thumbnail)"
run inbox "${PROJECT_DIR}/bin/render.py" \
    --input "$MOSAIC" --output "$PNG" \
    --thumbnail "${MOSAIC%.fits}_small.png" \
    --region-json region.json \
    --status-json "render_${BAND}.status.json" \
    --label "manual_${BAND}"

say "Step 5/5: composite + blend"
# The same mosaic is fed to all three channels: this validates the argument
# plumbing and the stretch/blend arithmetic without needing three real bands.
run inbox "${PROJECT_DIR}/bin/composite.py" \
    --blue "$MOSAIC" --green "$MOSAIC" --red "$MOSAIC" \
    --output 2MASS.png --stretched 2massStretch.png \
    --thumbnail 2MASS_small.png \
    --region-json region.json --status-json composite.status.json

if [[ "$WITH_OVERLAY" == "1" ]]; then
    TEMPLATE=$(apptainer exec "$SIF" python3 -c "
import json
print(json.load(open('${DATA_DIR}/manifest.json'))['template']['path'])")
    cp "${DATA_DIR}/${TEMPLATE}" "$WORKDIR/"
    run inbox "${PROJECT_DIR}/bin/overlay_template.py" \
        --input "$(basename "$TEMPLATE")" \
        --output MeerKAT.png --stretched meerkatStretch.png \
        --thumbnail MeerKAT_small.png \
        --region-json region.json --status-json overlay_template.status.json
else
    # Blend needs two same-sized stretched PNGs; reuse the composite so the
    # blend arithmetic and the geometry check are still exercised.
    echo "(--with-overlay not given: blending the composite with itself)"
    cp "$WORKDIR/2massStretch.png" "$WORKDIR/meerkatStretch.png"
fi

run inbox "${PROJECT_DIR}/bin/blend.py" \
    --tmass 2massStretch.png --template meerkatStretch.png \
    --output MeerKAT_2MASS.png --thumbnail MeerKAT_2MASS_small.png \
    --region-json region.json --status-json blend.status.json

# ------------------------------------------------------------------ results

say "Results"
( cd "$WORKDIR" && ls -lh -- *.fits *.png *.tbl *.tar 2>/dev/null | \
    awk '{printf "  %-42s %s\n", $9, $5}' )

say "Montage return values captured"
for status in "$WORKDIR"/*.status.json; do
    echo "--- $(basename "$status")"
    apptainer exec "$SIF" python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for name, result in d.get('montage', {}).items():
    print('   %-18s %s' % (name, result))
" "$status"
done

if [[ "$KEEP" == "0" ]]; then
    say "Removing ${WORKDIR} (pass --keep to retain it)"
    rm -rf "$WORKDIR"
else
    say "Working directory retained: ${WORKDIR}"
fi

echo
echo "All steps completed."
