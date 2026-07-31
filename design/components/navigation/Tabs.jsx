import React from "react";
import { Icon } from "../icons/Icon.jsx";

/** Underlined tabs for the detail pane: Overview, Processes, Logs, Apps, Sites. */
export function Tabs({ items = [], value, onChange, style, ...rest }) {
  return (
    <div
      role="tablist"
      style={{ display: "flex", alignItems: "stretch", gap: "var(--space-2)", height: "var(--toolbar-height)", borderBottom: "1px solid var(--border-subtle)", padding: "0 var(--space-6)", ...style }}
      {...rest}
    >
      {items.map((it) => {
        const on = it.value === value;
        return (
          <button
            key={it.value}
            role="tab"
            aria-selected={on}
            onClick={() => onChange && onChange(it.value)}
            style={{
              display: "inline-flex", alignItems: "center", gap: "var(--space-3)", padding: "0 var(--space-5)",
              background: "none", border: "none", cursor: "pointer",
              color: on ? "var(--text-primary)" : "var(--text-muted)",
              font: "var(--type-body-strong)", fontSize: "var(--text-md)",
              boxShadow: on ? "inset 0 -2px 0 var(--accent)" : "none",
              transition: "var(--transition-control)",
            }}
          >
            {it.icon ? <Icon name={it.icon} size={14} /> : null}
            {it.label}
            {it.count != null ? (
              <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>{it.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
