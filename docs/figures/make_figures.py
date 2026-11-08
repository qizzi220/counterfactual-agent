from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
INK = '#111111'
BLUE = '#c4e5ee'
GREEN = '#d4e9c5'
YELLOW = '#f0edc5'
ORANGE = '#f4dfb4'
PINK = '#efd1db'
PURPLE = '#ded9ee'
GRAY = '#f1f1f1'


def start(width, height, top=0):
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 {top} {width} {height}"><defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6.5" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 Z" fill="{INK}"/></marker></defs><rect y="{top}" width="100%" height="{height}" fill="white"/>']


def text(items, x, y, value, size=21, weight='normal', anchor='middle'):
    for index, line in enumerate(value.split('\n')):
        items.append(f'<text x="{x}" y="{y + index * (size + 3)}" text-anchor="{anchor}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{weight}" fill="{INK}">{escape(line)}</text>')


def box(items, x, y, width, height, label, fill, size=21):
    items.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="5" fill="{fill}" stroke="{INK}" stroke-width="2.2"/>')
    lines = label.count('\n') + 1
    text(items, x + width / 2, y + height / 2 - (lines - 1) * (size + 3) / 2 + size * .34, label, size)


def group(items, x, y, width, height):
    items.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="18" fill="{GRAY}" stroke="{INK}" stroke-width="2.2"/>')


def line(items, points, arrow=True):
    point_string = ' '.join(f'{x},{y}' for x, y in points)
    marker = 'marker-end="url(#arrow)"' if arrow else ''
    items.append(f'<polyline points="{point_string}" fill="none" stroke="{INK}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" {marker}/>')


def save(items, filename):
    items.append('</svg>')
    (ROOT / filename).write_text('\n'.join(items), encoding='utf-8')


items = start(820, 770, 45)
group(items, 145, 290, 230, 365)
group(items, 465, 390, 230, 205)
box(items, 170, 735, 180, 55, 'Task Context', PINK)
box(items, 490, 735, 180, 55, 'Data Snapshot', PINK)
box(items, 170, 575, 180, 50, 'Retrieve', ORANGE)
box(items, 170, 475, 180, 65, 'Generate SQL', BLUE)
box(items, 170, 325, 180, 65, 'Reflect', BLUE)
box(items, 170, 180, 180, 45, 'Answer', GREEN)
line(items, [(260, 735), (260, 625)])
line(items, [(260, 575), (260, 540)])
line(items, [(260, 475), (260, 390)])
line(items, [(260, 325), (260, 225)])
box(items, 490, 635, 180, 50, 'Perturb', ORANGE)
box(items, 490, 475, 180, 65, 'Execute', BLUE)
box(items, 490, 410, 180, 40, 'Compare', YELLOW)
box(items, 490, 285, 180, 45, 'Validate', YELLOW)
box(items, 490, 180, 180, 45, 'Memory Update', PURPLE, 20)
box(items, 490, 75, 180, 45, 'Lessons', GREEN)
line(items, [(580, 735), (580, 685)])
line(items, [(580, 635), (580, 540)])
line(items, [(580, 475), (580, 450)])
line(items, [(580, 410), (580, 330)])
line(items, [(580, 285), (580, 225)])
line(items, [(580, 180), (580, 120)])
line(items, [(350, 507), (490, 507)])
line(items, [(490, 430), (415, 430), (415, 357), (350, 357)])
line(items, [(170, 357), (115, 357), (115, 507), (170, 507)])
line(items, [(490, 97), (80, 97), (80, 600), (170, 600)])
save(items, 'architecture.svg')


items = start(820, 725, 90)
group(items, 105, 485, 610, 205)
box(items, 145, 735, 170, 55, 'Candidate q', PINK)
box(items, 335, 735, 150, 55, 'Snapshot Dᵢ', PINK, 19)
box(items, 505, 735, 170, 55, 'Reference q*', PINK, 20)
box(items, 145, 605, 170, 55, 'Execute', BLUE)
box(items, 505, 605, 170, 55, 'Execute', BLUE)
box(items, 310, 510, 200, 50, 'Multiset Match', YELLOW, 20)
line(items, [(200, 735), (200, 660)])
line(items, [(620, 735), (620, 660)])
line(items, [(410, 735), (410, 710), (270, 710), (270, 660)])
line(items, [(410, 710), (550, 710), (550, 660)])
line(items, [(230, 605), (230, 582), (360, 582), (360, 560)])
line(items, [(590, 605), (590, 582), (460, 582), (460, 560)])
box(items, 135, 405, 150, 45, 'Different', ORANGE)
box(items, 335, 405, 150, 45, 'Pass', GREEN)
box(items, 535, 405, 150, 45, 'Incomplete', GRAY, 20)
line(items, [(410, 510), (410, 472), (210, 472), (210, 450)])
line(items, [(410, 472), (410, 450)])
line(items, [(410, 472), (610, 472), (610, 450)])
box(items, 310, 300, 200, 45, 'Practice Gate', YELLOW, 20)
box(items, 310, 210, 200, 45, 'Holdout Gate', YELLOW, 20)
box(items, 310, 120, 200, 45, 'Memory Update', PURPLE, 20)
line(items, [(410, 405), (410, 345)])
line(items, [(410, 300), (410, 255)])
line(items, [(410, 210), (410, 165)])
line(items, [(135, 427), (65, 427), (65, 762), (145, 762)])
box(items, 555, 300, 110, 45, 'Stop', GRAY, 20)
line(items, [(610, 405), (610, 345)])
save(items, 'learning_cycle.svg')
