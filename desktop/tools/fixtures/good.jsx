// Clean design-consuming code: tokens via var(), index import, valid props.
// tools/bites.test.mjs asserts the policy reports ZERO problems here, so a
// false-positive regression fails the gate too.
import { Button } from "index.js";

export function Good() {
  const style = {
    color: "var(--accent)",
    padding: "var(--space-4)",
    fontFamily: "var(--font-sans)",
  };
  return (
    <Button variant="primary" size="md" style={style}>
      go
    </Button>
  );
}
