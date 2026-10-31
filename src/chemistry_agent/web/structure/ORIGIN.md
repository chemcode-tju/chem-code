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
