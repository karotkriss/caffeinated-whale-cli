<script setup>
import { Button, toast } from 'frappe-ui'
const props = defineProps({ command: { type: String, required: true } })
async function copy() {
  try {
    await navigator.clipboard.writeText(props.command)
    toast.success('Command copied', { id: 'copy' })
  } catch {
    toast.error('Copy failed; select the command instead', { id: 'copy' })
  }
}
</script>

<template>
  <div class="flex min-w-0 items-center gap-2 rounded-4 border border-outline-gray-1 bg-surface-gray-1 py-1 pl-2.5 pr-1">
    <span class="select-none font-mono text-sm text-ink-gray-4">$</span>
    <code class="min-w-0 flex-1 truncate font-mono text-sm text-ink-gray-8" :title="command">{{ command }}</code>
    <Button size="xs" variant="ghost" icon="lucide-copy" aria-label="Copy command" @click="copy" />
  </div>
</template>
