<script setup>
import { ref } from 'vue'
import { Button, FormControl, ErrorMessage } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { signInWithToken } from '../data/session'
import { startFleet } from '../data/fleet'

const token = ref('')
const error = ref(null)
const busy = ref(false)

async function submit() {
  busy.value = true
  error.value = null
  const ok = await signInWithToken(token.value.trim())
  busy.value = false
  if (ok) {
    token.value = ''
    startFleet()
  } else {
    error.value = 'That token was not accepted.'
  }
}
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-surface-gray-1 px-4">
    <div class="w-full max-w-md rounded-6 border border-outline-gray-1 bg-surface-base p-6">
      <div class="flex items-center gap-3">
        <img src="/logo-whale.png" alt="" class="size-10 rounded-4" />
        <div>
          <h1 class="text-lg-semibold text-ink-gray-9">Sign in to cwcli</h1>
          <p class="text-p-sm text-ink-gray-6">This daemon only answers a signed-in page.</p>
        </div>
      </div>
      <div class="mt-6 space-y-2">
        <p class="text-p-sm text-ink-gray-7">Get a fresh one-time sign-in link from the terminal:</p>
        <CommandLine command="cwcli ui" />
      </div>
      <form class="mt-6 space-y-3" @submit.prevent="submit">
        <FormControl
          v-model="token"
          type="password"
          label="Or paste the daemon's token"
          description="Only if you started it with CWCLI_SERVE_TOKEN set."
          autocomplete="off"
        />
        <ErrorMessage :message="error" />
        <div class="flex justify-end">
          <Button variant="solid" type="submit" :loading="busy" :disabled="!token.trim()" label="Sign in" />
        </div>
      </form>
    </div>
  </div>
</template>
