<script setup>
// Direction A: Pilot's Settings. A modal over the page with a section nav on the
// left and rows of title, one-line description and a control on the right. The
// route stays deep-linkable (/settings/:section) and closing returns to where
// you were. Every write still goes through applySetting -> POST /api/v1/settings,
// and each row keeps its terminal twin as a compact copy affordance.
import { computed, ref, watch } from 'vue'
import AppearanceControls from '../premium/AppearanceControls.vue'
import { Alert, Badge, Button, Checkbox, Dialog, Dropdown, FormControl, Select, Switch, LoadingText, ErrorMessage, SettingsDialog, SettingsSidebar, SettingsNavGroup, SettingsNavItem, SettingsContent, SettingsPanel, SettingsHeader, SettingsBody, SettingsRow } from 'frappe-ui'
import CommandLine from '../components/CommandLine.vue'
import CommandCopy from '../components/CommandCopy.vue'
import DoctorView from '../components/DoctorView.vue'
import { config, cacheProjects } from '../data/fleet'
import { settingsDialog, ADMIN_SECTIONS } from '../data/settings-dialog'
import { session, signOut, listSessions, can, ROLE_LABEL } from '../data/session'
import PasswordForm from './PasswordForm.vue'
import IntegrationDialog from './IntegrationDialog.vue'
import CommunityDialog from './CommunityDialog.vue'
import { integrations, loadIntegrations, disableIntegration, openIntegration, openCommunity, isDirectorySource, pickIntegrationsHost, STATE, PUBLISHER } from '../data/integrations'
import { openJob } from '../data/job-dialog'
import { applySetting, settings } from '../data/settings'
import { catalog, loadCatalog, scanCatalog, sources, hostLabel } from '../data/catalog'
import { ago } from '../data/fleet'
import { hosts, hostList, hasHosts, loadHosts, checkHost, removeHost, repinHost, applyHostLink, dropHost, openAddHost, linkLook, linkAdvice, integrationsRefusal } from '../data/hosts'

const open = computed({ get: () => settingsDialog.open, set: (v) => (settingsDialog.open = v) })
const tab = computed({ get: () => settingsDialog.tab, set: (v) => (settingsDialog.tab = v) })
// Load the config the first time the modal opens, and refresh on every later open.
// immediate: a /settings/<section> deep link opens the dialog before this component
// mounts, and a watcher armed afterwards would miss that transition (blank panels).
const isAdmin = computed(() => can('admin'))
// Ctrl+, opens the dialog on its last tab; a non-Admin lands on their account instead.
watch([open, tab], ([o, t]) => { if (o && !isAdmin.value && ADMIN_SECTIONS.has(t)) tab.value = 'account' }, { immediate: true })
watch(open, (v) => {
  if (!v || !isAdmin.value) return
  config.load(true)
  cacheProjects.load(true)
  loadIntegrations()
  loadHosts()
}, { immediate: true })
const c = computed(() => config.data)
// Your account
const pwOpen = ref(false)
const pwChanged = ref(false)
const sessions = ref([])
const otherSessions = computed(() => sessions.value.filter((s) => !s.current))
const signingOutOthers = ref(false)
async function loadSessions() { try { sessions.value = (await listSessions()) || [] } catch { sessions.value = [] } }
watch(tab, (t) => { if (t === 'account') loadSessions() }, { immediate: true })
watch(open, (v) => { if (v && tab.value === 'account') loadSessions() })
async function signOutOthers() { signingOutOthers.value = true; try { await signOut({ others: true }); await loadSessions() } finally { signingOutOthers.value = false } }
async function doSignOut() { open.value = false; await signOut() }
const directoryUser = computed(() => isDirectorySource(session.user?.source))
const signInOn = computed(() => integrations.list.some((i) => i.provides.includes('sign_in') && i.enabled))

// --- integrations -----------------------------------------------------------
// Whose integrations the panel shows: this machine (the hub), or a remote host whose
// own Cloudflare DNS and Nginx Proxy Manager are set up and tested from here through
// the hub's forward. The remote keeps its secrets; this hub relays the form and the
// test result. Its link must be connected and its grant the Admin cap, else the panel
// says which, and names the command the remote's owner runs (data/hosts.js).
const pickedHost = computed(() => (integrations.host ? hosts.byId[integrations.host] || null : null))
const machine = computed(() => pickedHost.value?.name || 'this machine')
const machineOptions = computed(() => [{ label: `This machine${hosts.hub?.name ? ` (${hosts.hub.name})` : ''}`, value: '' }, ...hostList.value.map((h) => ({ label: h.name, value: h.id }))])
const integrationsRefused = computed(() => {
  if (!integrations.host) return null
  if (!pickedHost.value) return { kind: 'link', title: 'This host is no longer listed', text: 'Pick another machine.', command: null }
  return integrationsRefusal(pickedHost.value, hosts.hub)
})
const integrationsHost = computed({
  get: () => integrations.host || '',
  set: (v) => { pickIntegrationsHost(v || null); if (!integrationsRefused.value) loadIntegrations() },
})
// A remote's link moves while the panel is open (its owner raised the cap, the link
// dropped and came back): read its list as soon as that is possible.
watch([() => integrations.host, () => integrationsRefused.value?.kind || 'ok'], ([h, kind], [wasH, was]) => { if (h && h === wasH && kind === 'ok' && was !== 'ok') loadIntegrations() })
// The picker is a view of this tab: leaving it, or closing, returns to this machine's list.
watch([open, tab], ([o, t]) => { if ((!o || t !== 'integrations') && integrations.host) { pickIntegrationsHost(null); if (o) loadIntegrations() } })
const refusalTheme = computed(() => (integrationsRefused.value?.kind === 'cap' ? 'amber' : ['red', 'amber'].includes(linkLook(pickedHost.value?.state).theme) ? linkLook(pickedHost.value?.state).theme : 'gray'))
const official = computed(() => integrations.list.filter((i) => i.publisher === 'official'))
const community = computed(() => integrations.list.filter((i) => i.publisher === 'community'))
const communityMenu = (i) => [
  ...(i.enabled ? [{ label: 'Turn off', icon: 'lucide-power', onClick: () => { turningOff.value = i } }] : []),
  { label: 'Check for update', icon: 'lucide-refresh-cw', onClick: () => openCommunity('update', i.id) },
  { label: 'Remove…', icon: 'lucide-trash-2', theme: 'red', onClick: () => openCommunity('remove', i.id) },
]
const repoName = (u) => { try { return new URL(u).pathname.replace(/^\/|\.git$/g, '') || u } catch { return u } }
const turnOffText = (i) => {
  if (i?.provides?.includes('sign_in')) return 'Directory users can no longer sign in, and every directory user signed in now is signed out. The local admin account keeps working.'
  if (i?.provides?.includes('backup_target')) return 'New backups are no longer copied off-site; the copies already there stay.'
  return `${i?.name || 'It'} stops running.`
}
const turningOff = ref(null)
const offBusy = ref(false)
async function turnOff() {
  offBusy.value = true
  try {
    await disableIntegration(turningOff.value.id)
    turningOff.value = null
    await loadIntegrations()
  } catch (e) { settings.error = e } finally { offBusy.value = false }
}
function when(epoch) { return epoch ? new Date(epoch * 1000).toLocaleString() : '' }
function stateLine(i) {
  if (i.state === 'error') return i.last_error?.message || 'The last use failed.'
  if (i.state === 'needs_setup') return i.last_test?.message || 'The last test failed; finish the setup to turn it on.'
  if (i.state === 'on') return `On since ${when(i.updated_at)}${i.updated_by ? ` · by ${i.updated_by}` : ''}`
  return ''
}
const busy = (setting) => settings.busy === setting
const errorText = computed(() => {
  const e = settings.error
  if (!e) return null
  return e.hint ? `${e.message} ${e.hint}` : e.message
})

// --- remote hosts ------------------------------------------------------------
const hostLine = (h) => {
  const parts = [`${h.user}@${h.address}${h.port !== 22 ? ':' + h.port : ''}`, `host key ${h.host_key_fingerprint}`]
  if (h.cap) parts.push(`${h.cap} cap`)
  if (h.remote?.version) parts.push(`cwcli ${h.remote.version}`)
  if (h.state !== 'connected' && h.last_seen) parts.push(`last seen ${ago(h.last_seen * 1000)}`)
  if (h.drops_10m) parts.push(`${h.drops_10m} ${h.drops_10m === 1 ? 'drop' : 'drops'} in 10 min`)
  return parts.join(' · ')
}
const checkingHost = ref('')
async function checkNow(h) {
  checkingHost.value = h.id
  try { const data = await checkHost(h.id); if (data.host) applyHostLink(data.host) } catch (e) { settings.error = e } finally { checkingHost.value = '' }
}
const repinning = ref(null)
const newHostKey = ref('')
const repinBusy = ref(false)
async function pinNewKey() {
  repinBusy.value = true
  try { const data = await repinHost(repinning.value.id, newHostKey.value.trim()); if (data.host) applyHostLink(data.host); repinning.value = null; newHostKey.value = '' } catch (e) { settings.error = e } finally { repinBusy.value = false }
}
const removingHost = ref(null)
const removeHostBusy = ref(false)
async function confirmRemoveHost() {
  removeHostBusy.value = true
  try { await removeHost(removingHost.value.id); dropHost(removingHost.value.id); removingHost.value = null } catch (e) { settings.error = e } finally { removeHostBusy.value = false }
}
const hubLine = computed(() => {
  const hub = hosts.hub
  if (!hub) return 'reading…'
  if (!hub.public_key) return 'No key yet: turn on Remote hosts over SSH in Integrations; its test creates the key pair.'
  return `${hub.name} · key ${hub.public_key.split(' ')[1]?.slice(0, 12)}… · ${hub.transport_on ? 'Remote hosts over SSH is on' : 'Remote hosts over SSH is off'} · protocol ${hub.protocol}`
})

// --- search paths -----------------------------------------------------------
const newPath = ref('')
async function addPath() {
  const path = newPath.value.trim()
  if (!path) return
  const data = await applySetting('paths.add', { path }, { done: (out) => (out?.changed ? `Added ${out.path}` : `${out?.path} was already a search path`) })
  if (data) newPath.value = ''
}
const removePath = (path) => applySetting('paths.remove', { path }, { done: `Removed ${path}` })
const prune = ref(null)
async function checkPrune() {
  const data = await applySetting('paths.prune', {}, { refresh: false })
  prune.value = data?.outcome || null
}
async function applyPrune() {
  const data = await applySetting('paths.prune', { apply: true }, { done: (out) => `Removed ${out?.pruned?.length || 0} unreferenced ${out?.pruned?.length === 1 ? 'path' : 'paths'}` })
  prune.value = data?.outcome || null
}
const prunable = computed(() => (prune.value?.statuses || []).filter((s) => s.prunable).map((s) => s.path))

// --- cache ------------------------------------------------------------------
const cacheOn = computed({
  get: () => !!c.value?.cache_enabled,
  set: (v) => applySetting(v ? 'cache.enable' : 'cache.disable', {}, { done: `Cache ${v ? 'enabled' : 'disabled'}` }),
})
const clearOpen = ref(false)
async function clearAll() {
  const data = await applySetting('cache.clear', { all: true, consent: true }, { done: 'Cache cleared' })
  if (data) clearOpen.value = false
}
const clearProject = (name) => applySetting('cache.clear', { project: name }, { done: (out) => (out?.found ? `Forgot ${name}` : `${name} was not in the cache`) })

// --- auto-inspect -----------------------------------------------------------
const autoOn = computed({
  get: () => !!c.value?.auto_inspect.enabled,
  set: (v) => applySetting(v ? 'auto_inspect.enable' : 'auto_inspect.disable', {}, { done: `Auto-inspect ${v ? 'enabled' : 'disabled'}` }),
})
const intervalMinutes = ref(60)
watch(() => c.value?.auto_inspect.interval, (s) => (intervalMinutes.value = Math.max(1, Math.round((s || 3600) / 60))), { immediate: true })
const intervalChanged = computed(() => c.value && Math.round(intervalMinutes.value * 60) !== c.value.auto_inspect.interval)
const saveInterval = () => applySetting('auto_inspect.enable', { interval: Math.round(Number(intervalMinutes.value) * 60) }, { done: (out) => `Inspecting every ${Math.round(out.state.interval / 60)} minutes` })
const autoAtBoot = computed({
  get: () => !!c.value?.auto_inspect.startup_enabled,
  set: (v) => applySetting('auto_inspect.enable', { at_boot: v }, { done: v ? 'Auto-inspect starts at boot' : 'Boot hook removed' }),
})
const startDaemon = (feature, running, name) => applySetting(`${feature}.${running ? 'restart' : 'start'}`, {}, { done: `${name} daemon ${running ? 'restarted' : 'started'}` })
const daemonBusy = (feature) => busy(`${feature}.start`) || busy(`${feature}.restart`)

// --- credential bridge ------------------------------------------------------
const bridgeOn = computed({
  get: () => !!c.value?.cred_bridge.enabled,
  set: (v) => applySetting(v ? 'cred_bridge.enable' : 'cred_bridge.disable', {}, { done: (out) => (v ? `Credential bridge enabled${out?.ensured_projects?.length ? ` for ${out.ensured_projects.join(', ')}` : ''}` : 'Credential bridge disabled') }),
})
const bridgeAtBoot = computed({
  get: () => !!c.value?.cred_bridge.startup_enabled,
  set: (v) => applySetting('cred_bridge.enable', { at_boot: v }, { done: v ? 'Credential bridge starts at boot' : 'Boot unit removed' }),
})

// --- tips and agent hooks ---------------------------------------------------
const tipsOn = computed({
  get: () => !!c.value?.show_tips,
  set: (v) => applySetting(v ? 'tips.enable' : 'tips.disable', {}, { done: `Tips ${v ? 'on' : 'off'}` }),
})
const hooks = ref(null)
async function installHooks() {
  const data = await applySetting('agent_hooks.install', {}, { refresh: false, done: 'Agent hooks checked' })
  hooks.value = data?.outcome || null
}
const HOOK_THEME = { installed: 'green', updated: 'green', unchanged: 'gray', skipped: 'gray', manual: 'amber' }

// --- app sources: GitHub and gitlab.com scan member repositories plus these extras;
// a self-hosted GitLab host scans everything the account can see.
const newSource = ref('')
const extras = computed(() => catalog.data?.extra_sources || [])
async function addSource() {
  const source = newSource.value.trim()
  if (!source) return
  const data = await applySetting('catalog.source_add', { source }, { refresh: false, done: `Added ${source}; rescan to include it` })
  if (data) { newSource.value = ''; loadCatalog({ fresh: true }) }
}
const removeSource = async (source) => { await applySetting('catalog.source_remove', { source }, { refresh: false, done: `Removed ${source}` }); loadCatalog({ fresh: true }) }
</script>

<template>
  <SettingsDialog v-model:open="open" v-model:tab="tab" size="4xl" keyboard-shortcut="Mod+Comma">
    <SettingsSidebar data-settings-dialog>
      <!-- A settings menu says so (captain note 02:12Z): a visible title, then grouped entries. -->
      <div class="flex items-center gap-2 px-3 pb-2 pt-1">
        <span class="lucide-settings size-4 text-ink-gray-6" aria-hidden="true" />
        <span class="text-lg font-semibold text-ink-gray-9">Settings</span>
      </div>
      <template v-if="isAdmin">
      <SettingsNavGroup label="Workspace">
        <SettingsNavItem value="paths"><template #prefix><span class="lucide-folder-search size-4" aria-hidden="true" /></template>Search paths</SettingsNavItem>
        <SettingsNavItem value="cache"><template #prefix><span class="lucide-database size-4" aria-hidden="true" /></template>Cache</SettingsNavItem>
        <SettingsNavItem value="daemons"><template #prefix><span class="lucide-cog size-4" aria-hidden="true" /></template>Background daemons</SettingsNavItem>
      </SettingsNavGroup>
      <SettingsNavGroup label="Apps">
        <SettingsNavItem value="sources"><template #prefix><span class="lucide-package-search size-4" aria-hidden="true" /></template>App sources</SettingsNavItem>
      </SettingsNavGroup>
      <SettingsNavGroup label="Agents">
        <SettingsNavItem value="agents"><template #prefix><span class="lucide-bot size-4" aria-hidden="true" /></template>Agent hooks</SettingsNavItem>
      </SettingsNavGroup>
      <SettingsNavGroup label="Connections">
        <SettingsNavItem value="integrations"><template #prefix><span class="lucide-blocks size-4" aria-hidden="true" /></template>Integrations</SettingsNavItem>
        <SettingsNavItem value="hosts"><template #prefix><span class="lucide-server size-4" aria-hidden="true" /></template>Remote hosts</SettingsNavItem>
      </SettingsNavGroup>
      </template>
      <SettingsNavGroup label="General">
        <SettingsNavItem value="account"><template #prefix><span class="lucide-user-round size-4" aria-hidden="true" /></template>Your account</SettingsNavItem>
        <SettingsNavItem value="appearance"><template #prefix><span class="lucide-palette size-4" aria-hidden="true" /></template>Appearance</SettingsNavItem>
        <SettingsNavItem value="health"><template #prefix><span class="lucide-stethoscope size-4" aria-hidden="true" /></template>Health check</SettingsNavItem>
        <SettingsNavItem value="about"><template #prefix><span class="lucide-info size-4" aria-hidden="true" /></template>About cwcli</SettingsNavItem>
      </SettingsNavGroup>
    </SettingsSidebar>
    <SettingsContent class="min-w-0">
      <LoadingText v-if="isAdmin && !c && config.loading" class="p-6" />
      <ErrorMessage v-if="isAdmin && config.error" class="p-6" :message="config.error" />
      <template v-if="c && isAdmin">
        <SettingsPanel value="paths">
          <SettingsHeader title="Bench search paths" :description="`Where inspect looks for benches inside each container. ${c.search_paths.length} ${c.search_paths.length === 1 ? 'path' : 'paths'}.`">
            <template #actions><CommandCopy command="cwcli config paths" /></template>
          </SettingsHeader>
          <SettingsBody>
            <ErrorMessage :message="errorText" />
            <SettingsRow v-for="p in c.search_paths" :key="p" :title="p" description="Searched in every instance on inspect.">
              <div class="flex items-center gap-1">
                <CommandCopy :command="`cwcli config paths remove ${p}`" />
                <Button size="sm" variant="ghost" icon="lucide-x" :aria-label="`Remove ${p}`" :loading="busy('paths.remove')" @click="removePath(p)" />
              </div>
            </SettingsRow>
            <SettingsRow title="Add a path" description="An absolute path inside the container, for example /workspace/other-bench.">
              <form class="flex items-center gap-2" @submit.prevent="addPath">
                <FormControl v-model="newPath" class="w-56" placeholder="/workspace/other-bench" autocomplete="off" />
                <Button type="submit" label="Add" :disabled="!newPath.trim()" :loading="busy('paths.add')" />
              </form>
            </SettingsRow>
            <SettingsRow title="Unreferenced paths" description="Checks every path live against every instance. A path a stopped instance might still use is never removed.">
              <div class="flex items-center gap-1">
                <CommandCopy command="cwcli config paths prune" />
                <Button label="Check" icon-left="lucide-search" :loading="busy('paths.prune') && !prune" :disabled="!c.search_paths.length" @click="checkPrune" />
              </div>
            </SettingsRow>
            <div v-if="prune" class="space-y-2 py-2">
              <template v-if="prunable.length && !prune.applied">
                <p class="text-sm text-ink-gray-7">{{ prunable.length }} {{ prunable.length === 1 ? 'path is' : 'paths are' }} absent from every instance:</p>
                <code v-for="p in prunable" :key="p" class="block truncate font-mono text-sm text-ink-gray-7">{{ p }}</code>
                <Button theme="red" :label="`Remove ${prunable.length} ${prunable.length === 1 ? 'path' : 'paths'}`" icon-left="lucide-trash-2" :loading="busy('paths.prune')" @click="applyPrune" />
              </template>
              <p v-else-if="prune.applied && prune.pruned.length" class="text-sm text-ink-green-6">Removed {{ prune.pruned.length }} {{ prune.pruned.length === 1 ? 'path' : 'paths' }}.</p>
              <p v-else-if="prune.unchecked_instances.length" class="text-sm text-ink-amber-6">Nothing can be confirmed dead while {{ prune.unchecked_instances.join(', ') }} {{ prune.unchecked_instances.length === 1 ? 'is' : 'are' }} stopped or unreachable.</p>
              <p v-else class="text-sm text-ink-gray-5">Every search path is still referenced by an instance.</p>
            </div>
          </SettingsBody>
        </SettingsPanel>

        <SettingsPanel value="cache">
          <SettingsHeader title="Cache" description="What cwcli remembers about benches, sites and apps between runs.">
            <template #actions><CommandCopy command="cwcli config show" /></template>
          </SettingsHeader>
          <SettingsBody>
            <ErrorMessage :message="errorText" />
            <SettingsRow title="Keep a cache of benches, sites and apps" :description="c.cache_env_override ? 'CWCLI_NO_CACHE is set in the daemon\'s environment, so this setting is inert until it is unset.' : c.cache_db">
              <div class="flex items-center gap-1"><CommandCopy :command="c.cache_enabled ? 'cwcli config cache disable' : 'cwcli config cache enable'" /><Switch v-model="cacheOn" :disabled="c.cache_env_override || busy('cache.enable') || busy('cache.disable')" /></div>
            </SettingsRow>
            <SettingsRow title="Remembered projects" :description="`${cacheProjects.data?.length ?? '...'} ${cacheProjects.data?.length === 1 ? 'project' : 'projects'}, including removed instances. Forgetting one drops its benches, sites and apps; the next inspect re-reads them.`">
              <div class="flex items-center gap-1"><CommandCopy command="cwcli config cache clear --all" /><Button theme="red" label="Clear everything" icon-left="lucide-trash-2" :disabled="!cacheProjects.data?.length" @click="clearOpen = true" /></div>
            </SettingsRow>
            <SettingsRow v-for="p in cacheProjects.data || []" :key="p.name" :title="p.name" :description="`cached ${p.last_updated}`">
              <div class="flex items-center gap-1"><CommandCopy :command="`cwcli config cache clear ${p.name}`" /><Button size="sm" variant="ghost" label="Forget" :loading="busy('cache.clear')" @click="clearProject(p.name)" /></div>
            </SettingsRow>
          </SettingsBody>
        </SettingsPanel>

        <SettingsPanel value="daemons">
          <SettingsHeader title="Background daemons" description="Auto-inspect keeps the cache warm; the credential bridge lets git inside containers use this machine's gh and glab sign-ins. No token ever enters a container." />
          <SettingsBody>
            <ErrorMessage :message="errorText" />
            <SettingsRow title="Auto-inspect" :description="`Refresh the cache in the background · daemon ${c.auto_inspect.daemon_running ? 'running' : 'not running'}`">
              <div class="flex items-center gap-1"><CommandCopy :command="c.auto_inspect.enabled ? 'cwcli config auto-inspect disable' : 'cwcli config auto-inspect enable'" /><Switch v-model="autoOn" :disabled="busy('auto_inspect.enable') || busy('auto_inspect.disable')" /></div>
            </SettingsRow>
            <template v-if="c.auto_inspect.enabled">
              <SettingsRow title="Interval" description="Minutes between background inspects.">
                <div class="flex items-center gap-2"><FormControl v-model="intervalMinutes" class="w-24" type="number" :min="1" /><Button label="Save" :disabled="!intervalChanged || Number(intervalMinutes) < 1" :loading="busy('auto_inspect.enable')" @click="saveInterval" /></div>
              </SettingsRow>
              <SettingsRow title="Start at boot" description="Installs a boot unit that runs cwcli config auto-inspect start.">
                <Checkbox v-model="autoAtBoot" :disabled="busy('auto_inspect.enable')" />
              </SettingsRow>
              <SettingsRow title="Daemon" :description="!c.auto_inspect.daemon_running && c.auto_inspect.last_log_line ? `Last log: ${c.auto_inspect.last_log_line}` : c.auto_inspect.daemon_running ? 'Running.' : 'Not running.'">
                <div class="flex items-center gap-1"><CommandCopy command="cwcli config auto-inspect start" /><Button :label="c.auto_inspect.daemon_running ? 'Restart' : 'Start'" :icon-left="c.auto_inspect.daemon_running ? 'lucide-rotate-cw' : 'lucide-play'" :variant="c.auto_inspect.daemon_running ? 'subtle' : 'solid'" :loading="daemonBusy('auto_inspect')" @click="startDaemon('auto_inspect', c.auto_inspect.daemon_running, 'Auto-inspect')" /></div>
              </SettingsRow>
            </template>
            <SettingsRow title="Persistent credential bridge" :description="c.cred_bridge.enabled ? (c.cred_bridge.daemon_running ? `daemon running · ${c.cred_bridge.transport} transport` : 'enabled, but the daemon is not running') : 'off; per-command bridging still works'">
              <div class="flex items-center gap-1"><CommandCopy :command="c.cred_bridge.enabled ? 'cwcli config cred-bridge disable' : 'cwcli config cred-bridge enable'" /><Switch v-model="bridgeOn" :disabled="busy('cred_bridge.enable') || busy('cred_bridge.disable')" /></div>
            </SettingsRow>
            <template v-if="c.cred_bridge.enabled">
              <SettingsRow title="Start at boot" description="Installs a boot unit that runs cwcli config cred-bridge start.">
                <Checkbox v-model="bridgeAtBoot" :disabled="busy('cred_bridge.enable')" />
              </SettingsRow>
              <SettingsRow title="Daemon" :description="!c.cred_bridge.daemon_running && c.cred_bridge.last_log_line ? `Last log: ${c.cred_bridge.last_log_line}` : c.cred_bridge.daemon_running ? 'Running.' : 'Not running.'">
                <div class="flex items-center gap-1"><CommandCopy command="cwcli config cred-bridge start" /><Button :label="c.cred_bridge.daemon_running ? 'Restart' : 'Start'" :icon-left="c.cred_bridge.daemon_running ? 'lucide-rotate-cw' : 'lucide-play'" :variant="c.cred_bridge.daemon_running ? 'subtle' : 'solid'" :loading="daemonBusy('cred_bridge')" @click="startDaemon('cred_bridge', c.cred_bridge.daemon_running, 'Credential bridge')" /></div>
              </SettingsRow>
            </template>
            <SettingsRow title="Allowed hosts" description="Edit [cred_bridge] allowed_hosts in the config file to change them.">
              <div class="flex flex-wrap justify-end gap-1"><Badge v-for="h in c.cred_bridge.allowed_hosts" :key="h" variant="outline" theme="gray" :label="h" /></div>
            </SettingsRow>
            <div v-if="c.cred_bridge.recent_audit?.length" class="py-3">
              <div class="text-sm text-ink-gray-5">Recent credential requests</div>
              <div class="mt-2 max-h-40 overflow-y-auto rounded-4 bg-surface-gray-1 p-2">
                <div v-for="line in [...c.cred_bridge.recent_audit].reverse()" :key="line" class="font-mono text-xs leading-5 text-ink-gray-6">{{ line }}</div>
              </div>
            </div>
          </SettingsBody>
        </SettingsPanel>

        <SettingsPanel value="sources">
          <SettingsHeader title="App sources" description="Where the apps grid looks for Frappe apps. GitHub and gitlab.com: the repositories your sign-in is a member of, plus any organisation or group added here. A self-hosted GitLab host: everything your account can see, keeping only what is proven to be a Frappe app.">
            <template #actions><Button label="Rescan now" icon-left="lucide-refresh-cw" :loading="catalog.scanning" @click="scanCatalog()" /></template>
          </SettingsHeader>
          <SettingsBody>
            <ErrorMessage :message="errorText" />
            <SettingsRow v-for="s in sources" :key="s.host" :title="hostLabel(s.host)" :description="s.signed_in ? `${s.scope} · ${s.repos} scanned, ${s.apps} Frappe apps${s.error ? ' · ' + s.error : ''}` : 'not signed in'">
              <Badge size="sm" :theme="s.signed_in ? (s.error ? 'amber' : 'green') : 'gray'" :label="s.signed_in ? s.state : 'off'" />
            </SettingsRow>
            <SettingsRow v-for="x in extras" :key="x" :title="x" description="Extra organisation or group, scanned with the member repositories.">
              <Button size="sm" variant="ghost" icon="lucide-x" :aria-label="`Remove ${x}`" :loading="busy('catalog.source_remove')" @click="removeSource(x)" />
            </SettingsRow>
            <SettingsRow title="Add an organisation or group" description="github:<org> or gitlab.com:<group/path>. Stored as [app_catalog] extra_sources in the config file.">
              <form class="flex items-center gap-2" @submit.prevent="addSource">
                <FormControl v-model="newSource" class="w-64" placeholder="github:frappe" autocomplete="off" />
                <Button type="submit" label="Add" :disabled="!newSource.trim()" :loading="busy('catalog.source_add')" />
              </form>
            </SettingsRow>
          </SettingsBody>
        </SettingsPanel>

        <SettingsPanel value="agents">
          <SettingsHeader title="Agent hooks" description="Installs the cwcli SessionStart hook into every agent harness found on this machine (Claude Code, Codex, OpenCode), so an agent opens with the cwcli axi surface in its context. Only existing harness configs are touched.">
            <template #actions><CommandCopy command="cwcli axi setup" /></template>
          </SettingsHeader>
          <SettingsBody>
            <SettingsRow title="Install or repair hooks" description="A moved cwcli is repaired in place; a harness that is not installed is skipped.">
              <Button label="Install or repair" icon-left="lucide-bot" :loading="busy('agent_hooks.install')" @click="installHooks" />
            </SettingsRow>
            <SettingsRow v-for="h in hooks || []" :key="h.agent" :title="h.agent" :description="h.detail || h.path">
              <Badge size="sm" :theme="HOOK_THEME[h.status] || 'gray'" :label="h.status" />
            </SettingsRow>
          </SettingsBody>
        </SettingsPanel>

        <!-- min-w-0: as on the account panel; the grant command (one long key token) must wrap, not widen the column. -->
        <SettingsPanel value="integrations" class="min-w-0">
          <SettingsHeader title="Integrations" :description="integrations.host ? `${machine}'s own integrations, set up and tested from this hub through its link. Each starts off; turning one on tests it on ${machine} first and turns it on only when the test passes. Secrets are stored encrypted on ${machine} and never leave it: this hub relays only the setup form and the test result.` : 'Connect cwcli to systems outside it. Each starts off; turning one on tests it first and turns it on only when the test passes. Secrets are stored encrypted on this machine and never shown again.'">
            <!-- The picker sits beside the heading, except on a phone, where it would squeeze the text to a word a line: there it goes below. -->
            <template v-if="hasHosts" #actions><Select v-model="integrationsHost" :options="machineOptions" class="w-56 max-sm:hidden" aria-label="Machine whose integrations to set up" /></template>
          </SettingsHeader>
          <SettingsBody>
            <Select v-if="hasHosts" v-model="integrationsHost" :options="machineOptions" class="mt-2 w-full sm:hidden" aria-label="Machine whose integrations to set up" />
            <ErrorMessage :message="integrations.error?.message || errorText" />
            <template v-if="integrationsRefused">
              <Alert class="mt-3" :theme="refusalTheme" :title="integrationsRefused.title" :description="integrationsRefused.text" />
              <CommandLine v-if="integrationsRefused.command" class="mt-2" :command="integrationsRefused.command" wrap />
              <div class="mt-3 flex flex-wrap items-center gap-2">
                <Button v-if="pickedHost" icon-left="lucide-refresh-cw" :label="`Check ${pickedHost.name} now`" :loading="checkingHost === pickedHost.id" @click="checkNow(pickedHost)" />
                <Button label="Remote hosts" @click="tab = 'hosts'" />
                <Button label="This machine" @click="integrationsHost = ''" />
              </div>
            </template>
            <template v-else>
            <LoadingText v-if="integrations.loading && !integrations.list.length" class="py-3" />
            <div class="pt-3 text-sm font-medium text-ink-gray-5">Official{{ integrations.host ? ` · on ${machine}` : '' }}</div>
            <div v-for="i in official" :key="i.id" class="flex items-center gap-8 py-3.5">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-base-medium leading-tighter text-ink-gray-8">{{ i.name }}</span>
                  <Badge size="sm" variant="subtle" :theme="PUBLISHER[i.publisher]?.theme" :label="PUBLISHER[i.publisher]?.label" />
                </div>
                <div class="mt-1 text-base leading-5 text-ink-gray-6">{{ i.description }}</div>
                <div v-if="i.state !== 'off'" class="mt-1 text-sm leading-5" :class="i.state === 'on' ? 'text-ink-gray-5' : 'text-ink-red-6'">{{ stateLine(i) }}</div>
                <div v-for="n in i.notices || []" :key="n" class="mt-1 text-sm leading-5 text-ink-amber-6">{{ n }}</div>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <Badge size="sm" variant="subtle" :theme="STATE[i.state]?.theme" :label="STATE[i.state]?.label" />
                <Button :label="i.enabled ? 'Settings…' : i.state === 'needs_setup' ? 'Finish setup…' : 'Turn on…'" @click="openIntegration(i.id)" />
                <Button v-if="i.enabled" label="Turn off" @click="turningOff = i" />
              </div>
            </div>
            <p v-if="integrations.host" class="pt-5 text-p-sm text-ink-gray-5">Community integrations are installed on {{ machine }} by its own Admin, from its own web UI or with cwcli integrations add there; a hub never installs code on a remote host.</p>
            <template v-else>
            <div class="flex items-center gap-4 pt-5">
              <div class="min-w-0 flex-1 text-sm font-medium text-ink-gray-5">Community</div>
              <div class="flex shrink-0 items-center gap-1"><CommandCopy command="cwcli integrations add <repository-url>" /><Button icon-left="lucide-package-plus" label="Add from a repository…" @click="openCommunity('add')" /></div>
            </div>
            <p class="mt-1 text-p-sm text-ink-gray-5">Added from a GitHub or GitLab repository and pinned to the commit you review. Nobody has reviewed community code: it runs with cwcli's own rights on this machine. Each starts off.</p>
            <div v-for="i in community" :key="i.id" class="flex items-center gap-8 py-3.5">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-base-medium leading-tighter text-ink-gray-8">{{ i.name }}</span>
                  <Badge size="sm" variant="subtle" :theme="PUBLISHER[i.publisher]?.theme" :label="PUBLISHER[i.publisher]?.label" />
                </div>
                <div class="mt-1 text-base leading-5 text-ink-gray-6">{{ i.description }}</div>
                <div v-if="i.community" class="mt-1 break-words text-sm leading-5 text-ink-gray-5" :title="`${i.community.url} @ ${i.community.commit}`">{{ repoName(i.community.url) }} @ <span class="font-mono">{{ i.community.commit.slice(0, 12) }}</span>{{ i.community.ref ? ` (${i.community.ref})` : '' }} · by {{ i.community.publisher }}</div>
                <div v-if="i.community?.load_error" class="mt-1 text-sm leading-5 text-ink-red-6">Does not load: {{ i.community.load_error }}</div>
                <div v-else-if="i.state !== 'off'" class="mt-1 text-sm leading-5" :class="i.state === 'on' ? 'text-ink-gray-5' : 'text-ink-red-6'">{{ stateLine(i) }}</div>
                <div v-for="n in i.notices || []" :key="n" class="mt-1 text-sm leading-5 text-ink-amber-6">{{ n }}</div>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <Badge size="sm" variant="subtle" :theme="STATE[i.state]?.theme" :label="STATE[i.state]?.label" />
                <Button v-if="!i.community?.load_error" :label="i.enabled ? 'Settings…' : i.state === 'needs_setup' ? 'Finish setup…' : 'Turn on…'" @click="openIntegration(i.id)" />
                <Dropdown :options="communityMenu(i)" align="end"><Button icon="lucide-ellipsis" :aria-label="`More for ${i.name}`" /></Dropdown>
              </div>
            </div>
            <p v-if="!community.length && !integrations.loading" class="py-3 text-p-sm text-ink-gray-5">No community integrations yet.</p>
            </template>
            </template>
          </SettingsBody>
        </SettingsPanel>

        <SettingsPanel value="hosts">
          <SettingsHeader title="Remote hosts" description="Other machines whose instances this web UI shows and drives, each reached over SSH with this hub's one key. The machine's owner grants the key with cwcli remote allow, restricted to cwcli's API and capped at a role; this hub pins the machine's host key when it is added. Nothing is trusted on first contact, and a host that stops answering is shown as such, never acted on.">
            <template #actions><CommandCopy command="cwcli remote ls" /><Button icon-left="lucide-plus" label="Add host…" @click="openAddHost" /></template>
          </SettingsHeader>
          <SettingsBody>
            <ErrorMessage :message="hosts.error?.message || errorText" />
            <SettingsRow title="This hub" :description="hubLine">
              <div class="flex items-center gap-1"><CommandCopy :command="hosts.hub?.allow_command || 'cwcli remote key'" /><Button label="Integrations" @click="tab = 'integrations'" /></div>
            </SettingsRow>
            <SettingsRow v-for="h in hostList" :key="h.id" :title="h.name" :description="`${hostLine(h)}${h.state !== 'connected' ? ' · ' + linkAdvice(h) : ''}`">
              <div class="flex flex-wrap items-center justify-end gap-1">
                <Badge size="sm" :theme="linkLook(h.state).theme" :variant="h.state === 'connected' ? 'subtle' : 'outline'" :label="linkLook(h.state).label" />
                <Button size="sm" variant="ghost" icon="lucide-refresh-cw" :aria-label="`Check ${h.name} now`" :loading="checkingHost === h.id" @click="checkNow(h)" />
                <Button v-if="h.state === 'host_key_changed'" size="sm" label="Pin new key…" @click="repinning = h; newHostKey = ''" />
                <Button size="sm" variant="ghost" icon="lucide-trash-2" :aria-label="`Remove ${h.name}`" @click="removingHost = h" />
              </div>
            </SettingsRow>
            <div v-if="repinning" class="space-y-2 rounded-6 border border-outline-red-2 bg-surface-red-1 p-3">
              <p class="text-sm text-ink-gray-8">{{ repinning.name }} presented a different SSH host key than the one pinned ({{ repinning.host_key_fingerprint }}). Confirm with its owner that the key really changed (a reinstall, a new VM), then paste the new one from /etc/ssh/ssh_host_ed25519_key.pub there. A changed key can also mean another machine is answering.</p>
              <FormControl v-model="newHostKey" type="textarea" :rows="2" placeholder="ssh-ed25519 AAAA…" spellcheck="false" autocomplete="off" aria-label="New host key" />
              <div class="flex justify-end gap-2"><Button label="Cancel" @click="repinning = null" /><Button variant="solid" theme="red" label="Pin this key" :disabled="!newHostKey.trim()" :loading="repinBusy" @click="pinNewKey" /></div>
            </div>
            <p v-if="!hostList.length && hosts.loaded" class="py-3 text-p-sm text-ink-gray-5">No remote host yet. Add host walks through the pairing: run one command on the other machine as the user who runs cwcli there, then paste what it prints here.</p>
            <p class="py-2 text-xs text-ink-gray-5">The remote lowers whatever this hub's user is to the cap it granted; a remote's owner withdraws this hub any time with cwcli remote revoke there, which also ends the hub's running connections.</p>
          </SettingsBody>
        </SettingsPanel>
      </template>
        <!-- min-w-0: the panel is a flex-row item, so without it one no-wrap session row sets
             the column's minimum width and the right edge of the panel leaves the dialog. -->
        <SettingsPanel value="account" class="min-w-0">
          <SettingsHeader title="Your account" :description="`Signed in as ${session.user?.name ? `${session.user.name} (${session.user.username})` : session.user?.username || ''} · ${ROLE_LABEL[session.user?.role] || 'Admin'}`" />
          <SettingsBody>
            <SettingsRow v-if="directoryUser && session.user?.source === 'ldap'" title="Directory account" :description="`Your directory groups gave you the ${ROLE_LABEL[session.user?.role]} role when you signed in. Your password and groups are managed in the directory.`" />
            <SettingsRow v-else-if="directoryUser" title="Signed in through an integration" :description="`The sign-in integration that is on gave you the ${ROLE_LABEL[session.user?.role]} role when you signed in. Your password is managed there, not in cwcli.`" />
            <template v-else>
              <SettingsRow class="max-sm:flex-col max-sm:items-stretch max-sm:gap-3" title="Change password" :description="signInOn ? 'This local account keeps working while directory sign-in is on, so keep its password somewhere safe. At least 8 characters, not the default.' : 'At least 8 characters, not the default. Other sessions stay signed in.'">
                <Button :label="pwOpen ? 'Hide' : 'Change…'" @click="pwOpen = !pwOpen" />
              </SettingsRow>
              <div v-if="pwOpen" class="max-w-sm px-1 pb-4"><PasswordForm @done="pwOpen = false; pwChanged = true" /><p v-if="pwChanged" class="mt-2 text-p-sm text-ink-green-6">Password changed.</p></div>
            </template>
            <SettingsRow class="max-sm:flex-col max-sm:items-stretch max-sm:gap-3" title="Other sessions" :description="otherSessions.length ? `${otherSessions.length} other ${otherSessions.length === 1 ? 'browser is' : 'browsers are'} signed in as ${session.user?.username}.` : 'No other browser is signed in.'">
              <Button label="Sign out everywhere else" :disabled="!otherSessions.length" :loading="signingOutOthers" @click="signOutOthers" />
            </SettingsRow>
            <ul v-if="otherSessions.length" class="space-y-1 px-1 pb-3 text-sm text-ink-gray-6">
              <li v-for="s in otherSessions" :key="s.created + s.agent" class="truncate" :title="s.agent || undefined">{{ s.agent || 'unknown browser' }} · last seen {{ new Date(s.last_seen * 1000).toLocaleString() }}{{ s.remember ? ' · kept signed in' : '' }}</li>
            </ul>
            <SettingsRow class="max-sm:flex-col max-sm:items-stretch max-sm:gap-3" title="Sign out" description="Ends this browser's session.">
              <div class="flex items-center gap-1"><CommandCopy v-if="!directoryUser" command="cwcli admin reset-password" /><Button icon-left="lucide-log-out" label="Sign out" @click="doSignOut" /></div>
            </SettingsRow>
          </SettingsBody>
        </SettingsPanel>
        <SettingsPanel value="health">
          <SettingsHeader title="Health check" description="Can cwcli operate on this machine: Docker, its own files, disk space, and the tools the credential bridge and transfers rely on. Instance health lives on each instance's page." />
          <SettingsBody>
            <DoctorView />
            <div class="mt-4 max-w-sm"><CommandLine command="cwcli doctor" /></div>
          </SettingsBody>
        </SettingsPanel>
        <SettingsPanel value="appearance">
          <SettingsHeader title="Appearance" description="The gradient theme, light or dark, and whether the visual effects run at all. Kept in this browser." />
          <SettingsBody><AppearanceControls /></SettingsBody>
        </SettingsPanel>
        <SettingsPanel value="about">
          <SettingsHeader title="About cwcli" :description="`Version ${session.meta?.version || 'unknown'} · web UI protocol ${session.meta?.protocol ?? 'unknown'}`" />
          <SettingsBody>
            <template v-if="isAdmin && c">
              <SettingsRow title="Config file" :description="c.config_file"><CommandCopy command="cwcli config show" /></SettingsRow>
              <SettingsRow title="Update cwcli" description="Checks the package index and updates this install; the daemon restarts and this tab reloads.">
                <div class="flex items-center gap-1"><CommandCopy command="cwcli self-update" /><Button icon-left="lucide-arrow-up-circle" label="Update" @click="openJob('self-update')" /></div>
              </SettingsRow>
              <SettingsRow title="Show tips after commands" :description="`Currently ${c.show_tips ? 'on' : 'off'}`">
                <div class="flex items-center gap-1"><CommandCopy :command="c.show_tips ? 'cwcli config tips disable' : 'cwcli config tips enable'" /><Switch v-model="tipsOn" :disabled="busy('tips.enable') || busy('tips.disable')" /></div>
              </SettingsRow>
            </template>
            <SettingsRow v-else title="Settings" description="Only an Admin can change cwcli's settings, integrations and version." />
          </SettingsBody>
        </SettingsPanel>
    </SettingsContent>
  </SettingsDialog>

  <IntegrationDialog />
  <CommunityDialog />
  <Dialog :open="!!removingHost" :title="`Remove ${removingHost?.name || ''} from this hub?`" icon="lucide-server-off" @update:open="(v) => { if (!v) removingHost = null }">
    <p class="text-p-base text-ink-gray-7">This hub forgets the host and drops its connection; nothing changes on {{ removingHost?.name }}. Its owner withdraws this hub's key there with <code class="font-mono text-sm">cwcli remote revoke</code>.</p>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" theme="red" data-primary label="Remove host" :loading="removeHostBusy" @click="confirmRemoveHost" />
      </div>
    </template>
  </Dialog>
  <Dialog :open="!!turningOff" :title="`Turn off ${turningOff?.name || ''}${integrations.host ? ` on ${machine}` : ''}?`" icon="lucide-power" @update:open="(v) => { if (!v) turningOff = null }">
    <p class="text-p-base text-ink-gray-7">{{ turnOffText(turningOff) }}{{ integrations.host ? ` This happens on ${machine}, not on this hub.` : '' }} Its settings are kept for the next time it is turned on.</p>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" theme="red" data-primary label="Turn off" :loading="offBusy" @click="turnOff" />
      </div>
    </template>
  </Dialog>
  <Dialog v-model:open="clearOpen" title="Clear the whole cache?" icon="lucide-trash-2" theme="red">
    <p class="text-p-base text-ink-gray-7">Every remembered bench, site and app is forgotten, for live and removed instances alike. Bench labels stay in their marker files and come back on the next inspect; durable bench numbers are kept.</p>
    <div class="mt-3"><CommandLine command="cwcli config cache clear --all" /></div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" theme="red" data-primary label="Clear everything" :loading="busy('cache.clear')" @click="clearAll" />
      </div>
    </template>
  </Dialog>
</template>
