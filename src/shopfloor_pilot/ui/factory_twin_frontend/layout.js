// Shared floor layout for the 3D and 2D renderers.
//
// Positions are a visual convention only: the manufacturing model has no
// coordinates, conveyors or transport. Units are placed by the zone that the
// Python projection derived from the authoritative snapshot.

export const STATUS_COLORS = {
  idle: "#94a3b8",
  running: "#22c55e",
  failed: "#ef4444",
  maintenance: "#f59e0b",
  restored: "#06b6d4",
};

export const PRODUCT_PALETTE = ["#60a5fa", "#f59e0b", "#c084fc", "#34d399", "#f472b6"];

export const STATE_COLORS = {
  interrupted: "#ef4444",
  queued_restart: "#fb923c",
  reassigned: "#a855f7",
};

const MACHINE_SPACING = 6;
const QUEUE_PER_ROW = 4;
const QUEUE_STEP = 0.78;
// Heights of the surfaces a unit (origin at its pallet's underside) rests on.
export const WORK_HEIGHT = { processing: 0.92, inspection: 0.9 };
const SHIPPING_PALLET_TOP = 0.12;
const STACK_HEIGHT = 0.45; // stacked fixture pallets in a long queue

export function displayStatus(machine) {
  return machine.restored ? "restored" : machine.status;
}

export function buildLayout(payload) {
  const processing = payload.machines.filter((item) => item.kind !== "inspection");
  const inspection = payload.machines.filter((item) => item.kind === "inspection");
  const machines = {};
  const column = (items, x) => {
    const offset = ((items.length - 1) * MACHINE_SPACING) / 2;
    items.forEach((item, index) => {
      machines[item.machine_id] = { x, z: index * MACHINE_SPACING - offset, ...item };
    });
  };
  column(processing, -2.5);
  column(inspection, 5.5);
  const products = {};
  payload.products.forEach((item, index) => {
    products[item.product_id] = {
      ...item,
      color: PRODUCT_PALETTE[index % PRODUCT_PALETTE.length],
    };
  });
  const unitOrder = {};
  payload.units.forEach((item, index) => {
    unitOrder[item.key] = index;
  });
  return {
    machines,
    products,
    unitOrder,
    incoming: { x: -11, z: 0, label: "Incoming stock" },
    finished: { x: 11, z: 0, label: "Finished goods" },
    buffer: { x: 1.5, z: 9.5, label: "Unassigned WIP" },
    bounds: { minX: -14, maxX: 14, minZ: -11, maxZ: 11 },
  };
}

function gridSlot(origin, index, columns, y = 0) {
  const col = index % columns;
  const row = Math.floor(index / columns);
  return {
    x: origin.x - ((columns - 1) * 0.85) / 2 + col * 0.85,
    y,
    z: origin.z - 2.4 + row * 0.85,
  };
}

// Target position of one unit for the derived playback state. ``placement`` is
// also what the renderers report back for end-of-playback verification.
export function unitTarget(layout, unit, key, state) {
  const machine = unit.machine_id ? layout.machines[unit.machine_id] : null;
  switch (unit.zone) {
    case "machine":
      return { x: machine.x, y: WORK_HEIGHT[machine.kind] ?? WORK_HEIGHT.processing, z: machine.z };
    case "held":
      return { x: machine.x + 0.4, y: 0, z: machine.z + 1.9 };
    case "queue": {
      const position = state.machines[unit.machine_id].queue.indexOf(key);
      const index = Math.max(0, position);
      const row = Math.floor(index / QUEUE_PER_ROW) % 2;
      const layer = Math.floor(index / (QUEUE_PER_ROW * 2));
      return {
        x: machine.x - 2.3 - (index % QUEUE_PER_ROW) * QUEUE_STEP,
        y: layer * STACK_HEIGHT,
        z: machine.z - 0.45 + row * 0.9,
      };
    }
    case "finished":
      return gridSlot(layout.finished, layout.unitOrder[key] ?? 0, 3, SHIPPING_PALLET_TOP);
    case "buffer":
      return gridSlot(layout.buffer, layout.unitOrder[key] ?? 0, 4);
    default:
      return gridSlot(layout.incoming, layout.unitOrder[key] ?? 0, 3);
  }
}

// Inverse of unitTarget: the zone (and queue slot) a *drawn* position falls in.
// Verification uses this to read the rendered scene back instead of trusting the
// zone fields the renderer was given.
export function locate(layout, point) {
  const near = (x, z, dx, dz) => Math.abs(point.x - x) <= dx && Math.abs(point.z - z) <= dz;
  for (const [machineId, machine] of Object.entries(layout.machines)) {
    if (near(machine.x, machine.z, 1.3, 1.15)) return { zone: "machine", machine_id: machineId };
    if (near(machine.x + 0.4, machine.z + 1.9, 0.3, 0.3)) {
      return { zone: "held", machine_id: machineId };
    }
    const col = (machine.x - 2.3 - point.x) / QUEUE_STEP;
    const row = (point.z - (machine.z - 0.45)) / 0.9;
    if (col > -0.3 && col < QUEUE_PER_ROW - 0.7 && row > -0.3 && row < 1.3) {
      const layer = Math.max(0, Math.round((point.y ?? 0) / STACK_HEIGHT));
      const slot = layer * QUEUE_PER_ROW * 2 + Math.round(row) * QUEUE_PER_ROW + Math.round(col);
      return { zone: "queue", machine_id: machineId, slot };
    }
  }
  for (const zone of ["incoming", "finished", "buffer"]) {
    if (near(layout[zone].x, layout[zone].z, 2, 4.5)) return { zone, machine_id: null };
  }
  return { zone: "unknown", machine_id: null };
}

export function hexToRgb(hex) {
  const value = Number.parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

export function colorDistance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}
