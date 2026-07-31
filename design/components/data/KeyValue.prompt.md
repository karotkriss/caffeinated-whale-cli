A labelled fact in the detail pane.

```jsx
<KeyValue label="Bench path" value="/workspace/frappe-bench" />
<KeyValue label="Web HTTP" value={null} hint="never probed" />
```

- A missing value renders as an em dash with a hint saying which kind of missing it is: "never probed" and "no answer" are different facts.
