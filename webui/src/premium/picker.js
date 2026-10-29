import { reactive } from 'vue'
export const picker = reactive({ open: false })
export const openPicker = () => (picker.open = true)
