<script setup>
import { computed, ref, watch } from 'vue'
import { Alert, Button, Dialog, FormControl, ErrorMessage } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { ACTIONS, availability } from '../data/actions'
import { instance } from '../data/fleet'
import { dialogState, runAction } from '../data/dialog'

const typed = ref('')
watch(() => dialogState.open, () => (typed.value = ''))

const action = computed(() => (dialogState.id ? ACTIONS[dialogState.id] : null))
const ctx = computed(() => dialogState.ctx)
const target = computed(() => ctx.value.site || ctx.value.benchName || ctx.value.project || '')
const title = computed(() => {
  if (!action.value) return ''
  const on = ctx.value.process || ctx.value.app || target.value
  return on ? `${action.value.label}: ${on}` : action.value.label
})
const inst = computed(() => instance(ctx.value.project))
const avail = computed(() =>
  availability(dialogState.id, inst.value, inst.value?.benches?.find((b) => String(b.index) === ctx.value.bench)),
)
const needsTyped = computed(() => action.value?.confirm === 'typed')
const ready = computed(() => {
  if (!action.value || !avail.value.ok || dialogState.running) return false
  if (action.value.field && !String(dialogState.values[action.value.field.name] || '').trim()) return false
  if (needsTyped.value) return typed.value.trim() === target.value
  return true
})
const errorText = computed(() => {
  const e = dialogState.error
  if (!e) return null
  return e.hint ? `${e.message} ${e.hint}` : e.message
})

function confirm() {
  runAction(dialogState.id, ctx.value, { ...dialogState.values })
}
</script>

<template>
  <Dialog v-model:open="dialogState.open" :title="title" size="lg">
    <div v-if="action" class="space-y-4">
      <p class="text-p-base text-ink-gray-7">{{ action.says(ctx) }}</p>
      <FormControl
        v-if="action.field"
        v-model="dialogState.values[action.field.name]"
        :label="action.field.label"
        :placeholder="action.field.placeholder"
        autocomplete="off"
      />
      <div class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running</div>
        <CommandLine :command="action.cli(ctx, dialogState.values)" />
      </div>
      <FormControl
        v-if="needsTyped"
        v-model="typed"
        :label="`Type ${target} to confirm`"
        :placeholder="target"
        autocomplete="off"
      />
      <Alert
        v-if="!avail.ok"
        theme="gray"
        :title="avail.reason"
        :description="`cwcli refuses this on a stopped instance rather than starting it behind your back. Start ${ctx.project} first.`"
      />
      <ErrorMessage :message="errorText" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button variant="solid" :disabled="!ready" :loading="dialogState.running" :label="action?.label || 'Confirm'" @click="confirm" />
      </div>
    </template>
  </Dialog>
</template>
