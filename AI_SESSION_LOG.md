# Claude Code Usage Log — MeerKAT / 2MASS Mosaic Workflow

Provenance record of how this workflow was built with Claude Code, for the study
on AI-assisted Pegasus workflow generation. Same entry format as
`pegasus/cc-usage-log.md`, but every metric here is **extracted from the raw
transcripts** (`~/.claude/projects/**.jsonl`) rather than estimated from inside
the session — see [§ Extraction method](#extraction-method).

**Machine**: `jupyter-kthare10` pod (container `notebook`), namespace
`jupyterhub`, JupyterHub on the Jetstream2 **`studio`** Kubernetes cluster
(`149.165.168.83:6443`), 8 cores / 29 GB, user `kthare10`. Claude Code v2.1.233,
permission mode `auto`, plugin `pegasus-ai@scitech`.

**Raw transcripts**: `pegasus/workflows/studio-claude-sessions/` — transcripts,
`SESSION_SUMMARY.md`, `session_metrics.csv`, extractor `extract_sessions.py`.

---
### Session: 2026-08-20 14:58 (design SPEC from MeerKAT notebook)
- **Workflow(s)**: meerkat-2mass-mosaic (NEW — designed this session)
- **Session ID**: `962c63cf-ffdc-472a-8c35-df02d86d434f` · cwd `/home/kthare10/work/workflows` · 14:58:34 → 15:09:54 UTC (11.3 min wall)
- **Prompts**: 2 logged (1 distinct — repeated after a `model_not_found` 404), 1 `/model` slash command
- **Summary of prompts**: (1) "help me design a pegasus workflow using the notebook MeerKAT.ipynb by creating a SPEC.md. Please plan the workflow, suggest constraints, non-constraints, expected outcomes. Make sure we can compare results against the notcebook. Any open decisions you want me to answer. Make parallel tasks where possible. Data Fetch can be pre workflow step so I can execute the workflow repeatedly without downloading the data every time."
- **Key actions**:
  - Dispatched **one sub-agent** to mine the 19 MB `MeerKAT.ipynb` and recover the ground truth now recorded in [`SPEC.md` §1.2](SPEC.md) — 479 images and 2,133 overlap pairs per band, 3 bands (J/H/K), 3.2° field
  - Probed the real MontagePy 2.2.0 API by running it, rather than trusting the notebook's calls (the API realities are written up in `SPEC.md` §9.1)
  - Posed four scoping decisions via `AskUserQuestion`: project directory name; Montage fan-out granularity (full fan-out + clustering vs coarse executives vs hybrid); default run size (full field with a `--quick` mode vs small-field default); and how to prove notebook equivalence (thumbnails + numeric metrics vs your own FITS vs metrics only)
- **Outcome**: `SPEC.md` drafted with constraints, non-constraints, expected outcomes and eight open questions handed back for decision. Session ended on one 404 `model_not_found`; the prompt was resubmitted, which is why it appears twice in the transcript
- **Models used**: `claude-opus-5` (15 assistant requests parent + 109 sub-agent)
- **Tool calls**: 9 parent (Bash×6, Read×1, Agent×1, AskUserQuestion×1) + 63 sub-agent = **72**
- **Agent tasks**: 1 (`agent-a538ab75df174a763`, 8.2 min)
- **Input tokens**: 122 (18 parent + 104 sub-agent)
- **Output tokens**: 17,887 (13,547 + 4,340)
- **Cache tokens**: 191,016 write / 5,081,531 read
- **Estimated cost (USD)**: **$12.55** ($3.14 parent + $9.41 sub-agent)
- **Files created**: `SPEC.md` (first draft)
- **Files modified**: none
- **Key decisions / milestones**: project named `meerkat-2mass-mosaic`; data fetch split out as a pre-workflow step so the DAG can be re-run without re-downloading; notebook equivalence adopted as a first-class deliverable rather than an afterthought. The sub-agent cost **3× its parent session** — it swept the whole notebook instead of reusing parent context
---

---
### Session: 2026-08-20 15:11 (build, containerise and run the workflow)
- **Workflow(s)**: meerkat-2mass-mosaic (implemented + executed)
- **Session ID**: `e628d52b-d27f-47cd-b674-86d02b057122` · cwd `/home/kthare10/work` · 15:11:34 → 16:31:03 UTC (79.5 min wall, 60.5 min active)
- **Prompts**: 6 logged (4 distinct asks — one repeated after a 404, one is a `/mode;` switch)
- **Summary of prompts**: (1) "can you continue with the workflow construction with MeerKAT.ipynb?" (404, resubmitted at 15:12); (2) `/mode;`; (3) re-stated design ask — "…Data fetch should be a pre-workflow step, so workflow can be executed repeatedly without data download multiple times."; (4) **`Q1 - chunked, Q2 - all Q3 - match notebook Q4 - local Q5 https://studio.pegasus-ai.org/ Q6 - url alive Q7 - yes Q8 - parameterize`** — the eight answers that unblocked the whole build; (5) "log token usage"
- **Key actions**:
  - Turn-by-turn cost, showing where the session actually went:

    | # | Time | Prompt | Dur | Reqs | Tools | Out tok | Cache R | Cost |
    |---|---|---|---|---|---|---|---|---|
    | 1 | 15:11:34 | continue construction | — | 1 | 0 | 0 | 0 | $0.00 (404) |
    | 2 | 15:12:07 | `/mode;` | — | 1 | 0 | 0 | 0 | $0.00 |
    | 3 | 15:12:26 | continue construction (retry) | 0.4 m | 4 | 4 Bash | 1,000 | 204,638 | $1.22 |
    | 4 | 15:15:47 | re-stated design ask | 7.7 m | 6 | 4 Bash, 1 Write | 33,194 | 466,786 | $3.89 |
    | 5 | 15:29:16 | **`Q1 - chunked, Q2 - all …`** | **57.5 m** | 149 | 105 Bash, 24 Edit, 18 Write | 195,801 | 39,788,300 | **$79.41** |
    | 6 | 16:30:21 | log token usage | 0.6 m | 3 | 3 Bash | 1,387 | 1,138,580 | $1.85 |

  - Turn 4 rewrote `SPEC.md` and restated the eight open decisions. **Turn 5 is the workflow**: one line of answers bought 57.5 min and 149 API requests of autonomous build — 85 % of the session's output tokens, 96 % of its cache reads, **92 % of its cost**
  - Implemented 14 wrappers under `bin/`, `workflow_generator.py`, `Apptainer/Montage_Container.def`, `run_manual.sh`, `README.md`; ran a full plan → submit → debug cycle against local `condorpool`
  - Bash-dominated (105 of 147 tool calls in turn 5): driven by running MontagePy, `apptainer build`, `pegasus-plan` and `pegasus-status` and reacting to real output, not by writing code blind
  - Two 404 `model_not_found` errors, both recovered by resubmitting; no failed edits or reverted work
- **Outcome**: Workflow ran **clean on the archived attempt — 100/100 nodes, 0 failures, 74.2 min** (`runs/full/`, wf UUID `9c5cb993-c37d-41ab-b9f6-a4923422bc1a`). Planner 15.5 s; 59 compute + 17 cleanup + 13 stage-in + 10 stage-out + 1 create-dir; 1,488 input / 131 intermediate / 13 output files; 3,028 s cumulative compute (avg 51 s, max 215 s); longest jobs `fitplanes_K` 215 s, `mosaic_J` 210 s, `fitplanes_J` 202 s, `composite` 146 s. So ≈1.5 h of AI session time (≈$99) produced a 100-node DAG reproducing a notebook that takes ~50 min serially *per band*
- **Models used**: `claude-opus-5` (346 assistant requests)
- **Tool calls**: **159** (Bash×116, Edit×24, Write×19)
- **Agent tasks**: 0
- **Input tokens**: 324
- **Output tokens**: 231,382
- **Cache tokens**: 352,886 write / 41,598,304 read
- **Estimated cost (USD)**: **$86.37**
- **Files created**: `bin/`{`blend`, `composite`, `extract_notebook_refs`, `fitplanes`, `mjob`, `mosaic`, `mtbl`, `overlay_template`, `prefetch`, `project_chunk`, `render`, `validate`, `run_notebook_baseline`}`.py`, `workflow_generator.py`, `run_manual.sh`, `Apptainer/Montage_Container.def`, `README.md` (18 files tracked as written or edited)
- **Files modified**: `SPEC.md` (rewritten in turn 4, amended through the build)
- **Key decisions / milestones**: the eight answers are recorded as [`SPEC.md` §2](SPEC.md) — Q1 chunked `*Exec` (~60 DAG nodes), Q2 all three validation tiers, Q3 `quickMode=True` to match the notebook, Q4 local `condorpool`, Q5 current volume with `--cleanup inplace`, Q6 MeerKAT URL live (fetched by `bin/prefetch.py` with resume + checksum), Q7 reproduce the uncorrected J mosaic behind `--skip-uncorrected`, Q8 parameterise survey/bands/region/template/chunk size
---

## Totals for this workflow

| Metric | Value |
|---|---|
| Sessions | 2 top-level + 1 sub-agent |
| Prompts | 8 logged, **5 distinct asks** |
| Assistant API requests | 470 |
| Tool calls | **231** |
| Wall-clock | **90.8 min** |
| Active model time | 60.5 min |
| Input tokens | 446 |
| Output tokens | **249,269** |
| Cache-write tokens | 543,902 |
| Cache-read tokens | **46,679,835** |
| Estimated cost (USD) | **≈ $98.92** |
| Resulting DAG | 100 nodes, 100 succeeded, 74.2 min |

Two thirds of the cost is cache reads: 46.7 M cache-read tokens ≈ $70 against
$18.7 for the 249 K output tokens. **Cost scales with session length, not with
code produced** — turn 5 alone re-read 39.8 M cached tokens.

## Archived artifacts

- `output/` — 4.1 GB, 95 files: three rectified 2MASS mosaics, the uncorrected J
  mosaic, colour composite, MeerKAT blend, and the `*.status.json` / `*.tbl`
  provenance
- `runs/full/` — 34 MB, 683 files: DAG, `jobstate.log`, `braindump.yml`,
  `*.stampede.db`, abstract and concrete workflow renderings

All 778 files were copied out of the studio pod and **verified byte-for-byte by
MD5 against the source**; manifest in `MD5SUMS-output-runs.txt`, re-check with
`md5sum -c MD5SUMS-output-runs.txt` (`md5 -r` on macOS). Not copied, being
regenerable and large: `data/` (3.1 GB, rebuild via `bin/prefetch.py`),
`output_subset/` (3.2 GB), `scratch/` (532 MB),
`Apptainer/Montage_Container.sif` (516 MB, rebuild from the `.def`), and the
`runs/subset2` + `runs/kthare10` run directories.

## Extraction method

Metrics come from the transcript JSONL, not from self-report:

- **Prompts** count only true human turns (`origin.kind == "human"` or
  `promptSource` in `typed`/`paste`); slash commands are tallied separately and
  `tool_result` blocks — which also arrive as `type: user` — are excluded.
- **Tokens and cost** are summed per API request from `message.usage`,
  **de-duplicated by `requestId`** (usage repeats across streamed blocks of one
  request). Cost applies `claude-opus-5` list price — $15 / $75 / $18.75 / $1.50
  per Mtok for input / output / cache-write / cache-read.
- **Tool calls** count `tool_use` blocks; **files** come from
  `file-history-delta` records; **active time** sums the
  `system/turn_duration` records (the design session logs none, so its active
  time reads 0).
- **Run provenance** comes from `runs/full/*.metrics`, `*.dag.metrics` and the
  `*.stampede.db` `job_instance` table, not from the transcript.

Caveats for the write-up:

1. **Cost is list-price arithmetic, not a billed amount.** A billed figure has to
   come from the account usage export, and the two will not match exactly.
2. **Both sessions contain a duplicated prompt** caused by a 404
   `model_not_found` plus manual resubmit — 8 logged, 5 distinct asks.
3. Two other sessions in the same container archive (`32da65d3`, 07-31,
   Opus 4.8, $26.72; `04b2be0c`, 08-16, $13.31) belong to the
   medical-imaging-FL and cper-soilmoisture workflows, **not** this one — do not
   fold them into these totals.
