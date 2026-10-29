// A short "browser on OS" label for a session row. The daemon keeps only the
// first 80 characters of a user agent, which often cuts the browser token off
// the end ("... (KHTML, like Gecko) Chrome/130" loses "Chrome/"), so the OS is
// the part that reliably survives and a missing browser is reported as such
// rather than guessed. The raw string stays reachable through the row's title.
const BROWSERS = [
  ['Edg/', 'Edge'],
  ['OPR/', 'Opera'],
  ['Firefox/', 'Firefox'],
  ['Headless', 'Headless Chrome'],
  ['Chrome/', 'Chrome'],
  ['Safari/', 'Safari'],
]
// Order matters: an iPhone agent also says "like Mac OS X", Android also says "Linux".
const SYSTEMS = [
  ['Windows', 'Windows'],
  ['iPhone', 'iOS'],
  ['iPad', 'iPadOS'],
  ['Mac OS X', 'macOS'],
  ['Android', 'Android'],
  ['CrOS', 'ChromeOS'],
  ['Linux', 'Linux'],
]

export function agentLabel(agent) {
  const raw = (agent || '').trim()
  const browser = BROWSERS.find(([m]) => raw.includes(m))?.[1]
  const os = SYSTEMS.find(([m]) => raw.includes(m))?.[1]
  if (os) return `${browser || 'Browser'} on ${os}`
  return browser || raw || 'unknown browser'
}
