<script setup>
// New site on an existing bench (captain note 04:51Z): instance and bench
// (preselected from where it was opened), a validated site name with clash and
// .localhost-vs-domain hints, the Administrator password, the MariaDB root
// password reused from the instance's stored one unless overridden, apps to
// install from what the bench already holds (frappe always), default site and
// developer mode. Runs as the `new-site` job with its card; a stopped instance
// gets the Start prompt first.
import { computed, onMounted, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, Select, TextInput } from 'frappe-ui'
import AppIcon from './AppIcon.vue'
import AppPickerDialog from './AppPickerDialog.vue'
import { apps as catalogApps, catalogApp, hostLabel, loadCatalog } from '../data/catalog'
import CommandLine from './CommandLine.vue'
import { newSiteDialog } from '../data/new-site-dialog'
import { instances, details, loadDetail, benchName } from '../data/fleet'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { ensureRunning } from '../data/start-gate'

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

watch(() => newSiteDialog.open, (open) => {
  if (!open) return
  error.value = null
  site.value = ''
  adminPassword.value = ''
  dbRootPassword.value = ''
  apps.value = []
  makeDefault.value = false
  devMode.value = false
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

const NAME_RE = /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/
const nameState = computed(() => {
  const n = site.value.trim()
  if (!n) return { ok: false, text: 'Lowercase letters, digits, dots and dashes; .localhost resolves on this machine without DNS.' }
  if (n !== n.toLowerCase()) return { ok: false, text: 'Site names are lowercase.' }
  if (!NAME_RE.test(n)) return { ok: false, text: 'Not a valid hostname-shaped name.' }
  if (existing.value.includes(n)) return { ok: false, text: `${n} already exists on this bench.` }
  if (allSites.value.includes(n)) return { ok: true, warn: `${n} exists on another bench of ${project.value}; both will answer to the same name on different ports.` }
  if (!n.includes('.')) return { ok: true, warn: 'No dot: the browser will treat it as a search unless you add a hosts entry. Consider name.localhost.' }
  if (!n.endsWith('.localhost')) return { ok: true, warn: `${n} looks like a real domain: it must resolve to this machine (hosts file or DNS) to open.` }
  return { ok: true, text: 'Resolves to this machine automatically.' }
})
const ready = computed(() => !!project.value && bench.value != null && nameState.value.ok && adminPassword.value.length >= 1 && !running.value)
const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const cli = computed(() => {
  let line = `CWCLI_ADMIN_PASSWORD=… cwcli new-site ${q(project.value || '<instance>')} ${q(site.value.trim() || '<site>')} --bench ${q(bench.value ?? '<bench>')}`
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
      target: { project: project.value, bench: String(bench.value), site: site.value.trim() },
      args: { apps: appNames.value, fetch: fetchUrls.value, default: makeDefault.value, developer_mode: devMode.value },
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
        <div class="sm:col-span-2">
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
      </div>
      <Alert v-if="project && !instances.find((i) => i.project === project)?.container_running" theme="gray" :title="`${project} is stopped`" :description="`cwcli will ask to start ${project} first, then create the site, the same as in a terminal.`" />
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running</div>
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
