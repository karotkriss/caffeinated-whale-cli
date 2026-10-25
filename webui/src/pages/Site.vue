<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { PageHeader, Breadcrumbs, Button, Dropdown, Badge, Tooltip, LoadingText, Alert } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import CommandLine from '../components/CommandLine.vue'
import { instance as findInstance, loadDetail, benchName, parseApp, setFocus, getJsonUrl } from '../data/fleet'
import { openAction, menuOption, dangerGroup } from '../data/dialog'
import { openJob } from '../data/job-dialog'

const route = useRoute()
const { project, bench: index, site: siteName } = route.params
onMounted(() => setFocus(project))
const inst = computed(() => findInstance(project))
const detail = loadDetail(project)
const bench = computed(() => detail.data?.benches.find((b) => String(b.index) === String(index)))
const site = computed(() => bench.value?.sites.find((s) => s.name === siteName))
const apps = computed(() => (site.value?.installed_apps || []).map(parseApp))
const health = computed(() => inst.value?.benches?.find((b) => String(b.index) === String(index)))
const running = computed(() => inst.value?.container_running)
const ctx = computed(() => ({ project, bench: String(index), benchName: bench.value ? benchName(bench.value) : index, site: siteName }))
const jobOption = (label, icon, verb) => ({ label: running.value ? label : `${label} (needs the instance running)`, icon, disabled: !running.value, onClick: () => openJob(verb, ctx.value) })
const more = computed(() => [
  jobOption('Back up site', 'lucide-database-backup', 'backup'),
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
    probe.value.data = await getJsonUrl(project, q)
    probe.value.error = null
  } catch (e) {
    probe.value.error = e
  } finally {
    probe.value.loading = false
  }
}
// This site's own maintenance flag is only known when it is the probed site.
const maintenance = computed(() => (health.value?.web_site === siteName ? health.value.maintenance_mode : null))
</script>

<template>
  <PageHeader>
    <Breadcrumbs :items="[
      { label: 'Instances', route: '/instances' },
      { label: project, route: `/instances/${project}` },
      { label: bench ? benchName(bench) : `#${index}`, route: `/instances/${project}/benches/${index}` },
      { label: siteName, route: route.path },
    ]" />
    <div class="flex items-center gap-2">
      <Tooltip :text="running ? 'Probe the host URL and open it' : 'Start the instance to open the site'">
        <Button variant="solid" icon-left="lucide-external-link" label="Open site" :disabled="!running" @click="openAction('site.open', ctx)" />
      </Tooltip>
      <Dropdown :options="more" align="end"><Button icon="lucide-ellipsis" aria-label="More actions" /></Dropdown>
    </div>
  </PageHeader>

  <LoadingText v-if="detail.loading && !detail.data" class="px-3 pt-5 sm:px-5" text="Reading the cache..." />
  <div v-else-if="!site" class="px-3 pt-5 sm:px-5">
    <Alert theme="gray" :title="`${siteName} is not in the cache for bench #${index}`" :description="detail.error ? detail.error.message : 'A full inspect re-reads the sites from the container.'" />
    <div class="mt-4 max-w-sm"><CommandLine :command="`cwcli inspect ${project} --update`" /></div>
  </div>
  <div v-else class="mx-auto max-w-4xl space-y-8 px-3 pb-20 pt-5 sm:px-5">
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
          <template v-else-if="probe.data && probe.data.url">{{ probe.data.reachable ? `answers HTTP ${probe.data.http_code}` : 'resolved, but did not answer' }}</template>
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
      <List class="-mx-3 list-row-px-3 mt-3" :columns="{ base: ['minmax(0,1fr)', '6rem'], md: ['minmax(10rem,1fr)', '7rem', 'minmax(0,1.4fr)'] }" :row-height="44">
        <ListHeader>
          <ListHeaderCell>App</ListHeaderCell>
          <ListHeaderCell>Version</ListHeaderCell>
          <ListHeaderCell class="max-md:hidden">Branch at install</ListHeaderCell>
        </ListHeader>
        <ListRows :items="apps" row-key="name" v-slot="{ item }">
          <ListRow>
            <ListCell><span class="font-mono text-sm text-ink-gray-8">{{ item.name }}</span></ListCell>
            <ListCell><span class="tabular-nums text-sm text-ink-gray-6">{{ item.version || 'unknown' }}</span></ListCell>
            <ListCell class="max-md:hidden"><span class="truncate font-mono text-sm text-ink-gray-6">{{ item.branch || 'unknown' }}</span></ListCell>
          </ListRow>
        </ListRows>
      </List>
    </section>
  </div>
</template>
