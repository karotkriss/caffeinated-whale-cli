<script setup>
import { computed, onMounted } from 'vue'
import { Button, LoadingText, ErrorMessage } from 'frappe-ui'
import { doctor, ago } from '../data/fleet'
import { applySetting, settings } from '../data/settings'

onMounted(() => doctor.load())
const groups = computed(() => {
  const out = {}
  for (const c of doctor.data?.checks || []) (out[c.group] ||= []).push(c)
  return out
})
// Start a stopped daemon from the check that found it, then re-run the checks.
const startDaemon = async (c) => {
  await applySetting(c.action, {}, { done: `${c.title} started` })
  doctor.load(true)
}
const ICON = {
  pass: 'lucide-circle-check text-ink-green-5',
  warn: 'lucide-circle-alert text-ink-amber-6',
  fail: 'lucide-circle-x text-ink-red-5',
}
</script>

<template>
  <div>
    <div class="flex items-center gap-3">
      <div v-if="doctor.data" class="flex flex-wrap items-center gap-3 text-sm">
        <span class="text-ink-green-6">{{ doctor.data.passed }} passed</span>
        <span :class="doctor.data.warned ? 'text-ink-amber-6' : 'text-ink-gray-5'">{{ doctor.data.warned }} warnings</span>
        <span :class="doctor.data.failed ? 'text-ink-red-6' : 'text-ink-gray-5'">{{ doctor.data.failed }} failed</span>
        <span class="text-ink-gray-4">checked {{ ago(doctor.at) }}</span>
      </div>
      <span v-else class="text-sm text-ink-gray-5">checking</span>
      <Button class="ml-auto" size="sm" icon-left="lucide-refresh-cw" label="Run again" :loading="doctor.loading" @click="doctor.load(true)" />
    </div>
    <LoadingText v-if="!doctor.data && doctor.loading" class="mt-6" text="Running checks..." />
    <ErrorMessage v-if="doctor.error" class="mt-4" :message="doctor.error" />
    <div v-for="(checks, group) in groups" :key="group" class="mt-6">
      <h3 class="text-sm text-ink-gray-5">{{ group }}</h3>
      <div class="mt-2 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
        <div v-for="c in checks" :key="c.id" class="flex items-start gap-3 px-3 py-2.5">
          <span class="mt-0.5 size-4 shrink-0" :class="ICON[c.status]" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <div class="flex items-baseline gap-2">
              <span class="text-base text-ink-gray-8">{{ c.title }}</span>
              <span class="font-mono text-xs text-ink-gray-4">{{ c.status }}</span>
              <span v-if="c.version_verified === false" class="text-xs text-ink-amber-6">version not verified</span>
            </div>
            <div class="mt-1 text-p-sm text-ink-gray-5">{{ c.detail }}</div>
            <code v-if="c.fix" class="mt-1 inline-block max-w-full break-words rounded-3 bg-surface-gray-2 px-1.5 py-0.5 font-mono text-xs text-ink-gray-7" :title="c.fix">{{ c.fix }}</code>
          </div>
          <Button v-if="c.action && c.status !== 'pass'" class="shrink-0" size="sm" variant="solid" icon-left="lucide-play" label="Start" :loading="settings.busy === c.action" @click="startDaemon(c)" />
        </div>
      </div>
    </div>
  </div>
</template>
