A busy indicator. The block form carries a rotating tip, exactly as the CLI's TipSpinner does during long phases.

```jsx
<Spinner label="Provisioning bench" tip="Use 'cwcli inspect <project>' to cache project structure for faster commands" />
<Spinner inline label="Probing…" size={14} />
```

- Tips come from the CLI's curated list (`utils/tips.py`); do not write new ones.
- A long phase must show progress. "Looking hung" is a bug the CLI fixed once already.
