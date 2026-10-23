// Three.js renderer for the factory twin. Display only: it receives a derived
// playback view and never reads or writes anything else.
//
// Hardware policy: render on demand (the caller decides when), pixel ratio capped
// at 1.5, one 1024² shadow map, procedural image-based lighting generated once,
// no post-processing. Spindles and probes animate only while playback runs.

import * as THREE from "three";
import { OrbitControls } from "./vendor/three/OrbitControls.js";
import { RoomEnvironment } from "./vendor/three/RoomEnvironment.js";
import { STATE_COLORS, STATUS_COLORS, displayStatus, locate, unitTarget } from "./layout.js";
import {
  createFloor,
  createHall,
  createMachineModel,
  createStorage,
  createWorkpiece,
  mergeStatic,
} from "./models.js";

const TWEEN_MS = 650;
const STATUS_BY_COLOR = Object.fromEntries(
  Object.entries(STATUS_COLORS).map(([name, hex]) => [new THREE.Color(hex).getHexString(), name]),
);
// Signal tower convention: red = fault, green = cycle running, amber = stopped/idle.
const TOWER = {
  failed: "red",
  running: "green",
  idle: "amber",
  restored: "amber",
  maintenance: "amber",
};
const SCREEN = { failed: "#7f1d1d", running: "#14532d", restored: "#164e63" };

function ease(value) {
  return value < 0.5 ? 2 * value * value : 1 - (-2 * value + 2) ** 2 / 2;
}

export function createScene3D(container, overlay, layout) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "default" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.9;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#2a323d");
  scene.fog = new THREE.Fog("#2a323d", 38, 75);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environment = environment;
  scene.environmentIntensity = 0.35;

  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 200);
  camera.position.set(-0.5, 15.2, 15.2);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0.3, 0.6, 1.9);
  controls.maxPolarAngle = Math.PI * 0.45;
  controls.minDistance = 8;
  controls.maxDistance = 45;
  controls.enablePan = false;
  controls.update();

  scene.add(new THREE.HemisphereLight("#e8f0ff", "#3a3226", 0.5));
  const sun = new THREE.DirectionalLight("#fff6e8", 1.7);
  sun.position.set(-8, 16, 10);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -17, right: 17, top: 13, bottom: -13, near: 1, far: 50 });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun);

  createFloor(scene, layout);
  createHall(scene);
  createStorage(scene, layout);

  const labels = new Map();
  function addLabel(id, text, position, className) {
    const element = document.createElement("div");
    element.className = `scene-label ${className || ""}`;
    element.textContent = text;
    overlay.appendChild(element);
    labels.set(id, { element, position: new THREE.Vector3(...position) });
    return element;
  }
  function removeLabel(id) {
    labels.get(id)?.element.remove();
    labels.delete(id);
  }
  const { incoming, finished } = layout;
  addLabel("zone-incoming", incoming.label, [incoming.x, 0.05, incoming.z + 3.7], "zone");
  addLabel("zone-finished", finished.label, [finished.x, 0.05, finished.z + 3.7], "zone");

  // Machines -------------------------------------------------------------------
  const basic = (color) => new THREE.MeshBasicMaterial({ color });
  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const machineObjects = {};
  for (const [machineId, machine] of Object.entries(layout.machines)) {
    const model = createMachineModel(machine.kind);
    model.group.position.set(machine.x, 0, machine.z);
    scene.add(model.group);
    // Progress of the running attempt: elapsed / planned duration.
    const barBack = new THREE.Mesh(unitBox, basic("#0f172a"));
    barBack.scale.set(2.2, 0.1, 0.1);
    barBack.position.set(machine.x, 2.75, machine.z + 1.0);
    const barFill = new THREE.Mesh(unitBox, basic(STATUS_COLORS.running));
    barFill.scale.set(0.001, 0.13, 0.13);
    barFill.position.set(machine.x - 1.1, 2.75, machine.z + 1.0);
    scene.add(barBack, barFill);
    // Floor hazard zone (translucent disc + ring) shown while the machine is down.
    const hazard = new THREE.Group();
    const hazardMaterial = new THREE.MeshBasicMaterial({
      color: "#ef4444",
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
    });
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.7, 1.98, 56), hazardMaterial);
    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(1.7, 56),
      new THREE.MeshBasicMaterial({ color: "#ef4444", transparent: true, opacity: 0.22, depthWrite: false }),
    );
    for (const mesh of [ring, disc]) {
      mesh.rotation.x = -Math.PI / 2;
      hazard.add(mesh);
    }
    hazard.position.set(machine.x, 0.012, machine.z);
    hazard.visible = false;
    hazard.material = hazardMaterial; // blink target
    scene.add(hazard);
    for (const object of [barBack, barFill, hazard]) object.userData.dynamic = true;
    machineObjects[machineId] = {
      ...model,
      barBack,
      barFill,
      hazard,
      status: null,
      displayStatus: null,
      flashUntil: 0,
      label: addLabel(`machine-${machineId}`, machineId, [machine.x, 3.25, machine.z], "machine"),
    };
  }

  const mergedMeshes = mergeStatic(scene); // before units and arcs are added

  // Units ----------------------------------------------------------------------
  const unitObjects = {};
  function unitObject(key, descriptor) {
    if (unitObjects[key]) return unitObjects[key];
    const model = createWorkpiece(layout.products[descriptor.product_id]);
    model.group.visible = false;
    scene.add(model.group);
    unitObjects[key] = { ...model, zone: null, tween: null };
    return unitObjects[key];
  }

  // Reassignment arcs (origin machine -> current machine), kept until the
  // reassigned operation completes and labelled with the units they carry.
  const arcGroup = new THREE.Group();
  scene.add(arcGroup);
  let arcSignature = "";
  let arcLabelIds = [];
  function rebuildArcs(pairs) {
    const signature = pairs
      .map((pair) => `${pair.from}>${pair.to}:${pair.units.map((u) => u.key + u.step).join(",")}`)
      .join("|");
    if (signature === arcSignature) return;
    arcSignature = signature;
    for (const child of [...arcGroup.children]) {
      arcGroup.remove(child);
      child.geometry?.dispose();
    }
    arcLabelIds.forEach(removeLabel);
    arcLabelIds = [];
    for (const pair of pairs) {
      const from = layout.machines[pair.from];
      const to = layout.machines[pair.to];
      if (!from || !to) continue;
      const start = new THREE.Vector3(from.x, 3.0, from.z);
      const end = new THREE.Vector3(to.x, 3.0, to.z);
      const middle = start.clone().lerp(end, 0.5);
      middle.x += 2.6;
      middle.y += 2.0;
      const curve = new THREE.QuadraticBezierCurve3(start, middle, end);
      const material = new THREE.MeshStandardMaterial({
        color: STATE_COLORS.reassigned,
        emissive: STATE_COLORS.reassigned,
        emissiveIntensity: 0.8,
        transparent: true,
        opacity: 0.9,
      });
      const radius = 0.07 + 0.03 * Math.min(pair.count, 3);
      arcGroup.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 48, radius, 10, false), material));
      const cone = new THREE.Mesh(new THREE.ConeGeometry(0.24, 0.6, 16), material);
      cone.position.copy(curve.getPoint(1));
      cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), curve.getTangent(1).normalize());
      arcGroup.add(cone);
      const id = `arc-${pair.from}-${pair.to}`;
      const text = pair.units
        .map((unit) => `${unit.key} s${unit.step + 1}${unit.restart ? " restart" : ""}`)
        .join(", ");
      addLabel(id, `↪ ${pair.from}→${pair.to}: ${text}`, curve.getPoint(0.5).toArray(), "arc");
      arcLabelIds.push(id);
    }
  }

  let blinking = false;

  function update(view, now, descriptors) {
    blinking = false;
    for (const [machineId, objects] of Object.entries(machineObjects)) {
      const machine = view.machines[machineId];
      const status = displayStatus(machine);
      const color = STATUS_COLORS[status];
      objects.status = machine.status;
      objects.displayStatus = status;
      objects.statusMaterial.color.set(color);
      objects.statusMaterial.emissive.set(color);
      for (const [name, lamp] of Object.entries(objects.lamps)) {
        lamp.emissiveIntensity = TOWER[status] === name ? 2.4 : 0.05;
      }
      objects.screen.emissive.set(SCREEN[status] ?? "#1e3a5f");
      if (objects.workLight) objects.workLight.emissiveIntensity = machine.running ? 1.6 : 0;
      objects.hazard.visible = status === "failed";
      if (status === "failed") blinking = true;
      if (machine.completedNow) objects.flashUntil = now + 900;
      const progress = machine.running ? machine.running.progress : 0;
      objects.barBack.visible = Boolean(machine.running);
      objects.barFill.visible = Boolean(machine.running);
      objects.barFill.scale.x = Math.max(0.001, 2.2 * progress);
      objects.barFill.position.x = layout.machines[machineId].x - 1.1 + 1.1 * progress;
      objects.label.textContent = `${machineId} · ${status.toUpperCase()}`;
      objects.label.dataset.status = status;
    }
    const present = new Set(Object.keys(view.units));
    for (const [key, unit] of Object.entries(view.units)) {
      const objects = unitObject(key, descriptors[key]);
      const { x, y, z } = unitTarget(layout, unit, key, view);
      const target = new THREE.Vector3(x, y, z);
      objects.group.visible = true;
      if (objects.zone === null) {
        objects.group.position.copy(target);
      } else if (
        objects.group.position.distanceTo(target) > 0.01 &&
        (!objects.tween || !objects.tween.to.equals(target))
      ) {
        objects.tween = {
          from: objects.group.position.clone(),
          to: target,
          start: now,
          hop: objects.zone !== unit.zone ? 1.0 : 0.2,
        };
      }
      objects.zone = unit.zone;
      const opacity = unit.released ? 1 : 0.35;
      for (const material of objects.materials) material.opacity = opacity;
      // Raw stock looks rough; machined parts are brighter; QC tag when complete.
      const machined = unit.completed_steps > 0;
      objects.metal.roughness = machined ? 0.3 : 0.7;
      objects.metal.metalness = machined ? 0.85 : 0.55;
      objects.tag.visible = unit.total_steps > 0 && unit.completed_steps === unit.total_steps;
      const outline =
        unit.state === "interrupted"
          ? STATE_COLORS.interrupted
          : unit.reassigned_from
            ? STATE_COLORS.reassigned
            : unit.state === "queued_restart"
              ? STATE_COLORS.queued_restart
              : null;
      objects.outline.visible = outline !== null;
      if (outline) objects.outline.material.color.set(outline);
    }
    for (const [key, objects] of Object.entries(unitObjects)) {
      if (!present.has(key)) {
        objects.group.visible = false;
        objects.zone = null;
        objects.tween = null;
      }
    }
    rebuildArcs(view.reassignmentPairs);
  }

  function animating(now) {
    const tweening = Object.values(unitObjects).some((item) => item.tween);
    const flashing = Object.values(machineObjects).some((item) => item.flashUntil > now);
    return { tweening: tweening || flashing, blinking };
  }

  let lastNow = null;
  let lastRenderMs = 0;
  function render(now, playing = false) {
    const dt = lastNow === null ? 0 : Math.min(0.1, (now - lastNow) / 1000);
    lastNow = now;
    for (const objects of Object.values(unitObjects)) {
      const tween = objects.tween;
      if (!tween) continue;
      const progress = Math.min(1, (now - tween.start) / TWEEN_MS);
      const eased = ease(progress);
      objects.group.position.lerpVectors(tween.from, tween.to, eased);
      objects.group.position.y += Math.sin(Math.PI * eased) * tween.hop;
      if (progress >= 1) {
        objects.group.position.copy(tween.to);
        objects.tween = null;
      }
    }
    for (const objects of Object.values(machineObjects)) {
      const running = objects.displayStatus === "running";
      // Visual motion only while playback runs; a paused view renders nothing.
      if (playing && running && objects.spindle) objects.spindle.rotation.y += dt * 14;
      if (playing && running && objects.carriage) {
        objects.carriage.position.x = Math.sin(now / 700) * 0.7;
      }
      let intensity = objects.flashUntil > now ? 2.6 : 0.9;
      if (objects.displayStatus === "failed") {
        const on = Math.sin(now / 160) > 0;
        objects.lamps.red.emissiveIntensity = on ? 3.0 : 0.1;
        objects.hazard.material.opacity = on ? 0.95 : 0.55; // never fully fades
        intensity = on ? 1.4 : 0.4;
      }
      objects.statusMaterial.emissiveIntensity = intensity; // colour stays exact
    }
    const started = performance.now();
    renderer.render(scene, camera);
    lastRenderMs = performance.now() - started;
    placeLabels();
  }

  const projected = new THREE.Vector3();
  function placeLabels() {
    const width = renderer.domElement.clientWidth;
    const height = renderer.domElement.clientHeight;
    for (const label of labels.values()) {
      projected.copy(label.position).project(camera);
      label.element.style.display = projected.z < 1 ? "block" : "none";
      label.element.style.transform = `translate(-50%, -50%) translate(${
        ((projected.x + 1) / 2) * width
      }px, ${((1 - projected.y) / 2) * height}px)`;
    }
  }

  function resize() {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return false;
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = `${width}px`;
    renderer.domElement.style.height = `${height}px`;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    return true;
  }

  // Read the scene graph that is submitted to WebGL: status-strip material colours
  // and workpiece world positions (mapped back through the layout). Not pixels.
  function readBack() {
    const units = {};
    const problems = [];
    for (const [key, objects] of Object.entries(unitObjects)) {
      if (!objects.group.visible) continue;
      const { x, y, z } = objects.group.position;
      units[key] = { ...locate(layout, { x, y, z }), x, y, z };
      if (objects.tween) problems.push(`${key}: still moving`);
    }
    const machines = {};
    for (const [machineId, objects] of Object.entries(machineObjects)) {
      if (objects.status === null) continue; // not drawn yet
      const shown = STATUS_BY_COLOR[objects.statusMaterial.color.getHexString()];
      if (!shown) problems.push(`${machineId}: status colour not recognised`);
      machines[machineId] = { status: shown === "restored" ? "idle" : (shown ?? "unrecognized") };
    }
    return { units, machines, problems };
  }

  function info() {
    const gl = renderer.getContext();
    const debug = gl.getExtension("WEBGL_debug_renderer_info");
    return {
      kind: "webgl",
      renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : "masked",
      vendor: debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : "masked",
      pixel_ratio: renderer.getPixelRatio(),
      draw_calls_last_frame: renderer.info.render.calls,
      triangles_last_frame: renderer.info.render.triangles,
      geometries: renderer.info.memory.geometries,
      textures: renderer.info.memory.textures,
      // CPU time to submit the frame; WebGL completes asynchronously.
      last_submit_ms: Math.round(lastRenderMs * 10) / 10,
      shadows: renderer.shadowMap.enabled,
      static_meshes_merged: mergedMeshes,
    };
  }

  function dispose() {
    controls.dispose();
    environment.dispose();
    pmrem.dispose();
    renderer.dispose();
    renderer.domElement.remove();
    overlay.replaceChildren();
  }

  return { kind: "webgl", update, render, animating, resize, readBack, info, dispose, controls };
}
