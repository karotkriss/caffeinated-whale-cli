import { reactive } from 'vue'
import { refuseBelow } from './session'

// The restore wizard's state: which set, which target, the typed confirm. One instance
// mounted in the Shell; opened from a set's menu anywhere.
export const restoreWizard = reactive({ open: false, set: null })
export function openRestoreWizard(set) {
  if (refuseBelow()) return
  restoreWizard.set = set
  restoreWizard.open = true
}
