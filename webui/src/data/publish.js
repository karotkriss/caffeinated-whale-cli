// Publishing sites at their public name (core/publish.py): the Cloudflare DNS record,
// the Nginx Proxy Manager proxy host and its Let's Encrypt certificate. The store is
// read from /api/v1/publications and refreshed by the `publications` SSE event each
// time a publish job writes a step.
import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { getJson, postJson } from './api'
import { createJob } from './jobs'
import { trackJob } from './job-toasts'

export const publications = reactive({ sites: {}, dns: null, proxy: null, loaded: false })

export function publicationsLanded(data) {
  publications.sites = data?.sites || {}
  publications.dns = data?.dns || null
  publications.proxy = data?.proxy || null
  publications.loaded = true
}

export async function loadPublications() {
  try { publicationsLanded(await getJson('/api/v1/publications')) } catch { /* the Sites list shows no column */ }
}

export const publishOptions = () => getJson('/api/v1/publish/options')
export const preflightPublish = (payload) => postJson('/api/v1/publish/preflight', payload)

// A part's state, as one word and a theme: what the Sites list and New site show.
// Class names are written out whole so Tailwind sees them.
export const PART_STATE = {
  done: { label: 'done', icon: 'lucide-circle-check text-ink-green-5' },
  failed: { label: 'failed', icon: 'lucide-circle-x text-ink-red-5' },
  waiting: { label: 'waiting', icon: 'lucide-circle-dashed text-ink-gray-4' },
  off: { label: 'not used', icon: 'lucide-circle-minus text-ink-gray-4' },
}
export const PART_LABEL = { dns: 'DNS', proxy: 'Proxy', certificate: 'Certificate', verify: 'Answers' }
export const SHOWN_PARTS = ['dns', 'proxy', 'certificate']

// Whether any publishing integration is on: Publish is offered only then.
export const canPublish = () => !!(publications.dns || publications.proxy)
// A real domain (not .localhost, has a dot): the only kind of name that can be published.
export const isPublicName = (name) => /^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]+$/.test(name || '') && !name.endsWith('.localhost')

export function publicationOf(site) {
  return publications.sites[site] || null
}
// Every part done (or not used), and the check passed.
export const isLive = (p) => !!p && Object.values(p.parts || {}).every((part) => ['done', 'off'].includes(part.state))

// Publish (or Retry: the same idempotent job, which repeats only what is missing).
export async function publishSite({ project, bench, site }, args = {}) {
  const record = await createJob({ verb: 'publish-site', target: { project, bench: bench != null ? String(bench) : undefined, site }, args })
  trackJob(record.id)
  return record
}

// Read-only drift check: compares what cwcli made with what is live, repairs nothing.
export async function checkSite(site) {
  try {
    const out = await postJson('/api/v1/publish/check', { site })
    const drift = Object.entries(out.parts).filter(([, p]) => p.state !== 'ok')
    if (out.ok && !drift.length) toast.success(`${site}: no drift`, { description: 'The record, proxy host and certificate match what cwcli made.' })
    else toast.warning(out.ok ? `${site}: some parts could not be read` : `${site} has drifted`, { duration: 15000, description: drift.map(([k, p]) => `${PART_LABEL[k]}: ${p.message}`).join(' ') })
    return out
  } catch (e) {
    toast.error('Check failed', { description: e.hint ? `${e.message} ${e.hint}` : e.message })
    return null
  }
}
