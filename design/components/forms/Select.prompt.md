A native select, used where the CLI would demand a `--bench` or `--lines` flag.

```jsx
<Select label="Bench" mono options={[{value:0,label:"[0] /workspace/frappe-bench"},{value:1,label:"[1] /workspace/staging-bench"}]} />
```

- Bench options keep their index prefix, exactly as `cwcli axi benches` prints them.
