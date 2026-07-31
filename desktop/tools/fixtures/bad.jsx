// Deliberate adherence violations. Kept OUT of the linted tree (tools/ is
// ignored) and driven directly by tools/bites.test.mjs to prove the policy bites.
import { Button } from "components/core/Button"; // no-restricted-imports: component internals

export function Bad() {
  const style = {
    color: "#ff00aa", // no-restricted-syntax: raw hex color
    padding: "12px", // no-restricted-syntax: raw px
    fontFamily: "font-family: Comic Sans", // no-restricted-syntax: off-system font
  };
  return (
    <Button variant="rainbow" bogusProp="x" style={style}>
      go
    </Button>
  );
}
