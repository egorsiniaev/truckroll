// Builds the isometric scene once, then recolours and animates it on every snapshot.
import { box, el, iso, line, paintBox, pts } from "./iso.js";

export const STATE_COLORS = {
  available: "#22c55e", preparing: "#f59e0b", charging: "#3b82f6",
  faulted: "#ef4444", offline: "#94a3b8", unavailable: "#a1a1aa",
};
const CONNECTOR_COLORS = {
  Available: STATE_COLORS.available, Preparing: STATE_COLORS.preparing, Finishing: STATE_COLORS.preparing,
  Charging: STATE_COLORS.charging, SuspendedEV: STATE_COLORS.charging, SuspendedEVSE: STATE_COLORS.charging,
  Faulted: STATE_COLORS.faulted, Unavailable: STATE_COLORS.unavailable,
};
const WITH_CAR = new Set(["Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"]);
const SIZE = 14;
const CENTERS = [[-19, 0], [0, -19], [0, 19], [19, 0]]; // screen: top-left, top-right, bottom-left, bottom-right
const PLATFORM_COLORS = ["#e8eef7", "#eef3ea", "#f3eee6", "#ebe9f4"];
const CAR_COLORS = ["#f8fafc", "#e2e8f0", "#cbd5e1", "#fde68a", "#bfdbfe"];

let refs = null;

export function buildScene(svg, sites, stations, onSelect) {
  svg.replaceChildren();
  refs = { sites: {}, stations: {}, onSelect };
  svg.append(el("polygon", { class: "ground", points: pts([[-30, -30, 0], [30, -30, 0], [30, 30, 0], [-30, 30, 0]]) }));
  const gridLayer = el("g");
  const siteLayer = el("g");
  const labelLayer = el("g");
  svg.append(gridLayer, siteLayer, labelLayer);
  buildSubstation(gridLayer);

  sites.forEach((site, i) => {
    const [cx, cy] = CENTERS[i % CENTERS.length];
    const ox = cx - SIZE / 2;
    const oy = cy - SIZE / 2;
    const siteStations = stations.filter((s) => s.site === site.code).sort((a, b) => a.id.localeCompare(b.id));
    const r = buildSite(siteLayer, labelLayer, gridLayer, site, siteStations, ox, oy, i);
    refs.sites[site.code] = r;
  });
}

function buildSubstation(layer) {
  const g = el("g", { class: "substation" });
  g.append(el("polygon", { class: "platform", fill: "#e5e7eb", points: pts([[-2, -2, 0], [2, -2, 0], [2, 2, 0], [-2, 2, 0]]) }));
  g.append(box(-1.2, -1.2, 0, 1.6, 1.6, 1.2, "#cbd5e1", "sub").g);
  g.append(box(0.6, 0.3, 0, 0.9, 0.9, 0.8, "#cbd5e1", "sub").g);
  const [tx, ty] = iso(0, 0, 3.2);
  g.append(el("text", { x: tx, y: ty, "text-anchor": "middle", class: "w-sub", "font-size": 12, fill: "#6b7280" }, ["Grid"]));
  layer.append(g);
}

function pylon(layer, x, y) {
  const base = [[x - 0.35, y, 0], [x, y, 2.6], [x + 0.35, y, 0]];
  layer.append(el("polyline", { points: pts(base), fill: "none", stroke: "#9ca3af", "stroke-width": 1.5 }));
  layer.append(line([[x - 0.6, y, 2.1], [x + 0.6, y, 2.1]], "wire"));
}

function buildSite(siteLayer, labelLayer, gridLayer, site, stations, ox, oy, index) {
  const g = el("g", { class: "site clickable", "data-site": site.code });
  g.addEventListener("click", (e) => { if (e.target.closest(".station")) return; refs.onSelect({ type: "site", id: site.code }); });

  const platform = el("polygon", {
    class: "platform", fill: PLATFORM_COLORS[index % PLATFORM_COLORS.length],
    points: pts([[ox, oy, 0], [ox + SIZE, oy, 0], [ox + SIZE, oy + SIZE, 0], [ox, oy + SIZE, 0]]),
  });
  g.append(platform);

  // Grid feed: substation -> pylon -> site transformer.
  const entry = [ox + SIZE - 1.4, oy + 1.4];
  const mid = [(entry[0]) / 2, (entry[1]) / 2];
  pylon(gridLayer, mid[0], mid[1]);
  const gridPath = [[0, 0, 0.6], [mid[0], mid[1], 2.1], [entry[0], entry[1], 0.9]];
  gridLayer.append(line(gridPath, "wire"));
  const gridFlow = line(gridPath, "flow grid");
  gridLayer.append(gridFlow);

  // Site bus from the transformer along the front of the buildings.
  const busY = oy + 6.2;
  const bus = [[entry[0], entry[1], 0.05], [entry[0], busY, 0.05], [ox + 1.2, busY, 0.05]];
  g.append(line(bus, "wire"));
  const busFlow = line(bus, "flow");
  g.append(busFlow);

  const objects = [];
  objects.push(...buildingFor(site, ox, oy));
  const tf = box(entry[0] - 0.6, entry[1] - 0.5, 0, 1.2, 1.0, 1.0, "#d1d5db", "tf");
  objects.push({ depth: entry[0] + entry[1], node: tf.g });

  const layout = chargerLayout(stations.length, site.connectors);
  const stationRefs = {};
  stations.forEach((st, k) => {
    const [x, y] = [ox + layout[k][0], oy + layout[k][1]];
    const feed = [[x + 0.25, busY, 0.05], [x + 0.25, y, 0.05]];
    g.append(line(feed, "wire"));
    const flow = line(feed, "flow");
    g.append(flow);
    const sg = el("g", { class: "station clickable", "data-station": st.id });
    sg.addEventListener("click", (e) => { e.stopPropagation(); refs.onSelect({ type: "station", id: st.id }); });
    const ped = box(x, y, 0, 0.5, 0.36, 1.0, STATE_COLORS.available, "ped");
    const cars = [];
    const n = Math.max(1, st.connectors?.length || site.connectors);
    for (let c = 0; c < n; c++) {
      const cx = n === 1 ? x - 0.15 : x - 0.75 + c * 1.2;
      const car = buildCar(cx, y + 0.65, CAR_COLORS[(k + c) % CAR_COLORS.length]);
      cars.push(car);
      objects.push({ depth: cx + y + 2.2, node: car.g });
    }
    const title = el("title", {}, [st.id]);
    sg.append(title, ped.g);
    objects.push({ depth: x + y + 0.9, node: sg });
    stationRefs[st.id] = { g: sg, ped, cars, flow, title };
    refs.stations[st.id] = stationRefs[st.id];
  });

  objects.sort((a, b) => a.depth - b.depth).forEach((o) => g.append(o.node));
  siteLayer.append(g);

  const label = buildLabel(labelLayer, site, ox, oy, index);
  return { g, platform, gridFlow, busFlow, label, stations: stationRefs };
}

function buildingFor(site, ox, oy) {
  const out = [];
  const add = (b, depth) => out.push({ depth, node: b.g });
  if (site.kind.includes("depot")) {
    const b = box(ox + 1, oy + 1, 0, 7, 3.6, 2.0, "#f1f5f9", "bld");
    add(b, ox + oy + 5);
    for (let i = 0; i < 6; i++) {
      for (let j = 0; j < 2; j++) {
        b.g.append(el("polygon", { fill: "#334155", opacity: 0.85,
          points: pts([[ox + 1.3 + i * 1.1, oy + 1.3 + j * 1.6, 2.02], [ox + 2.2 + i * 1.1, oy + 1.3 + j * 1.6, 2.02],
            [ox + 2.2 + i * 1.1, oy + 2.6 + j * 1.6, 2.02], [ox + 1.3 + i * 1.1, oy + 2.6 + j * 1.6, 2.02]]) }));
      }
    }
  } else if (site.kind.includes("retail")) {
    const b = box(ox + 1, oy + 1, 0, 6, 3.4, 1.5, "#fafafa", "bld");
    b.g.append(el("polygon", { fill: "#f97316", points: pts([[ox + 1, oy + 4.4, 1.1], [ox + 7, oy + 4.4, 1.1], [ox + 7, oy + 4.4, 1.4], [ox + 1, oy + 4.4, 1.4]]) }));
    add(b, ox + oy + 4);
  } else if (site.kind.includes("highway")) {
    const shop = box(ox + 1, oy + 1, 0, 3, 2.6, 1.3, "#f8fafc", "bld");
    add(shop, ox + oy + 3);
    const canopy = box(ox + 1.2, oy + 7.3, 2.0, 11.2, 5.2, 0.25, "#e2e8f0", "canopy");
    canopy.g.setAttribute("opacity", "0.55");
    out.push({ depth: ox + oy + 99, node: canopy.g });
  } else {
    const tower = box(ox + 1, oy + 1, 0, 3, 3, 4.6, "#eef2ff", "bld");
    for (let f = 0; f < 6; f++) {
      tower.g.append(el("polygon", { fill: "#94a3b8", opacity: 0.5,
        points: pts([[ox + 1.2, oy + 4, 0.5 + f * 0.7], [ox + 3.8, oy + 4, 0.5 + f * 0.7], [ox + 3.8, oy + 4, 0.8 + f * 0.7], [ox + 1.2, oy + 4, 0.8 + f * 0.7]]) }));
    }
    add(tower, ox + oy + 4);
    add(box(ox + 4.5, oy + 1, 0, 3.5, 2.2, 1.2, "#f8fafc", "bld"), ox + oy + 6);
  }
  return out;
}

function chargerLayout(count, connectors) {
  const cols = connectors > 1 ? Math.min(4, count) : Math.min(5, count);
  const rows = Math.ceil(count / cols);
  const x0 = 1.8;
  const x1 = SIZE - 1.8;
  const y0 = 7.6;
  const y1 = SIZE - 2.4;
  const dx = cols > 1 ? (x1 - x0) / (cols - 1) : 0;
  const dy = rows > 1 ? (y1 - y0) / (rows - 1) : 0;
  return Array.from({ length: count }, (_, k) => [x0 + (k % cols) * dx, y0 + Math.floor(k / cols) * dy]);
}

function buildCar(x, y, color) {
  const g = el("g", { class: "car hidden" });
  const body = box(x, y, 0, 0.75, 1.25, 0.32, color, "car");
  const cabin = box(x + 0.08, y + 0.3, 0.32, 0.6, 0.65, 0.24, color, "car");
  const glow = el("circle", { r: 3, fill: STATE_COLORS.charging, class: "glow" });
  const [gx, gy] = iso(x + 0.35, y, 0.9);
  glow.setAttribute("cx", gx);
  glow.setAttribute("cy", gy);
  g.append(body.g, cabin.g, glow);
  return { g, glow };
}

function buildLabel(layer, site, ox, oy, index) {
  // Anchor on the platform's outer corner, so each label clearly belongs to one site.
  const [wx, wy] = CENTERS[index % CENTERS.length];
  const isLeft = wx - wy < 0; // screen x of the site centre
  const [px, py] = isLeft ? iso(ox, oy + SIZE, 0) : iso(ox + SIZE, oy, 0);
  const ax = px + (isLeft ? 40 : -150);
  const ay = py - 150;
  const g = el("g", { class: "label-card clickable", transform: `translate(${ax.toFixed(0)},${ay.toFixed(0)})` });
  g.addEventListener("click", () => refs.onSelect({ type: "site", id: site.code }));
  const w = 196;
  g.append(el("rect", { x: -w / 2, y: -46, width: w, height: 62, rx: 10 }));
  const name = el("text", { x: -w / 2 + 12, y: -26, "font-weight": 600 }, [site.code]);
  const sub = el("text", { x: -w / 2 + 12, y: -10, class: "small" }, ["…"]);
  g.append(name, sub);
  g.append(el("rect", { x: -w / 2 + 12, y: 0, width: w - 24, height: 6, rx: 3, class: "barbg" }));
  const bar = el("rect", { x: -w / 2 + 12, y: 0, width: 0, height: 6, rx: 3, class: "barfg" });
  g.append(bar);
  const down = el("g", { visibility: "hidden" }, [
    el("rect", { x: w / 2 - 74, y: -40, width: 64, height: 18, rx: 9, class: "badge-down" }),
    el("text", { x: w / 2 - 42, y: -27, "text-anchor": "middle", fill: "#fff", "font-size": 10, "font-weight": 700 }, ["OUTAGE"]),
  ]);
  g.append(down);
  const weather = el("g", { class: "weather", transform: `translate(${w / 2 + 10},-30)` });
  const wIcon = el("g");
  const wText = el("text", { x: 30, y: 0 }, ["–"]);
  const wSub = el("text", { x: 30, y: 15, class: "w-sub" }, [""]);
  weather.append(wIcon, wText, wSub);
  g.append(weather);
  layer.append(g);
  return { g, sub, bar, barWidth: w - 24, down, wIcon, wText, wSub };
}

function weatherIcon(w) {
  const g = [];
  if (!w) return g;
  const cloudy = (w.cloud_pct ?? 0) >= 60;
  if (w.is_day && !cloudy) {
    g.push(el("circle", { cx: 12, cy: -4, r: 8, fill: "#fbbf24" }));
  } else if (!w.is_day && !cloudy) {
    g.push(el("circle", { cx: 12, cy: -4, r: 8, fill: "#cbd5e1" }), el("circle", { cx: 16, cy: -7, r: 7, fill: "#fff" }));
  }
  if ((w.cloud_pct ?? 0) >= 30) {
    g.push(el("path", { d: "M2 4 a6 6 0 0 1 6-8 a8 8 0 0 1 15 2 a5 5 0 0 1 1 10 h-18 a5 5 0 0 1-4-4z", fill: cloudy ? "#cbd5e1" : "#e2e8f0" }));
  }
  if ((w.gust_kmh ?? 0) >= 45) {
    g.push(el("path", { d: "M0 12 h18 M4 16 h14", stroke: "#64748b", "stroke-width": 1.5, "stroke-linecap": "round" }));
  }
  return g;
}

function setFlow(path, on, speedFactor) {
  path.classList.toggle("on", on);
  if (on) path.style.setProperty("--speed", `${Math.max(0.35, 2.4 - 2 * speedFactor).toFixed(2)}s`);
}

export function updateScene(snapshot, selected) {
  if (!refs) return;
  const sitesByCode = Object.fromEntries(snapshot.sites.map((s) => [s.code, s]));
  for (const [code, r] of Object.entries(refs.sites)) {
    const s = sitesByCode[code];
    if (!s) continue;
    const kw = (s.power_w / 1000).toFixed(0);
    const cap = (s.cap_w / 1000).toFixed(0);
    const avail = s.availability == null ? "–" : `${Math.round(s.availability * 100)}%`;
    r.label.sub.textContent = `${kw} of ${cap} kW · ${avail} available${s.network.latency_ms ? ` · +${s.network.latency_ms} ms` : ""}`;
    const used = Math.min(1, s.cap_used ?? 0);
    r.label.bar.setAttribute("width", (r.label.barWidth * used).toFixed(1));
    r.label.bar.classList.toggle("over", (s.cap_used ?? 0) > 1);
    r.label.down.setAttribute("visibility", s.network.enabled ? "hidden" : "visible");
    const w = s.weather;
    r.label.wIcon.replaceChildren(...weatherIcon(w));
    r.label.wText.textContent = w && w.temp_c != null ? `${Math.round(w.temp_c)}°C` : "";
    const solar = s.solar_w ? ` · ☀ ${(s.solar_w / 1000).toFixed(0)} kW` : "";
    r.label.wSub.textContent = w && w.gust_kmh != null ? `gusts ${Math.round(w.gust_kmh)} km/h${solar}${w.stale ? " (stale)" : ""}` : "";
    r.gridFlow.classList.toggle("down", !s.network.enabled);
    setFlow(r.gridFlow, s.network.enabled && s.power_w > 0, used);
    setFlow(r.busFlow, s.power_w > 0, used);
    r.g.classList.toggle("selected", selected?.type === "site" && selected.id === code);
  }
  const byId = Object.fromEntries(snapshot.stations.map((s) => [s.id, s]));
  for (const [id, r] of Object.entries(refs.stations)) {
    const st = byId[id];
    const state = st ? st.state : "offline";
    paintBox(r.ped.faces, STATE_COLORS[state] ?? STATE_COLORS.offline);
    r.title.textContent = st ? `${id}: ${state}, ${(st.power_w / 1000).toFixed(1)} kW` : `${id}: unknown`;
    const maxW = sitesByCode[st?.site]?.power_w ?? 22000;
    setFlow(r.flow, !!st?.connected && st.power_w > 0, Math.min(1, (st?.power_w ?? 0) / maxW));
    (st?.connectors ?? []).forEach((c, i) => {
      const car = r.cars[i];
      if (!car) return;
      car.g.classList.toggle("hidden", !(st.connected && WITH_CAR.has(c.status)));
      car.glow.setAttribute("visibility", c.status === "Charging" ? "visible" : "hidden");
      car.glow.setAttribute("fill", CONNECTOR_COLORS[c.status] ?? STATE_COLORS.charging);
    });
    r.g.classList.toggle("selected", selected?.type === "station" && selected.id === id);
  }
}
