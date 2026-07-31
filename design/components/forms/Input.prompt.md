A single-line text field with an optional label, icon and hint.

```jsx
<Input label="Site" placeholder="console.localhost" mono icon="globe" />
<Input label="Port" mono error="Port 8000 is already published by another instance." />
```

- `mono` for anything a machine will read back: ports, paths, sites, branches, tickets.
- The error string names the fix, in the CLI's voice — it never says "Invalid input".
