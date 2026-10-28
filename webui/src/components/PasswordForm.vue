<script setup>
// New password + confirm. `forced` is the first sign-in (and after a reset): the
// sign-in a moment ago proved the current password, so it is not asked again.
import { computed, ref } from 'vue'
import { Button, ErrorMessage, FormControl } from 'frappe-ui'
import { changePassword } from '../data/session'

const props = defineProps({ forced: { type: Boolean, default: false } })
const emit = defineEmits(['done'])

const current = ref('')
const next = ref('')
const confirm = ref('')
const busy = ref(false)
const error = ref('')
const problem = computed(() => {
  if (!next.value) return ''
  if (next.value.length < 8) return 'At least 8 characters.'
  if (next.value.toLowerCase() === 'admin') return 'Not the default password.'
  if (confirm.value && confirm.value !== next.value) return 'The two passwords differ.'
  return ''
})
const canSubmit = computed(() => next.value && confirm.value === next.value && !problem.value && (props.forced || current.value))

async function submit() {
  if (busy.value || !canSubmit.value) return
  error.value = ''
  busy.value = true
  try {
    await changePassword(props.forced ? null : current.value, next.value)
    current.value = next.value = confirm.value = ''
    emit('done')
  } catch (e) {
    error.value = e.message || 'Could not change the password.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="space-y-4" @submit.prevent="submit">
    <FormControl v-if="!forced" v-model="current" label="Current password" type="password" autocomplete="current-password" />
    <FormControl v-model="next" label="New password" type="password" autocomplete="new-password" description="At least 8 characters, not the default." />
    <FormControl v-model="confirm" label="Confirm new password" type="password" autocomplete="new-password" />
    <ErrorMessage :message="error || problem" />
    <Button variant="solid" type="submit" :class="forced ? 'w-full' : ''" :label="forced ? 'Set password and continue' : 'Change password'" :disabled="!canSubmit" :loading="busy" />
  </form>
</template>
