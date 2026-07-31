import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** The nothing-here surface: says what is missing and gives the one command that fixes it. */
export function EmptyState({ icon = "box", title, children, action, command, style, ...rest }) {
  return (
    <div
      style={{
        display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
        gap: "var(--space-5)", padding: "var(--space-12) var(--space-8)", textAlign: "center", ...style,
      }}
      {...rest}
    >
      <span style={{ color: "var(--text-disabled)" }}><Icon name={icon} size={26} strokeWidth={1.25} /></span>
      <div style={{ font: "var(--type-heading)", color: "var(--text-primary)" }}>{title}</div>
      {children ? <div style={{ font: "var(--type-body)", color: "var(--text-muted)", maxWidth: 400 }}>{children}</div> : null}
      {command ? (
        <code style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", color: "var(--text-accent)", background: "var(--bg-sunken)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: "5px 9px" }}>
          {command}
        </code>
      ) : null}
      {action ? <div style={{ marginTop: "var(--space-2)" }}>{action}</div> : null}
    </div>
  );
}
