const { TreeItem, Button, Icon, Badge } = window.CaffeinatedWhaleDesignSystem_2d9598;

function FleetTree({ instances, selection, onSelect, expanded, onToggle, onNew }) {
  const rows = [];
  instances.forEach((inst) => {
    const openInst = expanded["i:" + inst.project];
    rows.push(
      <TreeItem
        key={"i:" + inst.project}
        label={inst.project}
        icon="box"
        depth={0}
        status={inst.overall}
        expandable={inst.benches.length > 0}
        expanded={openInst}
        selected={selection.kind === "instance" && selection.project === inst.project}
        meta={inst.ports[0] ? inst.ports[0].split("-")[0] : ""}
        onToggle={() => onToggle("i:" + inst.project)}
        onClick={() => onSelect({ kind: "instance", project: inst.project })}
      />
    );
    if (!openInst) return;
    inst.benches.forEach((b) => {
      const key = "b:" + inst.project + ":" + b.index;
      const openBench = expanded[key];
      rows.push(
        <TreeItem
          key={key}
          label={"[" + b.index + "] " + b.bench_path.split("/").pop()}
          icon="layers"
          depth={1}
          status={b.overall}
          expandable
          expanded={openBench}
          selected={selection.kind === "bench" && selection.project === inst.project && selection.bench === b.index}
          meta={b.web_port}
          onToggle={() => onToggle(key)}
          onClick={() => onSelect({ kind: "bench", project: inst.project, bench: b.index })}
        />
      );
      if (!openBench) return;
      b.processes.forEach((p) => {
        rows.push(
          <TreeItem
            key={key + ":" + p.label}
            label={p.label}
            icon="terminal"
            depth={2}
            status={p.state === "RUNNING" ? "running" : p.state === "BACKOFF" || p.state === "STARTING" ? "degraded" : "offline"}
            selected={selection.kind === "process" && selection.project === inst.project && selection.bench === b.index && selection.process === p.label}
            onClick={() => onSelect({ kind: "process", project: inst.project, bench: b.index, process: p.label })}
          />
        );
      });
    });
  });

  return (
    <nav style={{
      width: "var(--tree-width)", flex: "none", display: "flex", flexDirection: "column",
      background: "var(--bg-surface)", borderRight: "1px solid var(--border-subtle)", minHeight: 0,
    }}>
      <div style={{
        height: "var(--toolbar-height)", flex: "none", display: "flex", alignItems: "center",
        justifyContent: "space-between", padding: "0 var(--space-5) 0 var(--space-6)",
        borderBottom: "1px solid var(--border-subtle)",
      }}>
        <span style={{ display: "flex", alignItems: "center", gap: "var(--space-4)", font: "var(--type-label)", letterSpacing: "var(--tracking-caps)", textTransform: "uppercase", color: "var(--text-muted)" }}>
          Fleet <Badge tone="neutral" uppercase={false}>{instances.length}</Badge>
        </span>
        <Button size="sm" variant="ghost" icon="plus" onClick={onNew}>New</Button>
      </div>
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-3) 0" }}>{rows}</div>
    </nav>
  );
}

Object.assign(window, { FleetTree });
