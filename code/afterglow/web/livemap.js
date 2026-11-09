// AFTERGLOW live map feed: the bus drawn over 3D terrain, with labels on the map and a built-in key.
// The operator TV's map (tv.js):
//
//   import { LiveMap } from './livemap.js';
//   const m = new LiveMap(el, meta, { onPick: (pin) => select(pin) });   // meta = GET /api/map
//   m.update(store.state, { sel, layers, follow });                       // on every store change
//
// docs/LIVEMAP.md explains every symbol. It only draws: no buttons, no commands. The ranger acts on the TV.
// Needs window.maplibregl (vendor/maplibre-gl-4.7.1). Terrain and basemaps load from the internet;
// offline it still draws the trails, pod, tracks and pins on a plain background.

export const PRIO_COLOR = { 1: '#e5484d', 2: '#f76b15', 3: '#f5c13d', 4: '#8b95a3' }; // red, orange, yellow, grey
const STATE_COLOR = { heard: '#8b95a3', localized: '#f5c13d', pinned: '#f76b15', confirmed: '#e5484d' };
const CLS = { findmy: 'Find My', findhub: 'Find Hub', samsung_off: 'Samsung offline', ref: 'Ref tag', wifi: 'Wi-Fi SOS', apple_other: 'Apple (live)', other: 'Other' };
const POD = '#3db9ff';
const M_PER_DEG = 111320, D2R = Math.PI / 180;
const DETS_MAX = 800, MAP_EVERY_MS = 200;
const LIVE_SRC = new Set(['gps', 'waypoint', 'imu']); // the real pod (imu: the indoor walk, position.py)
const fc = (f) => ({ type: 'FeatureCollection', features: f || [] });
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtM = (v) => (v == null || !isFinite(v) ? '?' : Math.abs(v) >= 100 ? `${Math.round(v)} m` : `${v.toFixed(1)} m`);

const KEY = [
  ['line', '#c9a36a', 'Trail', 'From the profile\'s trails GeoJSON'],
  ['line', '#7d838c', 'Road'],
  ['dash', '#d6912f', 'Campground (pins wait until clear of it)'],
  ['line', POD, 'Pod path: where the pod has been (ag/pose)'],
  ['ring', POD, 'Hearing radius around the pod ([locate] detect_m)'],
  ['dot', 'linear-gradient(90deg,#3b6bd6,#f5c13d,#e5484d)', 'Beacon heard here: weak → strong (ag/obs/ble at the pod\'s position)'],
  ['box', 'rgba(61,185,255,.45)', 'Ground already covered (ag/coverage)'],
  ['box', 'linear-gradient(90deg,#f5c13d,#e5484d)', 'Where a beacon probably is (ag/coverage p)'],
  ['dash', '#f5c13d', 'Track: tracker\'s 95% circle for one beacon (ag/track); grey = heard only'],
  ['pin', PRIO_COLOR[1], 'Pin P# = possible person for the ranger (ag/target). Colour = priority 1 red … 4 grey'],
  ['dash', '#d65bd6', 'Proposed leg from the planner (ag/plan), waiting for the ranger'],
];

const CSS = `
.lm-wrap{position:relative;width:100%;height:100%;min-height:300px;overflow:hidden;background:#1a2433;font:12px/1.4 system-ui,Segoe UI,Arial,sans-serif;color:#e8eef8}
.lm-map{position:absolute;inset:0}
.lm-box{position:absolute;z-index:3;background:rgba(11,18,32,.9);border:1px solid #2a3a54;border-radius:8px;padding:6px 9px}
.lm-status{top:8px;left:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-width:calc(100% - 70px)}
.lm-badge{border-radius:20px;padding:2px 8px;font-size:11px;font-weight:700;border:1px solid #2a3a54}
.lm-src-live{background:#1d4d33;border-color:#2f8f5c}.lm-src-sim{background:#4a3a12;border-color:#a8841f}.lm-src-replay{background:#2c2f5a;border-color:#5a60b8}.lm-src-none{background:#2a3445}
.lm-key{bottom:28px;left:8px;max-width:330px;max-height:calc(100% - 90px);overflow:auto}
.lm-key h4{margin:0;font-size:11px;letter-spacing:.5px;cursor:pointer;display:flex;justify-content:space-between;gap:10px}
.lm-key .lm-rows{display:grid;gap:3px;margin-top:6px;color:#c6d3e5}
.lm-key.lm-closed .lm-rows{display:none}
.lm-sym{display:inline-block;width:18px;margin-right:7px;vertical-align:middle}
.lm-tools{bottom:28px;right:8px;display:flex;flex-direction:column;gap:4px}
.lm-tools select,.lm-tools button{background:#20334d;color:#e8eef8;border:1px solid #38516f;border-radius:6px;font:inherit;font-size:11px;padding:3px 5px;cursor:pointer}
.lm-label{background:rgba(11,18,32,.88);border:1px solid #2a3a54;border-radius:6px;padding:1px 6px;font-size:11px;white-space:nowrap;color:#e8eef8;pointer-events:none;transform:translateY(-22px)}
.lm-pin{display:flex;align-items:center;gap:5px;cursor:pointer}
.lm-pin .lm-chip{min-width:24px;height:24px;padding:0 5px;border-radius:12px;border:2px solid #fff;color:#111;font:800 11px system-ui;display:grid;place-items:center;box-shadow:0 0 0 2px rgba(0,0,0,.45)}
.lm-pin .lm-txt{background:rgba(11,18,32,.9);border:1px solid #2a3a54;border-radius:6px;padding:1px 6px;font-size:11px;white-space:nowrap}
.lm-pin.lm-sel .lm-chip{outline:3px solid #3db9ff;outline-offset:2px}
.lm-pin.st-investigating .lm-chip,.lm-pin.st-confirmed .lm-chip{animation:lmring 1.1s infinite}
.lm-pin.st-confirmed .lm-chip{color:#fff}.lm-pin.st-cleared{opacity:.35}
@keyframes lmring{0%{box-shadow:0 0 0 0 rgba(229,72,77,.9)}100%{box-shadow:0 0 0 16px rgba(229,72,77,0)}}
.lm-wrap .maplibregl-popup-content{background:#121d30;color:#e8eef8;border:1px solid #2a3a54;font-size:12px;padding:6px 9px}
.lm-wrap .maplibregl-popup-tip{display:none}
`;
let cssDone = false;

// Terrain heights (Terrarium tiles), for the pod's height over the ground
class Dem {
  constructor(toLL) { this.toLL = toLL; this.z = 14; this.tiles = new Map(); this.ready = false; }
  tp(lon, lat) { const n = 2 ** this.z, lr = lat * D2R; return [(lon + 180) / 360 * n, (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n]; }
  async load(sw, ne) {
    const [x0, y0] = this.tp(sw[0], ne[1]), [x1, y1] = this.tp(ne[0], sw[1]), jobs = [];
    for (let x = Math.floor(x0); x <= Math.floor(x1); x++) for (let y = Math.floor(y0); y <= Math.floor(y1); y++) jobs.push([x, y]);
    if (jobs.length > 64) return; // a huge map: skip rather than hammer the tile server
    await Promise.all(jobs.map(([x, y]) => this.fetch(x, y)));
    this.ready = this.tiles.size > 0;
  }
  fetch(x, y) {
    return new Promise((res) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        const c = document.createElement('canvas'); c.width = c.height = 256;
        const g = c.getContext('2d'); g.drawImage(img, 0, 0);
        const d = g.getImageData(0, 0, 256, 256).data, h = new Float32Array(65536);
        for (let i = 0; i < 65536; i++) h[i] = d[i * 4] * 256 + d[i * 4 + 1] + d[i * 4 + 2] / 256 - 32768;
        this.tiles.set(`${x}/${y}`, h); res();
      };
      img.onerror = () => res();
      img.src = `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${this.z}/${x}/${y}.png`;
    });
  }
  px(x, y) { const tx = Math.floor(x / 256), ty = Math.floor(y / 256), t = this.tiles.get(`${tx}/${ty}`); return t ? t[(y - ty * 256) * 256 + (x - tx * 256)] : NaN; }
  at(xm, ym) {
    if (!this.ready) return null;
    const [lon, lat] = this.toLL(xm, ym), [fx, fy] = this.tp(lon, lat);
    const X = fx * 256 - 0.5, Y = fy * 256 - 0.5, x0 = Math.floor(X), y0 = Math.floor(Y), dx = X - x0, dy = Y - y0;
    const v = this.px(x0, y0) * (1 - dx) * (1 - dy) + this.px(x0 + 1, y0) * dx * (1 - dy) + this.px(x0, y0 + 1) * (1 - dx) * dy + this.px(x0 + 1, y0 + 1) * dx * dy;
    return isFinite(v) ? v : null;
  }
}

export class LiveMap {
  /**
   * @param el    the element to fill (give it a height)
   * @param meta  GET /api/map
   * @param opts  { onPick(pin), key: true | false | 'open' | 'closed', tools: true, status: true, labels: true }
   *              key: true folds the key on a small map; 'open' / 'closed' force it.
   */
  constructor(el, meta, opts = {}) {
    if (!window.maplibregl) throw new Error('livemap.js needs vendor/maplibre-gl-4.7.1/maplibre-gl.js loaded first');
    if (!cssDone) { const s = document.createElement('style'); s.textContent = CSS; document.head.appendChild(s); cssDone = true; }
    this.meta = meta;
    this.opts = { key: true, tools: true, status: true, labels: true, ...opts };
    const [lat0, lon0] = meta.origin, cosLat = Math.cos(lat0 * D2R);
    this.toLL = (x, y) => [lon0 + x / (M_PER_DEG * cosLat), lat0 + y / M_PER_DEG];
    const b = meta.map.bounds;
    this.big = Math.max(b[2] - b[0], b[3] - b[1]) > 200; // park scale vs the taped indoor park
    this.layers = { coverage: true, probability: true, trail: true, tracks: true, roads: true, heard: true, labels: this.opts.labels };
    this.dets = [];
    this.lastObs = null;
    this.stateRef = null;
    this.markers = new Map();   // "pin:P1" / "track:T2" / "pod" -> maplibregl.Marker
    this.lastDraw = 0;
    this.lastCam = 0;
    this.covKey = '';
    this.covT = null;
    this.canvas = document.createElement('canvas');

    el.classList.add('lm-wrap');
    el.innerHTML = '';
    const mapEl = document.createElement('div');
    mapEl.className = 'lm-map';
    el.appendChild(mapEl);
    this.el = el;

    const pad = Math.max(30, 0.15 * Math.max(b[2] - b[0], b[3] - b[1]));
    this.sw = this.toLL(b[0] - pad, b[1] - pad);
    this.ne = this.toLL(b[2] + pad, b[3] + pad);
    this.dem = new Dem(this.toLL);
    this.dem.load(this.sw, this.ne);
    this.makeMap(mapEl);
    this.makeOverlays();
  }

  // ---------- public API (what tv.js calls) ----------
  destroy() { this.ro?.disconnect(); this.map.remove(); this.el.innerHTML = ''; }
  fit() { this.map.fitBounds([this.sw, this.ne], { padding: 30 }); }
  flyTo(x, y) { this.map.easeTo({ center: this.toLL(x, y), zoom: Math.max(this.map.getZoom(), this.big ? 16 : 20), duration: 900 }); }
  setLayer(name, on) {
    this.layers[name] = on;
    if (!this.ready) return;
    const vis = (ids, v) => ids.forEach((id) => this.map.getLayer(id) && this.map.setLayoutProperty(id, 'visibility', v ? 'visible' : 'none'));
    vis(['crumbs'], this.layers.trail);
    vis(['tracks'], this.layers.tracks);
    vis(['dets'], this.layers.heard);
    vis(['lines-road'], this.layers.roads);
    if (name === 'coverage' || name === 'probability') { this.covT = null; this.drawCoverage(this.last?.latest['ag/coverage']); }
    if (name === 'labels' || name === 'tracks') this.drawMarkers(this.last || { pins: {}, tracks: {}, poses: {} });
  }

  /** Call with store.state whenever it changes. opts: { sel: 'P1', layers: {...}, follow: bool } */
  update(state, { sel = null, layers, follow = false } = {}) {
    if (layers) for (const [k, v] of Object.entries(layers)) if (this.layers[k] !== v) this.setLayer(k, v);
    if (state !== this.stateRef) { this.stateRef = state; this.lastObs = null; } // a fresh full state from the server
    this.last = state;
    this.sel = sel;
    const pose = state.poses.pod || Object.values(state.poses)[0] || null;
    this.collectDets(state, pose);
    this.drawStatus(state, pose);
    const now = performance.now();
    if (!this.ready || now - this.lastDraw < MAP_EVERY_MS) return;
    this.lastDraw = now;
    this.draw(state, pose);
    if (follow && pose && now - this.lastCam > 1000) { this.lastCam = now; this.map.easeTo({ center: this.toLL(pose.x, pose.y), duration: 900 }); }
  }

  // ---------- setup ----------
  makeMap(mapEl) {
    const terrarium = ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'];
    const map = (this.map = new maplibregl.Map({
      container: mapEl, bounds: [this.sw, this.ne], fitBoundsOptions: { padding: 30 }, pitch: this.big ? 55 : 0, maxPitch: 85, maxZoom: 22,
      style: {
        version: 8,
        sources: {
          topo: { type: 'raster', tiles: ['a', 'b', 'c'].map((s) => `https://${s}.tile.opentopomap.org/{z}/{x}/{y}.png`), tileSize: 256, maxzoom: 17, attribution: '© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)' },
          sat: { type: 'raster', tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 19, attribution: 'Imagery © Esri' },
          dem: { type: 'raster-dem', tiles: terrarium, tileSize: 256, maxzoom: 15, encoding: 'terrarium', attribution: 'Terrain: AWS Terrain Tiles' },
          demh: { type: 'raster-dem', tiles: terrarium, tileSize: 256, maxzoom: 15, encoding: 'terrarium' },
        },
        layers: [
          { id: 'bg', type: 'background', paint: { 'background-color': '#1a2433' } },
          { id: 'topo', type: 'raster', source: 'topo' },
          { id: 'sat', type: 'raster', source: 'sat', layout: { visibility: 'none' } },
          { id: 'hill', type: 'hillshade', source: 'demh', paint: { 'hillshade-exaggeration': 0.3 } },
        ],
        terrain: this.big ? { source: 'dem', exaggeration: 1.5 } : undefined,
      },
    }));
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    this.ro = new ResizeObserver(() => map.resize());
    this.ro.observe(mapEl);
    map.on('load', () => {
      for (const id of ['zones', 'lines', 'crumbs', 'dets', 'tracks', 'legs', 'foot', 'drone']) map.addSource(id, { type: 'geojson', data: fc() });
      map.addLayer({ id: 'zones-fill', type: 'fill', source: 'zones', filter: ['!=', ['get', 'kind'], 'park'], paint: { 'fill-color': '#d6912f', 'fill-opacity': 0.18 } });
      map.addLayer({ id: 'zones-line', type: 'line', source: 'zones', paint: { 'line-color': ['match', ['get', 'kind'], 'park', '#5f8f52', '#d6912f'], 'line-width': 1.5, 'line-dasharray': [3, 2] } });
      map.addLayer({ id: 'lines-casing', type: 'line', source: 'lines', paint: { 'line-color': '#0b1220', 'line-width': 5, 'line-opacity': 0.5 } });
      map.addLayer({ id: 'lines-road', type: 'line', source: 'lines', filter: ['==', ['get', 'kind'], 'road'], paint: { 'line-color': '#7d838c', 'line-width': 2.5 } });
      map.addLayer({ id: 'lines-trail', type: 'line', source: 'lines', filter: ['!=', ['get', 'kind'], 'road'], layout: { 'line-cap': 'round' }, paint: { 'line-color': '#c9a36a', 'line-width': 2.5 } });
      map.addLayer({ id: 'crumbs', type: 'line', source: 'crumbs', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': POD, 'line-width': 3 } });
      map.addLayer({ id: 'legs', type: 'line', source: 'legs', paint: { 'line-color': '#d65bd6', 'line-width': 3, 'line-dasharray': [2, 1.5] } });
      map.addLayer({ id: 'tracks', type: 'line', source: 'tracks', paint: { 'line-color': ['get', 'color'], 'line-width': 2, 'line-dasharray': [2, 1.5], 'line-opacity': ['get', 'op'] } });
      map.addLayer({ id: 'foot', type: 'fill', source: 'foot', paint: { 'fill-color': POD, 'fill-opacity': 0.12 } });
      map.addLayer({ id: 'foot-line', type: 'line', source: 'foot', paint: { 'line-color': POD, 'line-width': 1, 'line-opacity': 0.7 } });
      map.addLayer({ id: 'dets', type: 'circle', source: 'dets', paint: { 'circle-radius': 3.5, 'circle-stroke-width': 1, 'circle-stroke-color': '#0b1220',
        'circle-color': ['interpolate', ['linear'], ['get', 'rssi'], -100, '#3b6bd6', -80, '#f5c13d', -55, '#e5484d'] } });
      map.addLayer({ id: 'drone', type: 'fill-extrusion', source: 'drone', paint: { 'fill-extrusion-color': ['get', 'color'], 'fill-extrusion-base': ['get', 'base'], 'fill-extrusion-height': ['get', 'top'], 'fill-extrusion-opacity': 0.95 } });
      const mm = this.meta.map;
      map.getSource('lines').setData(fc(mm.lines.map((l) => ({ type: 'Feature', properties: { kind: l.kind, name: l.name }, geometry: { type: 'LineString', coordinates: l.pts.map(([x, y]) => this.toLL(x, y)) } }))));
      map.getSource('zones').setData(fc(mm.zones.map((z) => ({ type: 'Feature', properties: { kind: z.kind, name: z.name }, geometry: { type: 'Polygon', coordinates: [z.ring.map(([x, y]) => this.toLL(x, y))] } }))));
      if (mm.credit) map.getContainer().querySelector('.maplibregl-ctrl-attrib-inner')?.insertAdjacentText('afterbegin', `${mm.credit} | `);
      this.addHoverHelp();
      this.ready = true;
      for (const [k, v] of Object.entries(this.layers)) this.setLayer(k, v);
      if (this.last) { this.lastDraw = 0; this.update(this.last, { sel: this.sel }); }
    });
  }

  addHoverHelp() {
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
    const show = (e, html) => { this.map.getCanvas().style.cursor = 'help'; pop.setLngLat(e.lngLat).setHTML(html).addTo(this.map); };
    const hide = () => { this.map.getCanvas().style.cursor = ''; pop.remove(); };
    this.map.on('mousemove', 'dets', (e) => show(e, `<b>Beacon heard here</b><br>${e.features[0].properties.cls} at ${e.features[0].properties.rssi} dBm<br><span style="color:#9aabc2">where the pod was, not where the beacon is</span>`));
    this.map.on('mousemove', 'lines-trail', (e) => show(e, `<b>${esc(e.features[0].properties.name || 'Trail')}</b>`));
    this.map.on('mousemove', 'zones-fill', (e) => show(e, `<b>${esc(e.features[0].properties.name || 'Campground')}</b><br>campground: pins wait until a beacon is clearly outside it`));
    for (const id of ['dets', 'lines-trail', 'zones-fill']) this.map.on('mouseleave', id, hide);
  }

  makeOverlays() {
    const o = this.opts;
    if (o.status) {
      this.statusEl = document.createElement('div');
      this.statusEl.className = 'lm-box lm-status';
      this.el.appendChild(this.statusEl);
    }
    if (o.key) {
      const k = document.createElement('div');
      k.className = 'lm-box lm-key';
      const sym = ([kind, color]) => {
        if (kind === 'line') return `<span class="lm-sym" style="height:4px;border-radius:2px;background:${color}"></span>`;
        if (kind === 'dash') return `<span class="lm-sym" style="height:0;border-top:2px dashed ${color}"></span>`;
        if (kind === 'ring') return `<span class="lm-sym" style="height:12px;width:12px;border-radius:50%;border:1.5px solid ${color};background:rgba(61,185,255,.15)"></span>`;
        if (kind === 'dot') return `<span class="lm-sym" style="height:8px;border-radius:4px;background:${color}"></span>`;
        if (kind === 'box') return `<span class="lm-sym" style="height:10px;background:${color}"></span>`;
        return `<span class="lm-sym" style="height:14px;width:22px;border-radius:7px;border:2px solid #fff;background:${color}"></span>`;
      };
      k.innerHTML = `<h4><span>KEY · what the map shows</span><span>▾</span></h4><div class="lm-rows">
        <div>${sym(['box', POD])}<b>Pod</b>: block at its height over the ground, label = height</div>
        ${KEY.map((r) => `<div>${sym(r)}${esc(r[2])}</div>`).join('')}
        <div style="color:#9aabc2;margin-top:3px">Labels: P# pins say beacon type · off-trail distance · status. T# tracks say ± radius. Hover a dot or trail for details.</div></div>`;
      k.querySelector('h4').onclick = () => k.classList.toggle('lm-closed');
      // Small map (a dashboard panel, a phone): start folded so the key doesn't hide the map
      if (o.key === 'closed' || (o.key !== 'open' && (this.el.clientHeight < 700 || this.el.clientWidth < 900))) k.classList.add('lm-closed');
      this.el.appendChild(k);
    }
    if (o.tools) {
      const t = document.createElement('div');
      t.className = 'lm-box lm-tools';
      t.innerHTML = `<select data-t="base"><option value="topo">Topo</option><option value="sat">Satellite</option><option value="none">No basemap</option></select>
        <select data-t="exag"><option value="1.5">Terrain 1.5×</option><option value="1">Terrain 1×</option><option value="2">Terrain 2×</option><option value="0">Flat</option></select>
        <button data-t="fit">Fit map</button>`;
      t.querySelector('[data-t=base]').onchange = (e) => {
        const v = e.target.value;
        this.map.setLayoutProperty('topo', 'visibility', v === 'topo' ? 'visible' : 'none');
        this.map.setLayoutProperty('sat', 'visibility', v === 'sat' ? 'visible' : 'none');
      };
      t.querySelector('[data-t=exag]').onchange = (e) => { const v = +e.target.value; this.exag = v; this.map.setTerrain(v ? { source: 'dem', exaggeration: v } : null); };
      t.querySelector('[data-t=fit]').onclick = () => this.fit();
      if (!this.big) t.querySelector('[data-t=exag]').value = '0';
      this.el.appendChild(t);
    }
    this.exag = this.big ? 1.5 : 0;
  }

  // ---------- per-update ----------
  collectDets(s, pose) {
    if (!pose || !s.obs_log?.length) return;
    const onPod = new Set(Object.values(s.tracks).filter((t) => t.with_drone).flatMap((t) => t.ids || []));
    for (const e of s.obs_log) {
      if (e === this.lastObs) break;
      if (e.topic === 'ag/obs/ble' && !onPod.has(e.msg.id)) this.dets.push({ x: pose.x, y: pose.y, rssi: e.msg.rssi, cls: CLS[e.msg.cls] || e.msg.cls });
    }
    this.lastObs = s.obs_log[0];
    if (this.dets.length > DETS_MAX) this.dets.splice(0, this.dets.length - DETS_MAX);
  }

  agl(p) { // with GPS altitude: alt_m minus terrain; without it (sim, waypoints): z as reported
    const g = this.dem.at(p.x, p.y);
    return typeof p.alt_m === 'number' && g != null ? p.alt_m - g : p.z || 0;
  }

  drawStatus(s, pose) {
    if (!this.statusEl) return;
    const src = !pose ? ['none', 'NO POSE YET'] : pose.src === 'sim' ? ['sim', 'SIMULATED'] : pose.src === 'replay' ? ['replay', 'REPLAY']
      : LIVE_SRC.has(pose.src) ? ['live', `LIVE · ${pose.src}`] : ['none', pose.src];
    const cov = s.latest['ag/coverage'], mode = s.latest['ag/mode']?.mode;
    const pins = Object.values(s.pins).filter((p) => p.status !== 'cleared').length;
    const html = `<span class="lm-badge lm-src-${src[0]}">${esc(src[1])}</span>
      <span>${esc(this.meta.name)}</span>
      <span>Mode <b>${esc(mode || 'patrol')}</b></span>
      <span>Covered <b>${cov ? Math.round(cov.pct * 100) + '%' : '—'}</b></span>
      <span>Open pins <b>${pins}</b></span>
      <span>${s.bus_t ? new Date(s.bus_t).toLocaleTimeString([], { hour12: false }) : '--:--:--'}</span>`;
    if (this.statusEl._h !== html) { this.statusEl.innerHTML = html; this.statusEl._h = html; }
  }

  draw(s, pose) {
    const m = this.map, L = this.toLL;
    const trail = (s.trails.pod || Object.values(s.trails)[0] || []).map(([x, y]) => L(x, y));
    m.getSource('crumbs').setData(fc(trail.length > 1 ? [{ type: 'Feature', geometry: { type: 'LineString', coordinates: trail } }] : []));
    m.getSource('dets').setData(fc(this.dets.map((d) => ({ type: 'Feature', properties: { rssi: d.rssi, cls: d.cls }, geometry: { type: 'Point', coordinates: L(d.x, d.y) } }))));
    m.getSource('tracks').setData(fc(Object.values(s.tracks).filter((t) => !t.with_drone).map((t) => ({ type: 'Feature',
      properties: { color: STATE_COLOR[t.state] || STATE_COLOR.heard, op: t.baseline ? 0.3 : 0.9 },
      geometry: { type: 'LineString', coordinates: this.circle(t.x, t.y, Math.max(0.5, t.r95)) } }))));
    const legs = [];
    for (const p of Object.values(s.plans || {})) {
      if (p.status !== 'proposed' || !pose) continue;
      const pts = [L(pose.x, pose.y)];
      for (const l of p.legs) if (typeof l.to_x === 'number') pts.push(L(l.to_x, l.to_y));
      if (pts.length > 1) legs.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: pts } });
    }
    m.getSource('legs').setData(fc(legs));
    if (pose) {
      const h = Math.max(0, this.agl(pose)) * (this.exag || 1), size = this.big ? 6 : 0.25;
      m.getSource('drone').setData(fc([
        { type: 'Feature', properties: { base: Math.max(0, h - size / 2), top: h + size / 2, color: POD }, geometry: { type: 'Polygon', coordinates: [this.square(pose.x, pose.y, size)] } },
        { type: 'Feature', properties: { base: 0, top: Math.max(0, h - size / 2), color: '#e8f8ff' }, geometry: { type: 'Polygon', coordinates: [this.square(pose.x, pose.y, size / 8)] } }]));
      m.getSource('foot').setData(fc([{ type: 'Feature', geometry: { type: 'Polygon', coordinates: [this.circle(pose.x, pose.y, this.meta.detect_m)] } }]));
    }
    this.drawCoverage(s.latest['ag/coverage']);
    this.drawMarkers(s, pose);
  }

  drawCoverage(cov) {
    if (!cov || !this.ready || cov.t === this.covT) return;
    this.covT = cov.t;
    const key = `${cov.x0},${cov.y0},${cov.cell_m},${cov.nx},${cov.ny}`;
    if (key !== this.covKey) {
      this.covKey = key;
      this.canvas.width = cov.nx; this.canvas.height = cov.ny;
      const x1 = cov.x0 + cov.nx * cov.cell_m, y1 = cov.y0 + cov.ny * cov.cell_m;
      if (this.map.getLayer('coverage')) this.map.removeLayer('coverage');
      if (this.map.getSource('coverage')) this.map.removeSource('coverage');
      this.map.addSource('coverage', { type: 'canvas', canvas: this.canvas, animate: true,
        coordinates: [this.toLL(cov.x0, y1), this.toLL(x1, y1), this.toLL(x1, cov.y0), this.toLL(cov.x0, cov.y0)] });
      this.map.addLayer({ id: 'coverage', type: 'raster', source: 'coverage', paint: { 'raster-opacity': 0.75, 'raster-fade-duration': 0, 'raster-resampling': 'nearest' } }, 'lines-casing');
    }
    const g = this.canvas.getContext('2d'), img = g.createImageData(cov.nx, cov.ny);
    for (let i = 0; i < cov.nx * cov.ny; i++) {
      const row = Math.floor(i / cov.nx), k = ((cov.ny - 1 - row) * cov.nx + (i % cov.nx)) * 4; // row 0 is the south edge
      const p = cov.p[i] / 255;
      if (this.layers.probability && p > 0.02) { img.data[k] = 245 - 16 * p; img.data[k + 1] = 193 - 121 * p; img.data[k + 2] = 61 + 16 * p; img.data[k + 3] = 60 + 170 * p; }
      else if (this.layers.coverage && cov.covered[i]) { img.data[k] = 61; img.data[k + 1] = 185; img.data[k + 2] = 255; img.data[k + 3] = 55; }
    }
    g.putImageData(img, 0, 0);
  }

  // Pins, track labels and the pod label are HTML markers (no font server needed)
  drawMarkers(s, pose = s.poses?.pod || Object.values(s.poses || {})[0]) {
    if (!this.ready) return;
    const want = new Set();
    const put = (key, x, y, make, fill) => {
      want.add(key);
      let mk = this.markers.get(key);
      if (!mk) { mk = new maplibregl.Marker({ element: make() }).setLngLat(this.toLL(x, y)).addTo(this.map); this.markers.set(key, mk); }
      mk.setLngLat(this.toLL(x, y));
      fill(mk.getElement().firstChild);
    };
    const wrap = (inner) => () => { const o = document.createElement('div'); o.appendChild(inner); return o; }; // MapLibre owns the outer element
    for (const p of Object.values(s.pins)) {
      put(`pin:${p.pin}`, p.x, p.y, () => {
        const d = document.createElement('div');
        d.innerHTML = '<span class="lm-chip"></span><span class="lm-txt"></span>';
        d.onclick = (e) => { e.stopPropagation(); this.opts.onPick?.(p.pin); };
        return wrap(d)();
      }, (d) => {
        d.className = `lm-pin st-${p.status} ${p.pin === this.sel ? 'lm-sel' : ''}`;
        const chip = d.firstChild, txt = d.lastChild;
        chip.style.background = PRIO_COLOR[p.prio] || PRIO_COLOR[4];
        chip.textContent = p.pin;
        txt.style.display = this.layers.labels ? '' : 'none';
        txt.textContent = `${CLS[p.cls] || p.cls}${p.off_trail_m != null ? ` · ${p.off_trail_m} m off trail` : ''} · ${p.status.toUpperCase()}`;
        d.title = `${p.pin}: ${CLS[p.cls] || p.cls}, ${p.state} ±${fmtM(p.r95)}, ${Math.round((p.c || 0) * 100)}% confidence, priority ${p.prio}, status ${p.status}`;
      });
    }
    const pinned = new Set(Object.values(s.pins).map((p) => p.track));
    if (this.layers.labels && this.layers.tracks) {
      for (const t of Object.values(s.tracks)) {
        // a pin already labels its own track; "heard only" tracks are too rough to label (their circle still shows)
        if (t.with_drone || t.baseline || pinned.has(t.track) || t.state === 'heard') continue;
        put(`track:${t.track}`, t.x, t.y, wrap(Object.assign(document.createElement('div'), { className: 'lm-label' })),
          (d) => { d.textContent = `${t.track} · ${CLS[t.cls] || t.cls} · ±${fmtM(t.r95)}`; d.style.color = STATE_COLOR[t.state]; });
      }
    }
    if (pose && this.layers.labels) {
      put('pod', pose.x, pose.y, wrap(Object.assign(document.createElement('div'), { className: 'lm-label' })),
        (d) => { d.textContent = `POD · ${Math.round(this.agl(pose))} m${typeof pose.alt_m === 'number' ? ' over ground' : ' up'}`; d.style.color = POD; });
    }
    for (const [key, mk] of this.markers) if (!want.has(key)) { mk.remove(); this.markers.delete(key); }
  }

  circle(x, y, r, n = 40) { const out = []; for (let i = 0; i <= n; i++) { const a = (i / n) * 2 * Math.PI; out.push(this.toLL(x + r * Math.cos(a), y + r * Math.sin(a))); } return out; }
  square(x, y, s) { return [this.toLL(x - s, y - s), this.toLL(x + s, y - s), this.toLL(x + s, y + s), this.toLL(x - s, y + s), this.toLL(x - s, y - s)]; }
}

/** The whole feed in one call: fetch /api/map, connect to /api/events, keep the map updated. */
export async function mountLiveMap(el, opts = {}) {
  const { connect } = await import('./store.js');
  const meta = await (await fetch('/api/map')).json();
  const lm = new LiveMap(el, meta, opts);
  const store = connect({ trailStep: meta.trail_step_m, onChange: () => lm.update(store.state, { follow: opts.follow }) });
  return { map: lm, store, meta };
}
