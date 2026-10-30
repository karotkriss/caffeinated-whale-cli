<script setup>
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, FormControl, ErrorMessage } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { ACTIONS, availability } from '../data/actions'
import { instance } from '../data/fleet'
import { dialogState, runAction } from '../data/dialog'
import { onHost } from '../data/hosts'

const typed = ref('')

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
  return (on ? `${action.value.label}: ${on}` : action.value.label) + onHost(ctx.value.host)
})
const inst = computed(() => instance(ctx.value.project, ctx.value.host))
const avail = computed(() =>
  availability(
    dialogState.id,
    inst.value,
    inst.value?.benches?.find((b) => String(b.index) === ctx.value.bench),
    dialogState.values,
  ),
)
const needsTyped = computed(() => action.value?.confirm === 'typed')
const shown = computed(() => (action.value?.fields || []).filter((f) => !f.show || f.show(ctx.value)))
const textFields = computed(() => shown.value.filter((f) => f.type !== 'checkbox'))
const checkboxes = computed(() => shown.value.filter((f) => f.type === 'checkbox'))
const command = computed(() => (action.value ? action.value.cli(ctx.value, dialogState.values) : null))
watch(() => [dialogState.open, ...checkboxes.value.map((f) => !!dialogState.values[f.name])], () => (typed.value = ''))
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
        :description="f.description"
        :autocomplete="f.type === 'password' ? 'new-password' : 'off'"
      />
      <Checkbox v-for="f in checkboxes" :key="f.name" v-model="dialogState.values[f.name]" :label="f.label" :description="f.note?.(ctx)" />
      <Alert
        v-if="action.danger_when && action.danger_when(dialogState.values)"
        theme="red"
        title="No backup will be taken. This cannot be undone."
        :description="`Every database, site and file of ${ctx.project} is deleted the moment you confirm. Nothing is archived, so there is no restore. Untick Force to keep the verified backup, or tick Keep the named volumes to leave the data on Docker.`"
      />
      <div v-if="command" class="space-y-1.5">
        <div class="text-sm text-ink-gray-5">Same as running{{ onHost(ctx.host) }}</div>
        <CommandLine :command="command" />
      </div>
      <FormControl
        v-if="needsTyped"
        v-model="typed"
        :label="`Type ${target} to confirm`"
        :placeholder="target"
        autocomplete="off"
      />
      <Alert v-if="!avail.ok" theme="gray" :title="avail.reason" :description="avail.detail" />
      <Alert v-else-if="avail.startFirst" theme="gray" :title="`${ctx.project} is stopped`" :description="`cwcli will ask to start ${ctx.project} first, then run this, the same as in a terminal.`" />
      <ErrorMessage :message="errorText" />
    </div>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid"
          data-primary
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
