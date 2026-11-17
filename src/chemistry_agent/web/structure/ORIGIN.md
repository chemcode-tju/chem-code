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
The bundled `vendor/3Dmol-min.js` is copied unchanged, including upstream's
manual-resize patch; its license is in `vendor/LICENSE-3Dmol.txt`.
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
