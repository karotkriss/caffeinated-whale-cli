const { IconButton, SearchField, Icon, StatusPill } = window.CaffeinatedWhaleDesignSystem_2d9598;

function TitleBar({ onSearch, query }) {
  return (
    <header style={{
      height: "var(--titlebar-height)", flex: "none", display: "flex", alignItems: "center",
      gap: "var(--space-6)", padding: "0 var(--space-3) 0 var(--space-6)",
      background: "var(--bg-surface)", borderBottom: "1px solid var(--border-subtle)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-4)", flex: "none" }}>
        <img src="../../assets/logo-whale.png" alt="" width="20" height="20" style={{ display: "block" }} />
        <span style={{ font: "var(--type-body-strong)", fontSize: "var(--text-sm)", color: "var(--text-primary)", letterSpacing: "var(--tracking-tight)" }}>
          Caffeinated Whale
        </span>
      </div>
      <div style={{ flex: 1, display: "flex", justifyContent: "center" }}>
        <SearchField value={query} onChange={(e) => onSearch(e.target.value)} style={{ width: 340, height: 24 }} />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-1)", flex: "none" }}>
        <IconButton icon="minus" size="sm" title="Minimise" />
        <IconButton icon="square" size="sm" title="Maximise" />
        <IconButton icon="x" size="sm" title="Close" />
      </div>
    </header>
  );
}

function StatusBar({ daemon, probeMs }) {
  return (
    <footer style={{
      height: "var(--statusbar-height)", flex: "none", display: "flex", alignItems: "center",
      gap: "var(--space-7)", padding: "0 var(--space-6)", background: "var(--bg-surface)",
      borderTop: "1px solid var(--border-subtle)", fontFamily: "var(--font-mono)",
      fontSize: "var(--text-2xs)", color: "var(--text-muted)",
    }}>
      <span style={{ display: "flex", alignItems: "center", gap: 5, color: "var(--cw-status-running)" }}>
        <Icon name="circle-dot" size={11} /> cwcli serve · {daemon.host}:{daemon.port}
      </span>
      <span style={{ display: "flex", alignItems: "center", gap: 5 }}><Icon name="box" size={11} /> docker {daemon.docker}</span>
      <span style={{ flex: 1 }} />
      <span>{probeMs == null ? "no probe yet" : "last probe " + probeMs + " ms"}</span>
      <span>cwcli {daemon.version}</span>
    </footer>
  );
}

Object.assign(window, { TitleBar, StatusBar });
