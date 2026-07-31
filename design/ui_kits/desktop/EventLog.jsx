const { EventRow, IconButton, Icon } = window.CaffeinatedWhaleDesignSystem_2d9598;

function EventLog({ events, open, onToggle }) {
  return (
    <section style={{
      flex: "none", borderTop: "1px solid var(--border-subtle)", background: "var(--cw-term-bg)",
      display: "flex", flexDirection: "column", maxHeight: open ? 168 : 30, transition: "max-height var(--duration-base) var(--ease-out)", overflow: "hidden",
    }}>
      <div
        onClick={onToggle}
        style={{ height: 30, flex: "none", display: "flex", alignItems: "center", gap: "var(--space-4)", padding: "0 var(--space-6)", cursor: "pointer", color: "var(--text-muted)" }}
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size={12} />
        <span style={{ font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase" }}>Event stream</span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)" }}>{events.length} deltas</span>
        <span style={{ flex: 1 }} />
        <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)" }}>SSE · /api/events</span>
      </div>
      <div style={{ flex: 1, overflowY: "auto", paddingBottom: "var(--space-4)" }}>
        {events.map((e, i) => <EventRow key={i} {...e} fresh={i === events.length - 1} />)}
      </div>
    </section>
  );
}

Object.assign(window, { EventLog });
