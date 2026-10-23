// Procedural models for the 3D factory twin. Everything is built from primitive
// geometry at runtime; no external model, texture or HDR file is loaded.
//
// Equipment shapes are generic illustrations (a vertical machining centre, a
// bridge-type coordinate measuring machine). Nothing here adds a manufacturing
// fact: there are no conveyors, vehicles or people because transport and
// operators are not part of the model.

import * as THREE from "three";
import { mergeGeometries } from "./vendor/three/BufferGeometryUtils.js";
import { RoundedBoxGeometry } from "./vendor/three/RoundedBoxGeometry.js";
import { STATUS_COLORS, WORK_HEIGHT } from "./layout.js";

const box = new THREE.BoxGeometry(1, 1, 1);
const cylinder = new THREE.CylinderGeometry(1, 1, 1, 24);

const PAINT = {
  body: new THREE.MeshStandardMaterial({ color: "#dfe3e8", roughness: 0.45, metalness: 0.15 }),
  trim: new THREE.MeshStandardMaterial({ color: "#2f3742", roughness: 0.55, metalness: 0.3 }),
  steel: new THREE.MeshStandardMaterial({ color: "#a7afb8", roughness: 0.3, metalness: 0.85 }),
  dark: new THREE.MeshStandardMaterial({ color: "#1c2128", roughness: 0.6, metalness: 0.4 }),
  glass: new THREE.MeshStandardMaterial({
    color: "#cfe8ff",
    roughness: 0.05,
    metalness: 0.1,
    transparent: true,
    opacity: 0.16,
    depthWrite: false,
  }),
  granite: new THREE.MeshStandardMaterial({ color: "#3a3d42", roughness: 0.35, metalness: 0.05 }),
  rackUpright: new THREE.MeshStandardMaterial({ color: "#1d4ed8", roughness: 0.5, metalness: 0.4 }),
  rackBeam: new THREE.MeshStandardMaterial({ color: "#ea580c", roughness: 0.5, metalness: 0.35 }),
  wood: new THREE.MeshStandardMaterial({ color: "#b08452", roughness: 0.85, metalness: 0 }),
  wall: new THREE.MeshStandardMaterial({ color: "#5b636d", roughness: 0.9, metalness: 0.05 }),
  decking: new THREE.MeshStandardMaterial({ color: "#6b7280", roughness: 0.6, metalness: 0.6 }),
  column: new THREE.MeshStandardMaterial({ color: "#4b5563", roughness: 0.6, metalness: 0.5 }),
  window: new THREE.MeshStandardMaterial({
    color: "#c7dcf0",
    emissive: "#9fc2e6",
    emissiveIntensity: 0.55,
    roughness: 0.2,
  }),
  paintYellow: new THREE.MeshStandardMaterial({ color: "#facc15", roughness: 0.7 }),
  paintWhite: new THREE.MeshStandardMaterial({ color: "#e5e7eb", roughness: 0.7 }),
};

function part(geometry, material, size, position, parent, { cast = true, receive = true } = {}) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.scale.set(...size);
  mesh.position.set(...position);
  mesh.castShadow = cast;
  mesh.receiveShadow = receive;
  parent.add(mesh);
  return mesh;
}

function rounded(width, height, depth, radius, material, position, parent) {
  const mesh = new THREE.Mesh(new RoundedBoxGeometry(width, height, depth, 3, radius), material);
  mesh.position.set(...position);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

// Three-segment signal tower: red / amber / green from top to bottom.
function stackLight(parent, position) {
  const group = new THREE.Group();
  group.position.set(...position);
  part(cylinder, PAINT.trim, [0.025, 0.35, 0.025], [0, 0.175, 0], group);
  const lamps = {};
  ["green", "amber", "red"].forEach((name, index) => {
    const color = { red: "#ef4444", amber: "#f59e0b", green: "#22c55e" }[name];
    const material = new THREE.MeshStandardMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.05,
      roughness: 0.25,
      transparent: true,
      opacity: 0.92,
    });
    part(cylinder, material, [0.12, 0.17, 0.12], [0, 0.44 + index * 0.175, 0], group, {
      cast: false,
    });
    lamps[name] = material;
  });
  part(cylinder, PAINT.trim, [0.125, 0.03, 0.125], [0, 0.97, 0], group);
  parent.add(group);
  return lamps;
}

function statusParts(parent, stripPosition, stripWidth) {
  // LED status strip: its material colour is exactly STATUS_COLORS[status]; the
  // end-of-playback check reads it back from the scene graph.
  const statusMaterial = new THREE.MeshStandardMaterial({
    color: STATUS_COLORS.idle,
    emissive: STATUS_COLORS.idle,
    emissiveIntensity: 0.9,
    roughness: 0.3,
  });
  part(box, statusMaterial, [stripWidth, 0.1, 0.04], stripPosition, parent, { cast: false });
  return statusMaterial;
}

// Vertical machining centre (generic). Origin at floor centre; front faces +z.
function machiningCentre() {
  const group = new THREE.Group();
  part(box, PAINT.trim, [2.6, 0.25, 2.3], [0, 0.125, 0], group);
  // Enclosure walls, front lower panel, header, glass door and glass roof window.
  rounded(2.4, 1.9, 0.12, 0.04, PAINT.body, [0, 1.2, -0.94], group);
  rounded(0.12, 1.9, 2.0, 0.04, PAINT.body, [-1.14, 1.2, 0], group);
  rounded(0.12, 1.9, 2.0, 0.04, PAINT.body, [1.14, 1.2, 0], group);
  rounded(2.4, 0.5, 0.12, 0.04, PAINT.body, [0, 0.5, 0.94], group);
  rounded(2.4, 0.16, 0.12, 0.03, PAINT.trim, [0, 2.07, 0.94], group);
  part(box, PAINT.glass, [1.7, 1.28, 0.03], [-0.15, 1.39, 0.96], group, { cast: false });
  part(box, PAINT.trim, [0.06, 1.3, 0.06], [0.72, 1.39, 0.98], group);
  part(box, PAINT.steel, [0.04, 0.5, 0.06], [0.62, 1.4, 1.02], group); // door handle
  part(box, PAINT.trim, [2.4, 0.08, 0.1], [0, 2.18, -0.9], group);
  part(box, PAINT.trim, [2.4, 0.08, 0.1], [0, 2.18, 0.9], group);
  part(box, PAINT.glass, [2.2, 0.03, 1.7], [0, 2.17, 0], group, { cast: false });
  // Work area: pedestal and T-slot table; the workpiece sits on the table.
  part(box, PAINT.dark, [0.9, 0.55, 0.7], [0, 0.52, 0.05], group);
  part(box, PAINT.steel, [1.5, 0.12, 0.95], [0, 0.85, 0.05], group);
  for (const z of [-0.25, 0.05, 0.35]) {
    part(box, PAINT.dark, [1.5, 0.02, 0.05], [0, 0.915, z], group, { cast: false });
  }
  // Column, spindle head, rotating spindle with tool holder and cutter.
  part(box, PAINT.trim, [0.6, 1.7, 0.45], [0, 1.1, -0.62], group);
  rounded(0.62, 0.5, 0.75, 0.05, PAINT.body, [0, 1.85, -0.25], group);
  const spindle = new THREE.Group();
  spindle.userData.dynamic = true;
  spindle.position.set(0, 1.58, 0.0);
  part(cylinder, PAINT.steel, [0.13, 0.22, 0.13], [0, 0, 0], spindle);
  part(cylinder, PAINT.dark, [0.07, 0.16, 0.07], [0, -0.18, 0], spindle);
  part(cylinder, PAINT.steel, [0.03, 0.22, 0.03], [0, -0.36, 0], spindle);
  part(box, PAINT.paintYellow, [0.03, 0.05, 0.14], [0.08, -0.16, 0], spindle, { cast: false });
  group.add(spindle);
  // Work light inside the roof: glows while an operation is running.
  const workLight = new THREE.MeshStandardMaterial({
    color: "#f8fafc",
    emissive: "#fff7e0",
    emissiveIntensity: 0,
  });
  part(box, workLight, [1.4, 0.03, 0.12], [0, 2.1, -0.55], group, { cast: false });
  // Swing-arm control panel with a status-coloured screen.
  const panel = new THREE.Group();
  panel.position.set(1.42, 1.38, 0.78);
  panel.rotation.y = -0.6;
  rounded(0.5, 0.72, 0.1, 0.03, PAINT.trim, [0, 0, 0], panel);
  const screen = new THREE.MeshStandardMaterial({
    color: "#0b1626",
    emissive: "#1e3a5f",
    emissiveIntensity: 0.8,
    roughness: 0.2,
  });
  part(box, screen, [0.4, 0.3, 0.01], [0, 0.12, 0.055], panel, { cast: false });
  for (let row = 0; row < 3; row += 1) {
    for (let col = 0; col < 4; col += 1) {
      part(box, PAINT.steel, [0.06, 0.04, 0.02], [-0.13 + col * 0.087, -0.12 - row * 0.07, 0.055], panel, {
        cast: false,
      });
    }
  }
  part(box, PAINT.trim, [0.06, 0.06, 0.5], [-0.05, 0.25, -0.3], panel);
  group.add(panel);
  // Coolant tank and chip bin behind the machine.
  part(box, PAINT.trim, [0.7, 0.5, 0.35], [-0.75, 0.25, -1.32], group);
  part(box, PAINT.dark, [0.5, 0.35, 0.3], [0.6, 0.18, -1.3], group);
  const lamps = stackLight(group, [1.05, 2.2, -0.8]);
  const statusMaterial = statusParts(group, [0, 2.07, 1.005], 2.1);
  return { group, spindle, workLight, screen, lamps, statusMaterial, workY: WORK_HEIGHT.processing };
}

// Bridge-type coordinate measuring machine (generic). Front faces +z.
function measuringMachine() {
  const group = new THREE.Group();
  part(box, PAINT.trim, [2.4, 0.6, 1.9], [0, 0.3, 0], group);
  part(box, PAINT.granite, [2.3, 0.3, 1.8], [0, 0.75, 0], group);
  const bridge = new THREE.Group();
  part(box, PAINT.body, [0.16, 1.2, 0.3], [-1.0, 1.5, 0], bridge);
  part(box, PAINT.body, [0.16, 1.2, 0.3], [1.0, 1.5, 0], bridge);
  rounded(2.2, 0.22, 0.32, 0.04, PAINT.body, [0, 2.15, 0], bridge);
  const carriage = new THREE.Group();
  carriage.userData.dynamic = true;
  carriage.position.set(0, 2.15, 0.2);
  rounded(0.3, 0.36, 0.22, 0.03, PAINT.trim, [0, 0, 0], carriage);
  part(box, PAINT.steel, [0.07, 0.95, 0.07], [0, -0.55, 0], carriage);
  const ruby = new THREE.MeshStandardMaterial({ color: "#dc2626", roughness: 0.15, metalness: 0.1 });
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.05, 16, 12), ruby);
  tip.position.set(0, -1.06, 0);
  carriage.add(tip);
  bridge.add(carriage);
  group.add(bridge);
  // Operator workstation (desk and monitor) beside the machine.
  part(box, PAINT.trim, [0.7, 0.05, 0.5], [1.55, 0.95, 0.55], group);
  part(box, PAINT.dark, [0.05, 0.9, 0.05], [1.55, 0.47, 0.55], group);
  const screen = new THREE.MeshStandardMaterial({
    color: "#0b1626",
    emissive: "#1e3a5f",
    emissiveIntensity: 0.8,
  });
  part(box, PAINT.dark, [0.5, 0.34, 0.04], [1.55, 1.2, 0.4], group);
  part(box, screen, [0.44, 0.28, 0.01], [1.55, 1.2, 0.425], group, { cast: false });
  const lamps = stackLight(group, [1.0, 2.26, -0.15]);
  const statusMaterial = statusParts(group, [0, 2.15, 0.175], 1.9);
  return { group, carriage, screen, lamps, statusMaterial, workY: WORK_HEIGHT.inspection };
}

export function createMachineModel(kind) {
  return kind === "inspection" ? measuringMachine() : machiningCentre();
}

// One order unit on its own fixture pallet. Origin at the pallet's underside.
export function createWorkpiece(product) {
  const group = new THREE.Group();
  const pallet = new THREE.Mesh(
    box,
    new THREE.MeshStandardMaterial({ color: "#4b5563", roughness: 0.5, metalness: 0.6 }),
  );
  pallet.scale.set(0.62, 0.06, 0.62);
  pallet.position.y = 0.03;
  pallet.castShadow = true;
  pallet.receiveShadow = true;
  group.add(pallet);
  const outline = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(0.68, 0.12, 0.68)),
    new THREE.LineBasicMaterial({ color: "#ffffff" }),
  );
  outline.position.y = 0.05;
  outline.visible = false;
  group.add(outline);
  const metal = new THREE.MeshStandardMaterial({
    color: product?.color ?? "#cbd5e1",
    roughness: 0.6,
    metalness: 0.75,
  });
  const name = `${product?.product_id ?? ""} ${product?.name ?? ""}`.toLowerCase();
  const pieces = [];
  if (name.includes("plate")) {
    pieces.push(rounded(0.5, 0.07, 0.4, 0.02, metal, [0, 0.095, 0], group));
  } else if (name.includes("bracket")) {
    pieces.push(rounded(0.46, 0.07, 0.3, 0.015, metal, [0, 0.095, 0], group));
    pieces.push(rounded(0.07, 0.28, 0.3, 0.015, metal, [-0.195, 0.27, 0], group));
  } else if (name.includes("actuator") || name.includes("base")) {
    pieces.push(rounded(0.42, 0.1, 0.42, 0.02, metal, [0, 0.11, 0], group));
    const boss = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.14, 0.18, 24), metal);
    boss.position.y = 0.25;
    boss.castShadow = true;
    group.add(boss);
    pieces.push(boss);
  } else {
    pieces.push(rounded(0.4, 0.2, 0.4, 0.02, metal, [0, 0.16, 0], group));
  }
  // Green QC tag, shown only when every operation of the unit is complete.
  const tag = new THREE.Mesh(
    box,
    new THREE.MeshStandardMaterial({ color: "#16a34a", emissive: "#16a34a", emissiveIntensity: 0.4 }),
  );
  tag.scale.set(0.12, 0.012, 0.08);
  tag.position.set(0.22, 0.066, 0.22);
  tag.visible = false;
  group.add(tag);
  const materials = [pallet.material, metal, tag.material];
  for (const material of materials) material.transparent = true;
  return { group, outline, metal, tag, materials };
}

// Floor with a procedural concrete texture and painted aisle / zone markings.
export function createFloor(scene, layout) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 256;
  const context = canvas.getContext("2d");
  context.fillStyle = "#62686f";
  context.fillRect(0, 0, 256, 256);
  let seed = 7;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return seed / 2147483647;
  };
  for (let index = 0; index < 5000; index += 1) {
    const shade = 100 + Math.floor(random() * 50);
    context.fillStyle = `rgba(${shade},${shade + 4},${shade + 8},0.35)`;
    context.fillRect(random() * 256, random() * 256, 1 + random() * 2, 1 + random() * 2);
  }
  context.strokeStyle = "rgba(40,44,50,0.35)";
  context.lineWidth = 2;
  context.strokeRect(0, 0, 256, 256); // slab joints
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(7, 5);
  texture.colorSpace = THREE.SRGBColorSpace;
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(32, 23),
    new THREE.MeshStandardMaterial({ map: texture, roughness: 0.85, metalness: 0.05 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);

  const line = (x, z, width, depth, material = PAINT.paintYellow) => {
    const mesh = new THREE.Mesh(box, material);
    mesh.scale.set(width, 0.004, depth);
    mesh.position.set(x, 0.003, z);
    mesh.receiveShadow = true;
    scene.add(mesh);
  };
  const outlineRect = (x, z, width, depth, material) => {
    line(x, z - depth / 2, width, 0.07, material);
    line(x, z + depth / 2, width, 0.07, material);
    line(x - width / 2, z, 0.07, depth, material);
    line(x + width / 2, z, 0.07, depth, material);
  };
  // Main aisle along the front and zone outlines around stations and lanes.
  line(0, 8.2, 28, 0.12);
  line(0, 9.6, 28, 0.12);
  for (const machine of Object.values(layout.machines)) {
    outlineRect(machine.x, machine.z, 3.0, 2.7, PAINT.paintYellow);
    outlineRect(machine.x - 3.45, machine.z, 3.4, 2.1, PAINT.paintWhite);
  }
  outlineRect(layout.incoming.x, layout.incoming.z, 3.6, 6.4, PAINT.paintWhite);
  outlineRect(layout.finished.x, layout.finished.z, 3.6, 6.4, PAINT.paintWhite);
}

// Hall shell: back wall with a clerestory window band, side walls and columns.
export function createHall(scene) {
  const add = (material, size, position) => part(box, material, size, position, scene);
  add(PAINT.wall, [32, 4.5, 0.3], [0, 2.25, -11.6]);
  add(PAINT.wall, [0.3, 4.5, 23], [-16, 2.25, 0]);
  add(PAINT.wall, [0.3, 4.5, 23], [16, 2.25, 0]);
  add(PAINT.window, [30, 0.9, 0.05], [0, 3.5, -11.43]);
  for (const x of [-12, -6, 0, 6, 12]) {
    add(PAINT.column, [0.4, 4.5, 0.4], [x, 2.25, -11.2]);
  }
  for (const z of [-6, 0, 6]) {
    add(PAINT.column, [0.4, 4.5, 0.4], [-15.6, 2.25, z]);
    add(PAINT.column, [0.4, 4.5, 0.4], [15.6, 2.25, z]);
  }
}

// Empty pallet rack (incoming stock) and shipping pallets (finished goods).
export function createStorage(scene, layout) {
  const rack = new THREE.Group();
  rack.position.set(layout.incoming.x, 0, layout.incoming.z);
  for (const x of [-1.6, 1.6]) {
    for (const z of [-3.1, -1.0, 1.0, 3.1]) {
      part(box, PAINT.rackUpright, [0.08, 2.6, 0.08], [x, 1.3, z], rack);
    }
  }
  for (const level of [1.2, 2.4]) {
    for (const x of [-1.6, 1.6]) {
      part(box, PAINT.rackBeam, [0.08, 0.12, 6.2], [x, level, 0], rack);
    }
    part(box, PAINT.decking, [3.2, 0.03, 6.2], [0, level + 0.07, 0], rack);
  }
  scene.add(rack);
  for (const z of [-2.0, 0.0, 2.0]) {
    part(box, PAINT.wood, [3.2, 0.12, 1.8], [layout.finished.x, 0.06, layout.finished.z + z - 0.55], scene);
  }
}

// Merge every static mesh that shares a material (and shadow flags) into one
// mesh, so the static hall, storage and machine bodies cost a few draw calls.
// Meshes inside a group marked `userData.dynamic` keep their own transforms.
export function mergeStatic(root) {
  root.updateMatrixWorld(true);
  const buckets = new Map();
  root.traverse((object) => {
    if (!object.isMesh || !object.geometry.index) return;
    for (let node = object; node; node = node.parent) if (node.userData.dynamic) return;
    const key = `${object.material.uuid}|${object.castShadow}|${object.receiveShadow}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(object);
  });
  let removed = 0;
  for (const meshes of buckets.values()) {
    if (meshes.length < 2) continue;
    const merged = mergeGeometries(
      meshes.map((mesh) => mesh.geometry.clone().applyMatrix4(mesh.matrixWorld)),
      false,
    );
    if (!merged) continue; // incompatible attributes: keep the originals
    const mesh = new THREE.Mesh(merged, meshes[0].material);
    mesh.castShadow = meshes[0].castShadow;
    mesh.receiveShadow = meshes[0].receiveShadow;
    root.add(mesh);
    for (const original of meshes) original.parent.remove(original);
    removed += meshes.length - 1;
  }
  return removed;
}
