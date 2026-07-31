import React from "react";
import { Icon } from "../icons/Icon.jsx";
import { HealthDot } from "../status/HealthDot.jsx";

/** One row of the fleet tree: instance → bench → process, all the same row shape. */
export function TreeItem({
  label, depth = 0, icon, status, expandable = false, expanded = false, selected = false,
  meta, mono = true, onToggle, onClick, style, ...rest
}) {
  const [hover, setHover] = React.useState(false);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex", alignItems: "center", gap: "var(--space-4)",
        height: "var(--row-height)", paddingRight: "var(--space-5)",
        paddingLeft: 8 + depth * 14, cursor: "pointer", userSelect: "none",
        background: selected ? "var(--bg-selected)" : hover ? "var(--bg-hover)" : "transparent",
        boxShadow: selected ? "var(--ring-selected)" : "none",
        color: selected ? "var(--text-primary)" : "var(--text-secondary)",
        transition: "background-color var(--duration-instant) var(--ease-out)", ...style,
      }}
      {...rest}
    >
      <span
        onClick={(e) => { if (expandable && onToggle) { e.stopPropagation(); onToggle(); } }}
        style={{ width: 12, flex: "none", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", opacity: expandable ? 1 : 0 }}
      >
        <Icon name={expanded ? "chevron-down" : "chevron-right"} size={12} />
      </span>
      {icon ? <span style={{ color: selected ? "var(--text-accent)" : "var(--text-muted)", flex: "none" }}><Icon name={icon} size={14} /></span> : null}
      <span
        style={{
          flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: "var(--text-sm)",
          fontWeight: selected ? "var(--weight-medium)" : "var(--weight-regular)",
        }}
      >
        {label}
      </span>
      {meta ? <span style={{ font: "var(--type-meta)", fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{meta}</span> : null}
      {status ? <HealthDot status={status} size={6} title={status} /> : null}
    </div>
  );
}
