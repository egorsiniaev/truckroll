"""Draws docs/desk-scene.svg: the planned desk diorama in isometric view, sizes in mm."""

import math
from pathlib import Path

C, S = math.cos(math.radians(30)), math.sin(math.radians(30))
OX, OY = 600, 280


def p(x, y, z=0):
    return OX + (x - y) * C, OY + (x + y) * S - z


def poly(pts, fill, stroke="#334155", sw=1.2, extra=""):
    d = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    return f'<polygon points="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round" {extra}/>'


def box(x0, y0, z0, dx, dy, dz, top, left, right, sw=1.2):
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    return "".join([
        poly([p(x0, y1, z0), p(x1, y1, z0), p(x1, y1, z1), p(x0, y1, z1)], left, sw=sw),   # +y face
        poly([p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1)], right, sw=sw),  # +x face
        poly([p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1)], top, sw=sw),
    ])


def circle_y(cx, y, cz, r, n=40):
    """Circle lying on a +y face (plane x-z)."""
    return [p(cx + r * math.cos(t), y, cz + r * math.sin(t)) for t in (2 * math.pi * i / n for i in range(n))]


def circle_z(cx, cy, z, r, n=40):
    return [p(cx + r * math.cos(t), cy + r * math.sin(t), z) for t in (2 * math.pi * i / n for i in range(n))]


def rect_y(x0, y, z0, w, h, fill, stroke="#334155", sw=1):
    return poly([p(x0, y, z0), p(x0 + w, y, z0), p(x0 + w, y, z0 + h), p(x0, y, z0 + h)], fill, stroke, sw)


def label(anchor, text_xy, lines, color="#0f172a"):
    ax, ay = anchor
    tx, ty = text_xy
    out = [f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="#64748b" stroke-width="1"/>',
           f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="2.5" fill="#64748b"/>']
    anchor_end = "end" if tx < ax else "start"
    dx = -6 if tx < ax else 6
    for i, line in enumerate(lines):
        weight = 700 if i == 0 else 400
        size = 13 if i == 0 else 11.5
        fill = color if i == 0 else "#475569"
        out.append(f'<text x="{tx + dx:.1f}" y="{ty + 4 + i * 15:.1f}" text-anchor="{anchor_end}" '
                   f'font-size="{size}" font-weight="{weight}" fill="{fill}">{line}</text>')
    return "".join(out)


parts = []
# Base: two printed tiles, 250 x 180 mm each, joined along x = 250.
for x0 in (0, 250):
    parts.append(box(x0, 0, -8, 250, 180, 8, "#e2e8f0", "#cbd5e1", "#94a3b8"))
# Parking bay markings and a green EV bay.
parts.append(poly([p(150, 70), p(360, 70), p(360, 160), p(150, 160)], "#bbf7d0", "#16a34a", 1, 'opacity="0.55"'))
for x in (150, 360):
    parts.append(f'<line x1="{p(x, 70)[0]:.1f}" y1="{p(x, 70)[1]:.1f}" x2="{p(x, 160)[0]:.1f}" '
                 f'y2="{p(x, 160)[1]:.1f}" stroke="#f8fafc" stroke-width="3"/>')
parts.append(poly([p(240, 105), p(270, 105), p(258, 120), p(278, 120), p(236, 140), p(250, 124), p(230, 124)],
                  "#16a34a", "none", 0, 'opacity="0.7"'))
# Queue lane for a waiting car.
parts.append(poly([p(380, 70), p(500, 70), p(500, 160), p(380, 160)], "#e5e7eb", "#94a3b8", 1, 'stroke-dasharray="5 4"'))

# Grid box (back right) with ring 2 on top.
parts.append(box(118, 0, 0, 80, 80, 70, "#f1f5f9", "#cbd5e1", "#94a3b8"))
parts.append(poly(circle_z(158, 40, 70, 30), "none", "#f59e0b", 6))
parts.append(poly(circle_z(158, 40, 70, 30), "none", "#fde68a", 2))
parts.append(rect_y(130, 80, 30, 56, 22, "#fef3c7"))
parts.append(f'<text x="{p(158, 80, 37)[0]:.1f}" y="{p(158, 80, 37)[1]:.1f}" font-size="11" text-anchor="middle" '
             f'fill="#92400e" font-weight="700">GRID</text>')
# Cable from grid box to the charger, dashed like the page's power flow.
gx, gy = p(118, 40, 4)
cx_, cy_ = p(110, 50, 4)
parts.append(f'<path d="M{gx:.1f},{gy:.1f} L{cx_:.1f},{cy_:.1f}" stroke="#f59e0b" stroke-width="2.5" '
             f'stroke-dasharray="7 5" fill="none"/>')

# Charger pedestal: 80 x 50 mm, 190 mm tall, front on the +y face.
parts.append(box(30, 20, 0, 80, 50, 190, "#f8fafc", "#ffffff", "#e2e8f0"))
parts.append(box(26, 16, 190, 88, 58, 8, "#0f766e", "#115e59", "#134e4a"))  # cap
parts.append(poly(circle_y(70, 70, 150, 30), "none", "#2563eb", 7))  # ring 1 halo (charging: blue)
parts.append(poly(circle_y(70, 70, 150, 30), "none", "#93c5fd", 2))
parts.append(rect_y(52, 70, 138, 36, 22, "#0f172a"))  # OLED inside the ring
for i, t in enumerate(("7.4 kW", "3.1 kWh")):
    tx, ty = p(70, 70, 154 - i * 9)
    parts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="8" text-anchor="middle" fill="#67e8f9" '
                 f'font-family="ui-monospace,monospace">{t}</text>')
parts.append(rect_y(48, 70, 78, 44, 32, "#ccfbf1", "#0f766e"))  # RFID tap zone
tx, ty = p(70, 70, 92)
parts.append(f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="9" text-anchor="middle" fill="#0f766e" '
             f'font-weight="700">TAP CARD</text>')
parts.append(poly(circle_y(70, 70, 55, 9), "#dc2626", "#7f1d1d", 1.5))  # emergency button
parts.append(poly(circle_y(70, 70, 55, 5), "#ef4444", "none", 0))
parts.append(rect_y(92, 70, 18, 14, 24, "#cbd5e1"))  # plug holster (empty: plug is in the car)

# Taxi in the bay (1:24, about 170 x 70 mm), metal body, rear to the charger.
car = [box(170, 85, 8, 170, 68, 30, "#facc15", "#eab308", "#ca8a04"),
       box(215, 89, 38, 85, 60, 24, "#fde047", "#a3a3a3", "#737373"),
       box(245, 108, 62, 22, 22, 10, "#fef9c3", "#fde68a", "#fcd34d", 1)]
parts.extend(car)
# Windows glow: the LED strip inside follows the charging power.
parts.append(rect_y(222, 149, 42, 70, 17, "#fef08a", "#a16207"))
parts.append(f'<polygon points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in [p(222, 149, 42), p(292, 149, 42), p(292, 149, 59), p(222, 149, 59)])}" '
             f'fill="#fde047" opacity="0.8" filter="url(#glow)"/>')
for wx in (200, 310):  # wheels
    parts.append(poly(circle_y(wx, 153, 10, 11), "#1f2937", "#111827", 1))
    parts.append(poly(circle_y(wx, 153, 10, 5), "#d1d5db", "none", 0))
# GX12 socket on the rear side panel and the cable from the charger.
parts.append(poly(circle_y(182, 153, 26, 5), "#94a3b8", "#334155", 1))
hx, hy = p(110, 45, 30)
sx, sy = p(182, 153, 26)
parts.append(f'<path d="M{hx:.1f},{hy:.1f} C{hx + 30:.1f},{hy + 70:.1f} {sx - 60:.1f},{sy + 30:.1f} {sx:.1f},{sy:.1f}" '
             f'stroke="#111827" stroke-width="4" fill="none" stroke-linecap="round"/>')

# Waiting pickup (1:38) in the queue lane, no electronics.
parts.append(box(395, 95, 8, 100, 45, 20, "#1f2937", "#111827", "#030712"))
parts.append(box(395, 97, 28, 40, 41, 18, "#374151", "#1f2937", "#111827"))
for wx in (410, 478):
    parts.append(poly(circle_y(wx, 140, 8, 8), "#111827", "#000", 1))

# Card rack in front of the charger: three cards for the robot later.
for i, (col, name) in enumerate((("#16a34a", "FLEET"), ("#f59e0b", "EXPIRED"), ("#64748b", "UNKNOWN"))):
    parts.append(box(30 + i * 30, 120, 0, 22, 34, 4, "#e2e8f0", "#cbd5e1", "#94a3b8", 1))
    parts.append(rect_y(32 + i * 30, 146, 4, 18, 26, "#ffffff", col, 1.5))
    parts.append(rect_y(32 + i * 30, 146, 24, 18, 6, col, col, 1))

labels = [
    (p(70, 70, 178), (330, 120), ["Ring 1: status halo", "amber plugged · blue charging · red fault"]),
    (p(60, 70, 145), (330, 185), ["OLED", "kW, kWh, price, receipt"]),
    (p(55, 70, 94), (330, 255), ["RFID tap zone", "RC522 behind 2 mm of plastic"]),
    (p(70, 70, 55), (330, 320), ["Emergency button", "Faulted, relay opens"]),
    (p(100, 70, 25), (330, 385), ["Plug holster", "for when the cable is unplugged"]),
    (p(80, 135, 6), (330, 470), ["Card rack", "fleet · expired · unknown"]),
    (p(158, 40, 70), (930, 130), ["Ring 2 on the grid box", "site load vs grid cap", "pulses on grid requests"]),
    (p(256, 119, 72), (930, 300), ["Roof sign", "optional: lit while charging"]),
    (p(450, 118, 30), (930, 480), ["Waiting car (1:38)", "the queue, no electronics"]),
    (p(182, 153, 26), (470, 590), ["GX12 4-pin socket", "2 pins 12 V, 2 pins plug detect"]),
    (p(250, 149, 50), (760, 610), ["LED strip inside the taxi", "brightness = charging power"]),
]
for anchor, at, lines in labels:
    parts.append(label(anchor, at, lines))

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 660" font-family="ui-sans-serif,system-ui,sans-serif">
<defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter></defs>
<rect width="1100" height="660" fill="#fafaf9"/>
<text x="24" y="36" font-size="20" font-weight="700" fill="#0f172a">Truckroll desk charger: planned diorama</text>
<text x="24" y="58" font-size="12.5" fill="#475569">Two printed base tiles of 250 x 180 mm (Kobra 3 bed: 250 x 250). Charger 90 x 55 x 200 mm, out of scale on purpose so the ring, screen and reader fit.</text>
{"".join(parts)}
</svg>'''
Path(__file__).with_name("desk-scene.svg").write_text(svg)
