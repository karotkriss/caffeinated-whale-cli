<script setup>
// Storage: what sits on disk around the instances and how to trim it. The backup
// store has its own page; this one adds the three log stores (core.log_storage):
// each instance's Docker container logs and bench and site log files, and the host journal.
import { computed, onMounted } from 'vue'
import { Alert, Badge, Button, Tooltip } from 'frappe-ui'
import PageHead from '../components/PageHead.vue'
import EmptyState from '../components/EmptyState.vue'
import CommandLine from '../components/CommandLine.vue'
import { backups, loadBackups, sizeText } from '../data/backups'
import { dockerSizeText, instanceRows, loadLogStorage, logStorage, rotationText } from '../data/storage'
import { setFocus } from '../data/fleet'
import { openAction } from '../data/dialog'
import { can } from '../data/session'

onMounted(() => { setFocus(null); loadLogStorage(); if (!backups.data) loadBackups() })
const data = computed(() => logStorage.data)
const journal = computed(() => data.value?.journal)
const budget = computed(() => backups.data?.budget)
const rotatedTotal = computed(() => instanceRows.value.reduce((n, r) => n + r.benches.reduce((m, b) => m + b.rotated_bytes, 0), 0))
const unboundedRows = computed(() => instanceRows.value.filter((r) => r.docker?.containers.some((c) => c.driver === 'json-file' && !c.rotation)))
const unbounded = computed(() => unboundedRows.value.filter((r) => !r.docker.compose_rotation).map((r) => r.project))
const pending = computed(() => unboundedRows.value.filter((r) => r.docker.compose_rotation).map((r) => r.project))
const benchName = (path) => path.split('/').filter(Boolean).pop() || path
const admin = computed(() => can('admin'))
const truncate = (r) => openAction('logs.truncate_docker', { project: r.project, dockerTotal: r.docker?.total })
const rotation = (r) => openAction('logs.docker_rotation', { project: r.project })
const vacuum = () => openAction('logs.vacuum_journal', { journalSize: journal.value?.size })
const prune = (r, b) => openAction('logs.prune_bench', { project: r.project, bench: b.bench_index != null ? String(b.bench_index) : undefined, benchName: benchName(b.bench_path), rotated: b.rotated_files.length, rotatedBytes: b.rotated_bytes })
</script>

<template>
  <PageHead title="Storage">
    <template #actions>
      <Button variant="subtle" icon-left="lucide-refresh-cw" label="Refresh" :loading="logStorage.loading" @click="loadLogStorage()" />
      <Button variant="subtle" icon-left="lucide-archive" label="Backups" @click="$router.push('/backups')" />
    </template>
  </PageHead>

  <div class="space-y-6 px-3 pb-20 pt-5 sm:px-5">
    <div class="grid gap-px overflow-hidden rounded-7 border border-outline-gray-1 bg-surface-gray-2 sm:grid-cols-2 lg:grid-cols-4">
      <router-link to="/backups" class="min-w-0 bg-surface-elevation-1 px-4 py-3 hover:bg-surface-gray-1"><div class="text-sm text-ink-gray-5">Backup store</div><div class="mt-1 text-2xl font-semibold tabular-nums" :class="budget?.warn ? 'text-ink-amber-6' : 'text-ink-gray-9'">{{ budget ? `${budget.store_gb} GB` : '…' }}</div><div class="mt-0.5 truncate text-xs text-ink-gray-5" :title="budget?.root">{{ budget ? `${budget.free_gb} GB free on its disk · floor ${budget.min_free_gb} GB` : '' }}</div></router-link>
      <div class="min-w-0 bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Docker container logs</div><div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? (data.docker_total == null ? 'not measured' : sizeText(data.docker_total)) : '…' }}</div><div class="mt-0.5 text-xs text-ink-gray-5">{{ unbounded.length ? `${unbounded.length} ${unbounded.length === 1 ? 'instance has' : 'instances have'} no rotation` : pending.length ? `${pending.length} ${pending.length === 1 ? 'rotation waits' : 'rotations wait'} for the containers to be recreated` : 'every json-file log is rotated' }}</div></div>
      <div class="min-w-0 bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Bench logs</div><div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ data ? (data.bench_total == null ? 'not measured' : sizeText(data.bench_total)) : '…' }}</div><div class="mt-0.5 text-xs text-ink-gray-5">{{ data ? `${sizeText(rotatedTotal)} in rotated files that can go${data.bench_total == null ? ' (measured benches only)' : ''}` : '' }}</div></div>
      <div class="min-w-0 bg-surface-elevation-1 px-4 py-3"><div class="text-sm text-ink-gray-5">Host journal</div><div class="mt-1 text-2xl font-semibold tabular-nums text-ink-gray-9">{{ journal ? (journal.applies ? (journal.size == null ? 'unknown' : sizeText(journal.size)) : 'n/a') : '…' }}</div><div class="mt-0.5 truncate text-xs text-ink-gray-5" :title="journal?.reason || journal?.usage_text">{{ journal ? (journal.applies ? 'systemd-journald on this host' : journal.reason) : '' }}</div></div>
    </div>

    <Alert v-if="logStorage.error" theme="red" title="Log sizes could not be read" :description="logStorage.error.message" />
    <Alert v-else-if="unbounded.length" theme="amber" title="Some container logs have no size limit" :description="`${unbounded.join(', ')}: the Docker json-file logs grow until the disk fills. Set a rotation on the instance; it binds when its containers are next recreated. Empty the logs now for immediate room.`" />
    <Alert v-if="!logStorage.error && pending.length" theme="gray" title="A rotation is set but not live yet" :description="`${pending.join(', ')}: the compose file carries a rotation, and the running containers still log without a size limit until they are recreated. Empty the logs for immediate room.`" />

    <div v-if="logStorage.loading && !data" class="space-y-2"><div v-for="i in 3" :key="i" class="h-24 animate-pulse rounded-6 bg-surface-gray-1" /></div>
    <EmptyState v-else-if="data && !instanceRows.length" icon="lucide-hard-drive" title="No instance to measure" description="Create an instance and its logs appear here." command="cwcli axi storage logs" />

    <section v-for="r in instanceRows" :key="r.project" class="overflow-hidden rounded-6 border border-outline-gray-1 bg-surface-elevation-1">
      <div class="flex flex-wrap items-center gap-2 border-b border-outline-gray-1 px-3 py-2">
        <router-link :to="`/instances/${r.project}`" class="text-base-medium text-ink-gray-9 hover:underline">{{ r.project }}</router-link>
        <Badge :theme="r.running ? 'green' : 'gray'" variant="subtle" :label="r.running ? 'running' : 'stopped'" />
        <span v-if="r.note" class="text-xs text-ink-gray-5">{{ r.note }}</span>
        <span v-if="admin && r.docker" class="flex flex-wrap items-center gap-1 sm:ml-auto max-sm:w-full">
          <Button size="sm" variant="subtle" icon-left="lucide-rotate-ccw" label="Set rotation…" @click="rotation(r)" />
          <Button size="sm" variant="subtle" theme="red" icon-left="lucide-eraser" label="Empty Docker logs…" @click="truncate(r)" />
        </span>
      </div>

      <div v-if="r.docker" class="border-b border-outline-gray-1">
        <div class="grid grid-cols-[minmax(0,1.6fr)_7rem_8rem_6rem] items-center gap-3 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5 max-lg:grid-cols-[minmax(0,1fr)_6rem]"><span>Container log</span><span class="max-lg:hidden">Driver</span><span class="max-lg:hidden">Rotation</span><span class="text-right">Size</span></div>
        <div v-for="c in r.docker.containers" :key="c.container" class="grid grid-cols-[minmax(0,1.6fr)_7rem_8rem_6rem] items-center gap-3 border-t border-outline-gray-1 px-3 py-2 text-sm max-lg:grid-cols-[minmax(0,1fr)_6rem]">
          <span class="min-w-0"><span class="block truncate text-ink-gray-9">{{ c.container }}</span><span class="block truncate text-xs text-ink-gray-5">{{ c.service }}<template v-if="c.files != null"> · {{ c.files }} {{ c.files === 1 ? 'file' : 'files' }}</template></span></span>
          <span class="text-ink-gray-7 max-lg:hidden">{{ c.driver || '—' }}</span>
          <span class="inline-flex items-center gap-1.5 max-lg:hidden" :class="c.driver === 'json-file' && !c.rotation ? 'text-ink-amber-6' : 'text-ink-gray-7'"><span class="size-2 rounded-full" :class="c.driver !== 'json-file' ? 'bg-surface-gray-4' : c.rotation ? 'bg-surface-green-5' : 'bg-surface-amber-5'" aria-hidden="true" />{{ rotationText(c) }}</span>
          <span class="text-right tabular-nums text-ink-gray-7">{{ dockerSizeText(c) }}</span>
        </div>
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-outline-gray-1 px-3 py-1.5 text-xs text-ink-gray-5">
          <span v-if="r.docker.compose_rotation">Compose file: rotate at {{ r.docker.compose_rotation.max_size }}, keep {{ r.docker.compose_rotation.max_file }}<template v-if="r.docker.containers.some((c) => c.driver === 'json-file' && !c.rotation)"> (binds when the containers are next recreated)</template></span>
          <span v-else>Compose file: no logging options yet</span>
          <span v-if="r.docker.method === 'helper'">Measured through a read-only helper container</span>
          <span v-if="r.docker.note">{{ r.docker.note }}</span>
        </div>
      </div>

      <div v-if="r.benches.length">
        <div class="grid grid-cols-[minmax(0,1.6fr)_7rem_8rem_8rem] items-center gap-3 bg-surface-gray-1 px-3 py-1.5 text-xs text-ink-gray-5 max-lg:grid-cols-[minmax(0,1fr)_8rem]"><span>Bench logs</span><span class="max-lg:hidden">Current</span><span class="max-lg:hidden">Rotated</span><span class="text-right">Actions</span></div>
        <div v-for="b in r.benches" :key="b.bench_path" class="grid grid-cols-[minmax(0,1.6fr)_7rem_8rem_8rem] items-center gap-3 border-t border-outline-gray-1 px-3 py-2 text-sm max-lg:grid-cols-[minmax(0,1fr)_8rem]">
          <span class="min-w-0"><span class="block truncate text-ink-gray-9">{{ benchName(b.bench_path) }}</span><span class="block truncate text-xs text-ink-gray-5" :title="b.bench_path">{{ b.bench_path }}/logs and sites/*/logs · {{ b.files }} {{ b.files === 1 ? 'file' : 'files' }}</span></span>
          <span class="tabular-nums text-ink-gray-7 max-lg:hidden">{{ sizeText(b.current_bytes) }}</span>
          <span class="tabular-nums max-lg:hidden" :class="b.rotated_bytes ? 'text-ink-gray-9' : 'text-ink-gray-5'">{{ sizeText(b.rotated_bytes) }} <span class="text-xs text-ink-gray-5">in {{ b.rotated_files.length }}</span></span>
          <span class="flex justify-end"><Tooltip :text="b.rotated_files.length ? 'Removes the rotated files; every current log is kept' : 'Nothing rotated to remove'"><Button v-if="admin" size="sm" variant="subtle" :disabled="!b.rotated_files.length" label="Remove rotated…" @click="prune(r, b)" /></Tooltip></span>
        </div>
      </div>
    </section>

    <section v-if="journal" class="rounded-6 border border-outline-gray-1 bg-surface-elevation-1 px-3 py-3">
      <div class="flex flex-wrap items-center gap-2"><span class="lucide-scroll-text size-4 text-ink-gray-5" aria-hidden="true" /><span class="text-base-medium text-ink-gray-9">Host journal (systemd-journald)</span><Badge v-if="!journal.applies" theme="gray" variant="subtle" label="does not apply here" /></div>
      <p class="mt-1 text-sm text-ink-gray-6">{{ journal.applies ? (journal.usage_text || 'journalctl --disk-usage gave no answer.') : journal.reason }}</p>
      <p v-if="journal.applies && journal.size == null && journal.reason" class="mt-1 text-sm text-ink-amber-6">{{ journal.reason }}</p>
      <template v-if="journal.applies">
        <div v-if="journal.privileged" class="mt-2 flex flex-wrap items-center gap-2"><span class="text-xs text-ink-gray-5">cwcli runs as root here, so it can trim the journal itself.</span><Button v-if="admin" size="sm" variant="subtle" theme="red" icon-left="lucide-eraser" label="Trim the journal…" @click="vacuum()" /></div>
        <template v-else>
          <p class="mt-2 text-xs text-ink-gray-5">Vacuuming needs root and cwcli does not run as root here, so it shows the commands instead of running them. Copy one into a terminal:</p>
          <div class="mt-2 grid gap-2 sm:grid-cols-2"><CommandLine v-for="cmd in journal.commands" :key="cmd" :command="cmd" wrap /></div>
        </template>
      </template>
    </section>
  </div>
</template>
