A labelled action button; the default building block for every command the UI can issue.

```jsx
<Button variant="primary" icon="play">Start instance</Button>
<Button variant="secondary" icon="rotate-cw">Restart process</Button>
<Button variant="danger" icon="trash-2">Remove instance</Button>
```

- `primary` is reserved for the affirmative action in a view; never two per pane.
- `danger` means the action destroys data (rm, rm-site, volume removal) and always sits behind a Dialog.
- `loading` swaps the icon for a spinner — use it for a synchronous action endpoint call, not for a long job.
