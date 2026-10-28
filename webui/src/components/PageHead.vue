<script setup>
// The one page-chrome contract (Pilot's MainLayout header): breadcrumb or title
// on the left, an optional count or status badge beside it, actions on the right.
// Every page renders through this so the chrome reads the same everywhere.
import { Breadcrumbs, PageHeader, PageHeaderTitle } from 'frappe-ui'
defineProps({
  title: { type: String, default: '' },
  crumbs: { type: Array, default: null }, // [{ label, route }]
  count: { type: [Number, String], default: null },
})
</script>

<template>
  <PageHeader class="sticky top-0">
    <div class="flex min-w-0 items-center gap-2">
      <Breadcrumbs v-if="crumbs" :items="crumbs" />
      <PageHeaderTitle v-else :title="title" />
      <span v-if="count !== null && count !== undefined" class="rounded-full bg-surface-gray-2 px-2 py-0.5 text-xs tabular-nums text-ink-gray-6">{{ count }}</span>
      <slot name="badge" />
    </div>
    <div class="flex items-center gap-2"><slot name="actions" /></div>
  </PageHeader>
</template>
