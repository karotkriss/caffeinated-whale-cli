<script setup>
// Backups, all instances: one row per site with its last-good badge, count, size and
// the newest sets (the captain's pick, look A "By site", steering 036; the Ledger and
// Timeline looks were dropped). Plus the store's disk budget and each instance's
// retention settings.
import { computed, onMounted, ref } from 'vue'
import { Alert, Badge, Button, Dropdown, Select, TextInput, Tooltip } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import { backups, loadBackups, allSets, freshness, FRESH_DOT, FRESH_TEXT, REASON_LABEL, PART_LABEL, sizeText, whenText } from '../data/backups'
import { instances, setFocus } from '../data/fleet'
import { createJob } from '../data/jobs'
import { openJob } from '../data/job-dialog'
import { openRestoreWizard } from '../data/restore-wizard'
import { openAction } from '../data/dialog'
import { copyOffsite, openReceive, sendSet } from '../data/transfer'
import { openBackupSettings, downloadSet, uploadSet } from '../data/backups'
import { toast } from 'frappe-ui'

const query = ref('')
const project = ref('all')
const reason = ref('all')
onMounted(() => { setFocus(null); loadBackups() })
const data = computed(() => backups.data)
const q = computed(() => query.value.trim().toLowerCase())
const sets = computed(() => allSets.value.filter((s) =>
  (project.value === 'all' || s.project === project.value) &&
  (reason.value === 'all' || s.reason === reason.value) &&
  (!q.value || `${s.site} ${s.project} ${s.id} ${s.note || ''}`.toLowerCase().includes(q.value))))
const projectOptions = computed(() => [{ label: 'Any instance', value: 'all' }, ...[...new Set(allSets.value.map((s) => s.project))].sort().map((p) => ({ label: p, value: p }))])
const reasonOptions = computed(() => [{ label: 'Any reason', value: 'all' }, ...(data.value?.reasons || []).map((r) => ({ label: REASON_LABEL[r] || r, value: r }))])
// sites: every site cwcli knows (from the fleet rows' cached benches) plus any site with a set
const siteRows = computed(() => {
  const rows = new Map()
  for (const s of sets.value) {
    const k = `${s.project}/${s.site}`
    const r = rows.get(k) || { project: s.project, site: s.site, sets: [], size: 0 }
    r.sets.push(s); r.size += s.size; rows.set(k, r)
  }
  return [...rows.values()].map((r) => ({ ...r, fresh: freshness(r.project, r.site) })).sort((a, b) => a.project.localeCompare(b.project) || a.site.localeCompare(b.site))
})
const totals = computed(() => ({ sets: allSets.value.length, size: allSets.value.reduce((n, s) => n + s.size, 0), sites: new Set(allSets.value.map((s) => `${s.project}/${s.site}`)).size, failed: allSets.value.filter((s) => !s.verified).length }))
function backupNow(row) { openJob('backup', { project: row.project, bench: String(row.sets[0]?.bench_index ?? 0), site: row.site, args: { store: true, with_files: true, reason: 'manual' } }) }
function verify(s) { createJob({ verb: 'verify-backup', target: { project: s.project, site: s.site }, args: { set_key: s.key } }) }
function remove(s) { openAction('delete_backup', { project: s.project, site: s.site, set: s }) }
function menu(s) {
  return [{ group: 'Set', options: [
    { label: 'Restore…', icon: 'rotate-ccw', onClick: () => openRestoreWizard(s) },
    { label: 'Verify again', icon: 'shield-check', onClick: () => verify(s) },
    { label: 'Send to another machine…', icon: 'send', onClick: () => sendSet(s) },
    { label: s.offsite ? 'Copy off-site again' : 'Copy off-site (S3)', icon: 'cloud-upload', onClick: () => copyOffsite(s) },
    { label: 'Download (.tar)', icon: 'download', onClick: () => downloadSet(s).catch((e) => toast.error(e.message)) },
    { label: 'Delete', icon: 'trash-2', theme: 'red', onClick: () => remove(s) },
  ] }]
}
const uploadInput = ref(null)
const uploading = ref(false)
async function onUpload(ev) {
  const file = ev.target.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    const data = await uploadSet(file)
    toast.success(`Filed ${(data?.sets || []).join(', ')}`)
  } catch (e) { toast.error(e.message) } finally { uploading.value = false; ev.target.value = '' }
}
</script>

<template>
  <PageHead title="Backups" :count="totals.sets || null">
    <template #badge><Badge v-if="totals.failed" size="lg" theme="red" variant="subtle" :label="`${totals.failed} failed`" /></template>
    <template #actions>
      <Button variant="subtle" icon-left="lucide-settings-2" label="Retention and schedule…" @click="openBackupSettings(project !== 'all' ? project : null)" />
      <Button variant="subtle" icon-left="lucide-upload" label="Upload a set…" :loading="uploading" @click="uploadInput?.click()" />
      <input ref="uploadInput" type="file" accept=".tar,application/x-tar" class="hidden" @change="onUpload" />
      <Button variant="subtle" icon-left="lucide-download" label="Receive…" @click="openReceive()" />
      <Button variant="solid" icon-left="lucide-database-backup" label="Back up a site" @click="openJob('backup', { args: { store: true, with_files: true, reason: 'manual' } })" />
    </template>
  </PageHead>

  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <div class="grid gap-px overflow-hidden rounded-7 border border-outline-gray-1 bg-surface-gray-2 sm:grid-cols-2 lg:grid-cols-4">
      <div class="bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Sites with a backup</div><div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? totals.sites : '…' }}</div><div class="mt-0.5 text-xs text-ink-gray-5">{{ totals.sets }} {{ totals.sets === 1 ? 'set' : 'sets' }}, {{ sizeText(totals.size) }} in the store</div></div>
      <div class="bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Needing attention</div><div class="mt-1 text-2xl font-semibold tabular-nums" :class="siteRows.filter((r) => r.fresh.tone !== 'green').length ? 'text-ink-amber-6' : 'text-ink-gray-9'">{{ data ? siteRows.filter((r) => r.fresh.tone !== 'green').length : '…' }}</div><div class="mt-0.5 text-xs text-ink-gray-5">sites whose last good backup is older than their limit, or missing</div></div>
      <div class="bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Failed verification</div><div class="mt-1 text-2xl font-semibold tabular-nums" :class="totals.failed ? 'text-ink-red-6' : 'text-ink-gray-9'">{{ data ? totals.failed : '…' }}</div><div class="mt-0.5 text-xs text-ink-gray-5">unverified sets are never pruned; they stay until you delete them by hand</div></div>
      <div class="bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Store disk</div><div class="mt-1 text-2xl font-semibold tabular-nums" :class="data?.budget?.warn ? 'text-ink-amber-6' : 'text-ink-gray-9'">{{ data?.budget ? `${data.budget.free_gb} GB free` : '…' }}</div><div class="mt-0.5 truncate text-xs text-ink-gray-5" :title="data?.budget?.root">{{ data?.budget ? `${data.budget.store_gb} GB used · floor ${data.budget.min_free_gb} GB · ${data.budget.root}` : '' }}</div></div>
    </div>
    <Alert v-if="data?.budget?.warn" theme="amber" title="The store's disk is under its floor" :description="`${data.budget.free_gb} GB free, floor ${data.budget.min_free_gb} GB. Only the newest set per site is kept locally until space returns; off-machine copies carry the rest (phase 3).`" />

    <div class="space-y-4">
      <div class="flex flex-wrap items-center gap-2">
        <TextInput v-model="query" class="w-64 max-w-full" type="search" placeholder="Search sites, instances, notes"><template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template></TextInput>
        <Select v-model="project" :options="projectOptions" />
        <Select v-model="reason" :options="reasonOptions" />
        <span class="ml-auto text-sm tabular-nums text-ink-gray-5">{{ sets.length }} of {{ allSets.length }}</span>
      </div>

      <div v-if="backups.loading && !data" class="space-y-2"><div v-for="i in 4" :key="i" class="h-14 animate-pulse rounded-6 bg-surface-gray-1" /></div>
      <EmptyState v-else-if="data && !sets.length" icon="lucide-archive" title="No backup set in the store yet" description="Back up a site here or from its page; removals and core updates add their own sets." />

      <div v-else class="overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-elevation-1">
        <div class="grid grid-cols-[minmax(0,1.4fr)_9rem_6rem_6rem_minmax(0,1.6fr)_8rem] items-center gap-3 border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5 max-lg:hidden"><span>Site</span><span>Last good</span><span>Sets</span><span>Size</span><span>Newest</span><span class="text-right">Actions</span></div>
        <div v-for="r in siteRows" :key="`${r.project}/${r.site}`" class="grid grid-cols-[minmax(0,1.4fr)_9rem_6rem_6rem_minmax(0,1.6fr)_8rem] items-center gap-3 border-b border-outline-gray-1 px-3 py-2.5 text-sm last:border-b-0 max-lg:grid-cols-[minmax(0,1fr)_8rem]">
          <span class="min-w-0"><router-link :to="`/instances/${r.project}/benches/${r.sets[0]?.bench_index ?? 0}/sites/${r.site}?tab=backups`" class="block truncate text-base-medium text-ink-gray-9 hover:underline">{{ r.site }}</router-link><span class="block truncate text-xs text-ink-gray-5">{{ r.project }}<template v-if="r.sets[0]?.bench_index != null"> · bench {{ r.sets[0].bench_index }}</template></span></span>
          <span class="inline-flex items-center gap-1.5 text-sm max-lg:hidden" :class="FRESH_TEXT[r.fresh.tone]"><span class="size-2 rounded-full" :class="FRESH_DOT[r.fresh.tone]" aria-hidden="true" />{{ r.fresh.label }}</span>
          <span class="inline-flex items-center gap-1.5 tabular-nums text-ink-gray-7 max-lg:hidden">{{ r.sets.length }}<Badge v-if="r.sets.some((s) => !s.verified)" theme="red" variant="subtle" :label="`${r.sets.filter((s) => !s.verified).length} unverified`" title="Kept until you delete them by hand; retention never removes them" /></span>
          <span class="tabular-nums text-ink-gray-7 max-lg:hidden">{{ sizeText(r.size) }}</span>
          <span class="flex min-w-0 flex-wrap items-center gap-1 max-lg:hidden"><template v-for="s in r.sets.slice(0, 3)" :key="s.key"><button type="button" class="inline-flex items-center gap-1 rounded-4 border border-outline-gray-1 px-1.5 py-0.5 text-xs hover:bg-surface-gray-1" :class="s.verified ? 'text-ink-gray-7' : 'text-ink-red-6'" :title="`${s.verified ? '' : 'Unverified, kept until deleted by hand · '}${REASON_LABEL[s.reason] || s.reason} · ${s.parts.map((p) => PART_LABEL[p.kind]).join(', ')} · ${sizeText(s.size)}`" @click="openRestoreWizard(s)"><span :class="s.verified ? 'lucide-shield-check' : 'lucide-shield-x'" class="size-3" aria-hidden="true" />{{ whenText(s.created_at) }}<span v-if="s.offsite" class="lucide-cloud size-3 text-ink-gray-4" aria-hidden="true" title="copied off-site" /></button></template><span v-if="r.sets.length > 3" class="text-xs text-ink-gray-4">+{{ r.sets.length - 3 }}</span></span>
          <span class="flex items-center justify-end gap-1"><Button size="sm" variant="subtle" label="Back up" @click="backupNow(r)" /><Dropdown :options="menu(r.sets[0])" align="end"><Button size="sm" variant="ghost" icon="lucide-ellipsis" aria-label="Newest set actions" /></Dropdown></span>
        </div>
      </div>
    </div>
  </div>
</template>
