// Publishing sites at their public name (core/publish.py): the Cloudflare DNS record,
// the Nginx Proxy Manager proxy host and its Let's Encrypt certificate. The store is
// read from /api/v1/publications and refreshed by the `publications` SSE event each
// time a publish job writes a step.
//
// Each machine publishes with its own integrations: a remote host's store, options,
// preflight and drift check are read from and run on that host's own daemon through
// the hub's forward (`apiBase(host)`), and its DNS and proxy settings never leave it.
import { reactive } from 'vue'
import { toast } from 'frappe-ui'
import { apiBase, getJson, postJson } from './api'

export const publications = reactive({ sites: {}, dns: null, proxy: null, loaded: false })
// Each remote host's own store, keyed by host id, as last read through the hub.
export const remotePublications = reactive({})
export const publicationsFor = (host = null) => (host ? remotePublications[host] || null : publications)

export function publicationsLanded(data, host = null) {
  const store = host ? (remotePublications[host] ||= { sites: {}, dns: null, proxy: null, loaded: false }) : publications
  store.sites = data?.sites || {}
  store.dns = data?.dns || null
  store.proxy = data?.proxy || null
  store.loaded = true
  store.error = null
}

export async function loadPublications(host = null) {
  try { publicationsLanded(await getJson(`${apiBase(host)}/publications`), host) } catch (e) {
    // This machine's: the Sites list shows no column. A remote's: say the read failed.
    if (host) remotePublications[host] = { ...(remotePublications[host] || { sites: {}, dns: null, proxy: null, loaded: false }), error: e }
  }
}

export const publishOptions = (host = null) => getJson(`${apiBase(host)}/publish/options`)
export const preflightPublish = (payload, host = null) => postJson(`${apiBase(host)}/publish/preflight`, payload)

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

// Whether any publishing integration is on, on that machine: Publish is offered only then.
export const canPublish = (host = null) => { const s = publicationsFor(host); return !!(s?.dns || s?.proxy) }
// Whose integration settings a publish on `host` uses, as a phrase.
export const settingsOwner = (host, hostName) => (host ? `${hostName(host)}'s own` : "this machine's")
// A real domain (not .localhost, has a dot): the only kind of name that can be published.
export const isPublicName = (name) => /^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]+$/.test(name || '') && !name.endsWith('.localhost')

// A public name is published in one place: `site`'s publication only when it is the
// one on this instance's bench, never a same-named copy elsewhere. `store` is this
// machine's publications unless a remote host's own answer is passed.
export function publicationAt(site, project, benchPath, store = publications) {
  const p = store.sites?.[site]
  return p && p.project === project && p.bench_path === benchPath ? p : null
}
// Every part done (or not used), and the check passed.
export const isLive = (p) => !!p && Object.values(p.parts || {}).every((part) => ['done', 'off'].includes(part.state))

// The one sentence New site shows under "Publish it at a public address": what the
// second job does, and whose integration settings it uses when the instance is on
// a remote host (`machine` names that host; null is this machine).
export function publishPlan(options, machine = null) {
  const dns = options?.dns?.name || 'Cloudflare'
  const proxy = options?.proxy?.name || 'Nginx Proxy Manager'
  const where = machine ? ` on ${machine}` : ''
  if (!options) return `After the site is created, a second job${where} publishes it at its own domain${machine ? `, with ${machine}'s own integration settings` : ''}.`
  const both = !!options.dns && !!options.proxy
  const named = both ? `${dns} and ${proxy}` : options.dns ? dns : proxy
  const using = machine ? `, with ${machine}'s own ${named} settings,` : ''
  if (both) return `After the site is created, a second job (Publish site)${where}${using} writes its ${dns} A record, makes its ${proxy} proxy host, requests its Let's Encrypt certificate, then checks it answers over HTTPS. Retry repeats only what is missing.`
  if (options.dns) return `After the site is created, a second job${where}${using} writes its ${dns} A record (DNS only), then checks it answers at its bench port. Without a proxy there is no certificate.`
  return `After the site is created, a second job${where}${using} makes its ${proxy} proxy host and requests its Let's Encrypt certificate. With no DNS integration the name must already point at ${machine || 'this machine'}.`
}

// Read-only drift check on the site's own machine: compares what cwcli made with
// what is live, repairs nothing.
export async function checkSite(site, host = null) {
  try {
    const out = await postJson(`${apiBase(host)}/publish/check`, { site })
    const drift = Object.entries(out.parts).filter(([, p]) => p.state !== 'ok')
    if (out.ok && !drift.length) toast.success(`${site}: no drift`, { description: 'The record, proxy host and certificate match what cwcli made.' })
    else toast.warning(out.ok ? `${site}: some parts could not be read` : `${site} has drifted`, { duration: 15000, description: drift.map(([k, p]) => `${PART_LABEL[k]}: ${p.message}`).join(' ') })
    return out
  } catch (e) {
    toast.error('Check failed', { description: e.hint ? `${e.message} ${e.hint}` : e.message })
    return null
  }
}
