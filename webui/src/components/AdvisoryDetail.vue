<script setup>
// One advisory, in the order the GitHub Advisory Database and GitLab's vulnerability
// page settled on: title and triage state; a facts panel (severity with its CVSS and
// source, exploitation signals, dates, identifiers); what it hits HERE as a table
// with the action beside each row; the advisory text; the ranges each source stated;
// references. Triage is a status menu (open / read / dismissed / not applicable).
import { computed, ref } from 'vue'
import { Badge, Button, Dropdown, FormControl, Tooltip } from 'frappe-ui'
import { SEV_THEME, advisories, setAdvisoryState } from '../data/advisories'
import { openJob } from '../data/job-dialog'

const props = defineProps({ advisory: { type: Object, required: true } })
const a = computed(() => props.advisory)
const note = ref('')
const naOpen = ref(false)
const hits = computed(() => (a.value.matches || []).filter((m) => m.status !== 'fixed'))
// Captain note 42 + steering 035: the fix claim is decided by the daemon per row
// (m.fix): the newest release on the branch the bench TRACKS read from its remote,
// the dependency pin on that branch's tip, a commit-only fix named by version. The
// page only renders it; a button appears only when an update actually fixes it.
const reach = (m) => m.fix || { kind: m.fixed ? 'unchecked' : 'nofix', text: m.fixed ? `whether ${m.fixed} is reachable has not been checked yet` : 'no fix stated yet' }
const fixLabel = (m) => m.fix?.fixed_label || m.fixed || 'no fix stated'
const updateLabel = (m) => (m.fix?.app && m.fix.app !== m.name ? `Update ${m.fix.app}` : `Update ${m.name} to ${m.fix?.latest || ''}`.trim())
const actionOf = (m) => m.action || { project: m.project, bench: m.bench_index, apps: m.name }
const primary = computed(() => hits.value.find((m) => reach(m).kind === 'ready' && m.action) || null)
// Advisory text is markdown-ish: fenced blocks become <pre>, headings, **bold** and `code` render.
const blocks = computed(() => {
  const out = []
  let code = false
  for (const part of String(a.value.summary || '').split(/^```[^\n]*$/m)) {
    const text = part.replace(/^\n+|\n+$/g, '')
    if (code && text) out.push({ code, text })
    else if (text) {
      for (const para of text.replace(/^(#{1,6}\s[^\n]*)\n(?!\n)/gm, '$1\n\n').split(/\n{2,}/)) {
        const h = para.match(/^#{1,6}\s+(.*)$/)
        out.push(h ? { heading: true, text: h[1] } : { text: para, runs: para.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/).filter(Boolean) })
      }
    }
    code = !code
  }
  return out
})
const ranges = computed(() => (a.value.ranges || []).filter((r) => r.raw || r.fixed || r.introduced))
const ids = computed(() => [a.value.id, ...(a.value.aliases || [])].filter((v, i, arr) => arr.indexOf(v) === i))
const date = (v) => (v ? String(v).slice(0, 10) : '')
const epss = computed(() => a.value.signals?.epss?.score != null ? `${(a.value.signals.epss.score * 100).toFixed(1)}%` : null)
const STATE = { new: 'Open', read: 'Read', dismissed: 'Dismissed', not_applicable: 'Not applicable' }
const stateOptions = computed(() => [
  { label: 'Mark read', icon: 'lucide-check', onClick: () => mark('read'), condition: () => a.value.state === 'new' },
  { label: 'Dismiss', icon: 'lucide-x', onClick: () => mark('dismissed'), condition: () => a.value.state !== 'dismissed' },
  { label: 'Not applicable here…', icon: 'lucide-slash', onClick: () => (naOpen.value = true), condition: () => a.value.state !== 'not_applicable' },
  { label: 'Reopen', icon: 'lucide-rotate-ccw', onClick: () => mark('new'), condition: () => a.value.state !== 'new' },
])
// "Update pillow" closes every advisory in the same fix group, not only this one.
function closes(m) {
  const g = (advisories.data?.fix_groups || []).find((x) => x.kind === m.kind && x.name === m.name && (x.fixed || '?') === (m.fixed || '?'))
  return g && g.count > 1 ? g.count : null
}
function place(m) {
  return [m.project, m.bench_index != null ? `bench ${m.bench_index}` : null].filter(Boolean).join(' · ') || 'image'
}
function update(m) {
  const a = actionOf(m)
  openJob('apps-update', { project: a.project, bench: String(a.bench), args: { apps: a.apps || '', sites: '' } })
}
async function mark(state) {
  await setAdvisoryState(a.value.id, state, state === 'not_applicable' ? note.value : null)
  naOpen.value = false
  note.value = ''
}
</script>

<template>
  <div class="space-y-5">
    <!-- Title and triage -->
    <div class="flex flex-col gap-3 sm:flex-row sm:items-start">
      <div class="min-w-0 flex-1">
        <h2 class="text-xl font-semibold leading-6 text-ink-gray-9">{{ a.title }}</h2>
        <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-gray-5">
          <span class="font-mono">{{ a.id }}</span>
          <span v-if="a.signals?.kev" class="flex items-center gap-1 text-ink-red-6"><span class="lucide-flame size-3" aria-hidden="true" />known exploited</span>
          <span v-if="a.signals?.national_alert" class="flex items-center gap-1 text-ink-orange-6"><span class="lucide-megaphone size-3" aria-hidden="true" />CIRT.gy {{ a.signals.national_alert }}</span>
          <span v-if="a.published">· published {{ date(a.published) }}</span>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button v-if="primary" variant="solid" :label="updateLabel(primary)" @click="update(primary)" />
        <Dropdown :options="stateOptions" placement="right">
          <Button variant="subtle" :label="STATE[a.state] || a.state" icon-right="lucide-chevron-down" />
        </Dropdown>
      </div>
    </div>
    <div v-if="naOpen" class="flex items-end gap-2 rounded-6 border border-outline-gray-1 bg-surface-gray-1 p-3">
      <FormControl v-model="note" class="flex-1" label="Why it does not apply here" placeholder="e.g. the vulnerable module is not enabled" autofocus />
      <Button variant="solid" label="Save" :disabled="!note.trim()" @click="mark('not_applicable')" />
      <Button variant="ghost" label="Cancel" @click="naOpen = false" />
    </div>
    <p v-else-if="a.note" class="rounded-6 bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-6"><span class="text-ink-gray-5">Not applicable:</span> {{ a.note }}</p>

    <!-- Facts -->
    <dl class="grid grid-cols-2 gap-px overflow-hidden rounded-6 border border-outline-gray-1 bg-outline-gray-1 text-sm sm:grid-cols-4">
      <div class="bg-surface-elevation-1 px-3 py-2">
        <dt class="text-xs text-ink-gray-5">Severity</dt>
        <dd class="mt-1 flex items-center gap-2"><Badge :theme="SEV_THEME[a.severity]" :variant="a.severity === 'critical' ? 'solid' : 'subtle'" :label="a.severity" /><span v-if="a.cvss" class="tabular-nums text-ink-gray-8">{{ a.cvss }}<span class="text-ink-gray-4"> / 10</span></span></dd>
        <dd v-if="a.severity_source" class="mt-0.5 text-xs text-ink-gray-5">CVSS from {{ a.severity_source }}</dd>
      </div>
      <div class="bg-surface-elevation-1 px-3 py-2">
        <dt class="text-xs text-ink-gray-5">Exploitation</dt>
        <dd v-if="a.signals?.kev" class="mt-1 text-ink-red-6">In CISA KEV since {{ a.signals.kev.date_added }}</dd>
        <dd v-else-if="a.signals?.ssvc_exploitation" class="mt-1 text-ink-gray-8">{{ a.signals.ssvc_exploitation }}</dd>
        <dd v-else class="mt-1 text-ink-gray-8">No exploitation known</dd>
        <dd v-if="epss" class="mt-0.5 text-xs text-ink-gray-5">EPSS {{ epss }}<template v-if="a.signals.epss.percentile != null"> ({{ Math.round(a.signals.epss.percentile * 100) }}th percentile)</template></dd>
      </div>
      <div class="bg-surface-elevation-1 px-3 py-2">
        <dt class="text-xs text-ink-gray-5">Dates</dt>
        <dd class="mt-1 text-ink-gray-8">{{ a.published ? `Published ${date(a.published)}` : 'No publish date' }}</dd>
        <dd v-if="a.modified && date(a.modified) !== date(a.published)" class="mt-0.5 text-xs text-ink-gray-5">Updated {{ date(a.modified) }}</dd>
      </div>
      <div class="bg-surface-elevation-1 px-3 py-2">
        <dt class="text-xs text-ink-gray-5">Identifiers</dt>
        <dd class="mt-1 flex flex-wrap gap-x-2 font-mono text-xs text-ink-gray-7"><span v-for="i in ids" :key="i">{{ i }}</span></dd>
      </div>
    </dl>

    <!-- What it hits here -->
    <section>
      <h3 class="text-base font-medium text-ink-gray-8">{{ hits.length ? 'Affected here' : 'Nothing here is in range' }}</h3>
      <div v-if="hits.length" class="mt-2 overflow-x-auto rounded-6 border border-outline-gray-1">
        <table class="w-full text-sm">
          <thead class="bg-surface-gray-1 text-left text-xs font-normal text-ink-gray-5">
            <tr><th class="px-3 py-1.5 font-normal">Where</th><th class="px-3 py-1.5 font-normal">What</th><th class="px-3 py-1.5 font-normal">Installed</th><th class="px-3 py-1.5 font-normal">Fixed in</th><th class="px-3 py-1.5 font-normal">Decided by</th><th class="px-3 py-1.5 text-right font-normal">Action</th></tr>
          </thead>
          <tbody class="divide-y divide-outline-gray-1">
            <tr v-for="m in hits" :key="`${m.project}|${m.bench_index}|${m.name}`">
              <td class="px-3 py-2 text-ink-gray-8">{{ place(m) }}</td>
              <td class="px-3 py-2"><span class="text-ink-gray-9">{{ m.name }}</span><span class="ml-1.5 text-xs text-ink-gray-4">{{ m.kind.replace('-package', '') }}</span></td>
              <td class="px-3 py-2 font-mono text-xs text-ink-gray-7">{{ m.installed || '—' }}</td>
              <td class="px-3 py-2 font-mono text-xs" :class="m.fixed ? 'text-ink-green-6' : 'text-ink-gray-4'">{{ fixLabel(m) }}</td>
              <td class="px-3 py-2"><Tooltip :text="m.how"><span class="flex items-center gap-1 text-xs" :class="m.status === 'affected' ? 'text-ink-gray-6' : 'text-ink-amber-6'">{{ m.status === 'affected' ? m.precision : `possible · ${m.precision}` }}<span class="lucide-info size-3 text-ink-gray-4" aria-hidden="true" /></span></Tooltip></td>
              <td class="px-3 py-2 text-right">
                <template v-if="reach(m).kind === 'ready' && m.action"><Button size="sm" variant="subtle" :label="updateLabel(m)" @click="update(m)" /><span class="mt-0.5 block max-w-64 text-xs text-ink-green-6">{{ reach(m).text }}<template v-if="closes(m)"> · closes {{ closes(m) }}</template></span></template>
                <span v-else class="block max-w-64 text-xs" :class="reach(m).kind === 'ready' ? 'text-ink-green-6' : 'text-ink-gray-5'">{{ reach(m).text || m.note || 'no fix stated yet' }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Advisory text -->
    <section v-if="blocks.length">
      <h3 class="text-base font-medium text-ink-gray-8">Advisory</h3>
      <div class="mt-2 space-y-2">
        <template v-for="(b, i) in blocks" :key="i">
          <pre v-if="b.code" class="overflow-x-auto rounded-5 bg-surface-gray-1 px-3 py-2 font-mono text-xs text-ink-gray-7">{{ b.text }}</pre>
          <h4 v-else-if="b.heading" class="pt-1 text-sm font-medium text-ink-gray-8">{{ b.text }}</h4>
          <p v-else class="whitespace-pre-line break-words text-p-sm text-ink-gray-7"><template v-for="(r, j) in b.runs" :key="j"><strong v-if="r.startsWith('**')" class="font-medium text-ink-gray-8">{{ r.slice(2, -2) }}</strong><code v-else-if="r.startsWith('`')" class="rounded bg-surface-gray-1 px-1 font-mono text-xs">{{ r.slice(1, -1) }}</code><template v-else>{{ r }}</template></template></p>
        </template>
      </div>
    </section>

    <!-- Ranges by source -->
    <section v-if="ranges.length">
      <h3 class="text-base font-medium text-ink-gray-8">Ranges by source</h3>
      <div class="mt-2 overflow-x-auto rounded-6 border border-outline-gray-1">
        <table class="w-full text-sm">
          <thead class="bg-surface-gray-1 text-left text-xs text-ink-gray-5"><tr><th class="px-3 py-1.5 font-normal">Source</th><th class="px-3 py-1.5 font-normal">Package</th><th class="px-3 py-1.5 font-normal">Affected</th><th class="px-3 py-1.5 font-normal">Fixed</th></tr></thead>
          <tbody class="divide-y divide-outline-gray-1">
            <tr v-for="(r, i) in ranges.slice(0, 12)" :key="i">
              <td class="px-3 py-1.5 text-ink-gray-7">{{ r.source }}</td>
              <td class="px-3 py-1.5 text-ink-gray-8">{{ r.target_name }}<span v-if="r.ecosystem" class="ml-1 text-xs text-ink-gray-4">{{ r.ecosystem }}</span></td>
              <td class="px-3 py-1.5 font-mono text-xs text-ink-gray-7">{{ r.raw || (r.introduced ? `≥ ${r.introduced}` : '—') }}<span v-if="r.parsed?.status && !['ok', 'osv'].includes(r.parsed.status)" class="ml-1 font-sans text-ink-amber-6">({{ r.parsed.status }})</span></td>
              <td class="px-3 py-1.5 font-mono text-xs text-ink-gray-7">{{ r.fixed || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- References -->
    <section v-if="(a.sources || []).some((s) => s.url)">
      <h3 class="text-base font-medium text-ink-gray-8">References</h3>
      <ul class="mt-2 space-y-1 text-sm">
        <li v-for="s in (a.sources || []).filter((x) => x.url)" :key="s.url"><a :href="s.url" target="_blank" rel="noopener" class="flex items-center gap-1.5 text-ink-blue-6 hover:underline"><span class="lucide-external-link size-3.5" aria-hidden="true" />{{ s.name }}<span class="truncate text-xs text-ink-gray-4">{{ s.url.replace(/^https?:\/\//, '') }}</span></a></li>
      </ul>
    </section>
  </div>
</template>
