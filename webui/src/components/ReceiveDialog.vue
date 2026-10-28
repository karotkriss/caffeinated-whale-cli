<script setup>
// Receive a backup set from another machine: paste the sendme ticket; the download is a
// job and every set in it is filed into this machine's store, where Restore takes over.
import { computed, ref, watch } from 'vue'
import { Button, Dialog, ErrorMessage, FormControl, Select } from 'frappe-ui'
import { useRouter } from 'vue-router'
import { receiveDialog } from '../data/transfer'
import { instances } from '../data/fleet'
import { createJob } from '../data/jobs'
import { trackJob } from '../data/job-toasts'

const router = useRouter()
const ticket = ref('')
const project = ref('')
const site = ref('')
const running = ref(false)
const error = ref(null)
watch(() => receiveDialog.open, (open) => { if (open) { ticket.value = ''; project.value = ''; site.value = ''; error.value = null } })
const projectOptions = computed(() => [{ label: 'As filed by the sender', value: '' }, ...instances.value.map((i) => ({ label: i.project, value: i.project }))])
const clean = computed(() => ticket.value.replace(/\s+/g, ''))
const ok = computed(() => /^blob[a-z2-7]{40,}$/.test(clean.value) && (!project.value || !!site.value.trim()))
async function start() {
  running.value = true
  error.value = null
  try {
    const record = await createJob({ verb: 'receive-backup', target: {}, args: { ticket: clean.value, project: project.value || null, site: project.value ? site.value.trim() : null } })
    trackJob(record.id)
    receiveDialog.open = false
    router.push(`/jobs/${record.id}`)
  } catch (e) {
    error.value = e
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Dialog :open="receiveDialog.open" title="Receive a backup from another machine" size="xl" @update:open="receiveDialog.open = $event">
    <div class="space-y-4">
      <FormControl v-model="ticket" type="textarea" label="sendme ticket" placeholder="blob…" :rows="4" autocomplete="off" spellcheck="false" />
      <p class="text-xs text-ink-gray-5">On the other machine: a set's menu › Send to another machine, or <code class="font-mono">cwcli backups send &lt;key&gt;</code>. The sets it carries are filed under the instance and site they came from.</p>
      <div class="grid gap-4 sm:grid-cols-2">
        <Select v-model="project" label="File a single set under (optional)" :options="projectOptions" />
        <FormControl v-model="site" label="…and the site name" placeholder="shop.localhost" :disabled="!project" autocomplete="off" spellcheck="false" />
      </div>
      <ErrorMessage :message="error?.message || error" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button variant="ghost" label="Cancel" @click="close" />
        <Button variant="solid" label="Receive" :disabled="!ok" :loading="running" @click="start" />
      </div>
    </template>
  </Dialog>
</template>
