# Design-system adherence lint

`design/_adherence.oxlintrc.json` is the maintainer-authored canonical adherence policy for frontend JSX.
[oxlint](https://oxc.rs/docs/guide/usage/linter) cannot load it because the policy uses `no-restricted-syntax`, which oxlint does not implement, so oxlint rejects the entire config and enforces none of its rules.
Enforcing the policy therefore uses **ESLint**, which implements both `no-restricted-imports` and `no-restricted-syntax`.

The runner lives here and runs in the always-run `Design-system adherence` job in `.github/workflows/design.yml`, so every change is checked.

- **`eslint.config.mjs`** loads the maintainer's `design/_adherence.oxlintrc.json` **verbatim** - it reads that file's `rules`/`overrides` and applies them unchanged.
  The only dropped key is `x-omelette` (design-tooling metadata, not an ESLint rule).
  `design/` is never modified: the runner adapts to the config, not the reverse.
  Rule severities are authored as `warn`, so `npm run lint` runs with `--max-warnings 0` to make any adherence warning block.
- **Scope / honest limits.** ESLint's selectors are AST-based (`Literal`, `JSXOpeningElement`), so the policy's full rule set (raw hex/px, off-system fonts, restricted props/variants, restricted imports) runs on **JS/JSX under `tools/design-adherence/`**.
  DS-T3 (the Console rebuild onto the design-system component library) deliberately kept the Console as a single self-contained `commands/console.html` with **no build step**: the design-system components are themselves inline-style/token primitives (no CSS class library), so they map faithfully onto same-DOM/same-token markup by hand, and the served artifact stays the zero-dependency file `serve` reads verbatim (which is what keeps its CSP tight).
  That rebuild is therefore covered by `check-console-css.mjs` (below), not by the ESLint JS/JSX pass; the ESLint pass stays wired and ready for a future JSX build should the Console ever adopt one.
  The Console (`commands/console.html`) is a single HTML file whose tokens and consuming rules live in a `<style>` block, i.e. raw CSS text that ESLint's AST rules genuinely cannot see.
  So `check-console-css.mjs` supplements it with the smallest zero-false-positive check there: a raw **hex** colour used in consuming CSS instead of a `var()` token (token-layer `--custom-property` definitions and HTML numeric entities are correctly allowed).
  Raw **px** is deliberately not grepped in the Console: it documents ~50 legitimate raw-px exceptions (structural layout, hairlines, breakpoints, the focus ring, fixed chrome), so a px grep would be all false positives; px stays enforced by ESLint on JS/JSX, where it is clean.
- **Proof it bites.** `npm test` (`bites.test.mjs`) runs the real ESLint pass over `fixtures/bad.jsx` (one violation per family) asserting each is flagged, over `good.jsx` asserting zero, and self-checks the hex detector - so a regression back to the oxlint no-op fails CI.

Local commands (`cd tools/design-adherence`): `npm ci`, then `npm run lint`, `npm test`, `npm run check:css`.
Frontend lint tooling is a dev dependency only (`tools/design-adherence/package.json`), never a Python runtime dep.
