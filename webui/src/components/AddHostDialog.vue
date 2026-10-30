<script setup>
// Add a remote host: the two-step pairing. 1) the remote's owner runs the allow
// command (with this hub's public key in it) there, which prints a pairing string;
// 2) the Admin pastes it here, reviews the address it names and the host key it
// pins, and adds the host. Nothing is trusted on first contact: without a host key
// the daemon refuses, and a different key later is a hard stop.
import { computed, ref, watch } from 'vue'
import { Alert, Button, Dialog, ErrorMessage, FormControl } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { addHost, addHostDialog, applyHostLink, hosts, linkAdvice, linkLook, loadHosts, readPairing } from '../data/hosts'
import { openSettings } from '../data/settings-dialog'

const d = addHostDialog
const pairing = ref('')
const name = ref('')
const address = ref('')
const port = ref('')
const user = ref('')
const hostKey = ref('')
const busy = ref(false)
const error = ref(null)
const result = ref(null)

const parsed = computed(() => readPairing(pairing.value))
watch(parsed, (p) => {
  if (!p) return
  name.value = String(p.name || '')
  address.value = String(p.address || '')
  port.value = String(p.port || 22)
  user.value = String(p.user || '')
  hostKey.value = String(p.host_key || '')
})
watch(() => d.open, (open) => {
  if (!open) return
  pairing.value = name.value = address.value = port.value = user.value = hostKey.value = ''
  error.value = null
  result.value = null
  loadHosts()
})
const hubKey = computed(() => hosts.hub?.public_key || '')
const allowCommand = computed(() => hosts.hub?.allow_command || '')
const transportOn = computed(() => !!hosts.hub?.transport_on)
const keyText = computed(() => {
  const parts = hostKey.value.trim().split(/\s+/)
  return parts.length >= 2 ? `${parts[0]} ${parts[1].slice(0, 12)}…${parts[1].slice(-8)}` : ''
})
const ready = computed(() => !busy.value && !!address.value.trim() && !!user.value.trim() && !!hostKey.value.trim())
const look = computed(() => linkLook(result.value?.state))

async function submit() {
  busy.value = true
  error.value = null
  try {
    const data = await addHost({
      pairing: parsed.value ? pairing.value.trim() : null,
      name: name.value.trim() || null,
      address: address.value.trim() || null,
      port: port.value.trim() ? Number(port.value) : null,
      user: user.value.trim() || null,
      host_key: hostKey.value.trim() || null,
    })
    result.value = data.host
    if (data.host) applyHostLink(data.host)
    await loadHosts()
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
function toIntegrations() {
  d.open = false
  openSettings('integrations')
}
</script>

<template>
  <Dialog v-model:open="d.open" title="Add a remote host" size="2xl">
    <div v-if="result" class="space-y-4">
      <Alert :theme="look.theme === 'green' ? 'green' : look.theme === 'red' ? 'red' : 'amber'" :title="`${result.name} is ${look.label}`" :description="linkAdvice(result)" />
      <dl class="grid grid-cols-[8rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
        <dt class="text-ink-gray-5">Reached as</dt><dd class="font-mono text-ink-gray-8">{{ result.user }}@{{ result.address }}:{{ result.port }}</dd>
        <dt class="text-ink-gray-5">Host key</dt><dd class="font-mono text-ink-gray-8">{{ result.host_key_fingerprint }}</dd>
        <dt class="text-ink-gray-5">Role cap</dt><dd class="text-ink-gray-8">{{ result.cap || 'not read yet' }}</dd>
        <dt v-if="result.remote" class="text-ink-gray-5">Remote cwcli</dt><dd v-if="result.remote" class="text-ink-gray-8">{{ result.remote.version }} (protocol {{ result.remote.protocol }})</dd>
      </dl>
      <p class="text-p-sm text-ink-gray-5">Its instances appear on the Instances page labelled {{ result.name }}; Settings > Remote hosts shows the link.</p>
    </div>
    <div v-else class="space-y-5">
      <section class="space-y-2">
        <div class="flex items-center gap-2 text-sm font-medium text-ink-gray-8"><span class="flex size-5 items-center justify-center rounded-full bg-surface-gray-2 text-xs">1</span>On the other machine</div>
        <p class="text-p-sm text-ink-gray-6">As the user who runs cwcli there, run this. It restricts this hub's key to cwcli's bridge (no shell, no forwarding), caps what the hub may do at the Operator role, and prints a pairing string.</p>
        <CommandLine v-if="hubKey" :command="allowCommand" />
        <Alert v-else theme="amber" title="This hub has no key yet" description="Turn on Remote hosts over SSH in Settings > Integrations; its test creates the hub's key pair." :primary-action="{ label: 'Open Integrations', onClick: toIntegrations }" />
        <Alert v-if="hubKey && !transportOn" theme="gray" title="The Remote hosts over SSH integration is off" description="The host can be added now; the link connects once the integration is on." />
      </section>
      <section class="space-y-3">
        <div class="flex items-center gap-2 text-sm font-medium text-ink-gray-8"><span class="flex size-5 items-center justify-center rounded-full bg-surface-gray-2 text-xs">2</span>Paste the pairing string here</div>
        <FormControl v-model="pairing" type="textarea" :rows="3" placeholder="cwcli-remote:v1:…" spellcheck="false" autocomplete="off" aria-label="Pairing string" />
        <p v-if="pairing.trim() && !parsed" class="text-p-sm text-ink-red-6">That is not a pairing string. Copy the whole line cwcli remote allow printed.</p>
        <div v-if="parsed || hostKey" class="grid gap-3 sm:grid-cols-2">
          <FormControl v-model="name" label="Label" placeholder="build box" autocomplete="off" />
          <FormControl v-model="user" label="SSH user" autocomplete="off" spellcheck="false" />
          <FormControl v-model="address" label="Address" description="How this hub reaches it; the remote guessed one." autocomplete="off" spellcheck="false" />
          <FormControl v-model="port" label="SSH port" type="number" autocomplete="off" />
          <div class="sm:col-span-2">
            <FormControl v-model="hostKey" label="SSH host key to pin" type="textarea" :rows="2" spellcheck="false" autocomplete="off" :description="keyText ? `Pinned as ${keyText}. A different key later stops the link until an Admin re-pins it.` : 'From /etc/ssh/ssh_host_ed25519_key.pub on the remote; required.'" />
          </div>
        </div>
      </section>
      <ErrorMessage :message="error ? (error.hint ? `${error.message} ${error.hint}` : error.message) : null" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button :label="result ? 'Close' : 'Cancel'" @click="close" />
        <Button v-if="!result" variant="solid" data-primary label="Add host" :loading="busy" :disabled="!ready" @click="submit" />
      </div>
    </template>
  </Dialog>
</template>
