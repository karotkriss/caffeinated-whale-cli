<script setup>
// One site's backups (phase 1 of the Backups spec): the last-good badge, every set
// in the store for this site with date, contents, size, reason and verdict, Back up
// now, and per set Restore / Verify / Delete. Restore opens the wizard.
import { computed, onMounted } from 'vue'
import { Button, Dropdown, Tooltip } from 'frappe-ui'
import { backups, loadBackups, setsFor, freshness, FRESH_DOT, FRESH_TEXT, REASON_LABEL, PART_LABEL, sizeText, whenText } from '../data/backups'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'
import { openJob } from '../data/job-dialog'
import { openRestoreWizard } from '../data/restore-wizard'
import { sendSet } from '../data/transfer'
import { downloadSet, lastDrill } from '../data/backups'
import { toast } from 'frappe-ui'
import { useRouter } from 'vue-router'
import { openAction } from '../data/dialog'

const props = defineProps({
  project: { type: String, required: true },
  bench: { type: String, required: true },
  benchPath: { type: String, required: true },
  site: { type: String, required: true },
  running: { type: Boolean, default: false },
  compact: { type: Boolean, default: false },
})
onMounted(() => { if (!backups.data) loadBackups() })
const sets = computed(() => setsFor(props.project, props.site))
const fresh = computed(() => freshness(props.project, props.site))
const drill = computed(() => lastDrill(props.project, props.site))
async function runDrill() {
  const record = await createJob({ verb: 'restore-drill', target: { project: props.project, bench: props.bench, site: props.site }, args: {} })
  trackJob(record.id)
  router.push(`/jobs/${record.id}`)
}
function backupNow() {
  openJob('backup', { project: props.project, bench: props.bench, site: props.site, args: { store: true, with_files: true, reason: 'manual' } })
}
function verify(s) {
  createJob({ verb: 'verify-backup', target: { project: s.project, site: s.site }, args: { set_key: s.key } })
}
function remove(s) {
  openAction({ title: `Delete backup ${s.id}?`, description: `${s.site} on ${s.project}, ${sizeText(s.size)}, ${REASON_LABEL[s.reason] || s.reason}. A site's last good backup is never deleted.`, confirmLabel: 'Delete', theme: 'red', run: () => createJob({ verb: 'delete-backup', target: { project: s.project, site: s.site }, args: { set_key: s.key }, consent: true }) })
}
function menu(s) {
  return [{ group: 'Set', options: [
    { label: 'Restore…', icon: 'rotate-ccw', onClick: () => openRestoreWizard(s) },
    { label: 'Verify again', icon: 'shield-check', onClick: () => verify(s) },
    { label: 'Send to another machine…', icon: 'send', onClick: () => sendSet(s, router) },
    { label: s.offsite ? 'Copy off-site again' : 'Copy off-site (S3)', icon: 'cloud-upload', onClick: () => copyOffsite(s) },
    { label: 'Download (.tar)', icon: 'download', onClick: () => downloadSet(s).catch((e) => toast.error(e.message)) },
    { label: 'Delete', icon: 'trash-2', theme: 'red', onClick: () => remove(s) },
  ] }]
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-3">
      <span class="inline-flex items-center gap-1.5 text-sm" :class="FRESH_TEXT[fresh.tone]"><span class="size-2 rounded-full" :class="FRESH_DOT[fresh.tone]" aria-hidden="true" />{{ fresh.label }}</span>
      <span v-if="drill" class="text-xs" :class="drill.ok ? 'text-ink-gray-5' : 'text-ink-red-6'" :title="drill.detail">· last restore drill {{ drill.ok ? 'passed' : 'FAILED' }} {{ whenText(drill.at) }}</span>
      <span v-if="sets.length" class="text-xs text-ink-gray-5">{{ sets.length }} {{ sets.length === 1 ? 'set' : 'sets' }} in the store · {{ sizeText(sets.reduce((n, s) => n + s.size, 0)) }}</span>
      <span v-else class="text-xs text-ink-gray-5">no set in the store yet</span>
      <span class="ml-auto flex items-center gap-2">
        <Button size="sm" variant="solid" icon-left="lucide-database-backup" label="Back up now" @click="backupNow" />
      <Button variant="subtle" icon-left="lucide-flask-conical" label="Restore drill" @click="runDrill" />
      </span>
    </div>
    <div v-if="sets.length" class="overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-elevation-1">
      <div class="grid grid-cols-[minmax(0,1.2fr)_minmax(0,1.6fr)_6rem_minmax(0,1fr)_6rem_2.5rem] items-center gap-3 border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5 max-md:hidden"><span>When</span><span>Contents</span><span>Size</span><span>Reason</span><span>Verified</span><span /></div>
      <div v-for="s in sets" :key="s.key" class="grid grid-cols-[minmax(0,1.2fr)_minmax(0,1.6fr)_6rem_minmax(0,1fr)_6rem_2.5rem] items-center gap-3 border-b border-outline-gray-1 px-3 py-2 text-sm last:border-b-0 max-md:grid-cols-[minmax(0,1fr)_2.5rem]">
        <span class="min-w-0"><span class="block truncate text-ink-gray-9">{{ whenText(s.created_at) }}</span><span class="block truncate font-mono text-xs text-ink-gray-4">{{ s.created_at.replace('T', ' ').replace('Z', ' UTC') }}</span></span>
        <!-- Captain note 52: contents as plain text, not a row of pills. -->
        <span class="truncate text-xs text-ink-gray-6 max-md:hidden">{{ s.parts.map((p) => PART_LABEL[p.kind] || p.kind).join(' · ') }}</span>
        <span class="tabular-nums text-ink-gray-7 max-md:hidden">{{ sizeText(s.size) }}</span>
        <span class="truncate text-ink-gray-7 max-md:hidden" :title="s.note || ''">{{ REASON_LABEL[s.reason] || s.reason }}<span v-if="s.note" class="text-ink-gray-4"> · {{ s.note }}</span></span>
        <span class="max-md:hidden"><Tooltip :text="s.checks.map((c) => `${c.name}: ${c.ok ? 'ok' : 'FAILED'} (${c.detail})`).join('\n')"><span class="inline-flex items-center gap-1 text-xs" :class="s.verified ? 'text-ink-green-6' : 'text-ink-red-6'"><span :class="s.verified ? 'lucide-shield-check' : 'lucide-shield-x'" class="size-3.5" aria-hidden="true" />{{ s.verified ? 'pass' : 'FAIL' }}</span></Tooltip></span>
        <Dropdown :options="menu(s)" align="end"><Button size="sm" variant="ghost" icon="lucide-ellipsis" aria-label="Set actions" /></Dropdown>
      </div>
    </div>
  </div>
</template>
