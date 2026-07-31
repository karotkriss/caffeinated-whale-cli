import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** A native select in the system's chrome. Used for bench pickers and log line counts. */
export function Select({ label, hint, options = [], size = "md", mono = false, style, wrapperStyle, ...rest }) {
  const h = size === "sm" ? "var(--control-height-sm)" : "var(--control-height)";
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", minWidth: 0, ...wrapperStyle }}>
      {label ? <span style={{ font: "var(--type-meta)", color: "var(--text-secondary)" }}>{label}</span> : null}
      <span style={{ position: "relative", display: "flex", alignItems: "center" }}>
        <select
          style={{
            appearance: "none", width: "100%", height: h, padding: "0 28px 0 var(--space-5)",
            background: "var(--bg-sunken)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-md)", color: "var(--text-primary)",
            fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: "var(--text-md)",
            outline: "none", cursor: "pointer", ...style,
          }}
          {...rest}
        >
          {options.map((o) => (
            <option key={String(o.value)} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span style={{ position: "absolute", right: 9, color: "var(--text-muted)", pointerEvents: "none" }}>
          <Icon name="chevron-down" size={14} />
        </span>
      </span>
      {hint ? <span style={{ font: "var(--type-meta)", color: "var(--text-muted)" }}>{hint}</span> : null}
    </label>
  );
}
