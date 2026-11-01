<script setup>
// Asked before Open site opens an address that carries the bench's internal port on a
// name that is not a localhost one (core.url's internal_port). The address is what
// Frappe itself reports while webserver_port is set, so the page keeps it and explains
// the administrator's fix instead of rewriting it.
import { computed, ref, watch } from 'vue'
import { Button, Dialog } from 'frappe-ui'
import CommandLine from './CommandLine.vue'
import { answerPortWarning, portWarning } from '../data/dialog'
import { onHost } from '../data/hosts'

const probe = computed(() => portWarning.probe || {})
const files = computed(() => (probe.value.webserver_port_in || []).join(' and '))
const showFix = ref(false)
watch(() => portWarning.open, (open) => { if (open) showFix.value = false })

function onOpen(open) {
  if (!open && portWarning.open) answerPortWarning(false)
}
</script>

<template>
  <Dialog :open="portWarning.open" title="This address includes the bench's port" icon="lucide-triangle-alert" size="xl" @update:open="onOpen">
    <div class="space-y-3">
      <p class="text-p-base text-ink-gray-7">
        Frappe reports <span class="break-all font-mono text-sm text-ink-gray-8">{{ probe.url }}</span> for {{ probe.site }}, because <span class="font-mono text-sm">webserver_port</span> is set in {{ files }}.
        The port in it is the bench's own web port. If {{ probe.site }} is served by its hostname through a reverse proxy, that port is internal and this address does not open.
      </p>
      <section class="rounded-6 border border-outline-gray-1">
        <button type="button" class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm" :aria-expanded="showFix" @click="showFix = !showFix">
          <span class="size-3.5 shrink-0 text-ink-gray-4" :class="showFix ? 'lucide-chevron-down' : 'lucide-chevron-right'" aria-hidden="true" />
          <span class="min-w-0 break-all text-ink-gray-8">Is {{ probe.url }} not the right address?</span>
        </button>
        <div v-if="showFix" class="space-y-3 border-t border-outline-gray-1 px-3 py-3">
          <p class="text-p-sm text-ink-gray-7">
            Remove <span class="font-mono">webserver_port</span>, and Frappe builds the site's URLs without the port, so cwcli opens the site by its hostname.
            Run these{{ onHost(portWarning.host) }}, in order:
          </p>
          <ol class="space-y-2.5">
            <li v-for="(s, i) in probe.fix || []" :key="s.command" class="space-y-1">
              <div class="text-sm text-ink-gray-7">{{ i + 1 }}. {{ s.step }}</div>
              <CommandLine :command="s.command" wrap />
            </li>
          </ol>
          <p class="text-p-sm text-ink-gray-5">Then copy any OAuth or SSO redirect URL from Frappe again: URLs made while the key was set include the port.</p>
        </div>
      </section>
    </div>
    <template #actions>
      <div class="flex justify-end gap-2">
        <Button label="Cancel" @click="answerPortWarning(false)" />
        <Button variant="solid" data-primary label="Open anyway" @click="answerPortWarning(true)" />
      </div>
    </template>
  </Dialog>
</template>
