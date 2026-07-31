import React from "react";
import { Icon } from "../icons/Icon.jsx";
import { HealthDot } from "./HealthDot.jsx";

export const PROCESS_GRID = "16px minmax(84px,1fr) 72px 56px 56px 44px 56px";

const STATE_COLOR = {
  RUNNING: "var(--cw-status-running)",
  STARTING: "var(--cw-status-degraded)",
  BACKOFF: "var(--cw-status-degraded)",
  STOPPED: "var(--cw-status-offline)",
  FATAL: "var(--cw-status-offline)",
  EXITED: "var(--cw-status-offline)",
  UNKNOWN: "var(--cw-status-unknown)",
};

/** One supervisord program: its state, pid and the volatile numbers, laid out as a fixed grid. */
export function ProcessRow({ name, state = "UNKNOWN", pid, uptime, cpu, rss, selected = false, onClick, style, ...rest }) {
  const [hover, setHover] = React.useState(false);
  const color = STATE_COLOR[state] || STATE_COLOR.UNKNOWN;
  const dim = { color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", textAlign: "right" };
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "grid", gridTemplateColumns: PROCESS_GRID,
        alignItems: "center", gap: "var(--space-4)", height: "var(--row-height)",
        padding: "0 var(--space-6)", cursor: onClick ? "pointer" : "default",
        background: selected ? "var(--bg-selected)" : hover && onClick ? "var(--bg-hover)" : "transparent",
        boxShadow: selected ? "var(--ring-selected)" : "none",
        borderBottom: "1px solid var(--border-subtle)", ...style,
      }}
      {...rest}
    >
      <HealthDot status={state === "RUNNING" ? "running" : state === "STARTING" || state === "BACKOFF" ? "degraded" : state === "UNKNOWN" ? "unknown" : "offline"} size={6} title={state} />
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color, letterSpacing: "0.02em" }}>{state}</span>
      <span style={dim}>{pid == null ? "pid —" : "pid " + pid}</span>
      <span style={dim}>{uptime || "—"}</span>
      <span style={dim}>{cpu == null ? "—" : cpu + "%"}</span>
      <span style={dim}>{rss || "—"}</span>
    </div>
  );
}
