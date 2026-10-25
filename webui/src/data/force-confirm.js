// A forced frappe update discards uncommitted and unpushed app work, so it joins
// the typed-name confirms. Dependency-free: tests run it under plain node.

const updatesFrappe = (apps) =>
  String(apps || '').split(',').some((app) => app.trim().toLowerCase() === 'frappe')

export function forceConfirmTarget(values, context) {
  if (!values?.force || !updatesFrappe(values.apps)) return ''
  return String(context?.benchName || context?.bench || context?.project || '')
}

export function forceConfirmed(values, context, typed) {
  const target = forceConfirmTarget(values, context)
  return !target || String(typed || '').trim() === target
}
