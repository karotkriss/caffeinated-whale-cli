// The two pure pieces behind every auto-following log pane, kept free of
// imports so tests/test_webui_follow.py runs them under plain node.

// Is the reader at the bottom? Within one text line of the end still counts: a
// fractional scroll offset or a touch overscroll must never pause the follow,
// while a real scroll up (hundreds of pixels) always does.
export const SLACK_PX = 24

export function atBottom({ scrollTop, scrollHeight, clientHeight }, slack = SLACK_PX) {
  return scrollHeight - scrollTop - clientHeight <= slack
}

// A pane renders at most this many lines of one block. A single collapsed
// output block of a real init reached 30,000 lines (7.7 MB), and replacing that
// one text node on every refresh froze the page for 1.5 s; the last 2,000 lines
// patch in under 60 ms. The full output stays in the job record on disk.
export const MAX_LINES = 2000

// The last `max` lines of `text`, found by scanning back from the end so the
// cost is the tail's size, never the block's.
export function tailLines(text, max = MAX_LINES) {
  let cut = text.length
  for (let kept = 0; kept < max; kept++) {
    cut = text.lastIndexOf('\n', cut - 1)
    if (cut < 0) return { hidden: 0, text }
  }
  let hidden = 1
  for (let i = text.indexOf('\n'); i !== -1 && i < cut; i = text.indexOf('\n', i + 1)) hidden++
  return { hidden, text: text.slice(cut + 1) }
}
