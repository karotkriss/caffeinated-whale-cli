/* @ds-bundle: {"format":4,"namespace":"CaffeinatedWhaleDesignSystem_2d9598","components":[{"name":"Badge","sourcePath":"components/core/Badge.jsx"},{"name":"Button","sourcePath":"components/core/Button.jsx"},{"name":"Card","sourcePath":"components/core/Card.jsx"},{"name":"IconButton","sourcePath":"components/core/IconButton.jsx"},{"name":"Kbd","sourcePath":"components/core/Kbd.jsx"},{"name":"Tag","sourcePath":"components/core/Tag.jsx"},{"name":"DataTable","sourcePath":"components/data/DataTable.jsx"},{"name":"KeyValue","sourcePath":"components/data/KeyValue.jsx"},{"name":"LogView","sourcePath":"components/data/LogView.jsx"},{"name":"Banner","sourcePath":"components/feedback/Banner.jsx"},{"name":"Dialog","sourcePath":"components/feedback/Dialog.jsx"},{"name":"EmptyState","sourcePath":"components/feedback/EmptyState.jsx"},{"name":"Spinner","sourcePath":"components/feedback/Spinner.jsx"},{"name":"Checkbox","sourcePath":"components/forms/Checkbox.jsx"},{"name":"Input","sourcePath":"components/forms/Input.jsx"},{"name":"SearchField","sourcePath":"components/forms/SearchField.jsx"},{"name":"Select","sourcePath":"components/forms/Select.jsx"},{"name":"Switch","sourcePath":"components/forms/Switch.jsx"},{"name":"ICON_PATHS","sourcePath":"components/icons/Icon.jsx"},{"name":"ICON_NAMES","sourcePath":"components/icons/Icon.jsx"},{"name":"Icon","sourcePath":"components/icons/Icon.jsx"},{"name":"ActionRail","sourcePath":"components/navigation/ActionRail.jsx"},{"name":"Tabs","sourcePath":"components/navigation/Tabs.jsx"},{"name":"TreeItem","sourcePath":"components/navigation/TreeItem.jsx"},{"name":"EventRow","sourcePath":"components/status/EventRow.jsx"},{"name":"HealthDot","sourcePath":"components/status/HealthDot.jsx"},{"name":"PROCESS_GRID","sourcePath":"components/status/ProcessRow.jsx"},{"name":"ProcessRow","sourcePath":"components/status/ProcessRow.jsx"},{"name":"STATUS_TOKENS","sourcePath":"components/status/StatusPill.jsx"},{"name":"StatusPill","sourcePath":"components/status/StatusPill.jsx"}],"sourceHashes":{"components/core/Badge.jsx":"3a159010712e","components/core/Button.jsx":"f5a691067fc0","components/core/Card.jsx":"c1fb455722d1","components/core/IconButton.jsx":"610238816f03","components/core/Kbd.jsx":"26629127b17e","components/core/Tag.jsx":"8656e3439d7f","components/data/DataTable.jsx":"5f8c8db70236","components/data/KeyValue.jsx":"7cc8cccf9570","components/data/LogView.jsx":"d12ae7bd0920","components/feedback/Banner.jsx":"fac0fd937711","components/feedback/Dialog.jsx":"81358cedaaf6","components/feedback/EmptyState.jsx":"3b85b1c2ac99","components/feedback/Spinner.jsx":"1c0addbfc54e","components/forms/Checkbox.jsx":"0c3547ab46a8","components/forms/Input.jsx":"60d54ee26acd","components/forms/SearchField.jsx":"1ce00d2e542a","components/forms/Select.jsx":"4417d7b787fc","components/forms/Switch.jsx":"dcfa75bd552f","components/icons/Icon.jsx":"31f0554f056b","components/navigation/ActionRail.jsx":"d0bae7bd2d76","components/navigation/Tabs.jsx":"46ef0fdf5d84","components/navigation/TreeItem.jsx":"f784c43db31a","components/status/EventRow.jsx":"a49bf235e8f9","components/status/HealthDot.jsx":"2e4e4e046e0c","components/status/ProcessRow.jsx":"95436f284d5e","components/status/StatusPill.jsx":"ccf21d21dda4","ui_kits/desktop/App.jsx":"cdc2cac42831","ui_kits/desktop/DetailPane.jsx":"3ee70d4e6219","ui_kits/desktop/Dialogs.jsx":"7fba8010bd0b","ui_kits/desktop/EventLog.jsx":"f5054d9dc27d","ui_kits/desktop/FleetTree.jsx":"e0991f0d9340","ui_kits/desktop/TitleBar.jsx":"631edbfa48ba","ui_kits/desktop/data.js":"98fb87aeef1c"},"inlinedExternals":[],"unexposedExports":[]} */

(() => {

const __ds_ns = (window.CaffeinatedWhaleDesignSystem_2d9598 = window.CaffeinatedWhaleDesignSystem_2d9598 || {});

const __ds_scope = {};

(__ds_ns.__errors = __ds_ns.__errors || []);

// components/core/Badge.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const TONES = {
  neutral: {
    color: "var(--text-secondary)",
    bg: "var(--bg-hover)"
  },
  accent: {
    color: "var(--cw-whale-300)",
    bg: "var(--accent-soft)"
  },
  running: {
    color: "var(--cw-status-running)",
    bg: "rgba(47,191,143,0.14)"
  },
  online: {
    color: "var(--cw-status-online)",
    bg: "rgba(0,160,208,0.14)"
  },
  degraded: {
    color: "var(--cw-status-degraded)",
    bg: "rgba(232,178,58,0.14)"
  },
  offline: {
    color: "var(--cw-status-offline)",
    bg: "rgba(228,87,76,0.14)"
  },
  unknown: {
    color: "var(--cw-status-unknown)",
    bg: "rgba(124,138,165,0.14)"
  },
  magenta: {
    color: "var(--cw-term-magenta)",
    bg: "rgba(185,139,227,0.14)"
  }
};

/** A small tinted count or classifier. Not a status verdict — that is StatusPill. */
function Badge({
  tone = "neutral",
  uppercase = true,
  children,
  style,
  ...rest
}) {
  const t = TONES[tone] || TONES.neutral;
  return /*#__PURE__*/React.createElement("span", _extends({
    style: {
      display: "inline-flex",
      alignItems: "center",
      height: 18,
      padding: "0 6px",
      borderRadius: "var(--radius-sm)",
      background: t.bg,
      color: t.color,
      font: "var(--type-label)",
      fontSize: "var(--text-2xs)",
      letterSpacing: uppercase ? "var(--tracking-caps)" : "var(--tracking-normal)",
      textTransform: uppercase ? "uppercase" : "none",
      whiteSpace: "nowrap",
      ...style
    }
  }, rest), children);
}
Object.assign(__ds_scope, { Badge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Badge.jsx", error: String((e && e.message) || e) }); }

// components/core/Card.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A bordered surface with an optional header row. The system's only container chrome. */
function Card({
  title,
  subtitle,
  actions,
  footer,
  padded = true,
  tone = "default",
  children,
  style,
  ...rest
}) {
  const border = tone === "danger" ? "1px solid rgba(228,87,76,0.4)" : "1px solid var(--border-subtle)";
  return /*#__PURE__*/React.createElement("section", _extends({
    style: {
      display: "flex",
      flexDirection: "column",
      background: "var(--bg-surface)",
      border,
      borderRadius: "var(--radius-lg)",
      boxShadow: "var(--shadow-sm)",
      overflow: "hidden",
      ...style
    }
  }, rest), title || actions ? /*#__PURE__*/React.createElement("header", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-6)",
      padding: "var(--space-5) var(--space-6)",
      borderBottom: "1px solid var(--border-subtle)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: 1
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body-strong)",
      color: "var(--text-primary)"
    }
  }, title), subtitle ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)",
      marginTop: 2
    }
  }, subtitle) : null), actions ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-3)"
    }
  }, actions) : null) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      padding: padded ? "var(--space-6)" : 0,
      flex: 1,
      minHeight: 0
    }
  }, children), footer ? /*#__PURE__*/React.createElement("footer", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "flex-end",
      gap: "var(--space-3)",
      padding: "var(--space-5) var(--space-6)",
      borderTop: "1px solid var(--border-subtle)",
      background: "var(--bg-sunken)"
    }
  }, footer) : null);
}
Object.assign(__ds_scope, { Card });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Card.jsx", error: String((e && e.message) || e) }); }

// components/core/Kbd.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A key cap. The desktop app is keyboard-first and says so in its chrome. */
function Kbd({
  children,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("kbd", _extends({
    style: {
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      minWidth: 18,
      height: 18,
      padding: "0 5px",
      borderRadius: "var(--radius-xs)",
      border: "1px solid var(--border-default)",
      borderBottomWidth: 2,
      background: "var(--bg-surface-raised)",
      color: "var(--text-secondary)",
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)",
      lineHeight: 1,
      ...style
    }
  }, rest), children);
}
Object.assign(__ds_scope, { Kbd });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Kbd.jsx", error: String((e && e.message) || e) }); }

// components/data/DataTable.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * The pixel form of a Rich table. Column tints follow the CLI's own convention:
 * the name column is cyan, the status column magenta, ports green.
 */
function DataTable({
  columns = [],
  rows = [],
  caption,
  onRowClick,
  selectedIndex,
  style,
  ...rest
}) {
  const template = columns.map(c => c.width || "minmax(0,1fr)").join(" ");
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "flex",
      flexDirection: "column",
      minWidth: 0,
      ...style
    }
  }, rest), caption ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body-strong)",
      color: "var(--text-primary)",
      padding: "var(--space-5) var(--space-6)"
    }
  }, caption) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: template,
      gap: "var(--space-6)",
      padding: "0 var(--space-6)",
      height: 26,
      alignItems: "center",
      borderBottom: "1px solid var(--border-default)",
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase",
      color: "var(--text-muted)"
    }
  }, columns.map(c => /*#__PURE__*/React.createElement("span", {
    key: c.key,
    style: {
      textAlign: c.align || "left"
    }
  }, c.header))), rows.map((r, i) => /*#__PURE__*/React.createElement(Row, {
    key: i,
    columns: columns,
    row: r,
    template: template,
    onClick: onRowClick && (() => onRowClick(r, i)),
    selected: selectedIndex === i
  })));
}
const TINTS = {
  cyan: "var(--cw-term-cyan)",
  magenta: "var(--cw-term-magenta)",
  green: "var(--cw-term-green)",
  yellow: "var(--cw-term-yellow)",
  red: "var(--cw-term-red)",
  dim: "var(--cw-term-dim)"
};
function Row({
  columns,
  row,
  template,
  onClick,
  selected
}) {
  const [hover, setHover] = React.useState(false);
  return /*#__PURE__*/React.createElement("div", {
    onClick: onClick,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: "grid",
      gridTemplateColumns: template,
      gap: "var(--space-6)",
      padding: "0 var(--space-6)",
      minHeight: "var(--row-height)",
      alignItems: "center",
      borderBottom: "1px solid var(--border-subtle)",
      cursor: onClick ? "pointer" : "default",
      background: selected ? "var(--bg-selected)" : hover && onClick ? "var(--bg-hover)" : "transparent"
    }
  }, columns.map(c => /*#__PURE__*/React.createElement("span", {
    key: c.key,
    style: {
      textAlign: c.align || "left",
      minWidth: 0,
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      fontFamily: c.mono === false ? "var(--font-sans)" : "var(--font-mono)",
      fontSize: "var(--text-sm)",
      color: TINTS[c.tint] || "var(--text-primary)"
    }
  }, row[c.key])));
}
Object.assign(__ds_scope, { DataTable });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/DataTable.jsx", error: String((e && e.message) || e) }); }

// components/data/KeyValue.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A fact row: a stable label and the value the tool actually measured. */
function KeyValue({
  label,
  value,
  mono = true,
  tone = "default",
  hint,
  style,
  ...rest
}) {
  const color = tone === "muted" ? "var(--text-muted)" : tone === "accent" ? "var(--text-accent)" : tone === "danger" ? "var(--text-danger)" : "var(--text-primary)";
  const unmeasured = value == null || value === "";
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "grid",
      gridTemplateColumns: "132px minmax(0,1fr)",
      gap: "var(--space-6)",
      alignItems: "baseline",
      padding: "5px 0",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)"
    }
  }, label), /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: "var(--text-sm)",
      color: unmeasured ? "var(--text-disabled)" : color,
      wordBreak: "break-word"
    }
  }, unmeasured ? "—" : value), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      font: "var(--type-meta)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-muted)",
      marginTop: 1
    }
  }, hint) : null));
}
Object.assign(__ds_scope, { KeyValue });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/KeyValue.jsx", error: String((e && e.message) || e) }); }

// components/data/LogView.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const LEVELS = {
  error: "var(--cw-term-red)",
  warn: "var(--cw-term-yellow)",
  info: "var(--cw-term-fg)",
  debug: "var(--cw-term-dim)",
  success: "var(--cw-term-green)",
  command: "var(--cw-term-cyan)"
};

/** A bounded log tail on the terminal surface. Lines are pre-parsed; this only paints. */
function LogView({
  lines = [],
  showLineNumbers = true,
  height,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      background: "var(--cw-term-bg)",
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md)",
      overflow: "auto",
      height,
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-xs)",
      lineHeight: 1.65,
      padding: "var(--space-5) 0",
      ...style
    }
  }, rest), lines.map((l, i) => /*#__PURE__*/React.createElement("div", {
    key: i,
    style: {
      display: "grid",
      gridTemplateColumns: showLineNumbers ? "40px minmax(0,1fr)" : "minmax(0,1fr)",
      gap: "var(--space-5)",
      padding: "0 var(--space-6)",
      whiteSpace: "pre-wrap",
      wordBreak: "break-word"
    }
  }, showLineNumbers ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--cw-term-dim)",
      textAlign: "right",
      userSelect: "none"
    }
  }, i + 1) : null, /*#__PURE__*/React.createElement("span", {
    style: {
      color: LEVELS[l.level] || LEVELS.info
    }
  }, l.time ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--cw-term-dim)"
    }
  }, l.time, " ") : null, l.text))));
}
Object.assign(__ds_scope, { LogView });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/LogView.jsx", error: String((e && e.message) || e) }); }

// components/forms/Switch.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A persistent preference toggle: auto-inspect, follow logs, keep window on top. */
function Switch({
  checked = false,
  onChange,
  label,
  hint,
  disabled = false,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("label", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-5)",
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.5 : 1,
      ...style
    }
  }, /*#__PURE__*/React.createElement("input", _extends({
    type: "checkbox",
    role: "switch",
    checked: checked,
    onChange: onChange,
    disabled: disabled,
    style: {
      position: "absolute",
      opacity: 0,
      width: 0,
      height: 0
    }
  }, rest)), /*#__PURE__*/React.createElement("span", {
    style: {
      width: 32,
      height: 18,
      flex: "none",
      borderRadius: "var(--radius-pill)",
      padding: 2,
      background: checked ? "var(--accent)" : "var(--bg-active)",
      border: "1px solid " + (checked ? "transparent" : "var(--border-default)"),
      transition: "background-color var(--duration-fast) var(--ease-out)",
      display: "flex",
      justifyContent: checked ? "flex-end" : "flex-start"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: 14,
      height: 14,
      borderRadius: "50%",
      background: checked ? "var(--accent-fg)" : "var(--text-muted)",
      transition: "all var(--duration-fast) var(--ease-out)"
    }
  })), label ? /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      font: "var(--type-body)",
      color: "var(--text-primary)"
    }
  }, label), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      font: "var(--type-meta)",
      color: "var(--text-muted)",
      marginTop: 1
    }
  }, hint) : null) : null);
}
Object.assign(__ds_scope, { Switch });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Switch.jsx", error: String((e && e.message) || e) }); }

// components/icons/Icon.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
// Icon paths copied verbatim from Lucide (https://lucide.dev, ISC licence).
// Source SVGs also live in assets/icons/ — regenerate this map from those files.
const ICON_PATHS = {
  "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"></path>',
  "arrow-up-right": '<path d="M7 7h10v10"></path> <path d="M7 17 17 7"></path>',
  "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"></path> <path d="m3.3 7 8.7 5 8.7-5"></path> <path d="M12 22V12"></path>',
  "check": '<path d="M20 6 9 17l-5-5"></path>',
  "chevron-down": '<path d="m6 9 6 6 6-6"></path>',
  "chevron-right": '<path d="m9 18 6-6-6-6"></path>',
  "circle-alert": '<circle cx="12" cy="12" r="10"></circle> <line x1="12" x2="12" y1="8" y2="12"></line> <line x1="12" x2="12.01" y1="16" y2="16"></line>',
  "circle-check": '<circle cx="12" cy="12" r="10"></circle> <path d="m9 12 2 2 4-4"></path>',
  "circle-dot": '<circle cx="12" cy="12" r="10"></circle> <circle cx="12" cy="12" r="1"></circle>',
  "clock": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 6v6l4 2"></path>',
  "coffee": '<path d="M10 2v2"></path> <path d="M14 2v2"></path> <path d="M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1"></path> <path d="M6 2v2"></path>',
  "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path>',
  "cpu": '<path d="M12 20v2"></path> <path d="M12 2v2"></path> <path d="M17 20v2"></path> <path d="M17 2v2"></path> <path d="M2 12h2"></path> <path d="M2 17h2"></path> <path d="M2 7h2"></path> <path d="M20 12h2"></path> <path d="M20 17h2"></path> <path d="M20 7h2"></path> <path d="M7 20v2"></path> <path d="M7 2v2"></path> <rect x="4" y="4" width="16" height="16" rx="2"></rect> <rect x="8" y="8" width="8" height="8" rx="1"></rect>',
  "database": '<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse> <path d="M3 5V19A9 3 0 0 0 21 19V5"></path> <path d="M3 12A9 3 0 0 0 21 12"></path>',
  "download": '<path d="M12 15V3"></path> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path> <path d="m7 10 5 5 5-5"></path>',
  "ellipsis": '<circle cx="12" cy="12" r="1"></circle> <circle cx="19" cy="12" r="1"></circle> <circle cx="5" cy="12" r="1"></circle>',
  "external-link": '<path d="M15 3h6v6"></path> <path d="M10 14 21 3"></path> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>',
  "file-text": '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"></path> <path d="M14 2v5a1 1 0 0 0 1 1h5"></path> <path d="M10 9H8"></path> <path d="M16 13H8"></path> <path d="M16 17H8"></path>',
  "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"></path>',
  "git-branch": '<path d="M15 6a9 9 0 0 0-9 9V3"></path> <circle cx="18" cy="6" r="3"></circle> <circle cx="6" cy="18" r="3"></circle>',
  "globe": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"></path> <path d="M2 12h20"></path>',
  "hard-drive": '<path d="M10 16h.01"></path> <path d="M2.212 11.577a2 2 0 0 0-.212.896V18a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5.527a2 2 0 0 0-.212-.896L18.55 5.11A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path> <path d="M21.946 12.013H2.054"></path> <path d="M6 16h.01"></path>',
  "info": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 16v-4"></path> <path d="M12 8h.01"></path>',
  "layers": '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"></path> <path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"></path> <path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"></path>',
  "loader-circle": '<path d="M21 12a9 9 0 1 1-6.219-8.56"></path>',
  "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect> <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>',
  "lock-open": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect> <path d="M7 11V7a5 5 0 0 1 9.9-1"></path>',
  "minus": '<path d="M5 12h14"></path>',
  "package": '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"></path> <path d="M12 22V12"></path> <polyline points="3.29 7 12 12 20.71 7"></polyline> <path d="m7.5 4.27 9 5.15"></path>',
  "panel-left": '<rect width="18" height="18" x="3" y="3" rx="2"></rect> <path d="M9 3v18"></path>',
  "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path> <path d="m15 5 4 4"></path>',
  "play": '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"></path>',
  "plus": '<path d="M5 12h14"></path> <path d="M12 5v14"></path>',
  "refresh-cw": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"></path> <path d="M21 3v5h-5"></path> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"></path> <path d="M8 16H3v5"></path>',
  "rotate-cw": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"></path> <path d="M21 3v5h-5"></path>',
  "scroll-text": '<path d="M15 12h-5"></path> <path d="M15 8h-5"></path> <path d="M19 17V5a2 2 0 0 0-2-2H4"></path> <path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"></path>',
  "search": '<path d="m21 21-4.34-4.34"></path> <circle cx="11" cy="11" r="8"></circle>',
  "server": '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"></rect> <rect width="20" height="8" x="2" y="14" rx="2" ry="2"></rect> <line x1="6" x2="6.01" y1="6" y2="6"></line> <line x1="6" x2="6.01" y1="18" y2="18"></line>',
  "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"></path> <circle cx="12" cy="12" r="3"></circle>',
  "square": '<rect width="18" height="18" x="3" y="3" rx="2"></rect>',
  "tag": '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"></path> <circle cx="7.5" cy="7.5" r=".5" fill="currentColor"></circle>',
  "terminal": '<path d="M12 19h8"></path> <path d="m4 17 6-6-6-6"></path>',
  "trash-2": '<path d="M10 11v6"></path> <path d="M14 11v6"></path> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path> <path d="M3 6h18"></path> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>',
  "triangle-alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"></path> <path d="M12 9v4"></path> <path d="M12 17h.01"></path>',
  "upload": '<path d="M12 3v12"></path> <path d="m17 8-5-5-5 5"></path> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>',
  "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"></path>',
  "x": '<path d="M18 6 6 18"></path> <path d="m6 6 12 12"></path>',
  "zap": '<path d="M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z"></path>'
};
const ICON_NAMES = Object.keys(ICON_PATHS);

/** Lucide glyph rendered at the brand's 1.5px stroke, inheriting currentColor. */
function Icon({
  name,
  size = 16,
  strokeWidth = 1.5,
  className = "",
  style,
  ...rest
}) {
  const d = ICON_PATHS[name];
  if (!d) return null;
  return /*#__PURE__*/React.createElement("svg", _extends({
    xmlns: "http://www.w3.org/2000/svg",
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: strokeWidth,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true",
    focusable: "false",
    className: className,
    style: {
      display: "block",
      flex: "none",
      ...style
    },
    dangerouslySetInnerHTML: {
      __html: d
    }
  }, rest));
}
Object.assign(__ds_scope, { ICON_PATHS, ICON_NAMES, Icon });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/icons/Icon.jsx", error: String((e && e.message) || e) }); }

// components/core/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const HEIGHT = {
  sm: "var(--control-height-sm)",
  md: "var(--control-height)",
  lg: "var(--control-height-lg)"
};
const PAD = {
  sm: "0 8px",
  md: "0 12px",
  lg: "0 16px"
};
const FONT_SIZE = {
  sm: "var(--text-sm)",
  md: "var(--text-md)",
  lg: "var(--text-md)"
};
const VARIANTS = {
  primary: {
    background: "var(--accent)",
    color: "var(--accent-fg)",
    border: "1px solid transparent"
  },
  secondary: {
    background: "var(--bg-surface-raised)",
    color: "var(--text-primary)",
    border: "1px solid var(--border-default)"
  },
  ghost: {
    background: "transparent",
    color: "var(--text-secondary)",
    border: "1px solid transparent"
  },
  subtle: {
    background: "var(--accent-soft)",
    color: "var(--text-accent)",
    border: "1px solid transparent"
  },
  danger: {
    background: "var(--danger)",
    color: "var(--danger-fg)",
    border: "1px solid transparent"
  }
};
const HOVER = {
  primary: {
    background: "var(--accent-hover)"
  },
  secondary: {
    background: "var(--bg-hover)",
    borderColor: "var(--border-strong)"
  },
  ghost: {
    background: "var(--bg-hover)",
    color: "var(--text-primary)"
  },
  subtle: {
    background: "rgba(0,160,208,0.22)"
  },
  danger: {
    background: "var(--danger-hover)"
  }
};

/** The one clickable action in this system. Tier A verbs use primary; everything destructive uses danger. */
function Button({
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
  return /*#__PURE__*/React.createElement("button", _extends({
    type: type,
    disabled: off,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => {
      setHover(false);
      setActive(false);
    },
    onMouseDown: () => setActive(true),
    onMouseUp: () => setActive(false),
    style: {
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      gap: "var(--space-3)",
      height: HEIGHT[size],
      padding: PAD[size],
      width: fullWidth ? "100%" : undefined,
      font: "var(--type-body-strong)",
      fontSize: FONT_SIZE[size],
      fontFamily: "var(--font-sans)",
      borderRadius: "var(--radius-md)",
      cursor: off ? "not-allowed" : "pointer",
      opacity: off ? 0.42 : 1,
      whiteSpace: "nowrap",
      transition: "var(--transition-control)",
      transform: active && !off ? "translateY(0.5px)" : "none",
      ...VARIANTS[variant],
      ...(hover && !off ? HOVER[variant] : null),
      ...style
    }
  }, rest), loading ? /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      animation: "cw-spin 700ms linear infinite"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "loader-circle",
    size: size === "sm" ? 13 : 15
  })) : icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: size === "sm" ? 13 : 15
  }) : null, children, iconRight ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: iconRight,
    size: size === "sm" ? 13 : 15
  }) : null);
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Button.jsx", error: String((e && e.message) || e) }); }

// components/core/IconButton.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const SIZES = {
  sm: 24,
  md: 30,
  lg: 36
};

/** A square, label-less control for window chrome, row affordances and toolbars. */
function IconButton({
  icon,
  size = "md",
  variant = "ghost",
  disabled = false,
  title,
  style,
  ...rest
}) {
  const [hover, setHover] = React.useState(false);
  const px = SIZES[size];
  const base = variant === "secondary" ? {
    background: "var(--bg-surface-raised)",
    border: "1px solid var(--border-default)",
    color: "var(--text-primary)"
  } : variant === "danger" ? {
    background: "transparent",
    border: "1px solid transparent",
    color: "var(--text-danger)"
  } : {
    background: "transparent",
    border: "1px solid transparent",
    color: "var(--text-secondary)"
  };
  return /*#__PURE__*/React.createElement("button", _extends({
    type: "button",
    title: title,
    "aria-label": title,
    disabled: disabled,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      width: px,
      height: px,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      borderRadius: "var(--radius-md)",
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.4 : 1,
      transition: "var(--transition-control)",
      ...base,
      ...(hover && !disabled ? {
        background: variant === "danger" ? "var(--danger-soft)" : "var(--bg-hover)",
        color: variant === "danger" ? "var(--text-danger)" : "var(--text-primary)"
      } : null),
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: size === "sm" ? 14 : 16
  }));
}
Object.assign(__ds_scope, { IconButton });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/IconButton.jsx", error: String((e && e.message) || e) }); }

// components/core/Tag.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A monospace chip for a machine value: a port, a bench label, a branch, a site. */
function Tag({
  icon,
  mono = true,
  onRemove,
  children,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("span", _extends({
    style: {
      display: "inline-flex",
      alignItems: "center",
      gap: "var(--space-2)",
      height: 20,
      padding: onRemove ? "0 3px 0 7px" : "0 7px",
      borderRadius: "var(--radius-sm)",
      border: "1px solid var(--border-subtle)",
      background: "var(--bg-hover)",
      color: "var(--text-secondary)",
      fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: "var(--text-xs)",
      whiteSpace: "nowrap",
      ...style
    }
  }, rest), icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 12
  }) : null, children, onRemove ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onRemove,
    "aria-label": "Remove",
    style: {
      display: "flex",
      alignItems: "center",
      padding: 2,
      background: "none",
      border: "none",
      color: "var(--text-muted)",
      cursor: "pointer",
      borderRadius: "var(--radius-xs)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 11
  })) : null);
}
Object.assign(__ds_scope, { Tag });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Tag.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Banner.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const TONES = {
  info: {
    icon: "info",
    color: "var(--cw-whale-300)",
    bg: "rgba(0,160,208,0.10)",
    border: "rgba(0,160,208,0.32)"
  },
  success: {
    icon: "circle-check",
    color: "var(--cw-status-running)",
    bg: "rgba(47,191,143,0.10)",
    border: "rgba(47,191,143,0.32)"
  },
  warn: {
    icon: "triangle-alert",
    color: "var(--cw-status-degraded)",
    bg: "rgba(232,178,58,0.10)",
    border: "rgba(232,178,58,0.32)"
  },
  danger: {
    icon: "circle-alert",
    color: "var(--cw-status-offline)",
    bg: "rgba(228,87,76,0.10)",
    border: "rgba(228,87,76,0.34)"
  }
};

/** A typed message with an actionable hint — the pixel form of the CLI's error envelope. */
function Banner({
  tone = "info",
  title,
  children,
  hint,
  code,
  actions,
  style,
  ...rest
}) {
  const t = TONES[tone] || TONES.info;
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "flex",
      gap: "var(--space-5)",
      padding: "var(--space-6)",
      background: t.bg,
      border: "1px solid " + t.border,
      borderRadius: "var(--radius-md)",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    style: {
      color: t.color,
      marginTop: 1
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: t.icon,
    size: 16
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minWidth: 0
    }
  }, title ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body-strong)",
      color: "var(--text-primary)"
    }
  }, title) : null, children ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body)",
      color: "var(--text-secondary)",
      marginTop: title ? 3 : 0
    }
  }, children) : null, hint ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "var(--space-4)",
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-xs)",
      color: t.color
    }
  }, "help: ", hint) : null, actions ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      gap: "var(--space-3)",
      marginTop: "var(--space-5)"
    }
  }, actions) : null), code ? /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-muted)",
      alignSelf: "flex-start"
    }
  }, code) : null);
}
Object.assign(__ds_scope, { Banner });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Banner.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Dialog.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A modal. In this product a modal means a decision with consequences. */
function Dialog({
  open = true,
  title,
  subtitle,
  tone = "default",
  onClose,
  footer,
  width = 480,
  children,
  style,
  ...rest
}) {
  if (!open) return null;
  const danger = tone === "danger";
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      position: "absolute",
      inset: 0,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      background: "var(--bg-scrim)",
      backdropFilter: "blur(2px)",
      zIndex: 50,
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("div", {
    role: "dialog",
    "aria-modal": "true",
    style: {
      width,
      maxWidth: "calc(100% - 48px)",
      background: "var(--bg-overlay)",
      border: "1px solid " + (danger ? "rgba(228,87,76,0.4)" : "var(--border-default)"),
      borderRadius: "var(--radius-xl)",
      boxShadow: "var(--shadow-lg)",
      overflow: "hidden"
    }
  }, /*#__PURE__*/React.createElement("header", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      gap: "var(--space-5)",
      padding: "var(--space-7) var(--space-7) var(--space-5)"
    }
  }, danger ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-danger)",
      marginTop: 2
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "triangle-alert",
    size: 18
  })) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-heading)",
      color: "var(--text-primary)"
    }
  }, title), subtitle ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)",
      marginTop: 3,
      fontFamily: "var(--font-mono)"
    }
  }, subtitle) : null), onClose ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onClose,
    "aria-label": "Close",
    style: {
      background: "none",
      border: "none",
      color: "var(--text-muted)",
      cursor: "pointer",
      padding: 2,
      display: "flex"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 16
  })) : null), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "0 var(--space-7) var(--space-7)",
      font: "var(--type-body)",
      color: "var(--text-secondary)",
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-6)"
    }
  }, children), footer ? /*#__PURE__*/React.createElement("footer", {
    style: {
      display: "flex",
      justifyContent: "flex-end",
      gap: "var(--space-4)",
      padding: "var(--space-6) var(--space-7)",
      borderTop: "1px solid var(--border-subtle)",
      background: "var(--bg-sunken)"
    }
  }, footer) : null));
}
Object.assign(__ds_scope, { Dialog });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Dialog.jsx", error: String((e && e.message) || e) }); }

// components/feedback/EmptyState.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** The nothing-here surface: says what is missing and gives the one command that fixes it. */
function EmptyState({
  icon = "box",
  title,
  children,
  action,
  command,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      gap: "var(--space-5)",
      padding: "var(--space-12) var(--space-8)",
      textAlign: "center",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-disabled)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 26,
    strokeWidth: 1.25
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-heading)",
      color: "var(--text-primary)"
    }
  }, title), children ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body)",
      color: "var(--text-muted)",
      maxWidth: 400
    }
  }, children) : null, command ? /*#__PURE__*/React.createElement("code", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-sm)",
      color: "var(--text-accent)",
      background: "var(--bg-sunken)",
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-sm)",
      padding: "5px 9px"
    }
  }, command) : null, action ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "var(--space-2)"
    }
  }, action) : null);
}
Object.assign(__ds_scope, { EmptyState });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/EmptyState.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Spinner.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * A spinner with the product's rotating tip line, mirroring the CLI's TipSpinner.
 * Tips are the CLI's own strings — pass them in rather than inventing new ones.
 */
function Spinner({
  label,
  tip,
  size = 16,
  inline = false,
  style,
  ...rest
}) {
  if (inline) {
    return /*#__PURE__*/React.createElement("span", _extends({
      style: {
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-4)",
        color: "var(--text-secondary)",
        ...style
      }
    }, rest), /*#__PURE__*/React.createElement("span", {
      style: {
        display: "block",
        color: "var(--accent)",
        animation: "cw-spin 700ms linear infinite"
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "loader-circle",
      size: size
    })), label);
  }
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "var(--space-5)",
      padding: "var(--space-10) var(--space-7)",
      textAlign: "center",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      color: "var(--accent)",
      animation: "cw-spin 700ms linear infinite"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "loader-circle",
    size: 22
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-body-strong)",
      color: "var(--text-primary)"
    }
  }, label), tip ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)",
      maxWidth: 380
    }
  }, tip) : null);
}
Object.assign(__ds_scope, { Spinner });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Spinner.jsx", error: String((e && e.message) || e) }); }

// components/forms/Checkbox.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A consent checkbox. Destructive dialogs use it to gate the confirm button. */
function Checkbox({
  checked = false,
  onChange,
  label,
  hint,
  disabled = false,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("label", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      gap: "var(--space-5)",
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.5 : 1,
      ...style
    }
  }, /*#__PURE__*/React.createElement("input", _extends({
    type: "checkbox",
    checked: checked,
    onChange: onChange,
    disabled: disabled,
    style: {
      position: "absolute",
      opacity: 0,
      width: 0,
      height: 0
    }
  }, rest)), /*#__PURE__*/React.createElement("span", {
    style: {
      width: 16,
      height: 16,
      flex: "none",
      marginTop: 1,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      borderRadius: "var(--radius-xs)",
      border: "1px solid " + (checked ? "var(--accent)" : "var(--border-strong)"),
      background: checked ? "var(--accent)" : "var(--bg-sunken)",
      color: "var(--accent-fg)",
      transition: "var(--transition-control)"
    }
  }, checked ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "check",
    size: 12,
    strokeWidth: 2.5
  }) : null), /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      font: "var(--type-body)",
      color: "var(--text-primary)"
    }
  }, label), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      display: "block",
      font: "var(--type-meta)",
      color: "var(--text-muted)",
      marginTop: 2
    }
  }, hint) : null));
}
Object.assign(__ds_scope, { Checkbox });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Checkbox.jsx", error: String((e && e.message) || e) }); }

// components/forms/Input.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A single-line text field. Values a machine produced are monospace. */
function Input({
  label,
  hint,
  error,
  icon,
  mono = false,
  size = "md",
  style,
  wrapperStyle,
  ...rest
}) {
  const [focus, setFocus] = React.useState(false);
  const h = size === "sm" ? "var(--control-height-sm)" : size === "lg" ? "var(--control-height-lg)" : "var(--control-height)";
  return /*#__PURE__*/React.createElement("label", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-3)",
      minWidth: 0,
      ...wrapperStyle
    }
  }, label ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-secondary)"
    }
  }, label) : null, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      height: h,
      padding: "0 var(--space-5)",
      background: "var(--bg-sunken)",
      border: "1px solid " + (error ? "var(--danger)" : focus ? "var(--border-accent)" : "var(--border-default)"),
      borderRadius: "var(--radius-md)",
      boxShadow: focus ? "var(--ring-focus)" : "none",
      transition: "var(--transition-control)"
    }
  }, icon ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-muted)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 14
  })) : null, /*#__PURE__*/React.createElement("input", _extends({
    onFocus: () => setFocus(true),
    onBlur: () => setFocus(false),
    style: {
      flex: 1,
      minWidth: 0,
      background: "none",
      border: "none",
      outline: "none",
      color: "var(--text-primary)",
      fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: "var(--text-md)",
      ...style
    }
  }, rest))), error ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-danger)"
    }
  }, error) : hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)"
    }
  }, hint) : null);
}
Object.assign(__ds_scope, { Input });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Input.jsx", error: String((e && e.message) || e) }); }

// components/forms/SearchField.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** The fleet / cache search box. Its results come from the same cache `cwcli where` reads. */
function SearchField({
  value,
  onChange,
  placeholder = "Search apps and sites…",
  shortcut = "⌘K",
  style,
  ...rest
}) {
  const [focus, setFocus] = React.useState(false);
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      height: "var(--control-height)",
      padding: "0 var(--space-4) 0 var(--space-5)",
      background: "var(--bg-sunken)",
      border: "1px solid " + (focus ? "var(--border-accent)" : "var(--border-default)"),
      borderRadius: "var(--radius-md)",
      boxShadow: focus ? "var(--ring-focus)" : "none",
      transition: "var(--transition-control)",
      ...style
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-muted)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "search",
    size: 14
  })), /*#__PURE__*/React.createElement("input", _extends({
    value: value,
    onChange: onChange,
    placeholder: placeholder,
    onFocus: () => setFocus(true),
    onBlur: () => setFocus(false),
    style: {
      flex: 1,
      minWidth: 0,
      background: "none",
      border: "none",
      outline: "none",
      color: "var(--text-primary)",
      fontFamily: "var(--font-sans)",
      fontSize: "var(--text-md)"
    }
  }, rest)), shortcut && !focus ? /*#__PURE__*/React.createElement(__ds_scope.Kbd, null, shortcut) : null);
}
Object.assign(__ds_scope, { SearchField });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/SearchField.jsx", error: String((e && e.message) || e) }); }

// components/forms/Select.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A native select in the system's chrome. Used for bench pickers and log line counts. */
function Select({
  label,
  hint,
  options = [],
  size = "md",
  mono = false,
  style,
  wrapperStyle,
  ...rest
}) {
  const h = size === "sm" ? "var(--control-height-sm)" : "var(--control-height)";
  return /*#__PURE__*/React.createElement("label", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-3)",
      minWidth: 0,
      ...wrapperStyle
    }
  }, label ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-secondary)"
    }
  }, label) : null, /*#__PURE__*/React.createElement("span", {
    style: {
      position: "relative",
      display: "flex",
      alignItems: "center"
    }
  }, /*#__PURE__*/React.createElement("select", _extends({
    style: {
      appearance: "none",
      width: "100%",
      height: h,
      padding: "0 28px 0 var(--space-5)",
      background: "var(--bg-sunken)",
      border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)",
      color: "var(--text-primary)",
      fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: "var(--text-md)",
      outline: "none",
      cursor: "pointer",
      ...style
    }
  }, rest), options.map(o => /*#__PURE__*/React.createElement("option", {
    key: String(o.value),
    value: o.value
  }, o.label))), /*#__PURE__*/React.createElement("span", {
    style: {
      position: "absolute",
      right: 9,
      color: "var(--text-muted)",
      pointerEvents: "none"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "chevron-down",
    size: 14
  }))), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      color: "var(--text-muted)"
    }
  }, hint) : null);
}
Object.assign(__ds_scope, { Select });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Select.jsx", error: String((e && e.message) || e) }); }

// components/navigation/ActionRail.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * The narrow right-hand rail of Tier A actions.
 * A disabled action always says WHY, because a silently dead button is the defect
 * this rail exists to avoid.
 */
function ActionRail({
  groups = [],
  onAction,
  width = "var(--action-rail-width)",
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("aside", _extends({
    style: {
      width,
      flex: "none",
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-8)",
      padding: "var(--space-7) var(--space-6)",
      borderLeft: "1px solid var(--border-subtle)",
      background: "var(--bg-surface)",
      overflowY: "auto",
      ...style
    }
  }, rest), groups.map(g => /*#__PURE__*/React.createElement("div", {
    key: g.title,
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-3)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase",
      color: "var(--text-muted)",
      padding: "0 var(--space-2) var(--space-2)"
    }
  }, g.title), g.actions.map(a => /*#__PURE__*/React.createElement(RailButton, {
    key: a.id,
    action: a,
    onAction: onAction
  })))));
}
function RailButton({
  action,
  onAction
}) {
  const [hover, setHover] = React.useState(false);
  const off = !!action.disabledReason;
  const danger = action.tone === "danger";
  return /*#__PURE__*/React.createElement("button", {
    type: "button",
    title: action.disabledReason || undefined,
    disabled: off,
    onClick: () => onAction && onAction(action.id),
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-5)",
      width: "100%",
      height: "var(--control-height-lg)",
      padding: "0 var(--space-5)",
      textAlign: "left",
      borderRadius: "var(--radius-md)",
      border: "1px solid transparent",
      cursor: off ? "not-allowed" : "pointer",
      background: off ? "transparent" : hover ? danger ? "var(--danger-soft)" : "var(--bg-hover)" : "transparent",
      color: off ? "var(--text-disabled)" : danger ? "var(--text-danger)" : "var(--text-primary)",
      font: "var(--type-body)",
      transition: "var(--transition-control)"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: action.icon,
    size: 15
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1,
      minWidth: 0,
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap"
    }
  }, action.label), off ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-disabled)"
    }
  }, action.disabledHint) : null);
}
Object.assign(__ds_scope, { ActionRail });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/ActionRail.jsx", error: String((e && e.message) || e) }); }

// components/navigation/Tabs.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** Underlined tabs for the detail pane: Overview, Processes, Logs, Apps, Sites. */
function Tabs({
  items = [],
  value,
  onChange,
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    role: "tablist",
    style: {
      display: "flex",
      alignItems: "stretch",
      gap: "var(--space-2)",
      height: "var(--toolbar-height)",
      borderBottom: "1px solid var(--border-subtle)",
      padding: "0 var(--space-6)",
      ...style
    }
  }, rest), items.map(it => {
    const on = it.value === value;
    return /*#__PURE__*/React.createElement("button", {
      key: it.value,
      role: "tab",
      "aria-selected": on,
      onClick: () => onChange && onChange(it.value),
      style: {
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-3)",
        padding: "0 var(--space-5)",
        background: "none",
        border: "none",
        cursor: "pointer",
        color: on ? "var(--text-primary)" : "var(--text-muted)",
        font: "var(--type-body-strong)",
        fontSize: "var(--text-md)",
        boxShadow: on ? "inset 0 -2px 0 var(--accent)" : "none",
        transition: "var(--transition-control)"
      }
    }, it.icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: it.icon,
      size: 14
    }) : null, it.label, it.count != null ? /*#__PURE__*/React.createElement("span", {
      style: {
        fontFamily: "var(--font-mono)",
        fontSize: "var(--text-2xs)",
        color: "var(--text-muted)"
      }
    }, it.count) : null);
  }));
}
Object.assign(__ds_scope, { Tabs });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/Tabs.jsx", error: String((e && e.message) || e) }); }

// components/status/EventRow.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const TIERS = {
  INSTANT: {
    color: "var(--cw-term-magenta)",
    hint: "Docker event stream"
  },
  FAST: {
    color: "var(--cw-term-cyan)",
    hint: "health poll"
  },
  ACTION: {
    color: "var(--cw-status-running)",
    hint: "you asked for this"
  },
  ERROR: {
    color: "var(--cw-status-offline)",
    hint: "failed"
  }
};

/** One line of the delta log: which tier saw the change, on what, and what it became. */
function EventRow({
  tier = "FAST",
  project,
  message,
  time,
  fresh = false,
  style,
  ...rest
}) {
  const t = TIERS[tier] || TIERS.FAST;
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      display: "grid",
      gridTemplateColumns: "62px minmax(0,150px) minmax(0,1fr) auto",
      gap: "var(--space-5)",
      alignItems: "baseline",
      padding: "5px var(--space-6)",
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-xs)",
      lineHeight: 1.4,
      animation: fresh ? "cw-delta-in 900ms var(--ease-out) 1" : "none",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    style: {
      color: t.color,
      fontWeight: "var(--weight-medium)",
      letterSpacing: "0.04em"
    }
  }, tier), /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-primary)",
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap"
    }
  }, project), /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--text-secondary)",
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap"
    }
  }, message), /*#__PURE__*/React.createElement("span", {
    style: {
      color: "var(--cw-term-dim)"
    }
  }, time));
}
Object.assign(__ds_scope, { EventRow });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/status/EventRow.jsx", error: String((e && e.message) || e) }); }

// components/status/StatusPill.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const STATUS_TOKENS = {
  running: {
    label: "running",
    color: "var(--cw-status-running)",
    bg: "rgba(47,191,143,0.13)"
  },
  online: {
    label: "online",
    color: "var(--cw-status-online)",
    bg: "rgba(0,160,208,0.13)"
  },
  degraded: {
    label: "degraded",
    color: "var(--cw-status-degraded)",
    bg: "rgba(232,178,58,0.13)"
  },
  offline: {
    label: "offline",
    color: "var(--cw-status-offline)",
    bg: "rgba(228,87,76,0.13)"
  },
  unknown: {
    label: "unknown",
    color: "var(--cw-status-unknown)",
    bg: "rgba(124,138,165,0.13)"
  }
};

/**
 * The health verdict, exactly as core.status / core.fleet spell it.
 * "unknown" is a state, not a default: it renders grey and says so, never green.
 */
function StatusPill({
  status = "unknown",
  size = "md",
  showDot = true,
  label,
  style,
  ...rest
}) {
  const t = STATUS_TOKENS[status] || STATUS_TOKENS.unknown;
  const sm = size === "sm";
  return /*#__PURE__*/React.createElement("span", _extends({
    style: {
      display: "inline-flex",
      alignItems: "center",
      gap: sm ? 5 : 6,
      height: sm ? 18 : 22,
      padding: sm ? "0 7px 0 6px" : "0 9px 0 8px",
      borderRadius: "var(--radius-pill)",
      background: t.bg,
      color: t.color,
      fontFamily: "var(--font-mono)",
      fontSize: sm ? "var(--text-2xs)" : "var(--text-xs)",
      fontWeight: "var(--weight-medium)",
      letterSpacing: "0.01em",
      whiteSpace: "nowrap",
      ...style
    }
  }, rest), showDot ? /*#__PURE__*/React.createElement("span", {
    style: {
      width: sm ? 5 : 6,
      height: sm ? 5 : 6,
      borderRadius: "50%",
      background: "currentColor",
      animation: status === "unknown" ? "cw-pulse 1.6s var(--ease-in-out) infinite" : "none"
    }
  }) : null, label || t.label);
}
Object.assign(__ds_scope, { STATUS_TOKENS, StatusPill });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/status/StatusPill.jsx", error: String((e && e.message) || e) }); }

// components/status/HealthDot.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** A bare health dot for dense tree rows where a full pill would not fit. */
function HealthDot({
  status = "unknown",
  size = 7,
  title,
  style,
  ...rest
}) {
  const t = __ds_scope.STATUS_TOKENS[status] || __ds_scope.STATUS_TOKENS.unknown;
  return /*#__PURE__*/React.createElement("span", _extends({
    title: title || status,
    style: {
      width: size,
      height: size,
      borderRadius: "50%",
      background: t.color,
      flex: "none",
      boxShadow: status === "running" ? "0 0 0 3px rgba(47,191,143,0.14)" : "none",
      animation: status === "unknown" ? "cw-pulse 1.6s var(--ease-in-out) infinite" : "none",
      ...style
    }
  }, rest));
}
Object.assign(__ds_scope, { HealthDot });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/status/HealthDot.jsx", error: String((e && e.message) || e) }); }

// components/navigation/TreeItem.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/** One row of the fleet tree: instance → bench → process, all the same row shape. */
function TreeItem({
  label,
  depth = 0,
  icon,
  status,
  expandable = false,
  expanded = false,
  selected = false,
  meta,
  mono = true,
  onToggle,
  onClick,
  style,
  ...rest
}) {
  const [hover, setHover] = React.useState(false);
  return /*#__PURE__*/React.createElement("div", _extends({
    onClick: onClick,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      height: "var(--row-height)",
      paddingRight: "var(--space-5)",
      paddingLeft: 8 + depth * 14,
      cursor: "pointer",
      userSelect: "none",
      background: selected ? "var(--bg-selected)" : hover ? "var(--bg-hover)" : "transparent",
      boxShadow: selected ? "var(--ring-selected)" : "none",
      color: selected ? "var(--text-primary)" : "var(--text-secondary)",
      transition: "background-color var(--duration-instant) var(--ease-out)",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement("span", {
    onClick: e => {
      if (expandable && onToggle) {
        e.stopPropagation();
        onToggle();
      }
    },
    style: {
      width: 12,
      flex: "none",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      color: "var(--text-muted)",
      opacity: expandable ? 1 : 0
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: expanded ? "chevron-down" : "chevron-right",
    size: 12
  })), icon ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: selected ? "var(--text-accent)" : "var(--text-muted)",
      flex: "none"
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 14
  })) : null, /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1,
      minWidth: 0,
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap",
      fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: "var(--text-sm)",
      fontWeight: selected ? "var(--weight-medium)" : "var(--weight-regular)"
    }
  }, label), meta ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-meta)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-muted)",
      fontFamily: "var(--font-mono)"
    }
  }, meta) : null, status ? /*#__PURE__*/React.createElement(__ds_scope.HealthDot, {
    status: status,
    size: 6,
    title: status
  }) : null);
}
Object.assign(__ds_scope, { TreeItem });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/navigation/TreeItem.jsx", error: String((e && e.message) || e) }); }

// components/status/ProcessRow.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const PROCESS_GRID = "16px minmax(84px,1fr) 72px 56px 56px 44px 56px";
const STATE_COLOR = {
  RUNNING: "var(--cw-status-running)",
  STARTING: "var(--cw-status-degraded)",
  BACKOFF: "var(--cw-status-degraded)",
  STOPPED: "var(--cw-status-offline)",
  FATAL: "var(--cw-status-offline)",
  EXITED: "var(--cw-status-offline)",
  UNKNOWN: "var(--cw-status-unknown)"
};

/** One supervisord program: its state, pid and the volatile numbers, laid out as a fixed grid. */
function ProcessRow({
  name,
  state = "UNKNOWN",
  pid,
  uptime,
  cpu,
  rss,
  selected = false,
  onClick,
  style,
  ...rest
}) {
  const [hover, setHover] = React.useState(false);
  const color = STATE_COLOR[state] || STATE_COLOR.UNKNOWN;
  const dim = {
    color: "var(--text-muted)",
    fontFamily: "var(--font-mono)",
    fontSize: "var(--text-xs)",
    textAlign: "right"
  };
  return /*#__PURE__*/React.createElement("div", _extends({
    onClick: onClick,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: "grid",
      gridTemplateColumns: PROCESS_GRID,
      alignItems: "center",
      gap: "var(--space-4)",
      height: "var(--row-height)",
      padding: "0 var(--space-6)",
      cursor: onClick ? "pointer" : "default",
      background: selected ? "var(--bg-selected)" : hover && onClick ? "var(--bg-hover)" : "transparent",
      boxShadow: selected ? "var(--ring-selected)" : "none",
      borderBottom: "1px solid var(--border-subtle)",
      ...style
    }
  }, rest), /*#__PURE__*/React.createElement(__ds_scope.HealthDot, {
    status: state === "RUNNING" ? "running" : state === "STARTING" || state === "BACKOFF" ? "degraded" : state === "UNKNOWN" ? "unknown" : "offline",
    size: 6,
    title: state
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-sm)",
      color: "var(--text-primary)",
      overflow: "hidden",
      textOverflow: "ellipsis"
    }
  }, name), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-xs)",
      color,
      letterSpacing: "0.02em"
    }
  }, state), /*#__PURE__*/React.createElement("span", {
    style: dim
  }, pid == null ? "pid —" : "pid " + pid), /*#__PURE__*/React.createElement("span", {
    style: dim
  }, uptime || "—"), /*#__PURE__*/React.createElement("span", {
    style: dim
  }, cpu == null ? "—" : cpu + "%"), /*#__PURE__*/React.createElement("span", {
    style: dim
  }, rss || "—"));
}
Object.assign(__ds_scope, { PROCESS_GRID, ProcessRow });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/status/ProcessRow.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/App.jsx
try { (() => {
const DS = window.CaffeinatedWhaleDesignSystem_2d9598;
const {
  ActionRail,
  Button,
  IconButton,
  Banner,
  EmptyState,
  StatusPill,
  Icon,
  Tag
} = DS;
const {
  TitleBar,
  StatusBar,
  FleetTree,
  EventLog,
  PaneHeader,
  InstanceOverview,
  BenchDetail,
  NewInstanceDialog,
  RemoveDialog
} = window;
function useConsole() {
  const [instances, setInstances] = React.useState(() => JSON.parse(JSON.stringify(window.CWData.instances)));
  const [events, setEvents] = React.useState(() => window.CWData.events.slice());
  const [selection, setSelection] = React.useState({
    kind: "bench",
    project: "my-erp",
    bench: 0
  });
  const [expanded, setExpanded] = React.useState({
    "i:my-erp": true,
    "b:my-erp:0": true
  });
  const [tab, setTab] = React.useState("processes");
  const [logOpen, setLogOpen] = React.useState(true);
  const [follow, setFollow] = React.useState(true);
  const [query, setQuery] = React.useState("");
  const [dialog, setDialog] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [tip, setTip] = React.useState(window.CWData.tips[0]);
  const push = (tier, project, message) => setEvents(e => [...e, {
    tier,
    project,
    message,
    time: clock()
  }].slice(-40));
  return {
    instances,
    setInstances,
    events,
    push,
    selection,
    setSelection,
    expanded,
    setExpanded,
    tab,
    setTab,
    logOpen,
    setLogOpen,
    follow,
    setFollow,
    query,
    setQuery,
    dialog,
    setDialog,
    busy,
    setBusy,
    tip,
    setTip
  };
}
function clock() {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, "0")).join(":");
}
function App() {
  const s = useConsole();
  const inst = s.instances.find(i => i.project === s.selection.project);
  const bench = inst && s.selection.bench != null ? inst.benches.find(b => b.index === s.selection.bench) : null;
  const filtered = s.query ? s.instances.filter(i => i.project.includes(s.query) || i.benches.some(b => b.apps.some(a => a.includes(s.query)) || b.sites.some(x => x.includes(s.query)))) : s.instances;
  function mutate(project, fn) {
    s.setInstances(list => list.map(i => i.project === project ? fn(JSON.parse(JSON.stringify(i))) : i));
  }
  function run(id) {
    const project = s.selection.project;
    if (id === "start_instance") {
      s.push("ACTION", project, "start instance");
      mutate(project, i => {
        i.container_running = true;
        i.docker_status = "running";
        i.overall = "unknown";
        return i;
      });
      setTimeout(() => {
        s.push("INSTANT", project, "start(frappe) → unknown");
      }, 300);
      setTimeout(() => {
        mutate(project, i => {
          i.overall = "running";
          i.probe_ms = 251;
          i.probed_at = clock();
          return i;
        });
        s.push("FAST", project, "→ running");
      }, 1400);
    }
    if (id === "stop_instance") {
      s.push("ACTION", project, "stop instance");
      mutate(project, i => {
        i.container_running = false;
        i.docker_status = "exited";
        i.overall = "offline";
        i.probe_ms = null;
        i.probed_at = null;
        return i;
      });
      setTimeout(() => s.push("INSTANT", project, "stop → offline"), 300);
    }
    if (id === "restart_instance") {
      s.push("ACTION", project, "restart instance");
      mutate(project, i => {
        i.overall = "unknown";
        return i;
      });
      setTimeout(() => {
        mutate(project, i => {
          i.overall = "running";
          return i;
        });
        s.push("FAST", project, "→ running");
      }, 1400);
    }
    if (id === "refresh_status") {
      s.push("ACTION", project, "refresh status");
      mutate(project, i => {
        if (i.container_running) {
          i.overall = i.benches.some(b => b.overall === "degraded") ? "degraded" : "running";
          i.probe_ms = 233 + Math.round(Math.random() * 60);
          i.probed_at = clock();
        }
        return i;
      });
    }
    if (id === "restart_process") {
      const name = s.selection.process;
      s.push("ACTION", project, "restart process " + name);
      mutate(project, i => {
        const b = i.benches.find(x => x.index === s.selection.bench);
        const p = b && b.processes.find(x => x.label === name);
        if (p) {
          p.state = "STARTING";
          p.pid = 4000 + Math.round(Math.random() * 900);
          p.uptime = "0s";
        }
        return i;
      });
      setTimeout(() => {
        mutate(project, i => {
          const b = i.benches.find(x => x.index === s.selection.bench);
          const p = b && b.processes.find(x => x.label === name);
          if (p) {
            p.state = "RUNNING";
            p.uptime = "3s";
            p.cpu = 0.6;
            p.rss = "104 MB";
          }
          if (b) b.overall = b.processes.every(x => x.state === "RUNNING") ? "running" : "degraded";
          i.overall = i.benches.every(x => x.overall === "running") ? "running" : "degraded";
          return i;
        });
        s.push("FAST", project, name + " → RUNNING");
      }, 1200);
    }
    if (id === "remove_instance") s.setDialog("remove");
  }
  const railGroups = [{
    title: "Instance",
    actions: [inst && inst.container_running ? {
      id: "stop_instance",
      label: "Stop instance",
      icon: "square"
    } : {
      id: "start_instance",
      label: "Start instance",
      icon: "play"
    }, {
      id: "restart_instance",
      label: "Restart instance",
      icon: "refresh-cw",
      disabledReason: inst && inst.container_running ? "" : "The instance is stopped",
      disabledHint: "stopped"
    }, {
      id: "refresh_status",
      label: "Refresh status",
      icon: "activity"
    }]
  }, {
    title: "Process",
    actions: [{
      id: "restart_process",
      label: "Restart process",
      icon: "rotate-cw",
      disabledReason: s.selection.kind === "process" ? "" : "Select a process in the tree",
      disabledHint: "one process"
    }]
  }, {
    title: "Not in the safe set",
    actions: [{
      id: "remove_instance",
      label: "Remove instance",
      icon: "trash-2",
      tone: "danger"
    }]
  }];
  return /*#__PURE__*/React.createElement("div", {
    style: {
      position: "relative",
      display: "flex",
      flexDirection: "column",
      height: "100%",
      minHeight: 0,
      background: "var(--bg-app)",
      overflow: "hidden"
    }
  }, /*#__PURE__*/React.createElement(TitleBar, {
    query: s.query,
    onSearch: s.setQuery
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      display: "flex",
      minHeight: 0
    }
  }, /*#__PURE__*/React.createElement(FleetTree, {
    instances: filtered,
    selection: s.selection,
    expanded: s.expanded,
    onToggle: k => s.setExpanded(e => ({
      ...e,
      [k]: !e[k]
    })),
    onSelect: sel => {
      s.setSelection(sel);
      if (sel.kind === "instance") s.setTab("processes");
    },
    onNew: () => s.setDialog("new")
  }), /*#__PURE__*/React.createElement("main", {
    style: {
      flex: 1,
      display: "flex",
      flexDirection: "column",
      minWidth: 0,
      minHeight: 0,
      background: "var(--bg-app)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      display: "flex",
      flexDirection: "column",
      minHeight: 0,
      overflow: "hidden"
    }
  }, !inst ? /*#__PURE__*/React.createElement(EmptyState, {
    icon: "search",
    title: "Nothing matches that search",
    command: "cwcli where " + (s.query || "<term>")
  }, "The cache holds no app or site containing \u201C", s.query, "\u201D.") : s.selection.kind === "instance" || !bench ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(PaneHeader, {
    title: inst.project,
    subtitle: inst.container_running ? inst.ports.join("  ·  ") : "container stopped",
    status: inst.overall,
    right: /*#__PURE__*/React.createElement("div", {
      style: {
        display: "flex",
        gap: "var(--space-3)"
      }
    }, /*#__PURE__*/React.createElement(IconButton, {
      icon: "external-link",
      title: "Open in browser",
      variant: "secondary"
    }), /*#__PURE__*/React.createElement(IconButton, {
      icon: "folder",
      title: "Open in VS Code",
      variant: "secondary"
    }))
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minHeight: 0,
      overflowY: "auto"
    }
  }, /*#__PURE__*/React.createElement(InstanceOverview, {
    inst: inst
  }))) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(PaneHeader, {
    title: inst.project + " / [" + bench.index + "] " + bench.label,
    subtitle: bench.bench_path + "   " + bench.web_site + ":" + bench.web_port + (bench.web_http_code == null ? " → no answer" : " → " + bench.web_http_code),
    status: bench.overall,
    right: /*#__PURE__*/React.createElement("div", {
      style: {
        display: "flex",
        gap: "var(--space-3)"
      }
    }, /*#__PURE__*/React.createElement(IconButton, {
      icon: "external-link",
      title: "Open site",
      variant: "secondary"
    }), /*#__PURE__*/React.createElement(IconButton, {
      icon: "pencil",
      title: "Set bench label",
      variant: "secondary"
    }))
  }), bench.overall === "degraded" ? /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "0 var(--space-8) var(--space-6)"
    }
  }, /*#__PURE__*/React.createElement(Banner, {
    tone: "warn",
    title: "One process is down on this bench",
    hint: "cwcli logs " + inst.project + " --bench " + bench.index + " --process web"
  }, "The bench answers nothing on ", bench.web_site, ":", bench.web_port, ". Restarting the process is a safe action; select it in the tree.")) : null, /*#__PURE__*/React.createElement(BenchDetail, {
    bench: bench,
    tab: s.tab,
    onTab: s.setTab,
    selectedProcess: s.selection.kind === "process" ? s.selection.process : null,
    onSelectProcess: name => s.setSelection({
      kind: "process",
      project: inst.project,
      bench: bench.index,
      process: name
    }),
    logs: window.CWData.logs,
    follow: s.follow,
    onFollow: () => s.setFollow(v => !v)
  }))), /*#__PURE__*/React.createElement(EventLog, {
    events: s.events,
    open: s.logOpen,
    onToggle: () => s.setLogOpen(v => !v)
  })), /*#__PURE__*/React.createElement(ActionRail, {
    groups: railGroups,
    onAction: run
  })), /*#__PURE__*/React.createElement(StatusBar, {
    daemon: window.CWData.daemon,
    probeMs: inst ? inst.probe_ms : null
  }), /*#__PURE__*/React.createElement(NewInstanceDialog, {
    open: s.dialog === "new",
    busy: s.busy,
    tip: s.tip,
    onClose: () => s.setDialog(null),
    onCreate: () => {
      s.setBusy(true);
      let n = 0;
      const t = setInterval(() => {
        n += 1;
        s.setTip(window.CWData.tips[n % window.CWData.tips.length]);
      }, 1600);
      setTimeout(() => {
        clearInterval(t);
        s.setBusy(false);
        s.setDialog(null);
        s.setInstances(list => [...list, {
          project: "new-erp",
          docker_status: "running",
          container_running: true,
          ports: ["18000-18005"],
          overall: "unknown",
          web_probed: false,
          probe_ms: null,
          probed_at: null,
          benches: []
        }]);
        s.push("INSTANT", "new-erp", "start(frappe) → unknown");
      }, 4200);
    }
  }), /*#__PURE__*/React.createElement(RemoveDialog, {
    open: s.dialog === "remove",
    project: s.selection.project,
    volumes: false,
    onVolumes: () => {},
    onClose: () => s.setDialog(null),
    onConfirm: () => {
      const p = s.selection.project;
      s.setInstances(list => list.filter(i => i.project !== p));
      s.push("INSTANT", p, "destroy → gone");
      s.setSelection({
        kind: "instance",
        project: "my-erp"
      });
      s.setDialog(null);
    }
  }));
}
Object.assign(window, {
  App
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/App.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/DetailPane.jsx
try { (() => {
const DS = window.CaffeinatedWhaleDesignSystem_2d9598;
const {
  Tabs,
  StatusPill,
  KeyValue,
  ProcessRow,
  PROCESS_GRID,
  LogView,
  Tag,
  Badge,
  Button,
  Banner,
  EmptyState,
  Icon,
  Select,
  Switch,
  DataTable
} = DS;
function PaneHeader({
  title,
  subtitle,
  status,
  right
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-6)",
      padding: "var(--space-7) var(--space-8) var(--space-6)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0,
      flex: 1
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-5)"
    }
  }, /*#__PURE__*/React.createElement("h1", {
    style: {
      margin: 0,
      font: "var(--type-title)",
      fontFamily: "var(--font-mono)",
      color: "var(--text-primary)",
      letterSpacing: "var(--tracking-tight)",
      overflow: "hidden",
      textOverflow: "ellipsis",
      whiteSpace: "nowrap"
    }
  }, title), status ? /*#__PURE__*/React.createElement(StatusPill, {
    status: status
  }) : null), subtitle ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-meta)",
      fontFamily: "var(--font-mono)",
      color: "var(--text-muted)",
      marginTop: 4
    }
  }, subtitle) : null), right);
}
function InstanceOverview({
  inst
}) {
  if (inst.overall === "unknown") {
    return /*#__PURE__*/React.createElement("div", {
      style: {
        padding: "0 var(--space-8) var(--space-8)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-6)"
      }
    }, /*#__PURE__*/React.createElement(Banner, {
      tone: "info",
      title: "No probe has answered for this instance yet",
      hint: "wait for the next FAST tick, or press Refresh status"
    }, "The container is up, which is not the same as a bench that serves. Health stays ", /*#__PURE__*/React.createElement("code", {
      style: {
        fontFamily: "var(--font-mono)",
        color: "var(--text-accent)"
      }
    }, "unknown"), " until a probe returns."), /*#__PURE__*/React.createElement("div", {
      style: {
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gap: "0 var(--space-10)"
      }
    }, /*#__PURE__*/React.createElement(KeyValue, {
      label: "Docker",
      value: inst.docker_status,
      tone: "accent"
    }), /*#__PURE__*/React.createElement(KeyValue, {
      label: "Ports",
      value: inst.ports.join(", ")
    }), /*#__PURE__*/React.createElement(KeyValue, {
      label: "Probed at",
      value: inst.probed_at,
      hint: "never probed"
    }), /*#__PURE__*/React.createElement(KeyValue, {
      label: "Web HTTP",
      value: null,
      hint: "not asked for on this cycle"
    })));
  }
  if (!inst.container_running) {
    return /*#__PURE__*/React.createElement("div", {
      style: {
        padding: "0 var(--space-8) var(--space-8)"
      }
    }, /*#__PURE__*/React.createElement(EmptyState, {
      icon: "box",
      title: "This instance is stopped",
      command: "cwcli start " + inst.project,
      action: /*#__PURE__*/React.createElement(Button, {
        variant: "primary",
        icon: "play"
      }, "Start instance")
    }, "Docker exited \u2014 container stopped \u2014 probe never completed. Bench rows are withheld because the processes they described are provably gone."));
  }
  return /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "0 var(--space-8) var(--space-8)",
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-7)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "1fr 1fr",
      gap: "0 var(--space-10)"
    }
  }, /*#__PURE__*/React.createElement(KeyValue, {
    label: "Docker",
    value: inst.docker_status,
    tone: "accent"
  }), /*#__PURE__*/React.createElement(KeyValue, {
    label: "Published ports",
    value: inst.ports.join(", ")
  }), /*#__PURE__*/React.createElement(KeyValue, {
    label: "Benches",
    value: inst.benches.length
  }), /*#__PURE__*/React.createElement(KeyValue, {
    label: "Last probe",
    value: inst.probe_ms == null ? null : inst.probe_ms + " ms at " + inst.probed_at,
    tone: "muted"
  })), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement(SectionLabel, null, "Benches"), /*#__PURE__*/React.createElement(DataTable, {
    columns: [{
      key: "bench",
      header: "Bench",
      width: "minmax(0,1.6fr)",
      tint: "cyan"
    }, {
      key: "label",
      header: "Label",
      width: "90px",
      tint: "magenta"
    }, {
      key: "health",
      header: "Health",
      width: "110px"
    }, {
      key: "web",
      header: "Web",
      width: "minmax(0,1.1fr)",
      tint: "green"
    }],
    rows: inst.benches.map(b => ({
      bench: "[" + b.index + "] " + b.bench_path,
      label: b.label,
      health: /*#__PURE__*/React.createElement(StatusPill, {
        status: b.overall,
        size: "sm"
      }),
      web: b.web_http_code == null ? b.web_site + ":" + b.web_port + " → no answer" : b.web_site + ":" + b.web_port + " → " + b.web_http_code
    }))
  })));
}
function SectionLabel({
  children
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase",
      color: "var(--text-muted)",
      marginBottom: "var(--space-4)"
    }
  }, children);
}
function BenchDetail({
  bench,
  tab,
  onTab,
  onSelectProcess,
  selectedProcess,
  logs,
  follow,
  onFollow
}) {
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Tabs, {
    value: tab,
    onChange: onTab,
    items: [{
      value: "processes",
      label: "Processes",
      icon: "cpu",
      count: bench.processes.length
    }, {
      value: "logs",
      label: "Logs",
      icon: "scroll-text"
    }, {
      value: "apps",
      label: "Apps",
      icon: "package",
      count: bench.apps.length
    }, {
      value: "where",
      label: "Sites",
      icon: "globe",
      count: bench.sites.length
    }]
  }), tab === "processes" ? /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minHeight: 0,
      overflowY: "auto"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: PROCESS_GRID,
      gap: "var(--space-4)",
      padding: "0 var(--space-6)",
      height: 26,
      alignItems: "center",
      borderBottom: "1px solid var(--border-default)",
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase",
      color: "var(--text-muted)"
    }
  }, /*#__PURE__*/React.createElement("span", null), /*#__PURE__*/React.createElement("span", null, "Process"), /*#__PURE__*/React.createElement("span", null, "State"), /*#__PURE__*/React.createElement("span", {
    style: {
      textAlign: "right"
    }
  }, "PID"), /*#__PURE__*/React.createElement("span", {
    style: {
      textAlign: "right"
    }
  }, "Uptime"), /*#__PURE__*/React.createElement("span", {
    style: {
      textAlign: "right"
    }
  }, "CPU"), /*#__PURE__*/React.createElement("span", {
    style: {
      textAlign: "right"
    }
  }, "RSS")), bench.processes.map(p => /*#__PURE__*/React.createElement(ProcessRow, {
    key: p.label,
    name: p.label,
    state: p.state,
    pid: p.pid,
    uptime: p.uptime,
    cpu: p.cpu,
    rss: p.rss,
    selected: selectedProcess === p.label,
    onClick: () => onSelectProcess(p.label)
  }))) : tab === "logs" ? /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minHeight: 0,
      display: "flex",
      flexDirection: "column",
      gap: "var(--space-5)",
      padding: "var(--space-6) var(--space-8) var(--space-8)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-6)"
    }
  }, /*#__PURE__*/React.createElement(Select, {
    size: "sm",
    mono: true,
    options: [{
      value: 200,
      label: "last 200 lines"
    }, {
      value: 500,
      label: "last 500 lines"
    }],
    wrapperStyle: {
      width: 170
    }
  }), /*#__PURE__*/React.createElement(Select, {
    size: "sm",
    mono: true,
    options: [{
      value: "web",
      label: "web"
    }, {
      value: "all",
      label: "all processes"
    }],
    wrapperStyle: {
      width: 150
    }
  }), /*#__PURE__*/React.createElement(Switch, {
    checked: follow,
    onChange: onFollow,
    label: "Follow"
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-muted)"
    }
  }, "bounded tail \u2014 the daemon never streams a whole file")), /*#__PURE__*/React.createElement(LogView, {
    lines: logs,
    style: {
      flex: 1,
      minHeight: 0
    }
  })) : tab === "apps" ? /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "var(--space-7) var(--space-8)"
    }
  }, /*#__PURE__*/React.createElement(SectionLabel, null, "Installed apps \xB7 verified from the bench, not the cache"), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      gap: "var(--space-4)"
    }
  }, bench.apps.map(a => /*#__PURE__*/React.createElement(Tag, {
    key: a,
    icon: "package"
  }, a)))) : /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "var(--space-7) var(--space-8)"
    }
  }, /*#__PURE__*/React.createElement(SectionLabel, null, "Sites"), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      gap: "var(--space-4)"
    }
  }, bench.sites.map(s => /*#__PURE__*/React.createElement(Tag, {
    key: s,
    icon: "globe"
  }, s)))));
}
Object.assign(window, {
  PaneHeader,
  InstanceOverview,
  BenchDetail,
  SectionLabel
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/DetailPane.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/Dialogs.jsx
try { (() => {
const {
  Dialog,
  Button,
  Input,
  Select,
  Checkbox,
  Banner,
  Spinner
} = window.CaffeinatedWhaleDesignSystem_2d9598;
function NewInstanceDialog({
  open,
  busy,
  tip,
  onClose,
  onCreate
}) {
  return /*#__PURE__*/React.createElement(Dialog, {
    open: open,
    title: "New instance",
    subtitle: "cwcli init",
    onClose: busy ? undefined : onClose,
    width: 470,
    footer: busy ? null : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Button, {
      variant: "ghost",
      onClick: onClose
    }, "Cancel"), /*#__PURE__*/React.createElement(Button, {
      variant: "primary",
      icon: "plus",
      onClick: onCreate
    }, "Create instance"))
  }, busy ? /*#__PURE__*/React.createElement(Spinner, {
    label: "Provisioning bench and site",
    tip: tip
  }) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "1fr 1fr",
      gap: "var(--space-6)"
    }
  }, /*#__PURE__*/React.createElement(Input, {
    label: "Project name",
    mono: true,
    placeholder: "my-erp",
    autoFocus: true
  }), /*#__PURE__*/React.createElement(Select, {
    label: "Frappe version",
    mono: true,
    options: [{
      value: 16,
      label: "16 (default)"
    }, {
      value: 15,
      label: "15"
    }, {
      value: 14,
      label: "14"
    }]
  }), /*#__PURE__*/React.createElement(Input, {
    label: "Site",
    mono: true,
    placeholder: "erp.localhost"
  }), /*#__PURE__*/React.createElement(Input, {
    label: "Base port",
    mono: true,
    placeholder: "8000",
    hint: "reserves 8000-8005 and 9000-9005"
  })), /*#__PURE__*/React.createElement(Checkbox, {
    label: "Open in VS Code when it is ready",
    hint: "Installs the Dev Containers extension if it is missing."
  })));
}
function RemoveDialog({
  open,
  project,
  volumes,
  onVolumes,
  onClose,
  onConfirm
}) {
  return /*#__PURE__*/React.createElement(Dialog, {
    open: open,
    tone: "danger",
    title: "Remove this instance?",
    subtitle: project,
    onClose: onClose,
    width: 470,
    footer: /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Button, {
      variant: "ghost",
      onClick: onClose
    }, "Cancel"), /*#__PURE__*/React.createElement(Button, {
      variant: "danger",
      icon: "trash-2",
      onClick: onConfirm
    }, "Remove instance"))
  }, /*#__PURE__*/React.createElement("div", null, "Containers, the project directory and every bench on it are deleted. A database backup is archived first unless you turn it off."), /*#__PURE__*/React.createElement(Checkbox, {
    checked: volumes,
    onChange: onVolumes,
    label: "Also remove named volumes",
    hint: "This deletes the database. There is no undo."
  }), /*#__PURE__*/React.createElement(Banner, {
    tone: "warn",
    hint: "cwcli rm " + project + " --yes --volumes"
  }, "Destructive verbs are not part of the app's safe action set. The desktop app runs the same command you would type."));
}
Object.assign(window, {
  NewInstanceDialog,
  RemoveDialog
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/Dialogs.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/EventLog.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const {
  EventRow,
  IconButton,
  Icon
} = window.CaffeinatedWhaleDesignSystem_2d9598;
function EventLog({
  events,
  open,
  onToggle
}) {
  return /*#__PURE__*/React.createElement("section", {
    style: {
      flex: "none",
      borderTop: "1px solid var(--border-subtle)",
      background: "var(--cw-term-bg)",
      display: "flex",
      flexDirection: "column",
      maxHeight: open ? 168 : 30,
      transition: "max-height var(--duration-base) var(--ease-out)",
      overflow: "hidden"
    }
  }, /*#__PURE__*/React.createElement("div", {
    onClick: onToggle,
    style: {
      height: 30,
      flex: "none",
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      padding: "0 var(--space-6)",
      cursor: "pointer",
      color: "var(--text-muted)"
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: open ? "chevron-down" : "chevron-right",
    size: 12
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase"
    }
  }, "Event stream"), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)"
    }
  }, events.length, " deltas"), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)"
    }
  }, "SSE \xB7 /api/events")), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: "auto",
      paddingBottom: "var(--space-4)"
    }
  }, events.map((e, i) => /*#__PURE__*/React.createElement(EventRow, _extends({
    key: i
  }, e, {
    fresh: i === events.length - 1
  })))));
}
Object.assign(window, {
  EventLog
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/EventLog.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/FleetTree.jsx
try { (() => {
const {
  TreeItem,
  Button,
  Icon,
  Badge
} = window.CaffeinatedWhaleDesignSystem_2d9598;
function FleetTree({
  instances,
  selection,
  onSelect,
  expanded,
  onToggle,
  onNew
}) {
  const rows = [];
  instances.forEach(inst => {
    const openInst = expanded["i:" + inst.project];
    rows.push(/*#__PURE__*/React.createElement(TreeItem, {
      key: "i:" + inst.project,
      label: inst.project,
      icon: "box",
      depth: 0,
      status: inst.overall,
      expandable: inst.benches.length > 0,
      expanded: openInst,
      selected: selection.kind === "instance" && selection.project === inst.project,
      meta: inst.ports[0] ? inst.ports[0].split("-")[0] : "",
      onToggle: () => onToggle("i:" + inst.project),
      onClick: () => onSelect({
        kind: "instance",
        project: inst.project
      })
    }));
    if (!openInst) return;
    inst.benches.forEach(b => {
      const key = "b:" + inst.project + ":" + b.index;
      const openBench = expanded[key];
      rows.push(/*#__PURE__*/React.createElement(TreeItem, {
        key: key,
        label: "[" + b.index + "] " + b.bench_path.split("/").pop(),
        icon: "layers",
        depth: 1,
        status: b.overall,
        expandable: true,
        expanded: openBench,
        selected: selection.kind === "bench" && selection.project === inst.project && selection.bench === b.index,
        meta: b.web_port,
        onToggle: () => onToggle(key),
        onClick: () => onSelect({
          kind: "bench",
          project: inst.project,
          bench: b.index
        })
      }));
      if (!openBench) return;
      b.processes.forEach(p => {
        rows.push(/*#__PURE__*/React.createElement(TreeItem, {
          key: key + ":" + p.label,
          label: p.label,
          icon: "terminal",
          depth: 2,
          status: p.state === "RUNNING" ? "running" : p.state === "BACKOFF" || p.state === "STARTING" ? "degraded" : "offline",
          selected: selection.kind === "process" && selection.project === inst.project && selection.bench === b.index && selection.process === p.label,
          onClick: () => onSelect({
            kind: "process",
            project: inst.project,
            bench: b.index,
            process: p.label
          })
        }));
      });
    });
  });
  return /*#__PURE__*/React.createElement("nav", {
    style: {
      width: "var(--tree-width)",
      flex: "none",
      display: "flex",
      flexDirection: "column",
      background: "var(--bg-surface)",
      borderRight: "1px solid var(--border-subtle)",
      minHeight: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      height: "var(--toolbar-height)",
      flex: "none",
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "0 var(--space-5) 0 var(--space-6)",
      borderBottom: "1px solid var(--border-subtle)"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      font: "var(--type-label)",
      letterSpacing: "var(--tracking-caps)",
      textTransform: "uppercase",
      color: "var(--text-muted)"
    }
  }, "Fleet ", /*#__PURE__*/React.createElement(Badge, {
    tone: "neutral",
    uppercase: false
  }, instances.length)), /*#__PURE__*/React.createElement(Button, {
    size: "sm",
    variant: "ghost",
    icon: "plus",
    onClick: onNew
  }, "New")), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: "auto",
      padding: "var(--space-3) 0"
    }
  }, rows));
}
Object.assign(window, {
  FleetTree
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/FleetTree.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/TitleBar.jsx
try { (() => {
const {
  IconButton,
  SearchField,
  Icon,
  StatusPill
} = window.CaffeinatedWhaleDesignSystem_2d9598;
function TitleBar({
  onSearch,
  query
}) {
  return /*#__PURE__*/React.createElement("header", {
    style: {
      height: "var(--titlebar-height)",
      flex: "none",
      display: "flex",
      alignItems: "center",
      gap: "var(--space-6)",
      padding: "0 var(--space-3) 0 var(--space-6)",
      background: "var(--bg-surface)",
      borderBottom: "1px solid var(--border-subtle)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-4)",
      flex: "none"
    }
  }, /*#__PURE__*/React.createElement("img", {
    src: "../../assets/logo-whale.png",
    alt: "",
    width: "20",
    height: "20",
    style: {
      display: "block"
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      font: "var(--type-body-strong)",
      fontSize: "var(--text-sm)",
      color: "var(--text-primary)",
      letterSpacing: "var(--tracking-tight)"
    }
  }, "Caffeinated Whale")), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      display: "flex",
      justifyContent: "center"
    }
  }, /*#__PURE__*/React.createElement(SearchField, {
    value: query,
    onChange: e => onSearch(e.target.value),
    style: {
      width: 340,
      height: 24
    }
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: "var(--space-1)",
      flex: "none"
    }
  }, /*#__PURE__*/React.createElement(IconButton, {
    icon: "minus",
    size: "sm",
    title: "Minimise"
  }), /*#__PURE__*/React.createElement(IconButton, {
    icon: "square",
    size: "sm",
    title: "Maximise"
  }), /*#__PURE__*/React.createElement(IconButton, {
    icon: "x",
    size: "sm",
    title: "Close"
  })));
}
function StatusBar({
  daemon,
  probeMs
}) {
  return /*#__PURE__*/React.createElement("footer", {
    style: {
      height: "var(--statusbar-height)",
      flex: "none",
      display: "flex",
      alignItems: "center",
      gap: "var(--space-7)",
      padding: "0 var(--space-6)",
      background: "var(--bg-surface)",
      borderTop: "1px solid var(--border-subtle)",
      fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)",
      color: "var(--text-muted)"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: 5,
      color: "var(--cw-status-running)"
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "circle-dot",
    size: 11
  }), " cwcli serve \xB7 ", daemon.host, ":", daemon.port), /*#__PURE__*/React.createElement("span", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: 5
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "box",
    size: 11
  }), " docker ", daemon.docker), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1
    }
  }), /*#__PURE__*/React.createElement("span", null, probeMs == null ? "no probe yet" : "last probe " + probeMs + " ms"), /*#__PURE__*/React.createElement("span", null, "cwcli ", daemon.version));
}
Object.assign(window, {
  TitleBar,
  StatusBar
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/TitleBar.jsx", error: String((e && e.message) || e) }); }

// ui_kits/desktop/data.js
try { (() => {
// Fixture shaped exactly like core.fleet.InstanceState → BenchStatus → ProcessHealth.
window.CWData = {
  daemon: {
    host: "127.0.0.1",
    port: 8777,
    version: "2.1.0",
    docker: "running"
  },
  instances: [{
    project: "my-erp",
    docker_status: "running",
    container_running: true,
    ports: ["8000-8005", "9000-9005"],
    overall: "running",
    web_probed: true,
    probe_ms: 248,
    probed_at: "14:02:14",
    benches: [{
      index: 0,
      bench_path: "/workspace/development/frappe-bench",
      label: "main",
      overall: "running",
      supervisor_up: true,
      web_port: 8000,
      web_port_verified: true,
      web_site: "erp.localhost",
      web_http_code: 200,
      bench_present: true,
      not_cwcli_supervised: false,
      apps: ["frappe", "erpnext", "hrms", "payments"],
      sites: ["erp.localhost"],
      processes: [{
        label: "web",
        state: "RUNNING",
        pid: 4711,
        uptime: "4h 12m",
        cpu: 1.4,
        rss: "212 MB"
      }, {
        label: "socketio",
        state: "RUNNING",
        pid: 4712,
        uptime: "4h 12m",
        cpu: 0.2,
        rss: "88 MB"
      }, {
        label: "watch",
        state: "RUNNING",
        pid: 4713,
        uptime: "4h 12m",
        cpu: 0.9,
        rss: "164 MB"
      }, {
        label: "schedule",
        state: "RUNNING",
        pid: 4714,
        uptime: "4h 12m",
        cpu: 0.1,
        rss: "72 MB"
      }, {
        label: "worker_default",
        state: "RUNNING",
        pid: 4715,
        uptime: "4h 12m",
        cpu: 0.3,
        rss: "118 MB"
      }, {
        label: "worker_short",
        state: "RUNNING",
        pid: 4716,
        uptime: "4h 12m",
        cpu: 0.1,
        rss: "112 MB"
      }]
    }, {
      index: 1,
      bench_path: "/workspace/development/staging-bench",
      label: "staging",
      overall: "degraded",
      supervisor_up: true,
      web_port: 8001,
      web_port_verified: true,
      web_site: "staging.localhost",
      web_http_code: null,
      bench_present: true,
      not_cwcli_supervised: false,
      apps: ["frappe", "erpnext"],
      sites: ["staging.localhost"],
      processes: [{
        label: "web",
        state: "STOPPED",
        pid: null,
        uptime: null,
        cpu: null,
        rss: null
      }, {
        label: "socketio",
        state: "RUNNING",
        pid: 5120,
        uptime: "1h 03m",
        cpu: 0.2,
        rss: "84 MB"
      }, {
        label: "schedule",
        state: "RUNNING",
        pid: 5121,
        uptime: "1h 03m",
        cpu: 0.1,
        rss: "70 MB"
      }, {
        label: "worker_default",
        state: "BACKOFF",
        pid: null,
        uptime: null,
        cpu: null,
        rss: null
      }]
    }]
  }, {
    project: "hr-sandbox",
    docker_status: "running",
    container_running: true,
    ports: ["16000-16005"],
    overall: "unknown",
    web_probed: false,
    probe_ms: null,
    probed_at: null,
    benches: []
  }, {
    project: "old-proj",
    docker_status: "exited",
    container_running: false,
    ports: [],
    overall: "offline",
    web_probed: false,
    probe_ms: null,
    probed_at: null,
    benches: []
  }],
  events: [{
    tier: "INSTANT",
    project: "my-erp",
    message: "start(frappe) → unknown",
    time: "14:02:11"
  }, {
    tier: "FAST",
    project: "my-erp",
    message: "→ degraded",
    time: "14:02:13"
  }, {
    tier: "FAST",
    project: "my-erp",
    message: "→ running",
    time: "14:02:14"
  }, {
    tier: "FAST",
    project: "my-erp",
    message: "[1] staging-bench → degraded",
    time: "14:02:16"
  }, {
    tier: "INSTANT",
    project: "hr-sandbox",
    message: "start(mariadb) → unknown",
    time: "14:03:02"
  }],
  logs: [{
    time: "14:02:09",
    level: "command",
    text: "$ supervisorctl restart frappe-bench-frappe:web"
  }, {
    time: "14:02:10",
    level: "info",
    text: "web: stopped"
  }, {
    time: "14:02:11",
    level: "info",
    text: "web: started"
  }, {
    time: "14:02:11",
    level: "debug",
    text: "bench serve --port 8000"
  }, {
    time: "14:02:12",
    level: "success",
    text: "* Serving Flask app 'frappe.app'"
  }, {
    time: "14:02:12",
    level: "warn",
    text: "* Debug mode: on"
  }, {
    time: "14:02:13",
    level: "info",
    text: "* Running on http://0.0.0.0:8000"
  }, {
    time: "14:02:14",
    level: "info",
    text: "127.0.0.1 - - [24/Jul/2026 14:02:14] \"GET /api/method/ping HTTP/1.1\" 200 -"
  }, {
    time: "14:02:18",
    level: "error",
    text: "worker_default: entered FATAL state, too many start retries too quickly"
  }, {
    time: "14:02:19",
    level: "debug",
    text: "supervisor: reaping worker_default"
  }],
  tips: ["Use 'cwcli inspect <project>' to cache project structure for faster commands", "Install tab completion with 'cwcli --install-completion' for faster workflows", "Run 'cwcli backup <project> --with-files' before major changes", "Unlock stuck sites with 'cwcli unlock <project> --site <site-name>'"]
};
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/desktop/data.js", error: String((e && e.message) || e) }); }

__ds_ns.Badge = __ds_scope.Badge;

__ds_ns.Button = __ds_scope.Button;

__ds_ns.Card = __ds_scope.Card;

__ds_ns.IconButton = __ds_scope.IconButton;

__ds_ns.Kbd = __ds_scope.Kbd;

__ds_ns.Tag = __ds_scope.Tag;

__ds_ns.DataTable = __ds_scope.DataTable;

__ds_ns.KeyValue = __ds_scope.KeyValue;

__ds_ns.LogView = __ds_scope.LogView;

__ds_ns.Banner = __ds_scope.Banner;

__ds_ns.Dialog = __ds_scope.Dialog;

__ds_ns.EmptyState = __ds_scope.EmptyState;

__ds_ns.Spinner = __ds_scope.Spinner;

__ds_ns.Checkbox = __ds_scope.Checkbox;

__ds_ns.Input = __ds_scope.Input;

__ds_ns.SearchField = __ds_scope.SearchField;

__ds_ns.Select = __ds_scope.Select;

__ds_ns.Switch = __ds_scope.Switch;

__ds_ns.ICON_PATHS = __ds_scope.ICON_PATHS;

__ds_ns.ICON_NAMES = __ds_scope.ICON_NAMES;

__ds_ns.Icon = __ds_scope.Icon;

__ds_ns.ActionRail = __ds_scope.ActionRail;

__ds_ns.Tabs = __ds_scope.Tabs;

__ds_ns.TreeItem = __ds_scope.TreeItem;

__ds_ns.EventRow = __ds_scope.EventRow;

__ds_ns.HealthDot = __ds_scope.HealthDot;

__ds_ns.PROCESS_GRID = __ds_scope.PROCESS_GRID;

__ds_ns.ProcessRow = __ds_scope.ProcessRow;

__ds_ns.STATUS_TOKENS = __ds_scope.STATUS_TOKENS;

__ds_ns.StatusPill = __ds_scope.StatusPill;

})();
