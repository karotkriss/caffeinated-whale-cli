<script setup>
// Install from the grid: instance, then bench, then sites (none means fetch
// only), then branch. Stopped instances stay listed but disabled with the
// reason; sites that already have the app are named rather than offered. It
// starts the existing detached `apps-install` job, so private repositories ride
// the credential bridge exactly as `cwcli apps install` does.
import { computed, ref, watch } from 'vue'
import { trackJob } from '../data/job-toasts'
import { Alert, Button, Checkbox, Combobox, Dialog, ErrorMessage, MultiSelect, Select } from 'frappe-ui'
import { benchRunning, ensureRunning } from '../data/start-gate'
import { getJson } from '../data/api'
import CommandLine from './CommandLine.vue'
import { installSheet } from '../data/app-panel'
import { instances, details, loadDetail, parseApp, benchName } from '../data/fleet'
import { createJob } from '../data/jobs'

const app = computed(() => installSheet.app)
const project = ref(null)
const bench = ref(null)
const sites = ref([])
const branch = ref('')
const fetchOnly = ref(false)
const running = ref(false)
const error = ref(null)

watch(() => installSheet.open, (open) => {
  if (!open) return
  error.value = null
  sites.value = []
  fetchOnly.value = false
  branch.value = app.value?.source?.default_branch || ''
  // Default to a running instance that already holds the app, else the first running one.
  const holders = new Set((app.value?.installs || []).map((i) => i.project))
  const first = instances.value.find((i) => i.container_running && holders.has(i.project)) || instances.value.find((i) => i.container_running) || instances.value.find((i) => holders.has(i.project)) || instances.value[0]
  project.value = first ? first.project : null
})
watch(project, (p) => {
  bench.value = null
  sites.value = []
  if (p) loadDetail(p)
})
const projectOptions = computed(() => instances.value.map((i) => ({ label: i.container_running ? i.project : `${i.project} (stopped, asks to start)`, value: i.project })))
const benches = computed(() => details[project.value]?.data?.benches || [])
// Captain note 46: every bench and site stays selectable; a stopped instance or bench is said, not hidden.
const instRunning = computed(() => !!instances.value.find((i) => i.project === project.value)?.container_running)
const benchState = (b) => (!instRunning.value ? 'stopped' : benchRunning(project.value, b.index) ? 'running' : 'stopped')
const benchOptions = computed(() => benches.value.map((b) => ({ label: `${benchName(b)} · ${b.sites.length} ${b.sites.length === 1 ? 'site' : 'sites'}${benchState(b) === 'stopped' ? ' · stopped' : ''}`, value: String(b.index) })))
const needsStart = computed(() => !!project.value && (!instRunning.value || (theBench.value && benchState(theBench.value) === 'stopped')))
const confirming = ref(false)
watch([project, bench], () => (confirming.value = false))
watch(benchOptions, (opts) => { if (opts.length && bench.value == null) bench.value = opts[0].value }, { immediate: true })
const theBench = computed(() => benches.value.find((b) => String(b.index) === String(bench.value)))
const hasIt = (site) => (site.installed_apps || []).map(parseApp).some((a) => a.name === app.value?.name)
const already = computed(() => (theBench.value?.sites || []).filter(hasIt).map((s) => s.name))
const siteOptions = computed(() => (theBench.value?.sites || []).filter((s) => !hasIt(s)).map((s) => ({ label: `${s.name}${needsStart.value ? ' · stopped' : ''}`, value: s.name })))
// The bench already holds the app's source: install by name. Otherwise fetch the repo.
const onBench = computed(() => !!theBench.value?.available_apps?.includes(app.value?.name))
// Captain note 47: the branch field offers the repository's branches and tags.
const refs = ref({ url: null, branches: [], tags: [], loading: false, error: null })
watch(() => [installSheet.open, app.value?.source?.url], async ([open, url]) => {
  if (!open || !url || refs.value.url === url) return
  refs.value = { url, branches: [], tags: [], loading: true, error: null }
  try {
    const data = await getJson(`/api/v1/apps/refs?url=${encodeURIComponent(url)}`)
    if (refs.value.url !== url) return
    refs.value = { url, branches: data.branches || [], tags: data.tags || [], loading: false, error: data.ok ? null : data.error || 'could not list refs' }
  } catch (e) {
    if (refs.value.url === url) refs.value = { url, branches: [], tags: [], loading: false, error: e.message }
  }
}, { immediate: true })
const refOptions = computed(() => [
  ...(refs.value.branches.length ? [{ group: 'Branches', options: refs.value.branches.map((b) => ({ label: b, value: b })) }] : []),
  ...(refs.value.tags.length ? [{ group: 'Tags', options: refs.value.tags.map((t) => ({ label: t, value: t })) }] : []),
  { type: 'custom', key: 'typed', label: 'Use what I typed', description: 'a ref the list does not show', condition: ({ query }) => !!query && !refs.value.branches.includes(query) && !refs.value.tags.includes(query), onClick: ({ query }) => (branch.value = query) },
])
const spec = computed(() => (onBench.value ? app.value?.name : app.value?.source?.url ? `${app.value.source.url}${app.value.source.url.endsWith('.git') ? '' : '.git'}` : null))
const ready = computed(() => !!project.value && bench.value != null && !!spec.value && !running.value && (fetchOnly.value || sites.value.length > 0))
const q = (s) => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`)
const cli = computed(() => {
  if (!spec.value) return 'cwcli apps install <instance> <repo-url>'
  let line = `cwcli apps install ${q(project.value || '<instance>')} ${q(spec.value)} --bench ${q(bench.value ?? '<bench>')}`
  for (const s of sites.value) line += ` --site ${q(s)}`
  if (branch.value.trim() && !onBench.value) line += ` --branch ${q(branch.value.trim())}`
  if (fetchOnly.value) line += ' --fetch-only'
  return line
})

async function start() {
  if (needsStart.value && !confirming.value) {
    confirming.value = true // captain note 46: say what will be started, then ask once
    return
  }
  if (needsStart.value) {
    const label = theBench.value ? benchName(theBench.value) : null
    const ok = await ensureRunning(project.value, 'Install apps', { bench: theBench.value ? theBench.value.index : null, benchLabel: label, serving: true })
    confirming.value = false
    if (!ok) return
  }
  running.value = true
  error.value = null
  try {
    const record = await createJob({
      verb: 'apps-install',
      target: { project: project.value, bench: String(bench.value) },
      args: { apps: [spec.value], sites: fetchOnly.value ? [] : sites.value, branch: onBench.value ? null : branch.value.trim() || null, fetch_only: fetchOnly.value },
    })
    trackJob(record.id)
    installSheet.open = false
  } catch (e) {
    error.value = e.hint ? `${e.message} ${e.hint}` : e.message
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="installSheet.open" :title="app ? `Install ${app.title}` : 'Install'" size="lg">
    <div v-if="app" class="space-y-4">
      <p class="text-p-base text-ink-gray-6">Fetches the app into the bench if it is not there yet, installs it on the chosen sites, then verifies the serving bench. A private repository uses this machine's gh or glab sign-in through the credential bridge; no token enters the container.</p>
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
          <div class="mb-1 text-xs text-ink-gray-5">Sites</div>
          <MultiSelect v-model="sites" :options="siteOptions" class="w-full" placeholder="None selected: fetch into the bench only" :disabled="fetchOnly || !theBench" />
          <p v-if="already.length" class="mt-1 text-xs text-ink-gray-5">Already installed on {{ already.join(', ') }}; those sites are not offered.</p>
        </div>
        <div v-if="!onBench">
            <Combobox v-model="branch" label="Branch or tag (optional)" :options="refOptions" :loading="refs.loading" :placeholder="app.source?.default_branch || 'default branch'" :empty-text="refs.error ? `refs not listed: ${refs.error}` : 'type a branch or tag'" class="w-full" />
            <p class="mt-1 text-xs text-ink-gray-5">{{ refs.loading ? 'listing branches and tags…' : refs.branches.length || refs.tags.length ? `${refs.branches.length} branches, ${refs.tags.length} tags from the repository` : 'free text; the list could not be read' }}</p>
          </div>
        <Checkbox v-model="fetchOnly" label="Fetch only; do not install on sites" />
      </div>
      <Alert v-if="confirming" theme="orange" :title="`Start ${instRunning ? benchName(theBench) : project} first?`" :description="`${instRunning ? `Bench ${benchName(theBench)} of ${project}` : project} is not running. Starting it is part of this install; the job then fetches and installs as usual.`" />
      <Alert v-else-if="needsStart" theme="gray" :title="`${instRunning ? benchName(theBench) : project} is stopped`" description="Its sites are listed anyway; starting it will be confirmed before the install runs." />
      <Alert v-else-if="!spec" theme="gray" title="No repository URL for this app" description="This app has no git remote cwcli can fetch from; pick a bench that already holds it in apps/." />
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running</div>
        <CommandLine :command="cli" />
      </div>
      <ErrorMessage :message="error" />
      <p class="text-p-sm text-ink-gray-5">The job continues if you close this tab. You can return to it from Jobs.</p>
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button v-if="confirming" label="Back" @click="confirming = false" />
        <Button variant="solid" data-primary :label="confirming ? `Start ${instRunning ? benchName(theBench) : project} and install` : needsStart ? 'Start and install…' : 'Start install job'" :loading="running" :disabled="!ready" @click="start" />
      </div>
    </template>
  </Dialog>
</template>
