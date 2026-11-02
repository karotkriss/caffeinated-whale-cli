<script setup>
// Set up one integration. The form is built from the integration's own settings
// schema (core/integrations.py Field), so a later integration needs no page of its
// own. Turning one on always tests it first and enables it only when the test
// passes; a secret is never shown again once saved, only replaced.
import { computed, ref, watch } from 'vue'
import { Alert, Badge, Button, Checkbox, Dialog, ErrorMessage, FormControl, FormLabel, Select, TextInput } from 'frappe-ui'
import { configureIntegration, HOOK_POINT, integrationAction, integrationDialog, integrations, loadIntegrations, PUBLISHER, STATE, testIntegration } from '../data/integrations'
import { hostName } from '../data/hosts'

const d = integrationDialog
const integ = computed(() => integrations.list.find((i) => i.id === d.id) || null)
// The machine this form sets up: a remote host's own integration when the panel's
// picker names one (every request goes to that host through the hub).
const machine = computed(() => (integrations.host ? hostName(integrations.host) : 'this machine'))
const form = ref({})
const secrets = ref({})
const check = ref(null)
const error = ref(null)
const busy = ref(null) // 'test' | 'save' | 'lookup' | 'detect' | 'edit'
// Another integration serves this one's hook point: the server's detail, until the Admin chooses to switch.
const switching = ref(null)
const lookupName = ref('')
const lookup = ref(null)
const advanced = ref(false)
// Cloudflare: the address Detect read, and the last Check edit access answer.
const detectedIp = ref(null)
const editAccess = ref(null)
// Nginx Proxy Manager: the container Detect found on this machine, if any.
const detected = ref(null)

const ROLES = [{ label: 'Admin', value: 'admin' }, { label: 'Operator', value: 'operator' }, { label: 'Viewer', value: 'viewer' }]
// The directory's own defaults, shown as placeholders until a value is typed
// (core/directory.py uses the same ones for a field left empty).
const LDAP_DEFAULTS = {
  'Active Directory': { user_filter: '(sAMAccountName={username})', username_attribute: 'sAMAccountName', email_attribute: 'mail', display_name_attribute: 'displayName' },
  OpenLDAP: { user_filter: '(uid={username})', username_attribute: 'uid', email_attribute: 'mail', display_name_attribute: 'cn' },
}
const ADVANCED = new Set(['user_filter', 'username_attribute', 'email_attribute', 'display_name_attribute'])
// Long values (URLs, DNs, filters) take the whole row.
const WIDE = new Set(['server_url', 'bind_dn', 'user_filter', 'endpoint', 'api_token', 'ipv6', 'challenge', 'dns_token'])
// Short switches that sit two to a row.
const HALF_BOOL = new Set(['ssl_forced', 'http2', 'block_exploits', 'hsts'])
const DNS_CHALLENGE = 'DNS through Cloudflare'

watch(() => d.open, (open) => {
  if (!open || !integ.value) return
  const values = {}
  for (const f of integ.value.fields) {
    if (f.kind === 'secret') continue
    const saved = integ.value.settings?.[f.key]
    if (f.kind === 'group_roles') values[f.key] = (saved || []).map((r) => ({ ...r }))
    else if (f.kind === 'bool') values[f.key] = saved ?? !!f.default
    else values[f.key] = saved ?? f.default ?? ''
  }
  if (integ.value.id === 'ldap' && !(values.group_roles || []).length) values.group_roles = [{ group: '', role: 'admin' }]
  form.value = values
  secrets.value = {}
  check.value = null
  error.value = null
  switching.value = null
  lookup.value = null
  lookupName.value = ''
  advanced.value = false
  detectedIp.value = null
  editAccess.value = null
  detected.value = null
  if (integ.value.id === 'npm') runDetect()
})

// A test answer describes the form it was run on; editing the form retires it.
watch([form, secrets], () => { check.value = null }, { deep: true })

const visible = computed(() => (integ.value?.fields || []).filter((f) => {
  if (f.key === 'starttls') return String(form.value.server_url || '').trim().toLowerCase().startsWith('ldap://')
  if (f.key === 'dns_token') return form.value.challenge === DNS_CHALLENGE
  return !ADVANCED.has(f.key) || advanced.value
}))
const hasAdvanced = computed(() => (integ.value?.fields || []).some((f) => ADVANCED.has(f.key)))
const placeholder = (f) => (integ.value?.id === 'ldap' && LDAP_DEFAULTS[form.value.directory_type]?.[f.key]) || f.placeholder
const secretPlaceholder = (f) => (integ.value?.secrets_set?.[f.key] ? 'Stored · type a new one to replace it' : '')
// What the request carries: the form, with the group rows that name nothing dropped.
function payload() {
  const settings = { ...form.value }
  if (settings.group_roles) settings.group_roles = settings.group_roles.filter((r) => r.group.trim())
  const typed = Object.fromEntries(Object.entries(secrets.value).filter(([, v]) => v))
  return [settings, typed]
}

async function runTest() {
  busy.value = 'test'
  error.value = null
  try { check.value = (await testIntegration(d.id, ...payload())).check } catch (e) { error.value = e; check.value = null } finally { busy.value = null }
}
async function save(takeOver = false) {
  busy.value = 'save'
  error.value = null
  try {
    const data = await configureIntegration(d.id, ...payload(), true, takeOver)
    check.value = data.check
    switching.value = null
    await loadIntegrations()
    if (data.integration.enabled && data.check?.ok !== false) { secrets.value = {}; d.open = false }
  } catch (e) {
    if (e.code === 'integration.hook_taken' && e.detail) switching.value = e.detail
    else error.value = e
  } finally { busy.value = null }
}
async function runLookup() {
  if (!lookupName.value.trim()) return
  busy.value = 'lookup'
  error.value = null
  try { lookup.value = (await integrationAction(d.id, 'lookup_user', { username: lookupName.value.trim() }, ...payload())).result } catch (e) { error.value = e; lookup.value = null } finally { busy.value = null }
}
const ROLE_LABEL = { admin: 'Admin', operator: 'Operator', viewer: 'Viewer' }

async function detectIp() {
  busy.value = 'detect'
  error.value = null
  try {
    detectedIp.value = (await integrationAction(d.id, 'detect_ip', {}, ...payload())).result.ipv4
    form.value.ipv4 = detectedIp.value
  } catch (e) { error.value = e } finally { busy.value = null }
}
async function runEditCheck() {
  busy.value = 'edit'
  error.value = null
  try { editAccess.value = (await integrationAction(d.id, 'check_edit_access', {}, ...payload())).result } catch (e) { error.value = e; editAccess.value = null } finally { busy.value = null }
}
// A local Nginx Proxy Manager container fills the admin URL and forward address the
// Admin has not set yet; nothing is saved until Test and turn on.
async function runDetect() {
  try {
    const found = (await integrationAction('npm', 'detect', {}, ...payload())).result
    detected.value = found
    if (!found.found) return
    if (!integ.value?.settings?.url && found.url) form.value.url = found.url
    if (!form.value.forward_host && found.forward_host) form.value.forward_host = found.forward_host
  } catch { detected.value = null }
}
const checkedAt = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
</script>

<template>
  <Dialog v-model:open="d.open" :title="integ ? `${integ.enabled ? '' : 'Turn on '}${integ.name}${integrations.host ? ` on ${machine}` : ''}` : 'Integration'" size="2xl">
    <div v-if="integ" class="space-y-4">
      <div class="flex flex-wrap items-center gap-2">
        <Badge size="sm" variant="subtle" :theme="PUBLISHER[integ.publisher]?.theme" :label="PUBLISHER[integ.publisher]?.label" />
        <Badge size="sm" variant="subtle" :theme="STATE[integ.state]?.theme" :label="STATE[integ.state]?.label" />
        <p class="min-w-0 flex-1 text-p-sm text-ink-gray-6">{{ integ.description }}</p>
      </div>
      <p v-if="integrations.host" class="text-p-sm text-ink-gray-6">Set up on {{ machine }} through this hub: the test, Detect and the save all run there, and the secrets you type are stored encrypted on {{ machine }}, never on this hub.</p>
      <Alert v-if="detected?.found" theme="blue" :title="`Found Nginx Proxy Manager${detected.version ? ' ' + detected.version : ''} on ${machine}`" :description="`Container ${detected.container}, admin port ${detected.admin_port ?? 'not published'}. The admin URL and forward address are filled from it; cwcli talks to it over 127.0.0.1 there, so its admin port can stay closed to the network.`" />
      <form class="grid gap-3 sm:grid-cols-2" autocomplete="off" @submit.prevent="save(false)">
        <template v-for="f in visible" :key="f.key">
          <div v-if="f.kind === 'group_roles'" class="space-y-2 sm:col-span-2">
            <FormLabel :label="f.label" />
            <div v-for="(row, i) in form[f.key]" :key="i" class="flex items-center gap-2">
              <FormControl v-model="row.group" class="min-w-0 flex-1" placeholder="cwcli-admins" :aria-label="`Group ${i + 1}`" spellcheck="false" />
              <Select v-model="row.role" class="w-32" :options="ROLES" :aria-label="`Role for group ${i + 1}`" />
              <Button variant="ghost" icon="lucide-x" :aria-label="`Remove group ${i + 1}`" @click="form[f.key].splice(i, 1)" />
            </div>
            <div class="flex items-center gap-3">
              <Button size="sm" icon-left="lucide-plus" label="Add a group" @click="form[f.key].push({ group: '', role: 'viewer' })" />
              <p class="text-p-sm text-ink-gray-5">{{ f.help }}</p>
            </div>
          </div>
          <div v-else-if="f.key === 'ipv4'" class="space-y-1.5 sm:col-span-2">
            <FormLabel :label="f.label" />
            <div class="flex items-center gap-2">
              <TextInput v-model="form.ipv4" class="min-w-0 flex-1" :placeholder="f.placeholder" :aria-label="f.label" spellcheck="false" />
              <Button icon-left="lucide-radar" label="Detect" :loading="busy === 'detect'" @click="detectIp" />
            </div>
            <p class="text-p-sm" :class="detectedIp && detectedIp === form.ipv4 ? 'text-ink-amber-6' : 'text-ink-gray-5'">{{ detectedIp && detectedIp === form.ipv4 ? `Cloudflare sees ${machine} as ${detectedIp}. Behind NAT, a VPN or two uplinks that may not be the address visitors reach; saving confirms it.` : f.help }}</p>
          </div>
          <div v-else-if="f.kind === 'bool'" :class="HALF_BOOL.has(f.key) ? '' : 'sm:col-span-2'">
            <Checkbox v-model="form[f.key]" :label="f.label" />
            <p v-if="f.help" class="mt-1 pl-6 text-p-sm text-ink-gray-5">{{ f.help }}</p>
          </div>
          <FormControl v-else-if="f.kind === 'select'" v-model="form[f.key]" :class="WIDE.has(f.key) && 'sm:col-span-2'" type="select" :label="f.label" :options="f.options" :description="f.help" />
          <FormControl v-else-if="f.kind === 'pem'" v-model="form[f.key]" class="sm:col-span-2" type="textarea" :rows="3" :label="f.label" :description="f.help" placeholder="-----BEGIN CERTIFICATE-----" spellcheck="false" />
          <FormControl v-else-if="f.kind === 'secret'" v-model="secrets[f.key]" :class="WIDE.has(f.key) && 'sm:col-span-2'" type="password" :label="f.label" :placeholder="secretPlaceholder(f)" :description="f.help" autocomplete="new-password" />
          <FormControl v-else v-model="form[f.key]" :class="WIDE.has(f.key) && 'sm:col-span-2'" :label="f.label" :placeholder="placeholder(f)" :description="f.help" spellcheck="false" />
        </template>
        <button v-if="hasAdvanced" type="button" class="flex items-center gap-1 text-sm text-ink-gray-6 hover:text-ink-gray-8 sm:col-span-2" :aria-expanded="advanced" @click="advanced = !advanced">
          <span class="size-4" :class="advanced ? 'lucide-chevron-down' : 'lucide-chevron-right'" aria-hidden="true" />Search filter and attributes
        </button>
        <button type="submit" class="hidden" />
      </form>

      <div v-if="integ.actions.includes('lookup_user')" class="rounded-6 border border-outline-gray-1 p-3">
        <div class="text-base font-medium text-ink-gray-8">Look up a user</div>
        <p class="mt-0.5 text-p-sm text-ink-gray-5">What sign-in would decide for this name with the settings above: no password is asked or checked.</p>
        <form class="mt-2 flex items-center gap-2" @submit.prevent="runLookup">
          <FormControl v-model="lookupName" class="w-56" placeholder="username" aria-label="Username to look up" autocomplete="off" spellcheck="false" />
          <Button type="submit" label="Look up" icon-left="lucide-search" :disabled="!lookupName.trim()" :loading="busy === 'lookup'" />
        </form>
        <div v-if="lookup" class="mt-3 space-y-1 text-p-sm">
          <p v-if="!lookup.found" class="text-ink-amber-6">No directory entry matches {{ lookup.username }}.</p>
          <template v-else>
            <p class="flex flex-wrap items-center gap-2 text-ink-gray-8">
              <span class="font-medium">{{ lookup.name || lookup.username }}</span>
              <span class="text-ink-gray-5">{{ lookup.email }}</span>
              <Badge v-if="lookup.allowed" size="sm" theme="green" :label="`Signs in as ${ROLE_LABEL[lookup.role]}`" />
              <Badge v-else size="sm" theme="red" label="Refused" />
            </p>
            <p class="truncate font-mono text-xs text-ink-gray-5" :title="lookup.dn">{{ lookup.dn }}</p>
            <p v-if="lookup.reason" class="text-ink-red-6">{{ lookup.reason }}</p>
            <p class="text-ink-gray-6">Groups: {{ lookup.groups.length ? lookup.groups.map((g) => g.cn).join(', ') : 'none' }}<span v-if="lookup.matched_groups.length"> · allowed through {{ lookup.matched_groups.join(', ') }}</span></p>
          </template>
        </div>
      </div>

      <div v-if="integ.actions.includes('check_edit_access')" class="rounded-6 border border-outline-gray-1 p-3">
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="text-base font-medium text-ink-gray-8">Check edit access</div>
            <p class="mt-0.5 text-p-sm text-ink-gray-5">Cloudflare has no read-only way to prove a token may edit a zone, so this creates and deletes a TXT record named <span class="font-mono">_cwcli-check</span> in each zone the token can see.</p>
          </div>
          <Button label="Check" icon-left="lucide-shield-check" :loading="busy === 'edit'" @click="runEditCheck" />
        </div>
        <ul v-if="editAccess" class="mt-2 space-y-1 text-p-sm">
          <li v-for="z in editAccess.zones" :key="z.zone" class="flex items-start gap-1.5" :class="z.ok ? 'text-ink-green-6' : 'text-ink-red-6'">
            <span class="mt-0.5 size-4 shrink-0" :class="z.ok ? 'lucide-circle-check' : 'lucide-circle-x'" aria-hidden="true" /><span><span class="font-medium">{{ z.zone }}</span>: {{ z.ok ? 'cwcli can create and delete records' : z.message }}</span>
          </li>
          <li v-if="!editAccess.zones.length" class="text-ink-amber-6">The token can see no zone.</li>
          <li class="text-xs text-ink-gray-5">Checked at {{ checkedAt(editAccess.checked_at) }}</li>
        </ul>
      </div>

      <p v-if="check" class="flex items-start gap-1.5 text-p-sm" :class="check.ok ? 'text-ink-green-6' : 'text-ink-red-6'">
        <span class="mt-0.5 size-4 shrink-0" :class="check.ok ? 'lucide-circle-check' : 'lucide-circle-x'" aria-hidden="true" />{{ check.message }}
      </p>
      <Alert v-if="switching" theme="amber" :title="`${switching.current_name} is the ${HOOK_POINT[switching.hook]?.toLowerCase() || switching.hook} now`" :description="`Only one integration serves it at a time. Switching tests ${integ.name} first; if the test passes, ${integ.name} turns on and ${switching.current_name} turns off, keeping its settings.`" />
      <ErrorMessage :message="error ? (error.hint ? `${error.message} ${error.hint}` : error.message) : null" />
    </div>
    <template #actions="{ close }">
      <div class="flex flex-wrap justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button label="Test connection" icon-left="lucide-plug-zap" :loading="busy === 'test'" @click="runTest" />
        <Button v-if="switching" variant="solid" data-primary :label="`Test and switch to ${integ?.name}`" :loading="busy === 'save'" @click="save(true)" />
        <Button v-else variant="solid" data-primary :label="integ?.enabled ? 'Test and save' : 'Test and turn on'" :loading="busy === 'save'" @click="save()" />
      </div>
    </template>
  </Dialog>
</template>
