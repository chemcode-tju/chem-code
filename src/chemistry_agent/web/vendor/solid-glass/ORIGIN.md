# Solid Glass

- Upstream: https://github.com/deeplethe/solid-glass
- Pinned revision: `6e8903e494a15491ceda38fafea5056c95a74d44` (2026-10-16).
- Source: `src/solid-glass.css`, `content/docs/material.mdx`.
- License: MIT, Copyright (c) 2026 DeepLethe; full text in `LICENSE`.

`tokens.css` contains the upstream light and dark variable blocks, with only a
`--sg-` prefix and the host's `data-theme` selector substituted. It does not
include Tailwind directives or shadcn selectors: Chem Code uses native HTML.

`../../theme.css` maps the material to the existing semantic tokens and retains
Harness aliases for compatibility. `../../solid-glass.css` adapts the upstream
rim, hairline, shadow, field, primary-button and single-state recipes to native
components. It is loaded after each page's component styles.

Product adaptations: retain the Chem Code mark, system/CJK fonts, user reading
size, existing scientific status colours and plot/atom palettes. Use an even
page background without ambient decorative lighting. Large surfaces and
repeated graph cards do not use blur; popup surfaces retain the upstream blur.
Reduced-transparency and forced-colour fallbacks preserve readable controls.
No React, Tailwind, shadcn or runtime network dependency is introduced.
