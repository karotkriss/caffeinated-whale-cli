<script setup>
// Advisories: the Inbox (captain's pick, note 32), modelled on the lists people already
// read advisories in (Dependabot alerts, GitLab's vulnerability report, the GitHub
// Advisory Database): a status bar, section tabs, severity counts that filter, one
// consistent row (severity · title · what it hits · age), and the open advisory in
// the right pane (a dialog below lg). Affects you / Possible / Exploited / Everything
// else per the spec; the research's rules show through in the detail.
import { computed, onMounted, ref, watch } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { Alert, Badge, Button, Dialog, Select, TabButtons, TextInput, Tooltip } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import AdvisoryDetail from '../components/AdvisoryDetail.vue'
import { advisories, checkAdvisories, loadAdvisories, SEV_THEME, idsOf, whenText } from '../data/advisories'
import { ago } from '../data/catalog'
import { can } from '../data/session'

const section = ref('affects') // affects | possible | global | others
const query = ref('')
const severity = ref('all') // all | critical | high | medium | low
const project = ref('all')
const exploitedOnly = ref(false)
const unreadOnly = ref(false)
const selected = ref(null)
const SEVERITIES = ['critical', 'high', 'medium', 'low']
// Below lg there is no room for two panes: the list stays, a tap opens the detail as a dialog.
const wide = useMediaQuery('(min-width: 1024px)')
const sheetOpen = ref(false)
function pick(a) {
  selected.value = a
  if (!wide.value) sheetOpen.value = true
}
onMounted(() => loadAdvisories())
const data = computed(() => advisories.data)
const lists = computed(() => ({ affects: data.value?.affects || [], possible: data.value?.possible || [], global: data.value?.global || [], others: data.value?.others || [] }))
const q = computed(() => query.value.trim().toLowerCase())
const searched = computed(() => {
  const list = lists.value[section.value] || []
  return q.value ? list.filter((a) => `${a.id} ${(a.aliases || []).join(' ')} ${a.title} ${a.summary}`.toLowerCase().includes(q.value)) : list
})
const sevOf = (a) => (a.severity === 'moderate' ? 'medium' : a.severity)
const hitsOf = (a) => (a.matches || []).filter((m) => m.status !== 'fixed')
const exploited = (a) => !!(a.signals?.kev || a.signals?.national_alert)
const shown = computed(() => searched.value.filter((a) =>
  (severity.value === 'all' || sevOf(a) === severity.value) &&
  (project.value === 'all' || hitsOf(a).some((m) => m.project === project.value)) &&
  (!exploitedOnly.value || exploited(a)) &&
  (!unreadOnly.value || a.state === 'new')))
const exploitedCount = computed(() => searched.value.filter(exploited).length)
const unreadCount = computed(() => searched.value.filter((a) => a.state === 'new').length)
const severityOptions = [{ label: 'Any severity', value: 'all' }, ...SEVERITIES.map((v) => ({ label: v[0].toUpperCase() + v.slice(1), value: v }))]
const projectOptions = computed(() => {
  const names = new Set()
  for (const list of Object.values(lists.value)) for (const a of list) for (const m of hitsOf(a)) if (m.project) names.add(m.project)
  return [{ label: 'Any instance', value: 'all' }, ...[...names].sort().map((n) => ({ label: n, value: n }))]
})
const total = computed(() => Object.values(lists.value).reduce((n, l) => n + l.length, 0))
const filtered = computed(() => !!q.value || severity.value !== 'all' || project.value !== 'all' || exploitedOnly.value || unreadOnly.value)
const thingsToFix = computed(() => (data.value?.fix_groups || []).length)
const running = computed(() => data.value?.inventory_summary?.projects ?? 0)
watch(shown, () => { if (!selected.value || !shown.value.some((a) => a.id === selected.value.id)) selected.value = shown.value[0] || null }, { immediate: true })
const sectionOptions = [
  { label: 'Affects you', value: 'affects' },
  { label: 'Possible', value: 'possible' },
  { label: 'Exploited', value: 'global' },
  { label: 'Everything else', value: 'others' },
]
const sources = computed(() => data.value?.sources || [])
const downSources = computed(() => sources.value.filter((s) => !s.ok))
const progressName = computed(() => sources.value.find((s) => s.key === data.value?.progress)?.name || data.value?.progress || '')
// The row's second line: what it hits here (name, installed → fixed, where), else its ids.
function metaOf(a) {
  const hits = (a.matches || []).filter((m) => m.status !== 'fixed')
  if (hits.length) {
    const m = hits[0]
    const where = [...new Set(hits.map((h) => h.project).filter(Boolean))]
    const more = hits.length > 1 ? ` +${hits.length - 1}` : ''
    // Steering 035: the daemon's fix claim (m.fix.short), never a raw commit hash.
    const fix = m.fix?.short || (m.fixed && !/^[0-9a-f]{7,40}$/.test(m.fixed) ? `fixed in ${m.fixed}` : m.fixed ? 'fixed in a later commit' : '')
    return `${m.name}${m.installed ? ' ' + m.installed : ''}${fix ? ' · ' + fix : ''}${more}${where.length ? ' · ' + where.join(', ') : ''}`
  }
  return idsOf(a).slice(1, 3).join(' · ') || (a.aliases || [])[0] || ''
}
</script>

<template>
  <PageHead title="Advisories" :count="data?.counts?.affects ?? null">
    <template #badge><Badge v-if="data?.counts?.affects_new" size="lg" theme="red" variant="subtle" :label="`${data.counts.affects_new} new`" /></template>
    <template #actions>
      <Button v-if="can('operator')" variant="subtle" icon-left="lucide-refresh-cw" label="Check now" :disabled="data?.checking" :loading="data?.checking" @click="checkAdvisories()" />
    </template>
  </PageHead>

  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <!-- The summary strip, as on the Apps page (captain note 35: one design language) -->
    <div class="grid gap-px overflow-hidden rounded-7 border border-outline-gray-1 bg-surface-gray-2 sm:grid-cols-2 lg:grid-cols-4">
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Affects you</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? lists.affects.length : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">on {{ running }} running {{ running === 1 ? 'instance' : 'instances' }}, {{ thingsToFix }} {{ thingsToFix === 1 ? 'thing' : 'things' }} to update</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Possible</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? lists.possible.length : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">a loosely stated range, or an OS package with no distro fix yet</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Exploited or alerted</div>
        <div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? lists.global.length : '…' }}</div>
        <div class="mt-0.5 text-xs text-ink-gray-5">CISA KEV, CIRT.gy or a high EPSS; not necessarily running here</div>
      </div>
      <div class="bg-surface-elevation-1 px-4 py-3">
        <div class="text-sm text-ink-gray-5">Sources</div>
        <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Tooltip v-for="s in sources" :key="s.key" :text="`${s.name}: ${s.ok ? 'ok' : 'failed'}${s.error ? ' · ' + s.error : ''} · ${s.count} records · ${s.seconds}s`">
            <span class="flex items-center gap-1.5 text-sm" :class="s.ok ? 'text-ink-gray-8' : 'text-ink-amber-6'"><span class="size-1.5 shrink-0 rounded-full" :class="s.ok ? 'bg-surface-green-6' : 'bg-surface-amber-6'" />{{ s.name.replace(' repository advisories', '').replace('.dev', '') }}</span>
          </Tooltip>
          <span v-if="data && !sources.length" class="text-sm text-ink-gray-8">none yet</span>
        </div>
        <div class="mt-0.5 flex items-center gap-1 text-xs text-ink-gray-5">
          <template v-if="data?.checking"><span class="lucide-loader-circle size-3 animate-spin text-ink-blue-6" aria-hidden="true" />checking{{ progressName ? ` ${progressName}` : '' }}</template>
          <template v-else-if="data?.checked_at">checked {{ ago(data.checked_at) }}<template v-if="data.rematched_at && data.rematched_at > data.checked_at + 5"> · re-matched {{ ago(data.rematched_at) }}</template></template>
          <template v-else>{{ data ? 'not checked yet' : 'reading the last check' }}</template>
        </div>
      </div>
    </div>
    <Alert v-if="downSources.length" theme="amber" :title="`${downSources.length} ${downSources.length === 1 ? 'source is' : 'sources are'} not answering`" :description="downSources.map((s) => `${s.name}: ${s.error || 'failed'}`).join(' · ')" />

    <div class="space-y-4">
    <!-- The toolbar, as on the Apps page: search, selects, toggle chips, the section tabs, the count -->
    <div class="flex flex-wrap items-center gap-2">
      <TextInput v-model="query" class="w-64 max-w-full" type="search" placeholder="Search ids, titles, text">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <Select v-model="severity" :options="severityOptions" />
      <Select v-if="projectOptions.length > 1" v-model="project" :options="projectOptions" />
      <button
        type="button"
        class="inline-flex h-7 items-center gap-1.5 rounded-4 border px-2.5 text-base transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
        :class="exploitedOnly ? 'border-outline-red-2 bg-surface-red-1 text-ink-red-6' : 'border-outline-gray-2 bg-surface-elevation-1 text-ink-gray-7 hover:bg-surface-gray-2'"
        :aria-pressed="exploitedOnly"
        title="Only advisories in CISA's Known Exploited Vulnerabilities or alerted by CIRT.gy"
        @click="exploitedOnly = !exploitedOnly"
      >
        <span class="lucide-flame size-3.5" aria-hidden="true" />Exploited<span class="tabular-nums" :class="exploitedOnly ? 'text-ink-red-5' : 'text-ink-gray-5'">{{ exploitedCount }}</span>
      </button>
      <button
        type="button"
        class="inline-flex h-7 items-center gap-1.5 rounded-4 border px-2.5 text-base transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-4"
        :class="unreadOnly ? 'border-outline-blue-2 bg-surface-blue-1 text-ink-blue-6' : 'border-outline-gray-2 bg-surface-elevation-1 text-ink-gray-7 hover:bg-surface-gray-2'"
        :aria-pressed="unreadOnly"
        title="Only advisories nobody has marked read, dismissed or not applicable"
        @click="unreadOnly = !unreadOnly"
      >
        <span class="lucide-mail size-3.5" aria-hidden="true" />Unread<span class="tabular-nums" :class="unreadOnly ? 'text-ink-blue-5' : 'text-ink-gray-5'">{{ unreadCount }}</span>
      </button>
      <TabButtons v-model="section" :options="sectionOptions" />
      <span class="ml-auto text-sm tabular-nums text-ink-gray-5">{{ shown.length }} of {{ total }}</span>
    </div>

    <div v-if="data" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-gray-5">
      <span v-if="data.inventory_summary">matched against {{ data.inventory_summary.apps }} apps, {{ data.inventory_summary.python }} Python and {{ data.inventory_summary.node }} Node packages and {{ data.inventory_summary.images }} images on {{ running }} running {{ running === 1 ? 'instance' : 'instances' }}</span>
      <span class="ml-auto flex items-center gap-2">
        <span>freshness is bounded by the slowest source<template v-if="data.slowest_source"> ({{ data.slowest_source }})</template></span>
        <Button size="xs" variant="ghost" icon-left="lucide-refresh-cw" label="Check now" :disabled="data.checking" @click="checkAdvisories()" />
      </span>
    </div>

    <div v-if="advisories.loading && !data" class="space-y-2"><div v-for="i in 6" :key="i" class="h-14 animate-pulse rounded-6 bg-surface-gray-1" /></div>
    <EmptyState v-else-if="data && !shown.length" icon="lucide-shield-check" :title="filtered ? 'No advisory matches these filters' : section === 'affects' ? 'Nothing matched what runs here' : 'Nothing here'" :description="filtered ? 'Clear a filter to widen the list.' : section === 'affects' ? 'No advisory places an installed app, package or image in an affected range. Possible items, if any, are under the next tab.' : 'Try another tab.'" />

    <div v-else class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(20rem,2fr)_minmax(0,3fr)] lg:items-start">
      <!-- The list -->
      <div class="overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-elevation-1">
        <div class="flex items-center justify-between border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5"><span>{{ shown.length }} {{ shown.length === 1 ? 'advisory' : 'advisories' }}{{ severity !== 'all' ? ` · ${severity}` : '' }}</span><span>newest first within severity</span></div>
        <div class="divide-y divide-outline-gray-1">
          <button v-for="a in shown" :key="a.id" type="button" class="grid w-full grid-cols-[4.5rem_minmax(0,1fr)_auto] items-center gap-3 px-3 py-2.5 text-left transition hover:bg-surface-gray-1" :class="selected?.id === a.id ? 'bg-surface-gray-2' : ''" :aria-current="selected?.id === a.id ? 'true' : undefined" @click="pick(a)">
            <Badge size="sm" :theme="SEV_THEME[a.severity]" :variant="a.severity === 'critical' ? 'solid' : 'subtle'" :label="sevOf(a)" class="justify-center" />
            <span class="min-w-0">
              <span class="block truncate text-base" :class="a.state === 'new' ? 'font-medium text-ink-gray-9' : 'text-ink-gray-6'">{{ a.title }}</span>
              <span class="block truncate text-xs text-ink-gray-5">{{ metaOf(a) }}</span>
            </span>
            <span class="flex items-center gap-1.5 text-xs tabular-nums text-ink-gray-4">
              <Tooltip v-if="a.signals?.kev" text="In CISA's Known Exploited Vulnerabilities"><span class="lucide-flame size-3.5 text-ink-red-5" aria-hidden="true" /></Tooltip>
              <Tooltip v-if="a.signals?.national_alert" :text="`CIRT.gy ${a.signals.national_alert}`"><span class="lucide-megaphone size-3.5 text-ink-orange-5" aria-hidden="true" /></Tooltip>
              <Tooltip v-if="a.state !== 'new'" :text="a.state.replace('_', ' ')"><span class="size-3.5" :class="a.state === 'read' ? 'lucide-check' : a.state === 'dismissed' ? 'lucide-x' : 'lucide-slash'" aria-hidden="true" /></Tooltip>
              <span>{{ whenText(a.published) }}</span>
            </span>
          </button>
        </div>
      </div>
      <!-- The open advisory -->
      <div class="rounded-6 border border-outline-gray-1 bg-surface-elevation-1 p-5 max-lg:hidden lg:sticky lg:top-4"><AdvisoryDetail v-if="selected" :advisory="selected" /></div>
      <Dialog v-if="!wide" :open="sheetOpen" title="Advisory" size="2xl" @update:open="sheetOpen = $event"><AdvisoryDetail v-if="selected" :advisory="selected" /></Dialog>
    </div>
    </div>
  </div>
</template>
