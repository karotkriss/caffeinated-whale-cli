<script setup>
// New bench inside an existing instance (captain note 04:55Z): folder name,
// Frappe version or a fork (private forks through the credential bridge, as the
// creation wizard), ERPNext, extra apps from the catalog, the first site and
// its Administrator password. Python and Node are not a choice: cwcli picks them
// from the Frappe major, exactly as init does for bench 0. Runs as the
// `new-bench` job with its card; a stopped instance gets the Start prompt first.
import { computed, onMounted, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, Select, TextInput } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { newBenchDialog } from '../data/new-bench-dialog'
import { fleet, instances, detailOf, loadDetail, benchName, instance as findInstance, ikey, needsDbRootPassword, DB_ROOT_PASSWORD_HELP } from '../data/fleet'
import { onHost } from '../data/hosts'
import { apps as catalogApps, loadCatalog } from '../data/catalog'
import AppIcon from './AppIcon.vue'
import AppPickerDialog from './AppPickerDialog.vue'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { ensureRunning } from '../data/start-gate'

const project = ref(null)
const host = ref(null) // the remote host the picked instance lives on (null: this machine)
const name = ref('')
const version = ref('16')
const frappeUrl = ref('')
const erpnext = ref(false)
const extra = ref([]) // catalog app keys
const pickerOpen = ref(false)
const extraApp = (key) => catalogApps.value.find((a) => a.key === key)
const siteName = ref('')
const adminPassword = ref('')
const dbRootPassword = ref('')
// The new bench shares the instance's MariaDB: no field when cwcli holds its root password,
// unless MariaDB rejected the one a job used on this same instance.
const rejected = ref(null) // { project, host, message }
const rejection = computed(() => (rejected.value && rejected.value.project === project.value && rejected.value.host === host.value ? rejected.value.message : null))
const askDbRootPassword = computed(() => !!project.value && (!!rejection.value || needsDbRootPassword(project.value, host.value)))
const running = ref(false)
const error = ref(null)
onMounted(() => loadCatalog())

watch(() => newBenchDialog.open, (open) => {
  if (!open) return
  error.value = null
  name.value = ''
  version.value = '16'
  frappeUrl.value = ''
  erpnext.value = false
  extra.value = []
  siteName.value = ''
  adminPassword.value = ''
  dbRootPassword.value = ''
  const first = newBenchDialog.project ? findInstance(newBenchDialog.project, newBenchDialog.host) : instances.value.find((i) => i.container_running) || instances.value[0]
  project.value = first?.project || newBenchDialog.project || null
  host.value = first?.host || newBenchDialog.host || null
  const retry = newBenchDialog.retry
  rejected.value = retry ? { project: project.value, host: host.value, message: retry.rejected } : null
  if (!retry) return
  const a = retry.args || {}
  name.value = a.bench_name || ''
  prevName = name.value
  siteName.value = a.site_name || ''
  version.value = a.version || '16'
  frappeUrl.value = a.frappe_url || ''
  erpnext.value = !!a.install_erpnext
  extra.value = (a.apps || []).map((u) => extraOptions.value.find((o) => o.url === u)?.value).filter(Boolean)
})
watch([project, host], ([p, h]) => { if (p) loadDetail(p, { host: h }) })
// The picker's value is the instance key, so two machines' same-named instances stay apart.
const pick = computed({
  get: () => (project.value ? ikey(host.value, project.value) : null),
  set: (k) => { const i = fleet.instances[k]; project.value = i?.project || null; host.value = i?.host || null },
})
watch(name, (n) => { if (!siteName.value || siteName.value === `${prevName}.localhost`) siteName.value = n ? `${n}.localhost` : ''; prevName = n })
let prevName = ''
const projectOptions = computed(() => instances.value.map((i) => ({ label: `${i.project}${onHost(i.host)}${i.container_running ? '' : ' (stopped, asks to start)'}`, value: i.key })))
const benches = computed(() => detailOf(project.value, host.value)?.data?.benches || [])
const SLUG_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/
const nameState = computed(() => {
  const n = name.value.trim()
  if (!n) return { ok: false, text: 'A folder name under /workspace: lowercase letters, digits and dashes.' }
  if (!SLUG_RE.test(n)) return { ok: false, text: 'Lowercase letters, digits and dashes only, no leading or trailing dash.' }
  if (benches.value.some((b) => benchName(b) === n || (b.path || '').split('/').pop() === n)) return { ok: false, text: `${project.value} already has a bench named ${n}.` }
  return { ok: true, text: `Will be created at /workspace/${n}.` }
})
const versionState = computed(() => (/^\d+(\.\d+\.\d+)?$/.test(version.value.trim()) ? null : 'A major like 16, or a tag like 16.3.0.'))
// Extra apps: anything in the catalog with a repository URL (private ones come through the credential bridge).
const extraOptions = computed(() => catalogApps.value.filter((a) => a.source?.url && !['frappe', 'erpnext'].includes(a.name)).map((a) => ({ label: `${a.title} (${a.name})`, value: a.key, url: a.source.url })))
const extraUrls = computed(() => extra.value.map((k) => extraOptions.value.find((o) => o.value === k)?.url).filter(Boolean))
const ready = computed(() => !!project.value && nameState.value.ok && !versionState.value && !!siteName.value.trim() && adminPassword.value.length >= 1 && !running.value)
const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const cli = computed(() => {
  let line = `CWCLI_ADMIN_PASSWORD=… cwcli new-bench ${q(project.value || '<instance>')} ${q(name.value.trim() || '<name>')} --version ${q(version.value.trim() || '16')} --site ${q(siteName.value.trim() || '<site>')}`
  if (frappeUrl.value.trim()) line += ` --frappe-url ${q(frappeUrl.value.trim())}`
  if (erpnext.value) line += ' --erpnext'
  for (const u of extraUrls.value) line += ` --app ${q(u)}`
  return line
})
function toggleExtra(key, on) {
  extra.value = on ? [...new Set([...extra.value, key])] : extra.value.filter((k) => k !== key)
}
async function start() {
  if (!(await ensureRunning(project.value, 'New bench', { host: host.value }))) return
  running.value = true
  error.value = null
  try {
    const record = await createJob({
      verb: 'new-bench',
      target: { project: project.value },
      args: { bench_name: name.value.trim(), site_name: siteName.value.trim(), version: version.value.trim(), frappe_url: frappeUrl.value.trim() || null, install_erpnext: erpnext.value, apps: extraUrls.value },
      secrets: { admin_password: adminPassword.value, ...(askDbRootPassword.value && dbRootPassword.value ? { db_root_password: dbRootPassword.value } : {}) },
    }, host.value)
    newBenchDialog.open = false
    trackJob(record.id, host.value)
  } catch (e) {
    error.value = e.hint ? `${e.message} ${e.hint}` : e.message
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="newBenchDialog.open" title="New bench" size="xl">
    <div class="space-y-4">
      <p class="text-p-base text-ink-gray-6">Adds a bench to the instance's container the way bench 0 was made: bench init for the chosen Frappe version or fork, the bench configs, a first site, ERPNext if asked, then its processes start under cwcli's supervisor like bench 0's, and any extra apps are fetched and installed on the first site. Takes a few minutes; the job continues if you close this tab.</p>
      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <div class="mb-1 text-xs text-ink-gray-5">Instance</div>
          <Select v-model="pick" :options="projectOptions" class="w-full" placeholder="Choose an instance" />
        </div>
        <div>
          <FormControl v-model="name" label="Bench folder name" placeholder="bench-v15" autocomplete="off" spellcheck="false" />
          <p class="mt-1 text-xs" :class="nameState.ok ? 'text-ink-gray-5' : name.trim() ? 'text-ink-red-5' : 'text-ink-gray-5'">{{ nameState.text }}</p>
        </div>
        <div>
          <FormControl v-model="version" label="Frappe version" placeholder="16 or 16.3.0" autocomplete="off" />
          <p class="mt-1 text-xs" :class="versionState ? 'text-ink-red-5' : 'text-ink-gray-5'">{{ versionState || 'Python and Node are chosen by cwcli from this version (pyenv / nvm), as for bench 0.' }}</p>
        </div>
        <FormControl v-model="frappeUrl" label="Frappe repository (optional)" placeholder="https://github.com/frappe/frappe" autocomplete="off" description="A fork to build from; a private GitHub or GitLab fork uses this machine's gh/glab sign-in through the credential bridge." />
        <FormControl v-model="siteName" label="First site" placeholder="bench-v15.localhost" autocomplete="off" description="bench init always creates one site; more can be added later with New site." />
        <FormControl v-model="adminPassword" label="Administrator password" type="password" autocomplete="new-password" />
        <div v-if="askDbRootPassword" class="sm:col-span-2">
          <FormControl v-model="dbRootPassword" label="The instance's existing MariaDB root password" type="password" autocomplete="off" :description="DB_ROOT_PASSWORD_HELP" />
          <ErrorMessage class="mt-1" :message="rejection" />
        </div>
        <Checkbox v-model="erpnext" label="Install ERPNext on the first site" />
        <div class="sm:col-span-2">
          <div class="mb-1 text-xs text-ink-gray-5">Extra apps to get and install on the first site</div>
          <div class="flex flex-wrap items-center gap-1.5">
            <span v-for="k in extra" :key="k" class="inline-flex h-7 items-center gap-1.5 rounded-4 border border-outline-gray-2 bg-surface-elevation-1 pl-2 pr-1 text-sm text-ink-gray-8">
              <AppIcon v-if="extraApp(k)" :app="extraApp(k)" size="xs" />{{ extraApp(k)?.title || k }}
              <button type="button" class="rounded p-0.5 text-ink-gray-4 hover:bg-surface-gray-2 hover:text-ink-gray-7" :aria-label="`Remove ${k}`" @click="toggleExtra(k, false)"><span class="lucide-x size-3.5" aria-hidden="true" /></button>
            </span>
            <Button size="sm" variant="subtle" icon-left="lucide-plus" :label="extra.length ? 'Change' : 'Choose apps'" :disabled="!extraOptions.length" @click="pickerOpen = true" />
            <span v-if="!extraOptions.length" class="text-xs text-ink-gray-5">the catalog holds no app with a repository URL yet</span>
          </div>
          <AppPickerDialog :open="pickerOpen" title="Extra apps for the new bench" :options="extraOptions.map((o) => ({ value: o.value, label: extraApp(o.value)?.title || o.label, app: extraApp(o.value), hint: extraApp(o.value)?.source?.url || '' }))" :model-value="extra" @update:model-value="(v) => (extra = v)" @update:open="pickerOpen = $event" />
        </div>
      </div>
      <Alert v-if="project && !findInstance(project, host)?.container_running" theme="gray" :title="`${project} is stopped`" :description="`cwcli will ask to start ${project} first, then add the bench, the same as in a terminal.`" />
      <Alert v-if="host" theme="gray" :title="`Runs${onHost(host)}`" description="The bench is built there, by that machine's own cwcli, through this hub." />
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running</div>
        <CommandLine :command="cli" />
      </div>
      <ErrorMessage :message="error" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" data-primary label="Create bench" :loading="running" :disabled="!ready" @click="start" />
      </div>
    </template>
  </Dialog>
</template>
