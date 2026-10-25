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
Its longest collapsed output block was 30,378 lines (7.7 MB) in one text node, and re-rendering it cost 437 ms of string work plus 1,482 ms of DOM patch and layout per refresh.
Capped to the block's last 2,000 lines (`MAX_LINES`, the cap the Logs tab already applied to its own buffer) the patch costs 58 ms; the block shows a `Show all 30,378 lines` control above the tail that renders the full output on demand; collapsing it again reads `Show last N lines`, where N is the line count of the view it returns to (the capped tail, or the lines held since the pause).
`Jump to latest` collapses every expanded block back to the capped tail.
While the follow is paused, a block stops dropping lines from its top, so the text never slides under a reader who scrolled up; trimming resumes on `Jump to latest`.
The bench Logs tab follows the same rule: while paused it keeps every line it holds and stops polling once a process buffer reaches 20,000 lines (`PAUSED_MAX_LINES`), and its byte cursor resumes where it stopped after `Jump to latest`.

## Auto-follow, pause, and resume

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
