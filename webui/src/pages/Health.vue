<script setup>
import { onMounted } from 'vue'
import { PageHeader, PageHeaderTitle } from 'frappe-ui'
import DoctorView from '../components/DoctorView.vue'
import CommandLine from '../components/CommandLine.vue'
import { setFocus } from '../data/fleet'

onMounted(() => setFocus(null))
</script>

<template>
  <PageHeader><PageHeaderTitle title="Health check" /></PageHeader>
  <div class="mx-auto max-w-4xl px-3 pb-20 pt-5 sm:px-5">
    <p class="mb-4 text-p-base text-ink-gray-6">Can cwcli operate on this machine: Docker, its own files, disk space, and the tools the credential bridge and transfers rely on. It never reports on an instance; that is what the instance pages are for.</p>
    <DoctorView />
    <div class="mt-6 max-w-sm"><CommandLine command="cwcli doctor" /></div>
  </div>
</template>
