// AFTERGLOW operator TV: the panels around the live map (livemap.js), for a TV in the ops room.
// Modes: Day sweep (patrol), Night sweep (night), Trail pathing (search), Emergency (investigate).
// Every command here comes from a click. Nothing auto-confirms a ping or accepts a leg: the drone
// never decides. "Ping" is the screen word for a pin (an ag/target record).
// TAK (tak_link.py) is the other half of the same system: the field teams' phones show every ping, the
// drone and the rescue route, and send back their positions (drawn here), chat and reports ("found P3"),
// which this screen offers to the ranger as the same buttons. A report never acts by itself.
// The TAK parts stay idle without TAK: no ag/team means no field-team panels, and the rescue route is only
// posted when the server has /api/rescue (meta.tak, set by tak/tak_dashboard.py).
import { html, render, useState, useEffect, useRef } from './vendor/htm-3.1.1/standalone.module.js';
import { connect } from './store.js';
import { LiveMap, PRIO_COLOR } from './livemap.js';

const MODES = [['patrol', 'Day sweep'], ['night', 'Night sweep'], ['search', 'Trail pathing'], ['investigate', 'Emergency']];
const MODE_NAME = Object.fromEntries(MODES);
const CLS_PLAIN = {
  findmy: 'Lost iPhone or AirTag (Find My)', findhub: 'Lost Android phone or tag (Find Hub)',
  samsung_off: 'Lost Samsung phone', ref: 'Our reference tag', wifi: 'SOS Wi-Fi answer',
};
const CLS_SHORT = { findmy: 'Find My', findhub: 'Find Hub', samsung_off: 'Samsung', ref: 'Ref tag', wifi: 'SOS' };
const CARRY = [['iphone', 'iPhone'], ['airtag', 'AirTag'], ['android', 'Android phone'], ['samsung', 'Samsung phone'], ['unknown', 'Not known']];
const CARRY_NAME = Object.fromEntries(CARRY);
const PRIO_NAME = { 1: 'urgent', 2: 'strong ping', 3: 'possible', 4: 'heard only' };
const STATUS_ORDER = { investigating: 0, new: 1, acknowledged: 2, confirmed: 3, cleared: 4 };
const LIVE_SRC = new Set(['gps', 'waypoint', 'imu']); // the real pod (imu: the indoor walk, position.py)
const POSE_STALE_MS = 10000;   // no pose this long: the Wi-Fi link to the pod is down
const ALERT_FRESH_MS = 20000;  // a LoRa alert this recent: LoRa is still getting through
const BLE_SILENT_MS = 15000;
const BIN_M = 10;              // trail progress is counted in 10 m pieces
const CHECK_MS = 12000;        // over a ping this long (bus time) with nobody seen: "no one here"
const FEED_EVERY_MS = 1000;    // the live camera and heat pictures reload this often (MJPEG is tier 2)
const FEED_OLD_S = 5;          // a picture older than this: the feed is frozen or lost
const THERMAL_FRESH_MS = 15000;
const WALK_TRAIL = 1.1, WALK_OFF = 0.4; // ground team, m/s on a trail and off it
const FORCE = new URLSearchParams(location.search).get('force');
const NO_CASE = { case: null, ranking: [], route: [], start: null, trail: null };
const FOREST = {
  day: { bg: '#0e1510', camp: '#b9772f', park: '#5f8f52', road: '#7d7a70', casing: '#0b100c' },
  night: { bg: '#080c09', camp: '#b9772f', park: '#3f5f37', road: '#5d5b54', casing: '#060906' },
};

// ---------- helpers ----------
async function post(url, body) {
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    return { ok: r.ok && j.ok, problems: j.problems || [`HTTP ${r.status}`], data: j };
  } catch (e) {
    return { ok: false, problems: [String(e.message || e)] };
  }
}
const pad = (n) => String(n).padStart(2, '0');
const fmtClock = (t) => (t ? new Date(t).toLocaleTimeString([], { hour12: false }) : '--:--:--');
const fmtHM = (t) => (t ? new Date(t).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit' }) : '--:--');
function fmtDur(ms) {
  if (ms == null || !isFinite(ms)) return '—';
  const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}
function fmtAgo(ms) {
  if (ms == null || !isFinite(ms)) return '—';
  const s = ms / 1000;
  if (s < 2) return 'now';
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}
const fmtC = (c) => (typeof c === 'number' && isFinite(c) ? `${c.toFixed(1)}°C` : null); // max_c can be null (picture only)
function heatText(th, bt) { // the latest ag/thermal in words, or null if there's none
  if (!th || bt - th.t > THERMAL_FRESH_MS) return null;
  const c = fmtC(th.max_c);
  return c ? `${c} max${th.hot ? ' · warm' : ''}` : th.hot ? 'warm spot' : 'picture only';
}
const fmtM = (v) => (v == null || !isFinite(v) ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} km` : v >= 100 ? `${Math.round(v)} m` : `${Math.round(v * 10) / 10} m`);
const pinNum = (p) => Number(String(p).replace(/\D/g, '')) || 0;
const isOpen = (p) => p.status !== 'cleared' && p.status !== 'confirmed';
const prioColor = (p) => PRIO_COLOR[p] || PRIO_COLOR[4];
function loadPref(k, d) { try { const v = localStorage.getItem(`ag.tv.${k}`); return v == null ? d : JSON.parse(v); } catch { return d; } }
function savePref(k, v) { try { localStorage.setItem(`ag.tv.${k}`, JSON.stringify(v)); } catch { /* private mode */ } }

function segDist(px, py, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy;
  const t = L2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / L2));
  return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
}
function inRing(px, py, ring) {
  let inside = false;
  for (let i = 0; i < ring.length - 1; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[i + 1];
    if ((y1 > py) !== (y2 > py) && px < x1 + ((py - y1) * (x2 - x1)) / (y2 - y1)) inside = !inside;
  }
  return inside;
}
function distToCamp(meta, x, y) { // 0 inside a campground, Infinity if there are none
  let best = Infinity;
  for (const z of meta.map.zones) {
    if (z.kind !== 'campground') continue;
    if (inRing(x, y, z.ring)) return 0;
    for (let i = 0; i < z.ring.length - 1; i++) best = Math.min(best, segDist(x, y, z.ring[i], z.ring[i + 1]));
  }
  return best;
}
const heading = (dx, dy) => (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360;
function routeBins(route) { // points every BIN_M metres along the route (evenly, across vertices)
  const out = [];
  let carry = 0;
  for (let i = 0; i < route.length - 1; i++) {
    const [ax, ay] = route[i], [bx, by] = route[i + 1], seg = Math.hypot(bx - ax, by - ay);
    let d = carry;
    for (; d < seg; d += BIN_M) out.push([ax + (bx - ax) * d / seg, ay + (by - ay) * d / seg]);
    carry = d - seg;
  }
  if (route.length) out.push(route[route.length - 1]);
  return out;
}
const routeLen = (r) => r.reduce((a, p, i) => (i ? a + Math.hypot(p[0] - r[i - 1][0], p[1] - r[i - 1][1]) : 0), 0);

function verdict(c, bt) { // what the check found so far
  if (!c || c.closeSince == null) return 'flying';
  if (c.person && c.warm) return 'person';
  if (bt - c.closeSince >= CHECK_MS) return c.person || c.warm ? 'unclear' : 'none';
  return 'checking';
}
const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const compassWord = (deg) => COMPASS[Math.round(deg / 45) % 8];
function rescuePlan(meta, cv, p) { // the ground team's walk: along the searched trail, then off it to the person
  const start = cv.case && cv.start ? cv.start : { x: meta.search.entrance_xy[0], y: meta.search.entrance_xy[1], label: 'Park entrance' };
  const route = cv.case ? cv.route : [];
  let best = null;
  for (let i = 0, run = 0; i < route.length - 1; i++) {
    const a = route[i], b = route[i + 1], seg = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const t = seg ? Math.max(0, Math.min(1, ((p.x - a[0]) * (b[0] - a[0]) + (p.y - a[1]) * (b[1] - a[1])) / (seg * seg))) : 0;
    const q = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])], d = Math.hypot(p.x - q[0], p.y - q[1]);
    if (!best || d < best.off) best = { i, q, off: d, along: run + t * seg };
    run += seg;
  }
  const toFirst = route.length ? Math.hypot(route[0][0] - start.x, route[0][1] - start.y) : 0;
  if (best && best.off < 150) {
    const pts = [[start.x, start.y], ...route.slice(0, best.i + 1), best.q, [p.x, p.y]];
    const onTrail = toFirst + best.along;
    return { start, pts, trail: cv.trail, onTrail, off: best.off, dir: compassWord(heading(p.x - best.q[0], p.y - best.q[1])),
             secs: onTrail / WALK_TRAIL + best.off / WALK_OFF };
  }
  const off = Math.hypot(p.x - start.x, p.y - start.y);
  return { start, pts: [[start.x, start.y], [p.x, p.y]], trail: null, onTrail: 0, off, dir: compassWord(heading(p.x - start.x, p.y - start.y)), secs: off / WALK_OFF };
}
function weather(bt) { // simulated, for the TV: mild and slowly changing
  const d = new Date(bt || Date.now()), h = d.getHours() + d.getMinutes() / 60;
  const temp = 11 + 5 * Math.sin(((h - 9) / 24) * 2 * Math.PI);
  const wind = 12 + 5 * Math.sin((bt || 0) / 900000) + 2 * Math.sin((bt || 0) / 170000);
  return { temp: Math.round(temp), wind: Math.max(0, Math.round(wind)), dir: COMPASS[(Math.floor((bt || 0) / 3600000) + 7) % 8] };
}

// ---------- the chime (Web Audio: no sound file) ----------
const Sound = {
  ctx: null,
  on: loadPref('chime', true),
  unlock() {
    try {
      if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') this.ctx.resume();
    } catch { /* no audio on this device */ }
  },
  ready() { return !!this.ctx && this.ctx.state === 'running'; },
  play(urgent) {
    if (!this.on || !this.ready()) return;
    const c = this.ctx, t0 = c.currentTime + 0.02, notes = urgent ? [880, 660, 880, 660] : [660, 880];
    notes.forEach((f, i) => {
      const o = c.createOscillator(), g = c.createGain(), t = t0 + i * 0.22;
      o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
      o.connect(g).connect(c.destination);
      o.start(t); o.stop(t + 0.22);
    });
  },
};

// ---------- what the page remembers between renders (not part of the bus state) ----------
function loadChecks() { try { return JSON.parse(sessionStorage.getItem('ag.tv.checks') || '{}'); } catch { return {}; } }
function saveChecks(c) { try { sessionStorage.setItem('ag.tv.checks', JSON.stringify(c)); } catch { /* private mode */ } }
function makeMemory() {
  return {
    ready: false, known: new Map(), sosSeen: new Set(), firstSeen: {}, events: [], mode: null, modeSince: null,
    alerts: {}, lastAlert: null, lastAlertWall: 0, poseRef: null, lastPoseWall: 0, bleCount: 0, lastBleWall: 0,
    cov: [], covT: null, bins: [], binsHit: null, binsRoute: null, binsPose: null, chimedAt: 0, checks: loadChecks(),
    thLast: null, thHist: [], // the last 2 minutes of ag/thermal [t, max_c, hot], for the Live tab
  };
}

function track(mem, st, view) {
  const s = st.state, bt = s.bus_t;
  const log = (text) => { mem.events.unshift({ t: bt, text }); mem.events.length = Math.min(mem.events.length, 20); };
  // link watch
  const pose = s.poses.pod || Object.values(s.poses)[0];
  if (pose !== mem.poseRef) { mem.poseRef = pose; if (pose) mem.lastPoseWall = Date.now(); }
  const ble = s.counts['ag/obs/ble'] || 0;
  if (ble !== mem.bleCount) { mem.bleCount = ble; mem.lastBleWall = Date.now(); }
  const al = s.latest['ag/alert'];
  if (al && al !== mem.lastAlert) {
    mem.lastAlert = al; mem.lastAlertWall = Date.now();
    if (al.kind !== 4) mem.alerts[`T${al.track}`] = { ...al, wall: Date.now() };
  }
  // coverage history, for the time-to-finish estimate
  const cov = s.latest['ag/coverage'];
  if (cov && cov.t !== mem.covT) {
    mem.covT = cov.t;
    mem.cov.push([bt || cov.t, cov.pct]);
    if (mem.cov.length > 120) mem.cov.shift();
  }
  // thermal history (Live tab)
  const th = s.latest['ag/thermal'];
  if (th && th !== mem.thLast) {
    mem.thLast = th;
    if (typeof th.max_c === 'number' && typeof th.t === 'number') {
      mem.thHist.push([th.t, th.max_c, !!th.hot]);
      while (mem.thHist.length > 1 && (th.t - mem.thHist[0][0] > 120000 || mem.thHist.length > 600)) mem.thHist.shift();
    }
  }
  // mode changes
  const mode = s.latest['ag/mode']?.mode || 'patrol';
  if (mode !== mem.mode) {
    if (mem.ready && mem.mode) log(`${MODE_NAME[mode] || mode} started`);
    mem.mode = mode; mem.modeSince = bt || null; mem.cov = [];
  }
  if (!mem.modeSince && bt) mem.modeSince = bt;
  // trail progress: which 10 m pieces of the route the pod has passed within the corridor
  const route = view?.route || [];
  if (route !== mem.binsRoute) {
    mem.binsRoute = route; mem.bins = routeBins(route); mem.binsHit = new Uint8Array(mem.bins.length); mem.binsPose = null;
    if (mem.mode === 'search') for (const [x, y] of s.trails.pod || []) hitBins(mem, x, y, st.corridor);
  }
  if (pose && pose !== mem.binsPose) { mem.binsPose = pose; hitBins(mem, pose.x, pose.y, st.corridor); }
  // checking a ping: is the drone over it, is it warm, has the camera seen someone
  for (const p of Object.values(s.pins)) {
    if (p.status === 'cleared' || p.status === 'new' || p.status === 'acknowledged') { delete mem.checks[p.pin]; continue; }
    const c = (mem.checks[p.pin] ||= { closeSince: null, maxC: null, warm: false, person: null, dist: null });
    if (p.status !== 'investigating' || !pose) continue; // a confirmed ping keeps what was found
    c.dist = Math.hypot(pose.x - p.x, pose.y - p.y);
    if (c.dist > st.checkM) continue;
    if (c.closeSince == null) c.closeSince = bt;
    const th = s.latest['ag/thermal'], v = s.latest['ag/vision'];
    if (th && bt - th.t < 5000) { if (typeof th.max_c === 'number') c.maxC = Math.max(c.maxC ?? -99, th.max_c); if (th.hot) c.warm = true; }
    if (v && v.kind === 'person' && bt - v.t < 5000) c.person = Math.max(c.person ?? 0, v.conf);
  }
  for (const p of Object.values(s.pins)) {
    if (p.status === 'confirmed' && pose && Math.hypot(pose.x - p.x, pose.y - p.y) <= st.checkM) (mem.checkins ||= {})[p.pin] = bt;
  }
  saveChecks(mem.checks); // what a check found survives a reload
  // pings: new ones chime, status changes go in the log
  if (!mem.ready) {
    if (st.link !== 'live') return;
    for (const p of Object.values(s.pins)) { mem.known.set(p.pin, p.status); if (p.sos) mem.sosSeen.add(p.pin); mem.firstSeen[p.pin] = bt; }
    mem.ready = true;
    return;
  }
  let chime = 0;
  for (const p of Object.values(s.pins)) {
    const was = mem.known.get(p.pin);
    if (was === undefined) {
      mem.firstSeen[p.pin] = bt;
      log(`New ping ${p.pin}: ${CLS_SHORT[p.cls] || p.cls}`);
      chime = Math.max(chime, p.prio === 1 ? 2 : 1);
    } else if (was !== p.status) {
      log(`${p.pin} ${p.status}`);
      if (p.status === 'investigating') mem.lastChecked = p.pin;
    }
    if (p.sos && !mem.sosSeen.has(p.pin)) {
      mem.sosSeen.add(p.pin);
      log(`SOS answered on ${p.pin}`);
      chime = 2;
    }
    mem.known.set(p.pin, p.status);
  }
  if (chime && Date.now() - mem.chimedAt > 1500) { mem.chimedAt = Date.now(); Sound.play(chime === 2); }
}
function hitBins(mem, x, y, r) {
  const b = mem.bins, h = mem.binsHit;
  for (let i = 0; i < b.length; i++) if (!h[i] && Math.abs(b[i][0] - x) <= r && Math.abs(b[i][1] - y) <= r && Math.hypot(b[i][0] - x, b[i][1] - y) <= r) h[i] = 1;
}

// ---------- app ----------
function App() {
  const [meta, setMeta] = useState(null);
  const [err, setErr] = useState(null);
  const [, setTick] = useState(0);
  const store = useRef(null);
  const mem = useRef(makeMemory());
  const caseRef = useRef(null);
  const [caseView, setCaseView] = useState(null);
  const [sel, setSel] = useState(null);
  const [follow, setFollow] = useState(() => loadPref('follow', false));
  const [form, setForm] = useState(null); // null, or the case form's starting values
  const [toasts, setToasts] = useState([]);
  const [busy, setBusy] = useState({}); // pin or key -> true while a click is in flight
  const [chimeOn, setChimeOn] = useState(Sound.on);
  const [sim, setSim] = useState(null); // {paused, speed} when the fake pod runs (local_stack.py)
  const [routePin, setRoutePin] = useState(null); // looking at a found person's rescue route while the drone sweeps (TV only)
  const [live, setLive] = useState(() => loadPref('live', false)); // the Live tab (TV only)

  useEffect(() => {
    fetch('/api/map').then((r) => r.json()).then((m) => {
      setMeta(m);
      document.title = `AFTERGLOW · ${m.name} · operator TV`;
      const st = connect({
        trailStep: m.trail_step_m,
        on: { case: (v) => { caseRef.current = v; setCaseView(v); }, sim: (v) => setSim(v),
              reset: () => { try { sessionStorage.removeItem('ag.tv.checks'); } catch { /* private mode */ } location.reload(); } },
        onChange: () => { track(mem.current, st, caseRef.current); setTick((t) => t + 1); },
      });
      st.corridor = m.search?.corridor_m ?? 40;
      st.checkM = Math.max(1, (m.detect_m ?? 40) / 2); // the drone is "over" a ping within this
      store.current = st;
      window.tvStore = st; // for poking at it from the browser console
      setTick((t) => t + 1);
    }).catch((e) => setErr(String(e)));
    const id = setInterval(() => { store.current?.sampleRates(); setTick((t) => t + 1); }, 1000);
    const unlock = () => Sound.unlock();
    window.addEventListener('pointerdown', unlock);
    window.addEventListener('keydown', unlock);
    return () => { clearInterval(id); window.removeEventListener('pointerdown', unlock); window.removeEventListener('keydown', unlock); };
  }, []);
  useEffect(() => savePref('follow', follow), [follow]);
  useEffect(() => savePref('live', live), [live]);
  useEffect(() => { // Esc leaves the rescue route view or the Live tab
    const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.overlay')) { setRoutePin(null); setLive(false); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const toast = (text, kind = 'info') => {
    const id = Math.random();
    setToasts((ts) => [...ts.slice(-3), { id, text, kind }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 4000);
  };

  if (err) return html`<div class="boot">Can't reach the dashboard server: ${err}<br />Start it with python -m afterglow.dashboard or python tools/local_stack.py.</div>`;
  if (!meta || !store.current) return html`<div class="boot">Loading the map…</div>`;

  const st = store.current, s = st.state, m = mem.current, bt = s.bus_t;
  const mode = s.latest['ag/mode']?.mode || 'patrol';
  const night = mode === 'night';
  document.documentElement.toggleAttribute('data-night', night);
  const pose = s.poses.pod || Object.values(s.poses)[0] || null;
  const cv = caseView || NO_CASE;
  const kase = cv.case;

  // links and health
  const paused = !!sim?.paused;
  if (paused) m.lastPoseWall = Date.now(); // a paused sim isn't a lost link
  const podStale = FORCE === 'lora' || (m.lastPoseWall > 0 && Date.now() - m.lastPoseWall > POSE_STALE_MS) || st.link !== 'live' || !s.broker;
  const loraFresh = FORCE === 'lora' || (m.lastAlertWall > 0 && Date.now() - m.lastAlertWall < ALERT_FRESH_MS);
  const locked = podStale; // commands go over Wi-Fi: none while it's down
  const problems = []; // the link only: health shows the battery alone (status bar, and the battery card on the live site)
  if (st.link !== 'live') problems.push('Dashboard server unreachable');
  else if (!s.broker) problems.push(meta.walk ? 'No signal from the pod' : 'Message broker down'); // the live demo reaches the pod's broker over Wi-Fi
  else if (podStale) problems.push('Pod Wi-Fi lost');

  // pings
  const pins = Object.values(s.pins);
  const open = pins.filter(isOpen).sort((a, b) => (STATUS_ORDER[a.status] - STATUS_ORDER[b.status]) || (a.prio - b.prio) || (pinNum(b.pin) - pinNum(a.pin)));
  // the alert stays on the highest priority, then the one waiting longest, so it doesn't jump about
  const fresh = pins.filter((p) => p.status === 'new').sort((a, b) => (a.prio - b.prio) || ((m.firstSeen[a.pin] ?? 0) - (m.firstSeen[b.pin] ?? 0)) || (pinNum(a.pin) - pinNum(b.pin)));
  const sosPin = pins.filter((p) => p.sos && p.status !== 'cleared').sort((a, b) => pinNum(b.pin) - pinNum(a.pin))[0] || null;
  const investigating = (m.lastChecked && s.pins[m.lastChecked]?.status === 'investigating' ? s.pins[m.lastChecked] : null)
    || pins.filter((p) => p.status === 'investigating').sort((a, b) => b.t - a.t)[0] || null;
  const selPin = sel && s.pins[sel] ? s.pins[sel] : null;
  const alertList = fresh.filter((p) => !p.sos); // every new ping waiting on the ranger; the banner can step through them
  const alertPin = (selPin && alertList.includes(selPin) ? selPin : alertList[0]) || null; // the one picked, else the most urgent
  const linked = kase?.ping && s.pins[kase.ping] ? s.pins[kase.ping] : null;
  const confirmed = pins.filter((p) => p.status === 'confirmed' && !m.donePins?.[p.pin]).sort((a, b) => b.t - a.t)[0] || null; // a closed case's person is done
  const focus = selPin || alertPin || (mode !== 'investigate' ? investigating : null);
  // the rescue route view: a look at a found person's route while the drone keeps sweeping. It changes
  // nothing on the bus (the mode stays a sweep) and Back (or Esc) returns to the sweep.
  const routeP = routePin && mode !== 'investigate' && s.pins[routePin]?.status === 'confirmed' ? s.pins[routePin] : null;
  const target = routeP || (mode === 'investigate' ? (selPin || (linked && linked.status !== 'cleared' ? linked : null) || investigating || sosPin || confirmed) : null);
  const rescue = target && (mode === 'investigate' || routeP) ? rescuePlan(meta, cv, target) : null;
  const viewMode = routeP ? 'investigate' : mode; // what the map frames and keys
  const every = (meta.search?.checkin_s ?? 300) * 1000;
  const checkin = confirmed ? checkinState(confirmed, m, kase, bt, every) : null;
  const stage = mode === 'investigate' || confirmed ? 4 : investigating ? 3 : fresh.length ? 2 : 1;
  // a field team's report from TAK ("check P3", "found P3", "nobody P3"), newest first, until the ranger acts or
  // dismisses it. "ack P3" (the team has seen it) only goes in the field log. A "check" is moot once the drone is on it.
  const report = (s.team_log || []).find((r) => r.kind === 'report' && r.verdict !== 'ack' && !m.handledReports?.[`${r.uid}|${r.t}`]
    && s.pins[r.pin] && isOpen(s.pins[r.pin]) && !(r.verdict === 'check' && s.pins[r.pin].status === 'investigating')) || null;
  const dismissReport = (r) => { (m.handledReports ||= {})[`${r.uid}|${r.t}`] = true; setTick((t) => t + 1); };

  // clicks
  const pick = (pin) => setSel(pin); // the map flies to it (MapPane)
  const sendCtrl = async (body, label, key) => {
    setBusy((b) => ({ ...b, [key]: true }));
    const r = await post('/api/ctrl', body);
    setBusy(({ [key]: _, ...rest }) => rest);
    if (!r.ok) toast(`${label} failed: ${r.problems.join('; ')}`, 'bad'); else toast(`Sent: ${label}`);
    return r.ok;
  };
  const linkPing = async (pin) => {
    const r = await post('/api/case', { ping: pin || '' });
    if (r.ok) { caseRef.current = r.data; setCaseView(r.data); } else toast(r.problems.join('; '), 'bad');
  };
  const pinCmd = async (pin, cmd, label) => {
    const ok = await sendCtrl({ cmd, pin }, `${label} ${pin}`, pin);
    if (ok && cmd === 'investigate') { mem.current.lastChecked = pin; setSel(pin); }
    if (ok && cmd === 'investigate' && caseRef.current?.case && !caseRef.current.case.ping) linkPing(pin); // the case's first checked ping
    return ok;
  };
  const confirmRescue = async (pin) => { // 4a: the ranger confirms; then the emergency view and the rescue route
    if (await pinCmd(pin, 'confirm', 'Confirm person')) {
      if (caseRef.current?.case && caseRef.current.case.ping !== pin) linkPing(pin);
      setSel(pin);
      await setMode('investigate');
      sendRescue(pin);
    }
  };
  const sendRescue = (pin) => { // the ground team's route, to the TAK phones as a white line (ag/rescue)
    const p = store.current.state.pins[pin];
    if (!p || !meta.tak) return; // no TAK server behind this dashboard: nothing to send the route to
    const plan = rescuePlan(meta, caseRef.current || NO_CASE, p);
    post('/api/rescue', { pin, pts: plan.pts, secs: Math.round(plan.secs), label: plan.start?.label || '' })
      .then((r) => { if (!r.ok) toast(`Rescue route to TAK failed: ${r.problems.join('; ')}`, 'bad'); });
  };
  const notPerson = async (pin) => { // 4b: clear it; the drone goes back to its path
    if (await pinCmd(pin, 'clear', 'Not a person:')) {
      if (caseRef.current?.case?.ping === pin) linkPing('');
      setSel(null);
    }
  };
  const setMode = async (md) => {
    const r = await post('/api/mode', { mode: md });
    if (!r.ok) toast(`Mode change failed: ${r.problems.join('; ')}`, 'bad');
    return r.ok;
  };
  const saveCase = async (body, startTrail) => {
    const r = await post('/api/case', startTrail ? { ...body, prior_mode: mode === 'night' ? 'night' : 'patrol' } : body);
    if (!r.ok) return r.problems;
    caseRef.current = r.data; setCaseView(r.data);
    if (startTrail && mode !== 'search') await setMode('search');
    setForm(null);
    toast(startTrail ? 'Case opened: trail pathing' : 'Case saved');
    return null;
  };
  const closeCase = async () => {
    if (meta.tak) for (const p of Object.values(store.current.state.pins)) if (p.status === 'confirmed') post('/api/rescue', { pin: p.pin, pts: [] }); // routes off the phones
    const r = await post('/api/case/close', {});
    if (r.ok) for (const p of Object.values(store.current.state.pins)) if (p.status === 'confirmed') (mem.current.donePins ||= {})[p.pin] = true;
    if (r.ok) { caseRef.current = r.data; setCaseView(r.data); toast('Case closed'); } else toast(`Close failed: ${r.problems.join('; ')}`, 'bad');
  };
  const pickTrail = async (name) => {
    const r = await post('/api/case', { trail: name });
    if (r.ok) { caseRef.current = r.data; setCaseView(r.data); toast(`Trail: ${name}`); } else toast(r.problems.join('; '), 'bad');
  };
  const setSimState = async (body) => {
    const r = await post('/api/sim', body);
    // the whole view: paused, speed, flight, base, plan, and started (tak_stack.py --start-paused)
    if (r.ok) { const { ok: _ok, problems: _p, ...view } = r.data; setSim(view); } else toast(`Simulation: ${r.problems.join('; ')}`, 'bad');
  };
  const teamOnWay = async (pin) => { // after a person is confirmed: the team is sent, the drone goes back to its sweep
    if (caseRef.current?.case) {
      const r = await post('/api/case', { team: true, ping: pin });
      if (r.ok) { caseRef.current = r.data; setCaseView(r.data); }
    }
    mem.current.teamPins = { ...(mem.current.teamPins || {}), [pin]: mem.current.teamPins?.[pin] || s.bus_t };
    sendRescue(pin); // in case it wasn't sent at Confirm (an older page); TAK only announces it once
    setSel(null);
    await setMode(caseRef.current?.case?.prior_mode || 'patrol');
    toast(`Team on the way to ${pin}. Check in on them every ${fmtDur(every)}.`);
  };
  const backToSweep = async () => { // leave the rescue route (or Emergency, once the team is sent)
    setRoutePin(null); setSel(null);
    if (mode === 'investigate') await setMode(caseRef.current?.case?.prior_mode || 'patrol');
  };
  const toggleChime = () => { Sound.unlock(); Sound.on = !Sound.on; savePref('chime', Sound.on); setChimeOn(Sound.on); if (Sound.on) Sound.play(false); };

  const podSite = !!(meta.sites || []).find((x) => x.profile === meta.profile)?.pod;
  const ctx = { alertList, viewMode, night, sim, setSimState, podStale, podSite, meta, st, s, m, bt, mode, pose, cv, kase, locked, busy, pick, pinCmd, sendCtrl, setMode, setForm, closeCase, pickTrail, setSel, sel, open, focus, target, linked, linkPing, confirmRescue, notPerson, rescue, confirmed, teamOnWay, routeP, setRoutePin, backToSweep, checkin, every };
  // the rail shows the ping waiting on the ranger (with its tabs); the strip over the map only when it doesn't
  const railShowsAlert = viewMode !== 'investigate' && !!focus && alertList.includes(focus);

  return html`
    <div class="tv">
      <${Header} ...${ctx} stage=${stage} live=${live} setLive=${setLive} />
      <div class="body">
        <main class="stage">
          <div class="banners">
            ${sim && sim.paused && sim.started === false && html`<${ReadyBanner} mode=${mode} teams=${Object.keys(s.teams || {}).length} start=${() => setSimState({ paused: false })} />`}
            ${sim?.flight && sim.flight !== 'out' && !(sim.paused && sim.started === false) && html`<${BaseBanner} sim=${sim} pose=${pose} setSimState=${setSimState} />`}
            ${podStale && html`<${LoraBanner} m=${m} loraFresh=${loraFresh} />`}
            ${sosPin && html`<${SosBanner} p=${sosPin} mode=${mode} setMode=${setMode} locked=${locked} pick=${pick} />`}
            ${report && html`<${FieldBanner} r=${report} p=${s.pins[report.pin]} locked=${locked} pinCmd=${pinCmd} confirmRescue=${confirmRescue} notPerson=${notPerson} pick=${pick} dismiss=${() => dismissReport(report)} />`}
            ${alertPin && !railShowsAlert && html`<${PingBanner} p=${alertPin} list=${alertList} m=${m} bt=${bt} kase=${kase} pick=${pick} />`}
            ${checkin?.due && mode !== 'investigate' && !routeP && html`<${CheckinBanner} p=${confirmed} c=${checkin} pose=${pose} kase=${kase} show=${() => { setLive(false); setRoutePin(confirmed.pin); }} />`}
          </div>
          ${live && html`<${LiveView} ...${ctx} />`}
          ${meta.walk ? !live && html`<${IndoorStage} ...${ctx} />`
            : html`<div class="stagemap" style=${live ? 'display:none' : ''}>
                <${MapPane} meta=${meta} s=${s} sel=${sel} follow=${follow} setFollow=${setFollow} night=${night} cv=${cv} podStale=${podStale} m=${m} setSel=${setSel} mode=${viewMode} target=${target} rescue=${rescue} checking=${!!investigating && mode !== 'investigate'} bt=${bt} live=${live} sim=${sim} />
                <${Hud} ...${ctx} />
              </div>`}
        </main>
        <aside class="rail" aria-label="Decisions and details">${rail(ctx, podStale)}</aside>
      </div>
      <${StatusBar} ...${ctx} problems=${problems} chimeOn=${chimeOn} toggleChime=${toggleChime} />
      ${form && html`<${CaseForm} meta=${meta} init=${form} editing=${!!kase} save=${saveCase} cancel=${() => setForm(null)} />`}
      ${chimeOn && !Sound.ready() && html`<div class="soundhint" role="status">Click anywhere to turn on the ping chime</div>`}
      <div class="toasts">${toasts.map((t) => html`<div key=${t.id} class=${`toast ${t.kind}`}>${t.text}</div>`)}</div>
    </div>`;
}

// ---------- top bar and banners ----------
// The weather in the top bar is simulated (weather()), so it shows only with the fake pod.
function Header({ meta, mode, locked, setMode, live, setLive, setRoutePin, stage, cv, bt, sim }) {
  return html`
    <header class="top">
      <div class="brand"><svg viewBox="0 0 24 24" fill="none" stroke="#f0a63a" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3.5"></circle><circle cx="12" cy="12" r="9" stroke-opacity=".45"></circle></svg><span>AFTERGLOW</span></div>
      <div class="vr"></div>
      <${SiteTabs} meta=${meta} />
      <${Phase} stage=${stage} mode=${mode} cv=${cv} />
      <div class="modes" role="group" aria-label="Mode">
        ${MODES.map(([k, label]) => html`<button class=${`${mode === k ? 'on' : ''} ${k === 'investigate' ? 'emerg' : ''}`} aria-pressed=${mode === k}
          disabled=${locked} onClick=${() => { setLive(false); setRoutePin(null); if (mode !== k) setMode(k); }}>${label}</button>`)}
      </div>
      <button class=${`livebtn ${live ? 'on' : ''}`} aria-pressed=${live} title="Live: camera, heat and every reading from the pod (Esc to leave)"
        onClick=${() => setLive(!live)}><i></i>Live</button>
      <div class=${`clock ${sim?.paused ? 'paused' : ''}`} title="Bus time: the latest pose's time">${fmtClock(bt)}</div>
    </header>`;
}

// The status bar: the source (simulated, replay, live), the sim controls, the links, the weather
// (simulated, so only with the fake pod), the battery and the chime.
function StatusBar({ s, m, bt, pose, sim, setSimState, problems, chimeOn, toggleChime, podSite }) {
  const src = !pose ? null : pose.src === 'sim' ? ['b-sim', 'SIMULATED'] : pose.src === 'replay' ? ['b-replay', 'REPLAY'] : LIVE_SRC.has(pose.src) ? ['b-live', 'LIVE · REAL POD'] : ['b-none', pose.src];
  const alertAgo = m.lastAlertWall > 0 ? Date.now() - m.lastAlertWall : null;
  const sys = s.latest['ag/sys'];
  const batt = typeof sys?.batt_pct === 'number' && isFinite(sys.batt_pct) ? sys.batt_pct : null;
  const bst = batt == null ? 'off' : batt < 20 ? 'crit' : batt < 40 ? 'warn' : '';
  return html`
    <footer class="status">
      ${src && html`<span class=${`badge ${src[0]}`}>${src[1]}</span>`}
      ${sim && html`<span class="simctl" role="group" aria-label="Simulation">
        <button class=${sim.paused ? 'paused' : ''} onClick=${() => setSimState({ paused: !sim.paused })} aria-label=${sim.paused ? 'Resume the simulation' : 'Pause the simulation'}>
          ${sim.paused ? html`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5v14l12-7z" fill="currentColor"></path></svg> Paused`
                       : html`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"></path></svg>`}
        </button>
        ${[1, 2, 4].map((v) => html`<button class=${sim.speed === v ? 'on' : ''} aria-pressed=${sim.speed === v} onClick=${() => setSimState({ speed: v })}>${v}×</button>`)}
        ${sim.flight === 'out' && html`<button title="Fly the drone straight back to base and land" onClick=${() => setSimState({ flight: 'home' })}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11 12 4l9 7"></path><path d="M5 10v10h14V10"></path></svg>Return to base</button>`}
        <${ResetButton} setSimState=${setSimState} />
      </span>`}
      <span class="sep"></span>
      <span class=${`st ${problems.length ? 'crit' : ''}`} title=${problems.join(' · ')}><i></i>${problems.length ? problems[0] : html`Pod link <b>live</b>`}</span>
      <span class=${`st ${alertAgo == null ? 'off' : alertAgo < ALERT_FRESH_MS ? '' : 'warn'}`} title="The last LoRa alert heard by the laptop"><i></i>LoRa <b>${alertAgo == null ? 'nothing heard' : fmtAgo(alertAgo)}</b></span>
      <span class="push"></span>
      ${sim && html`<${Weather} bt=${bt} />`}
      ${(podSite || sys) && html`<span class="sep"></span><span class=${`st ${bst}`} title="Pod battery (health.py)"><i></i>Battery <b>${batt != null ? `${batt}%` : 'not measured'}</b></span>`}
      <button class=${`iconbtn ${chimeOn ? '' : 'off'}`} aria-label=${chimeOn ? 'Chime on: click to mute' : 'Chime muted: click to turn on'} title=${chimeOn ? 'Chime on' : 'Chime muted'} onClick=${toggleChime}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5z"></path>${chimeOn ? html`<path d="M15.5 8.5a5 5 0 0 1 0 7"></path><path d="M18.5 5.5a9 9 0 0 1 0 13"></path>` : html`<path d="m16 9 5 6"></path><path d="m21 9-5 6"></path>`}</svg>
      </button>
    </footer>`;
}

// Location tabs ([http] sites): each site is its own dashboard on its own port (Acadia, the
// simulation; 60 State St, the live demo). The TV links between them on the same host, and checks
// every few seconds whether the other one is running.
const siteUrl = (x, path = location.pathname) => `${location.protocol}//${location.hostname}:${x.port}${path}`;
const SITE_TAG = { simulation: 'Sim', 'live demo': 'Live' };
function SiteTabs({ meta }) {
  const sites = meta.sites || [];
  const [up, setUp] = useState({}); // port -> reachable
  useEffect(() => {
    const others = sites.filter((x) => x.profile !== meta.profile);
    if (!others.length) return undefined;
    let alive = true;
    const probe = () => others.forEach((x) => {
      const ctl = new AbortController();
      const tm = setTimeout(() => ctl.abort(), 2500);
      // any HTTP answer means it's running (no-cors: we can't read it, and don't need to)
      fetch(siteUrl(x, '/api/sim'), { mode: 'no-cors', cache: 'no-store', signal: ctl.signal })
        .then(() => true, () => false)
        .then((ok) => { clearTimeout(tm); if (alive) setUp((u) => (u[x.port] === ok ? u : { ...u, [x.port]: ok })); });
    });
    probe();
    const id = setInterval(probe, 5000);
    return () => { alive = false; clearInterval(id); };
  }, [meta.profile]);
  if (sites.length < 2) return html`<div class="area">${meta.name}</div>`;
  return html`<nav class="sites" aria-label="Location">${sites.map((x) => {
    const tag = html`<small class=${x.pod ? 't-live' : 't-sim'}>${SITE_TAG[x.tag] || x.tag}</small>`;
    if (x.profile === meta.profile) return html`<span key=${x.port} class="site on" aria-current="page" title=${`${meta.name} · ${x.tag}`}>${x.name}${tag}</span>`;
    const down = up[x.port] === false;
    return html`<a key=${x.port} class=${`site ${down ? 'down' : ''}`} href=${siteUrl(x)}
      title=${down ? `${x.name} isn't running (port ${x.port})` : `Switch to ${x.name} (${x.tag})`}>${x.name}${down ? html`<small class="t-down">not running</small>` : tag}</a>`;
  })}</nav>`;
}

function ResetButton({ setSimState }) { // two clicks, so nobody resets the demo by accident
  const [sure, setSure] = useState(false);
  useEffect(() => { if (!sure) return; const id = setTimeout(() => setSure(false), 4000); return () => clearTimeout(id); }, [sure]);
  return html`<button class=${sure ? 'paused' : ''} title="Back to the start: no pings, no case, day sweep"
    aria-label=${sure ? 'Click again to reset' : 'Reset the simulation'}
    onClick=${() => (sure ? setSimState({ reset: true }) : setSure(true))}>${sure ? 'Sure?' : html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"></path><path d="M3 4v5h5"></path></svg>`}</button>`;
}

function Phase({ stage, mode, cv }) {
  const first = cv.case ? `Trail: ${cv.trail || '—'}` : mode === 'search' ? 'Trail pathing' : mode === 'night' ? 'Night sweep' : 'Day sweep';
  const steps = [['Search', first], ['Signal', 'a ping'], ['Check', 'heat and camera'], ['Rescue', cv.case?.team_t ? 'team on the way' : 'route the team']];
  return html`<ol class="phase" aria-label="Where the search is">${steps.map(([t, sub], i) => html`
    <li class=${i + 1 === stage ? 'now' : i + 1 < stage ? 'done' : ''} aria-current=${i + 1 === stage ? 'step' : null} title=${`${i + 1} ${t}: ${sub}`}>
      <b>${i + 1}</b><span>${t}</span></li>`)}</ol>`;
}

function Weather({ bt }) {
  const w = weather(bt);
  return html`<span class=${`wx ${w.wind > 30 ? 'windy' : ''}`} title="Simulated weather">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M3 8h11a3 3 0 1 0-3-3"></path><path d="M3 12h16a3 3 0 1 1-3 3"></path><path d="M3 16h8"></path></svg>
    <span>${w.temp}°C · wind ${w.wind} km/h ${w.dir}</span></span>`;
}

// The fake pod waits at base until the ranger sends it out (local_stack.py; the pilot flies the real pod).
function BaseBanner({ sim, pose, setSimState }) {
  const base = sim.base;
  const left = sim.flight === 'returning' && pose && base ? Math.hypot(pose.x - base.x, pose.y - base.y) : null;
  return html`<div class="banner ban-base" role="status">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11 12 4l9 7"></path><path d="M5 10v10h14V10"></path></svg>
    <div>
      <div class="big">${sim.flight === 'returning' ? 'Returning to base' : 'Drone at base'}</div>
      <div class="sub">${sim.flight === 'returning' ? `${base?.name || 'Base'} · ${fmtM(left)} to go` : `${base?.name || 'Base'} · on the ground, scanners off`}</div>
    </div>
    <div class="spacer"></div>
    ${sim.flight === 'docked' && html`<button class="go" onClick=${() => setSimState({ flight: 'out' })}>Send out · lawn-mower sweep</button>`}
  </div>`;
}

function LoraBanner({ m, loraFresh }) {
  const since = m.lastPoseWall ? Date.now() - m.lastPoseWall : null;
  return html`<div class="banner ban-lora" role="status">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#e0a43a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 21h20L12 3z"></path><path d="M12 10v5"></path><path d="M12 18h.01"></path></svg>
    <div>
      <div class="big">${loraFresh ? 'LoRa only · ' : ''}Wi-Fi to the pod lost${since != null ? html` <span class="mono" style="color:var(--amber)">${fmtDur(since)}</span> ago` : ''}</div>
      <div class="sub">${loraFresh ? 'Pings and SOS still arrive by LoRa. No video, no coverage, no commands until the link is back.' : 'No LoRa alerts either. Pings on screen are the last known.'}</div>
    </div>
    <div class="spacer"></div>
    ${m.lastAlertWall > 0 && html`<div style="text-align:right"><div class="small dim">Last LoRa packet</div><div class="mono" style="font-size:1.6rem;font-weight:600">${fmtAgo(Date.now() - m.lastAlertWall)}</div></div>`}
  </div>`;
}

function SosBanner({ p, mode, setMode, locked, pick }) {
  const sos = p.sos || {};
  const bits = [sos.hurt ? 'hurt' : 'not hurt', sos.walk ? 'can walk' : "can't walk", sos.count ? `${sos.count} ${sos.count === 1 ? 'person' : 'people'}` : null].filter(Boolean);
  return html`<div class="banner ban-sos" role="alert">
    <span class="chipbig">${p.pin}</span>
    <div><div class="big">SOS answered: ${bits.join(', ')}</div><div class="sub">Priority ${p.prio} · from the pod's SOS Wi-Fi page</div></div>
    <div class="spacer"></div>
    ${mode !== 'investigate'
      ? html`<button disabled=${locked} onClick=${() => { pick(p.pin); setMode('investigate'); }}>Open emergency view</button>`
      : html`<button onClick=${() => pick(p.pin)}>Show on map</button>`}
  </div>`;
}

function ReadyBanner({ mode, teams, start }) { // local_stack.py --start-paused: time zero, drone on the pad
  return html`<div class="banner ban-ready" role="status">
    <div><div class="big">Ready to launch · ${(MODES.find(([k]) => k === mode) || MODES[0])[1]}</div>
      <div class="sub">Drone on the pad, no pings yet. ${teams ? `${teams} field team${teams > 1 ? 's' : ''} on TAK.` : 'TAK phones can join now; they get the same map.'}</div></div>
    <div class="spacer"></div>
    <button class="go" onClick=${start}>▶ Start the search</button>
  </div>`;
}

const REPORT_WORDS = { ack: 'has seen', check: 'asks the drone to check', found: 'reports a person at', nobody: 'reports no one at' };

function FieldBanner({ r, p, locked, pinCmd, confirmRescue, notPerson, pick, dismiss }) { // from a TAK phone
  const found = r.verdict === 'found', check = r.verdict === 'check';
  const checking = p.status === 'investigating';
  return html`<div class="banner ban-field" role="alert">
    <span class="chipbig">${p.pin}</span>
    <div><div class="big">${r.callsign} (field team) ${REPORT_WORDS[r.verdict] || 'reports on'} ${p.pin}</div>
      <div class="sub">From TAK: “${r.text}” · the ranger decides</div></div>
    <div class="spacer"></div>
    ${(found || check) && !checking && html`<button disabled=${locked} onClick=${async () => { if (await pinCmd(p.pin, 'investigate', 'Investigate') && check) dismiss(); }}>Check ${p.pin} with the drone</button>`}
    ${found && checking && html`<button disabled=${locked} onClick=${() => { dismiss(); confirmRescue(p.pin); }}>Confirm person · start rescue</button>`}
    ${r.verdict === 'nobody' && html`<button disabled=${locked} onClick=${() => { dismiss(); notPerson(p.pin); }}>Not a person · continue search</button>`}
    <button onClick=${() => pick(p.pin)}>Show</button>
    <button onClick=${dismiss}>Dismiss</button>
  </div>`;
}

function FieldCard({ s }) { // the TAK phones: how many are on the map and what they last said
  const teams = Object.values(s.teams || {});
  const log = (s.team_log || []).slice(0, 4);
  if (!teams.length && !log.length) return null;
  return html`<section class="p">
    <h2 class="h">Field teams (TAK) · ${teams.length} on the map</h2>
    <div class="events">${log.length ? log.map((e) => html`<div key=${`${e.uid}|${e.t}`}><time class="mono">${fmtHM(e.t)}</time><span><b>${e.callsign}</b>: ${e.kind === 'report' ? `${REPORT_WORDS[e.verdict] || 'reports on'} ${e.pin}` : e.text}</span></div>`)
      : html`<div class="dim">No messages yet. Phones can reply “ack P3”, “check P3”, “found P3” or “nobody P3”.</div>`}</div>
  </section>`;
}

function PingBanner({ p, list, m, bt, kase, pick }) {
  // Several new pings: tabs (and ‹ ›, or the [ and ] keys) switch which one the banner, the map and the
  // ping card show. Picking only changes the view: it never acks or confirms anything.
  const match = kase?.expect_cls && kase.expect_cls === p.cls;
  const i = Math.max(0, list.findIndex((q) => q.pin === p.pin)), n = list.length;
  const step = (d) => pick(list[(i + d + n) % n].pin);
  useEffect(() => {
    if (n < 2) return undefined;
    const onKey = (e) => {
      if (document.querySelector('.overlay') || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      if (e.key === ']') step(1); else if (e.key === '[') step(-1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  return html`<div class="banner ban-ping" role="alert" style=${`--c:${prioColor(p.prio)}`}>
    <span class="chipbig">${p.pin}</span>
    <div><div class="big">${n > 1 ? `${n} new pings need a ranger decision` : 'New ping needs a ranger decision'}</div><div class="sub">${CLS_PLAIN[p.cls] || p.cls}${match ? ' · matches what the person is carrying' : ''}</div></div>
    <div class="spacer"></div>
    ${n > 1 && html`<div class="pingtabs" role="tablist" aria-label="New pings ([ and ] to switch)">
      <button class="pt-step" aria-label="Previous ping" title="Previous ping ([)" onClick=${() => step(-1)}>‹</button>
      ${list.map((q) => html`<button key=${q.pin} role="tab" aria-selected=${q.pin === p.pin} class=${`pt ${q.pin === p.pin ? 'on' : ''}`}
        title=${`${q.pin} · ${CLS_PLAIN[q.cls] || q.cls} · waiting ${fmtDur(bt - (m.firstSeen[q.pin] ?? bt))}`} onClick=${() => pick(q.pin)}>
        <i style=${`background:${prioColor(q.prio)}`}></i>${q.pin}</button>`)}
      <button class="pt-step" aria-label="Next ping" title="Next ping (])" onClick=${() => step(1)}>›</button>
    </div>`}
    <span class="sub">Waiting</span><span class="timer">${fmtDur(bt - (m.firstSeen[p.pin] ?? bt))}</span>
    <button onClick=${() => pick(p.pin)}>Show</button>
  </div>`;
}

// A found person, once the team is on the way: when the drone last checked in on them and when
// the next check-in is due. Times are bus time; the team's send time falls back to the wall clock
// (kase.team_t) after a reload. The TV only asks: the pilot flies the check-in.
function checkinState(p, m, kase, bt, every) {
  const teamBus = m.teamPins?.[p.pin];
  const teamWall = kase?.ping === p.pin ? kase?.team_t : null;
  if (teamBus == null && teamWall == null) return { sent: false };
  const last = m.checkins?.[p.pin] ?? null;
  const near = last != null && bt - last < 5000;
  const since = last != null ? bt - last : teamBus != null ? bt - teamBus : Date.now() - teamWall;
  const left = every - since;
  return { sent: true, last, near, left, due: !near && left <= 0 };
}

function CheckinBanner({ p, c, pose, kase, show }) {
  const hdg = pose ? heading(p.x - pose.x, p.y - pose.y) : null, dist = pose ? Math.hypot(p.x - pose.x, p.y - pose.y) : null;
  return html`<div class="banner ban-checkin" role="alert">
    <span class="chipbig">${p.pin}</span>
    <div><div class="big">Check-in due${kase?.ping === p.pin ? ` on ${kase.name}` : ''}: fly over the person</div>
      <div class="sub">${hdg != null ? `Heading ${Math.round(hdg)}°, ${fmtM(dist)} from the drone. ` : ''}Look at the heat and camera, then back to the sweep.</div></div>
    <div class="spacer"></div>
    <span class="sub">Late</span><span class="timer">${fmtDur(-c.left)}</span>
    <button onClick=${show}>Show route</button>
  </div>`;
}

// ---------- columns ----------
// The box on the map: the sweep in the sweeps, the stretch in trail pathing, the pilot's heading in an emergency.
function Hud(c) {
  if (c.routeP || (c.mode === 'investigate' && c.target)) return html`<div class="hud"><${PilotCard} ...${c} /></div>`;
  if (c.mode === 'search') return c.kase && c.cv.trail && c.m.bins.length ? html`<div class="hud"><${TrailProgress} ...${c} /></div>` : null;
  return html`<div class="hud"><${SweepCard} ...${c} /></div>`;
}

// The rail: the decision first (the ping waiting on the ranger, or the emergency), then the rest.
function rail(c, podStale) {
  const loraList = podStale && Object.keys(c.m.alerts).length ? html`<${LoraPings} m=${c.m} />` : null;
  const battery = c.podSite ? html`<${BatteryCard} ...${c} />` : null;
  if (c.routeP) return html`${loraList}<${RescueCard} ...${c} p=${c.routeP} /><${RescueWatch} ...${c} p=${c.routeP} /><${PingsTable} ...${c} /><${EventsCard} m=${c.m} />`;
  if (c.mode === 'investigate') {
    if (!c.target) return html`${loraList}<section class="p"><h2 class="h">Emergency</h2><div class="lbl">Nothing to investigate. Pick a ping, then press Investigate.</div></section>
      <${PingsTable} ...${c} /><${SosCard} ...${c} /><${FieldCard} s=${c.s} />${battery}<${EventsCard} m=${c.m} />`;
    return html`${loraList}<${RescueCard} ...${c} p=${c.target} /><${CameraCard} ...${c} /><${SosCard} ...${c} /><${FieldCard} s=${c.s} /><${PingsTable} ...${c} />${battery}`;
  }
  const caseBits = c.mode === 'search' ? (c.kase ? html`<${CaseCard} ...${c} /><${TrailsCard} ...${c} />` : html`<${CaseEntry} ...${c} big=${true} />`)
    : c.confirmed ? html`<${RescueWatch} ...${c} p=${c.confirmed} />` : html`<${CaseEntry} ...${c} />`;
  return html`${loraList}
    ${c.focus && html`<${PingCard} ...${c} p=${c.focus} />`}
    <${LegCard} ...${c} />
    ${c.mode === 'search' && caseBits}
    <${PingsTable} ...${c} />
    <${FieldCard} s=${c.s} />
    ${c.mode !== 'search' && caseBits}
    ${battery}
    <${EventsCard} m=${c.m} />`;
}

function SweepCard({ s, m, bt, mode, meta }) {
  const cov = s.latest['ag/coverage'];
  const pct = cov ? cov.pct : null;
  let eta = null;
  const h = m.cov;
  if (h.length > 3 && pct != null && pct < 1) {
    const [t0, p0] = h[0], [t1, p1] = h[h.length - 1];
    const rate = (p1 - p0) / Math.max(1, t1 - t0); // share per ms of bus time
    if (rate > 0) eta = (1 - p1) / rate;
  }
  const th = s.latest['ag/thermal'];
  return html`<section class="p grow">
    <h2 class="h">${mode === 'night' ? 'Night sweep' : 'Lawn-mower sweep'}</h2>
    <div style="display:flex;align-items:baseline;gap:0.7rem"><span class="hero">${pct != null ? Math.round(pct * 100) + '%' : '—'}</span><span class="lbl">of the search box covered</span></div>
    <div class="bar"><i style=${`width:${Math.round((pct || 0) * 100)}%`}></i></div>
    <div class="row"><span class="lbl">In this mode</span><span class="num">${fmtDur(m.modeSince ? bt - m.modeSince : null)}</span></div>
    <div class="row"><span class="lbl">Est. to finish</span><span class="num">${eta != null ? '~' + fmtDur(eta) : '—'}</span></div>
    ${mode === 'night' && html`<div class="row"><span class="lbl">Thermal</span><span>${heatText(th, bt) ? `On · ${heatText(th, bt)}` : 'No thermal data'}</span></div>`}
    <div class="rulenote"><b>Pings on:</b> ${mode === 'night'
      ? 'any lost-phone beacon outside the campground. Campers’ phones are ignored.'
      : `a lost-phone beacon more than ${meta.offtrail_m} m off trail, heard on 2+ passes, outside campgrounds.`}</div>
  </section>`;
}


function CaseEntry({ kase, setForm, setMode, locked, big }) {
  if (kase) {
    return html`<section class="p accent">
      <h2 class="h">Case open</h2>
      <div style="font-size:1.3rem;font-weight:700">${kase.name}${kase.age ? `, ${kase.age}` : ''}</div>
      <button class="btn primary" disabled=${locked} onClick=${() => setMode('search')}>Go to trail pathing</button>
    </section>`;
  }
  return html`<section class=${`p ${big ? 'grow' : ''}`} style=${big ? 'justify-content:center;text-align:center' : ''}>
    <h2 class="h">Missing person reported?</h2>
    <div class="lbl">Open a case to rank the trails from their car park, or from the park entrance if the car isn't found.</div>
    <button class=${`btn ${big ? 'primary big' : ''}`} onClick=${() => setForm({ name: '', age: '', details: '', carrying: 'iphone', car_park: '' })}>Open a case</button>
  </section>`;
}

function CaseCard({ kase, cv, setForm, closeCase }) {
  const [sure, setSure] = useState(false);
  return html`<section class="p">
    <div style="display:flex;align-items:center;gap:0.8rem">
      <span class="carp" aria-hidden="true">${kase.car_park ? 'P' : 'E'}</span>
      <div style="flex:1"><div style="font-size:1.25rem;font-weight:700">${kase.name}${kase.age ? `, ${kase.age}` : ''}</div>
        <div class="lbl small">From ${cv.start?.label || 'the park entrance'} · ${CARRY_NAME[kase.carrying] || 'carrying not known'} · reported ${fmtHM(kase.opened_t)}</div></div>
    </div>
    ${kase.details && html`<div style="line-height:1.4">${kase.details}</div>`}
    ${kase.ping && html`<div class="row"><span class="lbl">Linked ping</span><b>${kase.ping}</b></div>`}
    <div style="display:flex;gap:1rem">
      <button class="link" onClick=${() => setForm({ ...kase })}>Edit case</button>
      ${sure ? html`<button class="link" style="color:#ff8f93" onClick=${() => { setSure(false); closeCase(); }}>Sure? Close it</button>`
             : html`<button class="link" onClick=${() => setSure(true)}>Close case</button>`}
    </div>
  </section>`;
}

function TrailsCard({ cv, pickTrail }) {
  return html`<section class="p grow">
    <h2 class="h">Most likely trails</h2>
    ${cv.ranking.slice(0, 3).map((r, i) => html`<button class=${`trail ${r.name === cv.trail ? 'on' : ''}`} key=${r.name} aria-pressed=${r.name === cv.trail}
        onClick=${() => r.name !== cv.trail && pickTrail(r.name)} title=${r.name === cv.trail ? 'Being searched' : 'Search this trail instead'}>
      <span class="num" style="font-size:1.1rem;color:${r.name === cv.trail ? 'var(--text)' : 'var(--muted)'}">${i + 1}</span>
      <span><b style="font-weight:${r.name === cv.trail ? 700 : 600}">${r.name}</b><div class="lbl small">${r.name === cv.trail ? 'being searched' : r.dist_m < 50 ? 'starts by the car' : `${fmtM(r.dist_m)} from the car`}</div><div class="bar"><i style=${`width:${r.pct}%`}></i></div></span>
      <span class="num" style="font-size:1.25rem;text-align:right">${r.pct}%</span>
    </button>`)}
    <div class="lbl small" style="margin-top:auto">Sample numbers. Click a trail to search it instead.</div>
  </section>`;
}

function TrailProgress({ cv, m, mode, st }) {
  if (!cv.trail || !m.bins.length) return null;
  const hit = m.binsHit.reduce((a, v) => a + v, 0);
  const total = routeLen(cv.route);
  const pct = Math.min(100, Math.round((100 * hit) / m.bins.length));
  const flown = (total * pct) / 100;
  return html`<section class="p accent">
    <h2 class="h">Searching · ${cv.trail}</h2>
    <div style="display:flex;align-items:baseline;gap:0.7rem"><span class="hero">${pct}%</span><span class="lbl">of the first ${fmtM(total)} from the car</span></div>
    <div class="bar tan"><i style=${`width:${pct}%`}></i></div>
    <div class="row"><span class="lbl">Searched</span><span class="num">${fmtM(flown)} of ${fmtM(total)}</span></div>
    <div class="lbl small">${pct >= 100 ? 'Stretch done. Pick the next trail, or keep flying it.' : mode === 'search' ? `The drone hears ±${st.corridor} m either side of the trail.` : 'Trail pathing is off.'}</div>
  </section>`;
}

function LegCard({ s, locked, busy, sendCtrl }) {
  const plan = Object.values(s.plans || {}).filter((p) => p.status === 'proposed').sort((a, b) => b.t - a.t)[0];
  if (!plan) return null;
  const leg = plan.legs?.[0] || {};
  const key = `leg:${plan.id}`;
  return html`<section class="p legc">
    <h2 class="h">Next leg · needs ranger</h2>
    <div style="font-size:1.1rem;font-weight:600">${plan.id} · ${plan.kind}${plan.legs?.length > 1 ? ` · ${plan.legs.length} legs` : ''}</div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0.5rem;text-align:center">
      <div><div class="num" style="font-size:1.7rem">${leg.heading_deg != null ? Math.round(leg.heading_deg) + '°' : '—'}</div><div class="lbl small">heading</div></div>
      <div><div class="num" style="font-size:1.7rem">${fmtM(leg.dist_m)}</div><div class="lbl small">distance</div></div>
      <div><div class="num" style="font-size:1.7rem">${leg.alt_m != null ? Math.round(leg.alt_m) + ' m' : '—'}</div><div class="lbl small">altitude</div></div>
    </div>
    <div class="btns even">
      <button class="btn primary" disabled=${locked || busy[key]} onClick=${() => sendCtrl({ cmd: 'accept_leg', id: plan.id }, `Accept ${plan.id}`, key)}>Accept leg</button>
      <button class="btn" disabled=${locked || busy[key]} onClick=${() => sendCtrl({ cmd: 'reject_leg', id: plan.id }, `Reject ${plan.id}`, key)}>Reject</button>
    </div>
  </section>`;
}

function PingsTable({ open, bt, sel, pick, mode }) {
  return html`<section class="p">
    <div class="sh"><h2 class="h">Pings</h2><span class="aux">${open.length ? `${open.length} open` : 'none open'}</span></div>
    ${open.length ? html`<table class="tbl">
      <thead><tr><th></th><th>Beacon</th><th>Status</th><th class="r">Off trail</th><th class="r">Heard</th></tr></thead>
      <tbody>${open.map((p) => html`<tr key=${p.pin} class=${p.pin === sel ? 'sel' : ''} tabindex="0" onClick=${() => pick(p.pin)} onKeyDown=${(e) => e.key === 'Enter' && pick(p.pin)}>
        <td><span class="mini" style=${`background:${prioColor(p.prio)}`}>${p.pin}</span></td>
        <td>${CLS_SHORT[p.cls] || p.cls} <span class="dim">±${fmtM(p.r95)}</span></td>
        <td><span class="stt" style=${`color:${p.status === 'new' ? prioColor(p.prio) : 'var(--muted)'}`}>${p.status}</span></td>
        <td class="r mono">${p.off_trail_m != null ? `${p.off_trail_m} m` : '—'}</td>
        <td class="r mono dim">${fmtAgo(bt - (p.t - (p.last_s || 0) * 1000))}</td></tr>`)}</tbody>
    </table>` : html`<div class="lbl">${mode === 'night' ? 'Nothing yet. A chime plays when one arrives.' : 'Nothing needs the ranger.'}</div>`}
  </section>`;
}


function RescueWatch({ p, kase, m, bt, s, closeCase, checkin, every, routeP, setRoutePin }) { // back on the sweep, a person found: keep an eye on them
  const th = s.latest['ag/thermal'], v = s.latest['ag/vision'];
  const c = checkin;
  const near = !!c?.near;
  const [sure, setSure] = useState(false);
  return html`<section class="p" style="box-shadow:inset 3px 0 0 var(--p1)">
    <h2 class="h">Rescue under way · ${p.pin}${kase?.ping === p.pin ? ` · ${kase.name}` : ''}</h2>
    <div class="row"><span class="lbl">Team</span><span>${kase?.team_t ? `on the way · ${fmtDur(Date.now() - kase.team_t)}` : m.teamPins?.[p.pin] ? 'on the way' : 'not sent yet'}</span></div>
    <div class="row"><span class="lbl">Last check-in</span><span>${near ? 'over them now' : c?.last != null ? fmtAgo(bt - c.last) : 'none yet'}</span></div>
    ${c?.sent && !near && html`<div class="row"><span class="lbl">Next check-in</span>
      <span class=${`num ${c.due ? 'duetxt' : ''}`} style="font-size:1.4rem">${c.due ? `due · ${fmtDur(-c.left)} late` : `in ${fmtDur(c.left)}`}</span></div>`}
    ${c?.sent && html`<div class="lbl small">Every ${fmtDur(every)}: fly over them, look at the heat and camera, then back to the sweep.</div>`}
    ${near && html`<div class="row"><span class="lbl">Seen</span><span class="warmtxt">${th?.hot ? `${fmtC(th.max_c) || ''} warm`.trim() : 'cool'}${v && bt - v.t < 5000 ? ' · person on camera' : ''}</span></div>`}
    <div style="display:flex;gap:1rem">
      ${!routeP && html`<button class="link" onClick=${() => setRoutePin(p.pin)}>Show the rescue route</button>`}
      ${kase && (sure ? html`<button class="link" style="color:#ff8f93" onClick=${() => { setSure(false); closeCase(); }}>Sure? Close the case</button>`
                      : html`<button class="link" onClick=${() => setSure(true)}>Person safe · close case</button>`)}
    </div>
  </section>`;
}

function EventsCard({ m }) {
  const ev = m.events.slice(0, 8);
  return html`<section class="p fill">
    <h2 class="h">Activity</h2>
    <div class="events">${ev.length ? ev.map((e) => html`<div><time class="mono">${fmtClock(e.t)}</time><span>${e.text}</span></div>`) : html`<div class="dim">Nothing yet</div>`}</div>
  </section>`;
}

function PingCard({ p, alertList = [], meta, s, m, bt, cv, kase, locked, busy, pinCmd, setSel, sel, pick, linkPing, confirmRescue, notPerson }) {
  const camp = distToCamp(meta, p.x, p.y);
  const start = cv.start && kase ? cv.start : null;
  const fromStart = start ? Math.hypot(p.x - start.x, p.y - start.y) : null;
  const th = s.latest['ag/thermal'];
  const warm = p.flags?.thermal;
  const match = kase?.expect_cls && kase.expect_cls === p.cls;
  const b = busy[p.pin];
  // several new pings: tabs (and ‹ ›, or [ and ]) switch which one this card and the map show. Only the view changes.
  const n = alertList.length, i = alertList.findIndex((q) => q.pin === p.pin), tabs = n > 1 && i >= 0;
  const step = (d) => pick(alertList[(i + d + n) % n].pin);
  useEffect(() => {
    if (!tabs) return undefined;
    const onKey = (e) => {
      if (document.querySelector('.overlay') || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      if (e.key === ']') step(1); else if (e.key === '[') step(-1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  const head = p.status === 'new' ? html`Needs a decision${n ? html`<span class="count">${n}</span>` : ''}`
    : p.status === 'investigating' ? 'Checking this ping' : p.status === 'acknowledged' ? 'Ping acknowledged' : `Ping ${p.status}`;
  return html`<section class="p decision" style=${`--prio:${prioColor(p.prio)}`}>
    <div class="sh"><h2 class="h">${head}</h2>
      <span class="aux">${tabs ? '[ ] to switch' : ''}${sel === p.pin && html` <button class="link" onClick=${() => setSel(null)}>Back</button>`}</span></div>
    ${tabs && html`<div class="ptabs" role="tablist" aria-label="New pings">
      <button class="step" aria-label="Previous ping" onClick=${() => step(-1)}>‹</button>
      ${alertList.map((q) => html`<button key=${q.pin} role="tab" aria-selected=${q.pin === p.pin} class=${q.pin === p.pin ? 'on' : ''} onClick=${() => pick(q.pin)}
        title=${`${q.pin} · ${CLS_PLAIN[q.cls] || q.cls}`}><i style=${`background:${prioColor(q.prio)}`}></i>${q.pin}<small>${fmtDur(bt - (m.firstSeen[q.pin] ?? bt))}</small></button>`)}
      <button class="step" aria-label="Next ping" onClick=${() => step(1)}>›</button>
    </div>`}
    <div class="pid">
      <span class="pchip">${p.pin}</span>
      <div><div class="what">${CLS_PLAIN[p.cls] || p.cls}</div><div class="why">Priority ${p.prio} · ${PRIO_NAME[p.prio] || ''}</div></div>
      ${p.status === 'new' && html`<div class="wait"><b>${fmtDur(bt - (m.firstSeen[p.pin] ?? bt))}</b><span>waiting</span></div>`}
    </div>
    ${match && html`<div class="okline">Matches what the person is carrying</div>`}
    ${kase && html`<div class="row" style="align-items:center"><span class="lbl">${kase.ping === p.pin ? `This is ${kase.name}'s ping` : kase.ping ? `Case is linked to ${kase.ping}` : 'Not linked to the case'}</span>
      <button class="link" onClick=${() => linkPing(kase.ping === p.pin ? '' : p.pin)}>${kase.ping === p.pin ? 'Unlink' : 'Link to case'}</button></div>`}
    ${p.sos && html`<div class="badline">SOS: ${p.sos.hurt ? 'hurt' : 'not hurt'}, ${p.sos.walk ? 'can walk' : "can't walk"}, ${p.sos.count || '?'} people</div>`}
    <div class="kv">
      <div><b>±${fmtM(p.r95)}</b><span>search circle</span></div>
      <div><b>${fmtAgo(bt - (p.t - (p.last_s || 0) * 1000))}</b><span>last heard</span></div>
      <div><b>${p.off_trail_m != null ? `${p.off_trail_m} m` : '—'}</b><span>off trail</span></div>
      <div><b>${camp === 0 ? 'inside' : fmtM(camp)}</b><span>${camp === 0 ? 'the campground' : 'from campground'}</span></div>
      ${fromStart != null && html`<div><b>${fmtM(fromStart)}</b><span>from ${start.label === 'Park entrance' ? 'the entrance' : 'the car'}</span></div>`}
      ${warm && html`<div class="warm"><b>${fmtC(th?.max_c) || 'warm'}</b><span>thermal: warm spot</span></div>`}
    </div>
    ${p.status === 'new' || p.status === 'acknowledged' ? html`
      <div class="btns">
        <button class="btn primary big" disabled=${locked || b} onClick=${() => pinCmd(p.pin, 'investigate', 'Investigate')}>Investigate</button>
        <button class="btn big" disabled=${locked || b} onClick=${() => pinCmd(p.pin, 'clear', 'Clear')}>Clear</button>
      </div>
      <button class="btn ghost" disabled>Confirm person · unlocks while investigating</button>`
    : p.status === 'investigating' ? html`<${CheckPanel} p=${p} c=${m.checks[p.pin]} bt=${bt} locked=${locked} b=${b} confirmRescue=${confirmRescue} notPerson=${notPerson} />`
    : html`<div class="lbl" style="text-align:center">${p.status === 'confirmed' ? 'Person confirmed by the ranger' : 'Cleared by the ranger'}</div>`}
    ${locked && html`<div class="lbl small">Commands need the Wi-Fi link to the pod.</div>`}
    ${p.status === 'new' && html`<div class="rule">The drone never decides: every ping waits for a ranger.</div>`}
  </section>`;
}

function CheckPanel({ p, c, bt, locked, b, confirmRescue, notPerson }) {
  const v = verdict(c, bt);
  const heat = !c || c.closeSince == null ? 'waiting' : c.warm ? `${c.maxC?.toFixed(1)}°C · warm` : c.maxC != null ? `${c.maxC.toFixed(1)}°C · nothing warm` : 'reading…';
  const cam = !c || c.closeSince == null ? 'waiting' : c.person ? `person seen · ${Math.round(c.person * 100)}%` : v === 'none' || v === 'unclear' ? 'no person seen' : 'looking…';
  const head = { flying: `Drone flying to ${p.pin} · ${c?.dist != null ? fmtM(c.dist) : '…'}`, checking: `Over ${p.pin} · checking`,
    person: 'Person found: warm, and seen on camera', none: 'No one here: nothing warm, no person seen', unclear: 'Not sure: check the camera yourself' }[v];
  return html`<div class=${`check v-${v}`} role="status">
    <div class="check-head">${head}</div>
    <div class="row"><span class="lbl">Drone</span><span>${c?.closeSince != null ? 'over the ping' : c?.dist != null ? `${fmtM(c.dist)} away` : 'on its way'}</span></div>
    <div class="row"><span class="lbl">Heat</span><span class=${c?.warm ? 'warmtxt' : ''}>${heat}</span></div>
    <div class="row"><span class="lbl">Camera</span><span>${cam}</span></div>
  </div>
  ${v === 'person' ? html`<button class="btn danger big" disabled=${locked || b} onClick=${() => confirmRescue(p.pin)}>Confirm person · start rescue</button>
      <button class="btn" disabled=${locked || b} onClick=${() => notPerson(p.pin)}>Not a person · continue search</button>`
    : v === 'none' ? html`<button class="btn primary big" disabled=${locked || b} onClick=${() => notPerson(p.pin)}>Not a person · continue search</button>
      <button class="btn" disabled=${locked || b} onClick=${() => confirmRescue(p.pin)}>Confirm person anyway</button>`
    : html`<div class="btns even">
        <button class="btn danger" disabled=${locked || b} onClick=${() => confirmRescue(p.pin)}>Confirm person</button>
        <button class="btn" disabled=${locked || b} onClick=${() => notPerson(p.pin)}>Not a person</button></div>`}`;
}

function PilotCard({ s, pose, target }) {
  const plan = Object.values(s.plans || {}).filter((p) => p.kind === 'homing' && (p.status === 'accepted' || p.status === 'proposed')).sort((a, b) => b.t - a.t)[0];
  const leg = plan?.legs?.[0];
  let hdg = null, dist = null, alt = pose?.z ?? null, how = '';
  if (leg) { hdg = leg.heading_deg; dist = leg.dist_m; alt = leg.alt_m ?? alt; how = `${plan.id} · ${plan.status === 'accepted' ? 'accepted by ranger' : 'proposed, not accepted yet'}`; }
  else if (pose && target) { hdg = heading(target.x - pose.x, target.y - pose.y); dist = Math.hypot(target.x - pose.x, target.y - pose.y); how = `Worked out from the drone to ${target.pin}`; }
  return html`<section class="p podc">
    <h2 class="h">Read to pilot${target ? ` · to ${target.pin}` : ''}</h2>
    ${hdg == null ? html`<div class="lbl">Pick a ping to get a heading.</div>` : html`
      <div style="display:flex;align-items:center;gap:1.1rem">
        <svg class="compass" viewBox="0 0 100 100" role="img" aria-label=${`Heading ${Math.round(hdg)} degrees`}>
          <circle cx="50" cy="50" r="44" fill="none" stroke="var(--line-2)" stroke-width="3"></circle>
          <text x="50" y="17" fill="var(--muted)" font-size="11" text-anchor="middle">N</text>
          <g transform=${`rotate(${hdg} 50 50)`}><path d="M50 14 L60 52 L50 46 L40 52 Z" fill="var(--pod)"></path></g>
        </svg>
        <div><div class="num" style="font-size:4rem;line-height:1">${Math.round(hdg)}°</div><div class="lbl">heading</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem">
        <div><div class="num" style="font-size:2.3rem">${fmtM(dist)}</div><div class="lbl">to ping</div></div>
        <div><div class="num" style="font-size:2.3rem">${alt != null ? Math.round(alt) + ' m' : '—'}</div><div class="lbl">altitude</div></div>
      </div>
      <div class="lbl small">${how}</div>`}
  </section>`;
}

function SosCard({ target, locked, busy, sendCtrl }) {
  const sos = target?.sos;
  return html`<section class="p grow">
    <h2 class="h">SOS answer</h2>
    ${sos ? html`<div class="sosgrid">
        <div class=${sos.hurt ? 'bad' : ''}><b>Hurt</b><span class="lbl">${sos.hurt ? 'yes' : 'no'}</span></div>
        <div class=${sos.walk ? '' : 'bad'}><b>Walk</b><span class="lbl">${sos.walk ? 'yes' : 'no'}</span></div>
        <div><b>${sos.count ?? '?'}</b><span class="lbl">people</span></div>
      </div>` : html`<div class="lbl">No answer yet. Turn the SOS Wi-Fi page on when the pod is near the ping.</div>`}
    <div style="flex:1"></div>
    <div class="row" style="align-items:center"><span class="lbl">SOS Wi-Fi page</span>
      <span style="display:flex;gap:0.5rem">
        <button class="btn" disabled=${locked || busy.sos} onClick=${() => sendCtrl({ cmd: 'sos_on' }, 'SOS page on', 'sos')}>On</button>
        <button class="btn" disabled=${locked || busy.sos} onClick=${() => sendCtrl({ cmd: 'sos_off' }, 'SOS page off', 'sos')}>Off</button>
      </span></div>
  </section>`;
}

function CameraCard({ s, bt, m, target, podSite }) { // Emergency: the live camera and heat pictures, for the ranger's own check
  const v = s.latest['ag/vision'], th = s.latest['ag/thermal'];
  const c = target && m.checks[target.pin];
  const seen = v && bt - v.t < 10000 ? v : null;
  const snap = v?.snap && bt - v.t < 60000 ? v.snap : null; // stretch: the person detector's picture
  const heat = c?.warm ? `${fmtC(c.maxC) || 'warm spot'} · warm` : heatText(th, bt);
  return html`<section class="p grow feedcard">
    <h2 class="h">Camera check${seen ? ` · ${seen.kind} ${Math.round(seen.conf * 100)}%` : ''}</h2>
    ${snap ? html`<div class="feed"><img src=${`/${snap}`} alt=${`Camera snapshot: ${v.kind}`} /><span class="feed-name">Snapshot</span></div>`
           : html`<${LiveFeed} file="camera.jpg" name="Camera" pod=${podSite} />`}
    <${LiveFeed} file="thermal.png" name="Heat" tag=${heat} warm=${!!(c?.warm || (th?.hot && heat))} pod=${podSite} />
    ${seen && html`<div class="row"><span class="lbl">Camera</span><span>person seen · ${Math.round(seen.conf * 100)}%</span></div>`}
  </section>`;
}

// ---------- live pictures (live/camera.jpg from camera.py, live/thermal.png from thermal.py) ----------
// Each feed fetches its picture once a second and swaps it in only once it has decoded, so it never
// flickers. The server's Last-Modified and Date headers give the picture's age: an old picture is
// dimmed and marked "frozen" (the camera stopped), and failed fetches mark it "lost".
// pod: the real pod's feed (live-demo site). Then nothing stale is passed off as live: no picture, a
// failed fetch or a picture over a minute old shows NO SIGNAL instead of an old frame.
function LiveFeed({ file, name, tag, warm, every = FEED_EVERY_MS, pod = false, children }) {
  const [f, setF] = useState({ url: null, age: null, err: false, wall: 0 });
  useEffect(() => {
    let alive = true, busy = false, url = null;
    const pull = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const r = await fetch(`/live/${file}`, { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const lm = Date.parse(r.headers.get('Last-Modified') || ''), now = Date.parse(r.headers.get('Date') || '');
        const age = isFinite(lm) && isFinite(now) ? Math.max(0, (now - lm) / 1000) : null;
        const next = URL.createObjectURL(await r.blob());
        const img = new Image();
        img.src = next;
        try { await img.decode(); } catch (e) { URL.revokeObjectURL(next); throw e; }
        if (!alive) { URL.revokeObjectURL(next); return; }
        const old = url;
        url = next;
        setF({ url: next, age, err: false, wall: Date.now() });
        if (old) setTimeout(() => URL.revokeObjectURL(old), 2000); // after the <img> has moved on
      } catch {
        if (alive) setF((p) => ({ ...p, err: true }));
      } finally { busy = false; }
    };
    pull();
    const id = setInterval(pull, every);
    return () => { alive = false; clearInterval(id); if (url) setTimeout(() => URL.revokeObjectURL(url), 2000); };
  }, [file, every]);
  const lost = f.err && Date.now() - f.wall > FEED_OLD_S * 1000;
  const frozen = !lost && f.age != null && f.age > FEED_OLD_S;
  const since = lost ? Date.now() - f.wall + (f.age || 0) * 1000 : (f.age || 0) * 1000;
  if (pod && (f.url ? lost || (frozen && f.age > 60) : f.err)) {
    return html`<div class="feed nosignal" role="status">
      <div class="nosig"><b>No signal</b><span>The ${name.toLowerCase()} feed from the pod isn't reaching this laptop${f.url ? ` · last picture ${fmtAgo(since)}` : ''}</span></div>
      <span class="feed-name">${name}</span>
    </div>`;
  }
  return html`<div class=${`feed ${warm ? 'warm' : ''}`}>
    ${f.url ? html`<img src=${f.url} alt=${`Live ${name.toLowerCase()} picture`} class=${lost || frozen ? 'old' : ''} />`
            : html`<span class="feed-none">${f.err ? `No ${name.toLowerCase()} feed` : 'Connecting…'}</span>`}
    ${f.url && !lost && !frozen && children}
    <span class="feed-name">${name}</span>
    ${tag && html`<span class="feed-tag">${tag}</span>`}
    ${f.url && (lost || frozen) && html`<span class="feed-old">${lost ? 'Feed lost' : 'Feed frozen'} · picture from ${fmtAgo(since)}</span>`}
  </div>`;
}

// ---------- the Live tab: everything the pod is sending, live ----------
// Camera, the heat picture with the thermal readings drawn over it, the building map with the pod
// on it, and the raw numbers. Only looks: nothing here sends a command.
// Pictures first, side by side and the same size; the numbers under them. The live-demo site
// (pod = true) adds the building map and the pod's components; the simulation needs neither.
function LiveView({ meta, s, st, m, bt, pose, podStale, podSite }) {
  const th = s.latest['ag/thermal'];
  const fresh = th && bt - th.t < THERMAL_FRESH_MS ? th : null;
  const plan = !!meta.building && !meta.walk; // the main view already is the floor plan when there's a walk line
  return html`<div class=${`live ${plan ? '' : 'flat'}`}>
    <section class="p livecell">
      <h2 class="h">Camera · Camera Module 3</h2>
      <${LiveFeed} file="camera.jpg" name="Camera" every=${500} pod=${podSite} />
    </section>
    <section class="p livecell">
      <h2 class="h">Heat · HT-203U thermal${fresh?.src ? ` · ${fresh.src === 'ht203u' ? 'live sensor' : fresh.src}` : ''}</h2>
      <${LiveFeed} file="thermal.png" name="Heat" every=${250} tag=${heatText(th, bt)} warm=${!!fresh?.hot} pod=${podSite}>
        ${fresh && html`<${ThermalOverlay} th=${fresh} />`}
      </${LiveFeed}>
    </section>
    ${plan && html`<section class="p livecell">
      <h2 class="h">${meta.building.name}${meta.building.approximate ? ' · approximate outline' : ''}${pose ? ` · pod by ${pose.src === 'waypoint' ? 'taped waypoint' : pose.src}` : ''}</h2>
      <${BuildingMap} meta=${meta} s=${s} pose=${pose} podStale=${podStale} />
    </section>`}
    <section class="p livecell livedata">
      <h2 class="h">Live readings</h2>
      <${LiveData} meta=${meta} s=${s} st=${st} m=${m} bt=${bt} pose=${pose} th=${th} fresh=${fresh} podSite=${podSite} />
    </section>
    ${podSite && html`<div class="livehealth"><${BatteryCard} meta=${meta} s=${s} bt=${bt} podStale=${podStale} /></div>`}
  </div>`;
}

// Drawn over the heat picture in the frame's own pixels (viewBox w x h, scaled like the picture's
// object-fit: contain), so the marks sit on the warmest spot thermal.py found.
function ThermalOverlay({ th }) {
  const { w, h } = th;
  if (!(w > 0 && h > 0) || typeof th.col !== 'number' || typeof th.row !== 'number') return null;
  const x = th.col + 0.5, y = th.row + 0.5, r = Math.max(w, h) / 22;
  const c = fmtC(th.max_c);
  const right = x < w * 0.7; // keep the label inside the frame
  return html`<svg class=${`thover ${th.hot ? 'hot' : ''}`} viewBox=${`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
    <circle cx=${x} cy=${y} r=${r}></circle>
    <line x1=${x - r * 1.8} y1=${y} x2=${x - r * 0.6} y2=${y}></line><line x1=${x + r * 0.6} y1=${y} x2=${x + r * 1.8} y2=${y}></line>
    <line x1=${x} y1=${y - r * 1.8} x2=${x} y2=${y - r * 0.6}></line><line x1=${x} y1=${y + r * 0.6} x2=${x} y2=${y + r * 1.8}></line>
    <text x=${right ? x + r * 2.1 : x - r * 2.1} y=${y - r * 0.4} text-anchor=${right ? 'start' : 'end'} font-size=${r * 1.5}>${c || 'warmest'}${th.hot ? ' · warm' : ''}</text>
  </svg>`;
}

function Sparkline({ hist, lo, hi, band }) { // max_c over the last 2 minutes; band: the person-warm range
  if (hist.length < 2) return html`<div class="lbl small">Waiting for readings…</div>`;
  const t0 = hist[0][0], t1 = hist[hist.length - 1][0], span = Math.max(1, t1 - t0);
  const vs = hist.map((p) => p[1]);
  const a = Math.min(lo ?? Infinity, ...vs) - 1, b = Math.max(hi ?? -Infinity, ...vs) + 1;
  const X = (t) => ((t - t0) / span) * 100, Y = (v) => 40 - ((v - a) / (b - a)) * 40;
  const pts = hist.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  return html`<svg class="spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="Warmest spot over the last 2 minutes">
    ${band && html`<rect x="0" width="100" y=${Y(Math.min(band[1], b))} height=${Math.max(0, Y(Math.max(band[0], a)) - Y(Math.min(band[1], b)))} class="band"></rect>`}
    <polyline points=${pts}></polyline>
  </svg>`;
}

function LiveData({ meta, s, st, m, bt, pose, th, fresh, podSite }) {
  const rate = (t) => st.rates?.[t] ?? 0;
  const sys = s.latest['ag/sys'];
  const recent = Object.values(s.obs).filter((o) => bt - o.t < 30000);
  const beacons = recent.filter((o) => o.kind === 'ble' && o.cls !== 'apple_other' && o.cls !== 'other');
  const strongest = beacons.sort((a, b) => b.rssi - a.rssi)[0];
  const pins = Object.values(s.pins).filter(isOpen);
  const al = s.latest['ag/alert'];
  const band = meta.thermal ? [meta.thermal.hot_min_c, meta.thermal.hot_max_c] : null;
  const Row = (k, v, cls = '') => html`<div class="row"><span class="lbl">${k}</span><span class=${`num ${cls}`}>${v}</span></div>`;
  return html`
    <div class="livegrid">
      <div>
        <div class="livehead">Thermal</div>
        <div class=${`num livebig ${fresh?.hot ? 'warmtxt' : ''}`}>${fresh ? fmtC(fresh.max_c) || 'no °C' : '—'}</div>
        <div class=${fresh?.hot ? 'warmtxt' : 'lbl'}>${!fresh ? 'No thermal data' : fresh.hot ? 'Warm spot: person-warm' : 'Warmest spot: nothing person-warm'}</div>
        <${Sparkline} hist=${m.thHist} lo=${band?.[0] - 4} hi=${band?.[0] + 4} band=${band} />
        ${Row('Spot', typeof fresh?.col === 'number' ? `${fresh.col}, ${fresh.row} in ${fresh.w}×${fresh.h}` : '—')}
        ${Row('Confidence', fresh ? `${Math.round((fresh.conf ?? 0) * 100)}%` : '—')}
        ${Row('Frames', `${rate('ag/thermal').toFixed(1)} /s${th ? ` · ${fmtAgo(bt - th.t)}` : ''}`)}
        ${Row('Source', fresh ? ({ ht203u: 'HT-203U sensor', fake: 'fake frames' }[fresh.src] || fresh.src || 'simulation') : '—')}
        ${Row('Person-warm', band ? `${band[0]}–${band[1]} °C` : '—')}
      </div>
      <div>
        <div class="livehead">Pod</div>
        ${Row('Position', pose ? `${pose.x.toFixed(1)} E, ${pose.y.toFixed(1)} N m` : '—')}
        ${Row('Height', pose?.z != null ? `${pose.z.toFixed(1)} m` : '—')}
        ${Row('Position from', pose ? `${pose.src}${pose.wp != null ? ` ${pose.wp}` : ''} · ${rate('ag/pose').toFixed(1)} /s` : '—')}
        ${pose?.src === 'imu' && Row('Walked', `${(pose.dist_m ?? 0).toFixed(1)} m · ${pose.steps ?? 0} steps${pose.heading != null ? ` · heading ${Math.round(pose.heading)}°` : ''}`)}
        ${podSite && html`
        ${Row('CPU', sys ? `${Math.round(sys.cpu_c)} °C${sys.throttled && sys.throttled !== '0x0' ? ' · throttling' : ''}` : '—', sys?.cpu_c >= 80 ? 'duetxt' : '')}
        ${Row('LoRa radio', sys ? (sys.lora ? 'on' : 'down') : '—', sys && !sys.lora ? 'duetxt' : '')}`}
      </div>
      <div>
        <div class="livehead">Bluetooth and pings</div>
        ${Row('Scans', `${rate('ag/obs/ble').toFixed(1)} /s`)}
        ${Row('Lost devices', `${beacons.length} of ${recent.length} · 30 s`)}
        ${Row('Strongest', strongest ? `${CLS_SHORT[strongest.cls] || strongest.cls} · ${strongest.rssi} dBm` : '—')}
        ${Row('Open pings', pins.length ? pins.map((p) => p.pin).join(' ') : 'none')}
        ${Row('SOS Wi-Fi', s.latest['ag/sos'] ? `answer ${fmtAgo(bt - s.latest['ag/sos'].t)}` : 'no answers')}
        ${Row('LoRa heard', al ? `${fmtAgo(Date.now() - m.lastAlertWall)} · ${al.rssi} dBm` : '—')}
        ${Row('Link', st.link !== 'live' ? 'server unreachable' : !s.broker ? 'broker down' : 'live', st.link !== 'live' || !s.broker ? 'duetxt' : '')}
      </div>
    </div>`;
}

// ---------- health: the pod's battery alone (health.py's ag/sys batt_pct) ----------
function BatteryCard({ s, bt, meta, podStale }) {
  const sys = s.latest['ag/sys'];
  const v = typeof sys?.batt_pct === 'number' && isFinite(sys.batt_pct) ? sys.batt_pct : null;
  const age = sys && bt ? Math.max(0, bt - sys.t) : null;
  const every = (meta.health?.period_s ?? 5) * 1000;
  const stale = !!sys && (podStale || age > Math.max(3 * every, 15000));
  const state = v == null ? 'off' : v < 20 ? 'crit' : v < 40 ? 'warn' : 'ok';
  return html`<section class="p battery">
    <div class="sh"><h2 class="h">Pod battery</h2><span class="aux">${!sys ? 'no report yet' : `reported ${fmtAgo(age)}${stale ? ' · old' : ''}`}</span></div>
    <div class="battrow"><b class=${`mono b-${state}`}>${v != null ? `${v}%` : '—'}</b>
      <span class="lbl">${v == null ? (sys ? 'not measured (on USB power)' : 'waiting for health.py on the pod') : state === 'crit' ? 'low' : state === 'warn' ? 'getting low' : 'good'}</span></div>
    <div class="bar"><i class=${`b-${state}`} style=${`width:${v ?? 0}%`}></i></div>
  </section>`;
}


// ---------- the indoor walk (State St, [pose] source = "imu") ----------
// The main map for the live demo: inside the building, north up, in local metres. Someone carries
// the pod forward along one straight line ([imu] line_from -> line_to); position.py counts the
// IMU's steps along it. The walked part fills in, a mark every metre, base at the start.
function IndoorStage({ meta, s, st, m, bt, pose, podStale, podSite, sel, setSel }) {
  const th = s.latest['ag/thermal'];
  const fresh = th && bt - th.t < THERMAL_FRESH_MS ? th : null;
  const rate = (t) => st.rates?.[t] ?? 0;
  return html`<div class="istage">
    <div class="feeds">
      <section class="pane">
        <div class="sh"><h2 class="h">Camera</h2><span class="aux">Camera Module 3</span></div>
        <${LiveFeed} file="camera.jpg" name="Camera" every=${500} pod=${podSite} />
      </section>
      <section class="pane">
        <div class="sh"><h2 class="h">Heat</h2><span class="aux">HT-203U · <b>${rate('ag/thermal').toFixed(1)}</b> fps</span></div>
        <${LiveFeed} file="thermal.png" name="Heat" every=${250} tag=${heatText(th, bt)} warm=${!!fresh?.hot} pod=${podSite}>
          ${fresh && html`<${ThermalOverlay} th=${fresh} />`}
        </${LiveFeed}>
      </section>
      <section class="pane">
        <div class="sh"><h2 class="h">Nearby signals</h2></div>
        <iframe class="nearby" src="nearby.html?compact=1" title="Signals around the pod: rings heard, dots located"></iframe>
      </section>
    </div>
    <${HallWalk} meta=${meta} s=${s} pose=${pose} podStale=${podStale} sel=${sel} setSel=${setSel} />
    <${Readings} meta=${meta} s=${s} st=${st} m=${m} bt=${bt} fresh=${fresh} />
  </div>`;
}

// The indoor walk ([pose] source = "imu"): someone carries the pod along one straight line
// ([imu] line_from -> line_to); position.py counts the IMU's steps along it. Drawn as a strip,
// base at the left: the walked part fills in, a mark every metre, pings beside the line.
function HallWalk({ meta, s, pose, podStale, sel, setSel }) {
  const w = meta.walk;
  const [ax, ay] = w.from, [bx, by] = w.to;
  const len = Math.hypot(bx - ax, by - ay) || 1;
  const ux = (bx - ax) / len, uy = (by - ay) / len, nx = -uy, ny = ux; // along, and across, the line
  const live = pose && !podStale;
  const d = pose ? Math.max(0, Math.min(len, pose.dist_m ?? ((pose.x - ax) * ux + (pose.y - ay) * uy))) : 0;
  const pct = (v) => `${(100 * v) / len}%`;
  const every = len <= 20 ? 1 : len <= 60 ? 5 : 10;
  const ticks = [];
  for (let k = every; k < len - 1e-6; k += every) ticks.push(k);
  const pins = Object.values(s.pins).filter((p) => p.status !== 'cleared');
  const hd = pose?.heading;
  return html`<section class="walk">
    <div>
      <div class="sh"><h2 class="h">Hall walk</h2><span class="aux">${meta.building?.name || meta.name} · a mark every ${every} m</span></div>
      <div class="hall" role="img" aria-label=${`Indoor walk: ${d.toFixed(1)} of ${len.toFixed(0)} metres`}>
        <div class="h-todo"></div>
        ${d > 0 && html`<div class="h-done" style=${`width:${pct(d)}`}></div>`}
        ${ticks.map((k) => html`<div key=${k} class=${`tick ${d > 0 && k <= d + 1e-6 ? 'on' : ''}`} style=${`left:${pct(k)}`}><b>${k}</b></div>`)}
        ${pins.map((p) => {
          const along = Math.max(0, Math.min(len, (p.x - ax) * ux + (p.y - ay) * uy)), side = (p.x - ax) * nx + (p.y - ay) * ny;
          return html`<button key=${p.pin} class=${`h-ping ${side < 0 ? 'below' : ''} ${sel === p.pin ? 'sel' : ''}`} style=${`left:${pct(along)};--c:${prioColor(p.prio)}`}
            onClick=${() => setSel(p.pin)} title=${`${p.pin} · ${along.toFixed(1)} m along`}>${p.pin}</button>`;
        })}
        <div class="h-base" title="Base: the start">B</div><div class="h-end" title=${`End · ${len.toFixed(0)} m`}></div>
        ${pose && html`<div class=${`h-pod ${live ? '' : 'stale'}`} style=${`left:${pct(d)}`} title="The pod"></div>`}
      </div>
    </div>
    <div class="walkside">
      <div class="bigline"><b>${pose ? d.toFixed(1) : '—'}</b><span>of ${len.toFixed(0)} m walked</span></div>
      <div class="bar"><i style=${`width:${Math.round((d / len) * 100)}%`}></i></div>
      <div class="lbl small">${!pose || podStale ? 'No signal from the pod'
        : pose.src === 'imu' ? `IMU · ${pose.steps ?? 0} steps${hd != null ? ` · heading ${Math.round(hd)}°` : ''}`
        : `Position from ${pose.src}`}</div>
    </div>
  </section>`;
}

// The numbers under the pictures on the live site.
function Readings({ meta, s, st, m, bt, fresh }) {
  const rate = (t) => st.rates?.[t] ?? 0;
  const recent = Object.values(s.obs).filter((o) => bt - o.t < 30000);
  const beacons = recent.filter((o) => o.kind === 'ble' && o.cls !== 'apple_other' && o.cls !== 'other');
  const strongest = [...beacons].sort((a, b) => b.rssi - a.rssi)[0];
  const al = s.latest['ag/alert'];
  const band = meta.thermal ? [meta.thermal.hot_min_c, meta.thermal.hot_max_c] : null;
  return html`<section class="readings">
    <div><span>Warmest spot</span><b class=${fresh?.hot ? 'warmtxt' : ''}>${fresh && typeof fresh.max_c === 'number' ? html`${fresh.max_c.toFixed(1)}<small>°C</small>` : '—'}</b>
      <${Sparkline} hist=${m.thHist} lo=${band?.[0] - 4} hi=${band?.[0] + 4} band=${band} /></div>
    <div><span>Bluetooth scans</span><b>${rate('ag/obs/ble').toFixed(1)}<small>/s</small></b><span class="sub">passive · addresses hashed</span></div>
    <div><span>Lost devices heard</span><b>${beacons.length}</b><span class="sub">of ${recent.length} heard · 30 s</span></div>
    <div><span>Strongest</span><b>${strongest ? html`${strongest.rssi}<small>dBm</small>` : '—'}</b><span class="sub">${strongest ? CLS_SHORT[strongest.cls] || strongest.cls : 'nothing heard'}</span></div>
    <div><span>Last LoRa alert</span><b>${m.lastAlertWall > 0 ? fmtAgo(Date.now() - m.lastAlertWall) : '—'}</b><span class="sub">${al?.rssi != null ? `${al.rssi} dBm at the Pluto` : 'none heard yet'}</span></div>
  </section>`;
}

// The building, north up, in local metres: outline, the taped park, waypoints, the pod and the pings.
function BuildingMap({ meta, s, pose, podStale }) {
  const ring = meta.building?.ring;
  const zones = meta.map?.zones || [], lines = meta.map?.lines || [];
  const [zoom, setZoom] = useState(() => loadPref('bzoom', 'building'));
  useEffect(() => savePref('bzoom', zoom), [zoom]);
  const near = zoom === 'area' || !ring; // the demo area: the taped park, waypoints, pod and pings
  const pts = near ? [...zones.flatMap((z) => z.ring), ...Object.values(meta.waypoints || {}),
                      ...(pose ? [[pose.x, pose.y]] : [])]
                   : [...ring, ...zones.flatMap((z) => z.ring), ...Object.values(meta.waypoints || {})];
  if (!pts.length) for (const v of [meta.map?.bounds]) if (v) pts.push([v[0], v[1]], [v[2], v[3]]);
  if (!pts.length) return html`<div class="lbl">No map for this profile.</div>`;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  let [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const pad = Math.max(x1 - x0, y1 - y0) * 0.06;
  x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;
  const W = x1 - x0, H = y1 - y0, u = Math.max(W, H) / 60; // u: a "unit" for marks and text, in metres
  const P = ([x, y]) => `${x.toFixed(2)},${(-y).toFixed(2)}`; // north up: SVG y grows down
  const poly = (r) => r.map(P).join(' ');
  const trail = (s.trails.pod || []).slice(-300);
  const pins = Object.values(s.pins).filter((p) => p.status !== 'cleared');
  const scale = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000].find((v) => v >= W / 6) || 1000;
  return html`<div class="bmapbox">
  ${ring && html`<span class="seg bzoom" role="group" aria-label="Map zoom">
    <button class=${!near ? 'on' : ''} aria-pressed=${!near} onClick=${() => setZoom('building')}>Building</button>
    <button class=${near ? 'on' : ''} aria-pressed=${near} onClick=${() => setZoom('area')}>Demo area</button></span>`}
  <svg class="bmap" viewBox=${`${x0} ${-y1} ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Building map with the pod">
    ${ring && html`<polygon class="b-bldg" points=${poly(ring)}></polygon>`}
    ${zones.map((z) => html`<polygon key=${z.id} class=${`b-zone b-${z.kind}`} points=${poly(z.ring)}></polygon>`)}
    ${lines.map((l) => html`<polyline key=${l.id} class="b-trail" points=${poly(l.pts)}></polyline>`)}
    ${Object.entries(meta.waypoints || {}).map(([n, [x, y]]) => html`<g key=${`w${n}`} class="b-wp">
      <circle cx=${x} cy=${-y} r=${u * 1.2}></circle><text x=${x} y=${-y + u * 0.55} font-size=${u * 1.5} text-anchor="middle">${n}</text></g>`)}
    ${trail.length > 1 && html`<polyline class="b-path" points=${trail.map(P).join(' ')}></polyline>`}
    ${pins.map((p) => html`<g key=${p.pin}>
      <circle cx=${p.x} cy=${-p.y} r=${Math.max(p.r95 || 0, u)} class="b-pinr" style=${`stroke:${prioColor(p.prio)}`}></circle>
      <circle cx=${p.x} cy=${-p.y} r=${u * 0.8} style=${`fill:${prioColor(p.prio)}`}></circle>
      <text x=${p.x + u * 1.2} y=${-p.y - u * 0.8} font-size=${u * 1.5} class="b-lbl">${p.pin}</text></g>`)}
    ${pose && html`<g class=${`b-pod ${podStale ? 'stale' : ''}`}><circle cx=${pose.x} cy=${-pose.y} r=${u * 2.2} class="halo"></circle><circle cx=${pose.x} cy=${-pose.y} r=${u * 1.1}></circle></g>`}
    ${ring && !near && meta.building?.name && html`<text x=${x0 + u} y=${-y1 + u * 2.2} font-size=${u * 1.7} class="b-lbl">${meta.building.name}</text>`}
    <g class="b-scale"><line x1=${x1 - u - scale} y1=${-y0 - u} x2=${x1 - u} y2=${-y0 - u}></line>
      <text x=${x1 - u - scale / 2} y=${-y0 - u * 1.6} font-size=${u * 1.3} text-anchor="middle">${scale} m</text></g>
    <g class="b-north"><path d=${`M${x1 - u * 2} ${-y1 + u * 1} l${u} ${u * 2.6} l${-u} ${-u * 0.8} l${-u} ${u * 0.8} z`}></path>
      <text x=${x1 - u * 2} y=${-y1 + u * 5} font-size=${u * 1.3} text-anchor="middle">N</text></g>
  </svg></div>`;
}

// On the map, outside Emergency (which has its own Camera check card). "Auto": both pictures while
// the drone checks a ping, the heat picture alone in a night sweep, nothing otherwise.
const FEED_CHOICES = [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']];
function MapFeeds({ s, bt, which }) {
  const th = s.latest['ag/thermal'];
  return html`<div class="mapfeeds" aria-label="Live pictures from the drone">
    ${which.includes('camera') && html`<${LiveFeed} key="camera" file="camera.jpg" name="Camera" />`}
    ${which.includes('heat') && html`<${LiveFeed} key="heat" file="thermal.png" name="Heat" tag=${heatText(th, bt)} warm=${!!th?.hot && !!heatText(th, bt)} />`}
  </div>`;
}

function RescueCard({ p, rescue, kase, locked, busy, pinCmd, confirmRescue, notPerson, m, bt, teamOnWay, routeP, backToSweep }) {
  const b = busy[p.pin];
  const r = rescue;
  const sent = !!(kase?.team_t || m.teamPins?.[p.pin]);
  return html`<section class="p" style="box-shadow:inset 3px 0 0 var(--text)">
    ${routeP && html`<button class="btn primary big" onClick=${backToSweep}>← Back to the sweep</button>
      <div class="lbl small" style="text-align:center">The drone is still sweeping. This view changes nothing (Esc also leaves it).</div>`}
    <h2 class="h">Rescue route · ${p.pin}${kase?.ping === p.pin ? ` · ${kase.name}` : ''}</h2>
    <div class="num" style="font-size:1.45rem">${p.lat != null ? `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}` : `${Math.round(p.x)} E, ${Math.round(p.y)} N (local m)`}</div>
    ${r && html`<ol class="route">
      <li>Start at <b>${r.start.label}</b></li>
      ${r.trail && r.onTrail > 5 && html`<li>Walk <b>${fmtM(r.onTrail)}</b> along ${r.trail}</li>`}
      <li>Leave ${r.trail ? 'the trail' : 'the start'} heading <b>${r.dir}</b>, <b>${fmtM(r.off)}</b> through the woods</li>
    </ol>
    <div class="row"><span class="lbl">On foot</span><span class="num" style="font-size:1.3rem">~${fmtDur(r.secs * 1000)}</span></div>`}
    ${p.status === 'investigating' ? html`<${CheckPanel} p=${p} c=${m.checks[p.pin]} bt=${bt} locked=${locked} b=${b} confirmRescue=${confirmRescue} notPerson=${notPerson} />`
    : p.status === 'confirmed' ? (sent
      ? html`<div style="color:var(--moss);font-weight:700;text-align:center">Team on the way · sent ${fmtAgo(Date.now() - (kase?.team_t || Date.now()))}</div>
        ${!routeP && html`<button class="btn big" disabled=${locked} onClick=${backToSweep}>← Back to the sweep</button>`}`
      : html`<div style="color:var(--moss);font-weight:700;text-align:center">Person confirmed · the drone stays over them</div>
        <button class="btn primary big" disabled=${locked} onClick=${() => teamOnWay(p.pin)}>Team on the way · back to scanning</button>`)
    : p.status === 'cleared' ? html`<div class="lbl" style="text-align:center">Cleared</div>`
    : html`<button class="btn primary big" disabled=${locked || b} onClick=${() => pinCmd(p.pin, 'investigate', 'Investigate')}>Investigate</button>`}
  </section>`;
}

function LoraPings({ m }) {
  const rows = Object.values(m.alerts).sort((a, b) => (a.prio - b.prio) || (b.wall - a.wall));
  return html`<section class="p" style="box-shadow:inset 3px 0 0 var(--amber)">
    <h2 class="h">Pings from LoRa</h2>
    <div class="pinglist">${rows.map((a) => html`<div class="pingrow" key=${a.track}>
      <span class="chip" style=${`background:${prioColor(a.prio)}`}>T${a.track}</span>
      <span><div class="l1">${a.kind === 2 ? 'SOS' : CLS_SHORT[a.cls] || a.cls || 'Beacon'} · ±${fmtM(a.r95)}</div>
        <div class="l2">${a.sos ? `${a.sos.hurt ? 'hurt' : 'not hurt'}, ${a.sos.walk ? 'can walk' : "can't walk"} · ` : ''}alert ${fmtAgo(Date.now() - a.wall)}</div></span>
    </div>`)}</div>
  </section>`;
}

// ---------- the map: livemap.js as built, seen from straight above, with less on it ----------
// Its own extras (beacon dots, track circles, heatmap, labels, key, tools) are switched off through
// its public options, and we add the trail stretch, the car park, a clearer drone marker and a short key.
const MAP_LAYERS = { probability: false, trail: false, tracks: false, heard: false, roads: true, labels: false };
const SWEEP = new Set(['patrol', 'night']);

function MapPane({ meta, s, sel, follow, setFollow, night, cv, podStale, m, setSel, mode, target, rescue, checking, bt, live, sim }) {
  const host = useRef(null);
  const lm = useRef(null);
  const ex = useRef({ car: null, pod: null, hits: -1, fitKey: null });
  const [ready, setReady] = useState(false);
  const [base, setBase] = useState(() => loadPref('base', 'sat'));
  const [mapErr, setMapErr] = useState(null);
  const [feeds, setFeeds] = useState(() => loadPref('feeds', 'auto'));
  useEffect(() => savePref('feeds', feeds), [feeds]);
  const feedWhich = live || mode === 'investigate' || feeds === 'off' ? []
    : feeds === 'on' || checking ? ['camera', 'heat'] : night ? ['heat'] : [];

  useEffect(() => {
    try {
      lm.current = new LiveMap(host.current, meta, { onPick: (pin) => setSel(pin), status: false, key: false, tools: false, labels: false });
      window.tvMap = lm.current; // for poking at it from the browser console
      const map = lm.current.map;
      map.dragRotate.disable();
      map.touchZoomRotate.disableRotation();
      map.keyboard.disableRotation();
      map.once('load', () => { setupMap(lm.current); setReady(true); });
    } catch (e) { setMapErr(String(e.message || e)); }
    return () => { lm.current?.destroy(); lm.current = null; };
  }, []);

  const map = ready ? lm.current?.map : null;
  useEffect(() => { if (map) styleMap(map, night, base); savePref('base', base); }, [ready, night, base]);
  useEffect(() => { if (map) drawCase(lm.current, ex.current, cv); }, [ready, cv]);
  useEffect(() => { const p = sel && s.pins[sel]; if (p) lm.current?.flyTo(p.x, p.y); }, [sel]);

  if (map) {
    // what to show: the swept ground in the sweeps, the search area box, and frame the map on the job at hand
    const area = SWEEP.has(mode) ? meta.search.area : null;
    map.getSource('tv-area')?.setData(fc(area ? [box(lm.current, area)] : []));
    map.getSource('tv-rescue')?.setData(fc(rescue ? [lineF(lm.current, rescue.pts)] : []));
    const fitKey = `${mode}|${cv.case ? cv.route.length : 0}|${mode === 'investigate' ? target?.pin : ''}`;
    if (fitKey !== ex.current.fitKey) { ex.current.fitKey = fitKey; frame(lm.current, meta, mode, cv, target, s, rescue); }
    // the sim's lawn-mower lanes, faint, while it sweeps (not on the way home), and where base is
    const plan = sim?.plan?.length > 1 && SWEEP.has(mode) && sim.flight !== 'returning' ? sim.plan : null;
    if (plan !== ex.current.plan) { ex.current.plan = plan; map.getSource('tv-plan')?.setData(fc(plan ? [lineF(lm.current, plan)] : [])); }
    if (sim?.base && !ex.current.baseMark) {
      const el = Object.assign(document.createElement('div'), { className: 'basemark', textContent: 'H', title: `Base · ${sim.base.name}` });
      ex.current.baseMark = new maplibregl.Marker({ element: el }).setLngLat(lm.current.toLL(sim.base.x, sim.base.y)).addTo(map);
    }
    const hits = m.binsHit ? m.binsHit.reduce((a, v) => a + v, 0) : 0;
    if (hits !== ex.current.hits) { ex.current.hits = hits; map.getSource('tv-flown')?.setData(fc(flownParts(lm.current, m))); }
    const pose = s.poses.pod || Object.values(s.poses)[0];
    if (pose) {
      if (!ex.current.pod) ex.current.pod = new maplibregl.Marker({ element: Object.assign(document.createElement('div'), { className: 'podmark', title: 'Drone' }) }).setLngLat(lm.current.toLL(pose.x, pose.y)).addTo(map);
      ex.current.pod.setLngLat(lm.current.toLL(pose.x, pose.y));
      ex.current.pod.getElement().classList.toggle('stale', podStale);
    }
    // the field teams' phones (TAK), by callsign; faded after a minute without a position
    const teams = s.teams || {};
    ex.current.teams ||= {};
    for (const [uid, t] of Object.entries(teams)) {
      let mk = ex.current.teams[uid];
      if (!mk) {
        const el = Object.assign(document.createElement('div'), { className: 'teammark', title: 'Field team (TAK)' });
        el.innerHTML = '<i></i><span></span>';
        mk = ex.current.teams[uid] = new maplibregl.Marker({ element: el }).setLngLat(lm.current.toLL(t.x, t.y)).addTo(map);
      }
      mk.setLngLat(lm.current.toLL(t.x, t.y));
      mk.getElement().querySelector('span').textContent = t.callsign;
      mk.getElement().classList.toggle('stale', Date.now() - t.t > 60000);
    }
    for (const uid of Object.keys(ex.current.teams)) if (!teams[uid]) { ex.current.teams[uid].remove(); delete ex.current.teams[uid]; }
  }
  // the map gets the same state, but only the pings worth seeing: heard-only and cleared ones stay in the list
  if (!ex.current.view || ex.current.base !== s) { ex.current.base = s; ex.current.view = Object.create(s); }
  ex.current.view.pins = Object.fromEntries(Object.entries(s.pins).filter(([k, p]) =>
    k === sel || p.status === 'investigating' || p.status === 'confirmed' || (p.status !== 'cleared' && p.prio < 4)));
  lm.current?.update(ex.current.view, { sel, follow, layers: { ...MAP_LAYERS, coverage: SWEEP.has(mode) } });

  return html`<div class="mapbox">
    <div class="lmhost" ref=${host}></div>
    ${mapErr && html`<div class="boot">Map failed to start: ${mapErr}</div>`}
    <div class="maptag">
      <span class="seg" role="group" aria-label="Map imagery">
        <button class=${base === 'sat' ? 'on' : ''} aria-pressed=${base === 'sat'} onClick=${() => setBase('sat')}>Satellite</button>
        <button class=${base === 'topo' ? 'on' : ''} aria-pressed=${base === 'topo'} onClick=${() => setBase('topo')}>Map</button>
      </span>
      <button class=${follow ? 'on' : ''} aria-pressed=${follow} onClick=${() => setFollow(!follow)}>Follow drone</button>
      <button onClick=${() => lm.current && frame(lm.current, meta, mode, cv, target, s, rescue)}>Fit</button>
      ${mode !== 'investigate' && html`<span class="seg" role="group" aria-label="Camera and heat pictures">
        <span class="seglbl">Pictures</span>
        ${FEED_CHOICES.map(([k, label]) => html`<button key=${k} class=${feeds === k ? 'on' : ''} aria-pressed=${feeds === k} onClick=${() => setFeeds(k)}>${label}</button>`)}
      </span>`}
      ${podStale && m.lastPoseWall > 0 && html`<span class="stale">Drone last seen ${fmtAgo(Date.now() - m.lastPoseWall)}</span>`}
    </div>
    <${MapKey} mode=${mode} kase=${cv.case} rescue=${!!rescue} sim=${sim} teams=${Object.keys(s.teams || {}).length > 0} />
    ${feedWhich.length > 0 && html`<${MapFeeds} s=${s} bt=${bt} which=${feedWhich} />`}
  </div>`;
}

function MapKey({ mode, kase, rescue, sim, teams }) {
  const rows = [['pod', 'Drone and what it can hear']];
  if (sim?.base) rows.push(['base', 'Base: where it waits']);
  if (SWEEP.has(mode)) rows.push(['cov', 'Ground already searched'], ['area', 'Search area']);
  if (SWEEP.has(mode) && sim?.plan?.length > 1 && sim.flight !== 'returning') rows.push(['plan', 'Planned sweep (lanes)']);
  if (mode === 'search' && kase) rows.push(['route', 'Trail stretch to search'], ['flown', 'Searched so far'], ['car', 'Where the car is']);
  if (rescue) rows.push(['rescue', 'Route for the ground team']);
  if (teams) rows.push(['team', 'Field team (TAK phone)']);
  rows.push(['ping', 'Ping: colour shows how urgent']);
  return html`<div class="mapkey" aria-label="Map key">${rows.map(([k, t]) => html`<div key=${k}><i class=${`k-${k}`}>${k === 'car' ? 'P' : k === 'ping' ? 'P1' : k === 'base' ? 'H' : ''}</i>${t}</div>`)}</div>`;
}

const fc = (f) => ({ type: 'FeatureCollection', features: f || [] });
const lineF = (live, pts) => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: pts.map(([x, y]) => live.toLL(x, y)) } });
const box = (live, [x0, y0, x1, y1]) => lineF(live, [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]);

function setupMap(live) {
  const map = live.map;
  // straight down, flat: no terrain tilt, no rotation
  map.setTerrain(null);
  live.exag = 0;
  map.setMaxPitch(0);
  map.jumpTo({ pitch: 0, bearing: 0 });
  for (const id of ['drone', 'hill']) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', 'none');
  if (map.getLayer('foot')) map.setPaintProperty('foot', 'fill-opacity', 0.1);
  const before = map.getLayer('tracks') ? 'tracks' : undefined;
  for (const id of ['tv-plan', 'tv-area', 'tv-route', 'tv-flown', 'tv-rescue']) map.addSource(id, { type: 'geojson', data: fc() });
  map.addLayer({ id: 'tv-plan', type: 'line', source: 'tv-plan', layout: { 'line-join': 'round' }, paint: { 'line-color': '#9fd8ff', 'line-width': 1.5, 'line-opacity': 0.55, 'line-dasharray': [2, 3] } }, before);
  map.addLayer({ id: 'tv-area', type: 'line', source: 'tv-area', paint: { 'line-color': '#e6eadf', 'line-width': 2, 'line-opacity': 0.7, 'line-dasharray': [3, 2] } }, before);
  map.addLayer({ id: 'tv-route-glow', type: 'line', source: 'tv-route', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#e0a43a', 'line-width': 16, 'line-opacity': 0.35, 'line-blur': 4 } }, before);
  map.addLayer({ id: 'tv-route', type: 'line', source: 'tv-route', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#f2c26b', 'line-width': 4 } }, before);
  map.addLayer({ id: 'tv-rescue', type: 'line', source: 'tv-rescue', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffffff', 'line-width': 4, 'line-dasharray': [1.5, 1.2] } }, before);
  map.addLayer({ id: 'tv-flown', type: 'line', source: 'tv-flown', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#7fe08a', 'line-width': 7 } }, before);
}

function styleMap(map, night, base) {
  const c = night ? FOREST.night : FOREST.day;
  const set = (id, prop, v) => { if (map.getLayer(id)) map.setPaintProperty(id, prop, v); };
  const vis = (id, on) => { if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none'); };
  vis('sat', base === 'sat');
  vis('topo', base === 'topo');
  set('sat', 'raster-brightness-max', night ? 0.5 : 0.95);
  set('topo', 'raster-brightness-max', night ? 0.5 : 0.95);
  set('bg', 'background-color', c.bg);
  set('zones-fill', 'fill-color', c.camp);
  set('zones-line', 'line-color', ['match', ['get', 'kind'], 'park', c.park, c.camp]);
  set('lines-road', 'line-color', c.road);
  set('lines-casing', 'line-color', c.casing);
}

function drawCase(live, ex, cv) {
  const map = live.map;
  const route = cv.case && cv.route?.length > 1 ? cv.route : [];
  map.getSource('tv-route')?.setData(fc(route.length ? [lineF(live, route)] : []));
  ex.car?.remove();
  ex.car = null;
  if (cv.case && cv.start) {
    const el = Object.assign(document.createElement('div'), { className: 'carmark', textContent: cv.case.car_park ? 'P' : 'E', title: cv.start.label });
    ex.car = new maplibregl.Marker({ element: el }).setLngLat(live.toLL(cv.start.x, cv.start.y)).addTo(map);
  }
}

function flownParts(live, m) { // the 10 m pieces of the stretch already searched, as lines
  const out = [];
  let run = [];
  m.bins.forEach((b, i) => {
    if (m.binsHit[i]) run.push(b);
    else { if (run.length > 1) out.push(lineF(live, run)); run = []; }
  });
  if (run.length > 1) out.push(lineF(live, run));
  return out;
}

function frame(live, meta, mode, cv, target, s, rescue) { // zoom to the job at hand
  let pts;
  if (mode === 'search' && cv.case && cv.route.length > 1) pts = [...cv.route, [cv.start.x, cv.start.y]];
  else if (mode === 'investigate' && rescue) pts = rescue.pts;
  else if (mode === 'investigate' && target) {
    const pose = s.poses.pod || Object.values(s.poses)[0];
    pts = [[target.x, target.y], ...(pose ? [[pose.x, pose.y]] : [])];
  } else { const [x0, y0, x1, y1] = meta.search.area; pts = [[x0, y0], [x1, y1]]; }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const padM = Math.max(40, 0.15 * Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)));
  live.map.fitBounds([live.toLL(Math.min(...xs) - padM, Math.min(...ys) - padM), live.toLL(Math.max(...xs) + padM, Math.max(...ys) + padM)],
    { padding: 40, duration: 900, maxZoom: live.big ? 18 : 22 });
}

// ---------- the case form ----------
function CaseForm({ meta, init, editing, save, cancel }) {
  const [v, setV] = useState({ name: '', age: '', details: '', carrying: 'iphone', car_park: '', ...init });
  const [problems, setProblems] = useState(null);
  const [saving, setSaving] = useState(false);
  const first = useRef(null);
  useEffect(() => { first.current?.focus(); }, []);
  const f = (k) => (e) => setV({ ...v, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    if (!v.name.trim()) { setProblems(['A name is needed.']); return; }
    setSaving(true);
    const p = await save({ name: v.name, age: v.age, details: v.details, carrying: v.carrying, car_park: v.car_park }, !editing);
    setSaving(false);
    if (p) setProblems(p);
  };
  return html`<div class="overlay" role="dialog" aria-modal="true" aria-labelledby="case-title" onKeyDown=${(e) => e.key === 'Escape' && cancel()}>
    <form class="form" onSubmit=${submit}>
      <h2 id="case-title">${editing ? 'Edit the case' : 'Open a case'}</h2>
      <div class="lbl">Kept on this laptop only. Never sent over the radio or to the drone.</div>
      <div class="two">
        <label>Name<input ref=${first} value=${v.name} onInput=${f('name')} maxlength="80" required autocomplete="off" /></label>
        <label>Age<input value=${v.age} onInput=${f('age')} maxlength="12" inputmode="numeric" autocomplete="off" /></label>
      </div>
      <label>What they look like (clothing, colours, anything identifying)<textarea value=${v.details} onInput=${f('details')} maxlength="400"></textarea></label>
      <div class="two">
        <label>Car found at<select value=${v.car_park} onChange=${f('car_park')}>
          <option value="">Not found: start from the park entrance</option>
          ${(meta.search?.car_parks || []).map((cp) => html`<option value=${cp.name}>${cp.name}</option>`)}
        </select></label>
        <label>Carrying<select value=${v.carrying} onChange=${f('carrying')}>${CARRY.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select></label>
      </div>
      ${problems && html`<div class="err" role="alert">${problems.join(' ')}</div>`}
      <div class="btns even">
        <button type="button" class="btn big" onClick=${cancel}>Cancel</button>
        <button type="submit" class="btn primary big" disabled=${saving}>${editing ? 'Save' : 'Save and start trail pathing'}</button>
      </div>
    </form>
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
