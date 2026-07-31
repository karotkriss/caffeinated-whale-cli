import React from "react";
import { Icon } from "../icons/Icon.jsx";

/**
 * A spinner with the product's rotating tip line, mirroring the CLI's TipSpinner.
 * Tips are the CLI's own strings — pass them in rather than inventing new ones.
 */
export function Spinner({ label, tip, size = 16, inline = false, style, ...rest }) {
  if (inline) {
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-4)", color: "var(--text-secondary)", ...style }} {...rest}>
        <span style={{ display: "block", color: "var(--accent)", animation: "cw-spin 700ms linear infinite" }}><Icon name="loader-circle" size={size} /></span>
        {label}
      </span>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--space-5)", padding: "var(--space-10) var(--space-7)", textAlign: "center", ...style }} {...rest}>
      <span style={{ display: "block", color: "var(--accent)", animation: "cw-spin 700ms linear infinite" }}><Icon name="loader-circle" size={22} /></span>
      <div style={{ font: "var(--type-body-strong)", color: "var(--text-primary)" }}>{label}</div>
      {tip ? <div style={{ font: "var(--type-meta)", color: "var(--text-muted)", maxWidth: 380 }}>{tip}</div> : null}
    </div>
  );
}
