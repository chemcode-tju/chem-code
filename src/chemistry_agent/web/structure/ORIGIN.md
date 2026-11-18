# Structure Viewer source and licenses

Imported from the user's `liyang-tju/extxyz-extension` repository, commit
`035ef0e7e648fa344d089de67acb99cbbf818f29` (Structure Viewer 0.10.0).

- `parser.js`, `appearance.js`, `geometry.js`, `ase-traj.js`, `vasp-stream.js`,
  `trajectory-store.js`, `snapshot.js`, `selection.js`, `scene.js`, `viewport.js`,
  `controls.js`, and `locales.json` are copied from `plugin/web/`.
  `trajectory-store.js` additionally exports its byte-line reader for local MD formats.
- `i18n.js` embeds the original locale catalog for standalone static serving.
- `app.js`, `index.html`, and `style.css` are adapted for Chem-Agent: browser file
  upload, authenticated HTTP conversion, local downloads, and navigation replace
  the Codex MCP host bridge. Native save and FFmpeg animation controls are omitted.
- `import.js`, `lammps-metadata.js`, `md-stream.js` and the Python conversion API are new integration code.
- Core JavaScript regression tests in `tests/structure/` are adapted from upstream
  with module paths changed. `test_import.cjs` is new.

The upstream MIT notice is retained in `LICENSE-extxyz-extension.txt`.
The bundled `vendor/3Dmol-min.js` is the pinned renderer with Atom Lens'
`plugin/renderer_adapter.py` resize and studio-lighting patches applied once
when importing the asset. Both the live viewport and export reuse this file
and the light profile in `scene.js`. The renderer license is in
`vendor/LICENSE-3Dmol.txt`.
No runtime CDN, original Python MCP server, native helper, test trajectories,
or private local paths are required by this integration.

## UI adaptation, 2026-11-15

The compact header menus, OVITO-style transport bar, source-data inspector and
playback/resize fixes are adapted from the upstream **0.13.3 working tree** of
`liyang-tju/extxyz-extension` (including its latest uncommitted UI changes).
`inspector.js` is newly imported. `viewport.js` and the parser's metadata
round-trip follow that source; icon references use Chem-Agent's shared sprite.

Chem-Agent keeps its own browser upload, authenticated conversion, NEB image
sequence, simulation-step navigation and same-origin parent bridge. No native
or MCP file access is added. GRO and text dump preserve available source IDs,
types, names, charges and cell origins for the inspector. Missing force-field
types remain missing; elemental reference masses are marked explicitly.
The existing 100,000-atom per-frame limit and supported input formats are
unchanged. Upstream XTC/TRR/TPR support is not part of this UI adaptation.

The application design tokens style menus and controls. The actual structure,
source coordinates, scientific colors and snapshot export stay independent of
light/dark interface chrome.

The file-browser XYZ index uses the bounded byte scanning and opening-boundary
approach from Atom Lens (commit `7bf53a4`, `plugin/trajectory.py`). The index runs where the trajectory is mounted. SSH targets can run the scanner
through the existing connection with an SFTP fallback for restricted accounts.

Playback prefetch follows Atom Lens' `plugin/web/trajectory-store.js`: refill
only below a low watermark, cache neighboring raw frames and decode on demand.
Chem Code's HTTP adapter fetches the byte span once and splits it using the
existing frame index.

XYZ and extxyz playback may cache up to 160 frames / 256 MiB per open viewer
and look ahead by up to 72 frames. Two byte-range requests can run in parallel
with background indexing. The single-view layout creates one WebGL
context initially. Sphere-only frames with matching styles and composition
use the pinned renderer's existing position-only buffer update. Bond
perception, labels and measurement overlays continue to use the full path.

For range-backed trajectories such as XDATCAR, the browser scanner and player
share aligned 2 MiB blocks with a 256 MiB LRU and deduplicated concurrent
requests. The larger frame prefetch applies to VASP and indexed XYZ. The
current byte boundary and viewport/export lighting are shared as before.
