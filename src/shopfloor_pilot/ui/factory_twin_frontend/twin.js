// ShopfloorPilot factory twin: playback engine, status cards and verification.
//
// Display-only. The component receives a read-only timeline from Python and
// never sends a value back (no setComponentValue), never requests the network
// and has no path to operational storage. One playback clock (state.index +
// state.time) drives the scene, the cards, the ticker and the header together.

import { buildLayout, displayStatus } from "./layout.js";

const SCHEMA = "shopfloor_pilot.factory_twin/1";
const SPEEDS = [15, 30, 60, 120, 300, 600, 1200];
const IDLE_MULTIPLIER = 20;
const VERIFY_SETTLE_MS = 1000; // > tween (650 ms) and completion flash (900 ms)
const STORAGE_PREFIX = "shopfloor-twin:";

const $ = (id) => document.getElementById(id);
const root = $("twin");
const sceneHost = $("scene");
const overlay = $("scene-labels");

const state = {
  payload: null,
  layout: null,
  descriptors: {},
  frames: [],
  signature: null,
  index: 0,
  time: 0,
  playing: false,
  speed: 120,
  hold: true,
  holdMs: 1600,
  autoplay: true,
  skipIdle: true,
  holdUntil: 0,
  flash: new Set(),
  rendererChoice: "auto",
  scene: null,
  sceneNote: "",
  lastWall: null,
  scheduled: false,
  timer: null,
  view: null,
  verification: { status: "pending", mismatches: [] },
  verifyTimer: null,
  optionsApplied: false,
  domKey: "",
};

// ----------------------------------------------------------- Streamlit shim

function post(type, data = {}) {
  if (window.parent !== window) {
    window.parent.postMessage({ isStreamlitMessage: true, type, ...data }, "*");
  }
}

window.addEventListener("message", (event) => {
  if (event.source !== window.parent) return;
  const data = event.data;
  if (data && data.type === "streamlit:render") receive(data.args || {});
});

let lastHeight = 0;
function reportHeight() {
  const height = Math.ceil(root.getBoundingClientRect().height) + 8;
  if (height !== lastHeight) {
    lastHeight = height;
    post("streamlit:setFrameHeight", { height });
  }
}
new ResizeObserver(reportHeight).observe(root);

function storage(action, key, value) {
  try {
    if (action === "get") return window.sessionStorage.getItem(STORAGE_PREFIX + key);
    window.sessionStorage.setItem(STORAGE_PREFIX + key, value);
  } catch (_error) {
    return null;
  }
  return null;
}

function preference(action, key, value) {
  try {
    if (action === "get") return window.localStorage.getItem(STORAGE_PREFIX + key);
    window.localStorage.setItem(STORAGE_PREFIX + key, value);
  } catch (_error) {
    return null;
  }
  return null;
}

// --------------------------------------------------------------- receiving

function applyOptions(options) {
  if (state.optionsApplied) return; // later reruns keep the viewer's choices
  state.optionsApplied = true;
  if (SPEEDS.includes(options.speed)) state.speed = options.speed;
  if (typeof options.hold_seconds === "number") state.holdMs = options.hold_seconds * 1000;
  if (typeof options.autoplay === "boolean") state.autoplay = options.autoplay;
  const stored = preference("get", "renderer");
  const requested = stored || options.renderer;
  if (["auto", "webgl", "canvas2d"].includes(requested)) state.rendererChoice = requested;
}

function receive(args) {
  applyOptions(args.options || {});
  const payload = args.payload;
  if (!payload || payload.schema !== SCHEMA || !Array.isArray(payload.frames)) {
    showFatal("The visualization payload is missing or uses an unsupported schema.");
    return;
  }
  const signature = `${payload.run_id}:${payload.verification.state_version}:${payload.frames.length}`;
  if (signature === state.signature) return;
  const sameRun = state.payload && state.payload.run_id === payload.run_id;
  const wasAtEnd = state.index >= state.frames.length - 1;
  const previousVersion = state.frames[state.index]?.state_version ?? -1;
  const previousTime = state.time;

  state.payload = payload;
  state.frames = payload.frames;
  state.signature = signature;
  state.descriptors = Object.fromEntries(payload.units.map((item) => [item.key, item]));
  const last = state.frames.length - 1;

  if (!sameRun) {
    state.layout = buildLayout(payload);
    mountScene();
    buildCards();
    state.playing = false;
    const stored = Number.parseInt(storage("get", payload.run_id) ?? "", 10);
    const finalVersion = state.frames[last].state_version;
    if (Number.isFinite(stored) && stored < finalVersion) {
      seekVersion(stored);
      if (state.autoplay) play();
    } else {
      seekIndex(last);
    }
  } else {
    // Frames are a deterministic prefix of the stream: keep the cursor where
    // the viewer was, then continue into the newly committed groups.
    seekVersion(previousVersion);
    state.time = Math.max(state.frames[state.index].time, Math.min(previousTime, nextTime()));
    if (state.autoplay && wasAtEnd && state.index < last) play();
  }
  $("python-check").textContent = payload.verification.passed
    ? `Server replay check: event replay reproduces SQLite v${payload.verification.state_version} (hash ${payload.verification.snapshot_hash.slice(0, 12)}…)`
    : `Server replay check FAILED: ${payload.verification.mismatches.join("; ") || "hash mismatch"}`;
  $("python-check").dataset.passed = String(payload.verification.passed);
  syncControls();
  requestDraw();
}

function showFatal(message) {
  $("fatal").textContent = message;
  $("fatal").hidden = false;
}

// ----------------------------------------------------------------- renderer

function webglAvailable() {
  try {
    const probe = document.createElement("canvas");
    return Boolean(probe.getContext("webgl2") || probe.getContext("webgl"));
  } catch (_error) {
    return false;
  }
}

async function mountScene() {
  const choice = state.rendererChoice;
  state.scene?.dispose();
  state.scene = null;
  let note = "";
  if (choice !== "canvas2d" && webglAvailable()) {
    try {
      const { createScene3D } = await import("./scene3d.js");
      state.scene = createScene3D(sceneHost, overlay, state.layout);
      state.scene.controls.addEventListener("change", requestDraw);
    } catch (error) {
      note = `WebGL renderer failed (${error.message}); using the 2D canvas renderer.`;
    }
  } else if (choice !== "canvas2d") {
    note = "WebGL is unavailable in this browser; using the 2D canvas renderer.";
  }
  if (!state.scene) {
    const { createScene2D } = await import("./scene2d.js");
    state.scene = createScene2D(sceneHost, overlay, state.layout);
  }
  state.scene.resize();
  const info = state.scene.info();
  state.sceneNote = note;
  $("renderer-info").textContent =
    `${note ? `${note} ` : ""}Renderer: ${info.kind}${info.renderer ? ` · ${info.renderer}` : ""}`;
  root.dataset.renderer = info.kind;
  if (state.index === state.frames.length - 1 && !state.playing) scheduleVerification();
  requestDraw();
}

new ResizeObserver(() => {
  if (!state.scene?.resize()) return;
  // Resizing clears the canvas; redraw now so a blank frame is never presented.
  if (state.view) state.scene.render(performance.now(), state.playing);
  requestDraw();
}).observe(sceneHost);

// ----------------------------------------------------------------- playback

function nextTime() {
  const next = state.frames[state.index + 1];
  return next ? next.time : state.frames[state.index].time;
}

function seekIndex(index) {
  const bounded = Math.max(0, Math.min(state.frames.length - 1, index));
  state.index = bounded;
  state.time = state.frames[bounded].time;
  state.holdUntil = 0;
  afterSeek();
}

function seekVersion(version) {
  let index = 0;
  state.frames.forEach((frame, position) => {
    if (frame.state_version <= version) index = position;
  });
  seekIndex(index);
}

function seekTime(time) {
  let index = 0;
  state.frames.forEach((frame, position) => {
    if (frame.time <= time) index = position;
  });
  state.index = index;
  const next = state.frames[index + 1];
  state.time = next ? Math.min(time, next.time) : state.frames[index].time;
  afterSeek();
}

function afterSeek() {
  state.flash.clear();
  resetVerification();
  if (state.index === state.frames.length - 1 && !state.playing) scheduleVerification();
  requestDraw();
}

function play() {
  if (state.index >= state.frames.length - 1) seekIndex(0);
  state.playing = true;
  state.lastWall = null;
  resetVerification();
  syncControls();
  requestDraw();
}

function pause() {
  state.playing = false;
  syncControls();
  requestDraw();
}

function anyRunning(frame) {
  return Object.values(frame.machines).some((item) => item.running);
}

function enterFrame(frame, now) {
  for (const event of frame.events) {
    if (event.type === "operation_completed" && event.machine_id) state.flash.add(event.machine_id);
  }
  if (frame.key && state.hold) state.holdUntil = now + state.holdMs;
}

function advance(wallMs, now) {
  if (now < state.holdUntil) return;
  let remaining = wallMs;
  while (remaining > 0 || state.time >= nextTime()) {
    const next = state.frames[state.index + 1];
    if (!next) {
      finish();
      return;
    }
    const idle = state.skipIdle && !anyRunning(state.frames[state.index]);
    const rate = (state.speed * (idle ? IDLE_MULTIPLIER : 1)) / 1000; // sim s per wall ms
    const needed = (next.time - state.time) / rate;
    if (needed > remaining) {
      state.time += remaining * rate;
      return;
    }
    remaining -= needed;
    state.time = next.time;
    state.index += 1;
    enterFrame(next, now);
    if (state.hold && next.key) return;
  }
}

function finish() {
  state.playing = false;
  state.time = state.frames[state.index].time;
  storage("set", state.payload.run_id, String(state.frames[state.index].state_version));
  syncControls();
  scheduleVerification();
}

// -------------------------------------------------------------- derivation

function derive() {
  const frame = state.frames[state.index];
  const time = state.time;
  const machines = {};
  for (const [machineId, machine] of Object.entries(frame.machines)) {
    let running = null;
    if (machine.running) {
      const { start_seconds: start, duration_seconds: duration } = machine.running;
      running = {
        ...machine.running,
        progress: Math.max(0, Math.min(1, (time - start) / duration)),
        remaining_seconds: Math.max(0, start + duration - time),
      };
    }
    machines[machineId] = { ...machine, running, completedNow: state.flash.has(machineId) };
  }
  const units = {};
  const pairs = {};
  for (const [key, unit] of Object.entries(frame.units)) {
    const descriptor = state.descriptors[key];
    units[key] = { ...unit, released: time >= descriptor.release_time_seconds };
    if (unit.reassigned_from && unit.machine_id) {
      const pair = `${unit.reassigned_from}>${unit.machine_id}`;
      pairs[pair] = pairs[pair] || [];
      pairs[pair].push({ key, step: unit.step, restart: unit.reassigned_restart, zone: unit.zone });
    }
  }
  const reassignmentPairs = Object.entries(pairs).map(([pair, items]) => {
    const [from, to] = pair.split(">");
    return { from, to, count: items.length, units: items };
  });
  return {
    index: state.index,
    time,
    state_version: frame.state_version,
    plan_revision: frame.plan_revision,
    machines,
    units,
    reassignmentPairs,
    frame,
  };
}

// ------------------------------------------------------------------ drawing

function requestDraw() {
  if (state.scheduled) return;
  state.scheduled = true;
  window.clearTimeout(state.timer);
  window.requestAnimationFrame(tick);
}

function tick(now) {
  state.scheduled = false;
  if (!state.payload || !state.scene) return;
  const wall = state.lastWall === null ? 0 : Math.min(250, now - state.lastWall);
  state.lastWall = now;
  const visible = root.offsetWidth > 0 && document.visibilityState === "visible";
  if (!visible) {
    // Hidden tab: hold the clock so playback starts when the viewer looks.
    state.lastWall = null;
    state.timer = window.setTimeout(requestDraw, 400);
    return;
  }
  if (state.playing) advance(wall, now);
  const view = derive();
  state.view = view;
  state.scene.update(view, now, state.descriptors);
  state.flash.clear();
  state.scene.render(now, state.playing);
  renderDom(view, now);
  const { tweening, blinking } = state.scene.animating(now);
  if (state.playing || tweening || now < state.holdUntil) {
    requestDraw();
  } else if (blinking) {
    state.lastWall = null;
    state.timer = window.setTimeout(requestDraw, 250); // low-rate blink, not a busy loop
  } else {
    state.lastWall = null;
  }
}

function clock(seconds) {
  const value = Math.floor(seconds);
  const hours = String(Math.floor(value / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((value % 3600) / 60)).padStart(2, "0");
  const secs = String(value % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function bannerFor(frame) {
  const lines = frame.events
    .filter((item) => item.type !== "operation_started" && item.type !== "operation_completed")
    .map((item) => item.label);
  if (frame.reassignments.length) {
    lines.push(
      `${frame.reassignments.length} changed assignment(s): ` +
        frame.reassignments
          .map(
            (item) =>
              `${item.unit} step ${item.step + 1} ${item.from ?? "new"}→${item.to}${item.restart ? " (restart)" : ""}`,
          )
          .join(", "),
    );
  }
  return lines.length ? { time: frame.time, lines } : null;
}

function buildCards() {
  const host = $("cards");
  host.replaceChildren();
  for (const machine of state.payload.machines) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.machine = machine.machine_id;
    card.innerHTML = `
      <header><span class="machine-id"></span><span class="pill"></span></header>
      <div class="machine-name"></div>
      <div class="current"></div>
      <div class="bar"><i></i></div>
      <div class="queue"></div>
      <div class="held"></div>`;
    card.querySelector(".machine-id").textContent = machine.machine_id;
    card.querySelector(".machine-name").textContent =
      `${machine.display_name} · ${machine.capabilities.join(", ")}`;
    host.appendChild(card);
  }
}

function unitLabel(key, step) {
  return step === null || step === undefined ? key : `${key} s${step + 1}`;
}

function renderDom(view, now) {
  const frame = view.frame;
  const last = state.frames.length - 1;
  const holding = now < state.holdUntil;
  const domKey = `${view.index}:${Math.floor(view.time)}:${state.playing}:${holding}:${state.verification.status}`;
  root.dataset.playbackTime = String(view.time);
  root.dataset.frameIndex = String(view.index);
  root.dataset.stateVersion = String(view.state_version);
  if (domKey === state.domKey) return;
  state.domKey = domKey;

  $("time").textContent = `${clock(view.time)} (${Math.floor(view.time).toLocaleString()} s)`;
  $("version").textContent = `Playback v${view.state_version} · plan r${view.plan_revision} · live v${state.payload.verification.state_version}`;
  const live = view.index === last;
  $("mode").textContent = live ? "LIVE STATE" : state.playing ? "PLAYING" : "REPLAY";
  $("mode").dataset.mode = live ? "live" : "replay";
  $("scrub").max = String(state.frames[last].time);
  $("scrub").value = String(Math.floor(view.time));
  $("frame-pos").textContent = `event group ${view.index}/${last}`;

  for (const card of $("cards").children) {
    const machineId = card.dataset.machine;
    const machine = view.machines[machineId];
    const status = displayStatus(machine);
    card.dataset.status = machine.status;
    card.dataset.display = status;
    card.dataset.restored = String(Boolean(machine.restored));
    card.dataset.time = String(view.time);
    card.dataset.runningAttempt = machine.running ? machine.running.attempt_id : "";
    card.dataset.queue = machine.queue.join(",");
    card.dataset.held = machine.held.join(",");
    card.querySelector(".pill").textContent =
      status === "restored" ? "RESTORED · IDLE" : status.toUpperCase();
    const current = card.querySelector(".current");
    const bar = card.querySelector(".bar i");
    if (machine.running) {
      const unit = view.units[machine.running.unit];
      const product = state.layout.products[state.descriptors[machine.running.unit].product_id];
      const routeStep = product?.route[machine.running.step];
      current.textContent =
        `${machine.running.unit} · step ${machine.running.step + 1}/${unit.total_steps}` +
        `${routeStep ? ` ${routeStep.name}` : ""}` +
        `${machine.running.attempt_number > 1 ? ` · restart attempt ${machine.running.attempt_number}` : ""}` +
        `${unit.reassigned_from ? ` · ↪ reassigned from ${unit.reassigned_from}` : ""}` +
        ` · ${Math.round(machine.running.progress * 100)}% · ${Math.ceil(machine.running.remaining_seconds)} s left`;
      bar.style.width = `${machine.running.progress * 100}%`;
    } else {
      current.textContent =
        status === "failed"
          ? "Down — no work can start here until explicit maintenance confirmation"
          : status === "restored"
            ? "Back in service after explicit maintenance confirmation"
            : "No running operation";
      bar.style.width = "0%";
    }
    const queued = machine.queue.map((key) => {
      const unit = view.units[key];
      const marks = [];
      if (unit.reassigned_from) marks.push(`↪ from ${unit.reassigned_from}`);
      if (unit.state === "queued_restart") marks.push("restart");
      if (!unit.released) marks.push("not released");
      return `${unitLabel(key, unit.step)}${marks.length ? ` (${marks.join(", ")})` : ""}`;
    });
    card.querySelector(".queue").textContent = queued.length
      ? `Planned queue: ${queued.slice(0, 4).join(" · ")}${queued.length > 4 ? ` · +${queued.length - 4}` : ""}`
      : "Planned queue: empty";
    const held = machine.held.map((key) => unitLabel(key, view.units[key].step));
    card.querySelector(".held").textContent = held.length
      ? `Interrupted here, awaiting replan: ${held.join(", ")}`
      : "";
  }

  // Key-event callout: highlighted on the key frame itself (and while holding),
  // otherwise the most recent key event, explicitly labelled as past.
  const callout = $("banner");
  let keyIndex = view.index;
  while (keyIndex > 0 && !(state.frames[keyIndex].key && bannerFor(state.frames[keyIndex]))) {
    keyIndex -= 1;
  }
  const keyFrame = state.frames[keyIndex];
  const content = keyIndex > 0 ? bannerFor(keyFrame) : null;
  const current = keyIndex === view.index && (holding || !state.playing || view.time === keyFrame.time);
  callout.dataset.mode = content ? (current ? "current" : "past") : "none";
  const heading = content
    ? `${current ? "Key event" : "Last key event"} at t=${content.time.toLocaleString()} s`
    : "No failure, replan or restoration recorded up to this time";
  callout.replaceChildren(
    ...[heading, ...(content ? content.lines : [])].map((line, position) => {
      const element = document.createElement(position === 0 ? "strong" : "div");
      element.textContent = line;
      return element;
    }),
  );

  const items = [];
  for (let index = view.index; index >= 0 && items.length < 8; index -= 1) {
    for (const event of [...state.frames[index].events].reverse()) {
      if (items.length >= 8) break;
      items.push({ event, current: index === view.index, key: state.frames[index].key });
    }
  }
  $("ticker").replaceChildren(
    ...items.map(({ event, current }) => {
      const element = document.createElement("li");
      element.className = `${current ? "current" : ""} ${event.type}`;
      element.textContent = `${event.time.toLocaleString()} s · ${event.label}`;
      return element;
    }),
  );

  const badge = $("verify");
  badge.dataset.status = state.verification.status;
  badge.textContent =
    state.verification.status === "match"
      ? `✓ Visual projection matches authoritative SQLite state v${state.payload.verification.state_version} at ${state.payload.authoritative.simulated_time_seconds.toLocaleString()} s`
      : state.verification.status === "mismatch"
        ? `✗ Visual projection differs from authoritative state: ${state.verification.mismatches.slice(0, 4).join("; ")}`
        : "Verification runs when playback reaches the latest committed state";
  root.dataset.verification = state.verification.status;
  syncControls();
}

// ------------------------------------------------------------- verification

function resetVerification() {
  window.clearTimeout(state.verifyTimer);
  state.verification = { status: "pending", mismatches: [] };
}

function scheduleVerification() {
  window.clearTimeout(state.verifyTimer);
  state.verifyTimer = window.setTimeout(() => {
    verifyNow();
    requestDraw();
  }, VERIFY_SETTLE_MS);
}

// Only fields that the view actually displays are compared. A unit's machine is
// shown only on a machine, in a queue or in a held slot.
const PLACED_ON_MACHINE = new Set(["machine", "queue", "held"]);
const PILL_STATUS = {
  IDLE: "idle",
  "RESTORED · IDLE": "idle",
  RUNNING: "running",
  FAILED: "failed",
  MAINTENANCE: "maintenance",
};

function subset(projection) {
  const machines = {};
  for (const [machineId, machine] of Object.entries(projection.machines)) {
    machines[machineId] = {
      status: machine.status,
      running_unit: machine.running ? machine.running.unit : "",
      queue: machine.queue.join(","),
      held: [...machine.held].sort().join(","),
    };
  }
  const units = {};
  for (const [key, unit] of Object.entries(projection.units)) {
    units[key] = {
      zone: unit.zone,
      machine_id: PLACED_ON_MACHINE.has(unit.zone) ? unit.machine_id : null,
    };
  }
  return {
    state_version: projection.state_version,
    plan_revision: projection.plan_revision,
    simulated_time_seconds: projection.simulated_time_seconds,
    machines,
    units,
  };
}

// Build the projection from rendered output only: the renderer's read-back (2D
// canvas pixels and drawn coordinates; 3D scene-graph materials and positions),
// the cards' visible text and the header text. Nothing is copied from the view.
function displayedProjection() {
  const placed = state.scene.readBack();
  const problems = [...placed.problems];
  const machines = {};
  for (const card of $("cards").children) {
    const machineId = card.dataset.machine;
    const cardStatus = PILL_STATUS[card.querySelector(".pill").textContent] ?? "unrecognized";
    const sceneStatus = placed.machines[machineId]?.status ?? "not drawn";
    if (cardStatus !== sceneStatus) {
      problems.push(`${machineId}: card shows ${cardStatus}, scene shows ${sceneStatus}`);
    }
    const running = card.querySelector(".current").textContent.match(/^(\S+#\d+) · step /);
    machines[machineId] = {
      status: cardStatus,
      running_unit: running ? running[1] : "",
      queue: [],
      held: [],
    };
  }
  const units = {};
  const occupied = new Map();
  for (const [key, placement] of Object.entries(placed.units)) {
    units[key] = {
      zone: placement.zone,
      machine_id: PLACED_ON_MACHINE.has(placement.zone) ? placement.machine_id : null,
    };
    const spot = [placement.x, placement.y ?? 0, placement.z].map((v) => v.toFixed(2)).join(",");
    if (occupied.has(spot)) problems.push(`${key} is drawn on top of ${occupied.get(spot)}`);
    occupied.set(spot, key);
    const machine = machines[placement.machine_id];
    if (placement.zone === "queue" && machine) machine.queue.push([placement.slot, key]);
    if (placement.zone === "held" && machine) machine.held.push(key);
  }
  for (const machine of Object.values(machines)) {
    machine.queue = machine.queue
      .sort((a, b) => a[0] - b[0])
      .map((item) => item[1])
      .join(",");
    machine.held = machine.held.sort().join(",");
  }
  const header = $("version").textContent.match(/Playback v(\d+) · plan r(\d+)/);
  const seconds = $("time").textContent.match(/\(([^)]*) s\)/);
  return {
    projection: {
      state_version: header ? Number(header[1]) : null,
      plan_revision: header ? Number(header[2]) : null,
      simulated_time_seconds: seconds ? Number(seconds[1].replace(/\D/g, "")) : null,
      machines,
      units,
    },
    problems,
  };
}

function differences(left, right, path = "") {
  if (typeof left !== "object" || left === null || typeof right !== "object" || right === null) {
    return left === right ? [] : [`${path || "value"}: ${JSON.stringify(left)} ≠ ${JSON.stringify(right)}`];
  }
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].sort().flatMap((key) => differences(left[key], right[key], path ? `${path}.${key}` : key));
}

function verifyNow() {
  const last = state.frames.length - 1;
  const drawnLatest = state.view && state.view.frame === state.frames[last];
  if (!state.payload || !state.scene || !drawnLatest || root.offsetWidth === 0) {
    // Latest state not drawn yet (for example mounted inside a hidden tab): retry.
    if (state.payload && state.index === last) scheduleVerification();
    return state.verification;
  }
  if (state.index !== state.frames.length - 1) {
    state.verification = { status: "pending", mismatches: [] };
    return state.verification;
  }
  const displayed = displayedProjection();
  const mismatches = [
    ...displayed.problems,
    ...differences(displayed.projection, subset(state.payload.authoritative)),
  ];
  state.verification = { status: mismatches.length ? "mismatch" : "match", mismatches };
  state.domKey = "";
  return state.verification;
}

// ----------------------------------------------------------------- controls

function syncControls() {
  $("play").textContent = state.playing ? "❚❚ Pause" : "▶ Play";
  $("speed").value = String(state.speed);
  $("hold").checked = state.hold;
  $("autoplay").checked = state.autoplay;
  $("skip-idle").checked = state.skipIdle;
  $("renderer").value = state.rendererChoice;
}

function bindControls() {
  const speed = $("speed");
  speed.replaceChildren(
    ...SPEEDS.map((value) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `${value}× (${value} sim s / s)`;
      return option;
    }),
  );
  $("play").addEventListener("click", () => (state.playing ? pause() : play()));
  $("restart").addEventListener("click", () => {
    seekIndex(0);
    play();
  });
  $("prev").addEventListener("click", () => {
    pause();
    seekIndex(state.time > state.frames[state.index].time ? state.index : state.index - 1);
  });
  $("next").addEventListener("click", () => {
    pause();
    seekIndex(state.index + 1);
  });
  $("live").addEventListener("click", () => {
    pause();
    seekIndex(state.frames.length - 1);
  });
  speed.addEventListener("change", () => {
    state.speed = Number(speed.value);
  });
  $("hold").addEventListener("change", (event) => {
    state.hold = event.target.checked;
    if (!state.hold) state.holdUntil = 0;
  });
  $("autoplay").addEventListener("change", (event) => {
    state.autoplay = event.target.checked;
  });
  $("skip-idle").addEventListener("change", (event) => {
    state.skipIdle = event.target.checked;
  });
  $("scrub").addEventListener("input", (event) => {
    pause();
    seekTime(Number(event.target.value));
  });
  $("renderer").addEventListener("change", (event) => {
    state.rendererChoice = event.target.value;
    preference("set", "renderer", state.rendererChoice);
    if (state.layout) mountScene();
  });
}

bindControls();

// Read-only hooks for automated browser checks and the offline harness page.
window.shopfloorTwin = {
  render: receive,
  play,
  pause,
  seekIndex,
  seekTime,
  verifyNow,
  setSpeed: (value) => {
    state.speed = value;
  },
  setHold: (value) => {
    state.hold = value;
  },
  status: () => ({
    index: state.index,
    frames: state.frames.length,
    time: state.time,
    playing: state.playing,
    speed: state.speed,
    renderer: state.scene?.info(),
    verification: state.verification,
    view: state.view && {
      state_version: state.view.state_version,
      machines: Object.fromEntries(
        Object.entries(state.view.machines).map(([id, item]) => [
          id,
          { status: item.status, display: displayStatus(item), running: item.running?.attempt_id ?? null },
        ]),
      ),
    },
    placed: state.scene?.readBack(),
  }),
};

post("streamlit:componentReady", { apiVersion: 1 });
