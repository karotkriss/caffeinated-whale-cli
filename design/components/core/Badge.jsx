import React from "react";

const TONES = {
  neutral: { color: "var(--text-secondary)", bg: "var(--bg-hover)" },
  accent: { color: "var(--cw-whale-300)", bg: "var(--accent-soft)" },
  running: { color: "var(--cw-status-running)", bg: "rgba(47,191,143,0.14)" },
  online: { color: "var(--cw-status-online)", bg: "rgba(0,160,208,0.14)" },
  degraded: { color: "var(--cw-status-degraded)", bg: "rgba(232,178,58,0.14)" },
  offline: { color: "var(--cw-status-offline)", bg: "rgba(228,87,76,0.14)" },
  unknown: { color: "var(--cw-status-unknown)", bg: "rgba(124,138,165,0.14)" },
  magenta: { color: "var(--cw-term-magenta)", bg: "rgba(185,139,227,0.14)" },
};

/** A small tinted count or classifier. Not a status verdict — that is StatusPill. */
export function Badge({ tone = "neutral", uppercase = true, children, style, ...rest }) {
  const t = TONES[tone] || TONES.neutral;
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", height: 18, padding: "0 6px",
        borderRadius: "var(--radius-sm)", background: t.bg, color: t.color,
        font: "var(--type-label)", fontSize: "var(--text-2xs)",
        letterSpacing: uppercase ? "var(--tracking-caps)" : "var(--tracking-normal)",
        textTransform: uppercase ? "uppercase" : "none", whiteSpace: "nowrap",
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
