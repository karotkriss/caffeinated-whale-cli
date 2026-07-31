States an instance's or bench's health using the five tokens the fleet model can actually produce.

```jsx
<StatusPill status="running" />
<StatusPill status="degraded" size="sm" />
<StatusPill status="unknown" />
```

- The five tokens are `running | online | degraded | offline | unknown`. There is no sixth, and `unknown` is a real state — never render it as a dash, a zero, or a healthy default.
- Lowercase, monospace, always. The CLI prints these words; the app must not retitle them.
