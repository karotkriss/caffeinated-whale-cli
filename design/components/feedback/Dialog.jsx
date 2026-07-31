import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** A modal. In this product a modal means a decision with consequences. */
export function Dialog({ open = true, title, subtitle, tone = "default", onClose, footer, width = 480, children, style, ...rest }) {
  if (!open) return null;
  const danger = tone === "danger";
  return (
    <div
      style={{
        position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
        background: "var(--bg-scrim)", backdropFilter: "blur(2px)", zIndex: 50, ...style,
      }}
      {...rest}
    >
      <div
        role="dialog"
        aria-modal="true"
        style={{
          width, maxWidth: "calc(100% - 48px)", background: "var(--bg-overlay)",
          border: "1px solid " + (danger ? "rgba(228,87,76,0.4)" : "var(--border-default)"),
          borderRadius: "var(--radius-xl)", boxShadow: "var(--shadow-lg)", overflow: "hidden",
        }}
      >
        <header style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-5)", padding: "var(--space-7) var(--space-7) var(--space-5)" }}>
          {danger ? <span style={{ color: "var(--text-danger)", marginTop: 2 }}><Icon name="triangle-alert" size={18} /></span> : null}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ font: "var(--type-heading)", color: "var(--text-primary)" }}>{title}</div>
            {subtitle ? <div style={{ font: "var(--type-meta)", color: "var(--text-muted)", marginTop: 3, fontFamily: "var(--font-mono)" }}>{subtitle}</div> : null}
          </div>
          {onClose ? (
            <button type="button" onClick={onClose} aria-label="Close" style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, display: "flex" }}>
              <Icon name="x" size={16} />
            </button>
          ) : null}
        </header>
        <div style={{ padding: "0 var(--space-7) var(--space-7)", font: "var(--type-body)", color: "var(--text-secondary)", display: "flex", flexDirection: "column", gap: "var(--space-6)" }}>
          {children}
        </div>
        {footer ? (
          <footer style={{ display: "flex", justifyContent: "flex-end", gap: "var(--space-4)", padding: "var(--space-6) var(--space-7)", borderTop: "1px solid var(--border-subtle)", background: "var(--bg-sunken)" }}>
            {footer}
          </footer>
        ) : null}
      </div>
    </div>
  );
}
