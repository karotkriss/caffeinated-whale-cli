const DS = window.CaffeinatedWhaleDesignSystem_2d9598;
const { ActionRail, Button, IconButton, Banner, EmptyState, StatusPill, Icon, Tag } = DS;
const { TitleBar, StatusBar, FleetTree, EventLog, PaneHeader, InstanceOverview, BenchDetail, NewInstanceDialog, RemoveDialog } = window;

function useConsole() {
  const [instances, setInstances] = React.useState(() => JSON.parse(JSON.stringify(window.CWData.instances)));
  const [events, setEvents] = React.useState(() => window.CWData.events.slice());
  const [selection, setSelection] = React.useState({ kind: "bench", project: "my-erp", bench: 0 });
  const [expanded, setExpanded] = React.useState({ "i:my-erp": true, "b:my-erp:0": true });
  const [tab, setTab] = React.useState("processes");
  const [logOpen, setLogOpen] = React.useState(true);
  const [follow, setFollow] = React.useState(true);
  const [query, setQuery] = React.useState("");
  const [dialog, setDialog] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [tip, setTip] = React.useState(window.CWData.tips[0]);

  const push = (tier, project, message) =>
    setEvents((e) => [...e, { tier, project, message, time: clock() }].slice(-40));

  return { instances, setInstances, events, push, selection, setSelection, expanded, setExpanded,
    tab, setTab, logOpen, setLogOpen, follow, setFollow, query, setQuery, dialog, setDialog, busy, setBusy, tip, setTip };
}

function clock() {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

function App() {
  const s = useConsole();
  const inst = s.instances.find((i) => i.project === s.selection.project);
  const bench = inst && s.selection.bench != null ? inst.benches.find((b) => b.index === s.selection.bench) : null;
  const filtered = s.query
    ? s.instances.filter((i) => i.project.includes(s.query) || i.benches.some((b) => b.apps.some((a) => a.includes(s.query)) || b.sites.some((x) => x.includes(s.query))))
    : s.instances;

  function mutate(project, fn) {
    s.setInstances((list) => list.map((i) => (i.project === project ? fn(JSON.parse(JSON.stringify(i))) : i)));
  }

  function run(id) {
    const project = s.selection.project;
    if (id === "start_instance") {
      s.push("ACTION", project, "start instance");
      mutate(project, (i) => { i.container_running = true; i.docker_status = "running"; i.overall = "unknown"; return i; });
      setTimeout(() => { s.push("INSTANT", project, "start(frappe) → unknown"); }, 300);
      setTimeout(() => {
        mutate(project, (i) => { i.overall = "running"; i.probe_ms = 251; i.probed_at = clock(); return i; });
        s.push("FAST", project, "→ running");
      }, 1400);
    }
    if (id === "stop_instance") {
      s.push("ACTION", project, "stop instance");
      mutate(project, (i) => { i.container_running = false; i.docker_status = "exited"; i.overall = "offline"; i.probe_ms = null; i.probed_at = null; return i; });
      setTimeout(() => s.push("INSTANT", project, "stop → offline"), 300);
    }
    if (id === "restart_instance") {
      s.push("ACTION", project, "restart instance");
      mutate(project, (i) => { i.overall = "unknown"; return i; });
      setTimeout(() => { mutate(project, (i) => { i.overall = "running"; return i; }); s.push("FAST", project, "→ running"); }, 1400);
    }
    if (id === "refresh_status") {
      s.push("ACTION", project, "refresh status");
      mutate(project, (i) => { if (i.container_running) { i.overall = i.benches.some((b) => b.overall === "degraded") ? "degraded" : "running"; i.probe_ms = 233 + Math.round(Math.random() * 60); i.probed_at = clock(); } return i; });
    }
    if (id === "restart_process") {
      const name = s.selection.process;
      s.push("ACTION", project, "restart process " + name);
      mutate(project, (i) => {
        const b = i.benches.find((x) => x.index === s.selection.bench);
        const p = b && b.processes.find((x) => x.label === name);
        if (p) { p.state = "STARTING"; p.pid = 4000 + Math.round(Math.random() * 900); p.uptime = "0s"; }
        return i;
      });
      setTimeout(() => {
        mutate(project, (i) => {
          const b = i.benches.find((x) => x.index === s.selection.bench);
          const p = b && b.processes.find((x) => x.label === name);
          if (p) { p.state = "RUNNING"; p.uptime = "3s"; p.cpu = 0.6; p.rss = "104 MB"; }
          if (b) b.overall = b.processes.every((x) => x.state === "RUNNING") ? "running" : "degraded";
          i.overall = i.benches.every((x) => x.overall === "running") ? "running" : "degraded";
          return i;
        });
        s.push("FAST", project, name + " → RUNNING");
      }, 1200);
    }
    if (id === "remove_instance") s.setDialog("remove");
  }

  const railGroups = [
    {
      title: "Instance",
      actions: [
        inst && inst.container_running
          ? { id: "stop_instance", label: "Stop instance", icon: "square" }
          : { id: "start_instance", label: "Start instance", icon: "play" },
        { id: "restart_instance", label: "Restart instance", icon: "refresh-cw", disabledReason: inst && inst.container_running ? "" : "The instance is stopped", disabledHint: "stopped" },
        { id: "refresh_status", label: "Refresh status", icon: "activity" }
      ]
    },
    {
      title: "Process",
      actions: [
        { id: "restart_process", label: "Restart process", icon: "rotate-cw", disabledReason: s.selection.kind === "process" ? "" : "Select a process in the tree", disabledHint: "one process" }
      ]
    },
    {
      title: "Not in the safe set",
      actions: [
        { id: "remove_instance", label: "Remove instance", icon: "trash-2", tone: "danger" }
      ]
    }
  ];

  return (
    <div style={{ position: "relative", display: "flex", flexDirection: "column", height: "100%", minHeight: 0, background: "var(--bg-app)", overflow: "hidden" }}>
      <TitleBar query={s.query} onSearch={s.setQuery} />
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <FleetTree
          instances={filtered}
          selection={s.selection}
          expanded={s.expanded}
          onToggle={(k) => s.setExpanded((e) => ({ ...e, [k]: !e[k] }))}
          onSelect={(sel) => { s.setSelection(sel); if (sel.kind === "instance") s.setTab("processes"); }}
          onNew={() => s.setDialog("new")}
        />
        <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, background: "var(--bg-app)" }}>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
            {!inst ? (
              <EmptyState icon="search" title="Nothing matches that search" command={"cwcli where " + (s.query || "<term>")}>
                The cache holds no app or site containing “{s.query}”.
              </EmptyState>
            ) : s.selection.kind === "instance" || !bench ? (
              <React.Fragment>
                <PaneHeader
                  title={inst.project}
                  subtitle={inst.container_running ? inst.ports.join("  ·  ") : "container stopped"}
                  status={inst.overall}
                  right={<div style={{ display: "flex", gap: "var(--space-3)" }}>
                    <IconButton icon="external-link" title="Open in browser" variant="secondary" />
                    <IconButton icon="folder" title="Open in VS Code" variant="secondary" />
                  </div>}
                />
                <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}><InstanceOverview inst={inst} /></div>
              </React.Fragment>
            ) : (
              <React.Fragment>
                <PaneHeader
                  title={inst.project + " / [" + bench.index + "] " + bench.label}
                  subtitle={bench.bench_path + "   " + bench.web_site + ":" + bench.web_port + (bench.web_http_code == null ? " → no answer" : " → " + bench.web_http_code)}
                  status={bench.overall}
                  right={<div style={{ display: "flex", gap: "var(--space-3)" }}>
                    <IconButton icon="external-link" title="Open site" variant="secondary" />
                    <IconButton icon="pencil" title="Set bench label" variant="secondary" />
                  </div>}
                />
                {bench.overall === "degraded" ? (
                  <div style={{ padding: "0 var(--space-8) var(--space-6)" }}>
                    <Banner tone="warn" title="One process is down on this bench" hint={"cwcli logs " + inst.project + " --bench " + bench.index + " --process web"}>
                      The bench answers nothing on {bench.web_site}:{bench.web_port}. Restarting the process is a safe action; select it in the tree.
                    </Banner>
                  </div>
                ) : null}
                <BenchDetail
                  bench={bench} tab={s.tab} onTab={s.setTab}
                  selectedProcess={s.selection.kind === "process" ? s.selection.process : null}
                  onSelectProcess={(name) => s.setSelection({ kind: "process", project: inst.project, bench: bench.index, process: name })}
                  logs={window.CWData.logs} follow={s.follow} onFollow={() => s.setFollow((v) => !v)}
                />
              </React.Fragment>
            )}
          </div>
          <EventLog events={s.events} open={s.logOpen} onToggle={() => s.setLogOpen((v) => !v)} />
        </main>
        <ActionRail groups={railGroups} onAction={run} />
      </div>
      <StatusBar daemon={window.CWData.daemon} probeMs={inst ? inst.probe_ms : null} />

      <NewInstanceDialog
        open={s.dialog === "new"} busy={s.busy} tip={s.tip}
        onClose={() => s.setDialog(null)}
        onCreate={() => {
          s.setBusy(true);
          let n = 0;
          const t = setInterval(() => { n += 1; s.setTip(window.CWData.tips[n % window.CWData.tips.length]); }, 1600);
          setTimeout(() => {
            clearInterval(t); s.setBusy(false); s.setDialog(null);
            s.setInstances((list) => [...list, {
              project: "new-erp", docker_status: "running", container_running: true, ports: ["18000-18005"],
              overall: "unknown", web_probed: false, probe_ms: null, probed_at: null, benches: []
            }]);
            s.push("INSTANT", "new-erp", "start(frappe) → unknown");
          }, 4200);
        }}
      />
      <RemoveDialog
        open={s.dialog === "remove"} project={s.selection.project} volumes={false}
        onVolumes={() => {}}
        onClose={() => s.setDialog(null)}
        onConfirm={() => {
          const p = s.selection.project;
          s.setInstances((list) => list.filter((i) => i.project !== p));
          s.push("INSTANT", p, "destroy → gone");
          s.setSelection({ kind: "instance", project: "my-erp" });
          s.setDialog(null);
        }}
      />
    </div>
  );
}

Object.assign(window, { App });
