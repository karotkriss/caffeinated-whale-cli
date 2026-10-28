<script setup>
// Per-instance backup settings (captain decision 4: every value is a setting) and the
// retention preview: what the policy keeps and removes right now, with a prune button.
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl, Select } from 'frappe-ui'
import { useRouter } from 'vue-router'
import { backupSettingsDialog, loadRetention, saveBackupSettings, configureS3, loadOffsite, sizeText, whenText, REASON_LABEL } from '../data/backups'
import { instances } from '../data/fleet'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'

const router = useRouter()
const d = backupSettingsDialog
const project = ref(null)
const form = ref({})
const plan = ref(null)
const saving = ref(false)
const pruning = ref(false)
const error = ref(null)
// Off-site copies: the S3 target (the secret is sent once and stored on the daemon's
// machine, never echoed back) and the bucket's sets with Fetch for the ones not local.
const s3 = ref({ enabled: false, endpoint: '', bucket: '', prefix: 'cwcli-backups', region: 'us-east-1', access_key: '', copy_on_backup: true })
const s3Secret = ref('')
const s3Saving = ref(false)
const s3Probe = ref(null)
const offsite = ref(null)
const offsiteBusy = ref(false)
const projectOptions = computed(() => instances.value.map((i) => ({ label: i.project, value: i.project })))
function fill(settings) {
  const s = settings || {}
  form.value = {
    daily: s.local?.daily ?? 7, weekly: s.local?.weekly ?? 4, monthly: s.local?.monthly ?? 0,
    s3_daily: s.s3?.daily ?? 7, s3_weekly: s.s3?.weekly ?? 4, s3_monthly: s.s3?.monthly ?? 12,
    safety_days: s.safety?.days ?? 14, safety_max: s.safety?.max_per_site ?? 5,
    min_free_gb: s.min_free_gb ?? 20, stale_after_days: s.stale_after_days ?? 7, with_files: s.with_files ?? true,
    schedule_enabled: !!s.schedule?.enabled, daily_at: s.schedule?.daily_at || '02:00', last_run: s.schedule?.last_run || null,
  }
}
async function refresh() {
  if (!project.value) return
  error.value = null
  try {
    plan.value = await loadRetention(project.value)
    fill(plan.value.settings)
    const t = plan.value.settings?.s3_target || {}
    s3.value = { enabled: !!t.enabled, endpoint: t.endpoint || '', bucket: t.bucket || '', prefix: t.prefix ?? 'cwcli-backups', region: t.region || 'us-east-1', access_key: t.access_key || '', copy_on_backup: t.copy_on_backup !== false }
    s3Secret.value = ''
    s3Probe.value = null
    offsite.value = null
    if (t.enabled) loadOffsiteList()
  } catch (e) { error.value = e }
}
async function loadOffsiteList() {
  offsiteBusy.value = true
  try { offsite.value = await loadOffsite(project.value) } catch (e) { offsite.value = { error: e.message || String(e), sets: [] } } finally { offsiteBusy.value = false }
}
async function saveS3() {
  s3Saving.value = true
  error.value = null
  try {
    const data = await configureS3(project.value, s3.value, s3Secret.value)
    s3Secret.value = ''
    s3Probe.value = data.probe
    if (data.probe?.ok) loadOffsiteList()
  } catch (e) { error.value = e } finally { s3Saving.value = false }
}
async function fetchOffsite(key) {
  try {
    const record = await createJob({ verb: 's3-fetch', target: { project: project.value }, args: { set_keys: [key] } })
    trackJob(record.id)
    d.open = false
    router.push(`/jobs/${record.id}`)
  } catch (e) { error.value = e }
}
async function pruneOffsite() {
  try {
    const record = await createJob({ verb: 's3-prune', target: { project: project.value }, args: {}, consent: true })
    trackJob(record.id)
    d.open = false
    router.push(`/jobs/${record.id}`)
  } catch (e) { error.value = e }
}
watch(() => d.open, (open) => { if (open) { project.value = d.project || instances.value[0]?.project || null; plan.value = null; refresh() } })
watch(project, () => { if (d.open) refresh() })
const removing = computed(() => (plan.value?.decisions || []).filter((x) => !x.keep))
const keeping = computed(() => (plan.value?.decisions || []).filter((x) => x.keep))
const num = (v, lo = 0) => Math.max(lo, parseInt(v, 10) || 0)
async function save() {
  saving.value = true
  error.value = null
  try {
    const f = form.value
    await saveBackupSettings(project.value, {
      local: { daily: num(f.daily), weekly: num(f.weekly), monthly: num(f.monthly) },
      s3: { daily: num(f.s3_daily), weekly: num(f.s3_weekly), monthly: num(f.s3_monthly) },
      safety: { days: num(f.safety_days), max_per_site: num(f.safety_max) },
      min_free_gb: Math.max(0, parseFloat(f.min_free_gb) || 0), stale_after_days: num(f.stale_after_days, 1), with_files: !!f.with_files,
      schedule: { enabled: !!f.schedule_enabled, daily_at: /^[0-2]\d:[0-5]\d$/.test(f.daily_at) ? f.daily_at : '02:00' },
    })
    await refresh()
  } catch (e) { error.value = e } finally { saving.value = false }
}
async function prune() {
  pruning.value = true
  try {
    const record = await createJob({ verb: 'prune-backups', target: { project: project.value }, args: {}, consent: true })
    trackJob(record.id)
    d.open = false
    router.push(`/jobs/${record.id}`)
  } catch (e) { error.value = e } finally { pruning.value = false }
}
</script>

<template>
  <Dialog :open="d.open" title="Backup retention and schedule" size="2xl" @update:open="d.open = $event">
    <div class="space-y-4">
      <Select v-model="project" label="Instance" :options="projectOptions" />
      <div class="grid gap-3 sm:grid-cols-3">
        <FormControl v-model="form.daily" type="number" label="Keep daily (local)" />
        <FormControl v-model="form.weekly" type="number" label="Keep weekly (local)" />
        <FormControl v-model="form.monthly" type="number" label="Keep monthly (local)" />
        <FormControl v-model="form.s3_daily" type="number" label="Keep daily (S3)" />
        <FormControl v-model="form.s3_weekly" type="number" label="Keep weekly (S3)" />
        <FormControl v-model="form.s3_monthly" type="number" label="Keep monthly (S3)" />
        <FormControl v-model="form.safety_days" type="number" label="Safety backups: days" />
        <FormControl v-model="form.safety_max" type="number" label="Safety backups: max per site" />
        <FormControl v-model="form.min_free_gb" type="number" label="Disk floor (GB free)" />
        <FormControl v-model="form.stale_after_days" type="number" label="Last good turns amber after (days)" />
      </div>
      <div class="flex flex-wrap items-end gap-4 rounded-6 border border-outline-gray-1 p-3">
        <Checkbox v-model="form.schedule_enabled" label="Back up every site daily, then prune" />
        <FormControl v-model="form.daily_at" class="w-28" label="At (local time)" placeholder="02:00" />
        <Checkbox v-model="form.with_files" label="Include files" />
        <span v-if="form.last_run" class="text-xs text-ink-gray-5">last run {{ form.last_run.slice(0, 16).replace('T', ' ') }}</span>
      </div>
      <p class="text-xs text-ink-gray-5">A site's last good backup is never deleted. Safety backups (before a core update, migrate, install or removal) follow their own window. Under the disk floor only the newest copy of a set that is also off-site stays local.</p>
      <div class="flex justify-end"><Button variant="solid" label="Save settings" :loading="saving" @click="save" /></div>
      <div v-if="plan" class="rounded-6 border border-outline-gray-1">
        <div class="flex items-center justify-between border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5"><span>Right now the policy keeps {{ keeping.length }} and would remove {{ removing.length }} ({{ sizeText(removing.reduce((n, x) => n + x.size, 0)) }})</span><Button v-if="removing.length" size="sm" theme="red" variant="subtle" :label="`Prune ${removing.length} now`" :loading="pruning" @click="prune" /></div>
        <div class="max-h-60 overflow-auto divide-y divide-outline-gray-1">
          <div v-for="x in removing" :key="x.key" class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-1.5 text-xs"><span class="truncate"><span class="font-mono text-ink-gray-8">{{ x.key }}</span><span class="ml-2 text-ink-red-6">remove · {{ x.why }}</span></span><span class="tabular-nums text-ink-gray-5">{{ sizeText(x.size) }}</span></div>
          <div v-for="x in keeping" :key="x.key" class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-1.5 text-xs"><span class="truncate"><span class="font-mono text-ink-gray-8">{{ x.key }}</span><span class="ml-2 text-ink-gray-5">keep · {{ x.why }}</span></span><span class="tabular-nums text-ink-gray-5">{{ sizeText(x.size) }}</span></div>
        </div>
      </div>
      <div class="rounded-6 border border-outline-gray-1 p-3">
        <div class="flex items-center gap-3"><h3 class="text-base font-medium text-ink-gray-8">Off-site copies (S3)</h3><Checkbox v-model="s3.enabled" label="Enabled" /><Checkbox v-model="s3.copy_on_backup" label="Copy every new backup" /></div>
        <div class="mt-3 grid gap-3 sm:grid-cols-2">
          <FormControl v-model="s3.endpoint" label="Endpoint" placeholder="https://s3.eu-west-1.amazonaws.com or http://minio:9000" autocomplete="off" />
          <FormControl v-model="s3.bucket" label="Bucket" autocomplete="off" />
          <FormControl v-model="s3.prefix" label="Prefix" autocomplete="off" />
          <FormControl v-model="s3.region" label="Region" autocomplete="off" />
          <FormControl v-model="s3.access_key" label="Access key" autocomplete="off" />
          <FormControl v-model="s3Secret" type="password" label="Secret key" :placeholder="s3.access_key ? 'unchanged unless typed' : ''" autocomplete="new-password" />
        </div>
        <p class="mt-2 text-xs text-ink-gray-5">Copies are encrypted on this machine before upload (AES-256-GCM) under a key kept in cwcli's home; export it with <code class="font-mono">cwcli backups key export</code> to restore them elsewhere. The secret key is stored on this machine only and never shown again.</p>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <Button variant="solid" label="Save and test the bucket" :loading="s3Saving" @click="saveS3" />
          <span v-if="s3Probe" class="text-xs" :class="s3Probe.ok ? 'text-ink-green-6' : 'text-ink-red-6'">{{ s3Probe.ok ? `bucket answers · ${s3Probe.objects} object(s) under the prefix` : s3Probe.error }}</span>
        </div>
        <div v-if="s3.enabled" class="mt-3 rounded-6 border border-outline-gray-1">
          <div class="flex items-center justify-between border-b border-outline-gray-1 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5"><span>In the bucket{{ offsite?.sets ? `: ${offsite.sets.length} set${offsite.sets.length === 1 ? '' : 's'}` : '' }}</span><span class="flex gap-1"><Button size="sm" variant="ghost" label="Refresh" :loading="offsiteBusy" @click="loadOffsiteList" /><Button size="sm" variant="ghost" label="Prune off-site now" @click="pruneOffsite" /></span></div>
          <p v-if="offsite?.error" class="px-3 py-2 text-xs text-ink-red-6">{{ offsite.error }}</p>
          <div v-else class="max-h-48 divide-y divide-outline-gray-1 overflow-auto">
            <div v-for="x in offsite?.sets || []" :key="x.key" class="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2 px-3 py-1.5 text-xs"><span class="truncate"><span class="font-mono text-ink-gray-8">{{ x.key }}</span><span class="ml-2 text-ink-gray-5">{{ whenText(x.created_at) }} · {{ REASON_LABEL[x.reason] || x.reason }}</span></span><span class="text-ink-gray-5">{{ x.local ? 'also local' : 'off-site only' }}</span><Button v-if="!x.local" size="sm" variant="subtle" label="Fetch" @click="fetchOffsite(x.key)" /><span v-else /></div>
            <p v-if="offsite && !offsite.sets.length" class="px-3 py-2 text-xs text-ink-gray-5">Nothing copied yet.</p>
          </div>
        </div>
      </div>
      <ErrorMessage :message="error?.message || error" />
    </div>
  </Dialog>
</template>
