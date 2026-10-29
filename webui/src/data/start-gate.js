import { reactive } from 'vue'
import { instance } from './fleet'
import { ACTIVE_STATES, createJob, loadJob } from './jobs'
import { trackJob } from './job-toasts'

// The CLI's "<project> is not running. Start it?" question, for the page: a verb
// that needs the instance running is never greyed out on a stopped one. It asks
// first, starts the instance through the same `start` job a click on Start runs,
// waits for it, then carries on with the verb (captain note 24). Nothing starts
// behind the user's back: a No, a closed prompt, or a failed start cancels the verb.
// On a remote host the start job runs on that host, through the hub's forward.
export const startGate = reactive({ open: false, project: null, bench: null, benchLabel: null, host: null, verb: null, starting: false, error: null })

// A bench of a running instance that is not serving (fleet rows carry per-bench health).
export function benchRunning(project, bench, host = null) {
  if (bench == null) return true
  const row = (instance(project, host)?.benches || []).find((b) => String(b.index) === String(bench))
  return !row || row.overall === 'running'
}
let settle = null

// `bench` is the bench the page acts on: the start starts that one, so a
// multi-bench instance never comes back asking which bench. `serving` also asks
// when the container is up but that bench is not serving (Install apps).
export function ensureRunning(project, verb = '', { bench = null, benchLabel = null, serving = false, host = null } = {}) {
  if (!project || (instance(project, host)?.container_running && (!serving || benchRunning(project, bench, host)))) return Promise.resolve(true)
  return new Promise((resolve) => {
    settle?.(false)
    settle = resolve
    startGate.project = project
    startGate.bench = bench
    startGate.benchLabel = benchLabel
    startGate.host = host
    startGate.verb = verb
    startGate.error = null
    startGate.starting = false
    startGate.open = true
  })
}

export function answerStart(yes) {
  if (!yes) return finish(false)
  return startThenContinue()
}

function finish(ok) {
  startGate.open = false
  startGate.starting = false
  const done = settle
  settle = null
  done?.(ok)
  return ok
}

async function startThenContinue() {
  startGate.starting = true
  startGate.error = null
  const host = startGate.host
  try {
    // Without a bench, only the containers start: a multi-bench instance would otherwise ask which bench.
    const spec = startGate.bench != null
      ? { verb: 'start', target: { project: startGate.project, bench: String(startGate.bench) }, args: {} }
      : { verb: 'start', target: { project: startGate.project }, args: { containers_only: true } }
    const record = await createJob(spec, host)
    trackJob(record.id, host)
    const final = await waitForJob(record.id, host)
    if (final?.state !== 'succeeded') {
      startGate.error = { message: `${startGate.project} did not start (${final?.state || 'unknown'}).`, hint: 'See the job for the reason.' }
      startGate.starting = false
      return false
    }
    await waitForRunning(startGate.project, host)
    return finish(true)
  } catch (e) {
    startGate.error = e
    startGate.starting = false
    return false
  }
}

async function waitForJob(id, host = null, limitMs = 10 * 60 * 1000) {
  const until = Date.now() + limitMs
  let record = null
  while (Date.now() < until) {
    record = await loadJob(id, 0, null, host)
    if (!ACTIVE_STATES.has(record?.state)) return record
    await new Promise((r) => setTimeout(r, 1000))
  }
  return record
}

// The fleet stream reports the container a probe later; give it a moment so the
// verb that follows sees the instance up.
async function waitForRunning(project, host = null, limitMs = 15000) {
  const until = Date.now() + limitMs
  while (Date.now() < until && !instance(project, host)?.container_running) await new Promise((r) => setTimeout(r, 500))
}
