<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { PageHeader, PageHeaderTitle, Badge, Button, Checkbox, Dialog, FormControl, Switch, LoadingText, ErrorMessage } from 'frappe-ui'
import CommandLine from '../components/CommandLine.vue'
import { config, cacheProjects, setFocus } from '../data/fleet'
import { session } from '../data/session'
import { openJob } from '../data/job-dialog'
import { applySetting, settings } from '../data/settings'

// Every write goes through applySetting -> POST /api/v1/settings, which calls
// the same core function the named terminal command does; the command stays
// beside each control so the two are visibly one.
onMounted(() => {
  setFocus(null)
  config.load()
  cacheProjects.load()
})
const c = computed(() => config.data)
const busy = (setting) => settings.busy === setting
const onOff = (v) => (v ? 'on' : 'off')
const errorText = computed(() => {
  const e = settings.error
  if (!e) return null
  return e.hint ? `${e.message} ${e.hint}` : e.message
})

// --- search paths -----------------------------------------------------------
const showAllPaths = ref(false)
const paths = computed(() => (showAllPaths.value ? c.value?.search_paths : c.value?.search_paths.slice(0, 6)) || [])
const newPath = ref('')
async function addPath() {
  const path = newPath.value.trim()
  if (!path) return
  const data = await applySetting('paths.add', { path }, { done: (out) => (out?.changed ? `Added ${out.path}` : `${out?.path} was already a search path`) })
  if (data) newPath.value = ''
}
const removePath = (path) => applySetting('paths.remove', { path }, { done: `Removed ${path}` })
// Prune is two steps: a dry run that names what would go (and why nothing can
// go while an instance is unchecked), then the apply the user asks for.
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
const showAllProjects = ref(false)
const cached = computed(() => {
  const rows = cacheProjects.data || []
  return showAllProjects.value ? rows : rows.slice(0, 6)
})
const clearProject = (name) => applySetting('cache.clear', { project: name }, { done: (out) => (out?.found ? `Forgot ${name}` : `${name} was not in the cache`) })

// --- auto-inspect -----------------------------------------------------------
const autoOn = computed({
  get: () => !!c.value?.auto_inspect.enabled,
  set: (v) => applySetting(v ? 'auto_inspect.enable' : 'auto_inspect.disable', {}, { done: `Auto-inspect ${v ? 'enabled' : 'disabled'}` }),
})
const intervalMinutes = ref(60)
watch(() => c.value?.auto_inspect.interval, (s) => (intervalMinutes.value = Math.max(1, Math.round((s || 3600) / 60))), { immediate: true })
const intervalChanged = computed(() => c.value && Math.round(intervalMinutes.value * 60) !== c.value.auto_inspect.interval)
const saveInterval = () =>
  applySetting('auto_inspect.enable', { interval: Math.round(Number(intervalMinutes.value) * 60) }, { done: (out) => `Inspecting every ${Math.round(out.state.interval / 60)} minutes` })
const autoAtBoot = computed({
  get: () => !!c.value?.auto_inspect.startup_enabled,
  set: (v) => applySetting('auto_inspect.enable', { at_boot: v }, { done: v ? 'Auto-inspect starts at boot' : 'Boot hook removed' }),
})

// Start (or Restart, when it runs) the daemon of an enabled feature: enabling
// starts it too, but a daemon can stop later and the switch would still read on.
const startDaemon = (feature, running, name) =>
  applySetting(`${feature}.${running ? 'restart' : 'start'}`, {}, { done: `${name} daemon ${running ? 'restarted' : 'started'}` })
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
</script>

<template>
  <PageHeader><PageHeaderTitle title="Settings" /></PageHeader>
  <div class="mx-auto max-w-3xl px-3 pb-20 pt-5 sm:px-5">
    <LoadingText v-if="!c && config.loading" />
    <ErrorMessage v-if="config.error" :message="config.error" />
    <div v-if="c" class="space-y-10">
      <p class="text-p-sm text-ink-gray-5">Changes apply to this machine's cwcli config at once. Each setting also names the terminal command that does the same.</p>
      <ErrorMessage :message="errorText" />

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Bench search paths</h2>
        <p class="mt-1 text-p-sm text-ink-gray-5">Where inspect looks for benches inside each container. {{ c.search_paths.length }} {{ c.search_paths.length === 1 ? 'path' : 'paths' }}.</p>
        <div v-if="c.search_paths.length" class="mt-3 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
          <div v-for="p in paths" :key="p" class="flex items-center justify-between gap-2 py-1 pl-3 pr-1">
            <code class="truncate font-mono text-sm text-ink-gray-7">{{ p }}</code>
            <Button size="xs" variant="ghost" icon="lucide-x" :aria-label="`Remove ${p}`" :loading="busy('paths.remove')" @click="removePath(p)" />
          </div>
          <button v-if="c.search_paths.length > 6" class="w-full px-3 py-2 text-left text-sm text-ink-gray-5 hover:bg-surface-gray-1" @click="showAllPaths = !showAllPaths">
            {{ showAllPaths ? 'Show fewer' : `Show all ${c.search_paths.length}` }}
          </button>
        </div>
        <form class="mt-3 flex items-end gap-2" @submit.prevent="addPath">
          <FormControl v-model="newPath" class="flex-1" label="Add a path" placeholder="/workspace/other-bench" autocomplete="off" />
          <Button type="submit" label="Add" :disabled="!newPath.trim()" :loading="busy('paths.add')" />
        </form>
        <div class="mt-4 rounded-6 border border-outline-gray-1 p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div class="text-base text-ink-gray-8">Unreferenced paths</div>
              <div class="mt-1 text-sm text-ink-gray-5">Checks every path live against every instance. A path a stopped instance might still use is never removed.</div>
            </div>
            <Button label="Check" icon-left="lucide-search" :loading="busy('paths.prune') && !prune" :disabled="!c.search_paths.length" @click="checkPrune" />
          </div>
          <div v-if="prune" class="mt-3 space-y-2">
            <template v-if="prunable.length && !prune.applied">
              <p class="text-sm text-ink-gray-7">{{ prunable.length }} {{ prunable.length === 1 ? 'path is' : 'paths are' }} absent from every instance:</p>
              <code v-for="p in prunable" :key="p" class="block truncate font-mono text-sm text-ink-gray-7">{{ p }}</code>
              <Button theme="red" :label="`Remove ${prunable.length} ${prunable.length === 1 ? 'path' : 'paths'}`" icon-left="lucide-trash-2" :loading="busy('paths.prune')" @click="applyPrune" />
            </template>
            <p v-else-if="prune.applied && prune.pruned.length" class="text-sm text-ink-green-6">Removed {{ prune.pruned.length }} {{ prune.pruned.length === 1 ? 'path' : 'paths' }}.</p>
            <p v-else-if="prune.unchecked_instances.length" class="text-sm text-ink-amber-6">Nothing can be confirmed dead while {{ prune.unchecked_instances.join(', ') }} {{ prune.unchecked_instances.length === 1 ? 'is' : 'are' }} stopped or unreachable. Start {{ prune.unchecked_instances.length === 1 ? 'it' : 'them' }} and check again.</p>
            <p v-else class="text-sm text-ink-gray-5">Every search path is still referenced by an instance.</p>
          </div>
        </div>
        <div class="mt-3"><CommandLine command="cwcli config paths prune" /></div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Cache</h2>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="py-3">
            <Switch v-model="cacheOn" label="Keep a cache of benches, sites and apps" :description="c.cache_env_override ? `CWCLI_NO_CACHE is set in the daemon's environment, so this setting is inert until it is unset.` : c.cache_db" :disabled="c.cache_env_override || busy('cache.enable') || busy('cache.disable')" />
          </div>
          <div class="py-3">
            <div class="flex items-center justify-between gap-4">
              <div><div class="text-base text-ink-gray-8">Remembered projects</div>
                <div class="mt-1 text-sm text-ink-gray-5">{{ cacheProjects.data?.length ?? '...' }} {{ cacheProjects.data?.length === 1 ? 'project' : 'projects' }}, including removed instances. Forgetting one drops its benches, sites and apps from the cache; the next inspect re-reads them.</div></div>
              <Button theme="red" label="Clear everything" icon-left="lucide-trash-2" :disabled="!cacheProjects.data?.length" @click="clearOpen = true" />
            </div>
            <div v-if="cacheProjects.data?.length" class="mt-3 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
              <div v-for="p in cached" :key="p.name" class="flex items-center justify-between gap-2 py-1 pl-3 pr-1">
                <div class="min-w-0"><span class="font-mono text-sm text-ink-gray-7">{{ p.name }}</span> <span class="text-xs text-ink-gray-5">· {{ p.last_updated }}</span></div>
                <Button size="xs" variant="ghost" label="Forget" :loading="busy('cache.clear')" @click="clearProject(p.name)" />
              </div>
              <button v-if="cacheProjects.data.length > 6" class="w-full px-3 py-2 text-left text-sm text-ink-gray-5 hover:bg-surface-gray-1" @click="showAllProjects = !showAllProjects">
                {{ showAllProjects ? 'Show fewer' : `Show all ${cacheProjects.data.length}` }}
              </button>
            </div>
          </div>
          <div class="py-3">
            <Switch v-model="autoOn" label="Auto-inspect" :description="`Refresh the cache in the background · daemon ${c.auto_inspect.daemon_running ? 'running' : 'not running'}`" :disabled="busy('auto_inspect.enable') || busy('auto_inspect.disable')" />
            <div v-if="c.auto_inspect.enabled" class="mt-3 flex flex-wrap items-end gap-3 pl-0.5">
              <FormControl v-model="intervalMinutes" class="w-40" type="number" label="Every (minutes)" :min="1" />
              <Button label="Save interval" :disabled="!intervalChanged || Number(intervalMinutes) < 1" :loading="busy('auto_inspect.enable')" @click="saveInterval" />
              <Checkbox v-model="autoAtBoot" label="Start at boot" :disabled="busy('auto_inspect.enable')" />
            </div>
            <div v-if="c.auto_inspect.enabled" class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 pl-0.5">
              <Button :label="c.auto_inspect.daemon_running ? 'Restart daemon' : 'Start daemon'" :icon-left="c.auto_inspect.daemon_running ? 'lucide-rotate-cw' : 'lucide-play'" :variant="c.auto_inspect.daemon_running ? 'subtle' : 'solid'" :loading="daemonBusy('auto_inspect')" @click="startDaemon('auto_inspect', c.auto_inspect.daemon_running, 'Auto-inspect')" />
              <code v-if="!c.auto_inspect.daemon_running && c.auto_inspect.last_log_line" class="min-w-0 basis-full break-words font-mono text-xs text-ink-gray-5" :title="c.auto_inspect.log_file">Last log: {{ c.auto_inspect.last_log_line }}</code>
            </div>
          </div>
        </div>
        <div class="mt-3 space-y-2">
          <CommandLine :command="c.auto_inspect.enabled ? 'cwcli config auto-inspect disable' : 'cwcli config auto-inspect enable'" />
          <CommandLine command="cwcli config cache clear --all" />
        </div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Credential bridge</h2>
        <p class="mt-1 text-p-sm text-ink-gray-5">Lets git inside containers use this machine's gh and glab sign-ins for private repositories. The token never enters a container.</p>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="py-3">
            <Switch v-model="bridgeOn" label="Persistent bridge" :description="c.cred_bridge.enabled ? (c.cred_bridge.daemon_running ? `daemon running · ${c.cred_bridge.transport} transport` : 'enabled, but the daemon is not running') : 'off; per-command bridging still works'" :disabled="busy('cred_bridge.enable') || busy('cred_bridge.disable')" />
            <div v-if="c.cred_bridge.enabled" class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 pl-0.5">
              <Button :label="c.cred_bridge.daemon_running ? 'Restart daemon' : 'Start daemon'" :icon-left="c.cred_bridge.daemon_running ? 'lucide-rotate-cw' : 'lucide-play'" :variant="c.cred_bridge.daemon_running ? 'subtle' : 'solid'" :loading="daemonBusy('cred_bridge')" @click="startDaemon('cred_bridge', c.cred_bridge.daemon_running, 'Credential bridge')" />
              <Checkbox v-model="bridgeAtBoot" label="Start at boot" :disabled="busy('cred_bridge.enable')" />
              <code v-if="!c.cred_bridge.daemon_running && c.cred_bridge.last_log_line" class="min-w-0 basis-full break-words font-mono text-xs text-ink-gray-5" :title="c.cred_bridge.log_file">Last log: {{ c.cred_bridge.last_log_line }}</code>
            </div>
          </div>
          <div class="py-3">
            <div class="text-base text-ink-gray-8">Allowed hosts</div>
            <div class="mt-2 flex flex-wrap gap-1.5"><Badge v-for="h in c.cred_bridge.allowed_hosts" :key="h" variant="outline" theme="gray" :label="h" /></div>
            <div class="mt-2 text-sm text-ink-gray-5">Edit <code class="font-mono">[cred_bridge] allowed_hosts</code> in the config file to change them.</div>
          </div>
          <div v-if="c.cred_bridge.recent_audit?.length" class="py-3">
            <div class="text-base text-ink-gray-8">Recent requests</div>
            <div class="mt-2 max-h-40 overflow-y-auto rounded-4 bg-surface-gray-1 p-2">
              <div v-for="line in [...c.cred_bridge.recent_audit].reverse()" :key="line" class="font-mono text-xs leading-5 text-ink-gray-6">{{ line }}</div>
            </div>
          </div>
        </div>
        <div class="mt-3 space-y-2">
          <CommandLine :command="c.cred_bridge.enabled ? 'cwcli config cred-bridge disable' : 'cwcli config cred-bridge enable'" />
          <CommandLine v-if="c.cred_bridge.enabled" command="cwcli config cred-bridge start" />
        </div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Agent hooks</h2>
        <p class="mt-1 text-p-sm text-ink-gray-5">Installs the cwcli SessionStart hook into every agent harness found on this machine (Claude Code, Codex, OpenCode), so an agent opens with the <code class="font-mono">cwcli axi</code> surface in its context. Only existing harness configs are touched; a moved cwcli is repaired in place.</p>
        <div class="mt-3 flex items-center gap-2">
          <Button label="Install or repair hooks" icon-left="lucide-bot" :loading="busy('agent_hooks.install')" @click="installHooks" />
        </div>
        <div v-if="hooks" class="mt-3 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
          <div v-for="h in hooks" :key="h.agent" class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
            <span class="w-28 text-sm text-ink-gray-8">{{ h.agent }}</span>
            <Badge size="sm" :theme="HOOK_THEME[h.status] || 'gray'" :label="h.status" />
            <code class="min-w-0 flex-1 truncate font-mono text-xs text-ink-gray-5" :title="h.path">{{ h.path }}</code>
            <span v-if="h.detail" class="basis-full text-sm text-ink-gray-6">{{ h.detail }}</span>
          </div>
        </div>
        <div class="mt-3"><CommandLine command="cwcli axi setup" /></div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">cwcli</h2>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="flex items-center justify-between gap-4 py-3">
            <div><div class="text-base text-ink-gray-8">Version {{ session.meta?.version || 'unknown' }}</div>
              <div class="mt-1 text-sm text-ink-gray-5">web UI protocol {{ session.meta?.protocol ?? 'unknown' }} · config {{ c.config_file }}</div></div>
            <Button icon-left="lucide-arrow-up-circle" label="Update cwcli" @click="openJob('self-update')" />
          </div>
          <div class="py-3">
            <Switch v-model="tipsOn" label="Show tips after commands" :description="`Currently ${onOff(c.show_tips)}`" :disabled="busy('tips.enable') || busy('tips.disable')" />
          </div>
        </div>
        <div class="mt-3 space-y-2"><CommandLine command="cwcli self-update --check" /><CommandLine command="cwcli config show" /></div>
      </section>
    </div>

    <Dialog v-model:open="clearOpen" title="Clear the whole cache?" icon="lucide-trash-2" theme="red">
      <p class="text-p-base text-ink-gray-7">Every remembered bench, site and app is forgotten, for live and removed instances alike. Bench labels stay in their marker files and come back on the next inspect; durable bench numbers are kept.</p>
      <div class="mt-3"><CommandLine command="cwcli config cache clear --all" /></div>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2">
          <Button label="Cancel" @click="close" />
          <Button variant="solid" theme="red" label="Clear everything" :loading="busy('cache.clear')" @click="clearAll" />
        </div>
      </template>
    </Dialog>
  </div>
</template>
