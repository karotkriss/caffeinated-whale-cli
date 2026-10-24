<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { PageHeader, PageHeaderTitle, Button, Dropdown, Badge, Tooltip, Alert, LoadingText } from 'frappe-ui'
import { List, ListHeader, ListHeaderCell, ListRows, ListRow, ListCell } from 'frappe-ui/list'
import StatusBadge from '../components/StatusBadge.vue'
import CommandLine from '../components/CommandLine.vue'
import { fleet, instances, details, publishedRange, countSites, doctor, cacheProjects, setFocus } from '../data/fleet'
import { openAction, menuOption } from '../data/dialog'
import { openJob } from '../data/job-dialog'

const router = useRouter()
onMounted(() => {
  setFocus(null)
  cacheProjects.load()
  doctor.load()
})
const live = computed(() => new Set(instances.value.map((i) => i.project)))
const remembered = computed(() => (cacheProjects.data || []).filter((p) => !live.value.has(p.name)))
const benchCount = (p) => details[p]?.data?.benches?.length
const menu = (i) => [
  menuOption('instance.restart', { project: i.project }),
  menuOption('instance.refresh', { project: i.project }),
  menuOption('instance.scale', { project: i.project }),
]
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Instances" />
    <Button variant="solid" icon-left="lucide-plus" label="New instance" @click="openJob('init')" />
  </PageHeader>
  <div class="space-y-8 px-3 pb-20 pt-5 sm:px-5">
    <Alert
      v-if="doctor.data && (doctor.data.warned || doctor.data.failed)"
      :theme="doctor.data.failed ? 'red' : 'amber'"
      :title="`Health check: ${doctor.data.failed ? doctor.data.failed + ' failed, ' : ''}${doctor.data.warned} warnings`"
      :description="doctor.data.checks.filter((c) => c.status !== 'pass').map((c) => `${c.title}: ${c.detail}`).join(' · ')"
      :primary-action="{ label: 'Open health check', onClick: () => router.push('/health') }"
    />
    <LoadingText v-if="fleet.connection === 'connecting' && !instances.length" text="Waiting for the daemon..." />
    <div v-else-if="!instances.length" class="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <div class="rounded-full bg-surface-gray-2 p-3 text-ink-gray-5"><span class="lucide-boxes size-6" aria-hidden="true" /></div>
      <p class="text-base text-ink-gray-7">No instances on this Docker daemon</p>
      <p class="text-sm text-ink-gray-5">Create one here. The detached job keeps going if you close this tab.</p>
      <Button variant="solid" icon-left="lucide-plus" label="Create instance" @click="openJob('init')" />
    </div>
    <List v-else class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '6.5rem', '2.5rem'], lg: ['minmax(10rem,1.3fr)', '6.5rem', '6rem', '6rem', 'minmax(8rem,1fr)', '7rem'] }" :row-height="56">
      <ListHeader>
        <ListHeaderCell>Instance</ListHeaderCell>
        <ListHeaderCell>Health</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Benches</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Sites</ListHeaderCell>
        <ListHeaderCell class="max-lg:hidden">Published ports</ListHeaderCell>
        <ListHeaderCell />
      </ListHeader>
      <ListRows :items="instances" row-key="project" v-slot="{ item }">
        <ListRow :route="`/instances/${item.project}`">
          <ListCell class="flex-col !items-start justify-center gap-1">
            <span class="text-base-medium text-ink-gray-9">{{ item.project }}</span>
            <span class="text-sm text-ink-gray-5">container {{ item.docker_status }}</span>
          </ListCell>
          <ListCell><StatusBadge :token="item.overall" /></ListCell>
          <ListCell class="max-lg:hidden"><span class="whitespace-nowrap tabular-nums text-base text-ink-gray-7">{{ benchCount(item.project) ?? (details[item.project]?.loading ? 'reading' : 'not cached') }}</span></ListCell>
          <ListCell class="max-lg:hidden"><span class="whitespace-nowrap tabular-nums text-base text-ink-gray-7">{{ details[item.project]?.data ? countSites(details[item.project].data) : details[item.project]?.loading ? 'reading' : 'not cached' }}</span></ListCell>
          <ListCell class="max-lg:hidden">
            <span class="font-mono text-sm text-ink-gray-6">{{ publishedRange(item.ports).web || 'none' }}</span>
            <span class="ml-2 text-xs text-ink-gray-4">{{ publishedRange(item.ports).count }} web</span>
          </ListCell>
          <ListCell class="justify-end gap-1" @click.stop.prevent>
            <Button v-if="!item.container_running" class="max-lg:hidden" size="sm" icon-left="lucide-play" label="Start" @click.stop.prevent="openAction('instance.start', { project: item.project })" />
            <Button v-else class="max-lg:hidden" size="sm" icon-left="lucide-square" label="Stop" @click.stop.prevent="openAction('instance.stop', { project: item.project })" />
            <Dropdown :options="menu(item)" align="end">
              <Button size="sm" variant="ghost" icon="lucide-ellipsis" aria-label="More actions" @click.stop.prevent />
            </Dropdown>
          </ListCell>
        </ListRow>
      </ListRows>
    </List>

    <section v-if="remembered.length">
      <div class="flex items-baseline gap-2">
        <h2 class="text-lg-semibold text-ink-gray-9">In the cache, not on Docker</h2>
        <span class="text-sm text-ink-gray-5">{{ remembered.length }} {{ remembered.length === 1 ? 'project' : 'projects' }}</span>
      </div>
      <p class="mt-1 text-p-sm text-ink-gray-5">These instances were removed or live on another Docker daemon. cwcli still remembers their benches and sites; search finds them as remembered.</p>
      <div class="mt-3 flex flex-wrap gap-2">
        <Tooltip v-for="p in remembered" :key="p.name" :text="`cached ${p.last_updated.slice(0, 16)}`">
          <Badge variant="outline" theme="gray" size="md" :label="p.name" />
        </Tooltip>
      </div>
    </section>
  </div>
</template>
