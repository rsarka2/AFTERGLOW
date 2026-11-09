# Vendored front-end libraries

Saved here so the dashboard runs with no internet and no CDN. Don't upgrade during the hackathon.

| Folder | Package | Source | License |
|---|---|---|---|
| `htm-3.1.1/` | htm 3.1.1, `preact/standalone.module.js` (bundles Preact 10 + hooks) | `cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js` | Apache-2.0 (Preact is MIT) |
| `maplibre-gl-4.7.1/` | MapLibre GL JS 4.7.1, `dist/maplibre-gl.js`, `maplibre-gl.css` (the TV's map, `livemap.js`) | `cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/` | BSD-3-Clause |
| `ibm-plex/` | IBM Plex Sans 400/500/600/700 and IBM Plex Mono 400/500/600, Latin woff2 (the operator TV, `tv.css`) | npm `@fontsource/ibm-plex-sans@5.1.1`, `@fontsource/ibm-plex-mono@5.1.1` | SIL OFL 1.1 |

## Use

```js
import { html, render, useState, useEffect, useRef } from './vendor/htm-3.1.1/standalone.module.js';
```

The bundle has no imports of its own. It exports `h`, `html`, `render`, `Component`, `createContext` and the hooks (`useState`, `useReducer`, `useEffect`, `useLayoutEffect`, `useRef`, `useMemo`, `useCallback`, `useContext`, …).

## SHA-256

```
72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7  htm-3.1.1/standalone.module.js
be9633c4d870e26fb37f1cfe5c5a77181667114003ea16207ac7850d8da8add1  maplibre-gl-4.7.1/maplibre-gl.js
576b085fdd9487a65a19215328c1e086c07ce5bf6da09b666b3806d3d008dae9  maplibre-gl-4.7.1/maplibre-gl.css
3c5a451f9ec27a354b0c2bcca636c6ec17a651281aabf29f8427e210a1d31e85  ibm-plex/ibm-plex-mono-latin-400-normal.woff2
756026ff72eb76fd971ac4b7504cec55eef62109d2684c2cad8da32170b80b37  ibm-plex/ibm-plex-mono-latin-500-normal.woff2
c4d3deb734a27e6d0dc7a6b464779f70ba1c272e26287860a14e35e85acb5b76  ibm-plex/ibm-plex-mono-latin-600-normal.woff2
db71f8a28ad8501544fb4e7668e3c6d0b731760b6f20de3525ebaeba597f1922  ibm-plex/ibm-plex-sans-latin-400-normal.woff2
5ef914e59b0047a261844d96acabb60c34d3acab6b85ea24198726ce4781fd37  ibm-plex/ibm-plex-sans-latin-500-normal.woff2
31535a91ce3f6b8ed3ddedadab1e49957e2220263a640df1a3f14f6fdfe15eb6  ibm-plex/ibm-plex-sans-latin-600-normal.woff2
fff71a83690454ee6ea9014780a6797408918cb90cde1f0f3be65ea28a03c678  ibm-plex/ibm-plex-sans-latin-700-normal.woff2
```
