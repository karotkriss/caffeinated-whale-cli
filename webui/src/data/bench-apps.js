// Which catalog apps a bench already holds, so a New site never offers one of them as
// a remote pick to fetch again: bench get-app clones into apps/<repo slug> and then
// renames to the app name, so fetching an app the bench has fails on the rename.
// Matched by app name (the bench's apps/) and by repository (a catalog entry is keyed
// by its normalized remote, and lists the benches whose checkout has that remote).
// Dependency-free: tests run it under plain node.

export function splitBenchApps({ catalog = [], benchApps = [], project = null, bench = null, host = null } = {}) {
  const names = new Set(benchApps)
  // The catalog's installs are this machine's instances only.
  const installedHere = (a) => !host && (a.installs || []).some((i) => i.project === project && String(i.bench_index) === String(bench))
  const held = (a) => names.has(a.name) || installedHere(a)
  const onBench = [...new Set([...benchApps, ...catalog.filter(installedHere).map((a) => a.name)])].filter((n) => n && n !== 'frappe')
  const remote = catalog.filter((a) => a.source?.url && a.name !== 'frappe' && !held(a))
  return { onBench, remote }
}

// The bench's app list is LIVE only when inspect read the running bench ('partial' or
// 'full'); 'cache' is what cwcli remembered (a stopped instance, or a failed read).
export function inventoryState(detail) {
  if (!detail || detail.loading) return 'reading'
  if (!detail.data) return 'unknown'
  return detail.data.served_from === 'cache' ? 'remembered' : 'live'
}
