import * as THREE from "../vendor/three.module.min.js";
import { bereiche, gewichtText, karten, texte } from "./katalog.js";

const TAU = Math.PI * 2;
const KARTE = { breite: 2.1, hoehe: 3.4, platz: 1.22, dicke: 0.19, fase: 0.05, faseSchritte: 3, verdrehung: 0.33 };
const FLAECHE = { vorn: 0, fase: 1, wand: 2, hinten: 3 };
const ATLAS = { spalten: 10, zeilen: 11, px: 240, py: 300 };
const MAX_GEWICHT = Math.max(...bereiche.map((b) => b.gewicht));

const vertex = `
  uniform float uRotation;
  uniform float uRadius;
  uniform float uAnzahl;
  uniform float uGeschwindigkeit;
  uniform float uZeit;
  uniform float uHervor;
  uniform float uGewaehlt;
  uniform float uVerdrehung;
  uniform float uWahl;
  attribute float aIndex;
  attribute vec2 aKachel;
  attribute float aFlaeche;
  varying vec2 vUv;
  varying vec2 vKachel;
  varying float vTiefe;
  varying float vFokus;
  varying float vHervor;
  varying float vFlaeche;
  varying vec3 vNormaleAnsicht;
  varying vec3 vPosAnsicht;

  void main() {
    float winkel = aIndex / uAnzahl * 6.28318530718 + uRotation;
    float fokus = 0.5 + 0.5 * cos(winkel);
    float gewaehlt = step(abs(aIndex - uGewaehlt), 0.5);
    float hervor = max(step(abs(aIndex - uHervor), 0.5), gewaehlt);
    float skala = 1.0 + 0.18 * pow(fokus, 8.0) + 0.1 * hervor;
    vHervor = hervor;
    vec3 p = position * skala;

    float welle = sin(uv.y * 3.14159 + uZeit * 4.0 + aIndex * 0.35) * uGeschwindigkeit * 0.22;
    p.z += welle * (1.0 - fokus * 0.5);
    p.x += (uv.y - 0.5) * uGeschwindigkeit * 0.45;
    p.z += 0.3 * hervor + 0.5 * gewaehlt;

    float dreh = winkel + uVerdrehung * (1.0 - gewaehlt * uWahl);
    float s = sin(dreh);
    float c = cos(dreh);
    vec3 welt = vec3(p.x * c + p.z * s, p.y, -p.x * s + p.z * c);
    welt += vec3(uRadius * sin(winkel), 0.0, uRadius * cos(winkel));
    vec3 n = vec3(normal.x * c + normal.z * s, normal.y, -normal.x * s + normal.z * c);

    vec4 mv = modelViewMatrix * vec4(welt, 1.0);
    vNormaleAnsicht = mat3(viewMatrix) * n;
    vPosAnsicht = mv.xyz;
    vFlaeche = aFlaeche;
    vTiefe = -mv.z;
    vFokus = fokus;
    vUv = uv;
    vKachel = aKachel;
    gl_Position = projectionMatrix * mv;
  }
`;

const fragment = `
  precision highp float;
  uniform sampler2D tAtlas;
  uniform vec2 uRaster;
  uniform vec2 uAtlasPx;
  uniform float uNah;
  uniform float uFern;
  uniform float uGeschwindigkeit;
  uniform vec3 uNebelFarbe;
  uniform vec2 uClip;
  uniform vec3 uKanteHell;
  uniform vec3 uKanteDunkel;
  uniform vec3 uGoldTief;
  uniform vec3 uRueckOben;
  uniform vec3 uRueckUnten;
  varying vec2 vUv;
  varying vec2 vKachel;
  varying float vTiefe;
  varying float vFokus;
  varying float vHervor;
  varying float vFlaeche;
  varying vec3 vNormaleAnsicht;
  varying vec3 vPosAnsicht;

  vec3 vorderseite() {
    vec2 zelle = 1.0 / uRaster;
    vec2 rand = 1.0 / uAtlasPx;
    vec2 lo = vKachel * zelle + rand;
    vec2 hi = (vKachel + 1.0) * zelle - rand;
    vec2 uv0 = (vKachel + vUv) * zelle;
    float spalt = uGeschwindigkeit * 0.012;
    float r = texture2D(tAtlas, clamp(uv0 + vec2(spalt, 0.0), lo, hi)).r;
    float g = texture2D(tAtlas, clamp(uv0, lo, hi)).g;
    float b = texture2D(tAtlas, clamp(uv0 - vec2(spalt, 0.0), lo, hi)).b;
    return vec3(r, g, b);
  }

  vec3 koerper() {
    vec3 n = normalize(vNormaleAnsicht);
    vec3 blick = normalize(-vPosAnsicht);
    vec3 licht = normalize(vec3(-0.45, 0.8, 0.5));
    float streu = max(dot(n, licht), 0.0);
    float glanz = pow(max(dot(n, normalize(licht + blick)), 0.0), 40.0);
    float saum = pow(1.0 - max(dot(n, blick), 0.0), 3.0);
    if (vFlaeche < 1.5) {
      return uKanteHell * (0.3 + 0.7 * streu) + uKanteHell * glanz * 1.2 + uGoldTief * saum * 0.4;
    }
    if (vFlaeche < 2.5) {
      return uKanteDunkel * (0.3 + 0.9 * streu) + uGoldTief * glanz * 0.5 + uGoldTief * saum * 0.25;
    }
    float rahmen = 1.0 - smoothstep(0.0, 0.012, min(min(vUv.x, 1.0 - vUv.x) * 1.6, min(vUv.y, 1.0 - vUv.y)));
    vec3 flaeche = mix(uRueckUnten, uRueckOben, vUv.y) * (0.7 + 0.5 * streu);
    return flaeche + uGoldTief * rahmen * 0.7 + uGoldTief * glanz * 0.3;
  }

  void main() {
    if (gl_FragCoord.y < uClip.x || gl_FragCoord.y > uClip.y) discard;
    vec3 vorn = vorderseite();
    vec3 farbe = vFlaeche < 0.5 ? vorn : koerper();

    float nah = smoothstep(uFern, uNah, vTiefe);
    float deckkraft = mix(0.45, 1.0, pow(nah, 1.2));
    farbe = mix(uNebelFarbe, farbe, mix(0.6, 1.0, nah));
    farbe += vec3(0.08, 0.06, 0.02) * pow(vFokus, 12.0);
    float randAbstand = min(min(vUv.x, 1.0 - vUv.x) * 1.25, min(vUv.y, 1.0 - vUv.y));
    float kante = vFlaeche < 0.5 ? 1.0 - smoothstep(0.0, 0.03, randAbstand) : 0.6;
    farbe += vHervor * (vec3(0.06, 0.05, 0.02) + vec3(0.9, 0.7, 0.35) * kante);
    deckkraft = max(deckkraft, vHervor * 0.9);
    gl_FragColor = vec4(farbe * deckkraft, deckkraft);
  }
`;

function kartenAtlas(sprache) {
  const liste = karten(sprache);
  const canvas = document.createElement("canvas");
  canvas.width = ATLAS.spalten * ATLAS.px;
  canvas.height = ATLAS.zeilen * ATLAS.py;
  const ctx = canvas.getContext("2d");
  const font = getComputedStyle(document.body).fontFamily;

  function zeile(text, x, y, maxBreite, zeilenHoehe, maxZeilen) {
    const woerter = text.split(" ");
    const zeilen = [];
    let aktuell = "";
    for (const wort of woerter) {
      const probe = aktuell ? aktuell + " " + wort : wort;
      if (ctx.measureText(probe).width > maxBreite && aktuell) {
        zeilen.push(aktuell);
        aktuell = wort;
      } else {
        aktuell = probe;
      }
    }
    if (aktuell) zeilen.push(aktuell);
    if (zeilen.length > maxZeilen) {
      let letzte = zeilen.slice(maxZeilen - 1).join(" ");
      while (ctx.measureText(letzte + "…").width > maxBreite && letzte.length > 1) letzte = letzte.slice(0, -1);
      zeilen.length = maxZeilen - 1;
      zeilen.push(letzte.trimEnd() + "…");
    }
    zeilen.forEach((t, n) => ctx.fillText(t, x, y + n * zeilenHoehe));
  }

  liste.forEach((karte, i) => {
    const x0 = (i % ATLAS.spalten) * ATLAS.px;
    const y0 = Math.floor(i / ATLAS.spalten) * ATLAS.py;
    const b = bereiche[karte.bereich];
    const verlauf = ctx.createLinearGradient(x0, y0, x0, y0 + ATLAS.py);
    verlauf.addColorStop(0, "#1b2745");
    verlauf.addColorStop(1, "#0c1326");
    ctx.fillStyle = verlauf;
    ctx.fillRect(x0, y0, ATLAS.px, ATLAS.py);
    const band = Math.round(ATLAS.py * (0.12 + (b.gewicht / MAX_GEWICHT) * 0.3));
    const gold = ctx.createLinearGradient(x0, y0, x0, y0 + band);
    gold.addColorStop(0, "rgba(240, 212, 146, 0.95)");
    gold.addColorStop(1, "rgba(169, 128, 58, 0.75)");
    ctx.fillStyle = gold;
    ctx.fillRect(x0, y0, ATLAS.px, band);
    ctx.fillStyle = "#120d04";
    ctx.font = `700 ${Math.min(44, band - 10)}px ${font}`;
    ctx.textBaseline = "middle";
    ctx.fillText(String(karte.kennung.slice(1)), x0 + 14, y0 + band / 2 + 1);
    ctx.textBaseline = "alphabetic";
    ctx.strokeStyle = "rgba(220, 178, 92, 0.55)";
    ctx.lineWidth = 2;
    ctx.strokeRect(x0 + 1, y0 + 1, ATLAS.px - 2, ATLAS.py - 2);

    ctx.fillStyle = "#dcb25c";
    ctx.font = `600 16px ${font}`;
    ctx.fillText(karte.kennung.toUpperCase(), x0 + 16, y0 + band + 28);
    ctx.textAlign = "right";
    ctx.fillStyle = "#a9a79f";
    ctx.font = `500 15px ${font}`;
    ctx.fillText(`${texte[sprache].gewicht} ${gewichtText(b.gewicht, sprache)}`, x0 + ATLAS.px - 16, y0 + band + 28);
    ctx.textAlign = "left";

    ctx.fillStyle = "#f3f1ea";
    ctx.font = `600 21px ${font}`;
    zeile(karte.name, x0 + 16, y0 + band + 64, ATLAS.px - 32, 27, Math.max(2, Math.floor((ATLAS.py - band - 120) / 27)));

    ctx.fillStyle = "rgba(220, 178, 92, 0.25)";
    ctx.fillRect(x0 + 16, y0 + ATLAS.py - 52, ATLAS.px - 32, 1);
    ctx.fillStyle = "#a9a79f";
    ctx.font = `500 14px ${font}`;
    zeile(sprache === "en" ? b.en : b.de, x0 + 16, y0 + ATLAS.py - 30, ATLAS.px - 32, 17, 1);
  });

  const textur = new THREE.CanvasTexture(canvas);
  textur.colorSpace = THREE.SRGBColorSpace;
  textur.anisotropy = 4;
  textur.minFilter = THREE.LinearMipmapLinearFilter;
  textur.generateMipmaps = true;
  return { textur, liste };
}

function kartenKoerper() {
  const { breite: w, hoehe: h, dicke: t, fase: f, faseSchritte: schritte } = KARTE;
  const pos = [];
  const nor = [];
  const uvs = [];
  const flaeche = [];
  const index = [];

  function punkt(x, y, z, nx, ny, nz, art) {
    pos.push(x, y, z);
    nor.push(nx, ny, nz);
    uvs.push(x / w + 0.5, y / h + 0.5);
    flaeche.push(art);
    return pos.length / 3 - 1;
  }

  function vektor(feld, i) {
    return new THREE.Vector3(feld[i * 3], feld[i * 3 + 1], feld[i * 3 + 2]);
  }

  function dreieck(a, b, c) {
    const geometrisch = vektor(pos, b).sub(vektor(pos, a)).cross(vektor(pos, c).sub(vektor(pos, a)));
    const gewollt = vektor(nor, a).add(vektor(nor, b)).add(vektor(nor, c));
    if (geometrisch.dot(gewollt) >= 0) index.push(a, b, c);
    else index.push(a, c, b);
  }

  function viereck(a, b, c, d) {
    dreieck(a, b, c);
    dreieck(b, d, c);
  }

  for (const seite of [1, -1]) {
    const art = seite > 0 ? FLAECHE.vorn : FLAECHE.hinten;
    const ecken = [[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([sx, sy]) =>
      punkt(sx * (w / 2 - f), sy * (h / 2 - f), (seite * t) / 2, 0, 0, seite, art)
    );
    viereck(...ecken);
  }

  const bogen = Array.from({ length: schritte + 1 }, (_, i) => {
    const a = (i / schritte) * (Math.PI / 2);
    return { d: f * (1 - Math.sin(a)), z: t / 2 - f * (1 - Math.cos(a)), nAussen: Math.sin(a), nz: Math.cos(a) };
  });
  const streifen = [
    { punkte: bogen, art: FLAECHE.fase },
    { punkte: [{ d: 0, z: t / 2 - f, nAussen: 1, nz: 0 }, { d: 0, z: -(t / 2 - f), nAussen: 1, nz: 0 }], art: FLAECHE.wand },
    { punkte: bogen.map((q) => ({ ...q, z: -q.z, nz: -q.nz })), art: FLAECHE.fase }
  ];
  const kanten = [
    { aussen: [0, 1], laengs: [1, 0], halb: w / 2, abstand: h / 2 },
    { aussen: [0, -1], laengs: [1, 0], halb: w / 2, abstand: h / 2 },
    { aussen: [1, 0], laengs: [0, 1], halb: h / 2, abstand: w / 2 },
    { aussen: [-1, 0], laengs: [0, 1], halb: h / 2, abstand: w / 2 }
  ];
  for (const kante of kanten) {
    for (const streif of streifen) {
      const reihen = streif.punkte.map((q) =>
        [-1, 1].map((vorzeichen) => {
          const laengs = vorzeichen * (kante.halb - q.d);
          const nach = kante.abstand - q.d;
          return punkt(
            kante.aussen[0] * nach + kante.laengs[0] * laengs,
            kante.aussen[1] * nach + kante.laengs[1] * laengs,
            q.z,
            kante.aussen[0] * q.nAussen,
            kante.aussen[1] * q.nAussen,
            q.nz,
            streif.art
          );
        })
      );
      for (let i = 0; i < reihen.length - 1; i++) viereck(reihen[i][0], reihen[i][1], reihen[i + 1][0], reihen[i + 1][1]);
    }
  }

  const koerper = new THREE.BufferGeometry();
  koerper.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  koerper.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
  koerper.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
  koerper.setAttribute("aFlaeche", new THREE.Float32BufferAttribute(flaeche, 1));
  koerper.setIndex(index);
  return koerper;
}

export function ring(wurzel, sprache, scroll) {
  const stage = wurzel.querySelector(".ring__stage");
  const labelsEl = wurzel.querySelector(".ring__labels");
  const bereichEl = wurzel.querySelector(".ring__area");
  const metaEl = wurzel.querySelector(".ring__meta");
  const { textur, liste } = kartenAtlas(sprache);
  const N = liste.length;
  const R = (N * KARTE.platz) / TAU;

  const szene = new THREE.Scene();
  const kamera = new THREE.PerspectiveCamera(32, 1, 0.1, 400);

  const basis = kartenKoerper();
  const geometrie = new THREE.InstancedBufferGeometry();
  geometrie.index = basis.index;
  geometrie.attributes.position = basis.attributes.position;
  geometrie.attributes.normal = basis.attributes.normal;
  geometrie.attributes.uv = basis.attributes.uv;
  geometrie.attributes.aFlaeche = basis.attributes.aFlaeche;
  const index = new Float32Array(N);
  const kachel = new Float32Array(N * 2);
  for (let i = 0; i < N; i++) {
    index[i] = i;
    kachel[i * 2] = i % ATLAS.spalten;
    kachel[i * 2 + 1] = ATLAS.zeilen - 1 - Math.floor(i / ATLAS.spalten);
  }
  geometrie.setAttribute("aIndex", new THREE.InstancedBufferAttribute(index, 1));
  geometrie.setAttribute("aKachel", new THREE.InstancedBufferAttribute(kachel, 2));
  geometrie.instanceCount = N;

  const material = new THREE.ShaderMaterial({
    uniforms: {
      tAtlas: { value: textur },
      uRaster: { value: new THREE.Vector2(ATLAS.spalten, ATLAS.zeilen) },
      uAtlasPx: { value: new THREE.Vector2(ATLAS.spalten * ATLAS.px, ATLAS.zeilen * ATLAS.py) },
      uRotation: { value: 0 },
      uRadius: { value: R },
      uAnzahl: { value: N },
      uGeschwindigkeit: { value: 0 },
      uZeit: { value: 0 },
      uNah: { value: 1 },
      uFern: { value: 10 },
      uNebelFarbe: { value: new THREE.Color("#0b1222") },
      uHervor: { value: -1 },
      uGewaehlt: { value: -1 },
      uVerdrehung: { value: KARTE.verdrehung },
      uWahl: { value: 0 },
      uClip: { value: new THREE.Vector2(0, 0) },
      uKanteHell: { value: new THREE.Color("#f0d492") },
      uKanteDunkel: { value: new THREE.Color("#1b2745").lerp(new THREE.Color("#a9803a"), 0.4) },
      uGoldTief: { value: new THREE.Color("#a9803a") },
      uRueckOben: { value: new THREE.Color("#1b2745") },
      uRueckUnten: { value: new THREE.Color("#0c1326") }
    },
    vertexShader: vertex,
    fragmentShader: fragment,
    transparent: true,
    depthWrite: true
  });
  const netz = new THREE.Mesh(geometrie, material);
  netz.frustumCulled = false;
  szene.add(netz);

  const labels = bereiche.map((b, bi) => {
    const el = document.createElement("span");
    el.className = "ring__label";
    el.innerHTML = `${sprache === "en" ? b.en : b.de}<sup>${gewichtText(b.gewicht, sprache)}</sup>`;
    labelsEl.appendChild(el);
    const start = liste.findIndex((k) => k.bereich === bi);
    const mitte = start + (b.pruefungen.length - 1) / 2;
    return { el, winkel: (mitte / N) * TAU, ziel: new THREE.Vector3() };
  });

  let scrollDrehung = 0;
  let ziehDrehung = 0;
  let ziehTempo = 0;
  let eigen = 0;
  let letzteDrehung = 0;
  let tempoGlatt = 0;
  let vorn = -1;

  let zeigerAktiv = false;
  let zeigerX = 0;
  let zeigerStart = { x: 0, y: 0 };
  let zeigerWeg = 0;
  let hervor = -1;

  const wahl = { index: -1, drehZiel: 0, dreh: 0, zoom: 0, zoomZiel: 0, scrollBeiWahl: 0 };
  const tafel = document.createElement("aside");
  tafel.className = "ring__tafel";
  tafel.hidden = true;
  tafel.innerHTML = `<button type="button" class="ring__zu" aria-label="${sprache === "en" ? "Close" : "Schließen"}">×</button>
    <p class="ring__tafel-kopf"></p>
    <h3 class="ring__tafel-name"></h3>
    <p class="ring__tafel-frage"></p>
    <p class="ring__tafel-wirkung"></p>`;
  stage.appendChild(tafel);
  const tafelKopf = tafel.querySelector(".ring__tafel-kopf");
  const tafelName = tafel.querySelector(".ring__tafel-name");
  const tafelFrage = tafel.querySelector(".ring__tafel-frage");
  const tafelWirkung = tafel.querySelector(".ring__tafel-wirkung");

  const hilfsVektor = new THREE.Vector3();
  const hilfsMatrix = new THREE.Matrix4();

  function treffer(clientX, clientY, drehung) {
    const s = stage.getBoundingClientRect();
    const px = clientX - s.left;
    const py = clientY - s.top;
    const halbTan = Math.tan((kamera.fov * Math.PI) / 360);
    hilfsMatrix.copy(kamera.matrixWorldInverse);
    let bester = -1;
    let besteWertung = Infinity;
    for (let i = 0; i < N; i++) {
      const w = (i / N) * TAU + drehung;
      const nah = 0.5 + 0.5 * Math.cos(w);
      if (nah < 0.18) continue;
      hilfsVektor.set(Math.sin(w) * R, 0, Math.cos(w) * R).applyMatrix4(hilfsMatrix);
      const tiefe = -hilfsVektor.z;
      if (tiefe <= 0) continue;
      const halbHoehePx = ((KARTE.hoehe * 0.5) / (tiefe * halbTan)) * (groesse.hoehe * 0.5);
      hilfsVektor.set(Math.sin(w) * R, 0, Math.cos(w) * R).project(kamera);
      const x = (hilfsVektor.x * 0.5 + 0.5) * groesse.breite - s.left;
      const y = (-hilfsVektor.y * 0.5 + 0.5) * groesse.hoehe - s.top;
      const dx = Math.abs(px - x);
      const dy = Math.abs(py - y);
      if (dy > halbHoehePx + 28 || dx > halbHoehePx * 0.5 + 28) continue;
      const wertung = Math.hypot(dx * 1.6, dy) / (0.35 + nah);
      if (wertung < besteWertung) { besteWertung = wertung; bester = i; }
    }
    return bester;
  }

  function normiereWinkel(a) {
    return ((((a + Math.PI) % TAU) + TAU) % TAU) - Math.PI;
  }

  function waehle(i, drehung) {
    const k = liste[i];
    const b = bereiche[k.bereich];
    wahl.index = i;
    wahl.drehZiel = wahl.dreh + normiereWinkel(-(i / N) * TAU - drehung);
    wahl.zoomZiel = 1;
    wahl.scrollBeiWahl = scrollDrehung + ziehDrehung;
    material.uniforms.uGewaehlt.value = i;
    tafelKopf.textContent = `${k.kennung.toUpperCase()} · ${sprache === "en" ? b.en : b.de} · ${texte[sprache].gewicht} ${gewichtText(b.gewicht, sprache)}`;
    tafelName.textContent = k.name;
    tafelFrage.textContent = sprache === "en" ? "" : k.frage;
    tafelWirkung.textContent = sprache === "en" ? "" : k.wirkung;
    tafel.hidden = false;
    wurzel.classList.add("ist-gewaehlt");
  }

  function abwaehlen() {
    if (wahl.index < 0) return;
    wahl.index = -1;
    wahl.zoomZiel = 0;
    material.uniforms.uGewaehlt.value = -1;
    wurzel.classList.remove("ist-gewaehlt");
  }

  tafel.addEventListener("pointerdown", (e) => e.stopPropagation());
  tafel.querySelector(".ring__zu").addEventListener("click", abwaehlen);
  window.addEventListener("keydown", (e) => { if (e.key === "Escape") abwaehlen(); });

  stage.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    zeigerAktiv = true;
    zeigerX = e.clientX;
    zeigerStart = { x: e.clientX, y: e.clientY };
    zeigerWeg = 0;
    ziehTempo = 0;
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("ist-am-ziehen");
  });
  stage.addEventListener("pointermove", (e) => {
    if (!zeigerAktiv) {
      if (e.pointerType === "mouse") {
        hervor = treffer(e.clientX, e.clientY, letzteDrehung);
        material.uniforms.uHervor.value = hervor;
        stage.classList.toggle("hat-treffer", hervor >= 0);
      }
      return;
    }
    const dx = e.clientX - zeigerX;
    zeigerX = e.clientX;
    zeigerWeg = Math.max(zeigerWeg, Math.hypot(e.clientX - zeigerStart.x, e.clientY - zeigerStart.y));
    const schritt = (dx / stage.clientWidth) * TAU * 0.55;
    ziehDrehung += schritt;
    ziehTempo = schritt;
  });
  function loslassen(e) {
    if (!zeigerAktiv) return;
    zeigerAktiv = false;
    stage.classList.remove("ist-am-ziehen");
    if (e.type === "pointerup" && zeigerWeg < 6) {
      const i = treffer(e.clientX, e.clientY, letzteDrehung);
      if (i >= 0 && i !== wahl.index) waehle(i, letzteDrehung);
      else abwaehlen();
    }
  }
  stage.addEventListener("pointerup", loslassen);
  stage.addEventListener("pointercancel", loslassen);
  stage.addEventListener("pointerleave", () => {
    if (hervor >= 0) { hervor = -1; material.uniforms.uHervor.value = -1; stage.classList.remove("hat-treffer"); }
  });

  let groesse = { breite: 1, hoehe: 1 };
  const basisPos = new THREE.Vector3();
  const basisBlick = new THREE.Vector3();
  const kartenVorn = new THREE.Vector3();
  const blick = new THREE.Vector3();
  function anpassen(g) {
    groesse = g;
    kamera.aspect = g.breite / g.hoehe;
    const schmal = g.breite <= 720;
    const sinE = schmal ? 0.42 : 0.6;
    const cosE = Math.sqrt(1 - sinE * sinE);
    const halbTan = Math.tan((kamera.fov * Math.PI) / 360);
    const halbBreite = R + KARTE.breite * 0.8;
    const fuellung = schmal ? 1.35 : 0.74;
    const abstand = R * cosE + halbBreite / (halbTan * kamera.aspect * fuellung);
    basisPos.set(0, abstand * sinE, abstand * cosE);
    basisBlick.set(0, 0, R * 0.14);
    kamera.position.copy(basisPos);
    kamera.lookAt(basisBlick);
    kamera.updateProjectionMatrix();
    material.uniforms.uNah.value = abstand - R * cosE * 0.95;
    material.uniforms.uFern.value = abstand + R * cosE * 1.05;
  }

  function sichtbar() {
    const r = wurzel.getBoundingClientRect();
    return r.bottom > 0 && r.top < groesse.hoehe;
  }

  function zeichne(renderer, z) {
    const r = wurzel.getBoundingClientRect();
    const weg = Math.max(1, r.height - groesse.hoehe);
    const fortschritt = Math.min(1, Math.max(0, -r.top / weg));
    scrollDrehung = -fortschritt * TAU * 0.9;

    if (!zeigerAktiv) {
      ziehDrehung += ziehTempo;
      ziehTempo *= 0.94;
      if (Math.abs(ziehTempo) < 0.00005) ziehTempo = 0;
    }
    if (wahl.index >= 0) {
      if (Math.abs(scrollDrehung + ziehDrehung - wahl.scrollBeiWahl) > 0.03) abwaehlen();
    } else {
      eigen += z.dt * 0.035;
    }
    wahl.dreh += (wahl.drehZiel - wahl.dreh) * Math.min(1, z.dt * 5);
    wahl.zoom += (wahl.zoomZiel - wahl.zoom) * Math.min(1, z.dt * 4);
    if (wahl.zoom < 0.002 && wahl.zoomZiel === 0 && !tafel.hidden) tafel.hidden = true;

    kartenVorn.set(0, KARTE.hoehe * 0.05, R);
    const ease = wahl.zoom * wahl.zoom * (3 - 2 * wahl.zoom);
    kamera.position.copy(basisPos).lerp(kartenVorn, ease * 0.7);
    const seitlich = groesse.breite > 720 ? 1.6 : 0;
    blick.copy(basisBlick).lerp(kartenVorn, ease).x += seitlich * ease;
    kamera.lookAt(blick);
    kamera.updateMatrixWorld();

    const drehung = scrollDrehung + ziehDrehung + eigen + wahl.dreh;
    const tempo = (drehung - letzteDrehung) / Math.max(0.001, z.dt);
    letzteDrehung = drehung;
    tempoGlatt += (tempo - tempoGlatt) * Math.min(1, z.dt * 8);
    const normiert = Math.max(-1, Math.min(1, tempoGlatt / 4.5));

    material.uniforms.uWahl.value = wahl.zoom;
    material.uniforms.uRotation.value = drehung;
    material.uniforms.uGeschwindigkeit.value = normiert;
    material.uniforms.uZeit.value = z.zeit;

    const i = ((Math.round((-drehung / TAU) * N) % N) + N) % N;
    if (i !== vorn) {
      vorn = i;
      const b = bereiche[liste[i].bereich];
      bereichEl.textContent = sprache === "en" ? b.en : b.de;
      metaEl.textContent = `${texte[sprache].gewicht} ${gewichtText(b.gewicht, sprache)} · ${b.pruefungen.length} ${texte[sprache].pruefungen}`;
      wurzel.style.setProperty("--ring-fokus", String(liste[i].bereich));
    }

    const s = stage.getBoundingClientRect();
    kamera.setViewOffset(groesse.breite, groesse.hoehe, 0, -s.top, groesse.breite, groesse.hoehe);
    const dpr = groesse.dpr || 1;
    material.uniforms.uClip.value.set(
      (groesse.hoehe - Math.min(groesse.hoehe, s.bottom)) * dpr,
      (groesse.hoehe - Math.max(0, s.top)) * dpr
    );
    labels.forEach((l) => {
      const w = l.winkel + drehung;
      l.ziel.set(Math.sin(w) * R * 1.16, KARTE.hoehe * 0.62, Math.cos(w) * R * 1.16).project(kamera);
      const x = (l.ziel.x * 0.5 + 0.5) * groesse.breite - s.left;
      const y = (-l.ziel.y * 0.5 + 0.5) * groesse.hoehe - s.top;
      const nah = 0.5 + 0.5 * Math.cos(w);
      const links = Math.sin(w) < 0;
      const schmal = groesse.breite <= 720;
      const breite = l.el.offsetWidth;
      const xk = links ? Math.max(12 + breite, x) : Math.min(s.width - 12 - breite, x);
      l.el.style.transform = `translate(${links ? "-100%" : "0"}, -50%) translate(${xk.toFixed(1)}px, ${y.toFixed(1)}px)`;
      l.el.style.opacity = schmal ? (nah > 0.55 ? ((nah - 0.55) / 0.45).toFixed(2) : "0") : (0.4 + nah * 0.6).toFixed(2);
      l.el.style.zIndex = nah > 0.5 ? "2" : "1";
    });

    renderer.render(szene, kamera);
  }

  return { sichtbar, zeichne, anpassen };
}
