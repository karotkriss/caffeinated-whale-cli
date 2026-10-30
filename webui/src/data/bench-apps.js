// Which catalog apps a bench already holds, so a New site never offers one of them as
// a remote pick to fetch again: bench get-app clones into apps/<repo slug> and then
// renames to the app name, so fetching an app the bench has fails on the rename.
// Matched by app name against the bench's freshly read apps/; the fetch itself also
// matches by repository (core/apps.py), so a differently named checkout is not
// fetched again either. Dependency-free: tests run it under plain node.

export function splitBenchApps({ catalog = [], benchApps = [] } = {}) {
  const onBench = benchApps.filter((n) => n && n !== 'frappe')
  const remote = catalog.filter((a) => a.source?.url && a.name !== 'frappe' && !benchApps.includes(a.name))
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
