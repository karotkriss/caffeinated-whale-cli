// Whether this machine's Docker is reachable, as the daemon reports it: `{reachable,
// error, since}` in the stream's snapshot and in every `docker` event. While it is
// down every instance row is the last thing Docker said before it went away, so the
// pages that read this machine's Docker show one "not reachable" state instead of
// stale rows or a wall of errors. `reachable: null` is "not asked yet", never down.
export const dockerDown = (docker) => docker?.reachable === false

// The pages replaced by that state. A remote host's pages read another daemon's
// Docker, so they stay; the two lists also hold remote hosts' rows, so they are
// replaced only on a machine with no linked hosts (the strip still says it there).
const LOCAL_PAGES = new Set(['instance', 'bench', 'site'])
const LIST_PAGES = new Set(['instances', 'sites'])
export function dockerGates(docker, routeName, { hasHosts = false } = {}) {
  if (!dockerDown(docker)) return false
  return LOCAL_PAGES.has(routeName) || (LIST_PAGES.has(routeName) && !hasHosts)
}

// The moment to re-read what the outage left stale (each instance's detail).
export const dockerCameBack = (before, after) => dockerDown(before) && after?.reachable === true
