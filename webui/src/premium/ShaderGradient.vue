<script setup>
// The Arc-style gradient: a fullscreen-quad WebGL fragment shader (domain-warped
// simplex fbm, 2 octaves, blending four theme stops, plus static film grain), the same
// approach Shader Gradient takes minus three.js - a quad needs no scene graph,
// and three.js would add ~160 kB gzip for it. Draws at half resolution (the
// field is smooth; the browser upscales it) and at most 30 fps; stops drawing
// when `animate` is false (effects off, reduced motion, hidden tab, scrolled out
// of view) after painting one static frame. No WebGL, or a shader that fails to compile or link, falls back to the
// CSS radial stand-in from themes.js.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cssGradient } from './themes'

const props = defineProps({
  stops: { type: Array, required: true }, // four [r,g,b]
  animate: { type: Boolean, default: true },
  speed: { type: Number, default: 1 }, // 1 = Arc-slow
  grain: { type: Number, default: 0.05 },
  scale: { type: Number, default: 0.6 }, // bigger = smaller blobs
  resolution: { type: Number, default: 0.4 }, // render scale vs CSS pixels (the field is smooth; 0.4 is invisible after upscaling)
})

const canvas = ref(null)
const fallback = ref(false)
let gl, program, uniforms, raf = 0, last = 0, t0 = performance.now(), paused = 0, pausedAt = 0, visible = true, io
// A spot of turbulence at a random point every 9 to 17 s (independently per surface), easing in and settling back
// over 1.8 s. Scheduled from the running frame loop only, so it never fires under reduced motion, with effects off,
// or while hidden; a pause reschedules from the moment of resume.
let burst = null, nextBurstAt = 0
const TURB_DUR = 1800
const scheduleBurst = (now) => { nextBurstAt = now + 9000 + Math.random() * 8000 }
function turbulence(now) {
  if (!burst && now >= nextBurstAt) { burst = { x: 0.15 + Math.random() * 0.7, y: 0.15 + Math.random() * 0.7, start: now } }
  if (!burst) return [0, 0, 0, 0]
  const phase = (now - burst.start) / TURB_DUR
  if (phase >= 1) { burst = null; scheduleBurst(now); return [0, 0, 0, 0] }
  const amp = Math.sin(Math.PI * phase) // eases in, peaks mid-burst, settles back
  return [burst.x, burst.y, amp, phase]
}

const VERT = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}'
const FRAG = `precision highp float;
uniform vec2 u_res;uniform float u_time;uniform vec3 u_c0,u_c1,u_c2,u_c3;uniform float u_grain,u_scale;uniform vec3 u_turb;uniform float u_tphase;
vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;}
vec2 mod289(vec2 x){return x-floor(x*(1./289.))*289.;}
vec3 permute(vec3 x){return mod289(((x*34.)+1.)*x);}
float snoise(vec2 v){const vec4 C=vec4(.211324865405187,.366025403784439,-.577350269189626,.024390243902439);
vec2 i=floor(v+dot(v,C.yy));vec2 x0=v-i+dot(i,C.xx);vec2 i1=(x0.x>x0.y)?vec2(1.,0.):vec2(0.,1.);
vec4 x12=x0.xyxy+C.xxzz;x12.xy-=i1;i=mod289(i);vec3 p=permute(permute(i.y+vec3(0.,i1.y,1.))+i.x+vec3(0.,i1.x,1.));
vec3 m=max(.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.);m=m*m;m=m*m;
vec3 x=2.*fract(p*C.www)-1.;vec3 h=abs(x)-.5;vec3 ox=floor(x+.5);vec3 a0=x-ox;m*=1.79284291400159-.85373472095314*(a0*a0+h*h);
vec3 g;g.x=a0.x*x0.x+h.x*x0.y;g.yz=a0.yz*x12.xz+h.yz*x12.yw;return 130.*dot(m,g);}
float fbm(vec2 p){float v=0.,a=.5;for(int i=0;i<2;i++){v+=a*snoise(p);p=p*2.03+vec2(1.7,9.2);a*=.4;}return v;}
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
void main(){
vec2 uv=gl_FragCoord.xy/u_res;
// An occasional spot of turbulence - a gaussian swirl plus a fading ripple around a random point,
// eased by u_turb.z (0 when idle, so the extra work is a few multiplies).
vec2 asp=vec2(u_res.x/u_res.y,1.);vec2 d=(uv-u_turb.xy)*asp;float rr=length(d);
float env=u_turb.z*exp(-rr*rr*14.);float ang=env*2.6;float cs=cos(ang),sn=sin(ang);
vec2 dr=vec2(cs*d.x-sn*d.y,sn*d.x+cs*d.y);
uv+=(dr-d)/asp+(d/max(rr,1e-3))/asp*env*.03*sin(rr*45.-u_tphase*9.);
vec2 p=uv;p.x*=u_res.x/u_res.y;p*=u_scale;float t=u_time;
vec2 q=vec2(fbm(p+vec2(0.,t*.07)),fbm(p+vec2(5.2,1.3)-t*.05));
vec2 r=vec2(fbm(p+.9*q+vec2(1.7,9.2)+t*.04),fbm(p+.9*q+vec2(8.3,2.8)-t*.03));
float f=fbm(p+.8*r);
vec3 col=mix(u_c0,u_c1,smoothstep(-.6,.6,f));
col=mix(col,u_c2,smoothstep(-.2,.8,q.x));
col=mix(col,u_c3,smoothstep(-.4,.7,r.y));
col+=(hash(gl_FragCoord.xy)-.5)*u_grain;
gl_FragColor=vec4(col,1.);}`

function compile(type, src) {
  const s = gl.createShader(type)
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s))
  return s
}
function init() {
  gl = canvas.value.getContext('webgl', { antialias: false, depth: false, stencil: false, alpha: false, powerPreference: 'low-power' })
  if (!gl) { fallback.value = true; return false }
  try {
    program = gl.createProgram()
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERT))
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAG))
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program))
  } catch {
    gl = null
    fallback.value = true
    return false
  }
  gl.useProgram(program)
  const buf = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
  const loc = gl.getAttribLocation(program, 'p')
  gl.enableVertexAttribArray(loc)
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0)
  uniforms = Object.fromEntries(['u_res', 'u_time', 'u_c0', 'u_c1', 'u_c2', 'u_c3', 'u_grain', 'u_scale', 'u_turb', 'u_tphase'].map((n) => [n, gl.getUniformLocation(program, n)]))
  return true
}
function size() {
  const c = canvas.value
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5) * props.resolution
  const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr))
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; gl.viewport(0, 0, w, h) }
}
function draw(now) {
  if (!gl || gl.isContextLost()) return
  size()
  gl.uniform2f(uniforms.u_res, canvas.value.width, canvas.value.height)
  gl.uniform1f(uniforms.u_time, ((now - t0 - paused) / 1000) * 0.25 * props.speed)
  props.stops.forEach((c, i) => gl.uniform3f(uniforms[`u_c${i}`], c[0] / 255, c[1] / 255, c[2] / 255))
  gl.uniform1f(uniforms.u_grain, props.grain)
  gl.uniform1f(uniforms.u_scale, props.scale)
  const [tx, ty, tamp, tphase] = running() ? turbulence(now) : [0, 0, 0, 0]
  gl.uniform3f(uniforms.u_turb, tx, ty, tamp)
  gl.uniform1f(uniforms.u_tphase, tphase)
  gl.drawArrays(gl.TRIANGLES, 0, 3)
}
function frame(now) {
  raf = 0
  if (!running()) return
  if (now - last >= 1000 / 30) { last = now; draw(now) }
  raf = requestAnimationFrame(frame)
}
const running = () => props.animate && visible && !!gl
function sync() {
  if (!gl) return
  if (running()) {
    if (pausedAt) { paused += performance.now() - pausedAt; pausedAt = 0 }
    burst = null; scheduleBurst(performance.now())
    if (!raf) raf = requestAnimationFrame(frame)
  } else {
    if (raf) { cancelAnimationFrame(raf); raf = 0 }
    if (!pausedAt) pausedAt = performance.now()
    draw(performance.now()) // one static frame (reduced motion, effects paused)
  }
}
onMounted(() => {
  if (!init()) return
  scheduleBurst(performance.now())
  canvas.value.addEventListener('webglcontextlost', (e) => { e.preventDefault(); if (raf) cancelAnimationFrame(raf); raf = 0 })
  canvas.value.addEventListener('webglcontextrestored', () => { init(); sync() })
  io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; sync() })
  io.observe(canvas.value)
  sync()
})
onBeforeUnmount(() => { if (raf) cancelAnimationFrame(raf); io?.disconnect(); gl?.getExtension('WEBGL_lose_context')?.loseContext() })
watch(() => [props.animate, props.stops, props.grain, props.scale], sync, { deep: true })
defineExpose({ canvas })
</script>

<template>
  <div v-if="fallback" class="size-full" :style="{ background: cssGradient(stops) }" aria-hidden="true" />
  <canvas v-else ref="canvas" class="size-full" aria-hidden="true" />
</template>
