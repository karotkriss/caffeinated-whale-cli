<script setup>
import { computed, ref, watch } from 'vue'
import { Alert, Button, Checkbox, Dialog, ErrorMessage, FormControl } from 'frappe-ui'
import { jobDialog, submitJob } from '../data/job-dialog'
import { forceConfirmTarget, forceConfirmed } from '../data/force-confirm'

const titles = {
  init: 'Create an instance',
  'apps-install': 'Install apps',
  'apps-update': 'Update apps',
  backup: 'Back up site',
  migrate: 'Migrate site',
  'run-tests': 'Run tests',
  build: 'Build assets',
  'setup-wizard': 'Complete setup wizard',
  'self-update': 'Update cwcli',
}

const descriptions = {
  'apps-install': 'Fetches each app, installs it on the selected sites, then verifies the serving bench.',
  'apps-update': 'Runs the same preflight and update path as the CLI. Findings appear in the job before any reset.',
  backup: 'Creates a database backup inside the bench. Include public and private files when you need a complete site archive.',
  migrate: 'Applies pending schema patches under maintenance mode. A failed patch is not transactional.',
  'run-tests': 'Runs app test code against the named site. Tests can create, change, and delete data.',
  build: 'Compiles the bench assets, optionally for one app.',
  'setup-wizard': 'Completes Frappe setup for this site with the selected locale defaults.',
  'self-update': 'Updates this cwcli install. The local daemon restarts and this tab reloads when the protocol changes.',
}

const v = computed(() => jobDialog.values)
const isInit = computed(() => jobDialog.verb === 'init')
const title = computed(() => titles[jobDialog.verb] || 'Start job')
const typed = ref('')
watch(() => jobDialog.open, () => (typed.value = ''))
const forceTarget = computed(() => (jobDialog.verb === 'apps-update' ? forceConfirmTarget(v.value, jobDialog.context) : ''))
const canContinue = computed(() => {
  if (!isInit.value) return jobDialog.verb !== 'apps-update' || forceConfirmed(v.value, jobDialog.context, typed.value)
  if (jobDialog.step === 0) return !!v.value.project?.trim() && !!v.value.bench_name?.trim() && !!v.value.site_name?.trim() && Number(v.value.port) > 0
  if (jobDialog.step === 1) return !!v.value.version?.trim() && !!v.value.bench_parent?.trim()
  return !!v.value.admin_password
})
const error = computed(() => jobDialog.error ? `${jobDialog.error.message}${jobDialog.error.hint ? ` ${jobDialog.error.hint}` : ''}` : null)

function next() {
  if (jobDialog.step < 2) jobDialog.step += 1
}
</script>

<template>
  <Dialog v-model:open="jobDialog.open" :title="title" size="xl">
    <div class="space-y-5">
      <template v-if="isInit">
        <div class="flex items-center gap-2 text-sm text-ink-gray-5">
          <template v-for="(label, index) in ['Instance', 'Versions', 'Secrets']" :key="label">
            <span class="flex size-6 items-center justify-center rounded-full" :class="index === jobDialog.step ? 'bg-surface-gray-10 text-ink-base' : index < jobDialog.step ? 'bg-surface-green-2 text-ink-green-7' : 'bg-surface-gray-2 text-ink-gray-5'">{{ index + 1 }}</span>
            <span :class="index === jobDialog.step ? 'text-ink-gray-8' : ''">{{ label }}</span>
            <span v-if="index < 2" class="h-px flex-1 bg-outline-gray-2" />
          </template>
        </div>

        <div v-if="jobDialog.step === 0" class="grid gap-4 sm:grid-cols-2">
          <FormControl v-model="v.project" label="Instance name" placeholder="my-instance" autocomplete="off" />
          <FormControl v-model="v.port" label="First web port" type="number" />
          <FormControl v-model="v.bench_name" label="Bench name" autocomplete="off" />
          <FormControl v-model="v.site_name" label="Site name" autocomplete="off" />
        </div>
        <div v-else-if="jobDialog.step === 1" class="grid gap-4 sm:grid-cols-2">
          <FormControl v-model="v.version" label="Frappe version" placeholder="16 or 16.3.0" autocomplete="off" />
          <FormControl v-model="v.bench_parent" label="Bench parent" autocomplete="off" />
          <FormControl v-model="v.frappe_url" class="sm:col-span-2" label="Frappe repository (optional)" placeholder="https://github.com/frappe/frappe" autocomplete="off" />
          <Checkbox v-model="v.install_erpnext" label="Install ERPNext" />
          <Checkbox v-model="v.start" label="Start the instance when ready" />
        </div>
        <div v-else class="space-y-4">
          <Alert theme="gray" title="Passwords are never written to the job record" description="They pass directly to the detached worker environment and are removed as soon as the worker starts." />
          <div class="grid gap-4 sm:grid-cols-2">
            <FormControl v-model="v.admin_password" label="Administrator password" type="password" autocomplete="new-password" />
            <FormControl v-model="v.db_root_password" label="Database root password" type="password" autocomplete="new-password" />
            <FormControl
              v-model="v.reuse_bench"
              class="sm:col-span-2"
              label="If the bench already exists"
              type="select"
              :options="[
                { label: 'Ask before reusing', value: '' },
                { label: 'Reuse it', value: 'reuse' },
                { label: 'Refuse', value: 'refuse' },
              ]"
            />
          </div>
        </div>
      </template>

      <template v-else>
        <p class="text-p-base text-ink-gray-6">{{ descriptions[jobDialog.verb] }}</p>

        <div v-if="jobDialog.verb === 'apps-install'" class="grid gap-4 sm:grid-cols-2">
          <FormControl v-model="v.apps" class="sm:col-span-2" label="Apps" placeholder="payments, https://github.com/acme/custom-app" />
          <FormControl v-model="v.sites" label="Sites (optional)" placeholder="site.localhost" />
          <FormControl v-model="v.branch" label="Branch (optional)" placeholder="develop" />
          <Checkbox v-model="v.fetch_only" label="Fetch only; do not install on sites" />
        </div>

        <div v-else-if="jobDialog.verb === 'apps-update'" class="space-y-4">
          <div class="grid gap-4 sm:grid-cols-2">
            <FormControl v-model="v.apps" label="Apps (optional)" placeholder="frappe, erpnext" />
            <FormControl v-model="v.sites" label="Sites (optional)" placeholder="site.localhost" />
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <Checkbox v-model="v.build" label="Build assets" />
            <Checkbox v-model="v.clear_cache" label="Clear cache" />
            <Checkbox v-model="v.clear_website_cache" label="Clear website cache" />
            <Checkbox v-model="v.skip_maintenance" label="Skip maintenance mode" />
            <Checkbox v-model="v.force" label="Force: discard uncommitted and unpushed app work, re-own app repos" />
          </div>
          <Alert theme="blue" title="Preflight runs first" description="Dirty trees, branch drift, and data-loss risks are collected and shown together. A blocker stops the update. A frappe update refuses when an app has uncommitted or unpushed work unless Force is ticked, which discards that work permanently." />
          <FormControl v-if="forceTarget" v-model="typed" :label="`Type ${forceTarget} to confirm`" autocomplete="off" />
        </div>

        <Checkbox v-else-if="jobDialog.verb === 'backup'" v-model="v.with_files" label="Include public and private files" />

        <div v-else-if="jobDialog.verb === 'run-tests'" class="grid gap-4 sm:grid-cols-2">
          <FormControl v-model="v.app" label="App" placeholder="my_app" />
          <FormControl v-model="v.module" label="Test module (optional)" placeholder="my_app.tests.test_example" />
        </div>

        <FormControl v-else-if="jobDialog.verb === 'build'" v-model="v.app" label="App (optional)" placeholder="Build every app when blank" />

        <div v-else-if="jobDialog.verb === 'setup-wizard'" class="grid gap-4 sm:grid-cols-2">
          <FormControl v-model="v.country" label="Country" />
          <FormControl v-model="v.currency" label="Currency" />
          <FormControl v-model="v.timezone" label="Timezone" />
          <FormControl v-model="v.language" label="Language" />
        </div>

        <Checkbox v-else-if="jobDialog.verb === 'self-update'" v-model="v.no_cache" label="Check the package index now" />
      </template>

      <ErrorMessage :message="error" />
      <p class="text-p-sm text-ink-gray-5">The job continues if you close this tab. You can return to it from Jobs.</p>
    </div>

    <template #actions="{ close }">
      <div class="flex justify-between gap-2">
        <Button v-if="isInit && jobDialog.step" label="Back" @click="jobDialog.step -= 1" />
        <span v-else />
        <div class="flex gap-2">
          <Button label="Cancel" @click="close" />
          <Button v-if="isInit && jobDialog.step < 2" variant="solid" label="Continue" :disabled="!canContinue" @click="next" />
          <Button v-else variant="solid" label="Start job" :loading="jobDialog.running" :disabled="!canContinue" @click="submitJob" />
        </div>
      </div>
    </template>
  </Dialog>
</template>
