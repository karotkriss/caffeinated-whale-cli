The visual form of cwcli's error envelope: a typed message, a plain-words body, and a `help:` line naming the fix.

```jsx
<Banner tone="danger" title="Could not connect to Docker" code="DOCKER_UNREACHABLE"
  hint="start Docker Desktop, then Retry">
  The daemon is not answering on this machine, so no instance can be read.
</Banner>
```

- Always give `hint` when a fix exists. An error with no next step is not finished.
- `code` is the machine code, right-aligned and dim; it is for copy-paste, not for reading.
