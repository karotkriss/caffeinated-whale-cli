A dense table. Column tints deliberately mirror the CLI's Rich table styles, so `cwcli ls` and the desktop list read the same.

```jsx
<DataTable
  columns={[
    { key: "project", header: "Project name", width: "200px", tint: "cyan" },
    { key: "status", header: "Status", width: "120px", tint: "magenta" },
    { key: "ports", header: "Ports", tint: "green" },
  ]}
  rows={[{ project: "my-erp", status: "running", ports: "8000-8005" }]}
/>
```

- Keep the CLI's tints: project cyan, status magenta, ports green. Do not recolour them per screen.
