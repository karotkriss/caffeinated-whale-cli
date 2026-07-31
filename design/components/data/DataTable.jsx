import React from "react";

/**
 * The pixel form of a Rich table. Column tints follow the CLI's own convention:
 * the name column is cyan, the status column magenta, ports green.
 */
export function DataTable({ columns = [], rows = [], caption, onRowClick, selectedIndex, style, ...rest }) {
  const template = columns.map((c) => c.width || "minmax(0,1fr)").join(" ");
  return (
    <div style={{ display: "flex", flexDirection: "column", minWidth: 0, ...style }} {...rest}>
      {caption ? (
        <div style={{ font: "var(--type-body-strong)", color: "var(--text-primary)", padding: "var(--space-5) var(--space-6)" }}>{caption}</div>
      ) : null}
      <div
        style={{
          display: "grid", gridTemplateColumns: template, gap: "var(--space-6)",
          padding: "0 var(--space-6)", height: 26, alignItems: "center",
          borderBottom: "1px solid var(--border-default)",
          font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase",
          color: "var(--text-muted)",
        }}
      >
        {columns.map((c) => (
          <span key={c.key} style={{ textAlign: c.align || "left" }}>{c.header}</span>
        ))}
      </div>
      {rows.map((r, i) => (
        <Row key={i} columns={columns} row={r} template={template} onClick={onRowClick && (() => onRowClick(r, i))} selected={selectedIndex === i} />
      ))}
    </div>
  );
}

const TINTS = { cyan: "var(--cw-term-cyan)", magenta: "var(--cw-term-magenta)", green: "var(--cw-term-green)", yellow: "var(--cw-term-yellow)", red: "var(--cw-term-red)", dim: "var(--cw-term-dim)" };

function Row({ columns, row, template, onClick, selected }) {
  const [hover, setHover] = React.useState(false);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "grid", gridTemplateColumns: template, gap: "var(--space-6)",
        padding: "0 var(--space-6)", minHeight: "var(--row-height)", alignItems: "center",
        borderBottom: "1px solid var(--border-subtle)", cursor: onClick ? "pointer" : "default",
        background: selected ? "var(--bg-selected)" : hover && onClick ? "var(--bg-hover)" : "transparent",
      }}
    >
      {columns.map((c) => (
        <span
          key={c.key}
          style={{
            textAlign: c.align || "left", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            fontFamily: c.mono === false ? "var(--font-sans)" : "var(--font-mono)",
            fontSize: "var(--text-sm)", color: TINTS[c.tint] || "var(--text-primary)",
          }}
        >
          {row[c.key]}
        </span>
      ))}
    </div>
  );
}
