<script setup>
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, FormControl, ErrorMessage } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { ACTIONS, availability } from '../data/actions'
import { instance } from '../data/fleet'
import { dialogState, runAction } from '../data/dialog'

const typed = ref('')
watch(() => dialogState.open, () => (typed.value = ''))

const action = computed(() => (dialogState.id ? ACTIONS[dialogState.id] : null))
const ctx = computed(() => dialogState.ctx)
// The name typed back for a destructive verb: the action's own choice (an app),
// else the most specific target on the page - site, then bench, then instance.
const target = computed(() => {
  if (action.value?.confirmTarget) return action.value.confirmTarget(ctx.value) || ''
  return ctx.value.site || ctx.value.benchName || ctx.value.project || ''
})
const title = computed(() => {
  if (!action.value) return ''
  const on = ctx.value.process || ctx.value.app || ctx.value.site || ctx.value.benchName || ctx.value.project
  return on ? `${action.value.label}: ${on}` : action.value.label
})
const inst = computed(() => instance(ctx.value.project))
const avail = computed(() =>
  availability(
    dialogState.id,
    inst.value,
    inst.value?.benches?.find((b) => String(b.index) === ctx.value.bench),
    dialogState.values,
  ),
)
const needsTyped = computed(() => action.value?.confirm === 'typed')
const textFields = computed(() => (action.value?.fields || []).filter((f) => f.type !== 'checkbox'))
const checkboxes = computed(() => (action.value?.fields || []).filter((f) => f.type === 'checkbox'))
// Every non-password text field is required; a password field left blank means its default.
const fieldsFilled = computed(() =>
  textFields.value.every((f) => f.type === 'password' || String(dialogState.values[f.name] || '').trim()),
)
const ready = computed(() => {
  if (!action.value || !avail.value.ok || dialogState.running) return false
  if (!fieldsFilled.value) return false
  if (needsTyped.value) return !!target.value && typed.value.trim() === target.value
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
      <p class="text-p-base text-ink-gray-7">{{ action.says(ctx, dialogState.values) }}</p>
      <FormControl
        v-for="f in textFields"
        :key="f.name"
        v-model="dialogState.values[f.name]"
        :type="f.type || 'text'"
        :label="f.label"
        :placeholder="f.placeholder"
        :autocomplete="f.type === 'password' ? 'new-password' : 'off'"
      />
      <Checkbox v-for="f in checkboxes" :key="f.name" v-model="dialogState.values[f.name]" :label="f.label" />
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
        :description="avail.detail || `cwcli refuses this on a stopped instance rather than starting it behind your back. Start ${ctx.project} first.`"
      />
      <ErrorMessage :message="errorText" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid"
          :theme="action?.danger ? 'red' : 'gray'"
          :disabled="!ready"
          :loading="dialogState.running"
          :label="action?.label || 'Confirm'"
          @click="confirm"
        />
      </div>
    </template>
  </Dialog>
</template>
