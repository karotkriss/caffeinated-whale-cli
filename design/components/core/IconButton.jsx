import React from "react";
import { Icon } from "../icons/Icon.jsx";

const SIZES = { sm: 24, md: 30, lg: 36 };

/** A square, label-less control for window chrome, row affordances and toolbars. */
export function IconButton({ icon, size = "md", variant = "ghost", disabled = false, title, style, ...rest }) {
  const [hover, setHover] = React.useState(false);
  const px = SIZES[size];
  const base =
    variant === "secondary"
      ? { background: "var(--bg-surface-raised)", border: "1px solid var(--border-default)", color: "var(--text-primary)" }
      : variant === "danger"
      ? { background: "transparent", border: "1px solid transparent", color: "var(--text-danger)" }
      : { background: "transparent", border: "1px solid transparent", color: "var(--text-secondary)" };
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        width: px, height: px, display: "inline-flex", alignItems: "center", justifyContent: "center",
        borderRadius: "var(--radius-md)", cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.4 : 1, transition: "var(--transition-control)",
        ...base,
        ...(hover && !disabled
          ? { background: variant === "danger" ? "var(--danger-soft)" : "var(--bg-hover)", color: variant === "danger" ? "var(--text-danger)" : "var(--text-primary)" }
          : null),
        ...style,
      }}
      {...rest}
    >
      <Icon name={icon} size={size === "sm" ? 14 : 16} />
    </button>
  );
}
