<script setup>
// The restore wizard (phase 1): pick the set (given), pick the target (same site, a new
// site, another bench, another instance), preflight (apps the set needs that the target
// bench lacks, frappe major, the set's verdict, disk), the config rule (carry the set's
// app settings, keep the target's db_* keys, a pre-restore copy), a typed confirm, then
// the restore-set job; its page shows progress and the post-checks.
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, Select } from 'frappe-ui'
import { restoreWizard } from '../data/restore-wizard'
import { instances, details, loadDetail, instance } from '../data/fleet'
import { createJob, loadJob, jobs, ACTIVE_STATES } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { backups, REASON_LABEL, PART_LABEL, sizeText, whenText } from '../data/backups'

const step = ref(1)
const project = ref(null)
const bench = ref(null)
const mode = ref('same') // same | new | existing
const newSite = ref('')
const existingSite = ref('')
const adminPassword = ref('')
const resetAdmin = ref(false)
const keepConfig = ref(false)
const noMigrate = ref(false)
const typed = ref('')
const running = ref(false)
const error = ref(null)
const set = computed(() => restoreWizard.set)

watch(() => restoreWizard.open, (open) => {
  if (!open) return
  step.value = 1
  project.value = set.value?.project || instances.value[0]?.project || null
  bench.value = set.value?.bench_index != null ? String(set.value.bench_index) : null
  mode.value = 'same'
  newSite.value = ''
  existingSite.value = ''
  adminPassword.value = ''
  resetAdmin.value = false
  keepConfig.value = false
  noMigrate.value = false
  typed.value = ''
  error.value = null
})
watch(project, (p) => { if (p) loadDetail(p) }, { immediate: true })
const projectOptions = computed(() => instances.value.map((i) => ({ label: i.container_running ? i.project : `${i.project} (stopped)`, value: i.project })))
const benches = computed(() => details[project.value]?.data?.benches || [])
const benchOptions = computed(() => benches.value.map((b) => ({ label: `${b.label || b.path.split('/').pop()} (#${b.index}) · ${b.sites.length} sites`, value: String(b.index) })))
watch(benchOptions, (opts) => { if (opts.length && (bench.value == null || !opts.some((o) => o.value === bench.value))) bench.value = opts[0].value }, { immediate: true })
const theBench = computed(() => benches.value.find((b) => String(b.index) === String(bench.value)))
const siteOptions = computed(() => (theBench.value?.sites || []).map((s) => ({ label: s.name, value: s.name })))
const sameSiteExists = computed(() => !!(theBench.value?.sites || []).find((s) => s.name === set.value?.site))
const targetSite = computed(() => (mode.value === 'same' ? set.value?.site : mode.value === 'new' ? newSite.value.trim() : existingSite.value))
const siteNameOk = computed(() => /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(targetSite.value || ''))

// Preflight: apps the set lists that the target bench does not hold, the frappe line.
const benchApps = computed(() => new Set(theBench.value?.available_apps || []))
const missingApps = computed(() => (set.value?.apps || []).filter((a) => !benchApps.value.has(a)))
const frappeOnBench = computed(() => {
  const s = (theBench.value?.sites || [])[0]
  const row = (s?.installed_apps || []).map(String).find((a) => a.startsWith('frappe '))
  return row ? row.split(/\s+/)[1] : null
})
const majorMismatch = computed(() => set.value?.frappe_version && frappeOnBench.value && set.value.frappe_version.split('.')[0] !== frappeOnBench.value.split('.')[0])
const budget = computed(() => backups.data?.budget)
const canGo2 = computed(() => !!project.value && bench.value != null && !!targetSite.value && (mode.value !== 'new' || siteNameOk.value))
const confirmOk = computed(() => typed.value.trim() === targetSite.value)

async function start() {
  running.value = true
  error.value = null
  try {
    const args = { set_key: set.value.key, config_mode: keepConfig.value ? 'keep' : 'carry', no_migrate: noMigrate.value }
    const target = { project: project.value, bench: bench.value, site: targetSite.value }
    const secrets = resetAdmin.value && adminPassword.value ? { admin_password: adminPassword.value } : {}
    if (mode.value === 'new') {
      // A new site first (bench new-site with the set's apps), then the restore into it.
      const made = await createJob({ verb: 'new-site', target, args: { apps: (set.value.apps || []).filter((a) => a !== 'frappe' && benchApps.value.has(a)), default: false, developer_mode: false }, secrets: { admin_password: adminPassword.value || crypto.randomUUID() } })
      trackJob(made.id)
      let job = made
      while (ACTIVE_STATES.has(job.state)) {
        await new Promise((resolve) => setTimeout(resolve, 1500))
        job = await loadJob(made.id, jobs.records[made.id]?.events?.at(-1)?.seq || 0)
      }
      if (job.state !== 'succeeded') throw { message: `Creating ${targetSite.value} ${job.state}; nothing was restored.`, hint: job.error?.message || '' }
    }
    const record = await createJob({ verb: 'restore-set', target, args, consent: true, secrets })
    trackJob(record.id)
    restoreWizard.open = false
  } catch (e) {
    error.value = e
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog :open="restoreWizard.open" :title="set ? `Restore ${set.site}` : 'Restore'" size="2xl" @update:open="restoreWizard.open = $event">
    <div v-if="set" class="space-y-4">
      <div class="rounded-6 border border-outline-gray-1 bg-surface-gray-1 px-3 py-2 text-sm">
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1"><span class="font-medium text-ink-gray-9">{{ set.site }}</span><span class="text-ink-gray-5">on {{ set.project }} · {{ whenText(set.created_at) }} · {{ sizeText(set.size) }} · {{ REASON_LABEL[set.reason] || set.reason }}</span><span class="ml-auto inline-flex items-center gap-1 text-xs" :class="set.verified ? 'text-ink-green-6' : 'text-ink-red-6'"><span :class="set.verified ? 'lucide-shield-check' : 'lucide-shield-x'" class="size-3.5" aria-hidden="true" />{{ set.verified ? 'verified' : 'NOT verified' }}</span></div>
        <div class="mt-1 flex flex-wrap gap-1"><span v-for="p in set.parts" :key="p.kind" class="rounded-4 bg-surface-elevation-1 px-1.5 py-0.5 text-xs text-ink-gray-7">{{ PART_LABEL[p.kind] || p.kind }}</span><span v-if="set.frappe_version" class="text-xs text-ink-gray-5">frappe {{ set.frappe_version }}</span><span v-if="set.apps?.length" class="text-xs text-ink-gray-5">· {{ set.apps.join(', ') }}</span></div>
      </div>
      <ol class="flex items-center gap-2 text-xs text-ink-gray-5">
        <li v-for="(label, i) in ['Target', 'Preflight', 'Config and confirm']" :key="label" class="flex items-center gap-2"><span class="flex size-5 items-center justify-center rounded-full text-[11px]" :class="step === i + 1 ? 'bg-surface-gray-7 text-ink-white' : step > i + 1 ? 'bg-surface-green-6 text-ink-white' : 'bg-surface-gray-2 text-ink-gray-6'">{{ i + 1 }}</span><span :class="step === i + 1 ? 'text-ink-gray-9' : ''">{{ label }}</span><span v-if="i < 2" class="lucide-chevron-right size-3" aria-hidden="true" /></li>
      </ol>

      <template v-if="step === 1">
        <div class="grid gap-4 sm:grid-cols-2">
          <Select v-model="project" label="Instance" :options="projectOptions" />
          <Select v-model="bench" label="Bench" :options="benchOptions" :disabled="!benchOptions.length" />
        </div>
        <div class="space-y-2">
          <div class="text-xs text-ink-gray-5">Into</div>
          <label class="flex items-start gap-2 text-sm"><input v-model="mode" type="radio" value="same" class="mt-1" :disabled="!sameSiteExists" /><span><span class="text-ink-gray-9">The same site, {{ set.site }}</span><span class="block text-xs text-ink-gray-5">{{ sameSiteExists ? 'replaces every byte of it' : 'that site does not exist on this bench' }}</span></span></label>
          <label class="flex items-start gap-2 text-sm"><input v-model="mode" type="radio" value="new" class="mt-1" /><span class="min-w-0 flex-1"><span class="text-ink-gray-9">A new site</span><FormControl v-if="mode === 'new'" v-model="newSite" class="mt-1" placeholder="copy.localhost" autocomplete="off" spellcheck="false" /><span v-if="mode === 'new' && newSite && !siteNameOk" class="block text-xs text-ink-red-5">lowercase letters, digits, dots and dashes</span></span></label>
          <label class="flex items-start gap-2 text-sm"><input v-model="mode" type="radio" value="existing" class="mt-1" /><span class="min-w-0 flex-1"><span class="text-ink-gray-9">Another existing site on this bench</span><Select v-if="mode === 'existing'" v-model="existingSite" class="mt-1" :options="siteOptions" placeholder="pick a site" /></span></label>
        </div>
      </template>

      <template v-else-if="step === 2">
        <div class="divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1 text-sm">
          <div class="flex items-start gap-3 px-3 py-2"><span :class="set.verified ? 'lucide-circle-check text-ink-green-6' : 'lucide-circle-x text-ink-red-6'" class="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span><span class="text-ink-gray-9">Set verified at creation</span><span class="block text-xs text-ink-gray-5">{{ set.checks.map((c) => `${c.name} ${c.ok ? 'ok' : 'FAILED'}`).join(' · ') }}</span></span></div>
          <div class="flex items-start gap-3 px-3 py-2"><span :class="missingApps.length ? 'lucide-triangle-alert text-ink-amber-6' : 'lucide-circle-check text-ink-green-6'" class="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span><span class="text-ink-gray-9">Apps the set was taken with</span><span class="block text-xs text-ink-gray-5">{{ set.apps?.length ? set.apps.join(', ') : 'not recorded' }}<template v-if="missingApps.length"> · missing on this bench: <span class="text-ink-amber-6">{{ missingApps.join(', ') }}</span> (fetch them with Install apps first, or the migrate after the restore will fail)</template></span></span></div>
          <div class="flex items-start gap-3 px-3 py-2"><span :class="majorMismatch ? 'lucide-triangle-alert text-ink-amber-6' : 'lucide-circle-check text-ink-green-6'" class="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span><span class="text-ink-gray-9">Frappe line</span><span class="block text-xs text-ink-gray-5">set {{ set.frappe_version || 'unknown' }} · bench {{ frappeOnBench || 'unknown' }}{{ majorMismatch ? ' · different major: the migrate will carry the schema forward, downgrades are not supported' : '' }}</span></span></div>
          <div class="flex items-start gap-3 px-3 py-2"><span :class="budget?.warn ? 'lucide-triangle-alert text-ink-amber-6' : 'lucide-circle-check text-ink-green-6'" class="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span><span class="text-ink-gray-9">Disk</span><span class="block text-xs text-ink-gray-5">{{ budget ? `${budget.free_gb} GB free where the store lives` : 'not read' }}</span></span></div>
          <div class="flex items-start gap-3 px-3 py-2"><span class="lucide-circle-check mt-0.5 size-4 shrink-0 text-ink-green-6" aria-hidden="true" /><span><span class="text-ink-gray-9">Database credentials</span><span class="block text-xs text-ink-gray-5">the instance's remembered MariaDB root password is used; never shown</span></span></div>
        </div>
      </template>

      <template v-else>
        <div class="space-y-3">
          <div class="rounded-6 border border-outline-gray-1 px-3 py-2 text-sm">
            <div class="text-ink-gray-9">Site config</div>
            <p class="mt-0.5 text-xs text-ink-gray-5">The restored site carries the set's app settings (every key of its site_config) and keeps <span class="font-mono">db_name, db_user, db_password, db_type, db_host, db_port, db_socket</span> of the target; the target's current config is copied aside first as <span class="font-mono">site_config.json.pre-restore-&lt;time&gt;</span>.</p>
            <Checkbox v-model="keepConfig" class="mt-2" label="Keep the target's site_config as bench restore leaves it instead (no settings carried)" />
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <div><Checkbox v-model="resetAdmin" label="Reset the Administrator password afterwards" /><FormControl v-if="resetAdmin" v-model="adminPassword" class="mt-2" type="password" label="New Administrator password" autocomplete="new-password" /></div>
            <Checkbox v-model="noMigrate" label="Do not migrate after the restore" />
          </div>
          <Alert theme="red" :title="`Everything in ${targetSite} on ${project} will be replaced`" :description="mode === 'new' ? 'The new site is created first, then the set is restored into it.' : 'Its current database and files are overwritten by the set; its own config is kept as described above.'" />
          <FormControl v-model="typed" :label="`Type the site name to confirm: ${targetSite}`" autocomplete="off" spellcheck="false" />
        </div>
      </template>
      <ErrorMessage :message="error?.hint ? `${error.message} ${error.hint}` : error?.message" />
    </div>
    <template #actions="{ close }">
      <div class="flex items-center gap-2">
        <Button v-if="step > 1" label="Back" @click="step -= 1" />
        <span class="ml-auto flex items-center gap-2">
          <Button label="Cancel" @click="close" />
          <Button v-if="step < 3" variant="solid" label="Next" :disabled="step === 1 && !canGo2" @click="step += 1" />
          <Button v-else variant="solid" theme="red" data-primary :label="`Restore into ${targetSite}`" :disabled="!confirmOk || (resetAdmin && !adminPassword)" :loading="running" @click="start" />
        </span>
      </div>
    </template>
  </Dialog>
</template>
