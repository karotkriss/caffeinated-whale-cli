<script setup>
// New site on an existing bench (captain note 04:51Z): instance and bench
// (preselected from where it was opened), a validated site name with clash and
// .localhost-vs-domain hints, the Administrator password, the MariaDB root
// password reused from the instance's stored one unless overridden, apps to
// install from what the bench already holds (frappe always), default site and
// developer mode. Runs as the `new-site` job with its card; a stopped instance
// gets the Start prompt first.
// With Cloudflare DNS or Nginx Proxy Manager on, "Publish it at a public address"
// starts a `publish-site` job as soon as the site exists (core/publish.py): the name
// becomes a label plus a zone the DNS token can see, and each part has a live,
// read-only preflight; a failing row keeps Create disabled.
import { computed, onMounted, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, FormLabel, Select, TextInput } from 'frappe-ui'
import AppIcon from './AppIcon.vue'
import AppPickerDialog from './AppPickerDialog.vue'
import { apps as catalogApps, catalogApp, hostLabel, loadCatalog } from '../data/catalog'
import CommandLine from './CommandLine.vue'
import { newSiteDialog } from '../data/new-site-dialog'
import { instances, details, loadDetail, benchName } from '../data/fleet'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { ensureRunning } from '../data/start-gate'
import { canPublish, loadPublications, preflightPublish, publishOptions } from '../data/publish'

const project = ref(null)
const bench = ref(null)
const site = ref('')
const adminPassword = ref('')
const dbRootPassword = ref('')
const apps = ref([])
const pickerOpen = ref(false)
onMounted(() => loadCatalog())
const makeDefault = ref(false)
const devMode = ref(false)
const running = ref(false)
const error = ref(null)
// Publishing (all off until the box is ticked).
const publishing = ref(false)
const label = ref('')
const zone = ref(null)
const options = ref(null)
const optionsError = ref(null)
const rows = ref(null) // the preflight's rows for the current name, null while reading
const changeAddress = ref(false)
const address = ref('')
const proxied = ref(false)
const accessList = ref('0')

watch(() => newSiteDialog.open, (open) => {
  if (!open) return
  error.value = null
  site.value = ''
  adminPassword.value = ''
  dbRootPassword.value = ''
  apps.value = []
  makeDefault.value = false
  devMode.value = false
  publishing.value = false
  label.value = ''
  rows.value = null
  options.value = null
  optionsError.value = null
  changeAddress.value = false
  address.value = ''
  proxied.value = false
  accessList.value = '0'
  loadPublications()
  project.value = newSiteDialog.project || instances.value.find((i) => i.container_running)?.project || instances.value[0]?.project || null
  bench.value = newSiteDialog.bench
})
watch(project, (p) => { if (p) loadDetail(p) })
const projectOptions = computed(() => instances.value.map((i) => ({ label: i.container_running ? i.project : `${i.project} (stopped, asks to start)`, value: i.project })))
const benches = computed(() => details[project.value]?.data?.benches || [])
const benchOptions = computed(() => benches.value.map((b) => ({ label: `${benchName(b)} · ${b.sites.length} ${b.sites.length === 1 ? 'site' : 'sites'}`, value: String(b.index) })))
watch(benchOptions, (opts) => { if (opts.length && (bench.value == null || !opts.some((o) => o.value === bench.value))) bench.value = opts[0].value }, { immediate: true })
const theBench = computed(() => benches.value.find((b) => String(b.index) === String(bench.value)))
const benchApps = computed(() => (theBench.value?.available_apps || []).filter((a) => a !== 'frappe'))
// Captain note 49: the picker lists the bench's apps AND the catalog's remote apps; a
// remote pick is fetched onto the bench first (`cwcli apps install --fetch-only`), then
// installed on the new site like any other. Remote picks are keyed `remote:<catalog key>`.
const remoteApps = computed(() => catalogApps.value.filter((a) => a.source?.url && a.name !== 'frappe' && !benchApps.value.includes(a.name)))
const remoteApp = (v) => (String(v).startsWith('remote:') ? remoteApps.value.find((a) => a.key === v.slice(7)) : null)
const pickerOptions = computed(() => [
  ...benchApps.value.map((a) => ({ value: a, label: catalogApp(a).title, app: catalogApp(a), hint: catalogApp(a).description || '', section: 'On this bench' })),
  ...remoteApps.value.map((a) => ({ value: `remote:${a.key}`, name: a.name, label: a.title, app: a, hint: `fetched from ${hostLabel(a.source.host)} first · ${a.source.url}`, section: 'From app sources' })),
])
const chipApp = (v) => remoteApp(v) || catalogApp(v)
const appNames = computed(() => apps.value.map((v) => remoteApp(v)?.name || v))
const fetchUrls = computed(() => apps.value.map((v) => remoteApp(v)?.source?.url).filter(Boolean))
const existing = computed(() => (theBench.value?.sites || []).map((s) => s.name))
const allSites = computed(() => benches.value.flatMap((b) => b.sites.map((s) => s.name)))

// The zone picker applies only while publishing with a DNS integration on.
const zones = computed(() => options.value?.dns?.zones || [])
const zoned = computed(() => publishing.value && zones.value.length > 0)
const fullName = computed(() => (zoned.value ? (label.value.trim() ? `${label.value.trim()}.${zone.value}` : '') : site.value.trim()))
watch(publishing, async (on) => {
  if (!on) {
    if (zoned.value || label.value) site.value = fullName.value || site.value
    return
  }
  if (!options.value) {
    try { options.value = await publishOptions() } catch (e) { optionsError.value = e.hint ? `${e.message} ${e.hint}` : e.message; return }
  }
  proxied.value = !!(options.value.proxied_allowed && options.value.dns?.proxied_default)
  const typed = site.value.trim()
  const z = [...zones.value].sort((a, b) => b.length - a.length).find((n) => typed === n || typed.endsWith(`.${n}`))
  zone.value = z || zones.value[0] || null
  label.value = z ? typed.slice(0, Math.max(0, typed.length - z.length - 1)) : typed.split('.')[0] || ''
})
const recordAddress = computed(() => (changeAddress.value && address.value.trim()) || options.value?.dns?.ipv4 || '')
const accessOptions = computed(() => [{ label: 'Anyone (no access list)', value: '0' }, ...(options.value?.proxy?.access_lists || []).map((a) => ({ label: a.name, value: String(a.id) }))])
const portRow = computed(() => (rows.value || []).find((r) => r.part === 'port' && r.port))
const publicUrl = computed(() => (options.value?.proxy ? `https://${fullName.value}` : `http://${fullName.value}:${portRow.value?.port ?? '<bench port>'}`))
const deep = computed(() => proxied.value && zoned.value && label.value.includes('.'))
const publishSays = computed(() => {
  const dns = options.value?.dns?.name || 'Cloudflare'
  const proxy = options.value?.proxy?.name || 'Nginx Proxy Manager'
  const both = !!options.value?.dns && !!options.value?.proxy
  if (!options.value) return 'After the site is created, a second job publishes it at its own domain.'
  if (both) return `After the site is created, a second job (Publish site) writes its ${dns} A record, makes its ${proxy} proxy host, requests its Let's Encrypt certificate, then checks it answers over HTTPS. Retry repeats only what is missing.`
  if (options.value.dns) return `After the site is created, a second job writes its ${dns} A record (DNS only), then checks it answers at its bench port. Without a proxy there is no certificate.`
  return `After the site is created, a second job makes its ${proxy} proxy host and requests its Let's Encrypt certificate. With no DNS integration the name must already point at this machine.`
})
let preflightTimer = null
let preflightSeq = 0
watch([publishing, fullName, project, bench, recordAddress], () => {
  clearTimeout(preflightTimer)
  rows.value = null
  if (!publishing.value || !fullName.value || !project.value || bench.value == null) return
  const seq = ++preflightSeq
  preflightTimer = setTimeout(async () => {
    try {
      const out = await preflightPublish({ project: project.value, bench: bench.value, site: fullName.value, address: changeAddress.value ? address.value.trim() : '' })
      if (seq === preflightSeq) rows.value = out.rows
    } catch (e) {
      if (seq === preflightSeq) rows.value = [{ part: 'name', state: 'fail', text: e.hint ? `${e.message} ${e.hint}` : e.message }]
    }
  }, 450)
})
const ROW_LABEL = { name: 'Name', port: 'Bench port', dns: 'DNS record', proxy: 'Proxy host', resolve: 'Name lookup' }
const ROW_ICON = { ok: 'lucide-circle-check text-ink-green-5', warn: 'lucide-triangle-alert text-ink-amber-5', fail: 'lucide-circle-x text-ink-red-5' }
const publishReady = computed(() => !publishing.value || (!!options.value && !!rows.value && rows.value.every((r) => r.state !== 'fail')))

const NAME_RE = /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/
const nameState = computed(() => {
  const n = fullName.value
  if (!n) return { ok: false, text: 'Lowercase letters, digits, dots and dashes; .localhost resolves on this machine without DNS.' }
  if (n !== n.toLowerCase()) return { ok: false, text: 'Site names are lowercase.' }
  if (!NAME_RE.test(n)) return { ok: false, text: 'Not a valid hostname-shaped name.' }
  if (existing.value.includes(n)) return { ok: false, text: `${n} already exists on this bench.` }
  if (allSites.value.includes(n)) return { ok: true, warn: `${n} exists on another bench of ${project.value}; both will answer to the same name on different ports.` }
  if (!n.includes('.')) return { ok: true, warn: 'No dot: the browser will treat it as a search unless you add a hosts entry. Consider name.localhost.' }
  if (publishing.value && n.endsWith('.localhost')) return { ok: false, text: 'A .localhost site cannot be published; give it its own domain.' }
  if (publishing.value) return { ok: true, text: `Frappe routes by name, so the site's name is its public address: ${n}.` }
  if (!n.endsWith('.localhost')) return { ok: true, warn: `${n} looks like a real domain: it must resolve to this machine (hosts file or DNS) to open.${canPublish() ? ' Tick Publish below to have cwcli point it here.' : ''}` }
  return { ok: true, text: 'Resolves to this machine automatically.' }
})
const ready = computed(() => !!project.value && bench.value != null && nameState.value.ok && adminPassword.value.length >= 1 && publishReady.value && !running.value)
const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const cli = computed(() => {
  let line = `CWCLI_ADMIN_PASSWORD=… cwcli new-site ${q(project.value || '<instance>')} ${q(fullName.value || '<site>')} --bench ${q(bench.value ?? '<bench>')}`
  for (const a of appNames.value) line += ` --app ${q(a)}`
  for (const u of fetchUrls.value) line = `cwcli apps install ${q(project.value || '<instance>')} ${q(u)} --bench ${q(bench.value ?? '<bench>')} --fetch-only && ` + line
  if (makeDefault.value) line += ' --default'
  if (devMode.value) line += ' --dev-mode'
  return line
})
function toggleApp(name, on) {
  apps.value = on ? [...new Set([...apps.value, name])] : apps.value.filter((a) => a !== name)
}
async function start() {
  if (!(await ensureRunning(project.value, 'New site', { bench: bench.value, benchLabel: theBench.value ? benchName(theBench.value) : null }))) return
  running.value = true
  error.value = null
  try {
    const record = await createJob({
      verb: 'new-site',
      target: { project: project.value, bench: String(bench.value), site: fullName.value },
      args: {
        apps: appNames.value,
        fetch: fetchUrls.value,
        default: makeDefault.value,
        developer_mode: devMode.value,
        ...(publishing.value ? { publish: { address: changeAddress.value ? address.value.trim() : '', proxied: proxied.value, access_list_id: Number(accessList.value) || 0 } } : {}),
      },
      secrets: { admin_password: adminPassword.value, ...(dbRootPassword.value ? { db_root_password: dbRootPassword.value } : {}) },
    })
    newSiteDialog.open = false
    trackJob(record.id)
  } catch (e) {
    error.value = e.hint ? `${e.message} ${e.hint}` : e.message
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="newSiteDialog.open" title="New site" size="xl">
    <div class="space-y-4">
      <p class="text-p-base text-ink-gray-6">Creates the site on the bench (bench new-site), installs the chosen apps on it, makes it the default if asked, and refreshes the cache. The job continues if you close this tab.</p>
      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <div class="mb-1 text-xs text-ink-gray-5">Instance</div>
          <Select v-model="project" :options="projectOptions" class="w-full" placeholder="Choose an instance" />
        </div>
        <div>
          <div class="mb-1 text-xs text-ink-gray-5">Bench</div>
          <Select v-model="bench" :options="benchOptions" class="w-full" :placeholder="project ? (details[project]?.loading ? 'Reading benches...' : 'Choose a bench') : 'Pick an instance first'" />
        </div>
        <div v-if="zoned" class="sm:col-span-2">
          <FormLabel label="Site name" />
          <div class="mt-1.5 flex items-center gap-1.5">
            <TextInput v-model="label" class="min-w-0 flex-1" placeholder="erp" aria-label="Name in the zone" autocomplete="off" spellcheck="false" />
            <span class="text-base text-ink-gray-5" aria-hidden="true">.</span>
            <Select v-model="zone" class="w-48 max-w-[45%]" :options="zones.map((z) => ({ label: z, value: z }))" aria-label="Zone" />
          </div>
          <p class="mt-1 text-xs" :class="nameState.ok ? 'text-ink-gray-5' : label.trim() ? 'text-ink-red-5' : 'text-ink-gray-5'">{{ fullName ? nameState.text : 'A name in one of the zones the Cloudflare token can see, so a typo cannot land in another zone.' }}</p>
        </div>
        <div v-else class="sm:col-span-2">
          <FormControl v-model="site" label="Site name" placeholder="shop.localhost" autocomplete="off" spellcheck="false" />
          <p class="mt-1 text-xs" :class="nameState.ok ? (nameState.warn ? 'text-ink-amber-6' : 'text-ink-gray-5') : site.trim() ? 'text-ink-red-5' : 'text-ink-gray-5'">{{ nameState.warn || nameState.text }}</p>
        </div>
        <FormControl v-model="adminPassword" label="Administrator password" type="password" autocomplete="new-password" />
        <div>
          <FormControl v-model="dbRootPassword" label="MariaDB root password (optional)" type="password" autocomplete="off" placeholder="blank reuses the instance's saved password" />
          <p class="mt-1 text-xs text-ink-gray-5">The password cwcli saved at init is reused and never shown here; set one only to override it.</p>
        </div>
        <div class="sm:col-span-2">
          <div class="mb-1 text-xs text-ink-gray-5">Apps to install</div>
          <div v-if="!theBench" class="text-sm text-ink-gray-5">Pick a bench first.</div>
          <!-- Captain note 40: a multiselect - picked apps as chips, the choosing in a picker modal. -->
          <div v-else class="flex flex-wrap items-center gap-1.5">
            <span class="inline-flex h-7 items-center gap-1.5 rounded-4 border border-outline-gray-1 bg-surface-gray-1 px-2 text-sm text-ink-gray-6"><AppIcon :app="catalogApp('frappe')" size="xs" />Frappe<span class="text-xs text-ink-gray-4">always</span></span>
            <span v-for="a in apps" :key="a" class="inline-flex h-7 items-center gap-1.5 rounded-4 border border-outline-gray-2 bg-surface-elevation-1 pl-2 pr-1 text-sm text-ink-gray-8">
              <AppIcon :app="chipApp(a)" size="xs" />{{ chipApp(a).title }}<span v-if="remoteApp(a)" class="text-xs text-ink-gray-4">fetch</span>
              <button type="button" class="rounded p-0.5 text-ink-gray-4 hover:bg-surface-gray-2 hover:text-ink-gray-7" :aria-label="`Remove ${a}`" @click="toggleApp(a, false)"><span class="lucide-x size-3.5" aria-hidden="true" /></button>
            </span>
            <Button size="sm" variant="subtle" icon-left="lucide-plus" :label="apps.length ? 'Change' : 'Choose apps'" :disabled="!pickerOptions.length" @click="pickerOpen = true" />
            <span v-if="!benchApps.length" class="text-xs text-ink-gray-5">only frappe is on this bench; a remote pick is fetched first</span>
          </div>
          <AppPickerDialog :open="pickerOpen" title="Apps to install on the new site" :options="pickerOptions" :locked="[{ value: 'frappe', label: 'Frappe Framework', app: catalogApp('frappe') }]" :model-value="apps" @update:model-value="(v) => (apps = v)" @update:open="pickerOpen = $event" />
        </div>
        <Checkbox v-model="makeDefault" label="Make it the bench's default site" />
        <Checkbox v-model="devMode" label="Turn developer mode on for it" />
        <div v-if="canPublish()" class="space-y-3 rounded-6 border border-outline-gray-1 p-3 sm:col-span-2">
          <div>
            <Checkbox v-model="publishing" label="Publish it at a public address" />
            <p class="mt-1 pl-6 text-p-sm text-ink-gray-5">{{ publishSays }}</p>
          </div>
          <template v-if="publishing">
            <ErrorMessage v-if="optionsError" :message="optionsError" />
            <div v-else-if="!options" class="h-16 animate-pulse rounded-6 bg-surface-gray-1" />
            <template v-else>
              <Alert v-if="options.dns?.error" theme="red" :title="`${options.dns.name} did not answer`" :description="options.dns.error" />
              <Alert v-if="options.proxy?.error" theme="amber" :title="`${options.proxy.name}: the access lists could not be read`" :description="options.proxy.error" />
              <dl class="grid gap-x-3 gap-y-0.5 text-p-sm sm:grid-cols-[7rem_minmax(0,1fr)] sm:gap-y-1.5">
                <dt class="text-ink-gray-5">Public address</dt>
                <dd class="break-all font-mono text-ink-gray-8 max-sm:mb-1.5 sm:truncate">{{ fullName ? publicUrl : '-' }}</dd>
                <template v-if="options.dns">
                  <dt class="text-ink-gray-5">A record</dt>
                  <dd class="flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-gray-8 max-sm:mb-1.5">
                    <span v-if="!changeAddress">points at <span class="font-mono">{{ recordAddress || 'no address set' }}</span> · {{ proxied ? 'proxied' : 'DNS only' }}</span>
                    <TextInput v-else v-model="address" class="w-44" :placeholder="options.dns.ipv4" aria-label="Address for this site's A record" spellcheck="false" />
                    <button type="button" class="text-left text-xs text-ink-gray-6 underline-offset-2 hover:underline" @click="changeAddress = !changeAddress; address = ''">{{ changeAddress ? 'Use the machine\'s address' : 'Use another address for this site' }}</button>
                  </dd>
                </template>
                <template v-if="options.proxy">
                  <dt class="text-ink-gray-5 sm:pt-1.5">Access</dt>
                  <dd>
                    <Select v-model="accessList" class="w-64 max-w-full" :options="accessOptions" aria-label="Nginx Proxy Manager access list" />
                    <p class="mt-1 text-ink-gray-5">The bench runs Frappe's development server, so restrict a site not meant for everyone.</p>
                  </dd>
                </template>
              </dl>
              <div v-if="fullName" class="space-y-1">
                <div class="text-xs text-ink-gray-5">Preflight · reads only, changes nothing</div>
                <div v-if="!rows" class="space-y-1"><div v-for="i in 3" :key="i" class="h-5 animate-pulse rounded bg-surface-gray-1" /></div>
                <ul v-else class="space-y-1 text-p-sm">
                  <li v-for="r in rows" :key="r.part" class="flex items-start gap-1.5">
                    <span class="mt-0.5 size-4 shrink-0" :class="ROW_ICON[r.state]" aria-hidden="true" />
                    <span class="min-w-0"><span class="font-medium text-ink-gray-8">{{ ROW_LABEL[r.part] || r.part }}</span> <span :class="r.state === 'fail' ? 'text-ink-red-6' : r.state === 'warn' ? 'text-ink-amber-6' : 'text-ink-gray-6'">{{ r.text }}</span></span>
                  </li>
                </ul>
              </div>
              <div v-if="options.proxied_allowed">
                <Checkbox v-model="proxied" label="Proxy it through Cloudflare (orange cloud)" />
                <p class="mt-1 pl-6 text-p-sm" :class="deep ? 'text-ink-amber-6' : 'text-ink-gray-5'">{{ deep ? `Cloudflare's Universal SSL covers only first-level names like ${label.split('.').pop()}.${zone}; this name needs Advanced Certificate Manager.` : 'Needs the zone\'s SSL mode at Full (strict); Flexible loops with Force SSL.' }}</p>
              </div>
            </template>
          </template>
        </div>
      </div>
      <Alert v-if="project && !instances.find((i) => i.project === project)?.container_running" theme="gray" :title="`${project} is stopped`" :description="`cwcli will ask to start ${project} first, then create the site, the same as in a terminal.`" />
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running{{ publishing ? ', then Publish site from the web UI (no terminal command)' : '' }}</div>
        <CommandLine :command="cli" />
      </div>
      <ErrorMessage :message="error" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" data-primary label="Create site" :loading="running" :disabled="!ready" @click="start" />
      </div>
    </template>
  </Dialog>
</template>
