import React from "react";
import { Icon } from "../icons/Icon.jsx";
import { Kbd } from "../core/Kbd.jsx";

/** The fleet / cache search box. Its results come from the same cache `cwcli where` reads. */
export function SearchField({ value, onChange, placeholder = "Search apps and sites…", shortcut = "⌘K", style, ...rest }) {
  const [focus, setFocus] = React.useState(false);
  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: "var(--space-4)", height: "var(--control-height)",
        padding: "0 var(--space-4) 0 var(--space-5)", background: "var(--bg-sunken)",
        border: "1px solid " + (focus ? "var(--border-accent)" : "var(--border-default)"),
        borderRadius: "var(--radius-md)", boxShadow: focus ? "var(--ring-focus)" : "none",
        transition: "var(--transition-control)", ...style,
      }}
    >
      <span style={{ color: "var(--text-muted)" }}><Icon name="search" size={14} /></span>
      <input
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        onFocus={() => setFocus(true)}
        onBlur={() => setFocus(false)}
        style={{ flex: 1, minWidth: 0, background: "none", border: "none", outline: "none", color: "var(--text-primary)", fontFamily: "var(--font-sans)", fontSize: "var(--text-md)" }}
        {...rest}
      />
      {shortcut && !focus ? <Kbd>{shortcut}</Kbd> : null}
    </div>
  );
}
