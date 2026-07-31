A log tail rendered on the terminal surface, with the CLI's colour vocabulary.

```jsx
<LogView height={260} lines={[
  { text: "$ cwcli status my-erp", level: "command" },
  { text: "web  RUNNING  pid 4711", level: "success", time: "14:02:14" },
  { text: "worker_default  STOPPED", level: "error" },
]} />
```

- The tail is always bounded — the daemon returns a fixed number of lines and the view says so above itself.
- Levels map to the terminal palette; do not add colours outside `--cw-term-*`.
