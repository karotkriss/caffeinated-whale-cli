import React from "react";

/** A fact row: a stable label and the value the tool actually measured. */
export function KeyValue({ label, value, mono = true, tone = "default", hint, style, ...rest }) {
  const color =
    tone === "muted" ? "var(--text-muted)" :
    tone === "accent" ? "var(--text-accent)" :
    tone === "danger" ? "var(--text-danger)" : "var(--text-primary)";
  const unmeasured = value == null || value === "";
  return (
    <div
      style={{ display: "grid", gridTemplateColumns: "132px minmax(0,1fr)", gap: "var(--space-6)", alignItems: "baseline", padding: "5px 0", ...style }}
      {...rest}
    >
      <span style={{ font: "var(--type-meta)", color: "var(--text-muted)" }}>{label}</span>
      <span style={{ minWidth: 0 }}>
        <span
          style={{
            fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: "var(--text-sm)",
            color: unmeasured ? "var(--text-disabled)" : color, wordBreak: "break-word",
          }}
        >
          {unmeasured ? "—" : value}
        </span>
        {hint ? <span style={{ display: "block", font: "var(--type-meta)", fontSize: "var(--text-2xs)", color: "var(--text-muted)", marginTop: 1 }}>{hint}</span> : null}
      </span>
    </div>
  );
}
