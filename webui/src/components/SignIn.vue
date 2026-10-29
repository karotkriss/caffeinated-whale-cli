<script setup>
// Username and password, nothing in the URL. A wrong pair and a locked account
// are the two errors a person can act on; both are said in plain words.
import { computed, ref } from 'vue'
import { Badge, Button, Checkbox, ErrorMessage, FormControl } from 'frappe-ui'
import { session, signIn } from '../data/session'
// The sign-in page: a split layout with the form on the left and the moving gradient on the right carrying the
// wordmark (ShaderGradient + the effects gates from webui/src/premium/).
import ShaderGradient from '../premium/ShaderGradient.vue'
import { active, motion, stops } from '../premium/effects'
// Wordmark face: Inter Display (Inter 4 variable at optical size 32), OFL-1.1, self-hosted from fontsource.
import('@fontsource-variable/inter/opsz.css')

// With directory sign-in on, most people type their directory name; the local
// admin account still signs in with its own password.
const directory = computed(() => !!session.meta?.directory_sign_in)
const username = ref(session.meta?.directory_sign_in ? '' : 'admin')
const password = ref('')
const remember = ref(false)
const busy = ref(false)
const error = ref('')
const copy = computed(() => (directory.value ? 'Use your directory account, or the local admin account.' : 'The web UI for your Frappe instances.'))

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
  <!-- Split page. One column on a phone (gradient band on top, form below), two halves from md up. -->
  <div class="cw-c-signin grid min-h-full grid-rows-[auto,1fr] bg-surface-gray-1 md:grid-cols-2 md:grid-rows-1">
    <section class="relative order-first h-52 overflow-hidden md:order-last md:h-auto" :class="!active && 'bg-surface-gray-3'" aria-hidden="true">
      <!-- Absolutely positioned so the canvas's own pixel size never sets the section's height on a resize. -->
      <div v-if="active" class="absolute inset-0"><ShaderGradient :stops="stops" :animate="motion" :scale="0.55" :grain="0.06" /></div>
      <div class="absolute inset-0 flex items-center justify-center px-6">
        <p class="cw-c-wordmark text-center leading-[0.95] text-ink-gray-9">
          <span class="block">Caffeinated</span>
          <span class="block">Whale</span>
        </p>
      </div>
      <div class="cw-c-signin-foot absolute inset-x-0 bottom-0 hidden p-8 md:block lg:p-10">
        <Badge v-if="session.meta?.version" variant="subtle" theme="gray" size="md">cwcli {{ session.meta.version }}</Badge>
        <h2 class="mt-3 text-[1.5rem] font-semibold leading-tight tracking-tight text-ink-gray-9 lg:text-[1.875rem]">All your Frappe benches in one place.</h2>
        <p class="mt-1 max-w-md text-p-base text-ink-gray-7">Start, update, back up and restore your instances from one screen.</p>
      </div>
    </section>
    <div class="flex items-center justify-center px-6 py-10 md:px-10">
      <form class="w-full max-w-xs" @submit.prevent="submit">
        <h1 class="text-center text-[1.5rem] font-semibold tracking-tight text-ink-gray-9">Sign in</h1>
        <p class="mt-2 text-center text-p-sm text-ink-gray-6">{{ copy }}</p>
        <div class="mt-8 space-y-4">
          <FormControl v-model="username" label="Username" autocomplete="username" spellcheck="false" autofocus />
          <FormControl v-model="password" label="Password" type="password" autocomplete="current-password" />
          <Checkbox v-model="remember" label="Keep me signed in" />
          <ErrorMessage :message="error" />
          <Button variant="solid" type="submit" class="w-full !rounded-full" size="md" label="Sign in" :loading="busy" />
        </div>
      </form>
    </div>
  </div>
</template>

