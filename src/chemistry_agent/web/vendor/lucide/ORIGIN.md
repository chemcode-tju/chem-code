# Lucide UI icons

- Upstream: https://github.com/lucide-icons/lucide
- Documentation: https://lucide.dev/guide/
- Pinned commit: `333574bdecb43e7cb4a4bb1cd21d1367cfbbee2c` (2026-11-16).
- Retrieved: 2026-11-17 from the official repository.
- License: ISC, with the upstream Feather-derived MIT notice retained in `LICENSE`.

`icons/` contains unchanged upstream SVG files. `icons.json` maps Chem Code's stable `i-*` sprite identifiers to those filenames. `../../ui-icons.svg` copies each SVG's children into a `symbol`, retaining the upstream 24 × 24 view box, 2-unit stroke and round line caps/joins. CSS disclosure and selected-state masks use the same source files. Native select backgrounds embed the official `chevron-down` path at 24 × 24 / stroke 2 with the existing muted icon color; this avoids depending on inherited color inside an external SVG image.

The `i-gpt` symbol is the existing OpenAI brand artwork, covered separately by `../lobe-icons/LICENSE`. Chem Code and Antigravity brand marks, scientific charts, axes and numeric symbols are not UI control icons and remain unchanged. Do not hand-draw new UI icons; add an official Lucide source and a mapping here.
