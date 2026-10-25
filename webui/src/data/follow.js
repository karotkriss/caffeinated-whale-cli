// The pure pieces behind every auto-following log pane, kept free of
// imports so tests/test_webui_follow.py runs them under plain node.

// Is the reader at the bottom? Within one text line of the end still counts: a
// fractional scroll offset or a touch overscroll must never pause the follow,
// while a real scroll up (hundreds of pixels) always does.
export const SLACK_PX = 24

export function atBottom({ scrollTop, scrollHeight, clientHeight }, slack = SLACK_PX) {
  return scrollHeight - scrollTop - clientHeight <= slack
}

// The follow state after a scroll event. Only a real upward scroll pauses it: a
// scroll event the pane's own pin queued can be handled after more content has
// landed, which reads as "not at the bottom" although the reader never moved.
export function nextFollowing(following, lastTop, pane) {
  if (atBottom(pane)) return true
  return pane.scrollTop < lastTop ? false : following
}

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g
const shown = (line) => line.slice(line.lastIndexOf('\r') + 1).replace(ANSI, '')

// Feeds a terminal the next piece of a command's output, touching only that
// piece: `term.lines` gains each line it finishes, and `term.open` holds the line
// still being written, kept raw so an escape sequence split across two pieces is
// stripped once whole. A \r progress bar keeps only its last repaint per line.
export function feed(term, text) {
  const parts = text.split('\n')
  let open = term.open + parts[0]
  for (let i = 1; i < parts.length; i++) {
    term.lines.push(shown(open))
    open = parts[i]
  }
  term.open = open.slice(open.lastIndexOf('\r') + 1)
  return term
}

export function openLine(term) {
  return shown(term.open)
}
