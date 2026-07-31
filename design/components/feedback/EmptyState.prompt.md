Shown when a pane has nothing to report. It names what is missing and offers the way forward.

```jsx
<EmptyState icon="box" title="No instances on this Docker daemon"
  command="cwcli init my-erp --version 16"
  action={<Button variant="primary" icon="plus">New instance</Button>}>
  cwcli found no Frappe or ERPNext projects here.
</EmptyState>
```

- Every empty state shows the equivalent CLI command. The desktop app is a face on the CLI and never hides it.
