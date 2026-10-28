<script setup>
// Username and password, nothing in the URL. A wrong pair and a locked account
// are the two errors a person can act on; both are said in plain words.
import { computed, ref } from 'vue'
import { Button, Checkbox, ErrorMessage, FormControl } from 'frappe-ui'
import { session, signIn } from '../data/session'

// With directory sign-in on, most people type their directory name; the local
// admin account still signs in with its own password.
const directory = computed(() => !!session.meta?.directory_sign_in)
const username = ref(session.meta?.directory_sign_in ? '' : 'admin')
const password = ref('')
const remember = ref(false)
const busy = ref(false)
const error = ref('')

async function submit() {
  if (busy.value) return
  error.value = ''
  busy.value = true
  try {
    await signIn(username.value.trim(), password.value, remember.value)
  } catch (e) {
    if (e.code === 'auth.locked') error.value = `Too many failed sign-ins. Try again in ${e.retryAfter || 60} seconds.`
    else if (e.code === 'auth.invalid') error.value = 'That username and password do not match.'
    else error.value = [e.message || 'Could not sign in.', e.hint].filter(Boolean).join(' ')
    password.value = ''
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-surface-gray-1 px-4">
    <form class="w-full max-w-sm rounded-6 border border-outline-gray-1 bg-surface-base p-6" @submit.prevent="submit">
      <div class="flex items-center gap-3">
        <img src="/logo-whale.png" alt="" class="size-10 rounded-4" />
        <div>
          <h1 class="text-lg-semibold text-ink-gray-9">Sign in to cwcli</h1>
          <p class="text-p-sm text-ink-gray-6">{{ directory ? 'Use your directory account, or the local admin account.' : 'The web UI for your Frappe instances.' }}</p>
        </div>
      </div>
      <div class="mt-6 space-y-4">
        <FormControl v-model="username" label="Username" autocomplete="username" spellcheck="false" autofocus />
        <FormControl v-model="password" label="Password" type="password" autocomplete="current-password" />
        <Checkbox v-model="remember" label="Keep me signed in for 30 days" />
        <ErrorMessage :message="error" />
        <Button variant="solid" type="submit" class="w-full" label="Sign in" :loading="busy" />
      </div>
    </form>
  </div>
</template>
