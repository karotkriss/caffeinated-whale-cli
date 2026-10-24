<script setup>
import { computed, onMounted, ref } from 'vue'
import { PageHeader, PageHeaderTitle, Badge, LoadingText, ErrorMessage } from 'frappe-ui'
import CommandLine from '../components/CommandLine.vue'
import { config, cacheProjects, setFocus } from '../data/fleet'
import { session } from '../data/session'

// Read-only in Phase 1: every row shows the cwcli command that changes it.
onMounted(() => {
  setFocus(null)
  config.load()
  cacheProjects.load()
})
const c = computed(() => config.data)
const showAllPaths = ref(false)
const paths = computed(() => (showAllPaths.value ? c.value?.search_paths : c.value?.search_paths.slice(0, 6)) || [])
const onOff = (v) => (v ? 'on' : 'off')
</script>

<template>
  <PageHeader><PageHeaderTitle title="Settings" /></PageHeader>
  <div class="mx-auto max-w-3xl px-3 pb-20 pt-5 sm:px-5">
    <LoadingText v-if="!c && config.loading" />
    <ErrorMessage v-if="config.error" :message="config.error" />
    <div v-if="c" class="space-y-10">
      <p class="text-p-sm text-ink-gray-5">Read-only here. Each setting names the terminal command that changes it.</p>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Bench search paths</h2>
        <p class="mt-1 text-p-sm text-ink-gray-5">Where inspect looks for benches inside each container. {{ c.search_paths.length }} {{ c.search_paths.length === 1 ? 'path' : 'paths' }}.</p>
        <div v-if="c.search_paths.length" class="mt-3 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
          <div v-for="p in paths" :key="p" class="flex items-center px-3 py-2"><code class="font-mono text-sm text-ink-gray-7">{{ p }}</code></div>
          <button v-if="c.search_paths.length > 6" class="w-full px-3 py-2 text-left text-sm text-ink-gray-5 hover:bg-surface-gray-1" @click="showAllPaths = !showAllPaths">
            {{ showAllPaths ? 'Show fewer' : `Show all ${c.search_paths.length}` }}
          </button>
        </div>
        <div class="mt-3"><CommandLine command="cwcli config paths add /workspace/other-bench" /></div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Cache</h2>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="flex items-center justify-between gap-4 py-3">
            <div><div class="text-base text-ink-gray-8">Keep a cache of benches, sites and apps</div>
              <div class="mt-1 font-mono text-xs text-ink-gray-5">{{ c.cache_db }}</div></div>
            <Badge :theme="c.cache_enabled ? 'green' : 'gray'" :label="c.cache_env_override ? `${onOff(c.cache_enabled)} (CWCLI_NO_CACHE)` : onOff(c.cache_enabled)" />
          </div>
          <div class="flex items-center justify-between gap-4 py-3">
            <div><div class="text-base text-ink-gray-8">Cached projects</div>
              <div class="mt-1 text-sm text-ink-gray-5">{{ cacheProjects.data?.length ?? '...' }} projects, including removed instances</div></div>
          </div>
          <div class="flex items-center justify-between gap-4 py-3">
            <div><div class="text-base text-ink-gray-8">Auto-inspect</div>
              <div class="mt-1 text-sm text-ink-gray-5">Refresh every {{ Math.round(c.auto_inspect.interval / 60) }} minutes in the background · daemon {{ c.auto_inspect.daemon_running ? 'running' : 'not running' }}</div></div>
            <Badge :theme="c.auto_inspect.enabled ? 'green' : 'gray'" :label="onOff(c.auto_inspect.enabled)" />
          </div>
        </div>
        <div class="mt-3"><CommandLine :command="c.auto_inspect.enabled ? 'cwcli config auto-inspect disable' : 'cwcli config auto-inspect enable'" /></div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">Credential bridge</h2>
        <p class="mt-1 text-p-sm text-ink-gray-5">Lets git inside containers use this machine's gh and glab sign-ins for private repositories. The token never enters a container.</p>
        <div class="mt-3 divide-y divide-outline-gray-1">
          <div class="flex items-center justify-between gap-4 py-3">
            <div><div class="text-base text-ink-gray-8">Persistent bridge</div>
              <div class="mt-1 text-sm" :class="c.cred_bridge.enabled && !c.cred_bridge.daemon_running ? 'text-ink-amber-6' : 'text-ink-gray-5'">
                {{ c.cred_bridge.enabled ? (c.cred_bridge.daemon_running ? 'daemon running' : 'enabled, but the daemon is not running') : 'off; per-command bridging still works' }}<template v-if="c.cred_bridge.transport"> · {{ c.cred_bridge.transport }} transport</template></div></div>
            <Badge :theme="c.cred_bridge.enabled ? 'green' : 'gray'" :label="onOff(c.cred_bridge.enabled)" />
          </div>
          <div class="py-3">
            <div class="text-base text-ink-gray-8">Allowed hosts</div>
            <div class="mt-2 flex flex-wrap gap-1.5"><Badge v-for="h in c.cred_bridge.allowed_hosts" :key="h" variant="outline" theme="gray" :label="h" /></div>
          </div>
          <div v-if="c.cred_bridge.recent_audit?.length" class="py-3">
            <div class="text-base text-ink-gray-8">Recent requests</div>
            <div class="mt-2 max-h-40 overflow-y-auto rounded-4 bg-surface-gray-1 p-2">
              <div v-for="line in [...c.cred_bridge.recent_audit].reverse()" :key="line" class="font-mono text-xs leading-5 text-ink-gray-6">{{ line }}</div>
            </div>
          </div>
        </div>
        <div class="mt-3"><CommandLine :command="c.cred_bridge.enabled ? 'cwcli config cred-bridge disable' : 'cwcli config cred-bridge enable'" /></div>
      </section>

      <section>
        <h2 class="text-lg-semibold text-ink-gray-9">cwcli</h2>
        <div class="mt-3 flex items-center justify-between gap-4">
          <div><div class="text-base text-ink-gray-8">Version {{ session.meta?.version || 'unknown' }}</div>
            <div class="mt-1 text-sm text-ink-gray-5">web UI protocol {{ session.meta?.protocol ?? 'unknown' }} · tips {{ onOff(c.show_tips) }} · config {{ c.config_file }}</div></div>
        </div>
        <div class="mt-3 space-y-2"><CommandLine command="cwcli self-update --check" /><CommandLine command="cwcli config show" /></div>
      </section>
    </div>
  </div>
</template>
