One supervisord program row: state, pid, and the volatile numbers.

```jsx
<ProcessRow name="web" state="RUNNING" pid={4711} uptime="4h 12m" cpu={1.4} rss="212 MB" />
<ProcessRow name="worker_default" state="STOPPED" pid={null} />
```

- Program names are raw Procfile keys in monospace (`worker_default`, not "Default worker").
- An unmeasured number renders as an em dash. Never substitute 0.
