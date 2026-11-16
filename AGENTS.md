# Repository instructions

## Frontend work

- Before adding or changing UI, read `docs/design/ui-spec.md` and the relevant component's code. This is the shared design contract for all future features.
- Keep Solid Glass materials and shared semantic tokens in `src/chemistry_agent/web/theme.css` and `solid-glass.css`; upstream provenance is in `vendor/solid-glass/ORIGIN.md`. Preserve Harness aliases only for compatibility. Reuse the existing menu, disclosure, chart and viewer patterns rather than introducing per-feature visual systems.
- Avoid dense explanatory microcopy. Keep normal UI at 14px and secondary information at 13px; preserve user-selected reading size.
- Preserve scientific values, units, dependencies and the distinction between plan completion, scheduler status and scientific convergence.
- Verify affected interactions and responsive layouts in a real browser. Do not launch computations or mutate production research data for UI testing.
- Do not read or write any `archive` directory.
