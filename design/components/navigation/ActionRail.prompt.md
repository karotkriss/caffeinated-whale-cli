The right-hand rail holding the small, safe, synchronous action set (the CLI's Tier A ruling).

```jsx
<ActionRail
  onAction={run}
  groups={[{ title: "Instance", actions: [
    { id: "start_instance", label: "Start instance", icon: "play" },
    { id: "restart_process", label: "Restart process", icon: "rotate-cw", disabledReason: "Select a process first", disabledHint: "one process" },
  ]}]}
/>
```

- Every disabled action carries `disabledReason`. A dead button that will not say why is the defect this rail exists to avoid.
- Destructive verbs are not Tier A. Point at the CLI command instead of adding them here.
