# Registered scientific results

The platform stores **display metadata**, not a scientific execution recipe. A producer may
keep any file layout and use any solver. Register a dataset for a plan step and one monitored
job/attempt; the UI reads its declared structures and series. Register once while a job is
running; live files are read without model calls. Registration does not change the plan,
job, submission script, or computational files.

Agent tools: `calculation_result_register`, `calculation_result_inspect`.
Platform operations: `result.register`, `result.inspect` (also in `/api/platform/operations`).
HTTP: `POST /api/automation/chat/{session}/results` with the registration below.
Read: `GET .../workflows/{plan}/steps/{node}/results?job_id=...&key=...`.
Omit `key` to list datasets; add `file=<registered relative path>` to read/download a structure.

```json
{
  "plan_id": "<plan UUID>",
  "node_id": "relaxation",
  "job_id": "<platform job UUID>",
  "key": "optimization",
  "title": "Optimization",
  "source": {"adapter": "manifest", "path": "my-layout/display.json"}
}
```

A portable manifest (or `source.adapter=inline` with the same fields):

```json
{
  "structures": [
    {"label": "Initial", "path": "inputs/start.vasp"},
    {"label": "Final", "path": "run-01/optimized.extxyz"}
  ],
  "series": [
    {"label": "Energy", "x_label": "Optimization step", "y_label": "E0", "unit": "eV",
     "points": [[0, -10.0], [1, -10.2]]},
    {"label": "Force", "x_label": "Optimization step", "y_label": "Fmax", "unit": "eV/Å",
     "points": [[0, 0.4], [1, 0.02]]}
  ]
}
```

Use finite coordinates with explicit units and correct scientific meaning. Publish manifests
and complete snapshots atomically. Structure paths are relative to the monitored job's
work directory. Binary data can still be accessed through existing artifact/job tools;
this text-preview endpoint is not a restriction on agent tools.

## Existing ASE NEB producer adapter

The current CI-NEB producer already writes independent JSON snapshots. No running script
needs editing. The following source maps that existing output to the display contract:

```json
{
  "adapter": "json_paths",
  "pattern": "paths/step_*/path.json",
  "image_pattern": "image_{index:02d}.vasp",
  "fields": {
    "step": "step",
    "energy": "energy_TOTEN_eV",
    "force": "projected_fmax_eV_A",
    "valid": "all_scf_valid"
  },
  "energy_label": "TOTEN",
  "force_label": "NEB 投影 Fmax",
  "force_target": 0.02
}
```

`image_pattern` resolves beside each snapshot JSON. `fields` map producer field names;
`valid` is optional and, when present, must be true. The reader skips malformed/incomplete
snapshots and surfaces their paths/reasons. It checks finite energy/force values and image
file presence, not scientific convergence. The UI shows the actual zero-based producer
step, every image's energy relative to image 0, projected Fmax history, and peak energy
history. Selecting a curve point selects the matching image or iteration. The structure icon opens
the selected image; the structure section lists all images of the selected complete path.
Latest-follow refreshes the curve and an open image together, while historical selection
keeps both on the same optimization step. This is a search
path display, not an assertion of a validated barrier/MEP.

The registration key is idempotent within session/plan/node/job. Different attempts must
use different job bindings. The platform writes only its local result registry and cache;
there is no submission or event-wakeup side effect. Snapshot reads are coalesced/cached
for 20 seconds. Cached indexes and previously viewed structure files survive restart;
when SSH is unavailable the UI labels them as stale, with their observation time. Unread
remote structures are not claimed to be available offline.

For new formats, agents can produce the portable manifest with ordinary Python tools;
add a read-only adapter when translating an established format is useful. Do not encode
scientific workflow choices into this registry, recursively scan all force-call files,
or require agents to rename their original calculation files.

## Default behavior for new jobs

Job adoption and platform submission now create the display binding automatically for
both the actual job node and its parent research task. This is a local write only:
no extra tool turn, model invocation or SSH discovery during submission. Ordinary root
VASP/LAMMPS files retain their existing monitor. `auto` watches `display.json`; it also
recognizes the existing `paths/step_*/path.json` NEB convention with a bounded read.
No manifest/results yet means no empty extra sidebar card.

For a custom layout, include only one hint in the existing adopt call or `job` settings:

- `"results": "neb"` — existing ASE path format.
- `"results": "neb:custom/iteration_*/path.json"` — same fields, custom path location.
- `"results": "analysis/display.json"` — portable manifest at any relative location.

The platform fills plan/step/job IDs, key and title. Agent instructions say to produce
custom display metadata alongside the calculation script; do not describe that metadata
in chat, repeatedly register it, or poll with the model. Advanced mappings remain available
through `calculation_result_register`. Native Chem Agent, Codex and new Antigravity
sessions share the concise automation instruction. Default registration also works without
an agent. Replaying adoption preserves existing display settings and never resubmits a job.

### Optional NEB endpoint references

Add only `"endpoint_outcars": {"initial": "endpoints/initial/OUTCAR",
"final": "endpoints/final/OUTCAR"}` to the result source to show endpoint references.
Paths are relative to the monitored job directory; use existing file tools to stage
upstream outputs if necessary. The agent identifies which calculation corresponds to
each endpoint. The reader uses the last complete force/TOTEN pair from an 8 MiB tail;
missing or partial outputs do not remove valid path snapshots. This is a reference,
not a convergence claim or an automatic verification of matching geometries/settings.
The UI preserves the snapshot energy profile, shows reference TOTEN and source on
selection, and offers a separate **endpoint atomic force** mode in the same chart.
These unprojected, all-atom forces include constrained atoms and must never replace
NEB projected force or be assessed against the NEB stopping threshold.

### NEB path playback

`GET .../results?job_id=...&key=...&path_step=4` reads all structures in one
complete snapshot, in producer image order, through a single remote read. It has
its own persistent cache entry; this endpoint never submits or changes a job.
The sidebar passes this explicit file sequence to the embedded viewer. It preserves
POSCAR cells and selective-dynamics flags and checks element-order compatibility.
Playback, seek, speed and loop controls operate locally. Frame changes update the
path chart selection and individual-image download link; selecting a curve point
seeks the already loaded sequence without reloading the iframe. Changing optimization
step opens that step's sequence and preserves the selected image. These frames are
NEB path images, not MD time steps or interpolated geometries. Ordinary companion-file
imports retain their existing behavior and are never automatically concatenated.
