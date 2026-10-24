<script setup>
import { FrappeUIProvider } from 'frappe-ui'
import Shell from './components/Shell.vue'
import SignIn from './components/SignIn.vue'
import { session } from './data/session'
</script>

<template>
  <FrappeUIProvider>
    <SignIn v-if="session.signedOut" />
    <Shell v-else />
  </FrappeUIProvider>
</template>
