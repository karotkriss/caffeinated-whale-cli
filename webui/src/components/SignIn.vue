<script setup>
import CommandLine from './CommandLine.vue'
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
    </div>
  </div>
</template>
