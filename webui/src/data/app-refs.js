// The branch, tag or commit of each app picked for a site (New site's app picker).
// An app the bench holds is installed as the copy it has unless another ref is asked
// for, and then the copy is checked out there FIRST, which moves it for every site on
// that bench (core/new_site.py); a remote pick is fetched at its ref. Dependency-free:
// tests run it under plain node.

// The plan node of the bench app picked as `app` (the plan names a bench pick by its app).
const pickedNode = (plan, app) => (plan?.nodes || []).find((n) => n.requested && n.requested_as === app) || null

// The branch the bench's copy of `app` sits at, as the plan read it live from the copy's
// .git/HEAD; null while the plan has not read it, or plans a checkout of it instead.
export function liveBranchOf(plan, app) {
  const n = pickedNode(plan, app)
  return n && !n.checkout ? n.ref || null : null
}

// The sites on the bench with `app` installed: the ones a checkout of it moves.
export function sitesHolding(bench, app) {
  return (bench?.sites || []).filter((s) => (s.installed_apps || []).some((e) => String(e).trim().split(/\s+/)[0] === app)).map((s) => s.name)
}

// Whether Create site checks the bench's copy of `app` out at `ref` first, decided as the
// job decides it (the live plan): null while the plan has not planned that pick at `ref`.
export function movesTo(plan, app, ref) {
  if (!ref) return false
  const n = pickedNode(plan, app)
  if (!n) return null
  if (n.checkout) return n.ref === ref ? true : null
  return n.ref !== ref
}

// What picking `ref` for bench app `app` does, said before it runs; '' when nothing moves,
// and said as a condition while the plan cannot tell yet (a pick it has not planned).
export function branchChangeNote(app, ref, bench, plan) {
  const moves = movesTo(plan, app, ref)
  if (moves === false) return ''
  const at = liveBranchOf(plan, app)
  const sites = sitesHolding(bench, app)
  const move = moves
    ? `Create site first checks out ${ref} in the bench's copy of ${app}${at ? ` (now at ${at})` : ''}`
    : `Unless the bench's copy of ${app} already sits at ${ref}, Create site first checks it out there`
  if (!sites.length) return `${move}; no other site on this bench has it installed.`
  return `${move}, which moves it for every site on this bench: ${sites.join(', ')}.`
}

// The plan endpoint's `ref` list, one per target in target order ('' leaves that
// target's ref to the plan); null when no ref was picked, so the query stays as before.
export function alignedRefs(targets, refByTarget) {
  const refs = targets.map((t) => refByTarget[t] || '')
  return refs.some(Boolean) ? refs : null
}
