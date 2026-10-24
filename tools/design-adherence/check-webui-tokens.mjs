// Adherence check for the web UI (webui/src): colour comes from frappe-ui's
// SEMANTIC tokens only.
//
// frappe-ui's guidance (and the design system's "one token layer" rule): in app
// markup, colour is `bg-surface-*`, `text-ink-*`, `border-outline-*` and their
// siblings, never Tailwind's raw palette (`bg-gray-100`, `text-red-500`) - a raw
// palette class is a fixed light-mode value that never flips with the theme and
// bypasses the token layer - and never a raw hex literal. This is the successor
// to the retired console.html check: the console's tokens were CSS custom
// properties, the web UI's are Tailwind utilities, so the detector reads class
// strings instead of `var()` calls.
//
// Scope: every .vue/.js under webui/src (templates, scripts and <style> blocks
// alike - the scan is textual, so a class built in script is caught too).
// Run `--selfcheck` to prove the detector still bites.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const WEBUI_SRC = fileURLToPath(new URL("../../webui/src", import.meta.url));

// Tailwind's raw palette families (frappe-ui's preset keeps these names, so the
// classes compile - which is exactly why a lint has to catch them).
const FAMILIES =
  "gray|red|green|blue|amber|orange|yellow|teal|cyan|violet|purple|pink|slate|zinc|neutral|stone|indigo|sky|emerald|rose|fuchsia|lime";
// Colour-bearing utility prefixes, with their variants (hover:, dark:, max-md:).
const PREFIXES =
  "bg|text|border|divide|ring|fill|stroke|placeholder|from|via|to|outline|decoration|accent|caret|shadow|ring-offset";
const RAW_PALETTE = new RegExp(
  `(?<![\\w-])(?:[\\w-]+:)*(?:${PREFIXES})-(?:${FAMILIES})-(?:\\d{2,3})(?:/\\d+)?(?![\\w-])`,
  "g",
);
// A hex colour literal (3-8 digits) that is not an HTML entity or an anchor id.
const RAW_HEX = /(?<![&\w])#[0-9a-fA-F]{3,8}\b(?![\w-])/g;

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(vue|js|ts|css)$/.test(name)) out.push(path);
  }
  return out;
}

/** Return [{line, text, match}] violations for one file's source text. */
export function findViolations(source) {
  const out = [];
  source.split("\n").forEach((text, i) => {
    for (const m of text.matchAll(RAW_PALETTE)) out.push({ line: i + 1, text: text.trim(), match: m[0] });
    // Hex only matters where it could be a colour: a class string or a style.
    if (/(?:class|style|color|background|border|fill|stroke)/i.test(text)) {
      for (const m of text.matchAll(RAW_HEX)) out.push({ line: i + 1, text: text.trim(), match: m[0] });
    }
  });
  return out;
}

function selfcheck() {
  const assertEq = (got, want, msg) => {
    if (got !== want) {
      console.error(`selfcheck FAILED: ${msg} (got ${got}, want ${want})`);
      process.exit(1);
    }
  };
  assertEq(findViolations('<div class="bg-gray-100 text-ink-gray-8">').length, 1, "raw palette bg must be caught");
  assertEq(findViolations('<span class="hover:text-red-500">').length, 1, "a variant-prefixed raw palette must be caught");
  assertEq(findViolations("const cls = 'border-blue-300/50'").length, 1, "a raw palette built in script must be caught");
  assertEq(findViolations('<div class="bg-surface-gray-2 text-ink-red-5 border-outline-gray-1 divide-outline-gray-1">').length, 0, "semantic tokens must pass");
  assertEq(findViolations('<div class="bg-surface-amber-7 ring-outline-gray-2 fill-ink-gray-5">').length, 0, "every semantic family must pass");
  assertEq(findViolations('<div style="color: #ff00aa">').length, 1, "raw hex in a style must be caught");
  assertEq(findViolations("router.push('/instances#login')").length, 0, "a hash in a URL is not a colour");
  assertEq(findViolations("<span>&#9662;</span>").length, 0, "an HTML entity is not a colour");
  assertEq(findViolations('<div class="rounded-6 px-4 py-3 text-base-medium tabular-nums">').length, 0, "non-colour utilities must pass");
  console.log("check-webui-tokens selfcheck: OK");
}

function main() {
  if (process.argv.includes("--selfcheck")) {
    selfcheck();
    return;
  }
  let total = 0;
  for (const file of walk(WEBUI_SRC)) {
    const violations = findViolations(readFileSync(file, "utf8"));
    if (violations.length === 0) continue;
    total += violations.length;
    console.error(`${file}: colour outside frappe-ui's semantic tokens:`);
    for (const v of violations) console.error(`  line ${v.line}: ${v.match}    ${v.text}`);
  }
  if (total > 0) {
    console.error(`\n${total} violation(s). Use bg-surface-*, text-ink-*, border-outline-* (see tools/design-adherence/README.md).`);
    process.exit(1);
  }
  console.log("check-webui-tokens: every colour in webui/src is a semantic token.");
}

main();
