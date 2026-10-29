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
  The web UI (`webui/`, the frappe-ui Vue app `cwcli ui` serves) is not JSX and its colour comes from frappe-ui's Tailwind preset, not from the design system's inline-style components, so it is covered by `check-webui-tokens.mjs` (below) rather than by the ESLint pass.
  Its colour rule: every colour in `webui/src` is a **semantic frappe-ui token** (`bg-surface-*`, `text-ink-*`, `border-outline-*` and their siblings), never a raw Tailwind palette class (`bg-gray-100`, `hover:text-red-500`) and never a raw hex literal.
  A raw palette class compiles under the preset and silently stops following the theme, which is exactly why a lint has to catch it.
  It is textual, so a class assembled in a `<script>` block is caught too; px is not checked for the web UI (frappe-ui's own sizing utilities carry the spacing scale).
  It succeeds the retired `check-console-css.mjs` that covered the single-file Console the web UI replaced.
  The same check also fails on a menu option (`label:` plus `icon:` in one object literal) whose icon string is not `lucide-*`: frappe-ui 1.0 renders a bare name such as `refresh-cw` as an empty icon slot with only a dev-mode console warning, which is how "Check for updates" shipped without its icon.
- **Proof it bites.** `npm test` (`bites.test.mjs`) runs the real ESLint pass over `fixtures/bad.jsx` (one violation per family) asserting each is flagged, over `good.jsx` asserting zero, and self-checks the web UI colour and menu-icon detectors - so a regression back to the oxlint no-op fails CI.

Local commands (`cd tools/design-adherence`): `npm ci`, then `npm run lint`, `npm test`, `npm run check:webui`.
Frontend lint tooling is a dev dependency only (`tools/design-adherence/package.json`), never a Python runtime dep.
