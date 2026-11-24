// Builds the isometric scene once, then recolours and animates it on every snapshot.
import { box, el, iso, line, paintBox, pts } from "./iso.js";

export const STATE_COLORS = {
  available: "#22c55e", preparing: "#f59e0b", charging: "#3b82f6",
  faulted: "#ef4444", offline: "#94a3b8", unavailable: "#a1a1aa",
};
const PLUG_COLORS = {
  Preparing: STATE_COLORS.preparing, Finishing: STATE_COLORS.preparing,
  Charging: STATE_COLORS.charging, SuspendedEV: "#8b5cf6", SuspendedEVSE: "#8b5cf6",
};
const WITH_CAR = new Set(Object.keys(PLUG_COLORS));
const SIZE = 16;
// Site centres on the isometric axes; screen order: top-left, top-right, bottom-left, bottom-right.
const CENTERS = [[-20, 0], [0, -20], [0, 20], [20, 0]];
// Overhead line route from the substation: along y then x, or x then y, so it stays in the gaps between sites.
const ROUTE_Y_FIRST = [true, false, false, true];
const PLATFORM_COLORS = ["#e8eef7", "#eef3ea", "#f3eee6", "#ebe9f4"];
// Cars are just cars: a mix of everyday colours. The small light on each one is the connector state.
const CAR_COLORS = ["#f8fafc", "#cbd5e1", "#334155", "#dc2626", "#2563eb", "#16a34a", "#f59e0b", "#7c3aed", "#0f172a", "#e5e7eb"];
const carColor = (n) => CAR_COLORS[(n * 7 + 3) % CAR_COLORS.length];
const QUEUE_SLOTS = 8;
export const loadClass = (ratio) => (ratio == null ? "load-low" : ratio >= 0.7 ? "load-high" : ratio >= 0.3 ? "load-mid" : "load-low");
const LINE_Z = 2.2;

let refs = null;

export function buildScene(svg, sites, stations, onSelect) {
  svg.replaceChildren();
  refs = { sites: {}, stations: {}, onSelect };
  svg.append(el("polygon", { class: "ground", points: pts([[-32, -32, 0], [32, -32, 0], [32, 32, 0], [-32, 32, 0]]) }));
  const gridLayer = el("g", { class: "grid-layer" });
  const siteLayer = el("g", { class: "site-layer" });
  const labelLayer = el("g", { class: "label-layer" });
  // Selection marker sits above everything, so cars and canopies never hide it.
  const selectRing = el("ellipse", { rx: 30, ry: 17, class: "select-ring top", visibility: "hidden" });
  const scenarioLayer = el("g", { class: "scenario-layer" });
  svg.append(gridLayer, siteLayer, scenarioLayer, selectRing, labelLayer);
  refs.selectRing = selectRing;
  refs.scenarioLayer = scenarioLayer;
  buildSubstation(gridLayer);

  sites.forEach((site, i) => {
    const [cx, cy] = CENTERS[i % CENTERS.length];
    const siteStations = stations.filter((s) => s.site === site.code).sort((a, b) => a.id.localeCompare(b.id));
    refs.sites[site.code] = buildSite(siteLayer, labelLayer, gridLayer, site, siteStations, cx - SIZE / 2, cy - SIZE / 2, i);
  });
}

function buildSubstation(layer) {
  const g = el("g", { class: "substation building" });
  g.append(el("polygon", { class: "platform", fill: "#e5e7eb", points: pts([[-2.2, -2.2, 0], [2.2, -2.2, 0], [2.2, 2.2, 0], [-2.2, 2.2, 0]]) }));
  g.append(box(-1.4, -1.4, 0, 1.8, 1.8, 1.3, "#cbd5e1", "sub").g);
  g.append(box(0.7, 0.4, 0, 1.0, 1.0, 0.9, "#cbd5e1", "sub").g);
  const [tx, ty] = iso(0, 0, 3.4);
  g.append(el("text", { x: tx, y: ty, "text-anchor": "middle", class: "scene-text" }, ["Grid substation"]));
  layer.append(g);
}

function pylon(layer, x, y) {
  const g = el("g", { class: "pylon" });
  g.append(el("polyline", { points: pts([[x - 0.3, y - 0.3, 0], [x, y, LINE_Z + 0.4], [x + 0.3, y + 0.3, 0]]) }));
  g.append(el("polyline", { points: pts([[x - 0.3, y + 0.3, 0], [x, y, LINE_Z + 0.4], [x + 0.3, y - 0.3, 0]]) }));
  g.append(line([[x - 0.5, y, LINE_Z], [x + 0.5, y, LINE_Z]], "pylon-arm"));
  layer.append(g);
}

function gridRoute(entry, yFirst) {
  const [ex, ey] = entry;
  const corner = yFirst ? [0, ey] : [ex, 0];
  return { corner, path: [[0, 0, 1.3], [0, 0, LINE_Z], [corner[0], corner[1], LINE_Z], [ex, ey, LINE_Z], [ex, ey, 1.0]] };
}

function buildSite(siteLayer, labelLayer, gridLayer, site, stations, ox, oy, index) {
  const g = el("g", { class: "site clickable", "data-site": site.code });
  g.addEventListener("click", (e) => { if (e.target.closest(".station")) return; refs.onSelect({ type: "site", id: site.code }); });
  const platform = el("polygon", {
    class: "platform", fill: PLATFORM_COLORS[index % PLATFORM_COLORS.length],
    points: pts([[ox, oy, 0], [ox + SIZE, oy, 0], [ox + SIZE, oy + SIZE, 0], [ox, oy + SIZE, 0]]),
  });
  g.append(platform);

  // Overhead line: substation -> pylons along the axes -> transformer in the site's back-right corner.
  const entry = [ox + SIZE - 0.7, oy + 0.7];
  const { corner, path } = gridRoute(entry, ROUTE_Y_FIRST[index % ROUTE_Y_FIRST.length]);
  pylon(gridLayer, corner[0], corner[1]);
  pylon(gridLayer, corner[0] / 2, corner[1] / 2);
  pylon(gridLayer, (corner[0] + entry[0]) / 2, (corner[1] + entry[1]) / 2);
  gridLayer.append(line(path, "wire overhead"));
  const gridFlow = line(path, "flow grid");
  gridLayer.append(gridFlow);

  // Site cable: down the right-hand service strip, then along the front of the buildings.
  const busY = oy + 6.0;
  const bus = [[entry[0], entry[1], 0.05], [entry[0], busY, 0.05], [ox + 1.2, busY, 0.05]];
  g.append(line(bus, "wire"));
  const busFlow = line(bus, "flow");
  g.append(busFlow);

  const objects = [...buildingFor(site, ox, oy)];
  objects.push({ depth: entry[0] + entry[1], node: box(entry[0] - 0.7, entry[1] - 0.5, 0, 1.2, 1.0, 1.0, "#d1d5db", "tf").g });

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
    const [ax, ay] = iso(x + 0.25, y + 0.18, 1.6);
    const alert = el("circle", { cx: ax, cy: ay, r: 7, class: "alert-ring", visibility: "hidden" });
    const pos = iso(x + 0.25, y + 0.18, 0.5);
    const title = el("title", {}, [st.id]);
    sg.append(title, ped.g, alert);
    objects.push({ depth: x + y + 0.9, node: sg });
    const n = Math.max(1, st.connectors?.length || site.connectors);
    const cars = [];
    for (let c = 0; c < n; c++) {
      const cx = n === 1 ? x - 0.12 : x - 0.85 + c * 1.3;
      const car = buildCar(cx, y + 0.6, carColor(k * 2 + c + index * 5));
      cars.push(car);
      objects.push({ depth: cx + y + 2.2, node: car.g });
    }
    stationRefs[st.id] = { g: sg, ped, cars, flow, title, alert, pos };
    refs.stations[st.id] = stationRefs[st.id];
  });

  // Waiting lane in front of the site: queued cars line up here.
  g.append(el("polygon", { class: "lane", points: pts([[ox, oy + SIZE + 0.15, 0], [ox + SIZE, oy + SIZE + 0.15, 0],
    [ox + SIZE, oy + SIZE + 1.75, 0], [ox, oy + SIZE + 1.75, 0]]) }));
  const queueCars = [];
  for (let q = 0; q < QUEUE_SLOTS; q++) {
    const qx = ox + 1.0 + q * 1.6;
    const car = buildCar(qx, oy + SIZE + 0.3, carColor(q * 3 + index));
    car.light.remove();
    queueCars.push(car);
    objects.push({ depth: qx + oy + SIZE + 5, node: car.g });
  }
  const [tx, ty] = iso(ox + 0.6, oy + SIZE + 2.4, 0);
  const queueText = el("text", { x: tx + 4, y: ty + 14, class: "queue-text", "text-anchor": "start" }, [""]);

  objects.sort((a, b) => a.depth - b.depth).forEach((o) => g.append(o.node));
  g.append(queueText);
  siteLayer.append(g);
  return { g, platform, gridFlow, busFlow, queueCars, queueText,
    label: buildLabel(labelLayer, site, ox, oy, index), stations: stationRefs };
}

function windows(b, list) {
  for (const p of list) b.g.append(el("polygon", { class: "window", points: pts(p) }));
}

function frontWindows(x0, x1, y, z0, z1, count) {
  const w = (x1 - x0) / count;
  return Array.from({ length: count }, (_, i) => {
    const a = x0 + i * w + w * 0.2;
    const b = x0 + (i + 1) * w - w * 0.2;
    return [[a, y, z0], [b, y, z0], [b, y, z1], [a, y, z1]];
  });
}

function buildingFor(site, ox, oy) {
  const out = [];
  const add = (b, depth) => { b.g.classList.add("building"); out.push({ depth, node: b.g }); };
  if (site.kind.includes("depot")) {
    const b = box(ox + 1, oy + 1, 0, 8, 3.6, 2.0, "#f1f5f9", "bld");
    for (let i = 0; i < 7; i++) {
      for (let j = 0; j < 2; j++) {
        b.g.append(el("polygon", { class: "solar-panel", points: pts([[ox + 1.3 + i * 1.1, oy + 1.3 + j * 1.6, 2.02],
          [ox + 2.2 + i * 1.1, oy + 1.3 + j * 1.6, 2.02], [ox + 2.2 + i * 1.1, oy + 2.6 + j * 1.6, 2.02],
          [ox + 1.3 + i * 1.1, oy + 2.6 + j * 1.6, 2.02]]) }));
      }
    }
    windows(b, frontWindows(ox + 1.3, ox + 8.7, oy + 4.6, 1.2, 1.6, 6));
    add(b, ox + oy + 6);
  } else if (site.kind.includes("retail")) {
    const b = box(ox + 1, oy + 1, 0, 7, 3.4, 1.6, "#fafafa", "bld");
    b.g.append(el("polygon", { fill: "#f97316", points: pts([[ox + 1, oy + 4.4, 1.2], [ox + 8, oy + 4.4, 1.2], [ox + 8, oy + 4.4, 1.5], [ox + 1, oy + 4.4, 1.5]]) }));
    windows(b, frontWindows(ox + 1.3, ox + 7.7, oy + 4.4, 0.2, 1.0, 4));
    add(b, ox + oy + 5);
  } else if (site.kind.includes("highway")) {
    const shop = box(ox + 1, oy + 1, 0, 3.4, 2.8, 1.3, "#f8fafc", "bld");
    windows(shop, frontWindows(ox + 1.2, ox + 4.2, oy + 3.8, 0.3, 1.0, 2));
    add(shop, ox + oy + 3);
    const canopy = box(ox + 1.2, oy + 7.0, 2.1, SIZE - 3.6, SIZE - 8.2, 0.22, "#e2e8f0", "canopy");
    canopy.g.classList.add("canopy");
    out.push({ depth: ox + oy + 99, node: canopy.g });
  } else {
    const tower = box(ox + 1, oy + 1, 0, 3.2, 3.2, 4.8, "#eef2ff", "bld");
    for (let f = 0; f < 6; f++) windows(tower, frontWindows(ox + 1.2, ox + 4.0, oy + 4.2, 0.5 + f * 0.7, 0.85 + f * 0.7, 3));
    add(tower, ox + oy + 4);
    const wing = box(ox + 4.8, oy + 1, 0, 3.8, 2.4, 1.3, "#f8fafc", "bld");
    windows(wing, frontWindows(ox + 5, ox + 8.4, oy + 3.4, 0.4, 0.9, 3));
    add(wing, ox + oy + 7);
  }
  return out;
}

function chargerLayout(count, connectors) {
  // Leave the right-hand strip for the cable and enough room between rows for a car.
  const [x0, x1, y0, y1] = [1.8, SIZE - 2.8, 7.4, SIZE - 2.2];
  const minDx = connectors > 1 ? 2.7 : 1.6;
  const maxCols = Math.max(1, Math.floor((x1 - x0) / minDx) + 1);
  const rows = Math.ceil(count / maxCols);
  const cols = Math.ceil(count / rows);
  const dx = cols > 1 ? (x1 - x0) / (cols - 1) : 0;
  const dy = rows > 1 ? Math.min(3.0, (y1 - y0) / (rows - 1)) : 0;
  return Array.from({ length: count }, (_, k) => [x0 + (k % cols) * dx, y0 + Math.floor(k / cols) * dy]);
}

function buildCar(x, y, color) {
  const g = el("g", { class: "car hidden" });
  const body = box(x, y, 0, 0.75, 1.25, 0.32, color, "car");
  const cabin = box(x + 0.08, y + 0.3, 0.32, 0.6, 0.65, 0.24, color === "#0f172a" ? "#475569" : color, "car");
  const [gx, gy] = iso(x + 0.38, y + 0.6, 0.75);
  const light = el("circle", { cx: gx, cy: gy, r: 3.2, class: "plug-light" });
  g.append(body.g, cabin.g, light);
  return { g, light };
}

function buildLabel(layer, site, ox, oy, index) {
  // Anchor on the platform's outer corner, so each label clearly belongs to one site.
  const [wx, wy] = CENTERS[index % CENTERS.length];
  const isLeft = wx - wy < 0;
  const [px, py] = isLeft ? iso(ox, oy + SIZE, 0) : iso(ox + SIZE, oy, 0);
  const ax = px + (isLeft ? 30 : -140);
  const ay = py - 150;
  const g = el("g", { class: "label-card clickable", transform: `translate(${ax.toFixed(0)},${ay.toFixed(0)})` });
  g.addEventListener("click", () => refs.onSelect({ type: "site", id: site.code }));
  const w = 272;
  g.append(el("rect", { x: -w / 2, y: -48, width: w, height: 82, rx: 10 }));
  g.append(el("text", { x: -w / 2 + 12, y: -28, "font-weight": 600 }, [site.code]));
  const sub = el("text", { x: -w / 2 + 12, y: -12, class: "small" }, ["…"]);
  g.append(sub);
  g.append(el("rect", { x: -w / 2 + 12, y: -2, width: w - 24, height: 6, rx: 3, class: "barbg" }));
  const bar = el("rect", { x: -w / 2 + 12, y: -2, width: 0, height: 6, rx: 3, class: "barfg" });
  g.append(bar);
  const demandLine = el("text", { x: -w / 2 + 12, y: 22, class: "small demand-line" }, [""]);
  g.append(demandLine);
  const tag = el("g", { visibility: "hidden" });
  const tagRect = el("rect", { y: -42, height: 18, rx: 9, class: "badge-down" });
  const tagText = el("text", { y: -29, "text-anchor": "middle", fill: "#fff", "font-size": 10, "font-weight": 700 }, [""]);
  tag.append(tagRect, tagText);
  g.append(tag);
  const weather = el("g", { class: "weather", transform: `translate(${w / 2 + 10},-30)` });
  const wIcon = el("g");
  const wText = el("text", { x: 30, y: 0 }, ["–"]);
  const wSub = el("text", { x: 30, y: 15, class: "w-sub" }, [""]);
  weather.append(wIcon, wText, wSub);
  g.append(weather);
  layer.append(g);
  return { g, sub, bar, barWidth: w - 24, w, tag, tagRect, tagText, wIcon, wText, wSub, demandLine };
}

function weatherIcon(w) {
  const g = [];
  if (!w) return g;
  const cloudy = (w.cloud_pct ?? 0) >= 60;
  if (w.is_day && !cloudy) g.push(el("circle", { cx: 12, cy: -4, r: 8, fill: "#fbbf24" }));
  else if (!w.is_day && !cloudy) g.push(el("circle", { cx: 12, cy: -4, r: 8, fill: "#cbd5e1" }), el("circle", { cx: 16, cy: -7, r: 7, class: "moon-cut" }));
  if ((w.cloud_pct ?? 0) >= 30) g.push(el("path", { d: "M2 4 a6 6 0 0 1 6-8 a8 8 0 0 1 15 2 a5 5 0 0 1 1 10 h-18 a5 5 0 0 1-4-4z", fill: cloudy ? "#cbd5e1" : "#e2e8f0" }));
  if ((w.gust_kmh ?? 0) >= 45) g.push(el("path", { d: "M0 12 h18 M4 16 h14", stroke: "#64748b", "stroke-width": 1.5, "stroke-linecap": "round" }));
  return g;
}

function setFlow(path, on, speedFactor) {
  path.classList.toggle("on", on);
  if (on) path.style.setProperty("--speed", `${Math.max(0.35, 2.4 - 2 * speedFactor).toFixed(2)}s`);
}

function setTag(label, text) {
  label.tag.setAttribute("visibility", text ? "visible" : "hidden");
  if (!text) return;
  const width = 12 + text.length * 6.2;
  label.tagRect.setAttribute("x", label.w / 2 - width - 8);
  label.tagRect.setAttribute("width", width);
  label.tagText.setAttribute("x", label.w / 2 - width / 2 - 8);
  label.tagText.textContent = text;
}

export function updateScene(snapshot, selected) {
  if (!refs) return;
  const picked = selected?.type === "station" ? refs.stations[selected.id] : null;
  refs.selectRing.setAttribute("visibility", picked ? "visible" : "hidden");
  if (picked) {
    refs.selectRing.setAttribute("cx", picked.pos[0]);
    refs.selectRing.setAttribute("cy", picked.pos[1]);
  }
  const NB = " ";
  const sitesByCode = Object.fromEntries(snapshot.sites.map((s) => [s.code, s]));
  const open = (snapshot.incidents || []).filter((i) => i.status === "open");
  const run = snapshot.scenario?.running;
  const hit = new Set(run?.stations ?? []);
  refs.scenarioLayer.replaceChildren(...[...hit].filter((id) => refs.stations[id]).map((id) => {
    const [cx, cy] = refs.stations[id].pos;
    return el("ellipse", { cx, cy, rx: 24, ry: 13, class: "scenario-ring" });
  }));
  const alertStations = new Set(open.flatMap((i) => i.stations));
  for (const [code, r] of Object.entries(refs.sites)) {
    const s = sitesByCode[code];
    if (!s) continue;
    const queue = s.demand?.queue ?? 0;
    r.label.sub.textContent = `${(s.power_w / 1000).toFixed(0)} of ${(s.cap_w / 1000).toFixed(0)}${NB}kW · ${s.free}/${s.connectors_total} free${queue ? ` · ${queue} waiting` : ""}${s.network.latency_ms ? ` · +${s.network.latency_ms}${NB}ms` : ""}`;
    const used = Math.min(1, s.cap_used ?? 0);
    r.label.bar.setAttribute("width", (r.label.barWidth * used).toFixed(1));
    r.label.bar.setAttribute("class", `barfg ${loadClass(s.cap_used)}`);
    r.queueCars.forEach((car, q) => car.g.classList.toggle("hidden", q >= queue));
    const d = s.demand || {};
    const tariff = s.economics?.tariff_eur_kwh;
    const priceEffect = d.price_factor != null && d.price_factor < 0.95 ? ` −${Math.round((1 - d.price_factor) * 100)}%${NB}price` : "";
    r.label.demandLine.textContent = `${d.reason ?? "–"} · ${Math.round(d.arrivals_per_hour ?? 0)} cars/h${priceEffect}${tariff != null ? ` · €${tariff.toFixed(2)}/kWh` : ""}`;
    r.queueText.textContent = queue ? `${queue} waiting · ${d.reason ?? ""}` : "";
    r.queueCars.forEach((car, q) => car.g.classList.toggle("more", q === QUEUE_SLOTS - 1 && queue > QUEUE_SLOTS));
    const siteOpen = open.filter((i) => i.site === code).length;
    const inScenario = run?.sites?.includes(code);
    r.g.classList.toggle("in-scenario", !!inScenario);
    setTag(r.label, !s.network.enabled ? "OUTAGE" : siteOpen ? `${siteOpen} INCIDENT${siteOpen > 1 ? "S" : ""}` : inScenario ? "SCENARIO" : "");
    const w = s.weather;
    r.label.wIcon.replaceChildren(...weatherIcon(w));
    r.label.wText.textContent = w && w.temp_c != null ? `${Math.round(w.temp_c)}°C` : "";
    const solar = s.solar_w ? ` · ☀${NB}${(s.solar_w / 1000).toFixed(0)}${NB}kW` : "";
    r.label.wSub.textContent = w && w.gust_kmh != null ? `gusts ${Math.round(w.gust_kmh)}${NB}km/h${solar}${w.stale ? " (stale)" : ""}` : "";
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
      const plugged = st.connected && WITH_CAR.has(c.status);
      car.g.classList.toggle("hidden", !plugged);
      car.light.setAttribute("fill", PLUG_COLORS[c.status] ?? "transparent");
      car.light.classList.toggle("glow", c.status === "Charging");
    });
    r.alert.setAttribute("visibility", alertStations.has(id) ? "visible" : "hidden");
    r.g.classList.toggle("selected", selected?.type === "station" && selected.id === id);
  }
}
