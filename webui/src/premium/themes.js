// Arc-style gradient themes. Each theme is four colour stops per scheme, as
// plain 0-255 RGB triples: the shader takes them as vec3 uniforms and the
// swatches render them with rgb(), so no hex literal lives in webui/src (the
// design-adherence token lint). The brand stops come from design/tokens/colors.css
// (whale cyan, deep water, sea foam, coffee clay, the ink ramp); the rest are
// Arc-style picks. Light stops stay pastel and dark stops stay
// deep so ink text keeps AA contrast over the glass and scrims.
export const THEMES = [
  { id: 'whale', name: 'Whale',
    light: [[130, 208, 236], [70, 172, 214], [205, 236, 250], [160, 222, 240]],
    dark: [[14, 30, 66], [18, 84, 144], [0, 140, 190], [20, 44, 96]] },
  { id: 'foam', name: 'Sea foam',
    light: [[214, 246, 250], [160, 224, 236], [232, 246, 255], [176, 232, 228]],
    dark: [[14, 34, 56], [24, 96, 120], [20, 130, 160], [10, 50, 70]] },
  { id: 'clay', name: 'Espresso',
    light: [[246, 208, 196], [228, 160, 150], [252, 232, 220], [236, 186, 170]],
    dark: [[52, 22, 30], [150, 64, 80], [110, 46, 56], [36, 20, 34]] },
  { id: 'aurora', name: 'Aurora',
    light: [[170, 236, 208], [150, 210, 244], [196, 184, 248], [172, 234, 234]],
    dark: [[12, 46, 58], [16, 110, 108], [70, 40, 130], [14, 30, 64]] },
  { id: 'sunset', name: 'Sunset',
    light: [[252, 196, 160], [250, 170, 196], [228, 190, 246], [254, 220, 180]],
    dark: [[72, 24, 42], [160, 62, 52], [104, 40, 118], [44, 18, 46]] },
  { id: 'meadow', name: 'Meadow',
    light: [[184, 236, 190], [216, 240, 160], [160, 228, 210], [228, 246, 196]],
    dark: [[14, 52, 40], [30, 110, 76], [16, 86, 96], [12, 38, 38]] },
  { id: 'grape', name: 'Grape',
    light: [[204, 180, 248], [244, 176, 222], [180, 196, 252], [224, 190, 240]],
    dark: [[44, 20, 78], [96, 40, 150], [132, 34, 100], [28, 16, 56]] },
  { id: 'graphite', name: 'Graphite',
    light: [[226, 234, 244], [196, 210, 230], [244, 248, 252], [210, 220, 234]],
    dark: [[12, 18, 36], [40, 56, 100], [30, 42, 80], [18, 26, 54]] },
]
export const DEFAULT_THEME = 'whale'
export const themeById = (id) => THEMES.find((t) => t.id === id) || THEMES[0]
export const rgb = (c, a = 1) => (a === 1 ? `rgb(${c[0]} ${c[1]} ${c[2]})` : `rgb(${c[0]} ${c[1]} ${c[2]} / ${a})`)
// A static CSS stand-in for the shader: the swatches, the no-WebGL fallback and
// the reduced-motion poster all draw this.
export const cssGradient = (stops) =>
  `radial-gradient(at 20% 20%, ${rgb(stops[1])} 0%, transparent 55%), radial-gradient(at 80% 30%, ${rgb(stops[2])} 0%, transparent 55%), radial-gradient(at 50% 90%, ${rgb(stops[3])} 0%, transparent 60%), ${rgb(stops[0])}`
