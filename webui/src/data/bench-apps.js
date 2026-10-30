// Which catalog apps a bench already holds, so a New site never offers one of them as
// a remote pick to fetch again: bench get-app clones into apps/<repo slug> and then
// renames to the app name, so fetching an app the bench has fails on the rename.
// Matched against the bench's freshly read detail: by app name (its apps/), and by
// repository (each checkout's credential-free remote urls against a catalog entry's
// host/path key), so a repository named unlike its app is still on the bench.
// Dependency-free: tests run it under plain node.

export function splitBenchApps({ catalog = [], benchApps = [], benchRemotes = {} } = {}) {
  const keys = new Set(Object.values(benchRemotes).flat().map((u) => u.replace(/^https:\/\//, '')))
  const onBench = benchApps.filter((n) => n && n !== 'frappe')
  const remote = catalog.filter((a) => a.source?.url && a.name !== 'frappe' && !benchApps.includes(a.name) && !keys.has(a.key))
  return { onBench, remote }
}

// The bench's app list is LIVE only when inspect read the running bench ('partial' or
// 'full'); 'cache' is what cwcli remembered (a stopped instance, or a failed read), and
// so is an earlier answer kept after the latest read failed.
export function inventoryState(detail) {
  if (!detail || detail.loading) return 'reading'
  if (!detail.data) return 'unknown'
  return detail.error || detail.data.served_from === 'cache' ? 'remembered' : 'live'
}
