<script setup>
// One app's detail: logo hero, the repository, where it is installed (every
// instance, bench and site, with the live checked-out ref when the instance is
// running), and the actions: Install, Update, Check out a ref, Open repository.
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Badge, Button, Dialog, Tooltip } from 'frappe-ui'
import AppIcon from './AppIcon.vue'
import HostIcon from './HostIcon.vue'
import { appPanel, openInstall } from '../data/app-panel'
import { ensureRunning } from '../data/start-gate'
import EditorIcon from './EditorIcon.vue'
import { installText, namespaceOf, sourceLabel, STATE_LABEL, STATE_THEME } from '../data/catalog'
import { instance as findInstance } from '../data/fleet'
import { openAction } from '../data/dialog'
import { openJob } from '../data/job-dialog'
import { postJson } from '../data/api'
import { updateItem } from '../data/updates'
import { catalog } from '../data/catalog'
import { toast } from 'frappe-ui'

const router = useRouter()
// Captain note 36: per instance, the version we are on and the current one. The live
// updates check knows both for a running instance; a stopped one only has the cache.
function versionLine(i) {
  const it = updateItem(i.project, i.bench_index, appPanel.app?.name)
  const cached = i.sites.map((s) => s.version).find(Boolean) || null
  const on = it?.installed_version || cached
  const branch = it?.branch || i.sites.map((s) => s.branch).find(Boolean) || null
  const latest = it?.release?.latest_same_major?.replace(/^v/, '') || null
  if (!it) return { on, branch, note: cached ? 'latest not checked: start the instance' : null, tone: 'text-ink-gray-4' }
  if (!it.behind) return { on, branch, note: latest ? `current (latest ${latest})` : 'current, at the branch tip', tone: 'text-ink-green-6' }
  if (latest && latest !== (on || '').replace(/^v/, '')) return { on, branch, note: `latest ${latest}${it.commits_behind != null ? ` · ${it.commits_behind} commits behind` : ''}`, tone: 'text-ink-orange-6' }
  return { on, branch, note: it.commits_behind != null ? `${it.commits_behind} commits behind the branch tip` : 'behind the branch tip', tone: 'text-ink-orange-6' }
}
const app = computed(() => appPanel.app)
const installed = computed(() => app.value && app.value.state !== 'remote')
// What the default branch declares (display only; the install checks the picked ref).
const declaresLine = computed(() => {
  const d = app.value?.declares
  if (!d) return ''
  const parts = []
  if (d.frappe_range) parts.push(`Needs Frappe ${d.frappe_range}`)
  if (d.required_apps?.length) parts.push(`requires ${d.required_apps.join(', ')}`)
  return parts.join(' · ')
})
const canInstall = computed(() => !!app.value?.source?.url || installed.value)
const running = (project) => !!findInstance(project)?.container_running
const present = (i) => i.project_state === 'present'
const benchName = (i) => i.bench_label || (i.bench_path || '').split('/').filter(Boolean).pop()
const ctx = (i) => ({ project: i.project, bench: String(i.bench_index), benchName: benchName(i), app: app.value.name })

function update(i) {
  appPanel.open = false
  openJob('apps-update', { ...ctx(i), args: { apps: app.value.name, sites: '' } })
}
function checkout(i) {
  appPanel.open = false
  openAction('app.checkout', ctx(i))
}
// Open the app's source in a host editor: the same plan and handover as `cwcli open --app`.
const EDITOR_LABEL = { code: 'VS Code', 'code-insiders': 'VS Code Insiders', cursor: 'Cursor' }
const editors = computed(() => (catalog.data?.editors || []).filter((e) => EDITOR_LABEL[e]))
const editorPath = (e) => (catalog.data?.editor_paths?.[e] ? `${EDITOR_LABEL[e]} at ${catalog.data.editor_paths[e]}` : EDITOR_LABEL[e])
const opening = ref(null)
async function openIn(i, editor) {
  if (!(await ensureRunning(i.project, `Open in ${EDITOR_LABEL[editor]}`, { bench: String(i.bench_index), benchLabel: benchName(i) }))) return
  opening.value = `${i.project}:${editor}`
  try {
    const data = await postJson('/api/v1/actions', { action: 'open_editor', project: i.project, bench: String(i.bench_index), app: app.value.name, editor })
    toast.success(`Opening ${app.value.name} in ${EDITOR_LABEL[editor]}`, { description: data?.outcome?.working_dir })
  } catch (e) {
    toast.error(`Could not open in ${EDITOR_LABEL[editor]}`, { description: e.hint ? `${e.message} ${e.hint}` : e.message })
  } finally {
    opening.value = null
  }
}
function go(path) {
  appPanel.open = false
  router.push(path)
}
const LOGO_SOURCE = {
  hooks: 'from hooks.py',
  marketplace: 'from the Marketplace index',
  gitlab_avatar: 'GitLab project avatar',
  gitlab_group_avatar: "GitLab group avatar, not the app's own",
  gitlab_user_avatar: "GitLab user avatar, not the app's own",
  github_owner_avatar: "GitHub org/user avatar, not the app's own",
  github_card: 'GitHub social card',
}
</script>

<template>
  <Dialog v-model:open="appPanel.open" size="2xl" bare>
    <!-- Captain note 45: the panel grows to the viewport, then its body scrolls; the footer stays. -->
    <div v-if="app" class="relative flex max-h-[calc(100vh-4rem)] flex-col">
      <Button class="absolute right-3 top-3 z-10" variant="ghost" icon="lucide-x" aria-label="Close" @click="appPanel.open = false" />
      <div class="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
        <div v-if="app.cover?.url" class="-mx-5 -mt-5 mb-5 h-36 overflow-hidden rounded-t-7 border-b border-outline-gray-1 bg-surface-gray-1 sm:-mx-6 sm:-mt-6">
          <img :src="app.cover.url" alt="" class="size-full object-cover" />
        </div>
        <div class="flex items-start gap-4">
          <div class="flex flex-col items-center gap-1">
            <AppIcon :app="app" size="xl" />
            <span v-if="app.logo?.kind === 'group'" class="text-[10px] uppercase tracking-wide text-ink-gray-4" :title="LOGO_SOURCE[app.logo.source]">group logo</span>
          </div>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-xl font-semibold text-ink-gray-9">{{ app.title }}</h2>
              <Badge size="sm" :theme="STATE_THEME[app.state]" :label="STATE_LABEL[app.state]" />
              <Tooltip v-if="app.official" text="Owned by the frappe organisation on GitHub"><Badge size="sm" theme="blue" label="official"><template #prefix><span class="lucide-badge-check size-3" aria-hidden="true" /></template></Badge></Tooltip>
              <Badge v-if="app.source?.private" size="sm" theme="gray" variant="outline" label="private" />
              <Badge v-if="app.source?.archived" size="sm" theme="amber" variant="outline" label="archived" />
            </div>
            <div class="mt-1 font-mono text-sm text-ink-gray-5">{{ app.name }}</div>
            <p v-if="declaresLine" class="mt-1 text-sm text-ink-gray-6" :title="`Declared on ${app.source?.default_branch || 'the default branch'}; the install checks the branch you pick.`">{{ declaresLine }}</p>
            <p class="mt-2 text-p-base text-ink-gray-7">{{ app.description || 'No description in the repository.' }}</p>
          </div>
        </div>

        <div class="mt-5 grid gap-px overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-gray-2 sm:grid-cols-3">
          <div class="bg-surface-base px-4 py-3">
            <div class="text-sm text-ink-gray-5">Source</div>
            <div class="mt-1 flex min-w-0 items-center gap-1.5 text-base text-ink-gray-8"><HostIcon :host="app.source?.kind === 'marketplace' ? 'marketplace' : app.source?.host" size="size-4" class="shrink-0" /><span class="truncate">{{ sourceLabel(app) }}<span v-if="namespaceOf(app)" class="text-ink-gray-6"> · {{ namespaceOf(app) }}</span></span></div>
            <div v-if="app.source?.registry && app.source?.kind !== 'marketplace'" class="text-xs text-ink-gray-5">also in the Marketplace index</div>
            <div class="mt-0.5 truncate font-mono text-xs text-ink-gray-5" :title="app.source?.path">{{ app.source?.path || 'no git remote recorded' }}</div>
          </div>
          <div class="bg-surface-base px-4 py-3">
            <div class="text-sm text-ink-gray-5">Default branch</div>
            <div class="mt-1 font-mono text-base text-ink-gray-8">{{ app.source?.default_branch || 'unknown' }}</div>
            <div class="mt-0.5 text-xs text-ink-gray-5">{{ app.source?.stars != null ? `${app.source.stars} stars · ` : '' }}{{ app.source?.categories?.length ? app.source.categories.join(', ') + ' · ' : '' }}{{ app.remote_state }}</div>
          </div>
          <div class="bg-surface-base px-4 py-3">
            <div class="text-sm text-ink-gray-5">Logo</div>
            <div class="mt-1 text-base text-ink-gray-8">{{ app.logo ? LOGO_SOURCE[app.logo.source] || app.logo.source : 'none found' }}</div>
            <div class="mt-0.5 truncate font-mono text-xs text-ink-gray-5" :title="app.hooks_logo || ''">{{ app.hooks_logo || 'hooks.py declares no logo' }}</div>
          </div>
        </div>

        <section class="mt-6">
          <div class="flex items-baseline gap-2">
            <h3 class="text-lg-semibold text-ink-gray-9">Installed on</h3>
            <span class="text-sm text-ink-gray-5">{{ installText(app) || 'no instance' }}</span>
          </div>
          <div v-if="app.installs.length" class="mt-3 divide-y divide-outline-gray-1 rounded-6 border border-outline-gray-1">
            <div v-for="i in app.installs" :key="i.project + i.bench_index" class="grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-2 px-3 py-3">
              <div class="min-w-0">
                <button class="flex min-w-0 items-center gap-2 text-left hover:underline" @click="go(`/instances/${i.project}/benches/${i.bench_index}?tab=apps`)">
                  <span class="size-2 shrink-0 rounded-full" :class="running(i.project) ? 'bg-surface-green-7' : present(i) ? 'bg-surface-gray-5' : 'border border-outline-gray-4'" />
                  <span class="truncate text-base-medium text-ink-gray-9">{{ i.project }}</span>
                  <span class="truncate text-sm text-ink-gray-5">{{ benchName(i) }}</span>
                </button>
                <div class="mt-1 flex flex-wrap items-baseline gap-x-2 pl-4 text-sm">
                  <span class="text-ink-gray-8">on <span class="font-medium tabular-nums">{{ versionLine(i).on || 'unknown version' }}</span><span v-if="versionLine(i).branch" class="text-ink-gray-5"> · {{ versionLine(i).branch }}</span></span>
                  <span v-if="versionLine(i).note" class="text-xs" :class="versionLine(i).tone">{{ versionLine(i).note }}</span>
                </div>
                <Tooltip :text="i.verified ? 'git remote and ref read live from the container' : 'remembered from the cache; start the instance to verify'">
                  <div class="mt-0.5 pl-4 text-xs" :class="i.verified ? 'text-ink-green-6' : 'text-ink-gray-4'">{{ i.verified ? (i.checked_out ? `checked out: ${i.checked_out}` : 'verified live') : i.project_state === 'absent' ? 'removed from Docker' : 'remembered from the cache' }}</div>
                </Tooltip>
                <!-- Each site is a way out of the panel and onto that site (captain note 02:04Z: no pills). -->
                <div class="mt-2 space-y-0.5 pl-2">
                  <button v-for="s in i.sites" :key="s.name" class="group flex w-full items-center gap-2 rounded-4 px-2 py-1 text-left hover:bg-surface-gray-1" @click="go(`/instances/${i.project}/benches/${i.bench_index}/sites/${s.name}`)">
                    <span class="lucide-globe size-3.5 shrink-0 text-ink-gray-5" aria-hidden="true" />
                    <span class="text-sm text-ink-gray-8">{{ s.name }}</span>
                    <span class="truncate text-xs text-ink-gray-5">{{ [s.version, s.branch].filter(Boolean).join(' · ') }}</span>
                    <span class="lucide-arrow-right ml-auto size-3.5 shrink-0 text-ink-gray-4 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
                  </button>
                  <div v-if="!i.sites.length" class="px-2 py-1 text-xs text-ink-gray-4">in apps/, installed on no site</div>
                </div>
              </div>
              <div class="flex w-40 flex-col gap-1">
                <Button v-for="e in editors" :key="e" size="sm" variant="subtle" class="w-full justify-start" :label="`Open in ${EDITOR_LABEL[e]}`" :loading="opening === `${i.project}:${e}`" :title="editorPath(e)" @click="openIn(i, e)"><template #prefix><EditorIcon :editor="e" /></template></Button>
                <Button size="sm" variant="subtle" icon-left="lucide-package-check" class="w-full justify-start" label="Update" @click="update(i)" />
                <Button size="sm" variant="subtle" icon-left="lucide-git-branch" class="w-full justify-start" label="Check out" @click="checkout(i)" />
              </div>
            </div>
          </div>
          <p v-else class="mt-2 text-p-sm text-ink-gray-5">Not on any bench cwcli knows. Install it on an instance to put it to work.</p>
        </section>
      </div>
      <div class="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-outline-gray-1 bg-surface-gray-1 px-5 py-3 sm:px-6">
        <span class="mr-auto flex items-center gap-2">
          <a v-if="app.source?.url" :href="app.source.url" target="_blank" rel="noopener"><Button icon-left="lucide-external-link" :label="app.source?.kind === 'marketplace' ? 'Repository' : `Open on ${sourceLabel(app)}`" /></a>
          <a v-if="app.source?.website" :href="app.source.website" target="_blank" rel="noopener"><Button variant="ghost" icon-left="lucide-globe" label="Website" /></a>
          <a v-if="app.source?.docs" :href="app.source.docs" target="_blank" rel="noopener"><Button variant="ghost" icon-left="lucide-book-open" label="Docs" /></a>
        </span>
        <Button label="Close" @click="appPanel.open = false" />
        <Button v-if="canInstall" variant="solid" icon-left="lucide-package-plus" data-primary :label="installed ? 'Install on another site' : 'Install'" @click="appPanel.open = false; openInstall(app)" />
      </div>
    </div>
  </Dialog>
</template>
