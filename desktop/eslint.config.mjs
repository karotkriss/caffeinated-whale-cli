// ESLint flat-config runner for the design system's adherence policy.
//
// The policy lives in `design/_adherence.oxlintrc.json` and is the maintainer's
// authored artifact - we load its `rules`/`overrides` VERBATIM and never copy or
// edit them here. oxlint cannot enforce it (it does not implement
// `no-restricted-syntax`); ESLint implements both `no-restricted-syntax` and
// `no-restricted-imports`, so this wraps the same file into an ESLint pass.
//
// The only key we drop is `x-omelette`, which is component/token metadata for the
// design tooling, not an ESLint rule. `plugins: ["react","import"]` is honoured by
// registering eslint-plugin-react (the sole plugin any rule references, via
// `react/forbid-elements`); `no-restricted-*` are core rules needing no plugin, so
// eslint-plugin-import is not pulled in for a rule set that references no `import/*`
// rule.
//
// Severity is left VERBATIM: the policy authors every rule at "warn", so the CI
// script runs eslint with `--max-warnings 0` to make any adherence warning block
// (see package.json `lint`). We gate on his rules without editing his severities.
//
// Scope: JS/JSX under desktop/ (the future Console JSX - DS-T3). The Console's
// inline CSS in `commands/console.html` is a `<style>` block, i.e. raw CSS text,
// NOT JS `Literal`/JSX nodes, so ESLint's AST selectors cannot reach it; that gap
// is covered by `tools/check-console-css.mjs`. Tooling and built/vendor trees are
// ignored so the pass only sees design-consuming app code.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "eslint-plugin-react";

const policy = JSON.parse(
  readFileSync(new URL("../design/_adherence.oxlintrc.json", import.meta.url), "utf8"),
);

const overrides = (policy.overrides ?? []).map((o) => ({
  files: o.files,
  rules: o.rules,
}));

export default [
  {
    ignores: [
      "**/node_modules/**",
      "src-tauri/**",
      "dist/**",
      "tools/**",
      "eslint.config.mjs",
    ],
  },
  {
    files: ["**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs"],
    plugins: { react },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: policy.rules,
  },
  ...overrides,
];

// Keep the referenced path importable for the test harness.
export const POLICY_PATH = fileURLToPath(
  new URL("../design/_adherence.oxlintrc.json", import.meta.url),
);
