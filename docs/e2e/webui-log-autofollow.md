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
`FollowPane` is a windowed list built on `@tanstack/vue-virtual`: only the rows in and near the viewport are in the DOM, however long the log grows, and each rendered row reports its real height through `measureElement`, so long event titles, output lines, and log lines wrap in full (on a 390px phone too) instead of being cut or scrolled sideways.
On the job page each row is either an event or one line of an output block, and the block's lines come from `feed` in `webui/src/data/follow.js`, which processes only each newly appended chunk (escape sequences stripped, a `\r` progress bar reduced to its last repaint) and keeps the lines as plain strings.
The bench Logs tab renders the same way: one row per log line, every process's lines in arrival order, with the process named on each row under the `All` filter.
Nothing is truncated: every line stays reachable by scrolling, and there is no cap, `Show all` control, or expand state.
Rows are only ever appended, so a paused reader keeps reading while new lines land below them, and nothing above them moves.

Measured on the virtualized build with a synthetic running job whose first output event is one 30,000-line block (the real init's shape) followed by three streamed lines every 50 ms, watched with chrome-devtools-axi while it ran:

| Width | Rows in the DOM | Long tasks (>50 ms) over 4 s of streaming | Horizontal overflow | Tallest rendered row |
| --- | --- | --- | --- | --- |
| 1280x800 | 44-65 of ~33,000 | none | 0 px | 30 px |
| 390x844 | 29-50 of ~33,000 | none | 0 px | 70 px (every rendered line wrapped) |

Per refresh the page appends the new rows and the virtualizer measures only the rows it renders, so the cost no longer grows with the block: the 437 ms + 1,482 ms per refresh above became no long task at all.

## Auto-follow, pause, and resume

Measured on the virtualized build at both widths, with the same synthetic job streaming while a 30,000-line block was already on the page (`distance` is `scrollHeight - scrollTop - clientHeight` of the pane, sampled every 500 ms):

| Width | Phase | Observed |
| --- | --- | --- |
| 1280x800 | following while streaming | `distance` 0 at every settled sample while `scrollHeight` grew 659,082 -> 661,532; no jump control |
| 1280x800 | after scrolling up 400px | `scrollTop` held at 677,884 while `scrollHeight` grew 678,762 -> 682,412; "Jump to latest" shown |
| 1280x800 | after clicking "Jump to latest" | `distance` 0 throughout further growth (682,962 -> 687,402); control hidden |
| 390x844 | following while streaming | `distance` 0 throughout, `scrollHeight` 744,782 -> 755,582 |
| 390x844 | after scrolling up 300px | `scrollTop` held at 757,178 while `scrollHeight` grew 759,212 -> 761,552; control shown |
| 390x844 | after tapping "Jump to latest" | `distance` back to 0 and held through further growth |

A faster read (60 samples 100 ms apart inside animation frames at 390) found 58 at `distance` 0 and two transient non-zero reads that returned to 0 within 100 ms: a row measured after a pin, re-pinned on the next frame.

The earlier build of this change, before the views were virtualized, gave the same follow, pause, and resume results:
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
