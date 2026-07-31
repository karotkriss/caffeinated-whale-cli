import React from "react";

const TIERS = {
  INSTANT: { color: "var(--cw-term-magenta)", hint: "Docker event stream" },
  FAST: { color: "var(--cw-term-cyan)", hint: "health poll" },
  ACTION: { color: "var(--cw-status-running)", hint: "you asked for this" },
  ERROR: { color: "var(--cw-status-offline)", hint: "failed" },
};

/** One line of the delta log: which tier saw the change, on what, and what it became. */
export function EventRow({ tier = "FAST", project, message, time, fresh = false, style, ...rest }) {
  const t = TIERS[tier] || TIERS.FAST;
  return (
    <div
      style={{
        display: "grid", gridTemplateColumns: "62px minmax(0,150px) minmax(0,1fr) auto",
        gap: "var(--space-5)", alignItems: "baseline", padding: "5px var(--space-6)",
        fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.4,
        animation: fresh ? "cw-delta-in 900ms var(--ease-out) 1" : "none", ...style,
      }}
      {...rest}
    >
      <span style={{ color: t.color, fontWeight: "var(--weight-medium)", letterSpacing: "0.04em" }}>{tier}</span>
      <span style={{ color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{project}</span>
      <span style={{ color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{message}</span>
      <span style={{ color: "var(--cw-term-dim)" }}>{time}</span>
    </div>
  );
}
