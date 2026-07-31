import React from "react";
import { STATUS_TOKENS } from "./StatusPill.jsx";

/** A bare health dot for dense tree rows where a full pill would not fit. */
export function HealthDot({ status = "unknown", size = 7, title, style, ...rest }) {
  const t = STATUS_TOKENS[status] || STATUS_TOKENS.unknown;
  return (
    <span
      title={title || status}
      style={{
        width: size, height: size, borderRadius: "50%", background: t.color, flex: "none",
        boxShadow: status === "running" ? "0 0 0 3px rgba(47,191,143,0.14)" : "none",
        animation: status === "unknown" ? "cw-pulse 1.6s var(--ease-in-out) infinite" : "none",
        ...style,
      }}
      {...rest}
    />
  );
}
