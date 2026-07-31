import React from "react";

/** A persistent preference toggle: auto-inspect, follow logs, keep window on top. */
export function Switch({ checked = false, onChange, label, hint, disabled = false, style, ...rest }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: "var(--space-5)", cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, ...style }}>
      <input type="checkbox" role="switch" checked={checked} onChange={onChange} disabled={disabled} style={{ position: "absolute", opacity: 0, width: 0, height: 0 }} {...rest} />
      <span
        style={{
          width: 32, height: 18, flex: "none", borderRadius: "var(--radius-pill)", padding: 2,
          background: checked ? "var(--accent)" : "var(--bg-active)",
          border: "1px solid " + (checked ? "transparent" : "var(--border-default)"),
          transition: "background-color var(--duration-fast) var(--ease-out)",
          display: "flex", justifyContent: checked ? "flex-end" : "flex-start",
        }}
      >
        <span style={{ width: 14, height: 14, borderRadius: "50%", background: checked ? "var(--accent-fg)" : "var(--text-muted)", transition: "all var(--duration-fast) var(--ease-out)" }} />
      </span>
      {label ? (
        <span style={{ minWidth: 0 }}>
          <span style={{ display: "block", font: "var(--type-body)", color: "var(--text-primary)" }}>{label}</span>
          {hint ? <span style={{ display: "block", font: "var(--type-meta)", color: "var(--text-muted)", marginTop: 1 }}>{hint}</span> : null}
        </span>
      ) : null}
    </label>
  );
}
