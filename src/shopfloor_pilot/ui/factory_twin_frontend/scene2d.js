// Canvas-2D top-down renderer: same interface as scene3d.js, no WebGL/GPU
// context. Used when WebGL is unavailable or when the operator selects it.

import {
  STATE_COLORS,
  STATUS_COLORS,
  colorDistance,
  displayStatus,
  hexToRgb,
  locate,
  unitTarget,
} from "./layout.js";

const TWEEN_MS = 650;
const MACHINE_FILL = { normal: hexToRgb("#475569"), failed: hexToRgb("#7f1d1d") };
const FLASH_BORDER = "#bbf7d0"; // 900 ms outer ring after a completion
const BORDER_STATUSES = [
  ...["idle", "running", "restored", "maintenance"].map((name) => [
    name,
    hexToRgb(STATUS_COLORS[name]),
  ]),
  ["flash", hexToRgb(FLASH_BORDER)], // must not occur: the ring is outside the border
];

export function createScene2D(container, overlay, layout) {
  const canvas = document.createElement("canvas");
  container.appendChild(canvas);
  const context = canvas.getContext("2d");
  const ratio = Math.min(window.devicePixelRatio || 1, 1.5);
  let width = 0;
  let height = 0;
  let view = null;
  let descriptors = {};
  const placed = {};
  const flashes = {};
  let blinking = false;
  // What the last render() actually drew; verification reads pixels at these points.
  let drawn = { machines: [], units: {} };

  function toScreen(x, z) {
    const { minX, maxX, minZ, maxZ } = layout.bounds;
    const scale = Math.min(width / (maxX - minX), height / (maxZ - minZ));
    const offsetX = (width - (maxX - minX) * scale) / 2;
    const offsetY = (height - (maxZ - minZ) * scale) / 2;
    return { x: offsetX + (x - minX) * scale, y: offsetY + (z - minZ) * scale, scale };
  }

  function update(nextView, now, nextDescriptors) {
    view = nextView;
    descriptors = nextDescriptors;
    blinking = false;
    for (const [machineId, machine] of Object.entries(view.machines)) {
      if (displayStatus(machine) === "failed") blinking = true;
      if (machine.completedNow) flashes[machineId] = now + 900;
    }
    const present = new Set(Object.keys(view.units));
    for (const [key, unit] of Object.entries(view.units)) {
      const target = unitTarget(layout, unit, key, view);
      const current = placed[key];
      if (!current) {
        placed[key] = {
          x: target.x,
          z: target.z,
          tx: target.x,
          tz: target.z,
        };
        continue;
      }
      if (current.tx !== target.x || current.tz !== target.z) {
        current.tween = { fromX: current.x, fromZ: current.z, start: now };
      }
      Object.assign(current, { tx: target.x, tz: target.z });
    }
    for (const key of Object.keys(placed)) if (!present.has(key)) delete placed[key];
  }

  function animating(now) {
    const tweening =
      Object.values(placed).some((item) => item.tween) ||
      Object.values(flashes).some((until) => until > now);
    return { tweening, blinking };
  }

  function rect(x, z, w, d, fill, stroke) {
    const a = toScreen(x - w / 2, z - d / 2);
    const b = toScreen(x + w / 2, z + d / 2);
    context.fillStyle = fill;
    context.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
    if (stroke) {
      context.strokeStyle = stroke;
      context.lineWidth = 2;
      context.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
    }
  }

  function text(value, x, z, color, size = 12, weight = 600) {
    const point = toScreen(x, z);
    context.fillStyle = color;
    context.font = `${weight} ${size}px system-ui, sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(value, point.x, point.y);
  }

  function render(now) {
    if (!width || !height) return;
    drawn = { machines: [], units: {} };
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.fillStyle = "#0b1220";
    context.fillRect(0, 0, width, height);
    rect(0, 0, 28, 22, "#1e293b");
    rect(layout.incoming.x, layout.incoming.z, 3.4, 6, "rgba(59,130,246,0.22)");
    rect(layout.finished.x, layout.finished.z, 3.4, 6, "rgba(34,197,94,0.22)");
    text(layout.incoming.label, layout.incoming.x, layout.incoming.z + 3.6, "#cbd5e1", 11);
    text(layout.finished.label, layout.finished.x, layout.finished.z + 3.6, "#cbd5e1", 11);
    if (!view) return;

    for (const [machineId, machine] of Object.entries(layout.machines)) {
      const state = view.machines[machineId];
      const status = displayStatus(state);
      rect(machine.x - 3.45, machine.z, 3.3, 2.0, "rgba(100,116,139,0.18)");
      const color = STATUS_COLORS[status];
      let border = color;
      if (status === "failed" && Math.sin(now / 160) <= 0) border = "#450a0a";
      // A completion highlight is an outer ring, so the status border stays visible.
      if ((flashes[machineId] || 0) > now) {
        rect(machine.x, machine.z, 3.1, 2.8, "rgba(0,0,0,0)", FLASH_BORDER);
      }
      rect(machine.x, machine.z, 2.6, 2.3, status === "failed" ? "#7f1d1d" : "#475569", border);
      drawn.machines.push(machineId);
      text(`${machineId} · ${status.toUpperCase()}`, machine.x, machine.z - 1.6, color, 12);
      if (state.running) {
        const left = toScreen(machine.x - 1.2, machine.z + 1.5);
        const right = toScreen(machine.x + 1.2, machine.z + 1.5);
        context.fillStyle = "#0f172a";
        context.fillRect(left.x, left.y - 3, right.x - left.x, 6);
        context.fillStyle = STATUS_COLORS.running;
        context.fillRect(left.x, left.y - 3, (right.x - left.x) * state.running.progress, 6);
      }
    }

    for (const pair of view.reassignmentPairs) {
      const from = layout.machines[pair.from];
      const to = layout.machines[pair.to];
      if (!from || !to) continue;
      const a = toScreen(from.x, from.z);
      const b = toScreen(to.x, to.z);
      const control = toScreen((from.x + to.x) / 2 + 2.6, (from.z + to.z) / 2);
      context.save();
      context.setLineDash([6, 4]);
      context.strokeStyle = STATE_COLORS.reassigned;
      context.lineWidth = 2.5;
      context.beginPath();
      context.moveTo(a.x, a.y);
      context.quadraticCurveTo(control.x, control.y, b.x, b.y);
      context.stroke();
      context.restore();
      context.fillStyle = STATE_COLORS.reassigned;
      context.beginPath();
      context.arc(b.x, b.y, 5, 0, Math.PI * 2);
      context.fill();
      const names = pair.units
        .map((unit) => `${unit.key} s${unit.step + 1}${unit.restart ? " restart" : ""}`)
        .join(", ");
      text(`↪ ${names}`, (from.x + to.x) / 2 + 2.4, (from.z + to.z) / 2, "#d8b4fe", 11);
    }

    for (const [key, item] of Object.entries(placed)) {
      if (item.tween) {
        const progress = Math.min(1, (now - item.tween.start) / TWEEN_MS);
        item.x = item.tween.fromX + (item.tx - item.tween.fromX) * progress;
        item.z = item.tween.fromZ + (item.tz - item.tween.fromZ) * progress;
        if (progress >= 1) {
          item.x = item.tx;
          item.z = item.tz;
          item.tween = null;
        }
      } else if (item.tx !== undefined) {
        item.x = item.tx;
        item.z = item.tz;
      }
      const unit = view.units[key];
      const product = layout.products[descriptors[key]?.product_id];
      context.globalAlpha = unit.released ? 1 : 0.35;
      const outline =
        unit.state === "interrupted"
          ? STATE_COLORS.interrupted
          : unit.reassigned_from
            ? STATE_COLORS.reassigned
            : unit.state === "queued_restart"
              ? STATE_COLORS.queued_restart
              : null;
      rect(item.x, item.z, 0.55, 0.55, product ? product.color : "#e2e8f0", outline);
      drawn.units[key] = { x: item.x, z: item.z, alpha: context.globalAlpha, color: product?.color };
      context.globalAlpha = 1;
    }
  }

  function resize() {
    width = container.clientWidth;
    height = container.clientHeight;
    if (!width || !height) return false;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    return true;
  }

  function pixel(x, z, offset = 0) {
    const point = toScreen(x, z);
    const data = context.getImageData(
      Math.round(point.x * ratio) + offset,
      Math.round(point.y * ratio),
      1,
      1,
    ).data;
    return [data[0], data[1], data[2]];
  }

  // Read the canvas back: machine status from the drawn fill/border pixels and
  // unit placement from the drawn coordinates, checked against their pixels.
  function readBack() {
    const machines = {};
    const units = {};
    const problems = [];
    const covered = (x, z) =>
      Object.values(drawn.units).some(
        (item) => Math.abs(item.x - x) <= 0.4 && Math.abs(item.z - z) <= 0.4,
      );
    for (const machineId of drawn.machines) {
      const machine = layout.machines[machineId];
      // Several sample points per machine; skip any that a drawn unit covers.
      const fillPoints = [
        [-0.95, 0.85],
        [0.95, 0.85],
        [-0.95, -0.85],
        [0.95, -0.85],
      ].map(([dx, dz]) => [machine.x + dx, machine.z + dz]);
      const edgePoints = [-0.6, 0, 0.6].flatMap((dz) => [
        [machine.x - 1.3, machine.z + dz],
        [machine.x + 1.3, machine.z + dz],
      ]);
      const fillAt = fillPoints.find(([x, z]) => !covered(x, z));
      const edges = edgePoints.filter(([x, z]) => !covered(x, z));
      let status = "unrecognized";
      if (!fillAt || edges.length === 0) {
        status = "occluded"; // transient: a moving unit covers the machine
      } else if (colorDistance(pixel(...fillAt), MACHINE_FILL.failed) < 40) {
        status = "failed";
      } else if (colorDistance(pixel(...fillAt), MACHINE_FILL.normal) < 40) {
        let best = null;
        for (const [x, z] of edges) {
          for (const offset of [-2, -1, 0, 1, 2]) {
            const sample = pixel(x, z, offset);
            for (const [name, rgb] of BORDER_STATUSES) {
              const distance = colorDistance(sample, rgb);
              if (!best || distance < best.distance) best = { name, distance };
            }
          }
        }
        if (best && best.distance < 60) status = best.name;
      }
      if (status === "unrecognized") problems.push(`${machineId}: drawn colour not recognised`);
      machines[machineId] = { status: status === "restored" ? "idle" : status };
    }
    for (const [key, item] of Object.entries(drawn.units)) {
      units[key] = { ...locate(layout, item), x: item.x, z: item.z };
      // Dimmed (not yet released) units blend with the floor; check only opaque ones.
      if (item.alpha === 1 && item.color) {
        if (colorDistance(pixel(item.x, item.z), hexToRgb(item.color)) >= 40) {
          problems.push(`${key}: unit pixels do not show its product colour`);
        }
      }
    }
    return { units, machines, problems };
  }

  function info() {
    return { kind: "canvas2d", renderer: "Canvas 2D (no WebGL context)" };
  }

  function dispose() {
    canvas.remove();
    overlay.replaceChildren();
  }

  return { kind: "canvas2d", update, render, animating, resize, readBack, info, dispose };
}
