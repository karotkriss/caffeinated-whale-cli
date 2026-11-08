// The branch, tag or commit of each app picked for a site (New site's app picker).
// An app the bench holds is installed as the copy it has unless another ref is asked
// for, and then the copy is checked out there FIRST, which moves it for every site on
// that bench (core/new_site.py); a remote pick is fetched at its ref. Dependency-free:
// tests run it under plain node.

// The branch the bench's copy of `app` sits at, from the `name version branch` entry
// of a site on the bench that has it installed (fleet.js's parseApp shape); null when
// no site on the bench has it, or the entry names no branch.
export function benchBranchOf(bench, app) {
  for (const site of bench?.sites || []) {
    for (const entry of site.installed_apps || []) {
      const [name, , ...rest] = String(entry).trim().split(/\s+/)
      if (name === app && rest.length) return rest.join(' ')
    }
  }
  return null
}

// The sites on the bench with `app` installed: the ones a checkout of it moves.
export function sitesHolding(bench, app) {
  return (bench?.sites || []).filter((s) => (s.installed_apps || []).some((e) => String(e).trim().split(/\s+/)[0] === app)).map((s) => s.name)
}

// What picking `ref` for a bench app does, said before it runs; '' when `ref` is the
// branch the bench already has (nothing moves).
export function branchChangeNote(app, ref, bench) {
  const sites = sitesHolding(bench, app)
  const at = benchBranchOf(bench, app)
  if (at && at === ref) return ''
  const move = `Create site first checks out ${ref} in the bench's copy of ${app}${at ? ` (now at ${at})` : ''}`
  if (!sites.length) return `${move}; no other site on this bench has it installed.`
  return `${move}, which moves it for every site on this bench: ${sites.join(', ')}.`
}

// The plan endpoint's `ref` list, one per target in target order ('' leaves that
// target's ref to the plan); null when no ref was picked, so the query stays as before.
export function alignedRefs(targets, refByTarget) {
  const refs = targets.map((t) => refByTarget[t] || '')
  return refs.some(Boolean) ? refs : null
}
