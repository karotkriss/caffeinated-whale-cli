// The premium layer's one store: effects on/off, the gradient theme, the colour
// scheme, and the two environmental gates (prefers-reduced-motion, hidden tab).
// `motion` is the only flag the shader and motion-v consumers read: it is true
// only when effects are on AND the OS does not ask for reduced motion AND the
// tab is visible. Persisted in localStorage next to frappe-ui's own `theme` key.
import { computed, reactive, watch, watchEffect } from 'vue'
import { useColorScheme } from 'frappe-ui'
import { DEFAULT_THEME, THEMES, themeById } from './themes'

const KEY = 'cw.premium'
const stored = (() => { try { return JSON.parse(localStorage.getItem(KEY) || '{}') } catch { return {} } })()
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')

export const effects = reactive({
  enabled: stored.enabled ?? true, // the settings toggle
  theme: THEMES.some((t) => t.id === stored.theme) ? stored.theme : DEFAULT_THEME,
  reducedMotion: reduced.matches,
  hidden: document.visibilityState === 'hidden',
})
reduced.addEventListener('change', (e) => (effects.reducedMotion = e.matches))
document.addEventListener('visibilitychange', () => (effects.hidden = document.visibilityState === 'hidden'))
watch(() => [effects.enabled, effects.theme], ([enabled, theme]) => localStorage.setItem(KEY, JSON.stringify({ enabled, theme })))

const { colorScheme, resolvedColorScheme, setColorScheme } = useColorScheme()
export const scheme = colorScheme
export const resolvedScheme = resolvedColorScheme
export const setScheme = setColorScheme

export const currentTheme = computed(() => themeById(effects.theme))
export const stops = computed(() => currentTheme.value[resolvedScheme.value])
// Effects drawn at all (static when motion is off); motion = the shader may move.
export const active = computed(() => effects.enabled)
export const motion = computed(() => effects.enabled && !effects.reducedMotion && !effects.hidden)

// The stops as CSS variables (`--cw-g0: r g b`) so tints, scrims and swatches
// can follow the theme from plain CSS: rgb(var(--cw-g1) / 0.3).
watchEffect(() => {
  const root = document.documentElement.style
  stops.value.forEach((c, i) => root.setProperty(`--cw-g${i}`, `${c[0]} ${c[1]} ${c[2]}`))
  // The OTHER scheme's stops: what gradient text is cut from, so it stays dark on light and light on dark.
  currentTheme.value[resolvedScheme.value === 'dark' ? 'light' : 'dark'].forEach((c, i) => root.setProperty(`--cw-t${i}`, `${c[0]} ${c[1]} ${c[2]}`))
  document.documentElement.toggleAttribute('data-effects', effects.enabled)
})
