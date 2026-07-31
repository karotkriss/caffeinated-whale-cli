const DS = window.CaffeinatedWhaleDesignSystem_2d9598;
const { Tabs, StatusPill, KeyValue, ProcessRow, PROCESS_GRID, LogView, Tag, Badge, Button, Banner, EmptyState, Icon, Select, Switch, DataTable } = DS;

function PaneHeader({ title, subtitle, status, right }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-6)", padding: "var(--space-7) var(--space-8) var(--space-6)" }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-5)" }}>
          <h1 style={{ margin: 0, font: "var(--type-title)", fontFamily: "var(--font-mono)", color: "var(--text-primary)", letterSpacing: "var(--tracking-tight)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</h1>
          {status ? <StatusPill status={status} /> : null}
        </div>
        {subtitle ? <div style={{ font: "var(--type-meta)", fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>{subtitle}</div> : null}
      </div>
      {right}
    </div>
  );
}

function InstanceOverview({ inst }) {
  if (inst.overall === "unknown") {
    return (
      <div style={{ padding: "0 var(--space-8) var(--space-8)", display: "flex", flexDirection: "column", gap: "var(--space-6)" }}>
        <Banner tone="info" title="No probe has answered for this instance yet" hint="wait for the next FAST tick, or press Refresh status">
          The container is up, which is not the same as a bench that serves. Health stays <code style={{ fontFamily: "var(--font-mono)", color: "var(--text-accent)" }}>unknown</code> until a probe returns.
        </Banner>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 var(--space-10)" }}>
          <KeyValue label="Docker" value={inst.docker_status} tone="accent" />
          <KeyValue label="Ports" value={inst.ports.join(", ")} />
          <KeyValue label="Probed at" value={inst.probed_at} hint="never probed" />
          <KeyValue label="Web HTTP" value={null} hint="not asked for on this cycle" />
        </div>
      </div>
    );
  }
  if (!inst.container_running) {
    return (
      <div style={{ padding: "0 var(--space-8) var(--space-8)" }}>
        <EmptyState icon="box" title="This instance is stopped" command={"cwcli start " + inst.project}
          action={<Button variant="primary" icon="play">Start instance</Button>}>
          Docker exited — container stopped — probe never completed. Bench rows are withheld because the processes they described are provably gone.
        </EmptyState>
      </div>
    );
  }
  return (
    <div style={{ padding: "0 var(--space-8) var(--space-8)", display: "flex", flexDirection: "column", gap: "var(--space-7)" }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 var(--space-10)" }}>
        <KeyValue label="Docker" value={inst.docker_status} tone="accent" />
        <KeyValue label="Published ports" value={inst.ports.join(", ")} />
        <KeyValue label="Benches" value={inst.benches.length} />
        <KeyValue label="Last probe" value={inst.probe_ms == null ? null : inst.probe_ms + " ms at " + inst.probed_at} tone="muted" />
      </div>
      <div>
        <SectionLabel>Benches</SectionLabel>
        <DataTable
          columns={[
            { key: "bench", header: "Bench", width: "minmax(0,1.6fr)", tint: "cyan" },
            { key: "label", header: "Label", width: "90px", tint: "magenta" },
            { key: "health", header: "Health", width: "110px" },
            { key: "web", header: "Web", width: "minmax(0,1.1fr)", tint: "green" }
          ]}
          rows={inst.benches.map((b) => ({
            bench: "[" + b.index + "] " + b.bench_path,
            label: b.label,
            health: <StatusPill status={b.overall} size="sm" />,
            web: b.web_http_code == null ? b.web_site + ":" + b.web_port + " → no answer" : b.web_site + ":" + b.web_port + " → " + b.web_http_code
          }))}
        />
      </div>
    </div>
  );
}

function SectionLabel({ children }) {
  return <div style={{ font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "var(--space-4)" }}>{children}</div>;
}

function BenchDetail({ bench, tab, onTab, onSelectProcess, selectedProcess, logs, follow, onFollow }) {
  return (
    <React.Fragment>
      <Tabs
        value={tab}
        onChange={onTab}
        items={[
          { value: "processes", label: "Processes", icon: "cpu", count: bench.processes.length },
          { value: "logs", label: "Logs", icon: "scroll-text" },
          { value: "apps", label: "Apps", icon: "package", count: bench.apps.length },
          { value: "where", label: "Sites", icon: "globe", count: bench.sites.length }
        ]}
      />
      {tab === "processes" ? (
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          <div style={{ display: "grid", gridTemplateColumns: PROCESS_GRID, gap: "var(--space-4)", padding: "0 var(--space-6)", height: 26, alignItems: "center", borderBottom: "1px solid var(--border-default)", font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase", color: "var(--text-muted)" }}>
            <span /><span>Process</span><span>State</span><span style={{ textAlign: "right" }}>PID</span><span style={{ textAlign: "right" }}>Uptime</span><span style={{ textAlign: "right" }}>CPU</span><span style={{ textAlign: "right" }}>RSS</span>
          </div>
          {bench.processes.map((p) => (
            <ProcessRow key={p.label} name={p.label} state={p.state} pid={p.pid} uptime={p.uptime} cpu={p.cpu} rss={p.rss}
              selected={selectedProcess === p.label} onClick={() => onSelectProcess(p.label)} />
          ))}
        </div>
      ) : tab === "logs" ? (
        <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: "var(--space-5)", padding: "var(--space-6) var(--space-8) var(--space-8)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-6)" }}>
            <Select size="sm" mono options={[{ value: 200, label: "last 200 lines" }, { value: 500, label: "last 500 lines" }]} wrapperStyle={{ width: 170 }} />
            <Select size="sm" mono options={[{ value: "web", label: "web" }, { value: "all", label: "all processes" }]} wrapperStyle={{ width: 150 }} />
            <Switch checked={follow} onChange={onFollow} label="Follow" />
            <span style={{ flex: 1 }} />
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>bounded tail — the daemon never streams a whole file</span>
          </div>
          <LogView lines={logs} style={{ flex: 1, minHeight: 0 }} />
        </div>
      ) : tab === "apps" ? (
        <div style={{ padding: "var(--space-7) var(--space-8)" }}>
          <SectionLabel>Installed apps · verified from the bench, not the cache</SectionLabel>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-4)" }}>
            {bench.apps.map((a) => <Tag key={a} icon="package">{a}</Tag>)}
          </div>
        </div>
      ) : (
        <div style={{ padding: "var(--space-7) var(--space-8)" }}>
          <SectionLabel>Sites</SectionLabel>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-4)" }}>
            {bench.sites.map((s) => <Tag key={s} icon="globe">{s}</Tag>)}
          </div>
        </div>
      )}
    </React.Fragment>
  );
}

Object.assign(window, { PaneHeader, InstanceOverview, BenchDetail, SectionLabel });
