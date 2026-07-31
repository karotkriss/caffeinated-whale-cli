import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** A single-line text field. Values a machine produced are monospace. */
export function Input({ label, hint, error, icon, mono = false, size = "md", style, wrapperStyle, ...rest }) {
  const [focus, setFocus] = React.useState(false);
  const h = size === "sm" ? "var(--control-height-sm)" : size === "lg" ? "var(--control-height-lg)" : "var(--control-height)";
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", minWidth: 0, ...wrapperStyle }}>
      {label ? <span style={{ font: "var(--type-meta)", color: "var(--text-secondary)" }}>{label}</span> : null}
      <span
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-4)", height: h,
          padding: "0 var(--space-5)", background: "var(--bg-sunken)",
          border: "1px solid " + (error ? "var(--danger)" : focus ? "var(--border-accent)" : "var(--border-default)"),
          borderRadius: "var(--radius-md)", boxShadow: focus ? "var(--ring-focus)" : "none",
          transition: "var(--transition-control)",
        }}
      >
        {icon ? <span style={{ color: "var(--text-muted)" }}><Icon name={icon} size={14} /></span> : null}
        <input
          onFocus={() => setFocus(true)}
          onBlur={() => setFocus(false)}
          style={{
            flex: 1, minWidth: 0, background: "none", border: "none", outline: "none",
            color: "var(--text-primary)", fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
            fontSize: "var(--text-md)", ...style,
          }}
          {...rest}
        />
      </span>
      {error ? <span style={{ font: "var(--type-meta)", color: "var(--text-danger)" }}>{error}</span>
       : hint ? <span style={{ font: "var(--type-meta)", color: "var(--text-muted)" }}>{hint}</span> : null}
    </label>
  );
}
