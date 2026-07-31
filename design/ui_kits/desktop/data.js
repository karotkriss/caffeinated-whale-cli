// Fixture shaped exactly like core.fleet.InstanceState → BenchStatus → ProcessHealth.
window.CWData = {
  daemon: { host: "127.0.0.1", port: 8777, version: "2.1.0", docker: "running" },
  instances: [
    {
      project: "my-erp", docker_status: "running", container_running: true,
      ports: ["8000-8005", "9000-9005"], overall: "running", web_probed: true,
      probe_ms: 248, probed_at: "14:02:14",
      benches: [
        {
          index: 0, bench_path: "/workspace/development/frappe-bench", label: "main",
          overall: "running", supervisor_up: true, web_port: 8000, web_port_verified: true,
          web_site: "erp.localhost", web_http_code: 200, bench_present: true, not_cwcli_supervised: false,
          apps: ["frappe", "erpnext", "hrms", "payments"], sites: ["erp.localhost"],
          processes: [
            { label: "web", state: "RUNNING", pid: 4711, uptime: "4h 12m", cpu: 1.4, rss: "212 MB" },
            { label: "socketio", state: "RUNNING", pid: 4712, uptime: "4h 12m", cpu: 0.2, rss: "88 MB" },
            { label: "watch", state: "RUNNING", pid: 4713, uptime: "4h 12m", cpu: 0.9, rss: "164 MB" },
            { label: "schedule", state: "RUNNING", pid: 4714, uptime: "4h 12m", cpu: 0.1, rss: "72 MB" },
            { label: "worker_default", state: "RUNNING", pid: 4715, uptime: "4h 12m", cpu: 0.3, rss: "118 MB" },
            { label: "worker_short", state: "RUNNING", pid: 4716, uptime: "4h 12m", cpu: 0.1, rss: "112 MB" }
          ]
        },
        {
          index: 1, bench_path: "/workspace/development/staging-bench", label: "staging",
          overall: "degraded", supervisor_up: true, web_port: 8001, web_port_verified: true,
          web_site: "staging.localhost", web_http_code: null, bench_present: true, not_cwcli_supervised: false,
          apps: ["frappe", "erpnext"], sites: ["staging.localhost"],
          processes: [
            { label: "web", state: "STOPPED", pid: null, uptime: null, cpu: null, rss: null },
            { label: "socketio", state: "RUNNING", pid: 5120, uptime: "1h 03m", cpu: 0.2, rss: "84 MB" },
            { label: "schedule", state: "RUNNING", pid: 5121, uptime: "1h 03m", cpu: 0.1, rss: "70 MB" },
            { label: "worker_default", state: "BACKOFF", pid: null, uptime: null, cpu: null, rss: null }
          ]
        }
      ]
    },
    {
      project: "hr-sandbox", docker_status: "running", container_running: true,
      ports: ["16000-16005"], overall: "unknown", web_probed: false, probe_ms: null, probed_at: null,
      benches: []
    },
    {
      project: "old-proj", docker_status: "exited", container_running: false,
      ports: [], overall: "offline", web_probed: false, probe_ms: null, probed_at: null,
      benches: []
    }
  ],
  events: [
    { tier: "INSTANT", project: "my-erp", message: "start(frappe) → unknown", time: "14:02:11" },
    { tier: "FAST", project: "my-erp", message: "→ degraded", time: "14:02:13" },
    { tier: "FAST", project: "my-erp", message: "→ running", time: "14:02:14" },
    { tier: "FAST", project: "my-erp", message: "[1] staging-bench → degraded", time: "14:02:16" },
    { tier: "INSTANT", project: "hr-sandbox", message: "start(mariadb) → unknown", time: "14:03:02" }
  ],
  logs: [
    { time: "14:02:09", level: "command", text: "$ supervisorctl restart frappe-bench-frappe:web" },
    { time: "14:02:10", level: "info", text: "web: stopped" },
    { time: "14:02:11", level: "info", text: "web: started" },
    { time: "14:02:11", level: "debug", text: "bench serve --port 8000" },
    { time: "14:02:12", level: "success", text: "* Serving Flask app 'frappe.app'" },
    { time: "14:02:12", level: "warn", text: "* Debug mode: on" },
    { time: "14:02:13", level: "info", text: "* Running on http://0.0.0.0:8000" },
    { time: "14:02:14", level: "info", text: "127.0.0.1 - - [24/Jul/2026 14:02:14] \"GET /api/method/ping HTTP/1.1\" 200 -" },
    { time: "14:02:18", level: "error", text: "worker_default: entered FATAL state, too many start retries too quickly" },
    { time: "14:02:19", level: "debug", text: "supervisor: reaping worker_default" }
  ],
  tips: [
    "Use 'cwcli inspect <project>' to cache project structure for faster commands",
    "Install tab completion with 'cwcli --install-completion' for faster workflows",
    "Run 'cwcli backup <project> --with-files' before major changes",
    "Unlock stuck sites with 'cwcli unlock <project> --site <site-name>'"
  ]
};
