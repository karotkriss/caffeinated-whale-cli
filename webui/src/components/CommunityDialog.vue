<script setup>
// Add, update or remove a community integration (core/community.py). Adding is two
// steps: cwcli fetches the repository and reads its declaration (nothing is built or
// run), then the trust screen shows what it is and the exact commit, and only
// Install installs that commit. An update shows the installed and the latest commit
// the same way before anything changes. Removing asks for the id typed back.
import { computed, ref, watch } from 'vue'
import { Alert, Badge, Button, Dialog, ErrorMessage, FormControl, LoadingText } from 'frappe-ui'
import { addCommunity, checkCommunity, communityDialog, HOOK_POINT, integrations, loadIntegrations, previewCommunity, removeCommunity, updateCommunity } from '../data/integrations'

const d = communityDialog
const url = ref('')
const refName = ref('')
const preview = ref(null) // add: the trust screen
const check = ref(null) // update: { current, latest, update_available, declaration, ... }
const typed = ref('')
const busy = ref(null) // 'review' | 'install' | 'check' | 'update' | 'remove'
const error = ref(null)

const installed = computed(() => integrations.list.find((i) => i.id === d.id) || null)

watch(() => d.open, (open) => {
  if (!open) return
  url.value = ''
  refName.value = ''
  preview.value = null
  check.value = null
  typed.value = ''
  error.value = null
  busy.value = null
  if (d.mode === 'update') runCheck()
})
// A trust screen describes the URL and ref it was read from; editing either retires it.
watch([url, refName], () => { preview.value = null })

async function run(step, fn) {
  busy.value = step
  error.value = null
  try { return await fn() } catch (e) { error.value = e; return null } finally { busy.value = null }
}
async function review() {
  if (!url.value.trim()) return
  const data = await run('review', () => previewCommunity(url.value.trim(), refName.value.trim() || null))
  if (data) preview.value = data.preview
}
async function install() {
  const p = preview.value
  if (await run('install', () => addCommunity(p.url, p.ref, p.commit))) { await loadIntegrations(); d.open = false }
}
async function runCheck() {
  const data = await run('check', () => checkCommunity(d.id))
  if (data) check.value = data.check
}
async function runUpdate() {
  if (await run('update', () => updateCommunity(d.id, check.value.latest))) { await loadIntegrations(); d.open = false }
}
async function remove() {
  if (await run('remove', () => removeCommunity(d.id))) { await loadIntegrations(); d.open = false }
}

const decl = computed(() => (d.mode === 'add' ? preview.value?.declaration : check.value?.declaration) || null)
const source = computed(() => (d.mode === 'add' ? preview.value : check.value) || null)
const commit = computed(() => (d.mode === 'add' ? preview.value?.commit : check.value?.latest) || '')
function host(u) { try { return new URL(u).host } catch { return u } }
const signIn = computed(() => {
  const s = source.value
  if (!s) return ''
  return s.signed_in_with ? `Read with this machine's ${s.signed_in_with} sign-in for ${host(s.url)}` : 'Read anonymously; no sign-in was needed'
})
const rows = computed(() => decl.value && source.value ? [
  { label: 'Publisher', value: decl.value.publisher },
  { label: 'Repository', value: source.value.url, mono: true },
  { label: 'Branch or tag', value: source.value.ref || 'Default branch' },
  { label: 'Commit', value: commit.value, mono: true },
  { label: 'Id', value: `${decl.value.id} · package ${decl.value.project}`, mono: true },
  { label: 'Sign-in', value: signIn.value },
] : [])
const short = (c) => (c || '').slice(0, 12)
const title = computed(() => {
  if (d.mode === 'remove') return `Remove ${installed.value?.name || d.id}?`
  if (d.mode === 'update') return `Update ${installed.value?.name || d.id}`
  return preview.value ? `Install ${preview.value.declaration.name}?` : 'Add a community integration'
})
const errorText = computed(() => (error.value ? (error.value.hint ? `${error.value.message} ${error.value.hint}` : error.value.message) : null))
</script>

<template>
  <Dialog v-model:open="d.open" :title="title" :icon="d.mode === 'remove' ? 'lucide-trash-2' : 'lucide-package-plus'" :theme="d.mode === 'remove' ? 'red' : undefined" size="xl">
    <div class="space-y-4">
      <form v-if="d.mode === 'add' && !preview" class="space-y-3" autocomplete="off" @submit.prevent="review">
        <p class="text-p-base text-ink-gray-7">Paste a GitHub, GitLab.com or self-hosted GitLab repository. cwcli reads what it declares before anything is installed. A public repository needs no sign-in; a private one uses this machine's <span class="font-mono text-p-sm">gh</span> or <span class="font-mono text-p-sm">glab</span> sign-in.</p>
        <FormControl v-model="url" label="Repository URL" placeholder="https://github.com/owner/repo" spellcheck="false" />
        <FormControl v-model="refName" label="Branch or tag" placeholder="Default branch" description="Optional. cwcli installs the commit it points at now, and updates only when you ask." spellcheck="false" />
        <button type="submit" class="hidden" />
      </form>

      <LoadingText v-if="busy === 'check'" text="Checking the repository…" />
      <template v-if="d.mode === 'update' && check">
        <dl class="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-p-sm">
          <dt class="text-ink-gray-5">Installed</dt><dd class="font-mono text-xs leading-5 text-ink-gray-8">{{ check.current }}</dd>
          <dt class="text-ink-gray-5">Latest</dt><dd class="font-mono text-xs leading-5 text-ink-gray-8">{{ check.latest }}</dd>
        </dl>
        <p v-if="!check.update_available" class="flex items-center gap-1.5 text-p-sm text-ink-green-6"><span class="lucide-circle-check size-4" aria-hidden="true" />Up to date: {{ check.ref || 'the default branch' }} still points at the installed commit.</p>
      </template>

      <template v-if="decl">
        <div class="space-y-3 rounded-6 border border-outline-gray-2 p-4">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-lg font-semibold text-ink-gray-9">{{ decl.name }}</span>
            <Badge size="sm" variant="subtle" theme="amber" label="Community, not reviewed" />
          </div>
          <p class="text-p-base text-ink-gray-7">{{ decl.description }}</p>
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="text-p-sm text-ink-gray-5">Provides</span>
            <Badge v-for="p in decl.provides" :key="p" size="sm" variant="outline" :label="HOOK_POINT[p] || p" />
            <span v-if="!decl.provides.length" class="text-p-sm text-ink-gray-6">no hook points</span>
          </div>
          <dl class="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-p-sm">
            <template v-for="r in rows" :key="r.label">
              <dt class="text-ink-gray-5">{{ r.label }}</dt>
              <dd class="min-w-0 break-all leading-5 text-ink-gray-8" :class="r.mono && 'font-mono text-xs'">{{ r.value }}</dd>
            </template>
          </dl>
        </div>
        <Alert theme="amber" title="Community code runs with cwcli's own rights on this machine" :description="d.mode === 'update' ? `Nobody has reviewed it. Update only if you trust the publisher and commit ${short(commit)}. The update turns it off; its settings and secrets are kept, and turning it on again runs the new code's test first.` : 'Nobody has reviewed it. Install it only if you trust the publisher and this commit. It starts off; turning it on runs its own test first.'" />
      </template>

      <template v-if="d.mode === 'remove' && installed">
        <p class="text-p-base text-ink-gray-7">Removes {{ installed.name }}'s package, settings and secrets from this machine. The repository is not touched; adding it again starts from scratch.</p>
        <p v-if="installed.community" class="break-all font-mono text-xs text-ink-gray-6">{{ installed.community.url }} @ {{ short(installed.community.commit) }}</p>
        <FormControl v-model="typed" :label="`Type ${d.id} to confirm`" :placeholder="d.id" autocomplete="off" spellcheck="false" />
      </template>

      <ErrorMessage :message="errorText" />
    </div>
    <template #actions="{ close }">
      <div class="flex flex-wrap justify-end gap-2">
        <Button v-if="d.mode === 'add' && preview" label="Back" @click="preview = null" />
        <Button v-else label="Cancel" @click="close" />
        <Button v-if="d.mode === 'add' && !preview" variant="solid" data-primary label="Review" icon-left="lucide-search" :disabled="!url.trim()" :loading="busy === 'review'" @click="review" />
        <Button v-if="d.mode === 'add' && preview" variant="solid" data-primary :label="`Install ${short(preview.commit)}`" :loading="busy === 'install'" @click="install" />
        <Button v-if="d.mode === 'update' && check?.update_available" variant="solid" data-primary :label="`Update to ${short(check.latest)}`" :loading="busy === 'update'" @click="runUpdate" />
        <Button v-if="d.mode === 'update' && error && !check" label="Try again" :loading="busy === 'check'" @click="runCheck" />
        <Button v-if="d.mode === 'remove'" variant="solid" theme="red" data-primary label="Remove" :disabled="typed.trim() !== d.id" :loading="busy === 'remove'" @click="remove" />
      </div>
    </template>
  </Dialog>
</template>
