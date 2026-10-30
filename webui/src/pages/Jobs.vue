<script setup>
// Direction C: the jobs list with Pilot's status tabs kept in the URL, relative
// times with the absolute time in a tooltip, the raw id moved to the detail
// page, and no stray "New instance" button in the header. Jobs on remote hosts
// are in the same list with a Machine column once a host is linked.
import { computed, onMounted, ref } from 'vue'
import { Badge, ErrorMessage, Select, TabButtons, TextInput, Tooltip } from 'frappe-ui'
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from 'frappe-ui/list'
import PageHead from '../components/PageHead.vue'
import JobState from '../components/JobState.vue'
import EmptyState from '../components/EmptyState.vue'
import { activeJobs, elapsed, JOB_LABEL, jobAccount, jobLabel, jobList, jobPath, jobTarget, jobs, loadJobs } from '../data/jobs'
import { setFocus, ago } from '../data/fleet'
import { useTabQuery } from '../data/tabs'
import { hasHosts, hostName } from '../data/hosts'

const query = ref('')
const status = useTabQuery('all', 'status')
const kind = useTabQuery('all', 'type') // captain note 44: a type filter, kept in the URL like status
onMounted(() => {
  setFocus(null)
  loadJobs()
})
const tabs = [
  { label: 'All', value: 'all' },
  { label: 'Active', value: 'active' },
  { label: 'Needs decision', value: 'needs_choice' },
  { label: 'Failed', value: 'failed' },
  { label: 'Succeeded', value: 'succeeded' },
]
const rows = computed(() => {
  const term = query.value.trim().toLowerCase()
  return jobList.value.filter((job) => {
    if (status.value === 'active' ? !['queued', 'running', 'interrupting'].includes(job.state) : status.value !== 'all' && job.state !== status.value) return false
    if (kind.value !== 'all' && job.verb !== kind.value) return false
    return !term || `${job.verb} ${jobTarget(job)} ${job.state} ${job.host ? hostName(job.host) : ''}`.toLowerCase().includes(term)
  })
})
const absolute = (value) => (value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value)) : 'not started')
const relative = (value) => (value ? ago(Date.parse(value)) : 'not started')
const filtered = computed(() => query.value.trim() || status.value !== 'all' || kind.value !== 'all')
const kindOptions = computed(() => [
  { label: 'Any type', value: 'all' },
  ...[...new Set(jobList.value.map((j) => j.verb))].map((v) => ({ label: JOB_LABEL[v] || v, value: v })).sort((a, b) => a.label.localeCompare(b.label)),
])
const columns = computed(() => (hasHosts.value
  ? { base: ['minmax(0,1fr)', '7rem'], md: ['minmax(10rem,1.4fr)', 'minmax(7rem,1fr)', '7rem', '8rem', '7rem', '6rem', '6rem'] }
  : { base: ['minmax(0,1fr)', '7rem'], md: ['minmax(10rem,1.4fr)', 'minmax(7rem,1fr)', '8rem', '7rem', '6rem', '6rem'] }))
</script>

<template>
  <PageHead title="Jobs" :count="jobList.length || null">
    <template #badge><Badge v-if="activeJobs.length" theme="blue" :label="`${activeJobs.length} active`" /></template>
  </PageHead>
  <div class="space-y-5 px-3 pb-20 pt-5 sm:px-5">
    <div class="flex flex-wrap items-center gap-3">
      <TabButtons v-model="status" :options="tabs" />
      <Select v-model="kind" :options="kindOptions" />
      <TextInput v-model="query" class="w-64 max-w-full" type="search" placeholder="Filter by verb or target">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <span class="ml-auto text-sm tabular-nums text-ink-gray-5">{{ rows.length }} of {{ jobList.length }}</span>
    </div>
    <ErrorMessage :message="jobs.error" />
    <EmptyState v-if="jobs.loaded && !rows.length" icon="lucide-list-checks" :title="filtered ? 'No job matches this filter' : 'No jobs yet'" :description="filtered ? 'Clear the filter or pick another status.' : 'Lifecycle work and long commands appear here with their full event history.'" />
    <List v-else class="-mx-3 list-row-px-3" :columns="columns" :row-height="52">
      <ListHeader>
        <ListHeaderCell>Job</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Target</ListHeaderCell>
        <ListHeaderCell v-if="hasHosts" class="max-md:hidden">Machine</ListHeaderCell>
        <ListHeaderCell>Status</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Started</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Duration</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">By</ListHeaderCell>
      </ListHeader>
      <ListRows :items="rows" row-key="key" v-slot="{ item }">
        <ListRow :route="jobPath(item)">
          <ListCell><span class="truncate text-base-medium text-ink-gray-9">{{ jobLabel(item) }}</span></ListCell>
          <ListCell class="max-md:hidden"><span class="truncate text-sm text-ink-gray-7">{{ jobTarget(item) }}</span></ListCell>
          <ListCell v-if="hasHosts" class="gap-1.5 max-md:hidden"><span v-if="item.host" class="lucide-server size-3.5 shrink-0 text-ink-gray-4" aria-hidden="true" /><span class="truncate text-sm text-ink-gray-6">{{ item.host ? hostName(item.host) : 'this machine' }}</span></ListCell>
          <ListCell>
            <JobState :state="item.state" />
          </ListCell>
          <ListCell class="max-md:hidden">
            <Tooltip :text="absolute(item.started_at || item.created_at)"><span class="whitespace-nowrap text-sm text-ink-gray-6">{{ relative(item.started_at || item.created_at) }}</span></Tooltip>
          </ListCell>
          <ListCell class="max-md:hidden"><span class="tabular-nums text-sm text-ink-gray-6">{{ elapsed(item) }}</span></ListCell>
          <ListCell class="max-md:hidden"><span class="truncate text-sm text-ink-gray-6">{{ jobAccount(item) }}</span></ListCell>
        </ListRow>
      </ListRows>
    </List>
  </div>
</template>
