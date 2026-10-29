<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Dropdown, Badge, Tooltip, LoadingText, Alert } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import CommandLine from '../components/CommandLine.vue'
import PageHero from '../components/PageHero.vue'
import AppIcon from '../components/AppIcon.vue'
import SiteLogsPane from '../components/SiteLogsPane.vue'
import SiteBackupsPane from '../components/SiteBackupsPane.vue'
import DevModeBadge from '../components/DevModeBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import RemoteHostBadge from '../components/RemoteHostBadge.vue'
import { apiBase, getJson } from '../data/api'
import { jobList } from '../data/jobs'
import { catalogApp, catalogAppFor, sourceLabel, sourceIcon, hostIcon } from '../data/catalog'
import { openApp } from '../data/app-panel'
import { instance as findInstance, loadDetail, benchName, parseApp, setFocus, getJsonUrl, instancePath, benchPath } from '../data/fleet'
import { openAction, menuOption, dangerGroup } from '../data/dialog'
import { openJob } from '../data/job-dialog'
import { openTransfer } from '../data/transfer'
import { can } from '../data/session'
import { hostName } from '../data/hosts'

const route = useRoute()
const { project, bench: index, site: siteName } = route.params
// A remote host's site: the same page, every read and action through the hub.
const host = route.params.host || null
const where = { project, host }
onMounted(() => setFocus(host ? null : project))
const inst = computed(() => findInstance(project, host))
const detail = loadDetail(project, { host })
const bench = computed(() => detail.data?.benches.find((b) => String(b.index) === String(index)))
const site = computed(() => bench.value?.sites.find((s) => s.name === siteName))
const apps = computed(() => (site.value?.installed_apps || []).map(parseApp))
const health = computed(() => inst.value?.benches?.find((b) => String(b.index) === String(index)))
const running = computed(() => inst.value?.container_running)
const ctx = computed(() => ({ project, host, bench: String(index), benchName: bench.value ? benchName(bench.value) : index, site: siteName }))
const jobOption = (label, icon, verb) => ({ label, icon, onClick: () => openJob(verb, ctx.value) })
// Clone and move are this machine's dialogs over its own backup store; a remote site keeps the rest.
const more = computed(() => [
  jobOption('Back up site', 'lucide-database-backup', 'backup'),
  ...(host ? [] : [{ label: 'Clone site…', icon: 'lucide-copy', onClick: () => openTransfer({ mode: 'clone', project, bench: String(index), sites: [siteName] }) }]),
  ...(!host && can('admin') ? [{ label: 'Move site…', icon: 'lucide-move', onClick: () => openTransfer({ mode: 'move', project, bench: String(index), sites: [siteName] }) }] : []),
  jobOption('Migrate site', 'lucide-database-zap', 'migrate'),
  jobOption('Run tests', 'lucide-flask-conical', 'run-tests'),
  jobOption('Complete setup wizard', 'lucide-list-checks', 'setup-wizard'),
  menuOption('site.unlock', ctx.value),
  menuOption('site.maintenance_off', ctx.value),
  dangerGroup(['site.remove'], ctx.value),
])

// The address is two hops (bench port from its config, then the host port Docker
// publishes), asked of the daemon fresh: never a guessed :8000.
const probe = ref({ loading: false, data: null, error: null })
async function checkUrl() {
  if (!running.value) return
  probe.value.loading = true
  try {
    const q = new URLSearchParams({ bench: String(index), site: siteName })
    probe.value.data = await getJsonUrl(project, q, host)
    probe.value.error = null
  } catch (e) {
    probe.value.error = e
  } finally {
    probe.value.loading = false
  }
}
// This site's own maintenance flag is only known when it is the probed site.
// Developer mode: the cache's answer at once (detail.dev_modes), then a live read
// of the two config files; re-read when a dev-mode job for this site finishes.
const devLive = ref(null)
const devMode = computed(() => devLive.value || detail.data?.dev_modes?.[`${bench.value?.path}/${siteName}`] || null)
async function readDevMode() {
  if (!bench.value) return
  try { devLive.value = await getJson(`${apiBase(host)}/instances/${encodeURIComponent(project)}/dev-mode?bench_path=${encodeURIComponent(bench.value.path)}&site=${encodeURIComponent(siteName)}`) } catch { devLive.value = null }
}
watch(bench, (b) => b && readDevMode(), { immediate: true })
const seenDevJobs = new Set()
watch(jobList, (list) => {
  for (const j of list) {
    if (j.verb === 'dev-mode' && (j.host || null) === host && j.target?.project === project && j.target?.site === siteName && j.state === 'succeeded' && !seenDevJobs.has(j.key)) {
      seenDevJobs.add(j.key)
      readDevMode()
      loadDetail(project, { force: true, host })
    }
  }
})
const maintenance = computed(() => (health.value?.web_site === siteName ? health.value.maintenance_mode : null))
const heroFacts = computed(() => [
  ...(host ? [{ label: 'Machine', value: hostName(host) }] : []),
  { label: 'Instance', value: project },
  { label: 'Bench', value: bench.value ? `${benchName(bench.value)} (--bench ${bench.value.index})` : `#${index}` },
  { label: 'Installed apps', value: String(apps.value.length) },
  { label: 'Maintenance mode', value: maintenance.value == null ? 'unknown' : maintenance.value ? 'on' : 'off' },
])
</script>

<template>
  <PageHero icon="lucide-globe" :title="siteName" :facts="heroFacts" :crumbs="[
      { label: 'Instances', route: '/instances' },
      ...(host ? [{ label: hostName(host), route: '/instances' }] : []),
      { label: project, route: instancePath(where) },
      { label: bench ? benchName(bench) : `#${index}`, route: benchPath(where, index) },
      { label: siteName, route: route.path },
    ]">
    <template #badge><StatusBadge v-if="running && health?.web_site === siteName" :token="health.overall" size="lg" /><Badge v-if="site?.name === bench?.default_site" size="lg" theme="gray" variant="outline" label="default site" /><DevModeBadge :state="devMode" size="lg" /><RemoteHostBadge :host="host" size="md" /></template>
    <template #actions>
      <Tooltip v-if="devMode && can('operator')" :text="devMode.source === 'common' ? 'Set bench-wide in common_site_config.json; the dialog can change it there' : 'Changes developer_mode for this site, then clears its cache'">
        <Button variant="subtle" :icon-left="devMode.effective ? 'lucide-code-xml' : 'lucide-code'" :label="devMode.effective ? 'Turn developer mode off' : 'Turn developer mode on'" @click="openAction(devMode.effective ? 'site.dev_mode_off' : 'site.dev_mode_on', ctx)" />
      </Tooltip>
      <Tooltip :text="running ? (host ? `Probe the URL on ${hostName(host)} and open it; it must resolve from this browser` : 'Probe the host URL and open it') : 'Start the instance to open the site'">
        <Button variant="solid" icon-left="lucide-external-link" label="Open site" @click="openAction('site.open', ctx)" />
      </Tooltip>
      <Dropdown :options="more" align="end"><Button icon="lucide-ellipsis" aria-label="More actions" /></Dropdown>
    </template>
  </PageHero>

  <LoadingText v-if="detail.loading && !detail.data" class="px-3 pt-5 sm:px-5" text="Reading the cache..." />
  <div v-else-if="!site" class="px-3 pt-5 sm:px-5">
    <Alert theme="gray" :title="`${siteName} is not in the cache for bench #${index}`" :description="detail.error ? detail.error.message : 'A full inspect re-reads the sites from the container.'" />
    <div class="mt-4 max-w-sm"><CommandLine :command="`cwcli inspect ${project} --update`" /></div>
  </div>
  <div v-else class="space-y-8 px-3 pb-20 pt-5 sm:px-5">
    <section class="grid gap-4 md:grid-cols-3">
      <div class="rounded-6 border border-outline-gray-1 p-4">
        <div class="flex items-center justify-between text-sm text-ink-gray-5">
          <span>Address</span>
          <Button v-if="running" size="xs" variant="ghost" icon="lucide-refresh-cw" aria-label="Check the address" :loading="probe.loading" @click="checkUrl" />
        </div>
        <div class="mt-1.5 break-all font-mono text-sm text-ink-gray-8">
          <template v-if="!running">not serving</template>
          <template v-else-if="probe.data">{{ probe.data.url || 'not serving' }}</template>
          <template v-else-if="probe.error">unknown</template>
          <template v-else>not checked</template>
        </div>
        <div class="mt-1 text-xs text-ink-gray-5">
          <template v-if="!running">the instance is stopped</template>
          <template v-else-if="probe.data && probe.data.url">{{ probe.data.reachable ? `answers HTTP ${probe.data.http_code}` : 'resolved, but did not answer' }}{{ host ? ` · an address on ${hostName(host)}` : '' }}</template>
          <template v-else-if="probe.data">no published port resolves for this bench</template>
          <template v-else-if="probe.error">{{ probe.error.message }}</template>
          <template v-else>resolved from Docker's published ports on request</template>
        </div>
      </div>
      <div class="rounded-6 border border-outline-gray-1 p-4">
        <div class="text-sm text-ink-gray-5">Maintenance mode</div>
        <div class="mt-1.5 text-base text-ink-gray-8">{{ maintenance == null ? 'unknown' : maintenance ? 'on' : 'off' }}</div>
        <div class="mt-1 text-xs text-ink-gray-5">{{ maintenance == null ? 'read live for the probed site while the bench serves' : 'read live while the bench serves' }}</div>
      </div>
      <div class="rounded-6 border border-outline-gray-1 p-4">
        <div class="text-sm text-ink-gray-5">Bench</div>
        <div class="mt-1.5 truncate text-base text-ink-gray-8">{{ benchName(bench) }} <span class="font-mono text-sm text-ink-gray-5">#{{ bench.index }}</span></div>
        <div class="mt-1 truncate font-mono text-xs text-ink-gray-5">{{ bench.path }}</div>
      </div>
    </section>

    <Alert v-if="maintenance" theme="amber" :title="`${siteName} is in maintenance mode`" description="It answers HTTP 503 until maintenance mode is turned off." :primary-action="{ label: 'Take it out of maintenance', onClick: () => openAction('site.maintenance_off', ctx) }" />

    <section>
      <div class="flex items-baseline gap-2">
        <h2 class="text-lg-semibold text-ink-gray-9">Installed apps</h2>
        <Badge size="sm" :theme="site.installed_apps_verified ? 'green' : 'gray'" variant="outline" :label="site.installed_apps_verified ? 'verified' : 'remembered'" />
      </div>
      <p class="mt-1 text-p-sm text-ink-gray-5">{{ site.installed_apps_verified ? 'Read live by a full inspect.' : 'From the cache. A full inspect re-reads them from the running container.' }}</p>
      <List class="-mx-3 list-row-px-3 mt-3" :columns="{ base: ['minmax(0,1fr)', '6rem'], md: ['minmax(10rem,1fr)', '7rem', 'minmax(0,1.4fr)', '8rem'] }" :row-height="44">
        <ListHeader>
          <ListHeaderCell>App</ListHeaderCell>
          <ListHeaderCell>Version</ListHeaderCell>
          <ListHeaderCell class="max-md:hidden">Branch at install</ListHeaderCell>
          <ListHeaderCell class="max-md:hidden">Source</ListHeaderCell>
        </ListHeader>
        <ListRows :items="apps" row-key="name" v-slot="{ item }">
          <ListRow @click="openApp(catalogApp(item.name))">
            <ListCell class="gap-2"><AppIcon :app="catalogApp(item.name)" size="sm" /><span class="text-base text-ink-gray-8">{{ catalogApp(item.name).title }}</span><span class="font-mono text-xs text-ink-gray-4">{{ item.name }}</span></ListCell>
            <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.version || 'unknown' }}</span></ListCell>
            <ListCell class="max-md:hidden"><span class="truncate font-mono text-sm text-ink-gray-6">{{ item.branch || 'unknown' }}</span></ListCell>
                      <!-- Captain note 53: where the app comes from, with the source's icon. The catalog is this machine's, so a remote site's source is the plain name. -->
            <ListCell class="gap-1.5 overflow-hidden max-md:hidden"><template v-if="!host"><img v-if="hostIcon(catalogAppFor(item.name, project, index).source?.host)" :src="hostIcon(catalogAppFor(item.name, project, index).source?.host)" class="size-3.5 rounded-sm" alt="" /><span v-else :class="sourceIcon(catalogAppFor(item.name, project, index))" class="size-3.5 text-ink-gray-5" aria-hidden="true" /><span class="truncate text-sm text-ink-gray-6">{{ sourceLabel(catalogAppFor(item.name, project, index)) }}</span></template><span v-else class="truncate text-sm text-ink-gray-5">on {{ hostName(host) }}</span></ListCell>
          </ListRow>
        </ListRows>
      </List>
    </section>

    <section id="backups">
      <div class="flex items-baseline gap-2">
        <h2 class="text-lg-semibold text-ink-gray-9">Backups</h2>
        <span class="text-xs text-ink-gray-5">{{ host ? `this site's sets in ${hostName(host)}'s store; restore and transfer run from that machine's own UI` : "this site's sets in the store outside the instance; they survive a removal" }}</span>
      </div>
      <div class="mt-3"><SiteBackupsPane :project="project" :host="host" :bench="String(index)" :bench-path="bench.path" :site="siteName" :running="!!running" /></div>
    </section>

    <section>
      <div class="flex items-baseline gap-2">
        <h2 class="text-lg-semibold text-ink-gray-9">Logs</h2>
        <span class="text-xs text-ink-gray-5">this site's own: its Frappe log files, its Error Log</span>
      </div>
      <div class="mt-3"><SiteLogsPane :project="project" :host="host" :bench="String(index)" :bench-path="bench.path" :site="siteName" :running="!!running" /></div>
    </section>
  </div>
</template>
