<script setup>
// frappe-ui's SettingsHeader keeps the title and the actions on one row that never
// wraps: on a phone a header with a button (Remote hosts, App sources) squeezes its
// title column to a few characters, which then break letter by letter. This header
// is the same row, but the actions move under the title once the row is too narrow
// for both. One place for that rule, so a new pane cannot forget it.
import { SettingsHeader } from 'frappe-ui'
defineProps({ title: { type: String, default: '' }, description: { type: String, default: '' } })
</script>

<template>
  <SettingsHeader>
    <div class="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
      <div class="flex min-w-0 flex-1 basis-60 flex-col gap-1">
        <h2 v-if="title" class="text-lg font-semibold text-ink-gray-8">{{ title }}</h2>
        <p v-if="description" class="text-base text-ink-gray-6">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="ml-auto flex shrink-0 items-center gap-2"><slot name="actions" /></div>
    </div>
  </SettingsHeader>
</template>
