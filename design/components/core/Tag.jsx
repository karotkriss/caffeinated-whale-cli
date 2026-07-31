import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** A monospace chip for a machine value: a port, a bench label, a branch, a site. */
export function Tag({ icon, mono = true, onRemove, children, style, ...rest }) {
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: "var(--space-2)", height: 20,
        padding: onRemove ? "0 3px 0 7px" : "0 7px", borderRadius: "var(--radius-sm)",
        border: "1px solid var(--border-subtle)", background: "var(--bg-hover)",
        color: "var(--text-secondary)", fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
        fontSize: "var(--text-xs)", whiteSpace: "nowrap", ...style,
      }}
      {...rest}
    >
      {icon ? <Icon name={icon} size={12} /> : null}
      {children}
      {onRemove ? (
        <button
          type="button" onClick={onRemove} aria-label="Remove"
          style={{ display: "flex", alignItems: "center", padding: 2, background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", borderRadius: "var(--radius-xs)" }}
        >
          <Icon name="x" size={11} />
        </button>
      ) : null}
    </span>
  );
}
