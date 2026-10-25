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

// A pane renders at most this many lines of one block. A single collapsed
// output block of a real init reached 30,000 lines (7.7 MB), and replacing that
// one text node on every refresh froze the page for 1.5 s; the last 2,000 lines
// patch in under 60 ms. "Show all" renders the rest on demand.
export const MAX_LINES = 2000

// The last `max` lines of `text`, found by scanning back from the end; counting
// the hidden lines still walks the prefix, with native indexOf.
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

// `text` without its first `n` lines: a paused reader's block keeps the top it
// had when they scrolled up, and only grows at the bottom.
export function dropLines(text, n) {
  let cut = -1
  for (let dropped = 0; dropped < n; dropped++) {
    cut = text.indexOf('\n', cut + 1)
    if (cut < 0) return { hidden: 0, text }
  }
  return { hidden: n, text: text.slice(cut + 1) }
}

// What one output block renders: everything when the reader asked for it, the
// lines held since the follow paused, else the capped tail.
export function blockView(text, { all = false, held = null } = {}) {
  if (all) return { hidden: 0, text }
  return held == null ? tailLines(text) : dropLines(text, held)
}
