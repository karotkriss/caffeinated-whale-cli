<script setup>
// New / edit schedule: what to run, where, when (a friendly picker with the cron
// underneath, or a one-off time), and the guards. Also opened prefilled from a
// job's page ("Schedule this…") and from a template.
import { computed, ref, watch } from 'vue'
import { Button, Checkbox, Dialog, ErrorMessage, FormControl, Select } from 'frappe-ui'
import { scheduleDialog } from '../data/schedule-dialog'
import { DAYS, VERBS, fromCron, saveSchedule, toCron, verbNeed, verbNeedsFlag } from '../data/schedules'
import { details, instances, loadDetail } from '../data/fleet'
import { can } from '../data/session'

const open = computed({ get: () => scheduleDialog.open, set: (v) => (scheduleDialog.open = v) })
const f = ref({})
const picker = ref({ mode: 'daily', time: '02:00', days: [1, 2, 3, 4, 5], day: 1, hours: 6, cron: '' })
const busy = ref(false)
const error = ref('')

function reset() {
  const s = scheduleDialog.schedule || {}
  const t = s.target || {}
  f.value = {
    id: s.id || null,
    name: s.name || '',
    verb: s.verb || 'backup-sites',
    project: t.project || instances.value[0]?.project || '',
    bench: t.bench || '',
    site: t.site || '',
    process: t.process || '',
    apps: Array.isArray(s.args?.apps) ? s.args.apps.join(', ') : s.args?.apps || '',
    on: s.args?.on !== false,
    kind: s.kind || 'cron',
    at: s.at ? s.at.slice(0, 16) : '',
    unattended: !!s.unattended,
    missed: s.missed || 'run',
    start_if_stopped: !!s.start_if_stopped,
    respect_window: !!s.respect_window,
    steps: (s.steps || []).map((x) => ({ verb: x.verb, site: x.target?.site || '' })),
    trigger_type: s.trigger?.type || 'after_job',
    trigger_verb: s.trigger?.verb || 'apps-update',
    trigger_project: s.trigger?.project || '',
  }
  if (s.steps?.length) f.value.verb = 'recipe'
  picker.value = s.cron ? { ...picker.value, ...fromCron(s.cron) } : { mode: 'daily', time: '02:00', days: [1, 2, 3, 4, 5], day: 1, hours: 6, cron: '' }
  error.value = ''
}
watch(open, (v) => v && reset())
watch(() => f.value.project, (p) => { if (p) loadDetail(p) })

const need = computed(() => verbNeed(f.value.verb))
const needsFlag = computed(() => verbNeedsFlag(f.value.verb))
const allBenches = computed(() => details[f.value.project]?.data?.benches || [])
const benches = computed(() => allBenches.value.map((b) => ({ value: String(b.index), label: b.label ? `${b.index} · ${b.label}` : `bench ${b.index}` })))
const sites = computed(() => {
  const b = allBenches.value.find((x) => String(x.index) === String(f.value.bench)) || allBenches.value[0]
  return (b?.sites || []).map((s) => ({ value: s.name || s, label: s.name || s }))
})
const cron = computed(() => toCron(picker.value))
const verbOptions = [...VERBS.filter((v) => !v.admin || can('admin')).map((v) => ({ value: v.value, label: v.label })), { value: 'recipe', label: 'Recipe (several steps, one after another)' }]
const isRecipe = computed(() => f.value.verb === 'recipe')
const addStep = () => f.value.steps.push({ verb: 'backup-sites', site: '' })
const removeStep = (i) => f.value.steps.splice(i, 1)
const projectOptions = computed(() => instances.value.map((i) => ({ value: i.project, label: i.project })))
const toggleDay = (d) => { const set = new Set(picker.value.days || []); set.has(d) ? set.delete(d) : set.add(d); picker.value.days = [...set] }

async function submit() {
  busy.value = true
  error.value = ''
  const v = f.value
  const target = { project: v.project }
  if (['bench', 'site', 'process'].includes(need.value) && v.bench) target.bench = v.bench
  if (need.value === 'site') target.site = v.site
  if (need.value === 'process') target.process = v.process
  const args = {}
  if (v.verb === 'apps-update') { args.apps = v.apps.split(',').map((x) => x.trim()).filter(Boolean); args.build = true }
  if (v.verb === 'dev-mode') args.on = !!v.on
  if (v.verb === 'backup-sites' || v.verb === 'backup') args.reason = 'scheduled'
  const body = { id: v.id || undefined, name: v.name, verb: v.verb, target, args, kind: v.kind, unattended: v.unattended, missed: v.missed, start_if_stopped: v.start_if_stopped, respect_window: v.respect_window }
  if (isRecipe.value) {
    if (v.bench) target.bench = v.bench
    if (v.site) target.site = v.site
    body.steps = v.steps.map((st) => ({ verb: st.verb, target: st.site ? { site: st.site } : {} }))
    body.args = {}
  }
  if (v.kind === 'once') body.at = v.at ? new Date(v.at).toISOString() : ''
  else if (v.kind === 'event') body.trigger = v.trigger_type === 'on_start' ? { type: 'on_start' } : { type: 'after_job', verb: v.trigger_verb, project: v.trigger_project || undefined }
  else body.cron = cron.value
  try {
    await saveSchedule(body)
    open.value = false
  } catch (e) {
    error.value = e.message || 'Could not save the schedule.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="open" :title="f.id ? 'Edit schedule' : 'New schedule'" icon="lucide-calendar-clock" size="2xl">
    <div class="space-y-4">
      <FormControl v-model="f.name" label="Name" placeholder="Nightly backup" />
      <div class="grid gap-4 sm:grid-cols-2">
        <Select v-model="f.verb" label="Job" :options="verbOptions" />
        <Select v-model="f.project" label="Instance" :options="projectOptions" />
        <Select v-if="['bench', 'site', 'process'].includes(need) && benches.length > 1" v-model="f.bench" label="Bench" :options="[{ value: '', label: 'default bench' }, ...benches]" />
        <Select v-if="need === 'site'" v-model="f.site" label="Site" :options="sites" />
        <FormControl v-if="need === 'process'" v-model="f.process" label="Process label" placeholder="worker:default" />
        <FormControl v-if="f.verb === 'apps-update'" v-model="f.apps" label="Apps (comma separated)" placeholder="frappe" description="frappe or erpnext run as a core update, with its automatic backup." />
        <Checkbox v-if="f.verb === 'dev-mode'" v-model="f.on" label="Turn developer mode on (off when unticked)" />
        <template v-if="isRecipe">
          <Select v-if="benches.length > 1" v-model="f.bench" label="Bench" :options="[{ value: '', label: 'default bench' }, ...benches]" />
          <Select v-model="f.site" label="Site (for the site steps)" :options="[{ value: '', label: 'none' }, ...sites]" />
        </template>
      </div>
      <div v-if="isRecipe" class="rounded-6 border border-outline-gray-1 p-3">
        <div class="flex items-center justify-between"><span class="text-base-medium text-ink-gray-9">Steps</span><Button size="sm" icon-left="lucide-plus" label="Add step" @click="addStep" /></div>
        <p class="text-p-sm text-ink-gray-6">Each step starts when the one before it succeeds; a failed step stops the recipe.</p>
        <div v-for="(st, i) in f.steps" :key="i" class="mt-2 flex items-center gap-2">
          <span class="w-5 text-sm text-ink-gray-5">{{ i + 1 }}.</span>
          <Select v-model="st.verb" class="flex-1" :options="verbOptions.filter((o) => o.value !== 'recipe')" />
          <Button size="sm" variant="ghost" icon="lucide-x" @click="removeStep(i)" />
        </div>
      </div>

      <div class="rounded-6 border border-outline-gray-1 p-3">
        <div class="flex flex-wrap items-center gap-2">
          <Select v-model="f.kind" :options="[{ value: 'cron', label: 'Recurring' }, { value: 'once', label: 'Once, at' }, { value: 'event', label: 'On an event' }]" />
          <template v-if="f.kind === 'cron'">
            <Select v-model="picker.mode" :options="[{ value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' }, { value: 'monthly', label: 'Monthly' }, { value: 'hourly', label: 'Hourly' }, { value: 'every', label: 'Every N hours' }, { value: 'custom', label: 'Cron' }]" />
            <FormControl v-if="['daily', 'weekly', 'monthly', 'hourly'].includes(picker.mode)" v-model="picker.time" type="time" :label="picker.mode === 'hourly' ? 'At minute' : 'At'" />
            <FormControl v-if="picker.mode === 'monthly'" v-model="picker.day" type="number" label="Day" min="1" max="28" />
            <FormControl v-if="picker.mode === 'every'" v-model="picker.hours" type="number" label="Hours" min="1" max="23" />
            <FormControl v-if="picker.mode === 'custom'" v-model="picker.cron" label="Cron expression" placeholder="0 2 * * *" spellcheck="false" />
          </template>
          <FormControl v-else-if="f.kind === 'once'" v-model="f.at" type="datetime-local" label="Run at" />
          <template v-else>
            <Select v-model="f.trigger_type" :options="[{ value: 'after_job', label: 'After a successful job' }, { value: 'on_start', label: 'When the instance starts' }]" />
            <Select v-if="f.trigger_type === 'after_job'" v-model="f.trigger_verb" :options="verbOptions.filter((o) => o.value !== 'recipe')" />
            <Select v-if="f.trigger_type === 'after_job'" v-model="f.trigger_project" :options="[{ value: '', label: 'on this instance' }, ...projectOptions]" />
          </template>
        </div>
        <div v-if="f.kind === 'cron' && picker.mode === 'weekly'" class="mt-3 flex flex-wrap gap-1">
          <button v-for="d in DAYS" :key="d.value" type="button" class="rounded-4 border px-2 py-0.5 text-sm" :class="(picker.days || []).includes(d.value) ? 'border-outline-gray-3 bg-surface-gray-3 text-ink-gray-9' : 'border-outline-gray-1 text-ink-gray-6'" @click="toggleDay(d.value)">{{ d.short }}</button>
        </div>
        <p v-if="f.kind === 'cron'" class="mt-2 font-mono text-xs text-ink-gray-5">cron: {{ cron }}</p>
      </div>

      <div class="grid gap-2 sm:grid-cols-2">
        <Checkbox v-if="needsFlag" v-model="f.unattended" label="Allowed unattended (updates and migrate run without anyone watching)" />
        <Checkbox v-model="f.respect_window" label="Only inside the instance's maintenance window" />
        <Checkbox v-model="f.start_if_stopped" label="Start the instance first if it is stopped" />
        <Checkbox :model-value="f.missed === 'skip'" label="Skip a missed run instead of running it on wake" @update:model-value="(v) => (f.missed = v ? 'skip' : 'run')" />
      </div>
      <ErrorMessage :message="error" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" data-primary :label="f.id ? 'Save' : 'Create schedule'" :loading="busy" :disabled="!f.name || !f.project" @click="submit" />
      </div>
    </template>
  </Dialog>
</template>
