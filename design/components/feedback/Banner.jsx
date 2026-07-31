import React from "react";
import { Icon } from "../icons/Icon.jsx";

const TONES = {
  info: { icon: "info", color: "var(--cw-whale-300)", bg: "rgba(0,160,208,0.10)", border: "rgba(0,160,208,0.32)" },
  success: { icon: "circle-check", color: "var(--cw-status-running)", bg: "rgba(47,191,143,0.10)", border: "rgba(47,191,143,0.32)" },
  warn: { icon: "triangle-alert", color: "var(--cw-status-degraded)", bg: "rgba(232,178,58,0.10)", border: "rgba(232,178,58,0.32)" },
  danger: { icon: "circle-alert", color: "var(--cw-status-offline)", bg: "rgba(228,87,76,0.10)", border: "rgba(228,87,76,0.34)" },
};

/** A typed message with an actionable hint — the pixel form of the CLI's error envelope. */
export function Banner({ tone = "info", title, children, hint, code, actions, style, ...rest }) {
  const t = TONES[tone] || TONES.info;
  return (
    <div
      style={{
        display: "flex", gap: "var(--space-5)", padding: "var(--space-6)",
        background: t.bg, border: "1px solid " + t.border, borderRadius: "var(--radius-md)", ...style,
      }}
      {...rest}
    >
      <span style={{ color: t.color, marginTop: 1 }}><Icon name={t.icon} size={16} /></span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {title ? <div style={{ font: "var(--type-body-strong)", color: "var(--text-primary)" }}>{title}</div> : null}
        {children ? <div style={{ font: "var(--type-body)", color: "var(--text-secondary)", marginTop: title ? 3 : 0 }}>{children}</div> : null}
        {hint ? (
          <div style={{ marginTop: "var(--space-4)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: t.color }}>
            help: {hint}
          </div>
        ) : null}
        {actions ? <div style={{ display: "flex", gap: "var(--space-3)", marginTop: "var(--space-5)" }}>{actions}</div> : null}
      </div>
      {code ? (
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", color: "var(--text-muted)", alignSelf: "flex-start" }}>{code}</span>
      ) : null}
    </div>
  );
}
