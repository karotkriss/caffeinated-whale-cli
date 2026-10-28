// The one schedule dialog: opened empty, from a template, from a row (edit) or from a
// job's page ("Schedule this…", prefilled with that job's verb, target and args).
import { reactive } from 'vue'

export const scheduleDialog = reactive({ open: false, schedule: null })

export function openSchedule(schedule = null) {
  scheduleDialog.schedule = schedule ? JSON.parse(JSON.stringify(schedule)) : null
  scheduleDialog.open = true
}

export function scheduleFromJob(job) {
  const a = { ...(job.args || {}) }
  delete a.reason
  return { name: '', verb: job.verb, target: { ...(job.target || {}) }, args: a, cron: '0 2 * * *' }
}

export function scheduleFromTemplate(t, project) {
  const target = { project }
  if (t.process) target.process = t.process
  return { name: t.name, verb: t.verb, target, args: t.args || {}, cron: t.cron, unattended: !!t.unattended, respect_window: !!t.respect_window }
}
