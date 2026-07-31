import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** A consent checkbox. Destructive dialogs use it to gate the confirm button. */
export function Checkbox({ checked = false, onChange, label, hint, disabled = false, style, ...rest }) {
  return (
    <label style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-5)", cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, ...style }}>
      <input type="checkbox" checked={checked} onChange={onChange} disabled={disabled} style={{ position: "absolute", opacity: 0, width: 0, height: 0 }} {...rest} />
      <span
        style={{
          width: 16, height: 16, flex: "none", marginTop: 1, display: "flex", alignItems: "center", justifyContent: "center",
          borderRadius: "var(--radius-xs)", border: "1px solid " + (checked ? "var(--accent)" : "var(--border-strong)"),
          background: checked ? "var(--accent)" : "var(--bg-sunken)", color: "var(--accent-fg)",
          transition: "var(--transition-control)",
        }}
      >
        {checked ? <Icon name="check" size={12} strokeWidth={2.5} /> : null}
      </span>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", font: "var(--type-body)", color: "var(--text-primary)" }}>{label}</span>
        {hint ? <span style={{ display: "block", font: "var(--type-meta)", color: "var(--text-muted)", marginTop: 2 }}>{hint}</span> : null}
      </span>
    </label>
  );
}
