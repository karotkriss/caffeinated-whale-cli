<script setup>
// Direction A, "Pilot Desk": Pilot's shell. The sidebar is grouped into labelled
// sections (Workspace, Insights), the per-instance list stays as its own
// section, Settings lives in the app menu under the product name, and a
// notifications panel collects failed jobs, decisions and health warnings so
// they stop landing as banners on whatever page is open.
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useIntervalFn, useMediaQuery } from '@vueuse/core'
import { DesktopShell, Sidebar, SidebarItem, SidebarLabel, ScrollArea, Alert, Dialog, KeyboardShortcut, Badge, Button, useKeyboardShortcut } from 'frappe-ui'
import StatusDot from './StatusDot.vue'
import { loadUpdates, updateCount } from '../data/updates'
import { affectsCount, affectsNew, loadAdvisories } from '../data/advisories'
loadUpdates()
loadAdvisories()
import ActionDialog from './ActionDialog.vue'
import JobDialog from './JobDialog.vue'
import Palette from './Palette.vue'
import AppPanel from './AppPanel.vue'
import InstallSheet from './InstallSheet.vue'
import NewSiteDialog from './NewSiteDialog.vue'
import NewBenchDialog from './NewBenchDialog.vue'
import RestoreWizard from './RestoreWizard.vue'
import TransferDialog from './TransferDialog.vue'
import ReceiveDialog from './ReceiveDialog.vue'
import BackupSettingsDialog from './BackupSettingsDialog.vue'
import ScheduleDialog from './ScheduleDialog.vue'
import { loadSchedules } from '../data/schedules'
import JobToasts from './JobToasts.vue'
import HostIcon from './HostIcon.vue'
import SettingsDialog from './SettingsDialog.vue'
import StartGateDialog from './StartGateDialog.vue'
import { fleet, instances, doctor, details, countSites } from '../data/fleet'
import { session, PROTOCOL, ROLE_LABEL } from '../data/session'
import { openPalette } from '../data/palette'
import { activeJobs, jobList } from '../data/jobs'
import { notifications, unread, isRead, markRead, markAllRead } from '../data/notifications'
import { apps, activeSources, loadCatalog, hostLabel } from '../data/catalog'
import { settingsDialog, openSettings } from '../data/settings-dialog'

const route = useRoute()
const router = useRouter()
const narrow = useMediaQuery('(max-width: 639px)')
const collapsed = ref(narrow.value)
watch(narrow, (m) => (collapsed.value = m))
doctor.load()
loadCatalog()
loadSchedules() // the digest notification reads the schedules' settings
const live = computed(() => fleet.connection === 'live')
// Captain note 43: no sidebar connection line. A short drop shows the amber reconnecting
// strip; past DOWN_AFTER_MS without an answer the daemon is treated as unreachable.
const DOWN_AFTER_MS = 15000
const reconnectingSince = ref(null)
const tick = ref(Date.now())
useIntervalFn(() => (tick.value = Date.now()), 2000)
watch(() => fleet.connection, (c) => { reconnectingSince.value = c === 'reconnecting' ? Date.now() : null }, { immediate: true })
const daemonDown = computed(() => reconnectingSince.value != null && tick.value - reconnectingSince.value > DOWN_AFTER_MS)
// Ctrl+Enter (Cmd+Enter) invokes the topmost open dialog's primary action (captain
// note 02:20Z). Primaries carry data-primary; a disabled one (typed confirmation
// not yet matching, nothing selected) stays untouched.
useKeyboardShortcut({
  combo: 'Mod+Enter',
  description: 'Confirm the open dialog',
  allowInInput: true,
  allowInDialog: true,
  handler: () => {
    const dialogs = [...document.querySelectorAll('[role=dialog]')]
    const top = dialogs.at(-1)
    const primary = top && top.querySelector('[data-primary]:not([disabled])')
    if (primary) primary.click()
  },
})
function reload() { window.location.reload() }
const siteCount = computed(() => instances.value.reduce((n, i) => n + countSites(details[i.project]?.data), 0))

// Notifications: what needs a person, unread only; opening one marks it read.
const notifOpen = ref(false)
const showRead = ref(false)
const shownNotifications = computed(() => (showRead.value ? notifications.value : unread.value))
function goTo(n) {
  markRead(n)
  notifOpen.value = false
  if (n.to.startsWith('/settings/')) openSettings(n.to.split('/')[2])
  else router.push(n.to)
}
// /settings/<section> is a deep link: open the modal over the instances page.
watch(() => route.name, (name) => {
  if (name !== 'settings') return
  openSettings(route.params.section)
  router.replace('/instances')
}, { immediate: true })
</script>

<template>
  <div class="h-screen w-full bg-surface-base text-ink-gray-8">
    <DesktopShell>
      <template #sidebar>
        <Sidebar v-model:collapsed="collapsed" width="15rem" class="border-r border-outline-gray-1">
          <div class="flex items-center gap-2 px-3 pb-2 pt-3" :class="collapsed && 'justify-center px-2'">
            <img src="/logo-whale.png" alt="" class="size-7 shrink-0 rounded-4" />
            <span v-if="!collapsed" class="min-w-0 flex-1">
              <span class="block truncate text-base-medium text-ink-gray-9">Caffeinated Whale</span>
              <span class="block truncate text-xs text-ink-gray-5">{{ session.meta ? `cwcli ${session.meta.version}` : 'cwcli' }}</span>
            </span>
          </div>
          <div v-if="!collapsed" class="space-y-0.5 px-2 pb-1">
            <button class="flex h-7 w-full items-center gap-2 rounded-4 px-2 text-sm text-ink-gray-6 hover:bg-surface-gray-2" @click="openPalette">
              <span class="lucide-search size-3.5" aria-hidden="true" />
              <span class="flex-1 text-left">Search</span>
              <KeyboardShortcut combo="Mod+K" />
            </button>
            <button class="flex h-7 w-full items-center gap-2 rounded-4 px-2 text-sm text-ink-gray-6 hover:bg-surface-gray-2" @click="notifOpen = true">
              <span class="lucide-bell size-3.5" aria-hidden="true" />
              <span class="flex-1 text-left">Notifications</span>
              <span v-if="unread.length" class="rounded-full bg-surface-red-2 px-1.5 text-xs text-ink-red-6">{{ unread.length }}</span>
            </button>
          </div>
          <ScrollArea class="min-h-0 flex-1" viewport-class="px-2 pt-1 pb-10">
            <SidebarLabel>Workspace</SidebarLabel>
            <div class="space-y-0.5">
              <SidebarItem label="Instances" icon="lucide-boxes" route="/instances" :active="route.name === 'instances'">
                <template #suffix><span class="mr-1 text-xs text-ink-gray-5">{{ instances.length }}</span></template>
              </SidebarItem>
              <SidebarItem label="Apps" icon="lucide-package" route="/apps" :active="route.name === 'apps'">
                <template #suffix><span v-if="updateCount" class="mr-1 rounded-full bg-surface-orange-2 px-1.5 text-[10px] font-medium text-ink-orange-6" :title="`${updateCount} updates available`">{{ updateCount }}</span><span v-if="apps.length" class="mr-1 text-xs text-ink-gray-5">{{ apps.length }}</span></template>
              </SidebarItem>
              <SidebarItem label="Sites" icon="lucide-globe" route="/sites" :active="route.name === 'sites'">
                <template #suffix><span v-if="siteCount" class="mr-1 text-xs text-ink-gray-5">{{ siteCount }}</span></template>
              </SidebarItem>
              <SidebarItem label="Backups" icon="lucide-archive" route="/backups" :active="route.name === 'backups'" />
              <SidebarItem label="Schedules" icon="lucide-calendar-clock" route="/schedules" :active="route.name === 'schedules'" />
              <SidebarItem label="Jobs" icon="lucide-list-checks" route="/jobs" :active="route.name === 'jobs' || route.name === 'job'">
                <template #suffix>
                  <span v-if="activeJobs.length" class="mr-1 rounded-full bg-surface-blue-2 px-1.5 text-xs text-ink-blue-6" :title="`${activeJobs.length} active`">{{ activeJobs.length }} active</span>
                  <span v-else class="mr-1 text-xs text-ink-gray-5" :title="`${jobList.length} recorded, none active`">{{ jobList.length }}</span>
                </template>
              </SidebarItem>
            </div>
            <SidebarLabel class="mt-4">App sources</SidebarLabel>
            <div class="space-y-0.5">
              <SidebarItem v-for="s in activeSources" :key="s.host" :label="hostLabel(s.host)" route="/apps" :active="false">
                <template #prefix><HostIcon :host="s.host" size="size-4" /></template>
                <template #suffix><span class="mr-1 text-xs" :class="s.signed_in ? (s.error ? 'text-ink-amber-6' : 'text-ink-gray-5') : 'text-ink-gray-4'">{{ s.signed_in ? (s.apps || (s.error ? '!' : '0')) : 'off' }}</span></template>
              </SidebarItem>
            </div>
            <SidebarLabel class="mt-4">Insights</SidebarLabel>
            <div class="space-y-0.5">
              <SidebarItem label="Advisories" icon="lucide-shield-alert" route="/advisories" :active="route.name === 'advisories'">
                <template #suffix><span v-if="affectsCount" class="mr-1 rounded-full px-1.5 text-[10px] font-medium" :class="affectsNew ? 'bg-surface-red-2 text-ink-red-6' : 'bg-surface-gray-2 text-ink-gray-6'" :title="`${affectsCount} advisories affect this machine${affectsNew ? ', ' + affectsNew + ' new' : ''}`">{{ affectsCount }}</span></template>
              </SidebarItem>
            </div>
            <template v-if="instances.length">
              <SidebarLabel class="mt-4">Instances</SidebarLabel>
              <div class="space-y-0.5">
                <SidebarItem v-for="i in instances" :key="i.project" :label="i.project" :route="`/instances/${i.project}`" :active="route.params.project === i.project">
                  <template #prefix><StatusDot :token="i.overall" /></template>
                </SidebarItem>
              </div>
            </template>
          </ScrollArea>
          <div class="space-y-0.5 border-t border-outline-gray-1 px-2 pb-2.5 pt-2">
            <SidebarItem v-if="session.user" :label="session.user.name || session.user.username" icon="lucide-circle-user-round" :active="false" @click="openSettings('account')">
              <template #suffix><span class="mr-1 text-xs text-ink-gray-5" :title="session.user.source === 'ldap' ? 'Directory account' : 'Local account'">{{ ROLE_LABEL[session.user.role] }}</span></template>
            </SidebarItem>
            <SidebarItem label="Settings" icon="lucide-settings" :active="settingsDialog.open" @click="openSettings()">
              <template #suffix><span v-if="doctor.data && (doctor.data.warned + doctor.data.failed)" class="mr-1 text-xs text-ink-amber-6" :title="`${doctor.data.warned + doctor.data.failed} health check items need attention`" @click.stop="openSettings('health')">{{ doctor.data.warned + doctor.data.failed }}</span><KeyboardShortcut combo="Mod+," class="mr-1" /></template>
            </SidebarItem>
          </div>
        </Sidebar>
      </template>
      <div v-if="session.protocolMismatch" class="px-3 pt-3 sm:px-5">
        <Alert v-if="session.meta && session.meta.protocol < PROTOCOL" theme="amber" title="cwcli was updated but this daemon is still the old version" description="Run cwcli ui in a terminal. It restarts the daemon onto the new version, then reload this page." :primary-action="{ label: 'Reload', onClick: reload }" />
        <Alert v-else theme="amber" title="This page was built for a different cwcli daemon" description="Reload to pick up the daemon that is running now." :primary-action="{ label: 'Reload', onClick: reload }" />
      </div>
      <div v-if="daemonDown" class="flex items-center gap-2 border-b border-outline-red-1 bg-surface-red-1 px-4 py-1.5 text-sm text-ink-red-6">
        <span class="lucide-unplug size-3.5" aria-hidden="true" />The cwcli daemon is not reachable. Start it with <code class="rounded bg-surface-red-2 px-1 font-mono text-xs">cwcli ui</code> in a terminal; this page reconnects by itself.
      </div>
      <div v-else-if="fleet.connection === 'reconnecting'" class="flex items-center gap-2 border-b border-outline-amber-1 bg-surface-amber-1 px-4 py-1.5 text-sm text-ink-amber-6">
        <span class="lucide-loader-circle size-3.5 animate-spin" aria-hidden="true" />Reconnecting to the cwcli daemon
      </div>
      <router-view :key="route.fullPath" />
    </DesktopShell>
    <ActionDialog />
    <JobDialog />
    <Palette />
    <AppPanel />
    <InstallSheet />
    <NewSiteDialog />
    <NewBenchDialog />
    <RestoreWizard />
    <TransferDialog />
    <ReceiveDialog />
    <BackupSettingsDialog />
    <ScheduleDialog />
    <SettingsDialog />
    <StartGateDialog />
    <JobToasts />
    <Dialog v-model:open="notifOpen" title="Notifications" size="md">
      <div class="mb-2 flex items-center gap-2 text-sm text-ink-gray-5">
        <span>{{ unread.length }} unread</span>
        <button class="ml-auto hover:underline" @click="showRead = !showRead">{{ showRead ? 'Hide read' : `Show read (${notifications.length - unread.length})` }}</button>
        <Button size="sm" variant="ghost" label="Mark all read" :disabled="!unread.length" @click="markAllRead()" />
      </div>
      <div v-if="!shownNotifications.length" class="py-8 text-center text-p-sm text-ink-gray-5">{{ showRead ? 'Nothing here yet.' : 'Nothing unread. Failed jobs, decisions and health warnings land here.' }}</div>
      <div v-else class="-mx-1 divide-y divide-outline-gray-1">
        <button v-for="n in shownNotifications" :key="n.id" class="flex w-full items-start gap-3 px-1 py-2.5 text-left hover:bg-surface-gray-1" :class="isRead(n) && 'opacity-60'" @click="goTo(n)">
          <span class="mt-0.5 size-4 shrink-0" :class="[n.kind === 'job' ? 'lucide-list-checks' : 'lucide-stethoscope', n.theme === 'red' ? 'text-ink-red-5' : 'text-ink-amber-6']" aria-hidden="true" />
          <span class="min-w-0 flex-1">
            <span class="block truncate text-base text-ink-gray-8">{{ n.title }}</span>
            <span class="block truncate text-sm text-ink-gray-5">{{ n.detail }}</span>
          </span>
          <Badge size="sm" :theme="isRead(n) ? 'gray' : n.theme" :label="isRead(n) ? 'read' : n.kind" />
        </button>
      </div>
      <template #actions="{ close }">
        <div class="flex justify-end"><Button label="Close" @click="close" /></div>
      </template>
    </Dialog>
  </div>
</template>
