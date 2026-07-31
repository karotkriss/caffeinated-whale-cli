A modal for a decision with consequences — never for information alone.

```jsx
<Dialog tone="danger" title="Remove this instance?" subtitle="my-erp"
  footer={<><Button variant="ghost">Cancel</Button><Button variant="danger" icon="trash-2">Remove</Button></>}>
  <p>Containers, the project directory and every bench on it are deleted.</p>
  <Checkbox label="Also remove named volumes" hint="This deletes the database. There is no undo." />
</Dialog>
```

- The subtitle is the exact target in monospace, so a person can check what they are about to destroy.
- The confirm button repeats the verb ("Remove"), never "OK".
- The Dialog positions itself absolutely inside the nearest positioned ancestor, so a window mock can host it.
