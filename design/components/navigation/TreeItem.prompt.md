A row in the left-hand fleet tree. Instances, benches and processes all use it — only `depth` and `icon` change.

```jsx
<TreeItem label="my-erp" icon="box" depth={0} expandable expanded status="running" selected />
<TreeItem label="[0] frappe-bench" icon="layers" depth={1} meta="8000" status="running" />
<TreeItem label="web" icon="terminal" depth={2} status="running" />
```

- Selection is a 2px left accent bar plus a tinted background, never a full border.
