// Proves the adherence gate bites, so it never silently degrades back to the
// oxlint no-op it replaces. Runs the real ESLint pass over deliberate fixtures and
// the raw-hex detector's self-check.
//
// - bad.jsx MUST fail, and must name each violation family (hex, px, font,
//   restricted prop/variant, restricted import).
// - good.jsx MUST pass, so a false-positive regression fails the gate too.

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const eslintBin = here("../node_modules/.bin/eslint");

function runEslint(fixture) {
  try {
    const stdout = execFileSync(
      eslintBin,
      ["--no-ignore", "--max-warnings", "0", "--format", "json", here(fixture)],
      { cwd: here(".."), encoding: "utf8" },
    );
    return { code: 0, report: JSON.parse(stdout) };
  } catch (err) {
    // ESLint exits non-zero when it finds problems; the JSON report is on stdout.
    return { code: err.status ?? 1, report: JSON.parse(err.stdout) };
  }
}

let failed = false;
const check = (cond, msg) => {
  if (cond) {
    console.log(`  ok: ${msg}`);
  } else {
    console.error(`  FAIL: ${msg}`);
    failed = true;
  }
};

// --- bad.jsx must be rejected, naming every violation family ---
const bad = runEslint("fixtures/bad.jsx");
const badMsgs = bad.report.flatMap((f) => f.messages.map((m) => m.message));
const badText = badMsgs.join("\n");
check(bad.code !== 0, "bad.jsx is rejected (non-zero exit)");
check(/Raw hex color/i.test(badText), "raw hex color is flagged");
check(/Raw px value/i.test(badText), "raw px value is flagged");
check(/Font not provided/i.test(badText), "off-system font is flagged");
check(/<Button> variant must be one of/i.test(badText), "invalid Button variant is flagged");
check(/<Button> doesn't accept that prop/i.test(badText), "undeclared Button prop is flagged");
check(/design-system components from 'index.js'/i.test(badText), "component-internals import is flagged");

// --- good.jsx must pass clean ---
const good = runEslint("fixtures/good.jsx");
const goodProblems = good.report.reduce((n, f) => n + f.messages.length, 0);
check(good.code === 0 && goodProblems === 0, "good.jsx passes with zero problems");

// --- raw-hex detector self-check ---
try {
  execFileSync("node", [here("check-console-css.mjs"), "--selfcheck"], { stdio: "inherit" });
  console.log("  ok: check-console-css selfcheck passed");
} catch {
  console.error("  FAIL: check-console-css selfcheck");
  failed = true;
}

if (failed) {
  console.error("\nadherence gate self-test FAILED");
  process.exit(1);
}
console.log("\nadherence gate self-test: OK");
