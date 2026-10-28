import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'
import { ensureRunning } from './start-gate'
import { JOB_LABEL } from './jobs'

export const jobDialog = reactive({
  open: false,
  verb: null,
  context: {},
  values: {},
  step: 0,
  running: false,
  error: null,
})

const defaults = {
  init: {
    project: '',
    bench_name: 'frappe-bench',
    site_name: '',
    port: 8000,
    version: '16',
    bench_parent: '/workspace',
    frappe_url: '',
    install_erpnext: true,
    start: true,
    reuse_bench: '',
    admin_password: '',
    db_root_password: '',
  },
  'apps-install': { apps: '', sites: '', branch: '', fetch_only: false , backup_first: false },
  'apps-update': {
    apps: '',
    sites: '',
    build: true,
    clear_cache: false,
    clear_website_cache: false,
    skip_maintenance: false,
    force: false,
    fix_branches: false,
  },
  backup: { with_files: true, store: true, reason: 'manual', note: '' },
  migrate: { backup_first: false },
  'run-tests': { app: '', module: '' },
  build: { app: '' },
  'setup-wizard': { country: 'United States', currency: 'USD', timezone: 'UTC', language: 'English' },
  'self-update': { no_cache: false },
}

export function openJob(verb, context = {}) {
  jobDialog.verb = verb
  jobDialog.context = { ...context }
  jobDialog.values = { ...(defaults[verb] || {}), ...(context.args || {}) }
  if (verb === 'init') {
    const reuse = context.args?.reuse_bench
    jobDialog.values.reuse_bench = reuse === true ? 'reuse' : reuse === false ? 'refuse' : ''
    jobDialog.values.frappe_url = jobDialog.values.frappe_url || ''
    jobDialog.values.project = context.project || jobDialog.values.project
    if (!jobDialog.values.site_name && context.project) jobDialog.values.site_name = `${context.project}.localhost`
  }
  jobDialog.step = 0
  jobDialog.error = null
  jobDialog.open = true
}

const split = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean)

function spec() {
  const verb = jobDialog.verb
  const value = jobDialog.values
  const context = jobDialog.context
  if (verb === 'init') {
    return {
      verb,
      target: { project: value.project.trim() },
      args: {
        bench_name: value.bench_name.trim(),
        site_name: value.site_name.trim(),
        port: Number(value.port),
        version: value.version.trim(),
        bench_parent: value.bench_parent.trim(),
        frappe_url: value.frappe_url.trim() || null,
        install_erpnext: value.install_erpnext,
        start: value.start,
        reuse_bench: value.reuse_bench === '' ? null : value.reuse_bench === 'reuse',
        auto_start: value.auto_start === true && value.project.trim() === context.project,
      },
      secrets: { admin_password: value.admin_password, db_root_password: value.db_root_password },
    }
  }
  const target = {
    project: context.project,
    ...(context.bench != null ? { bench: String(context.bench) } : {}),
    ...(context.site ? { site: context.site } : {}),
  }
  if (verb === 'run-tests') target.app = value.app.trim()
  if (verb === 'apps-install') return { verb, target, args: { apps: split(value.apps), sites: split(value.sites), branch: value.branch.trim() || null, fetch_only: value.fetch_only } }
  if (verb === 'apps-update') return { verb, target, args: { apps: split(value.apps), sites: split(value.sites), build: value.build, clear_cache: value.clear_cache, clear_website_cache: value.clear_website_cache, skip_maintenance: value.skip_maintenance, force: value.force } }
  if (verb === 'run-tests') return { verb, target, args: { module: value.module.trim() || null } }
  if (verb === 'build') return { verb, target: { ...target, ...(value.app.trim() ? { app: value.app.trim() } : {}) }, args: {} }
  return { verb, target, args: { ...value } }
}

export async function submitJob() {
  const verb = jobDialog.verb
  if (!['init', 'self-update'].includes(verb) && !(await ensureRunning(jobDialog.context.project, JOB_LABEL[verb]))) return null
  jobDialog.running = true
  jobDialog.error = null
  try {
    const record = await createJob(spec())
    jobDialog.open = false
    trackJob(record.id)
    return record
  } catch (error) {
    jobDialog.error = error
    return null
  } finally {
    jobDialog.running = false
  }
}
