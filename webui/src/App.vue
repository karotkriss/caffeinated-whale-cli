<script setup>
import { FrappeUIProvider } from 'frappe-ui'
import Shell from './components/Shell.vue'
import SignIn from './components/SignIn.vue'
import PasswordForm from './components/PasswordForm.vue'
import { session } from './data/session'
</script>

<template>
  <FrappeUIProvider>
    <div v-if="!session.booted" class="min-h-full bg-surface-gray-1" />
    <SignIn v-else-if="session.signedOut" />
    <!-- The first sign-in (and every one after a reset) picks a real password before anything else. -->
    <div v-else-if="session.user?.mustChange" class="flex min-h-full items-center justify-center bg-surface-gray-1 px-4">
      <div class="w-full max-w-sm rounded-6 border border-outline-gray-1 bg-surface-base p-6">
        <div class="flex items-center gap-3">
          <img src="/logo-whale.png" alt="" class="size-10 rounded-4" />
          <div>
            <h1 class="text-lg-semibold text-ink-gray-9">Choose your password</h1>
            <p class="text-p-sm text-ink-gray-6">The account still has the default password. Set your own to continue.</p>
          </div>
        </div>
        <div class="mt-6"><PasswordForm forced /></div>
      </div>
    </div>
    <Shell v-else />
  </FrappeUIProvider>
</template>
