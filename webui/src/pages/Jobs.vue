<script setup>
import { computed, onMounted, ref } from 'vue'
import { Badge, Button, ErrorMessage, PageHeader, PageHeaderTitle, TextInput } from 'frappe-ui'
import { List, ListCell, ListHeader, ListHeaderCell, ListRow, ListRows } from 'frappe-ui/list'
import { activeJobs, elapsed, JOB_THEME, jobLabel, jobList, jobTarget, jobs, loadJobs } from '../data/jobs'
import { openJob } from '../data/job-dialog'
import { setFocus } from '../data/fleet'

const query = ref('')
onMounted(() => {
  setFocus(null)
  loadJobs()
})
const rows = computed(() => {
  const term = query.value.trim().toLowerCase()
  return jobList.value.filter((job) => !term || `${job.verb} ${jobTarget(job)} ${job.state}`.toLowerCase().includes(term))
})
const date = (value) => value ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value)) : 'not started'
</script>

<template>
  <PageHeader>
    <PageHeaderTitle title="Jobs" />
    <Button variant="solid" icon-left="lucide-plus" label="New instance" @click="openJob('init')" />
  </PageHeader>
  <div class="space-y-5 px-3 pb-20 pt-5 sm:px-5">
    <div class="flex flex-wrap items-center gap-3">
      <TextInput v-model="query" class="w-72 max-w-full" type="search" placeholder="Filter jobs">
        <template #prefix><span class="lucide-search size-3.5 text-ink-gray-5" aria-hidden="true" /></template>
      </TextInput>
      <span class="text-sm text-ink-gray-5">{{ activeJobs.length }} active · {{ jobList.length }} recorded</span>
    </div>
    <ErrorMessage :message="jobs.error" />
    <div v-if="jobs.loaded && !rows.length" class="flex flex-col items-center gap-3 py-16 text-center">
      <div class="rounded-full bg-surface-gray-2 p-3 text-ink-gray-5"><span class="lucide-list-checks size-6" aria-hidden="true" /></div>
      <p class="text-base text-ink-gray-7">{{ query ? 'No jobs match this filter' : 'No jobs yet' }}</p>
      <p class="text-sm text-ink-gray-5">Lifecycle work and long commands appear here with their full event history.</p>
    </div>
    <List v-else class="-mx-3 list-row-px-3" :columns="{ base: ['minmax(0,1fr)', '6.5rem'], md: ['minmax(10rem,1.4fr)', 'minmax(7rem,1fr)', '7rem', '7rem', '6rem'] }" :row-height="56">
      <ListHeader>
        <ListHeaderCell>Job</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Target</ListHeaderCell>
        <ListHeaderCell>Status</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Started</ListHeaderCell>
        <ListHeaderCell class="max-md:hidden">Elapsed</ListHeaderCell>
      </ListHeader>
      <ListRows :items="rows" row-key="id" v-slot="{ item }">
        <ListRow :route="`/jobs/${item.id}`">
          <ListCell class="flex-col !items-start justify-center gap-1 overflow-hidden">
            <span class="truncate text-base-medium capitalize text-ink-gray-9">{{ jobLabel(item) }}</span>
            <span class="truncate font-mono text-xs text-ink-gray-5">{{ item.id }}</span>
          </ListCell>
          <ListCell class="max-md:hidden"><span class="truncate text-sm text-ink-gray-7">{{ jobTarget(item) }}</span></ListCell>
          <ListCell><Badge :theme="JOB_THEME[item.state] || 'gray'" :label="item.state.replace('_', ' ')" /></ListCell>
          <ListCell class="max-md:hidden"><span class="whitespace-nowrap text-sm text-ink-gray-6">{{ date(item.started_at || item.created_at) }}</span></ListCell>
          <ListCell class="max-md:hidden"><span class="tabular-nums text-sm text-ink-gray-6">{{ elapsed(item) }}</span></ListCell>
        </ListRow>
      </ListRows>
    </List>
  </div>
</template>
