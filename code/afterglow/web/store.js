// Live state from dashboard.py over Server-Sent Events.
// apply() mirrors State.apply() in dashboard.py: keep the two in step. Its ag/team part (the TAK field
// teams) mirrors TakState.apply() in tak/tak_dashboard.py instead.

const OBS_IDS = 300;
const OBS_LOG = 60;
const TRAIL_MAX = 4000;

export function emptyState() {
  return { poses: {}, trails: {}, pins: {}, tracks: {}, plans: {}, teams: {}, team_log: [], obs: {}, obs_log: [],
           latest: {}, counts: {}, bus_t: 0, pose_seen: false, broker: false };
}

export function apply(s, topic, msg, trailStep) {
  s.counts[topic] = (s.counts[topic] || 0) + 1;
  // The clock is the latest pose's t; before any pose, the newest t (see dashboard.py).
  if (typeof msg.t === 'number') {
    if (topic === 'ag/pose') { s.bus_t = msg.t; s.pose_seen = true; }
    else if (!s.pose_seen && msg.t > s.bus_t) s.bus_t = msg.t;
  }
  if (topic === 'ag/pose') {
    const d = String(msg.drone ?? 'pod');
    s.poses[d] = msg;
    const trail = (s.trails[d] ||= []);
    if (typeof msg.x === 'number' && typeof msg.y === 'number') {
      const last = trail[trail.length - 1];
      if (!last || Math.hypot(msg.x - last[0], msg.y - last[1]) >= trailStep) {
        trail.push([msg.x, msg.y]);
        if (trail.length > TRAIL_MAX) trail.splice(0, trail.length - TRAIL_MAX);
      }
    }
  } else if (topic === 'ag/obs/ble' || topic === 'ag/obs/wifi') {
    let o = s.obs[msg.id];
    if (!o) {
      const ids = Object.keys(s.obs);
      if (ids.length >= OBS_IDS) { // make room first: drop the id silent the longest
        let oldest = ids[0];
        for (const k of ids) if ((s.obs[k].t || 0) < (s.obs[oldest].t || 0)) oldest = k;
        delete s.obs[oldest];
      }
      o = s.obs[msg.id] = { id: msg.id, n: 0, first: msg.t, best: -999 };
    }
    Object.assign(o, { kind: topic === 'ag/obs/wifi' ? 'wifi' : 'ble', cls: msg.cls ?? 'wifi',
                       rssi: msg.rssi, t: msg.t, src: msg.src, n: o.n + 1 });
    if (typeof msg.rssi === 'number' && msg.rssi > o.best) o.best = msg.rssi;
    s.obs_log.unshift({ topic, msg });
    if (s.obs_log.length > OBS_LOG) s.obs_log.length = OBS_LOG;
  } else if (topic === 'ag/target' && msg.pin) {
    s.pins[msg.pin] = msg;
  } else if (topic === 'ag/track' && msg.track) {
    s.tracks[msg.track] = msg;
  } else if (topic === 'ag/plan' && msg.id) {
    s.plans[msg.id] = msg;
  } else if (topic === 'ag/team' && msg.uid) { // the TAK phones: positions by uid, chat and reports newest first
    if (msg.kind === 'pos') (s.teams ||= {})[msg.uid] = msg;
    else { (s.team_log ||= []).unshift(msg); if (s.team_log.length > 40) s.team_log.length = 40; }
  } else {
    s.latest[topic] = msg;
  }
}

// Connects and keeps reconnecting (EventSource does that by itself). onChange is called
// at most once per animation frame, however fast messages arrive.
// on: optional extra SSE events, { name: fn(parsed data) } (tv.js uses "case").
export function connect({ trailStep, onChange, on = {} }) {
  const store = {
    state: emptyState(),
    link: 'connecting',      // 'live' once the full state has arrived
    status: null,            // the server's last "status" event
    lastMsgWall: 0,          // Date.now() of the last message
    rates: {},               // topic -> messages per second
  };
  let prevCounts = {}, prevWall = Date.now();
  store.sampleRates = () => {
    const now = Date.now(), dt = (now - prevWall) / 1000;
    if (dt <= 0) return;
    const rates = {};
    for (const [k, v] of Object.entries(store.state.counts)) rates[k] = Math.max(0, (v - (prevCounts[k] || 0)) / dt);
    store.rates = rates;
    prevCounts = { ...store.state.counts };
    prevWall = now;
  };

  let pending = false;
  const changed = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; onChange(); });
  };

  const es = new EventSource('/api/events');
  es.addEventListener('state', (e) => {
    store.state = JSON.parse(e.data);
    prevCounts = { ...store.state.counts };
    store.link = 'live';
    store.lastMsgWall = Date.now();
    changed();
  });
  es.addEventListener('msg', (e) => {
    const { topic, msg } = JSON.parse(e.data);
    apply(store.state, topic, msg, trailStep);
    store.lastMsgWall = Date.now();
    changed();
  });
  es.addEventListener('status', (e) => {
    store.status = JSON.parse(e.data);
    store.state.broker = store.status.broker;
    changed();
  });
  for (const [name, fn] of Object.entries(on)) es.addEventListener(name, (e) => { fn(JSON.parse(e.data)); changed(); });
  es.onerror = () => { store.link = 'reconnecting'; changed(); };
  return store;
}
