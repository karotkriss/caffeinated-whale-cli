# UI kit — cwcli terminal surface

The CLI is the product's first surface, and the desktop app inherits its vocabulary. This kit pins
how cwcli looks so mocks, docs and marketing renders stay honest.

Three modes, all real shapes from the source:

1. **Rich table** — `cwcli ls`. Column styles come straight from `commands/list.py`:
   Project Name `cyan`, Status `magenta` (overridden green/yellow/red by value), Ports `green`.
2. **TOON** — `cwcli axi <verb>`. The agent surface prints one TOON document on stdout, never JSON.
   Actionable errors end with a `help:` line; self-contained reads omit it.
3. **Typed error** — the `CwcliError` envelope: `error:`, the options it could not choose between,
   and a `help:` line naming the fix.

The spinner at the bottom is `utils/tips.py`'s TipSpinner: a dots spinner, a bold green status line,
and a dim rotating tip every four seconds.

Terminal colours are the `--cw-term-*` tokens. Anything that renders CLI output — a doc, a screenshot
mock, a log pane in the desktop app — uses these and nothing else.
