import React from "react";

export const STATUS_TOKENS = {
  running: { label: "running", color: "var(--cw-status-running)", bg: "rgba(47,191,143,0.13)" },
  online: { label: "online", color: "var(--cw-status-online)", bg: "rgba(0,160,208,0.13)" },
  degraded: { label: "degraded", color: "var(--cw-status-degraded)", bg: "rgba(232,178,58,0.13)" },
  offline: { label: "offline", color: "var(--cw-status-offline)", bg: "rgba(228,87,76,0.13)" },
  unknown: { label: "unknown", color: "var(--cw-status-unknown)", bg: "rgba(124,138,165,0.13)" },
};

/**
 * The health verdict, exactly as core.status / core.fleet spell it.
 * "unknown" is a state, not a default: it renders grey and says so, never green.
 */
export function StatusPill({ status = "unknown", size = "md", showDot = true, label, style, ...rest }) {
  const t = STATUS_TOKENS[status] || STATUS_TOKENS.unknown;
  const sm = size === "sm";
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: sm ? 5 : 6, height: sm ? 18 : 22,
        padding: sm ? "0 7px 0 6px" : "0 9px 0 8px", borderRadius: "var(--radius-pill)",
        background: t.bg, color: t.color, fontFamily: "var(--font-mono)",
        fontSize: sm ? "var(--text-2xs)" : "var(--text-xs)", fontWeight: "var(--weight-medium)",
        letterSpacing: "0.01em", whiteSpace: "nowrap", ...style,
      }}
      {...rest}
    >
      {showDot ? (
        <span
          style={{
            width: sm ? 5 : 6, height: sm ? 5 : 6, borderRadius: "50%", background: "currentColor",
            animation: status === "unknown" ? "cw-pulse 1.6s var(--ease-in-out) infinite" : "none",
          }}
        />
      ) : null}
      {label || t.label}
    </span>
  );
}
