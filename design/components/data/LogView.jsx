import React from "react";

const LEVELS = {
  error: "var(--cw-term-red)",
  warn: "var(--cw-term-yellow)",
  info: "var(--cw-term-fg)",
  debug: "var(--cw-term-dim)",
  success: "var(--cw-term-green)",
  command: "var(--cw-term-cyan)",
};

/** A bounded log tail on the terminal surface. Lines are pre-parsed; this only paints. */
export function LogView({ lines = [], showLineNumbers = true, height, style, ...rest }) {
  return (
    <div
      style={{
        background: "var(--cw-term-bg)", border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)", overflow: "auto", height,
        fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.65,
        padding: "var(--space-5) 0", ...style,
      }}
      {...rest}
    >
      {lines.map((l, i) => (
        <div
          key={i}
          style={{
            display: "grid", gridTemplateColumns: showLineNumbers ? "40px minmax(0,1fr)" : "minmax(0,1fr)",
            gap: "var(--space-5)", padding: "0 var(--space-6)", whiteSpace: "pre-wrap", wordBreak: "break-word",
          }}
        >
          {showLineNumbers ? <span style={{ color: "var(--cw-term-dim)", textAlign: "right", userSelect: "none" }}>{i + 1}</span> : null}
          <span style={{ color: LEVELS[l.level] || LEVELS.info }}>
            {l.time ? <span style={{ color: "var(--cw-term-dim)" }}>{l.time} </span> : null}
            {l.text}
          </span>
        </div>
      ))}
    </div>
  );
}
