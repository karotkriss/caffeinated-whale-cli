import React from "react";
import { Icon } from "../icons/Icon.jsx";

const HEIGHT = { sm: "var(--control-height-sm)", md: "var(--control-height)", lg: "var(--control-height-lg)" };
const PAD = { sm: "0 8px", md: "0 12px", lg: "0 16px" };
const FONT_SIZE = { sm: "var(--text-sm)", md: "var(--text-md)", lg: "var(--text-md)" };

const VARIANTS = {
  primary: { background: "var(--accent)", color: "var(--accent-fg)", border: "1px solid transparent" },
  secondary: { background: "var(--bg-surface-raised)", color: "var(--text-primary)", border: "1px solid var(--border-default)" },
  ghost: { background: "transparent", color: "var(--text-secondary)", border: "1px solid transparent" },
  subtle: { background: "var(--accent-soft)", color: "var(--text-accent)", border: "1px solid transparent" },
  danger: { background: "var(--danger)", color: "var(--danger-fg)", border: "1px solid transparent" },
};

const HOVER = {
  primary: { background: "var(--accent-hover)" },
  secondary: { background: "var(--bg-hover)", borderColor: "var(--border-strong)" },
  ghost: { background: "var(--bg-hover)", color: "var(--text-primary)" },
  subtle: { background: "rgba(0,160,208,0.22)" },
  danger: { background: "var(--danger-hover)" },
};

/** The one clickable action in this system. Tier A verbs use primary; everything destructive uses danger. */
export function Button({
  variant = "secondary",
  size = "md",
  icon,
  iconRight,
  loading = false,
  disabled = false,
  fullWidth = false,
  type = "button",
  style,
  children,
  ...rest
}) {
  const [hover, setHover] = React.useState(false);
  const [active, setActive] = React.useState(false);
  const off = disabled || loading;
  return (
    <button
      type={type}
      disabled={off}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => { setHover(false); setActive(false); }}
      onMouseDown={() => setActive(true)}
      onMouseUp={() => setActive(false)}
      style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "var(--space-3)",
        height: HEIGHT[size], padding: PAD[size], width: fullWidth ? "100%" : undefined,
        font: "var(--type-body-strong)", fontSize: FONT_SIZE[size], fontFamily: "var(--font-sans)",
        borderRadius: "var(--radius-md)", cursor: off ? "not-allowed" : "pointer",
        opacity: off ? 0.42 : 1, whiteSpace: "nowrap", transition: "var(--transition-control)",
        transform: active && !off ? "translateY(0.5px)" : "none",
        ...VARIANTS[variant],
        ...(hover && !off ? HOVER[variant] : null),
        ...style,
      }}
      {...rest}
    >
      {loading ? (
        <span style={{ display: "block", animation: "cw-spin 700ms linear infinite" }}>
          <Icon name="loader-circle" size={size === "sm" ? 13 : 15} />
        </span>
      ) : icon ? (
        <Icon name={icon} size={size === "sm" ? 13 : 15} />
      ) : null}
      {children}
      {iconRight ? <Icon name={iconRight} size={size === "sm" ? 13 : 15} /> : null}
    </button>
  );
}
