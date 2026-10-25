<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { DesktopShell, Sidebar, SidebarHeader, SidebarItem, SidebarLabel, ScrollArea, Alert, KeyboardShortcut } from 'frappe-ui'
import StatusDot from './StatusDot.vue'
import ActionDialog from './ActionDialog.vue'
import JobDialog from './JobDialog.vue'
import Palette from './Palette.vue'
import { fleet, instances, doctor } from '../data/fleet'
import { session } from '../data/session'
import { openPalette } from '../data/palette'
import { activeJobs } from '../data/jobs'

const route = useRoute()
// Collapse to the icon rail below 640px, matching frappe-ui's own sm breakpoint.
const narrow = window.matchMedia('(max-width: 639px)')
const collapsed = ref(narrow.matches)
narrow.addEventListener('change', (e) => (collapsed.value = e.matches))
doctor.load()
const warn = computed(() => (doctor.data ? doctor.data.warned + doctor.data.failed : null))
const live = computed(() => fleet.connection === 'live')
const connectionLabel = computed(() =>
  fleet.connection === 'live' ? 'Live' : fleet.connection === 'connecting' ? 'Connecting' : 'Reconnecting',
)
</script>

<template>
  <div class="h-screen w-full bg-surface-base text-ink-gray-8">
    <DesktopShell>
      <template #sidebar>
        <Sidebar v-model:collapsed="collapsed" width="15rem" class="border-r border-outline-gray-1">
          <SidebarHeader title="Caffeinated Whale" :subtitle="session.meta ? `cwcli ${session.meta.version}` : 'cwcli'" logo="/logo-whale.png" />
          <div v-if="!collapsed" class="px-2 pb-1">
            <button class="flex h-7 w-full items-center gap-2 rounded-4 bg-surface-gray-2 px-2 text-sm text-ink-gray-5 hover:bg-surface-gray-3" @click="openPalette">
              <span class="lucide-search size-3.5" aria-hidden="true" />
              <span class="flex-1 text-left">Search or run</span>
              <KeyboardShortcut combo="Mod+K" />
            </button>
          </div>
          <ScrollArea class="min-h-0 flex-1" viewport-class="px-2 pt-0.5 pb-10">
            <div class="space-y-0.5">
              <SidebarItem label="Instances" icon="lucide-boxes" route="/instances" :active="route.name === 'instances'">
                <template #suffix><span class="mr-1 text-xs text-ink-gray-5">{{ instances.length }}</span></template>
              </SidebarItem>
              <SidebarItem label="Jobs" icon="lucide-list-checks" route="/jobs" :active="route.name === 'jobs' || route.name === 'job'">
                <template #suffix><span v-if="activeJobs.length" class="mr-1 text-xs text-ink-blue-6">{{ activeJobs.length }}</span></template>
              </SidebarItem>
              <SidebarItem label="Health check" icon="lucide-stethoscope" route="/health" :active="route.name === 'health'">
                <template #suffix><span v-if="warn" class="mr-1 text-xs text-ink-amber-6">{{ warn }}</span></template>
              </SidebarItem>
              <SidebarItem label="Settings" icon="lucide-settings" route="/settings" :active="route.name === 'settings'" />
            </div>
            <SidebarLabel class="mt-4">Instances</SidebarLabel>
            <div class="space-y-0.5">
              <SidebarItem
                v-for="i in instances"
                :key="i.project"
                :label="i.project"
                :route="`/instances/${i.project}`"
                :active="route.params.project === i.project"
              >
                <template #prefix><StatusDot :token="i.overall" /></template>
              </SidebarItem>
            </div>
          </ScrollArea>
          <div class="flex items-center gap-2 border-t border-outline-gray-1 py-2.5 text-xs text-ink-gray-5" :class="collapsed ? 'justify-center' : 'px-4'">
            <span class="size-1.5 shrink-0 rounded-full" :class="live ? 'bg-surface-green-7' : 'bg-surface-amber-7'" />
            <span v-if="!collapsed">{{ connectionLabel }}<template v-if="session.meta"> · protocol {{ session.meta.protocol }}</template></span>
          </div>
        </Sidebar>
      </template>
      <div v-if="session.protocolMismatch" class="px-3 pt-3 sm:px-5">
        <Alert theme="amber" title="This page was built for a different cwcli daemon" description="Reload to pick up the daemon that is running now." :primary-action="{ label: 'Reload', onClick: () => location.reload() }" />
      </div>
      <router-view :key="route.fullPath" />
    </DesktopShell>
    <ActionDialog />
    <JobDialog />
    <Palette />
  </div>
</template>
