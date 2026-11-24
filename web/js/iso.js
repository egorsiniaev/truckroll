// Isometric geometry helpers: world units (x, y on the ground, z up) to SVG pixels.
export const U = 20;
const COS = Math.cos(Math.PI / 6);
const SIN = 0.5;
const NS = "http://www.w3.org/2000/svg";

export function iso(x, y, z = 0) {
  return [(x - y) * COS * U, (x + y) * SIN * U - z * U];
}

export function pts(list) {
  return list.map(([x, y, z]) => iso(x, y, z).map((n) => n.toFixed(1)).join(",")).join(" ");
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

export function shade(hex, factor) {
  const n = parseInt(hex.slice(1), 16);
  const ch = (s) => Math.max(0, Math.min(255, Math.round(((n >> s) & 255) * factor)));
  return `#${[ch(16), ch(8), ch(0)].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

// A box with its back corner at (x, y, z); returns the group and its three faces for recolouring.
export function box(x, y, z, w, d, h, color, cls = "") {
  const top = el("polygon", { class: `${cls}-top`, points: pts([[x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]]) });
  const left = el("polygon", { class: `${cls}-left`, points: pts([[x, y + d, z], [x + w, y + d, z], [x + w, y + d, z + h], [x, y + d, z + h]]) });
  const right = el("polygon", { class: `${cls}-right`, points: pts([[x + w, y, z], [x + w, y + d, z], [x + w, y + d, z + h], [x + w, y, z + h]]) });
  const g = el("g", {}, [left, right, top]);
  paintBox({ top, left, right }, color);
  return { g, faces: { top, left, right } };
}

export function paintBox(faces, color) {
  faces.top.setAttribute("fill", color);
  faces.left.setAttribute("fill", shade(color, 0.86));
  faces.right.setAttribute("fill", shade(color, 0.72));
}

export function line(points, cls) {
  return el("polyline", { class: cls, points: pts(points) });
}
