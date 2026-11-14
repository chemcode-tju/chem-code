# Repository instructions

## Frontend work

- Before adding or changing UI, read `docs/design/ui-spec.md` and the relevant component's code. This is the shared design contract for all future features.
- Keep DeepSeek Harness semantic colors and shared tokens in `src/chemistry_agent/web/theme.css`. Reuse the existing menu, disclosure, chart and viewer patterns rather than introducing per-feature visual systems.
- Avoid dense explanatory microcopy. Keep normal UI at 14px and secondary information at 13px; preserve user-selected reading size.
- Preserve scientific values, units, dependencies and the distinction between plan completion, scheduler status and scientific convergence.
- Verify affected interactions and responsive layouts in a real browser. Do not launch computations or mutate production research data for UI testing.
- Do not read or write any `archive` directory.
