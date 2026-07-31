A single delta in the event log — the app's honest record of what changed and which tier saw it.

```jsx
<EventRow tier="INSTANT" project="my-erp" message="start(frappe) → unknown" time="14:02:11" fresh />
<EventRow tier="FAST" project="my-erp" message="→ running" time="14:02:14" />
```

- Tier colours are fixed: INSTANT magenta, FAST cyan, ACTION green, ERROR red.
- The message reads like the CLI's own line, arrow included. Do not sentence-case it.
