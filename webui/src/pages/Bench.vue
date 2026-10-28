<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Dropdown, Tabs, TabList, TabTrigger, TabPanel, Alert, LoadingText } from 'frappe-ui'
import StatusBadge from '../components/StatusBadge.vue'
import PageHero from '../components/PageHero.vue'
import PerformancePanel from '../components/PerformancePanel.vue'
import ProcessTable from '../components/ProcessTable.vue'
import SitesTable from '../components/SitesTable.vue'
import AppsTable from '../components/AppsTable.vue'
import LogsPane from '../components/LogsPane.vue'
import CommandLine from '../components/CommandLine.vue'
import { instance as findInstance, loadDetail, benchName, setFocus, ago } from '../data/fleet'
import { openAction, menuOption, dangerGroup } from '../data/dialog'
import { openJob } from '../data/job-dialog'
import { openNewSite } from '../data/new-site-dialog'
import { openTransfer } from '../data/transfer'
import { useTabQuery } from '../data/tabs'
import { can } from '../data/session'

const route = useRoute()
const { project, bench: index } = route.params
onMounted(() => setFocus(project))
const inst = computed(() => findInstance(project))
const detail = loadDetail(project)
const bench = computed(() => detail.data?.benches.find((b) => String(b.index) === String(index)))
const health = computed(() => inst.value?.benches?.find((b) => String(b.index) === String(index)))
const running = computed(() => inst.value?.container_running)
// The latest probe failed: every health reading on the page is the last successful probe's, not live.
const stale = computed(() => (running.value ? inst.value?.probe_error : null))
const staleNote = computed(() => {
  const why = `${String(stale.value).replace(/\.$/, '')}.`
  return inst.value?.probed_at ? `${why} The health readings on this page (web port check, process rows, status badge) are from the last successful probe, ${ago(inst.value.probed_at * 1000)}.` : why
})
const tab = useTabQuery('processes')
const logsProcess = ref(route.query.process || null)
const down = computed(() => (health.value?.processes || []).filter((p) => !p.up))
const name = computed(() => (bench.value ? benchName(bench.value) : `#${index}`))
const benchUp = computed(() => ['running', 'degraded'].includes(health.value?.overall))
const ctx = computed(() => ({ project, bench: String(index), benchName: name.value }))
const jobOption = (label, icon, verb) => ({ label, icon, onClick: () => openJob(verb, ctx.value) })
const more = computed(() => [
  menuOption('bench.restart', ctx.value, health.value),
  jobOption('Back up default site', 'lucide-database-backup', 'backup'),
  jobOption('Install apps', 'lucide-package-plus', 'apps-install'),
  jobOption('Update apps', 'lucide-package-check', 'apps-update'),
  jobOption('Migrate default site', 'lucide-database-zap', 'migrate'),
  jobOption('Build assets', 'lucide-hammer', 'build'),
  jobOption('Complete setup wizard', 'lucide-list-checks', 'setup-wizard'),
  menuOption('bench.label', ctx.value, health.value),
  dangerGroup(['bench.remove'], ctx.value, health.value),
])

function showLogs(process) {
  logsProcess.value = process
  tab.value = 'logs'
}
const heroFacts = computed(() => bench.value ? [
  { label: 'Identity', value: `--bench ${bench.value.index}`, mono: true },
  { label: 'Path', value: bench.value.path, mono: true },
  { label: 'Web port', value: health.value?.web_port != null ? `${health.value.web_port}${health.value.web_http_code && !stale.value ? ' · HTTP ' + health.value.web_http_code : stale.value ? ' · unknown' : ''}` : 'unknown', mono: true },
  { label: 'Default site', value: bench.value.default_site || 'none set' },
  { label: 'Sites', value: String(bench.value.sites.length) },
  { label: 'Apps', value: String(bench.value.available_apps.length) },
] : [])
</script>

<template>
  <PageHero icon="lucide-layers" :title="name" :facts="heroFacts" :crumbs="[
      { label: 'Instances', route: '/instances' },
      { label: project, route: `/instances/${project}` },
      { label: name, route: route.path },
    ]">
    <template #badge><StatusBadge :token="running ? (stale ? 'unknown' : health?.overall) : 'offline'" size="lg" /></template>
    <template #actions>
      <template v-if="can('operator')">
        <Button v-if="benchUp" icon-left="lucide-square" label="Stop bench" @click="openAction('bench.stop', ctx)" />
        <Button v-else variant="solid" icon-left="lucide-play" label="Start bench" @click="openAction('bench.start', ctx)" />
      </template>
      <Dropdown :options="more" align="end"><Button icon="lucide-ellipsis" aria-label="More actions" /></Dropdown>
    </template>
  </PageHero>

  <LoadingText v-if="detail.loading && !detail.data" class="px-3 pt-5 sm:px-5" text="Reading the cache..." />
  <div v-else-if="!bench" class="px-3 pt-5 sm:px-5">
    <Alert theme="gray" :title="`Bench #${index} is not in the cache for ${project}`" :description="detail.error ? detail.error.message : 'A full inspect re-reads the benches from the container.'" />
    <div class="mt-4 max-w-sm"><CommandLine :command="`cwcli inspect ${project} --update`" /></div>
  </div>
  <div v-else class="space-y-5 px-3 pb-20 pt-5 sm:px-5">
    <Alert v-if="stale" theme="gray" title="The last health probe failed" :description="staleNote" />
    <Alert
      v-if="running && benchUp && down.length"
      theme="amber"
      :title="`${down.map((p) => p.label).join(', ')} ${down.length > 1 ? 'are' : 'is'} down`"
      :description="`${down[0].label} is ${down[0].state || 'down'}${down[0].state === 'FATAL' ? ': supervisord gave up restarting it' : ''}. Its log usually names the cause.`"
      :primary-action="{ label: `Restart ${down[0].label}`, onClick: () => openAction('process.restart', { ...ctx, process: down[0].label }) }"
      :secondary-action="{ label: 'Open its log', onClick: () => showLogs(down[0].label) }"
    />
    <Alert
      v-if="running && health?.maintenance_mode"
      theme="amber"
      :title="`${health.web_site || 'The probed site'} is in maintenance mode`"
      description="It answers HTTP 503 until maintenance mode is turned off. An interrupted migrate leaves it this way."
      :primary-action="{ label: 'Take it out of maintenance', onClick: () => openAction('site.maintenance_off', { ...ctx, site: health.web_site }) }"
    />

    <Tabs v-model="tab">
      <TabList>
        <TabTrigger value="processes" label="Processes" />
        <TabTrigger value="sites" label="Sites"><template #suffix><span class="text-xs text-ink-gray-5">{{ bench.sites.length }}</span></template></TabTrigger>
        <TabTrigger value="apps" label="Apps"><template #suffix><span class="text-xs text-ink-gray-5">{{ bench.available_apps.length }}</span></template></TabTrigger>
        <TabTrigger value="logs" label="Logs" />
        <TabTrigger value="performance" label="Performance" />
      </TabList>
      <TabPanel value="processes" class="pt-4">
        <ProcessTable v-if="running && health" :bench="health" :project="project" @logs="showLogs" />
        <div v-else-if="running" class="px-3 py-8 text-center text-p-sm text-ink-gray-5">Probing this bench's processes...</div>
        <div v-else class="rounded-6 border border-outline-gray-1 px-4 py-10 text-center">
          <p class="text-base text-ink-gray-7">No process health while {{ project }} is stopped.</p>
          <div class="mx-auto mt-4 max-w-sm"><CommandLine :command="`cwcli start ${project} --bench ${bench.index}`" /></div>
        </div>
      </TabPanel>
      <TabPanel value="sites" class="pt-4">
        <div v-if="can('operator')" class="mb-3 flex justify-end gap-2"><Button variant="subtle" icon-left="lucide-copy" label="Clone or move sites…" @click="openTransfer({ mode: 'clone', project, bench: bench.index, pick: true })" /><Button variant="subtle" icon-left="lucide-plus" label="New site" @click="openNewSite({ project, bench: bench.index })" /></div>
        <SitesTable :bench="bench" :dev-modes="detail.data?.dev_modes || {}" :route="(s) => `/instances/${project}/benches/${bench.index}/sites/${s.name}`" />
      </TabPanel>
      <TabPanel value="apps" class="pt-4">
        <AppsTable :bench="bench" :project="project" />
        <p class="mt-3 text-p-sm text-ink-gray-5">Versions and branches are what bench recorded at install time. git is the authority on what is checked out now.</p>
      </TabPanel>
      <TabPanel value="logs" class="pt-4">
        <LogsPane v-if="running" :key="logsProcess || 'all'" :project="project" :bench="String(bench.index)" :process="logsProcess" />
        <div v-else class="rounded-6 border border-outline-gray-1 px-4 py-10 text-center">
          <p class="text-base text-ink-gray-7">Logs need {{ project }} running.</p>
          <div class="mx-auto mt-4 max-w-md"><CommandLine :command="`cwcli logs ${project} --bench ${bench.index} -f`" /></div>
        </div>
      </TabPanel>
      <TabPanel value="performance" class="pt-4">
        <PerformancePanel :project="project" :bench="String(bench.index)" />
      </TabPanel>
    </Tabs>
  </div>
</template>
