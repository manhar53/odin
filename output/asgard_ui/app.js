/* ============================================================
   YGGDRASIL — painted World Tree + living overlay.

   The scene is a painting (assets/yggdrasil.jpg — night-blue tree with
   glowing star-leaves on a floating root island, aurora falling to a
   mirror lake). Code does NOT try to draw the tree; it makes the
   painting BREATHE: heart-glow throbs with ODIN's voice, leaf-motes
   twinkle in the canopy, sap pulses run from the heartwood to whichever
   faculty just acted, galaxies in the roots shimmer when he thinks, the
   aurora swells when he listens, ravens circle the crown, light-drips
   fall, mist drifts over the lake.

   Python -> JS (contract unchanged since the throne room):
     odin.setState(state, payload)     idle | listening | thinking | speaking
     odin.speakChunk(text)             append to answer panel
     odin.showAnswerText(text)         replace answer panel content
     odin.beginTurn()                  clear panel, reset turn
     odin.setMouthLevel(level)         voice amplitude -> heart-glow throb
     odin.resetMouth()                 heart decays back to idle breathing
     odin.moduleActive(module, skill)  sap pulse heart -> region, mote flash

   JS -> Python:
     pywebview.api.on_click()             click empty sky -> PHANTOM mode
     pywebview.api.submit_text(text)      command bar line
     pywebview.api.on_drop(payloadJson)   file/link/text fed to the tree
     pywebview.api.get_module_info(name)  module skill list for the dossier

   Anatomy (regions of the painting, image-fraction coords):
     Heartwood   = the lit trunk — CORE (GILGAMESH, MARDUK)
     Canopy      = seven glowing clusters — the upper layers
     Root island = MEMORY (THOTH, HERMES, NABU drink beside the galaxies)
     The far castle = IDLE (SELENE's distant hall)
     The far peaks  = ENVIRONMENT (FUJIN's winds, SINDBAD's voyages)

   Interaction: wheel zoom · drag pan · hover cards · click region =
   dossier · click sky = phantom · dbl-click/Esc reset · "/" command bar ·
   drag & drop feeds the tree (regions have purposes; see asgard.py).
   ============================================================ */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const canvas = $("space");
  const ctx = canvas.getContext("2d");
  const answer = $("answer");
  const answerText = $("answerText");
  const answerState = $("answerState");
  const status = $("status");
  const bodyCard = $("bodyCard");
  const dossier = $("dossier");
  const dossierBody = $("dossierBody");
  const dossierTitle = $("dossierTitle");
  const dossierLayer = $("dossierLayer");
  const dossierPurpose = $("dossierPurpose");
  const dossierClose = $("dossierClose");
  const cmdbar = $("cmdbar");
  const cmdInput = $("cmdInput");
  const toast = $("toast");
  const ticker = $("ticker");
  const mind = $("mind");
  const mindTitle = $("mindTitle");
  const mindMeta = $("mindMeta");
  const body = document.body;

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DEBUG = /debug/.test(window.location.hash);

  /* ── Regions of the painting ────────────────────────────────
     fx, fy: image-fraction coords. r: hotspot radius as fraction of
     image height. `drop` mirrors asgard.py's routing table — keep in
     sync. `where` is the hover-card subtitle. */
  const TARGETS = [
    { layer: "INTELLIGENCE", fx: 0.565, fy: 0.150, r: 0.075, color: "#9ec7ff",
      where: "the high crown",
      moons: ["SARASWATI", "SESHAT", "ATHENA", "AKASHA", "CHITRA",
              "TYR", "MIMIR", "MERLIN", "PROMETHEUS", "WAYLAND"],
      drop: "Drop code or links — the high crown studies and explains them." },
    { layer: "PERSONALITY",  fx: 0.380, fy: 0.130, r: 0.060, color: "#d3a6c4",
      where: "the western crown",
      moons: ["LOKI", "PSYCHE", "APOLLO"],
      drop: "Drop anything — archived to the Brain vault." },
    { layer: "INPUT",        fx: 0.240, fy: 0.210, r: 0.065, color: "#a9c3dd",
      where: "the listening boughs",
      moons: ["HEIMDALL", "HORUS", "AETHER", "NARADA", "HUGIN"],
      drop: "Drop text — the listening boughs run it as a command." },
    { layer: "OUTPUT",       fx: 0.715, fy: 0.205, r: 0.065, color: "#ffd9a0",
      where: "the speaking boughs",
      moons: ["IRIS", "THOR", "MERCURY", "ARGUS", "HERMOD"],
      drop: "Drop a text file — ODIN reads it aloud. Other files open." },
    { layer: "PROTECTION",   fx: 0.150, fy: 0.305, r: 0.060, color: "#e89a87",
      where: "the warding boughs",
      moons: ["KARN", "ENKIDU", "OSIRIS"],
      drop: "Drop files — sealed into a timestamped backup." },
    { layer: "MANAGEMENT",   fx: 0.650, fy: 0.300, r: 0.060, color: "#8fb0e8",
      where: "the ordering boughs",
      moons: ["CHRONOS", "MIDAS", "SAINT", "ARJUN", "AURORA"],
      drop: "Drop text — it becomes a reminder (one hour)." },
    { layer: "UTILITY",      fx: 0.840, fy: 0.290, r: 0.060, color: "#9adbe0",
      where: "the working boughs",
      moons: ["HEPHAESTUS", "GANESH", "STIRLING", "CASSANDRA", "OGMA", "SHERLOCK", "JANUS", "VESTA"],
      drop: "Drop a spreadsheet — instant structure summary." },
    { layer: "ENVIRONMENT",  fx: 0.835, fy: 0.685, r: 0.070, color: "#b9d4e8",
      where: "the far peaks",
      moons: ["FUJIN", "SINDBAD"],
      drop: "Drop anything — archived to the Brain vault." },
    { layer: "IDLE",         fx: 0.160, fy: 0.675, r: 0.060, color: "#c9c2e8",
      where: "the distant hall",
      moons: ["SELENE", "VYASA"],
      drop: "Drop anything — archived to the Brain vault." },
    { layer: "MEMORY",       fx: 0.500, fy: 0.505, r: 0.100, color: "#86b6ff",
      where: "the roots and their galaxies",
      moons: ["THOTH", "HERMES", "NABU"],
      drop: "Drop files or links — buried in the roots: the Brain vault." },
    { layer: "CORE",         fx: 0.500, fy: 0.385, r: 0.055, color: "#e8f0ff",
      where: "the heartwood",
      moons: ["GILGAMESH", "MARDUK"],
      drop: "Drop text on the heartwood — ODIN runs it as a command." },
  ];
  const TGT_MEMORY = 9, TGT_HEART = 10;
  // The two painted galaxies among the roots (image fractions).
  const GALAXIES = [
    { fx: 0.455, fy: 0.452, r: 0.040 },
    { fx: 0.415, fy: 0.528, r: 0.034 },
  ];
  const CANOPY_BOTTOM = 0.42;      // light-drips spawn along this line

  const SUN_MODULES = { GILGAMESH: 1, GIL: 1, MARDUK: 1, ODIN: 1 };
  const MODULE_MAP = {};
  TARGETS.forEach((tgt, ti) => tgt.moons.forEach((m, mi) => { MODULE_MAP[m] = { ti, mi }; }));

  function mulberry32(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      let z = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      z = (z + Math.imul(z ^ (z >>> 7), 61 | z)) ^ z;
      return ((z ^ (z >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* ── The painting ───────────────────────────────────────────── */
  const art = new Image();
  let artReady = false;
  let fadedArt = null;            // edge-faded copy so it melts into the void
  art.onload = () => {
    // Pre-fade the left/right edges into transparency once.
    fadedArt = document.createElement("canvas");
    fadedArt.width = art.width; fadedArt.height = art.height;
    const fc = fadedArt.getContext("2d");
    fc.drawImage(art, 0, 0);
    const fadeW = Math.round(art.width * 0.085);
    const fadeH = Math.round(art.height * 0.06);
    fc.globalCompositeOperation = "destination-out";
    let g = fc.createLinearGradient(0, 0, fadeW, 0);
    g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
    fc.fillStyle = g; fc.fillRect(0, 0, fadeW, art.height);
    g = fc.createLinearGradient(art.width, 0, art.width - fadeW, 0);
    g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
    fc.fillStyle = g; fc.fillRect(art.width - fadeW, 0, fadeW, art.height);
    g = fc.createLinearGradient(0, 0, 0, fadeH);
    g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
    fc.fillStyle = g; fc.fillRect(0, 0, art.width, fadeH);
    g = fc.createLinearGradient(0, art.height, 0, art.height - fadeH);
    g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
    fc.fillStyle = g; fc.fillRect(0, art.height - fadeH, art.width, fadeH);
    artReady = true;
    layout();
  };
  art.onerror = () => {
    console.error("yggdrasil.jpg missing — run with assets/yggdrasil.jpg in place");
  };
  art.src = "assets/yggdrasil.jpg";

  /* ── Layout ─────────────────────────────────────────────────
     World space: image-pixel coords scaled so the painting fits the
     window height, origin at the painting's center. The scene is FIXED
     in place — no pan/zoom; `cam` survives only so the coordinate
     helpers stay identical. */
  let W = 0, H = 0, CX = 0, CY = 0, DPR = 1;
  let IMG_W = 0, IMG_H = 0;        // world-space size of the painting
  const cam = { x: 0, y: 0, z: 1 };

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = W * DPR; canvas.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    CX = W / 2; CY = H / 2;
    makeStars();
    layout();
  }
  function layout() {
    if (!artReady) return;
    const s = (H * 1.13) / art.height;     // overscan: trade plain sky/water margins for a bigger tree
    IMG_W = art.width * s;
    IMG_H = art.height * s;
    seedOverlayPoints();
  }
  function w2sX(wx) { return CX + (wx - cam.x) * cam.z; }
  function w2sY(wy) { return CY + (wy - cam.y) * cam.z; }
  function s2wX(sx) { return (sx - CX) / cam.z + cam.x; }
  function s2wY(sy) { return (sy - CY) / cam.z + cam.y; }
  function fx2w(fx) { return (fx - 0.5) * IMG_W; }
  function fy2w(fy) { return (fy - 0.5) * IMG_H; }

  /* ── Glow sprite cache ──────────────────────────────────────── */
  const spriteCache = {};
  function glowSprite(color, size) {
    const key = color + "|" + size;
    if (spriteCache[key]) return spriteCache[key];
    const c = document.createElement("canvas");
    c.width = c.height = size * 2;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(size, size, 0, size, size, size);
    const [r, gg, b] = hexToRgb(color);
    grad.addColorStop(0, `rgba(${r},${gg},${b},0.9)`);
    grad.addColorStop(0.25, `rgba(${r},${gg},${b},0.45)`);
    grad.addColorStop(1, `rgba(${r},${gg},${b},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, size * 2, size * 2);
    spriteCache[key] = c;
    return c;
  }

  /* ── Background stars (the void either side of the painting) ── */
  const STAR_COLORS = ["#dce6f5", "#dce6f5", "#cfd9ff", "#ffe9c9", "#bcd2ff"];
  let starLayers = [];
  function makeStars() {
    const defs = [
      { par: 0.10, density: 7000, rMax: 1.0 },
      { par: 0.30, density: 5400, rMax: 1.6 },
    ];
    starLayers = defs.map((d) => {
      const n = Math.floor((W * H) / d.density);
      const arr = [];
      for (let i = 0; i < n; i++) {
        arr.push({
          x: Math.random() * W * 1.5 - W * 0.25,
          y: Math.random() * H * 1.5 - H * 0.25,
          r: Math.random() * d.rMax + 0.3,
          base: Math.random() * 0.5 + 0.2,
          tw: Math.random() * Math.PI * 2,
          twSpeed: Math.random() * 1.5 + 0.4,
          c: STAR_COLORS[(Math.random() * STAR_COLORS.length) | 0],
        });
      }
      return { par: d.par, stars: arr };
    });
  }

  let meteors = [];
  let nextMeteorAt = performance.now() + 8000;
  function maybeSpawnMeteor(nowMs) {
    if (REDUCED || nowMs < nextMeteorAt) return;
    nextMeteorAt = nowMs + 8000 + Math.random() * 15000;
    meteors.push({
      x: Math.random() * W, y: Math.random() * H * 0.30,
      vx: (Math.random() < 0.5 ? -1 : 1) * (6 + Math.random() * 5),
      vy: 3 + Math.random() * 3,
      born: nowMs, life: 900,
    });
  }

  /* ── Overlay anchor points (seeded; recomputed on layout) ───── */
  let leafMotes = [];      // twinkles inside the canopy clusters + realms
  let moduleLeaves = [];   // one orb per module, golden-spiral per region
  let handles = [];        // hover/drop anchors in world coords
  function seedOverlayPoints() {
    const rnd = mulberry32(777001);
    leafMotes = []; moduleLeaves = []; handles = [];
    TARGETS.forEach((tgt, ti) => {
      const cx = fx2w(tgt.fx), cy = fy2w(tgt.fy);
      const rad = tgt.r * IMG_H;
      handles[ti] = { x: cx, y: cy, r: Math.max(rad * 1.25, 44) };
      // Twinkle motes: canopy regions get many, realms a few.
      const isRealm = tgt.layer === "ENVIRONMENT" || tgt.layer === "IDLE";
      const n = isRealm ? 4 : 9 + tgt.moons.length * 2;
      for (let i = 0; i < n; i++) {
        const a = rnd() * Math.PI * 2;
        const rr = Math.sqrt(rnd()) * rad * 1.15;
        leafMotes.push({
          x: cx + Math.cos(a) * rr, y: cy + Math.sin(a) * rr * 0.75,
          ti, ph: rnd() * Math.PI * 2, sz: 1.6 + rnd() * 2.6,
          sp: 0.7 + rnd() * 1.1,
        });
      }
      // Module orbs on a golden-angle spiral inside the region.
      const mc = tgt.moons.length;
      for (let mi = 0; mi < mc; mi++) {
        const rr = rad * 0.78 * Math.sqrt((mi + 0.6) / mc);
        const ga = mi * 2.39996 + ti * 1.3;
        moduleLeaves.push({
          x: cx + Math.cos(ga) * rr, y: cy + Math.sin(ga) * rr * 0.7,
          ti, mi, ph: rnd() * Math.PI * 2,
        });
      }
    });
  }

  /* ── Live state ─────────────────────────────────────────────── */
  let state = "idle";
  let coronaLevel = 0, coronaTarget = 0;
  let sapPulses = [];         // {ti, born}
  const flashes = {};         // "ti:mi" or "t:ti" -> expiry
  const lastSkill = {};
  let heartRipple = 0;
  let turnId = 0, panelTurnId = 0, answerHideTimer = null;
  let selected = -1;
  let dropTarget = -1;
  let dragDepth = 0;

  /* ── Python-facing API ──────────────────────────────────────── */
  function setStateClass(s) {
    body.classList.remove("idle", "listening", "thinking", "speaking");
    body.classList.add(s);
    status.className = "state-" + s;
    status.textContent = s === "idle" ? "idle — the tree stands" : s;
  }

  function hideAnswer(delay) {
    if (answerHideTimer) clearTimeout(answerHideTimer);
    answerHideTimer = setTimeout(() => {
      answer.classList.add("hidden");
      answerHideTimer = null;
    }, delay || 0);
  }

  function beginTurn() {
    turnId += 1;
    answerText.textContent = "";
    answer.classList.add("hidden");
    if (answerHideTimer) { clearTimeout(answerHideTimer); answerHideTimer = null; }
  }

  function setState(newState, payload) {
    payload = payload || {};
    state = newState;
    setStateClass(newState);
    if (newState === "speaking") {
      const t = payload.text || "";
      const isNewTurn = panelTurnId !== turnId;
      if (isNewTurn || !payload.append) {
        answerText.textContent = t;
        panelTurnId = turnId;
      } else if (answerText.textContent) {
        answerText.textContent = answerText.textContent.trimEnd() + " " + t;
      } else {
        answerText.textContent = t;
      }
      answerState.textContent = "speaking";
      answer.classList.remove("hidden");
      if (answerHideTimer) { clearTimeout(answerHideTimer); answerHideTimer = null; }
      requestAnimationFrame(() => { answerText.scrollTop = answerText.scrollHeight; });
    } else if (newState === "idle") {
      answerState.textContent = "spoken";
      hideAnswer(3500);
    }
  }

  function speakChunk(text)     { setState("speaking", { text, append: true }); }
  function showAnswerText(text) { setState("speaking", { text, append: false }); }
  function setMouthLevel(level) { coronaTarget = Math.max(0, Math.min(1, level)); }
  function resetMouth()         { coronaTarget = 0; }

  function moduleActive(module, skill) {
    module = String(module || "").toUpperCase();
    if (skill) lastSkill[module] = String(skill);
    pushTicker(module, skill);
    if (SUN_MODULES[module]) { heartRipple = 1; return; }
    const hit = MODULE_MAP[module];
    if (!hit) return;
    const now = performance.now();
    flashes["t:" + hit.ti] = now + 2200;
    flashes[hit.ti + ":" + hit.mi] = now + 2200;
    if (!REDUCED) sapPulses.push({ ti: hit.ti, born: now });
    if (TARGETS[hit.ti].layer === "INPUT" && !REDUCED) ravens[0].diveUntil = now + 1800;
    if (selected === hit.ti) refreshDossierSkills();
  }

  // MIND panel — what ODIN currently holds in working memory (the last
  // link/file/screen it comprehended). ASGARD pushes updates when the
  // working-memory file changes; the panel flashes so the moment of
  // "ODIN just took something in" is visible.
  function setMind(m) {
    if (!mind || !m || !m.title) return;
    mindTitle.textContent = m.title;
    const kind = m.kind || "context";
    let ago = "";
    if (m.ts) {
      const t = new Date(m.ts.replace(" ", "T"));
      if (!isNaN(t)) {
        const mins = Math.max(0, Math.round((Date.now() - t.getTime()) / 60000));
        ago = mins < 1 ? " · just now" : mins < 60 ? ` · ${mins}m ago` : ` · ${Math.round(mins / 60)}h ago`;
      }
    }
    mindMeta.textContent = kind + ago;
    mind.classList.remove("hidden");
    mind.classList.remove("pulse");
    void mind.offsetWidth;               // restart the CSS animation
    mind.classList.add("pulse");
  }

  window.odin = {
    setState, speakChunk, showAnswerText, beginTurn,
    setMouthLevel, resetMouth,
    moduleActive, setMind,
    ping: () => "asgard-alive",
  };

  /* ── Ticker / toast ─────────────────────────────────────────── */
  function pushTicker(module, skill) {
    if (!ticker) return;
    const row = document.createElement("div");
    row.className = "tick";
    row.textContent = module + (skill ? " · " + skill : "");
    ticker.prepend(row);
    while (ticker.children.length > 4) ticker.removeChild(ticker.lastChild);
    setTimeout(() => { row.classList.add("fade"); }, 4200);
    setTimeout(() => { if (row.parentNode) row.parentNode.removeChild(row); }, 5400);
  }

  let toastTimer = null;
  function showToast(text, ms) {
    toast.textContent = text;
    toast.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.add("hidden"), ms || 3800);
  }

  /* ── Ambience actors ────────────────────────────────────────── */
  const ravens = [
    { ph: 0, speed: 0.085, rx: 0.46, ry: 0.07, flap: 0, diveUntil: 0 },
    { ph: 2.9, speed: 0.065, rx: 0.55, ry: 0.09, flap: 1.7, diveUntil: 0 },
  ];
  let drips = [];
  let nextDripAt = performance.now() + 2000;
  function maybeSpawnDrip(nowMs, rndUnit) {
    if (REDUCED || nowMs < nextDripAt) return;
    nextDripAt = nowMs + 1400 + rndUnit * 2600;
    drips.push({
      fx: 0.18 + rndUnit * 0.64,
      born: nowMs, life: 2600 + rndUnit * 1400,
    });
  }

  /* ── Render ─────────────────────────────────────────────────── */
  function draw(now) {
    const t = now / 1000;
    ctx.clearRect(0, 0, W, H);

    // The void: deep gradient + parallax stars filling the whole window.
    const bg = ctx.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#05070f");
    bg.addColorStop(0.5, "#0a0e1e");
    bg.addColorStop(1, "#070a15");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);
    // Side nebulas tie the void to the painting's palette — kept wispy
    // and faint so they read as haze, not spotlights.
    ctx.globalAlpha = 0.45;
    ctx.drawImage(glowSprite("#26396b", 160), -W * 0.16, -H * 0.1, W * 0.46, H * 0.85);
    ctx.drawImage(glowSprite("#1d2c54", 160), -W * 0.08, H * 0.45, W * 0.38, H * 0.75);
    ctx.drawImage(glowSprite("#2b3a66", 160), W * 0.72, H * 0.05, W * 0.44, H * 0.8);
    ctx.drawImage(glowSprite("#1d2c54", 160), W * 0.78, H * 0.5, W * 0.36, H * 0.7);
    ctx.globalAlpha = 1;

    for (const layer of starLayers) {
      const ox = -cam.x * cam.z * layer.par;
      const oy = -cam.y * cam.z * layer.par;
      for (const s of layer.stars) {
        const sx = s.x + ox, sy = s.y + oy;
        if (sx < -4 || sx > W + 4 || sy < -4 || sy > H + 4) continue;
        const a = REDUCED ? s.base : s.base + Math.sin(s.tw + t * s.twSpeed) * 0.2;
        ctx.globalAlpha = Math.max(0.05, a);
        ctx.fillStyle = s.c;
        ctx.beginPath();
        ctx.arc(sx, sy, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;

    maybeSpawnMeteor(now);
    meteors = meteors.filter((m) => now - m.born < m.life);
    for (const m of meteors) {
      const k = (now - m.born) / m.life;
      const mx = m.x + m.vx * k * 60, my = m.y + m.vy * k * 60;
      const grad = ctx.createLinearGradient(mx - m.vx * 6, my - m.vy * 6, mx, my);
      grad.addColorStop(0, "rgba(220,230,245,0)");
      grad.addColorStop(1, "rgba(220,230,245," + (0.7 * (1 - k)).toFixed(3) + ")");
      ctx.strokeStyle = grad;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(mx - m.vx * 6, my - m.vy * 6);
      ctx.lineTo(mx, my);
      ctx.stroke();
    }

    if (!artReady) {
      ctx.fillStyle = "rgba(201,209,217,0.6)";
      ctx.font = "13px Cascadia Mono, Consolas, monospace";
      ctx.textAlign = "center";
      ctx.fillText("yggdrasil.jpg missing from assets/", CX, CY);
      return;
    }

    // The painting, edge-faded into the void.
    const ix = w2sX(-IMG_W / 2), iy = w2sY(-IMG_H / 2);
    ctx.drawImage(fadedArt, ix, iy, IMG_W * cam.z, IMG_H * cam.z);

    /* ── Living overlay (everything below composites on the art) ── */

    // Aurora swell while listening: the painted aurora region breathes.
    if (state === "listening" || state === "thinking") {
      const k = state === "listening" ? 1 : 0.45;
      const pulse = REDUCED ? 0.6 : 0.45 + 0.55 * Math.sin(t * 2.2);
      const ax = w2sX(fx2w(0.52)), ay0 = w2sY(fy2w(0.50)), ay1 = w2sY(fy2w(0.92));
      const ag = ctx.createLinearGradient(0, ay0, 0, ay1);
      ag.addColorStop(0, "rgba(120,235,190,0)");
      ag.addColorStop(0.5, "rgba(120,235,190," + (0.07 * k * pulse).toFixed(3) + ")");
      ag.addColorStop(1, "rgba(120,235,190,0)");
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      ctx.fillStyle = ag;
      const aw = IMG_W * 0.38 * cam.z;
      ctx.fillRect(ax - aw, ay0, aw * 2, ay1 - ay0);
      ctx.restore();
    }

    // Galaxies in the roots shimmer — faster when thinking.
    const gSpeed = state === "thinking" ? 3.4 : 1.0;
    GALAXIES.forEach((ga, i) => {
      const gx = w2sX(fx2w(ga.fx)), gy = w2sY(fy2w(ga.fy));
      const gr = ga.r * IMG_H * cam.z;
      const pulse = REDUCED ? 0.5 : 0.35 + 0.65 * Math.max(0, Math.sin(t * 1.1 * gSpeed + i * 2.4));
      ctx.globalAlpha = 0.30 * pulse;
      ctx.drawImage(glowSprite("#9dc4ff", 60), gx - gr * 1.6, gy - gr * 1.6, gr * 3.2, gr * 3.2);
      ctx.globalAlpha = 1;
    });

    // Heart of the tree — the lit trunk throbs with ODIN's voice.
    coronaLevel += (coronaTarget - coronaLevel) * 0.25;
    heartRipple = Math.max(0, heartRipple - 0.02);
    const hx = w2sX(handles[TGT_HEART].x), hy = w2sY(handles[TGT_HEART].y);
    let hBreathe = REDUCED ? 0.35 : 0.35 + Math.sin(t * 1.3) * 0.13;
    if (state === "thinking") hBreathe = REDUCED ? 0.5 : 0.45 + Math.sin(t * 7) * 0.22;
    const hGlow = hBreathe + coronaLevel * 0.85 + heartRipple * 0.4;
    const hR = TARGETS[TGT_HEART].r * IMG_H * cam.z * (1.6 + coronaLevel * 0.8);
    ctx.save();
    ctx.globalCompositeOperation = "screen";
    ctx.globalAlpha = Math.min(1, hGlow);
    ctx.drawImage(glowSprite("#cfe1ff", 80), hx - hR, hy - hR, hR * 2, hR * 2);
    ctx.globalAlpha = Math.min(1, hGlow * 0.65);
    ctx.drawImage(glowSprite("#ffe9c4", 60), hx - hR * 0.55, hy - hR * 0.55, hR * 1.1, hR * 1.1);
    ctx.restore();
    ctx.globalAlpha = 1;

    // Listening pulse ring around the heartwood.
    if (state === "listening" && !REDUCED) {
      const ringT = (t % 1.6) / 1.6;
      ctx.strokeStyle = "rgba(140,190,255," + (0.5 * (1 - ringT)).toFixed(3) + ")";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(hx, hy, hR * 0.5 + ringT * 52 * cam.z, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Sap pulses: light leaving the heart for whichever region acted.
    sapPulses = sapPulses.filter((p) => now - p.born < 900);
    for (const p of sapPulses) {
      const k = (now - p.born) / 900;
      const tgt = handles[p.ti];
      // Bezier via the crown fork so canopy pulses climb the trunk first.
      const via = p.ti === TGT_MEMORY ? { x: fx2w(0.5), y: fy2w(0.44) }
                : { x: fx2w(0.5), y: fy2w(0.30) };
      const u = 1 - k;
      const px = u * u * handles[TGT_HEART].x + 2 * u * k * via.x + k * k * tgt.x;
      const py = u * u * handles[TGT_HEART].y + 2 * u * k * via.y + k * k * tgt.y;
      const sx = w2sX(px), sy = w2sY(py);
      const sz = (10 - k * 5) * cam.z;
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      ctx.drawImage(glowSprite(TARGETS[p.ti].color, 30), sx - sz * 2, sy - sz * 2, sz * 4, sz * 4);
      ctx.restore();
    }

    // Leaf-motes: the canopy's painted star-clusters twinkle further.
    ctx.save();
    ctx.globalCompositeOperation = "screen";
    for (const lm of leafMotes) {
      const tw = REDUCED ? 0.45 : 0.18 + 0.82 * Math.max(0, Math.sin(t * lm.sp + lm.ph));
      const sx = w2sX(lm.x), sy = w2sY(lm.y);
      if (sx < -20 || sx > W + 20 || sy < -20 || sy > H + 20) continue;
      const sz = lm.sz * cam.z * 3.0;
      ctx.globalAlpha = tw * 0.55;
      ctx.drawImage(glowSprite("#cfe1ff", 12), sx - sz, sy - sz, sz * 2, sz * 2);
    }
    ctx.restore();
    ctx.globalAlpha = 1;

    // Module orbs — one light per module, flashing on dispatch.
    hover.bodies.length = 0;
    hover.moons.length = 0;
    for (const ml of moduleLeaves) {
      const tgt = TARGETS[ml.ti];
      const sx = w2sX(ml.x), sy = w2sY(ml.y);
      const mFlash = (flashes[ml.ti + ":" + ml.mi] || 0) > now;
      const mHot = hover.moonTi === ml.ti && hover.moonMi === ml.mi;
      const base = 2.6 * Math.max(0.9, cam.z * 0.9);
      const r = mFlash ? base + 1.8 : mHot ? base + 1.4 : base;
      const pulse = REDUCED ? 0.7 : 0.55 + Math.sin(t * 1.2 + ml.ph) * 0.25;
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      ctx.globalAlpha = mFlash ? 1 : mHot ? 0.95 : pulse * 0.75;
      const gsz = r * (mFlash ? 6 : 4);
      ctx.drawImage(glowSprite(mFlash ? "#ffe9c4" : tgt.color, 22), sx - gsz, sy - gsz, gsz * 2, gsz * 2);
      ctx.restore();
      ctx.globalAlpha = 1;
      ctx.fillStyle = mFlash ? "#fff3d6" : mHot ? "#ffffff" : "#dbe7ff";
      ctx.beginPath();
      ctx.arc(sx, sy, r * 0.8, 0, Math.PI * 2);
      ctx.fill();
      if (mHot) {
        ctx.font = "9px Cascadia Mono, Consolas, monospace";
        ctx.textAlign = "left";
        ctx.fillStyle = mHot ? "rgba(255,255,255,0.95)" : "rgba(201,209,217,0.6)";
        ctx.fillText(tgt.moons[ml.mi], sx + r + 4, sy + 3);
      }
      hover.moons.push({ x: sx, y: sy, r: Math.max(r + 6, 10), ti: ml.ti, mi: ml.mi });
    }

    // Region anchors: labels on hover/flash/zoom + drop rings + hit zones.
    for (let ti = 0; ti < TARGETS.length; ti++) {
      const h = handles[ti];
      const sx = w2sX(h.x), sy = w2sY(h.y);
      hover.bodies.push({ x: sx, y: sy, r: h.r * cam.z, ti });
      const tFlash = (flashes["t:" + ti] || 0) > now;
      const hot = ti === hover.active || ti === selected;
      if (hot || tFlash) {
        ctx.font = "11px Cascadia Mono, Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillStyle = tFlash ? "rgba(255,233,196,0.9)"
                      : hot ? "rgba(230,240,255,0.95)" : "rgba(201,209,217,0.42)";
        const ly = ti === TGT_MEMORY ? sy + h.r * cam.z * 0.7 : sy - h.r * cam.z * 0.72;
        ctx.fillText(TARGETS[ti].layer, sx, ly);
      }
      if (hot) {
        ctx.strokeStyle = "rgba(150,195,255,0.30)";
        ctx.lineWidth = 1.2;
        ctx.setLineDash([3, 6]);
        ctx.beginPath();
        ctx.arc(sx, sy, h.r * cam.z * 0.72, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (dropTarget === ti) drawDropRing(sx, sy, h.r * cam.z * 0.78, t);
      if (DEBUG) {
        ctx.strokeStyle = "rgba(255,80,80,0.8)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(sx, sy, h.r * cam.z, 0, Math.PI * 2);
        ctx.stroke();
        ctx.font = "10px monospace";
        ctx.fillStyle = "#ff9090";
        ctx.fillText(TARGETS[ti].layer, sx, sy);
      }
    }

    // Heart label.
    ctx.font = "11px Cascadia Mono, Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(201,209,217,0.5)";
    ctx.fillText("ODIN", hx, hy + 30 * cam.z);

    // Light-drips: the painting's falling streams get moving company.
    maybeSpawnDrip(now, Math.random());
    drips = drips.filter((d) => now - d.born < d.life);
    ctx.save();
    ctx.globalCompositeOperation = "screen";
    for (const d of drips) {
      const k = (now - d.born) / d.life;
      const x = w2sX(fx2w(d.fx));
      const y0 = w2sY(fy2w(CANOPY_BOTTOM + k * 0.16));
      const len = 26 * cam.z;
      const g = ctx.createLinearGradient(0, y0 - len, 0, y0);
      g.addColorStop(0, "rgba(190,215,255,0)");
      g.addColorStop(1, "rgba(190,215,255," + (0.5 * Math.sin(Math.PI * k)).toFixed(3) + ")");
      ctx.strokeStyle = g;
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(x, y0 - len);
      ctx.lineTo(x, y0);
      ctx.stroke();
    }
    ctx.restore();

    // Ravens — HUGIN & MUNIN circling the crown.
    drawRavens(t, now);

    // Mist drifting over the lake at the painting's foot.
    drawMist(t);
  }

  function drawRavens(t, now) {
    if (REDUCED || !artReady) return;
    const crownX = 0, crownY = fy2w(0.16);
    for (const rv of ravens) {
      let wx, wy;
      if (now < rv.diveUntil) {
        const k = 1 - (rv.diveUntil - now) / 1800;
        const swoop = Math.sin(k * Math.PI);
        wx = crownX + Math.cos(t * rv.speed * 6 + rv.ph) * IMG_W * rv.rx * (1 - swoop * 0.8);
        wy = crownY * (1 - swoop) + handles[TGT_HEART].y * swoop;
      } else {
        wx = crownX + Math.cos(t * rv.speed * 2 + rv.ph) * IMG_W * rv.rx;
        wy = crownY + Math.sin(t * rv.speed * 4.6 + rv.ph) * IMG_H * rv.ry;
      }
      const sx = w2sX(wx), sy = w2sY(wy);
      const dir = -Math.sin(t * rv.speed * 2 + rv.ph) > 0 ? 1 : -1;
      const flap = Math.sin(t * 7 + rv.flap) * 0.9;
      const s = 8.5 * cam.z;
      ctx.save();
      ctx.translate(sx, sy);
      ctx.scale(dir, 1);
      ctx.strokeStyle = "rgba(10,12,22,0.92)";
      ctx.fillStyle = "rgba(10,12,22,0.92)";
      ctx.lineWidth = Math.max(1.3, s * 0.22);
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.ellipse(0, 0, s * 0.55, s * 0.22, 0.1, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.quadraticCurveTo(-s * 0.8, -s * (0.5 + flap * 0.5), -s * 1.5, -s * (0.15 + flap * 0.75));
      ctx.moveTo(0, 0);
      ctx.quadraticCurveTo(s * 0.5, -s * (0.55 + flap * 0.45), s * 1.15, -s * (0.2 + flap * 0.65));
      ctx.stroke();
      ctx.strokeStyle = "rgba(190,210,240,0.22)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(-s * 0.5, -s * 0.16);
      ctx.quadraticCurveTo(0, -s * 0.34, s * 0.45, -s * 0.12);
      ctx.stroke();
      ctx.restore();
    }
  }

  let mistBlobs = null;
  function drawMist(t) {
    if (!mistBlobs) {
      mistBlobs = [];
      for (let i = 0; i < 4; i++) {
        mistBlobs.push({
          fy: 0.84 + Math.random() * 0.10,
          sp: 3 + Math.random() * 6,
          ph: Math.random() * Math.PI * 2,
          sz: 110 + Math.random() * 160,
          a: 0.04 + Math.random() * 0.04,
        });
      }
    }
    for (const mb of mistBlobs) {
      const x = ((t * mb.sp + mb.ph * 120) % (W + mb.sz * 4)) - mb.sz * 2;
      const y = H * mb.fy;
      ctx.globalAlpha = mb.a;
      ctx.drawImage(glowSprite("#a8bcd9", 80), x - mb.sz, y - mb.sz * 0.4, mb.sz * 2, mb.sz * 0.8);
      ctx.globalAlpha = 1;
    }
  }

  function drawDropRing(sx, sy, r, t) {
    ctx.save();
    ctx.strokeStyle = "rgba(190,215,255,0.9)";
    ctx.lineWidth = 2;
    ctx.setLineDash([7, 7]);
    ctx.lineDashOffset = REDUCED ? 0 : -t * 26;
    ctx.beginPath();
    ctx.arc(sx, sy, r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  /* ── Color helpers ──────────────────────────────────────────── */
  function hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* ── Hover (regions + module orbs) ──────────────────────────── */
  const hover = { bodies: [], moons: [], active: -1, moonTi: -1, moonMi: -1 };
  function hitTest(x, y) {
    for (const m of hover.moons) {
      const dx = x - m.x, dy = y - m.y;
      if (dx * dx + dy * dy <= m.r * m.r) return { kind: "moon", ti: m.ti, mi: m.mi };
    }
    let best = null, bestD = Infinity;
    for (const b of hover.bodies) {
      const dx = x - b.x, dy = y - b.y;
      const d2 = dx * dx + dy * dy;
      if (d2 <= b.r * b.r && d2 < bestD) { bestD = d2; best = { kind: "target", ti: b.ti }; }
    }
    return best;
  }

  document.addEventListener("mousemove", (e) => {
    if (panning) return;
    const hit = hitTest(e.clientX, e.clientY);
    hover.active = -1; hover.moonTi = -1; hover.moonMi = -1;
    if (!hit) {
      bodyCard.classList.add("hidden");
      canvas.style.cursor = "default";
      return;
    }
    canvas.style.cursor = "pointer";
    let html = "";
    const tgt = TARGETS[hit.ti];
    if (hit.kind === "moon") {
      hover.moonTi = hit.ti; hover.moonMi = hit.mi;
      const m = tgt.moons[hit.mi];
      const sk = lastSkill[m] ? `<div class="card-skill">last: ${escapeHtml(lastSkill[m])}</div>` : "";
      html = `<div class="card-name">${m}</div>` +
             `<div class="card-layer">${tgt.layer} · ${tgt.where}</div>` + sk +
             `<div class="card-hint">click for dossier</div>`;
    } else {
      hover.active = hit.ti;
      const moonRows = tgt.moons.map((m) => {
        const sk = lastSkill[m] ? ` <span class="card-skill">· ${escapeHtml(lastSkill[m])}</span>` : "";
        return `<b>${m}</b>${sk}`;
      }).join("<br>");
      html = `<div class="card-name">${tgt.layer}</div>` +
             `<div class="card-layer">${tgt.where} · ${tgt.moons.length} module${tgt.moons.length > 1 ? "s" : ""}</div>` +
             `<div class="card-moons">${moonRows}</div>` +
             `<div class="card-hint">click for dossier · drop files to feed</div>`;
    }
    bodyCard.innerHTML = html;
    bodyCard.classList.remove("hidden");
    const cw = bodyCard.offsetWidth, ch = bodyCard.offsetHeight;
    let x = e.clientX + 18, y = e.clientY - ch / 2;
    if (x + cw > W - 12) x = e.clientX - cw - 18;
    y = Math.max(12, Math.min(H - ch - 12, y));
    bodyCard.style.left = x + "px";
    bodyCard.style.top = y + "px";
  }, { passive: true });

  /* ── Mouse: click to select; the scene itself never moves ───── */
  const panning = false;   // kept so the hover handler's guard reads the same
  canvas.addEventListener("mouseup", (e) => {
    if (e.button !== 0) return;
    const hit = hitTest(e.clientX, e.clientY);
    if (hit) { openDossier(hit.ti, hit.kind === "moon" ? hit.mi : -1); return; }
    if (e.target !== canvas) return;
    if (selected !== -1) { closeDossier(); return; }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.on_click) {
      try { window.pywebview.api.on_click(); } catch (_) {}
    }
  });

  /* ── Dossier panel ──────────────────────────────────────────── */
  const moduleInfoCache = {};
  function openDossier(ti, focusMi) {
    selected = ti;
    const tgt = TARGETS[ti];
    dossierTitle.textContent = tgt.layer;
    dossierLayer.textContent = tgt.where;
    dossierPurpose.textContent = tgt.drop;
    dossierBody.innerHTML = "";
    tgt.moons.forEach((m, mi) => {
      const row = document.createElement("div");
      row.className = "mod-row" + (mi === focusMi ? " focus" : "");
      row.innerHTML =
        `<div class="mod-head"><span class="mod-dot" style="background:${tgt.color}"></span>` +
        `<span class="mod-name">${m}</span>` +
        `<span class="mod-last">${lastSkill[m] ? escapeHtml(lastSkill[m]) : ""}</span></div>` +
        `<div class="mod-skills hidden"></div>`;
      row.querySelector(".mod-head").addEventListener("click", () => toggleModuleSkills(row, m));
      dossierBody.appendChild(row);
      if (mi === focusMi) toggleModuleSkills(row, m);
    });
    dossier.classList.remove("hidden");
    if (focusMi >= 0) {
      const el = dossierBody.children[focusMi];
      requestAnimationFrame(() => el.scrollIntoView({ block: "nearest" }));
    }
  }
  function closeDossier() {
    selected = -1;
    dossier.classList.add("hidden");
  }
  dossierClose.addEventListener("click", closeDossier);

  function refreshDossierSkills() {
    if (selected === -1) return;
    const tgt = TARGETS[selected];
    const rows = dossierBody.querySelectorAll(".mod-row");
    rows.forEach((row, mi) => {
      const m = tgt.moons[mi];
      const lastEl = row.querySelector(".mod-last");
      if (lastEl && lastSkill[m]) lastEl.textContent = lastSkill[m];
    });
  }

  function toggleModuleSkills(row, moduleName) {
    const box = row.querySelector(".mod-skills");
    if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    if (box.dataset.loaded) return;
    box.innerHTML = '<div class="mod-skill dim">loading skills…</div>';
    const render = (info) => {
      box.dataset.loaded = "1";
      if (!info || !info.skills || !info.skills.length) {
        box.innerHTML = '<div class="mod-skill dim">no skill data</div>';
        return;
      }
      box.innerHTML = info.skills.map((s) =>
        `<div class="mod-skill"><b>${escapeHtml(s.name)}</b>` +
        (s.description ? `<span>${escapeHtml(s.description)}</span>` : "") + `</div>`
      ).join("");
    };
    const cached = moduleInfoCache[moduleName];
    if (cached) { render(cached); return; }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_module_info) {
      window.pywebview.api.get_module_info(moduleName).then((json) => {
        let info = null;
        try { info = JSON.parse(json); } catch (_) {}
        moduleInfoCache[moduleName] = info;
        render(info);
      }).catch(() => render(null));
    } else {
      render(null);
    }
  }

  /* ── Command bar ────────────────────────────────────────────── */
  function openCmdbar() {
    cmdbar.classList.remove("hidden");
    requestAnimationFrame(() => cmdInput.focus());
  }
  function closeCmdbar() {
    cmdbar.classList.add("hidden");
    cmdInput.value = "";
    cmdInput.blur();
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!cmdbar.classList.contains("hidden")) { closeCmdbar(); return; }
      if (selected !== -1) closeDossier();
      return;
    }
    if (cmdbar.classList.contains("hidden") &&
        (e.key === "/" || (e.key === "Enter" && document.activeElement === body))) {
      e.preventDefault();
      openCmdbar();
    }
  });
  cmdInput.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { closeCmdbar(); return; }
    if (e.key !== "Enter") return;
    const text = cmdInput.value.trim();
    if (!text) { closeCmdbar(); return; }
    closeCmdbar();
    showToast("→ " + text, 2500);
    if (window.pywebview && window.pywebview.api && window.pywebview.api.submit_text) {
      try { window.pywebview.api.submit_text(text); } catch (_) {}
    }
  });

  /* ── Drag & drop: feed the tree ─────────────────────────────── */
  function nearestDropTarget(x, y) {
    let best = -1, bestD = Infinity;
    for (const b of hover.bodies) {
      const dx = x - b.x, dy = y - b.y;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d < bestD) { bestD = d; best = b.ti; }
    }
    if (best !== -1 && bestD <= Math.max(170 * cam.z, 130)) return best;
    return -1;   // open sky -> the roots keep everything
  }

  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth++;
    body.classList.add("dragging");
  });
  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) { body.classList.remove("dragging"); dropTarget = -1; }
  });
  window.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    dropTarget = nearestDropTarget(e.clientX, e.clientY);
  });
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    body.classList.remove("dragging");
    const target = dropTarget;
    dropTarget = -1;

    const tgt = target >= 0 ? TARGETS[target] : TARGETS[TGT_MEMORY];
    const layer = tgt.layer;
    const targetName = target === TGT_HEART ? "the heartwood"
                     : target === TGT_MEMORY || target === -1 ? "the roots"
                     : tgt.where;

    const files = [];
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      for (const f of e.dataTransfer.files) {
        files.push({ name: f.name, path: f.pywebviewFullPath || "" });
      }
    }
    let url = "";
    let text = "";
    try { url = e.dataTransfer.getData("text/uri-list") || ""; } catch (_) {}
    try { text = e.dataTransfer.getData("text/plain") || ""; } catch (_) {}
    if (!url && /^https?:\/\/\S+$/i.test(text.trim())) url = text.trim();
    if (url) url = url.split("\n")[0].trim();

    if (!files.length && !url && !text.trim()) {
      showToast("Nothing droppable detected.", 2600);
      return;
    }
    showToast("Feeding " + targetName + "…", 3200);
    flashes["t:" + (target === -1 ? TGT_MEMORY : target)] = performance.now() + 2600;

    const payload = JSON.stringify({ layer, files, url, text });
    if (window.pywebview && window.pywebview.api && window.pywebview.api.on_drop) {
      window.pywebview.api.on_drop(payload).then((ack) => {
        if (ack && ack !== "ok") showToast(String(ack), 4200);
      }).catch(() => showToast("Drop failed — see ODIN log.", 3600));
    } else {
      showToast("Bridge offline — drop ignored.", 3200);
    }
  });

  /* ── Main loop ──────────────────────────────────────────────── */
  let rafId = null;
  function loop(now) {
    draw(now);
    rafId = requestAnimationFrame(loop);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    } else if (!rafId) {
      rafId = requestAnimationFrame(loop);
    }
  });

  window.addEventListener("resize", resize);
  resize();
  setStateClass("idle");
  rafId = requestAnimationFrame(loop);
})();
