// Paints the stored colour scheme (frappe-ui's `theme` key, else the OS setting) before the app bundle
// loads, so a dark-OS user never sees a light first frame. frappe-ui's useColorScheme stays the runtime owner.
try {
  var scheme = localStorage.getItem('theme')
  var dark = scheme === 'dark' || (scheme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
} catch (e) {}
