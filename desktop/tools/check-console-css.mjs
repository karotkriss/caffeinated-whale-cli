// Supplementary adherence check for commands/console.html.
//
// Why this exists: the Console is a single HTML file whose design tokens and
// consuming rules live in a `<style>` block, i.e. raw CSS text. ESLint's
// adherence policy uses AST selectors (`Literal`, `JSXOpeningElement`), which
// cannot see CSS text, so the eslint.config.mjs pass genuinely cannot reach the
// Console's inline CSS. This is the smallest check that catches the highest-value,
// zero-false-positive drift there: a raw hex colour used in CONSUMING code instead
// of a design-system token via var().
//
// Deliberately NOT enforced here (honest tradeoff, stated in the PR): raw `px`.
// The Console documents legitimate raw-px exceptions (structural layout, hairlines,
// breakpoints, the focus ring, fixed chrome sizes) and ~50 such uses exist, so a
// px grep is all false positives with no maintainer-authored allowlist to filter
// against. px stays enforced where it is clean - the ESLint pass over JS/JSX,
// including any future Console JSX build.
//
// A hex is a violation unless it is (a) the value of a `--custom-property`
// declaration (the token layer legitimately holds raw hex) or (b) an HTML numeric
// entity like `&#9662;`. Run `--selfcheck` to prove the detector still bites.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const CONSOLE_HTML = fileURLToPath(
  new URL("../../src/caffeinated_whale_cli/commands/console.html", import.meta.url),
);

// A hex color literal NOT immediately preceded by '&' (which would be an HTML
// entity such as &#9662;).
const HEX = /(?<!&)#[0-9a-fA-F]{3,8}\b/;
// A custom-property declaration: `--token: ...;` - the allowed token layer.
const TOKEN_DEF = /--[\w-]+\s*:[^;}]*(?:[;}]|$)/g;

/** Return [{line, text}] for consuming raw-hex violations in the CSS/HTML text. */
export function findRawHex(source) {
  const out = [];
  source.split("\n").forEach((text, i) => {
    const consumingText = text.replace(TOKEN_DEF, "");
    if (HEX.test(consumingText)) out.push({ line: i + 1, text: text.trim() });
  });
  return out;
}

function selfcheck() {
  const bad = ".x { color: #ff00aa; }";
  const tokenOk = "  --accent: #00a0d0;";
  const mixed = "--local: #fff; color: #000;";
  const semicolonlessMixed = ":root { --local: #fff } .x { color: #000; }";
  const entityOk = '<span>&#9662;</span>';
  const consumeOk = "  color: var(--accent);";
  const assertEq = (got, want, msg) => {
    if (got !== want) {
      console.error(`selfcheck FAILED: ${msg} (got ${got}, want ${want})`);
      process.exit(1);
    }
  };
  assertEq(findRawHex(bad).length, 1, "raw consuming hex must be caught");
  assertEq(findRawHex(tokenOk).length, 0, "token definition must be allowed");
  assertEq(
    findRawHex(mixed).length,
    1,
    "consuming hex beside a token definition must be caught exactly once",
  );
  assertEq(
    findRawHex(semicolonlessMixed).length,
    1,
    "consuming hex beside a semicolonless token definition must be caught exactly once",
  );
  assertEq(findRawHex(entityOk).length, 0, "HTML numeric entity must not be flagged");
  assertEq(findRawHex(consumeOk).length, 0, "var() token use must be allowed");
  console.log("check-console-css selfcheck: OK");
}

function main() {
  if (process.argv.includes("--selfcheck")) {
    selfcheck();
    return;
  }
  const source = readFileSync(CONSOLE_HTML, "utf8");
  const violations = findRawHex(source);
  if (violations.length > 0) {
    console.error(
      `Raw hex colour(s) in ${CONSOLE_HTML} - use a design-system token via var():`,
    );
    for (const v of violations) console.error(`  line ${v.line}: ${v.text}`);
    process.exit(1);
  }
  console.log("check-console-css: no raw hex outside the token layer.");
}

main();
