# Web UI log views follow their newest lines

Browser evidence for the auto-follow fix, taken with chrome-devtools-axi against the daemon built from this change (`cwcli ui --foreground` under an isolated `CWCLI_HOME`).
Every log view (the job page's event log and the bench Logs tab) renders through `webui/src/components/FollowPane.vue`.

## The defect, reproduced on the unfixed build

A real `init` job (Frappe 16, no ERPNext) was created through `POST /api/v1/jobs` and its page opened at 1280x800 while it ran.

| Measurement at 81 events | Value |
| --- | --- |
| shell scroller `scrollTop` / `scrollHeight` / `clientHeight` | 0 / 1751 / 752 |
| newest output block top edge | y=1387, below the 800px viewport |
| that block's own `scrollTop` / `scrollHeight` / `clientHeight` | 0 / 1680 / 320 |

The page sat at the top while events landed below the fold, and the newest lines were hidden a second time inside the block's own 20rem scroller.

## Very long logs

The same init job reached 18,108 events.
Its longest collapsed output block was 30,378 lines (7.7 MB) in one text node, and on the pre-change build re-rendering it cost 437 ms of string work plus 1,482 ms of DOM patch and layout per refresh.

The redesign removes that cost at its source instead of capping the output.
`FollowPane` is a windowed list of fixed-height rows built on `@vueuse/core`'s `useVirtualList`, so only the rows in and near the viewport are in the DOM, however long the log grows.
On the job page each row is either an event or one line of an output block, and the block's lines come from `feed` in `webui/src/data/follow.js`, which processes only each newly appended chunk (escape sequences stripped, a `\r` progress bar reduced to its last repaint) and keeps the lines as plain strings.
The bench Logs tab renders the same way: one row per log line, every process's lines in arrival order, with the process named on each row under the `All` filter.
Nothing is truncated: every line stays reachable by scrolling, and there is no cap, `Show all` control, or expand state.
Rows are only ever appended, so a paused reader keeps reading while new lines land below them, and nothing above them moves.

Timings for the virtualized build are taken in a real browser after this change and recorded in the pull request, not here.

## Auto-follow, pause, and resume

Measured on the earlier build of this change, before the log views were virtualized; the follow, pause, and resume rules in `FollowPane` are unchanged since.
A synthetic running job streaming three lines every 50 ms through the package's own job recorder (no instance needed) was watched at both widths.
Chrome was closed between screenshots.
`distance` is `scrollHeight - scrollTop - clientHeight` of the pane.

| Width | Phase | Observed over 3-4 s of samples |
| --- | --- | --- |
| 1280x800 | following while streaming | `scrollHeight` 47,589 -> 52,976, `distance` 0 throughout, no jump control |
| 1280x800 | after scrolling up 400px | `scrollTop` held at 71,741 while `scrollHeight` grew 73,470 -> 78,978; "Jump to latest" shown |
| 1280x800 | after clicking "Jump to latest" | `distance` 0 throughout further growth (80,598 -> 86,633); control hidden |
| 390x844 | following while streaming | `scrollHeight` 401,189 -> 412,978, `distance` 0 throughout |
| 390x844 | after scrolling up 300px | `scrollTop` held at 448,146 while `scrollHeight` grew 475,763 -> 487,492; control shown |
| 390x844 | after tapping "Jump to latest" | `distance` 0 through the job's end |

Per-refresh long tasks while streaming stayed at 50-66 ms on the test box.
The pane ends at y=679 of 800 (1280) and y=773 of 844 (390), so the newest line is on screen without scrolling the page.
