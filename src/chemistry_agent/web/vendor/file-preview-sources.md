# File preview dependencies

These assets are copied from pinned npm release tarballs and served locally.

- `pdfjs/` comes from `pdfjs-dist@6.5.438` (Mozilla PDF.js, Apache-2.0). It contains the legacy distribution's minified display API and worker, character maps, standard fonts, color profiles and image decoders. The PDF scripting sandbox is omitted.
- `highlight/` comes from `@highlightjs/cdn-assets@11.12.0` (Highlight.js, BSD-3-Clause). `es/highlight.min.js` is served as `highlight.min.mjs`.
- `papaparse/` comes from `papaparse@5.7.1` (Papa Parse, MIT). It contains the minified browser build.

The preview dispatch follows the text and artifact split in OpenCode v2.2.0's `packages/gui-plugins/src/file/view.tsx`. The file tree keyboard adaptation retains its license in `opencode/`.
