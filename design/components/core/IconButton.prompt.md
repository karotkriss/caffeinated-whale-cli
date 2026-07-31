A square icon-only button for window chrome, toolbars and row-level affordances.

```jsx
<IconButton icon="refresh-cw" title="Refresh status" />
<IconButton icon="panel-left" title="Toggle fleet tree" size="sm" />
```

- `title` doubles as the accessible name; never ship one without it.
- Use `ghost` inside dense rows, `secondary` when it stands alone next to an input.
