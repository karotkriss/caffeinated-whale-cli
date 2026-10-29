<script setup>
// Clone or move sites (phase 2): the sources (one, or picked from a bench), the target
// instance and bench, a name per copy, the config rule, and for a move or a copy that
// replaces an existing site a typed confirm.
// One job runs them in order with x of N progress; a failure stops before the next source.
// A move of a published site keeps its public address when the name is kept (its proxy
// host is pointed at the new bench); a renamed one offers Also unpublish (ticked) for
// the old name and Publish for the new one, each a job of its own after the move.
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, Select } from 'frappe-ui'
import { transferDialog } from '../data/transfer'
import { instances, details, loadDetail } from '../data/fleet'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { ensureRunning } from '../data/start-gate'
import { can } from '../data/session'
import { canPublish, loadPublications, publicationAt } from '../data/publish'

const d = transferDialog
const mode = ref('clone')
const chosen = ref([])
const names = ref({})
const targetProject = ref(null)
const targetBench = ref(null)
const keepConfig = ref(false)
const withFiles = ref(true)
const typed = ref('')
const unpublish = ref(true)
const publishNew = ref(false)
const running = ref(false)
const error = ref(null)

watch(() => d.open, (open) => {
  if (!open) return
  mode.value = can('admin') ? d.mode : 'clone'
  chosen.value = d.pick ? [] : [...d.sites]
  targetProject.value = d.project
  targetBench.value = d.bench
  keepConfig.value = false
  withFiles.value = true
  typed.value = ''
  error.value = null
  names.value = {}
  unpublish.value = true
  publishNew.value = false
  loadPublications()
  if (d.project) loadDetail(d.project)
})
watch(targetProject, (p) => { if (p) loadDetail(p) })
const projectOptions = computed(() => instances.value.map((i) => ({ label: i.container_running ? i.project : `${i.project} (stopped)`, value: i.project })))
const sourceBench = computed(() => (details[d.project]?.data?.benches || []).find((b) => String(b.index) === String(d.bench)))
const benches = computed(() => details[targetProject.value]?.data?.benches || [])
const benchOptions = computed(() => benches.value.map((b) => ({ label: `${b.label || b.path.split('/').pop()} (#${b.index}) · ${b.sites.length} sites`, value: String(b.index) })))
watch(benchOptions, (opts) => { if (opts.length && !opts.some((o) => o.value === targetBench.value)) targetBench.value = opts[0].value }, { immediate: true })
const samePlace = computed(() => targetProject.value === d.project && String(targetBench.value) === String(d.bench))
const targetSites = computed(() => new Set((benches.value.find((b) => String(b.index) === String(targetBench.value))?.sites || []).map((s) => s.name)))
// The copy's default name: the same name elsewhere, `<label>-copy.<rest>` on the same bench.
function defaultName(site) {
  if (!samePlace.value) return site
  const [first, ...rest] = site.split('.')
  return [`${first}-copy`, ...rest].join('.')
}
const targetName = (site) => (names.value[site] ?? defaultName(site))
const nameOk = (n) => /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(n || '')
const pairs = computed(() => chosen.value.map((site) => ({ site, target_site: targetName(site).trim() })))
const clashes = computed(() => pairs.value.filter((p) => samePlace.value && p.site === p.target_site).map((p) => p.site))
const overwrites = computed(() => pairs.value.filter((p) => targetSites.value.has(p.target_site) && !(samePlace.value && p.site === p.target_site)).map((p) => p.target_site))
const valid = computed(() => pairs.value.length > 0 && pairs.value.every((p) => nameOk(p.target_site)) && !clashes.value.length && targetProject.value && targetBench.value != null)
const needsConsent = computed(() => mode.value === 'move' || overwrites.value.length > 0)
const confirmWord = computed(() => {
  if (overwrites.value.length) return overwrites.value.length === 1 ? overwrites.value[0] : `replace ${overwrites.value.length} sites`
  if (mode.value === 'move') return pairs.value.length === 1 ? pairs.value[0].site : `move ${pairs.value.length} sites`
  return ''
})
const confirmWhat = computed(() => (overwrites.value.length ? (mode.value === 'move' ? 'the move and the replacement' : 'the replacement') : 'the move'))
const confirmed = computed(() => !needsConsent.value || typed.value.trim() === confirmWord.value)
// Published sources of a move: those keeping their name, and those renamed.
const published = computed(() => (mode.value === 'move' ? pairs.value.filter((p) => publicationAt(p.site, d.project, sourceBench.value?.path)) : []))
const kept = computed(() => published.value.filter((p) => p.site === p.target_site))
const renamed = computed(() => published.value.filter((p) => p.site !== p.target_site))
function toggle(site) {
  chosen.value = chosen.value.includes(site) ? chosen.value.filter((s) => s !== site) : [...chosen.value, site]
}
async function start() {
  running.value = true
  error.value = null
  try {
    if (!(await ensureRunning(d.project, mode.value === 'move' ? 'Move sites' : 'Clone sites', { bench: d.bench }))) return
    if (targetProject.value !== d.project && !(await ensureRunning(targetProject.value, 'the target instance', { bench: targetBench.value }))) return
    const record = await createJob({
      verb: mode.value === 'move' ? 'move-sites' : 'clone-sites',
      target: { project: d.project, bench: d.bench },
      args: {
        sites: pairs.value,
        target_project: targetProject.value,
        target_bench: targetBench.value,
        config_mode: keepConfig.value ? 'keep' : 'carry',
        with_files: withFiles.value,
        ...(renamed.value.length ? { unpublish: unpublish.value, publish: publishNew.value && canPublish() } : {}),
      },
      consent: needsConsent.value,
    })
    trackJob(record.id)
    d.open = false
  } catch (e) {
    error.value = e
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog :open="d.open" :title="mode === 'move' ? 'Move sites' : 'Clone sites'" size="2xl" @update:open="d.open = $event">
    <div class="space-y-4">
      <div class="flex gap-2 text-sm">
        <label class="flex items-center gap-2"><input v-model="mode" type="radio" value="clone" /><span>Clone <span class="text-ink-gray-5">(the source stays)</span></span></label>
        <label class="flex items-center gap-2"><input v-model="mode" type="radio" value="move" :disabled="!can('admin')" /><span>Move <span class="text-ink-gray-5">(the source is dropped once its copy answers)</span></span></label>
      </div>
      <div v-if="d.pick" class="space-y-1">
        <div class="text-xs text-ink-gray-5">Sites on {{ d.project }} · bench {{ d.bench }}</div>
        <div class="grid gap-1 sm:grid-cols-2">
          <label v-for="s in sourceBench?.sites || []" :key="s.name" class="flex items-center gap-2 rounded-6 border border-outline-gray-1 px-2 py-1.5 text-sm"><input type="checkbox" :checked="chosen.includes(s.name)" @change="toggle(s.name)" /><span class="truncate">{{ s.name }}</span></label>
        </div>
        <div class="flex gap-3 text-xs"><button type="button" class="text-ink-gray-6 hover:underline" @click="chosen = (sourceBench?.sites || []).map((s) => s.name)">all</button><button type="button" class="text-ink-gray-6 hover:underline" @click="chosen = []">none</button></div>
      </div>
      <div v-else class="text-sm text-ink-gray-8">{{ d.sites.join(', ') }} <span class="text-ink-gray-5">on {{ d.project }} · bench {{ d.bench }}</span></div>
      <div class="grid gap-4 sm:grid-cols-2">
        <Select v-model="targetProject" label="To instance" :options="projectOptions" />
        <Select v-model="targetBench" label="To bench" :options="benchOptions" :disabled="!benchOptions.length" />
      </div>
      <div v-if="pairs.length" class="space-y-1">
        <div class="text-xs text-ink-gray-5">Name of each copy</div>
        <div v-for="p in pairs" :key="p.site" class="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 text-sm">
          <span class="truncate text-ink-gray-7">{{ p.site }}</span><span class="lucide-arrow-right size-3.5 text-ink-gray-4" aria-hidden="true" />
          <FormControl :model-value="targetName(p.site)" autocomplete="off" spellcheck="false" @update:model-value="names[p.site] = $event" />
        </div>
        <p v-if="clashes.length" class="text-xs text-ink-red-5">{{ clashes.join(', ') }} cannot be copied onto itself; give the copy another name.</p>
        <p v-else-if="pairs.some((p) => !nameOk(p.target_site))" class="text-xs text-ink-red-5">lowercase letters, digits, dots and dashes</p>
      </div>
      <Alert v-if="overwrites.length" theme="amber" title="Existing sites will be replaced" :description="`${overwrites.join(', ')} ${overwrites.length === 1 ? 'exists' : 'already exist'} on the target bench; each is backed up into the store first (a verified before-overwrite set), then the copy replaces every byte of it (the target keeps its own database name and credentials).`" />
      <div class="space-y-2">
        <Checkbox v-model="withFiles" label="Include public and private files" />
        <Checkbox v-model="keepConfig" label="Keep the target's site config as is (default: carry the source's app settings, keep the target's db_* keys)" />
      </div>
      <div v-if="published.length" class="space-y-2 rounded-6 border border-outline-gray-1 p-3">
        <div class="text-sm font-medium text-ink-gray-8">Public addresses</div>
        <p v-if="kept.length" class="text-p-sm text-ink-gray-6">{{ kept.map((p) => p.site).join(', ') }} {{ kept.length === 1 ? 'keeps its' : 'keep their' }} public address: after the move, Publish site points the proxy host at the new bench. The DNS record does not change.</p>
        <template v-if="renamed.length">
          <Checkbox v-model="unpublish" :label="`Also unpublish ${renamed.map((p) => p.site).join(', ')}: remove the DNS record, proxy host and certificate cwcli made for the old ${renamed.length === 1 ? 'name' : 'names'}`" />
          <Checkbox v-if="canPublish()" v-model="publishNew" :label="`Publish ${renamed.map((p) => p.target_site).join(', ')} at ${renamed.length === 1 ? 'its' : 'their'} new ${renamed.length === 1 ? 'name' : 'names'}`" />
        </template>
      </div>
      <p class="text-xs text-ink-gray-5">Each site is backed up into the store first (a verified set; a failed one stops before anything is created), the copy is created if it does not exist, restored, migrated and checked.<template v-if="mode === 'move'"> Then the source is dropped through the ordinary site-removal gate, so its own archive lands in the store too.</template> {{ pairs.length > 1 ? 'Sites run in order with x of N progress; a failure stops before the next source is touched.' : '' }}</p>
      <div v-if="needsConsent" class="flex items-end gap-2">
        <FormControl v-model="typed" class="w-80" :label="`Type ${confirmWord} to confirm ${confirmWhat}`" :placeholder="confirmWord" autocomplete="off" />
      </div>
      <ErrorMessage :message="error?.message || error" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button variant="ghost" label="Cancel" @click="close" />
        <Button variant="solid" data-primary :theme="needsConsent ? 'red' : 'gray'" :label="mode === 'move' ? `Move ${pairs.length || ''}`.trim() : `Clone ${pairs.length || ''}`.trim()" :disabled="!valid || !confirmed" :loading="running" @click="start" />
      </div>
    </template>
  </Dialog>
</template>
