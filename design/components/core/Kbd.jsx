import React from "react";

/** A key cap. The desktop app is keyboard-first and says so in its chrome. */
export function Kbd({ children, style, ...rest }) {
  return (
    <kbd
      style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 18, height: 18,
        padding: "0 5px", borderRadius: "var(--radius-xs)", border: "1px solid var(--border-default)",
        borderBottomWidth: 2, background: "var(--bg-surface-raised)", color: "var(--text-secondary)",
        fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", lineHeight: 1, ...style,
      }}
      {...rest}
    >
      {children}
    </kbd>
  );
}
