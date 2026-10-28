<script setup>
// The page's twin of cwcli's own prompt: "Project <p> is not running. Start it?"
import { computed } from 'vue'
import { Button, Dialog, ErrorMessage } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { answerStart, startGate } from '../data/start-gate'

const errorText = computed(() => (startGate.error ? (startGate.error.hint ? `${startGate.error.message} ${startGate.error.hint}` : startGate.error.message) : null))
function onOpen(open) {
  if (!open && startGate.open) answerStart(false)
}
</script>

<template>
  <Dialog :open="startGate.open" :title="startGate.benchLabel && instance(startGate.project)?.container_running ? `${startGate.benchLabel} is not running` : `${startGate.project} is not running`" size="sm" @update:open="onOpen">
    <div class="space-y-3">
      <p class="text-p-base text-ink-gray-7">
        {{ startGate.verb ? `${startGate.verb} needs ${startGate.benchLabel ? `${startGate.project} and its bench ${startGate.benchLabel}` : startGate.project} running.` : `This needs ${startGate.project} running.` }}
        Start it now? The same question cwcli asks in a terminal; the verb runs once the instance is up.
      </p>
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running</div>
        <CommandLine :command="`cwcli start ${startGate.project}${startGate.bench != null ? ` --bench ${startGate.bench}` : ''}`" />
      </div>
      <ErrorMessage :message="errorText" />
    </div>
    <template #actions>
      <div class="flex justify-end gap-2">
        <Button label="No, leave it stopped" :disabled="startGate.starting" @click="answerStart(false)" />
        <Button variant="solid" data-primary :label="startGate.starting ? 'Starting...' : `Start ${startGate.project}`" :loading="startGate.starting" @click="answerStart(true)" />
      </div>
    </template>
  </Dialog>
</template>
