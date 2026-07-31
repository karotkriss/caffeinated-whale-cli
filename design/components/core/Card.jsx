import React from "react";

/** A bordered surface with an optional header row. The system's only container chrome. */
export function Card({ title, subtitle, actions, footer, padded = true, tone = "default", children, style, ...rest }) {
  const border = tone === "danger" ? "1px solid rgba(228,87,76,0.4)" : "1px solid var(--border-subtle)";
  return (
    <section
      style={{
        display: "flex", flexDirection: "column", background: "var(--bg-surface)",
        border, borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-sm)",
        overflow: "hidden", ...style,
      }}
      {...rest}
    >
      {title || actions ? (
        <header style={{ display: "flex", alignItems: "center", gap: "var(--space-6)", padding: "var(--space-5) var(--space-6)", borderBottom: "1px solid var(--border-subtle)" }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ font: "var(--type-body-strong)", color: "var(--text-primary)" }}>{title}</div>
            {subtitle ? <div style={{ font: "var(--type-meta)", color: "var(--text-muted)", marginTop: 2 }}>{subtitle}</div> : null}
          </div>
          {actions ? <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>{actions}</div> : null}
        </header>
      ) : null}
      <div style={{ padding: padded ? "var(--space-6)" : 0, flex: 1, minHeight: 0 }}>{children}</div>
      {footer ? (
        <footer style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: "var(--space-3)", padding: "var(--space-5) var(--space-6)", borderTop: "1px solid var(--border-subtle)", background: "var(--bg-sunken)" }}>
          {footer}
        </footer>
      ) : null}
    </section>
  );
}
