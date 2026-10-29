<script setup>
// Schedules: every scheduled job with its target, timetable, next run, last result and
// an on/off switch; a timeline of the coming week; templates; the windows and guards.
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Checkbox, Dialog, Dropdown, ErrorMessage, FormControl, Select, Switch, Tooltip } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import { instances, setFocus } from '../data/fleet'
import { openSchedule, scheduleFromTemplate } from '../data/schedule-dialog'
import { DAYS, deleteSchedule, enableSchedule, list, loadSchedules, runSchedule, saveScheduleSettings, schedules, templates, upcoming, verbLabel, whenAbs, whenRel } from '../data/schedules'
import { toast } from 'frappe-ui'
import { can } from '../data/session'

onMounted(() => { setFocus(null); loadSchedules() })
const busy = ref('')
const err = ref(null)

async function run(s) {
  busy.value = s.id
  try {
    const job = await runSchedule(s.id)
    toast({ title: `${verbLabel(s.verb)} started`, text: `job ${job.id}`, icon: 'play' })
  } catch (e) { err.value = e } finally { busy.value = '' }
}
async function toggle(s, on) { try { await enableSchedule(s.id, on) } catch (e) { err.value = e } }
async function remove(s) { if (!confirm(`Delete the schedule "${s.name}"?`)) return; try { await deleteSchedule(s.id) } catch (e) { err.value = e } }

const targetText = (s) => { const t = s.target || {}; return [t.project, t.bench != null && t.bench !== '' ? `bench ${t.bench}` : null, t.site, t.process].filter(Boolean).join(' · ') }
const LAST = { succeeded: ['bg-ink-green-5', 'ok'], failed: ['bg-ink-red-5', 'failed'], skipped: ['bg-ink-amber-5', 'skipped'], queued: ['bg-ink-blue-5', 'running'], running: ['bg-ink-blue-5', 'running'], interrupted: ['bg-ink-amber-5', 'interrupted'], unknown: ['bg-ink-gray-4', 'unknown'], needs_choice: ['bg-ink-amber-5', 'needs a decision'] }
const lastOf = (s) => { const l = s.last_run; if (!l || !l.state) return null; const [dot, text] = LAST[l.state] || ['bg-ink-gray-4', l.state]; return { dot, text, at: l.finished_at || l.at, job: l.job_id, reason: l.reason || l.error } }

// Timeline: the next 7 days grouped by day.
const days = computed(() => {
  const groups = new Map()
  for (const u of upcoming.value) {
    const d = new Date(u.at)
    const key = d.toDateString()
    if (!groups.has(key)) groups.set(key, { key, label: d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' }), items: [] })
    groups.get(key).items.push(u)
  }
  return [...groups.values()]
})
const templateItems = computed(() => templates.value.map((t) => ({ label: t.name, onClick: () => openSchedule(scheduleFromTemplate(t, instances.value[0]?.project || '')) })))

// Windows and guards
const settingsOpen = ref(false)
const windowOn = ref(true)
const sv = ref({ project: '', start: '02:00', end: '05:00', days: [0, 1, 2, 3, 4, 5, 6], quietOn: false, qStart: '22:00', qEnd: '06:00', disk: 5, mem: 512, digest: 'off' })
const DAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
function openSettings() {
  const d = schedules.data || {}
  const p = sv.value.project || instances.value[0]?.project || ''
  const w = (d.windows || {})[p]
  sv.value = { project: p, start: w?.start || '02:00', end: w?.end || '05:00', days: (w?.days || DAY_KEYS).map((k) => DAY_KEYS.indexOf(k)), quietOn: !!d.quiet_hours, qStart: d.quiet_hours?.start || '22:00', qEnd: d.quiet_hours?.end || '06:00', disk: d.guards?.min_free_disk_gb ?? 5, mem: d.guards?.min_free_mem_mb ?? 512, digest: d.digest || 'off' }
  windowOn.value = !!w
  settingsOpen.value = true
}
function loadWindow(project) { sv.value.project = project; const w = ((schedules.data || {}).windows || {})[project]; windowOn.value = !!w; sv.value.start = w?.start || '02:00'; sv.value.end = w?.end || '05:00'; sv.value.days = (w?.days || DAY_KEYS).map((k) => DAY_KEYS.indexOf(k)) }
const toggleDay = (d) => { const set = new Set(sv.value.days); set.has(d) ? set.delete(d) : set.add(d); sv.value.days = [...set] }
async function saveSettings() {
  const windows = { ...((schedules.data || {}).windows || {}) }
  windows[sv.value.project] = windowOn.value ? { start: sv.value.start, end: sv.value.end, days: sv.value.days.map((i) => DAY_KEYS[i]) } : null
  try {
    await saveScheduleSettings({ windows, quiet_hours: sv.value.quietOn ? { start: sv.value.qStart, end: sv.value.qEnd } : null, guards: { min_free_disk_gb: Number(sv.value.disk), min_free_mem_mb: Number(sv.value.mem) }, digest: sv.value.digest })
    settingsOpen.value = false
  } catch (e) { err.value = e }
}
const windowText = (p) => { const w = ((schedules.data || {}).windows || {})[p]; return w ? `${w.start}-${w.end}${(w.days || []).length < 7 ? ' ' + (w.days || []).map((k) => k[0].toUpperCase() + k.slice(1)).join(', ') : ''}` : null }
</script>

<template>
  <PageHead title="Schedules" :count="list.length || null">
    <template v-if="can('operator')" #actions>
      <Button variant="subtle" icon-left="lucide-sliders-horizontal" label="Windows and guards…" @click="openSettings" />
      <Dropdown :options="templateItems"><Button variant="subtle" icon-left="lucide-layout-template" label="From a template" icon-right="lucide-chevron-down" /></Dropdown>
      <Button variant="solid" icon-left="lucide-plus" label="New schedule" @click="openSchedule()" />
    </template>
  </PageHead>

  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <ErrorMessage :message="err?.message" />
    <Alert v-if="schedules.data?.quiet_hours" theme="gray" title="Quiet hours" :description="`No scheduled job starts between ${schedules.data.quiet_hours.start} and ${schedules.data.quiet_hours.end} (stops and starts excepted).`" />

    <EmptyState v-if="!schedules.loading && !list.length" icon="lucide-calendar-clock" title="Nothing scheduled yet" description="A schedule starts an ordinary job on a timetable: a nightly backup, a weekly core update inside a maintenance window, a restart, a stop in the evening. Removals can never be scheduled.">
      <Button variant="solid" label="New schedule" @click="openSchedule()" />
    </EmptyState>

    <section v-else class="space-y-2">
      <div v-for="s in list" :key="s.id" class="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-6 border border-outline-gray-1 bg-surface-base px-4 py-3" :class="s.enabled === false ? 'opacity-60' : ''">
        <Switch :model-value="s.enabled !== false" size="sm" @update:model-value="(v) => toggle(s, v)" />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-x-2">
            <span class="text-base-medium text-ink-gray-9">{{ s.name }}</span>
            <span class="text-sm text-ink-gray-6">{{ verbLabel(s.verb) }} · {{ targetText(s) }}</span>
            <span v-if="s.steps?.length" class="text-sm text-ink-gray-6">· recipe, {{ s.steps.length }} steps</span>
          </div>
          <div class="mt-0.5 flex flex-wrap items-center gap-x-3 text-sm text-ink-gray-6">
            <span>{{ s.kind === 'once' ? `once at ${whenAbs(s.at)}` : s.when_text }}</span>
            <span v-if="s.respect_window">· in the maintenance window{{ windowText(s.target?.project) ? ` (${windowText(s.target?.project)})` : ' (none set)' }}</span>
            <span v-if="s.unattended" class="text-ink-amber-6">· unattended</span>
            <span v-if="s.last_skip" class="text-ink-amber-6">· held: {{ s.last_skip.reason }}</span>
          </div>
        </div>
        <div class="w-40 text-sm">
          <div class="text-ink-gray-5">Next run</div>
          <Tooltip :text="whenAbs(s.next_run)"><div class="text-ink-gray-8">{{ s.next_run ? whenRel(s.next_run) : (s.kind === 'event' ? 'on its event' : 'off') }}</div></Tooltip>
        </div>
        <div class="w-44 text-sm">
          <div class="text-ink-gray-5">Last result</div>
          <div v-if="lastOf(s)" class="flex items-center gap-1.5 text-ink-gray-8">
            <span class="inline-block size-2 rounded-full" :class="lastOf(s).dot" />
            <router-link v-if="lastOf(s).job" :to="`/jobs/${lastOf(s).job}`" class="hover:underline">{{ lastOf(s).text }}</router-link>
            <span v-else :title="lastOf(s).reason">{{ lastOf(s).text }}</span>
            <span class="text-ink-gray-5">{{ whenRel(lastOf(s).at) }}</span>
          </div>
          <div v-else class="text-ink-gray-5">never ran</div>
        </div>
        <div class="flex items-center gap-1">
          <Button size="sm" variant="subtle" icon-left="lucide-play" label="Run now" :loading="busy === s.id" @click="run(s)" />
          <Dropdown :options="[{ label: 'Edit', icon: 'lucide-pencil', onClick: () => openSchedule(s) }, { label: 'Delete', icon: 'lucide-trash-2', onClick: () => remove(s) }]"><Button size="sm" variant="ghost" icon="lucide-ellipsis" /></Dropdown>
        </div>
      </div>
    </section>

    <section v-if="days.length">
      <h2 class="text-lg-semibold text-ink-gray-9">Coming up</h2>
      <div class="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <div v-for="d in days" :key="d.key" class="rounded-6 border border-outline-gray-1 bg-surface-base p-3">
          <div class="text-sm font-medium text-ink-gray-7">{{ d.label }}</div>
          <ul class="mt-2 space-y-1">
            <li v-for="(u, i) in d.items" :key="i" class="flex items-center gap-2 text-sm">
              <span class="w-16 shrink-0 tabular-nums text-ink-gray-5">{{ new Date(u.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }}</span>
              <span class="truncate text-ink-gray-8">{{ u.name }}</span>
              <span class="truncate text-ink-gray-5">{{ u.target?.project }}</span>
            </li>
          </ul>
        </div>
      </div>
    </section>
  </div>

  <Dialog v-model:open="settingsOpen" title="Windows and guards" icon="lucide-sliders-horizontal" size="xl">
    <div class="space-y-5">
      <div>
        <div class="text-base-medium text-ink-gray-9">Maintenance window</div>
        <p class="text-p-sm text-ink-gray-6">A schedule marked "only inside the window" waits for it. Per instance.</p>
        <div class="mt-2 grid gap-3 sm:grid-cols-3">
          <Select :model-value="sv.project" label="Instance" :options="instances.map((i) => ({ value: i.project, label: i.project }))" @update:model-value="loadWindow" />
          <FormControl v-model="sv.start" type="time" label="From" />
          <FormControl v-model="sv.end" type="time" label="To" />
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-1">
          <button v-for="d in DAYS" :key="d.value" type="button" class="rounded-4 border px-2 py-0.5 text-sm" :class="sv.days.includes(d.value) ? 'border-outline-gray-3 bg-surface-gray-3 text-ink-gray-9' : 'border-outline-gray-1 text-ink-gray-6'" @click="toggleDay(d.value)">{{ d.short }}</button>
          <Checkbox v-model="windowOn" class="ml-3" label="Window set for this instance" />
        </div>
      </div>
      <div>
        <div class="text-base-medium text-ink-gray-9">Quiet hours</div>
        <p class="text-p-sm text-ink-gray-6">Nothing starts in them (stops and starts excepted); a due run waits for their end.</p>
        <div class="mt-2 flex flex-wrap items-end gap-3">
          <Checkbox v-model="sv.quietOn" label="On" />
          <FormControl v-model="sv.qStart" type="time" label="From" :disabled="!sv.quietOn" />
          <FormControl v-model="sv.qEnd" type="time" label="To" :disabled="!sv.quietOn" />
        </div>
      </div>
      <div class="grid gap-3 sm:grid-cols-3">
        <FormControl v-model="sv.disk" type="number" label="Hold runs under (GB free on disk)" min="0" />
        <FormControl v-model="sv.mem" type="number" label="Hold runs under (MB memory free)" min="0" />
        <Select v-model="sv.digest" label="Digest in Notifications" :options="[{ value: 'off', label: 'Off' }, { value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' }]" />
      </div>
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2"><Button label="Cancel" @click="close" /><Button variant="solid" data-primary label="Save" @click="saveSettings" /></div>
    </template>
  </Dialog>
</template>
