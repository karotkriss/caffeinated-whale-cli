import React from "react";
import { Icon } from "../icons/Icon.jsx";

/**
 * The narrow right-hand rail of Tier A actions.
 * A disabled action always says WHY, because a silently dead button is the defect
 * this rail exists to avoid.
 */
export function ActionRail({ groups = [], onAction, width = "var(--action-rail-width)", style, ...rest }) {
  return (
    <aside
      style={{
        width, flex: "none", display: "flex", flexDirection: "column", gap: "var(--space-8)",
        padding: "var(--space-7) var(--space-6)", borderLeft: "1px solid var(--border-subtle)",
        background: "var(--bg-surface)", overflowY: "auto", ...style,
      }}
      {...rest}
    >
      {groups.map((g) => (
        <div key={g.title} style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          <div style={{ font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase", color: "var(--text-muted)", padding: "0 var(--space-2) var(--space-2)" }}>
            {g.title}
          </div>
          {g.actions.map((a) => (
            <RailButton key={a.id} action={a} onAction={onAction} />
          ))}
        </div>
      ))}
    </aside>
  );
}

function RailButton({ action, onAction }) {
  const [hover, setHover] = React.useState(false);
  const off = !!action.disabledReason;
  const danger = action.tone === "danger";
  return (
    <button
      type="button"
      title={action.disabledReason || undefined}
      disabled={off}
      onClick={() => onAction && onAction(action.id)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex", alignItems: "center", gap: "var(--space-5)", width: "100%",
        height: "var(--control-height-lg)", padding: "0 var(--space-5)", textAlign: "left",
        borderRadius: "var(--radius-md)", border: "1px solid transparent", cursor: off ? "not-allowed" : "pointer",
        background: off ? "transparent" : hover ? (danger ? "var(--danger-soft)" : "var(--bg-hover)") : "transparent",
        color: off ? "var(--text-disabled)" : danger ? "var(--text-danger)" : "var(--text-primary)",
        font: "var(--type-body)", transition: "var(--transition-control)",
      }}
    >
      <Icon name={action.icon} size={15} />
      <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{action.label}</span>
      {off ? <span style={{ font: "var(--type-meta)", fontSize: "var(--text-2xs)", color: "var(--text-disabled)" }}>{action.disabledHint}</span> : null}
    </button>
  );
}
