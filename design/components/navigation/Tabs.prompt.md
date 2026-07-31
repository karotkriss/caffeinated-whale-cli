Detail-pane tabs. One row, underlined, no pills.

```jsx
<Tabs value={tab} onChange={setTab} items={[
  { value: "overview", label: "Overview", icon: "activity" },
  { value: "processes", label: "Processes", icon: "cpu", count: 6 },
  { value: "logs", label: "Logs", icon: "scroll-text" },
]} />
```
