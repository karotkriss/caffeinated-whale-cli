// A tab that outlived a rebuild: its page was built with route chunks that a
// later build has replaced, so the first visit to a not-yet-loaded page (a job's
// detail, say) fails to fetch its chunk. The honest recovery is one full reload
// of the page the user asked for - once, so a build that is genuinely broken
// cannot loop. Pure decision here (tests run it under node); main.js wires it.
export const RELOAD_KEY = 'cwcli:chunk-reload'
export const RELOAD_WINDOW_MS = 30_000

// True when a reload is the right answer for this failure: at most one reload per
// window, keyed on the page that failed, so a second failure in the window falls
// through to the error the router reports.
export function shouldReload(storage, path, now = Date.now()) {
  let last = null
  try {
    last = JSON.parse(storage.getItem(RELOAD_KEY) || 'null')
  } catch {
    last = null
  }
  if (last && last.path === path && now - last.at < RELOAD_WINDOW_MS) return false
  try {
    storage.setItem(RELOAD_KEY, JSON.stringify({ path, at: now }))
  } catch {
    // storage unavailable: still reload once per page life, below
  }
  return true
}

export function isChunkLoadError(error) {
  const text = String(error?.message || error || '')
  return /dynamically imported module|Loading chunk|Importing a module script failed|preload/i.test(text)
}
