"""A Flipper, drawn: the LCD in a body, with buttons to click.

FlipperView is the widget in the middle of the window (fe_gui.py).  It shows
what the front-panel link reports: the picture on the LCD, the backlight, the
LED, the vibration motor, the speaker.  It turns the mouse and the keyboard
into button presses, and its ports are where a Flipper has them, to click:
the USB plug in its right side, the infrared window in the upper face of its
nose, the GPIO sockets along the top, the microSD card at the bottom right.

The drawing is this project's own, with the proportions of the real thing: a
body that narrows to a blunt nose at the left and has its top right corner cut
off, faces that slope away to the edges, a screen in a thin black line, one
round pad.  It is in units of one LCD pixel, so it is sharp at any zoom.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import math

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform

# ---- where everything is, in LCD pixels

VIEW_W, VIEW_H = 440, 180                       # (room at the right: the cable)
BX, BY, BW, BH = 14.0, 10.0, 380.0, 151.0       # the box around its body

# Its outline, clockwise from the top left, and how far each corner's round
# reaches.  The nose is at the left: the face above it has the infrared
# window.  The right side is upright: the USB port is in the middle of it.
OUTLINE = ((58, 0, 10), (326, 0, 16), (380, 62, 12), (380, 151, 8),
           (40, 151, 9), (0, 96, 4.5), (0, 83, 4.5))
# The flat front.  Between the two are the faces that slope away to the
# edges, each lit differently: how far towards the light (+) or the shade (-).
# The light is at the upper right.
FRONT = ((88, 4, 6), (315, 4, 10), (369, 74, 8), (369, 125, 5),
         (56, 125, 5), (36, 91, 2), (36, 87, 2))
SLOPES = (-0.12, 1.0, 0.85, -0.38, -0.5, -0.75, -0.95)

BEZEL = QRectF(BX + 89, BY + 18, 146, 88)       # the black line around ...
GLASS = BEZEL.adjusted(3, 3, -3, -3)            # ... the lit window, and in it
LCD = QRectF(BX + 98, BY + 30, 128, 64)         # the 128 x 64 pixels
PAD = QPointF(BX + 287, BY + 61)                # one round pad, OK in its middle
PAD_R, OK_R = 41.5, 13.5
BACK = QPointF(BX + 343, BY + 89)
BACK_R = 14.0
LED = QPointF(BX + 246, BY + 100)
MARK = QRectF(BX + 139, BY + 132.6, 150, 9.2)   # its name, on the bottom face

# The GPIO sockets are in the top edge: 8 of them, a gap, and 10 more
GPIO_PITCH = 10.3
GPIO_PINS = ([BX + 82 + i * GPIO_PITCH for i in range(8)]
             + [BX + 214.3 + i * GPIO_PITCH for i in range(10)])
GPIO_ROWS = (QRectF(GPIO_PINS[0] - 4.6, BY + 0.5, 7 * GPIO_PITCH + 9.2, 3.9),
             QRectF(GPIO_PINS[8] - 4.6, BY + 0.5, 9 * GPIO_PITCH + 9.2, 3.9))
SD_X, SD_W, SD_H = BX + 298, 40.0, 20.0         # the card, in the bottom edge
SD_OUT = (2.5, 13.0)                            # how far it shows: in, pulled
USB_OUT = 11.0                                  # how far an unplugged plug is

# What is in an edge is drawn in a frame of its own.  USB, in the right side:
# the origin is the port, x points out of it.  Infrared, in the upper face of
# the nose, which is at an angle: the origin is the middle of the window, x
# runs along the edge, y points into the body.
USB_T = QTransform().translate(BX + BW, BY + 106.5)
IR_T = QTransform().translate(BX + 29, BY + 41.5).rotate(
    math.degrees(math.atan2(-83, 58)))
IR_WINDOW = QRectF(-27, 0.5, 54, 4.6)

ZONES = {       # what is not round: where a click means it
    "usb": USB_T.map(QtGui.QPolygonF(QRectF(-7, -11, 46, 22))),
    "ir": IR_T.map(QtGui.QPolygonF(QRectF(-34, -12, 68, 23))),
    "gpio": QtGui.QPolygonF(QRectF(GPIO_PINS[0] - 8, 0, 17 * GPIO_PITCH + 61.4,
                                   BY + 9)),
    "sd": QtGui.QPolygonF(QRectF(SD_X - 4, BY + BH - 7, SD_W + 8,
                                 VIEW_H - BY - BH + 7)),
}

# Its name, in letters of this project's own: strokes on a grid, (x, y) with
# y down, each letter LETTER_H high
LETTER_H, LETTER_GAP, LETTER_PEN, LETTER_CUT = 8.0, 4.6, 2.3, 1.9
WORDMARK = "FLIPPEREMU"


def letters():
    h, m, w, c = LETTER_H, LETTER_H / 2, 11.0, LETTER_CUT
    bowl = [(0, h), (0, 0), (w - c, 0), (w, c), (w, m - c), (w - c, m), (0, m)]
    return {        # letter: (its width, its strokes)
        "F": (w, [[(w, 0), (0, 0), (0, h)], [(0, m), (w * 0.7, m)]]),
        "L": (w, [[(0, 0), (0, h), (w, h)]]),
        "I": (0, [[(0, 0), (0, h)]]),
        "P": (w, [bowl]),
        "E": (w, [[(w, 0), (0, 0), (0, h), (w, h)], [(0, m), (w * 0.7, m)]]),
        "R": (w, [bowl, [(w * 0.45, m), (w, h)]]),
        "M": (14.5, [[(0, h), (0, 0), (7.25, h * 0.62), (14.5, 0), (14.5, h)]]),
        "U": (w, [[(0, 0), (0, h - c), (c, h), (w - c, h), (w, h - c), (w, 0)]]),
    }


THEMES = {
    "white": dict(
        body="#f0eeea", light="#ffffff", shade="#c6c3be", edge="#a39f97",
        bezel="#131316", key="#f26d1f", key_hover="#ff8438", key_down="#d4570f",
        key_edge="#4a1f06", glyph="#b04a0c", text="#9a958a", accent="#f26d1f",
        port="#26262b", pin="#c9a227", ir="#3a1418"),
    "black": dict(
        body="#2c2c2f", light="#45454a", shade="#151517", edge="#060607",
        bezel="#050506", key="#3b3b40", key_hover="#4b4b51", key_down="#252528",
        key_edge="#0b0b0c", glyph="#ff8a1f", text="#7f7f87", accent="#ff8a1f",
        port="#08080a", pin="#c9a227", ir="#2a0c10"),
    "mocha": dict(
        body="#313244", light="#45475a", shade="#1e1e2e", edge="#11111b",
        bezel="#11111b", key="#cba6f7", key_hover="#d9bdfa", key_down="#a67fe0",
        key_edge="#3b2a5c", glyph="#6c4aa8", text="#a6adc8", accent="#fab387",
        port="#11111b", pin="#f9e2af", ir="#2a1420"),
}

# the LCD: an orange backlight behind dark pixels; without it, dim amber
LIT_BG, LIT_FG = "#ff8c1a", "#1a0e04"
UNLIT_BG, UNLIT_FG = "#8a5a22", "#1e1308"
DARK = "#1b140d"                                # a panel with no power

KEYMAP = {
    Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left",
    Qt.Key_Right: "right", Qt.Key_Return: "ok", Qt.Key_Enter: "ok",
    Qt.Key_Space: "ok", Qt.Key_Backspace: "back", Qt.Key_Escape: "back",
}
ARROWS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
SECTORS = {"right": -45, "up": 45, "left": 135, "down": 225}


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    f = lambda x, y: round(x + (y - x) * t)
    return QColor(f(a.red(), b.red()), f(a.green(), b.green()),
                  f(a.blue(), b.blue()))


def faded(colour, alpha):
    colour = QColor(colour)
    colour.setAlpha(alpha)
    return colour


def toward(a, b, d):
    """The point d away from a on the way to b"""
    length = math.hypot(b.x() - a.x(), b.y() - a.y())
    return QPointF(a.x() + (b.x() - a.x()) * d / length,
                   a.y() + (b.y() - a.y()) * d / length)


def rounded(points):
    """A polygon of (x, y, reach) with its corners rounded"""
    path = QPainterPath()
    for i, (x, y, reach) in enumerate(points):
        corner = QPointF(BX + x, BY + y)
        before = QPointF(BX + points[i - 1][0], BY + points[i - 1][1])
        after = points[(i + 1) % len(points)]
        after = QPointF(BX + after[0], BY + after[1])
        first = toward(corner, before, reach)
        if i:
            path.lineTo(first)
        else:
            path.moveTo(first)
        path.quadTo(corner, toward(corner, after, reach))
    path.closeSubpath()
    return path


def band(c1, r1, c2, r2):
    """What a band pulled tight around two circles encloses"""
    theta = math.atan2(c2.y() - c1.y(), c2.x() - c1.x())
    phi = math.acos((r1 - r2) / math.hypot(c2.x() - c1.x(), c2.y() - c1.y()))
    points = []
    for c, r, a0, a1 in ((c2, r2, theta - phi, theta + phi),
                         (c1, r1, theta + phi, theta - phi + 2 * math.pi)):
        steps = max(2, round(abs(a1 - a0) / math.radians(3)))
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * i / steps
            points.append(QPointF(c.x() + r * math.cos(a),
                                  c.y() + r * math.sin(a)))
    path = QPainterPath()
    path.addPolygon(QtGui.QPolygonF(points))
    path.closeSubpath()
    return path


def ring_sector(centre, r_out, r_in, start, span):
    out = QRectF(centre.x() - r_out, centre.y() - r_out, 2 * r_out, 2 * r_out)
    inn = QRectF(centre.x() - r_in, centre.y() - r_in, 2 * r_in, 2 * r_in)
    path = QPainterPath()
    path.arcMoveTo(out, start)
    path.arcTo(out, start, span)
    path.arcTo(inn, start + span, -span)
    path.closeSubpath()
    return path


def slope(i):
    """The face between edge i of the outline and edge i of the front, and
    the middles of those two edges"""
    j = (i + 1) % len(OUTLINE)
    a, b, c, d = (QPointF(BX + x, BY + y) for x, y in (
        OUTLINE[i][:2], OUTLINE[j][:2], FRONT[j][:2], FRONT[i][:2]))
    return QtGui.QPolygonF([a, b, c, d]), (a + b) / 2, (c + d) / 2


def wordmark():
    """The device's name as a shape to fill, in MARK, leaning forward"""
    shapes, strokes, x = letters(), QPainterPath(), 0.0
    for letter in WORDMARK:
        width, lines = shapes[letter]
        for line in lines:
            strokes.moveTo(x + line[0][0], line[0][1])
            for px, py in line[1:]:
                strokes.lineTo(x + px, py)
        x += width + LETTER_GAP
    pen = QtGui.QPainterPathStroker()
    pen.setWidth(LETTER_PEN)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    shape = pen.createStroke(strokes)
    shape.setFillRule(Qt.WindingFill)
    line = QPainterPath()                   # (no points above or below it)
    line.addRect(QRectF(-LETTER_PEN, -LETTER_PEN / 2, x + 2 * LETTER_PEN,
                        LETTER_H + LETTER_PEN))
    lean = QTransform().shear(-0.2, 0).map(shape.intersected(line))
    box = lean.boundingRect()
    k = min(MARK.width() / box.width(), MARK.height() / box.height())
    fit = QTransform().translate(MARK.center().x(), MARK.center().y())
    fit.scale(k, k)
    fit.translate(-box.center().x(), -box.center().y())
    return fit.map(lean)


BODY = rounded(OUTLINE)
FACE = rounded(FRONT)
SLOPE = [slope(i) for i in range(len(OUTLINE))]
HOLLOW = band(PAD, PAD_R + 3.4, BACK, BACK_R + 3.4)    # what the buttons sit in
NAME = wordmark()


def lcd_image(frame, bg=LIT_BG, fg=LIT_FG):
    """The frame of the panel link as a 128x64 picture."""
    frame = frame if frame and len(frame) == 1024 else bytes(1024)
    img = QtGui.QImage(frame, 128, 64, 16, QtGui.QImage.Format_Mono)
    img.setColorTable([QColor(bg).rgb(), QColor(fg).rgb()])
    return img.convertToFormat(QtGui.QImage.Format_RGB32)   # (its own copy)


def app_icon():
    """A small Flipper for the task bar."""
    t = THEMES["white"]
    pix = QtGui.QPixmap(128, 128)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    k = 120 / BW
    p.translate(4 - BX * k, 64 - (BY + BH / 2) * k)
    p.scale(k, k)
    p.setPen(QPen(QColor(t["edge"]), 5))
    p.setBrush(QColor(t["body"]))
    p.drawPath(BODY)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(t["bezel"]))
    p.drawRoundedRect(BEZEL.adjusted(-4, -4, 4, 4), 10, 10)
    p.setBrush(QColor(LIT_BG))
    p.drawRoundedRect(GLASS.adjusted(4, 4, -4, -4), 4, 4)
    p.setBrush(QColor(t["key"]))
    p.drawEllipse(PAD, PAD_R, PAD_R)
    p.drawEllipse(BACK, BACK_R + 2, BACK_R + 2)
    p.setBrush(QColor(t["body"]))
    p.drawEllipse(PAD, OK_R + 5, OK_R + 5)
    p.setBrush(QColor(t["key"]))
    p.drawEllipse(PAD, OK_R - 1, OK_R - 1)
    p.end()
    return QtGui.QIcon(pix)


class FlipperView(QtWidgets.QWidget):
    key = QtCore.Signal(str, bool)      # a button went down (True) or up
    usb_clicked = QtCore.Signal()
    sd_clicked = QtCore.Signal()
    port_clicked = QtCore.Signal(str, QtCore.QPoint)    # "ir" or "gpio"
    menu_wanted = QtCore.Signal(str, QtCore.QPoint)     # zone ("" for none)

    def __init__(self, name, scale=3, theme="white", parent=None):
        super().__init__(parent)
        self.name = name
        self.k = scale
        self.theme = THEMES.get(theme, THEMES["white"])
        self.grid = True
        self.frame = None               # 1024 bytes, or None: nothing yet
        self.backlight = 0
        self.led = 0
        self.vibro = False
        self.tone = False
        self.muted = False
        self.cable = "computer"         # computer | charger | unplugged
        self.sd = True                  # in the slot; False: out; None: none
        self.power = "wait"             # on | anything else: the LCD is dark
        self.hint = ()                  # ... and says this, in lines
        self.blank = False              # held in reset: dark, nothing said
        self.paused = ""                # time stands still on it: why
        self.ir_until = 0               # the IR window glows until then (ms)
        self.down = {}                  # key -> who holds it (mouse, keys)
        self.mouse_key = None
        self.mouse_zone = None
        self.hover = None
        self.shake = 0
        self.clock = QtCore.QElapsedTimer()
        self.clock.start()
        self.shaker = QtCore.QTimer(self)
        self.shaker.setInterval(33)
        self.shaker.timeout.connect(self._shake)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.setFixedSize(VIEW_W * self.k, VIEW_H * self.k)

    # ---- what the window tells it

    def _set(self, **kw):
        changed = False
        for name, value in kw.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self.update()
        return changed

    def set_scale(self, k):
        if self._set(k=k):
            self.setFixedSize(VIEW_W * k, VIEW_H * k)

    def set_theme(self, name):
        self._set(theme=THEMES.get(name, THEMES["white"]))

    def set_grid(self, on):
        self._set(grid=bool(on))

    def set_frame(self, data):
        self._set(frame=data)

    def set_backlight(self, level):
        self._set(backlight=max(0, min(255, level)))

    def set_led(self, rgb):
        self._set(led=rgb & 0xFFFFFF)

    def set_vibro(self, on):
        if self._set(vibro=bool(on)):
            if on:
                self.shaker.start()
            else:
                self.shaker.stop()
                self._set(shake=0)

    def set_tone(self, on):
        self._set(tone=bool(on))

    def set_muted(self, on):
        self._set(muted=bool(on))

    def set_cable(self, cable):
        self._set(cable=cable)

    def set_sd(self, state):
        self._set(sd=state)

    def set_power(self, power, *hint):
        """'on', or what else it is, with what to say on the dark screen"""
        self._set(power=power, hint=tuple(hint))
        if power != "on":
            self._set(led=0, tone=False, backlight=0)
            self.set_vibro(False)

    def set_blank(self, on):
        self._set(blank=bool(on))

    def set_paused(self, why):
        """Time stands still on it ("paused", "debug" ...), or goes on ("")"""
        self._set(paused=why or "")

    def flash_ir(self, ms=450):
        self.ir_until = self.clock.elapsed() + ms
        self.update()
        QtCore.QTimer.singleShot(ms + 30, self.update)

    def _shake(self):
        self.shake = self.shake % 4 + 1
        self.update()

    # ---- pictures of it

    def lcd_picture(self, scale=4):
        """The LCD alone, lit, as `flipperemu shot` saves it."""
        return lcd_image(self.frame).scaled(128 * scale, 64 * scale)

    def picture(self, scale=None):
        """The whole Flipper as it is drawn now, on nothing."""
        k = scale or self.k
        img = QtGui.QImage(VIEW_W * k, VIEW_H * k,
                           QtGui.QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.scale(k, k)
        self.draw(p, k)
        p.end()
        return img

    # ---- the buttons

    def pressed(self, key):
        return bool(self.down.get(key))

    def _down(self, key, who):
        holders = self.down.setdefault(key, set())
        first = not holders
        holders.add(who)
        if first:
            self.key.emit(key, True)
            self.update()

    def _up(self, key, who):
        holders = self.down.get(key)
        if holders and who in holders:
            holders.discard(who)
            if not holders:
                self.key.emit(key, False)
                self.update()

    def release_all(self, keyboard_only=False):
        for key, holders in list(self.down.items()):
            for who in list(holders):
                if who != "mouse" or not keyboard_only:
                    self._up(key, who)
        if not keyboard_only:
            self.mouse_key = None

    def zone_at(self, pos):
        """What is under a point of the widget: a key, a zone, or None"""
        x, y = pos.x() / self.k, pos.y() / self.k
        dx, dy = x - PAD.x(), y - PAD.y()
        d = math.hypot(dx, dy)
        if d <= OK_R + 1:
            return "ok"
        if d <= PAD_R + 1:
            if abs(dx) > abs(dy):
                return "right" if dx > 0 else "left"
            return "down" if dy > 0 else "up"
        if math.hypot(x - BACK.x(), y - BACK.y()) <= BACK_R + 2:
            return "back"
        for name, zone in ZONES.items():
            if zone.containsPoint(QPointF(x, y), Qt.OddEvenFill):
                return name
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.setFocus(Qt.MouseFocusReason)
        zone = self.zone_at(e.position())
        if zone in ARROWS or zone in ("ok", "back"):
            self.mouse_key = zone
            self._down(zone, "mouse")
        else:
            self.mouse_zone = zone

    mouseDoubleClickEvent = mousePressEvent     # a quick second press is one

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.mouse_key:
            self._up(self.mouse_key, "mouse")
            self.mouse_key = None
        zone, self.mouse_zone = self.mouse_zone, None
        if zone and zone == self.zone_at(e.position()):
            if zone == "usb":
                self.usb_clicked.emit()
            elif zone == "sd":
                self.sd_clicked.emit()
            else:
                self.port_clicked.emit(zone, e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        zone = self.zone_at(e.position())
        if self._set(hover=zone):
            self.setCursor(Qt.PointingHandCursor if zone else Qt.ArrowCursor)

    def leaveEvent(self, e):
        self._set(hover=None)
        self.unsetCursor()

    def contextMenuEvent(self, e):
        self.menu_wanted.emit(self.zone_at(e.pos()) or "", e.globalPos())

    def keyPressEvent(self, e):
        key = KEYMAP.get(e.key())
        if not key or e.modifiers() & (Qt.ControlModifier | Qt.AltModifier
                                       | Qt.MetaModifier):
            return super().keyPressEvent(e)
        if not e.isAutoRepeat():
            self._down(key, e.key())

    def keyReleaseEvent(self, e):
        key = KEYMAP.get(e.key())
        if not key:
            return super().keyReleaseEvent(e)
        if not e.isAutoRepeat():
            self._up(key, e.key())

    def focusOutEvent(self, e):
        self.release_all(keyboard_only=True)    # no key-up comes without focus
        super().focusOutEvent(e)

    def event(self, e):
        if e.type() == QtCore.QEvent.ToolTip:
            text = self.tip(self.zone_at(e.pos()))
            if text:
                QtWidgets.QToolTip.showText(e.globalPos(), text, self)
            else:
                QtWidgets.QToolTip.hideText()
            return True
        return super().event(e)

    def tip(self, zone):
        off = self.power != "on"
        return {
            "up": "Up (↑)", "down": "Down (↓)", "left": "Left (←)",
            "right": "Right (→)", "ok": "OK (Enter or Space)",
            "back": ("Back (Backspace or Esc): hold it to switch the Flipper on"
                     if off else "Back (Backspace or Esc)"),
            "usb": {"computer": "The USB cable, in a computer: click to unplug it",
                    "charger": "The USB cable, in a charger: click to unplug it",
                    "unplugged": "The USB cable: click to plug it into a computer"
                    }[self.cable],
            "sd": {True: "The microSD card: click to pull it out",
                   False: "The microSD card: click to push it in",
                   None: "No microSD card in this run"}[self.sd],
            "gpio": "The GPIO pins. What the firmware logs comes out of two "
                    "of them (13 and 14): click to read it",
            "ir": "The infrared window: click to point a remote at it",
        }.get(zone, "")

    # ---- the drawing

    def paintEvent(self, e):
        p = QPainter(self)
        p.scale(self.k, self.k)
        if self.shake:
            dx, dy = ((0.7, 0), (0, 0.5), (-0.7, 0), (0, -0.5))[self.shake - 1]
            p.translate(dx, dy)
        self.draw(p, self.k)
        p.end()

    def draw(self, p, k):
        t = {name: QColor(value) for name, value in self.theme.items()}
        p.setRenderHint(QPainter.Antialiasing)
        self.draw_behind(p, t)
        self.draw_cable(p, t)
        self.draw_body(p, t)
        self.draw_screen(p, t, k)
        self.draw_led(p, t)
        self.draw_keys(p, t)
        self.draw_marks(p, t)

    def draw_behind(self, p, t):
        """What sticks out from behind the body: the card, infrared light"""
        p.setPen(Qt.NoPen)
        if self.sd is not None:
            out = SD_OUT[0 if self.sd else 1]
            card = QRectF(SD_X, BY + BH + out - SD_H, SD_W, SD_H)
            p.setBrush(QColor("#4b4e58" if self.hover == "sd" else "#34363d"))
            p.drawRoundedRect(card, 2.0, 2.0)
            if not self.sd:                     # the lip a nail pulls it by
                p.setBrush(QColor("#9c9fa8"))
                p.drawRoundedRect(QRectF(card.x() + 6, card.bottom() - 5.2,
                                         SD_W - 12, 2.0), 1.0, 1.0)
        if self.clock.elapsed() < self.ir_until:
            p.save()
            p.setTransform(IR_T, True)
            p.setBrush(Qt.NoBrush)
            for i, r in enumerate((10, 15.5, 21)):
                p.setPen(QPen(QColor(255, 70, 90, 215 - 60 * i), 1.7,
                              Qt.SolidLine, Qt.RoundCap))
                p.drawArc(QRectF(-r, -r, 2 * r, 2 * r), 55 * 16, 70 * 16)
            p.restore()

    def draw_cable(self, p, t):
        """The USB plug in the right side, or a little way out of it"""
        out = 0.0 if self.cable != "unplugged" else USB_OUT
        end = USB_T.map(QPointF(out + 14, 0))
        wire = QPainterPath()                   # it hangs down, off the picture
        wire.moveTo(end)
        wire.cubicTo(QPointF(end.x() + 15, end.y()),
                     QPointF(end.x() + 13, VIEW_H - 22),
                     QPointF(end.x() + 5, VIEW_H + 6))
        p.setPen(QPen(QColor("#2b2c31"), 4.4, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawPath(wire)
        p.save()
        p.setTransform(USB_T, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#c3c6cd"))           # the metal that goes in
        p.drawRoundedRect(QRectF(out - 6, -3.6, 8, 7.2), 1.6, 1.6)
        p.setBrush(QColor("#5a5d66" if self.hover == "usb" else "#44464d"))
        p.drawRoundedRect(QRectF(out, -6.5, 15, 13), 2.6, 2.6)
        p.restore()
        c = USB_T.map(QPointF(out + 7.5, 0))    # what it is plugged into
        p.setPen(Qt.NoPen)
        if self.cable == "charger":             # a bolt: power, no data
            p.setBrush(QColor("#f9d949"))
            p.drawPolygon(QtGui.QPolygonF([
                QPointF(c.x() + 1.6, c.y() - 4.4), QPointF(c.x() - 2.8, c.y() + 0.9),
                QPointF(c.x() - 0.4, c.y() + 0.9), QPointF(c.x() - 1.6, c.y() + 4.4),
                QPointF(c.x() + 2.8, c.y() - 0.9), QPointF(c.x() + 0.4, c.y() - 0.9)]))
        else:                                   # data: a line between two ends
            mark = QColor("#e8e9ee" if self.cable == "computer" else "#8b8e97")
            p.setPen(QPen(mark, 1.1, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() - 3.4, c.y()), QPointF(c.x() + 2.0, c.y()))
            p.setPen(Qt.NoPen)
            p.setBrush(mark)
            p.drawEllipse(QPointF(c.x() - 3.5, c.y()), 1.3, 1.3)
            p.drawPolygon(QtGui.QPolygonF([QPointF(c.x() + 4.4, c.y()),
                                           QPointF(c.x() + 1.6, c.y() - 1.9),
                                           QPointF(c.x() + 1.6, c.y() + 1.9)]))

    def draw_body(self, p, t):
        p.setBrush(Qt.NoBrush)                  # the shadow it casts
        for width, alpha in ((10, 4), (7.5, 6), (5, 9), (3, 12), (1.4, 16)):
            p.setPen(QPen(QColor(0, 0, 0, alpha), width, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(BODY.translated(-0.6, 2.2))
        p.setPen(Qt.NoPen)
        p.setBrush(t["body"])
        p.drawPath(BODY)
        p.save()                                # the faces that slope away:
        p.setClipPath(BODY)                     # each a little rounder at
        for (face, outer, inner), lean in zip(SLOPE, SLOPES):   # its edge
            to = t["light" if lean > 0 else "shade"]
            fall = QtGui.QLinearGradient(outer, inner)
            fall.setColorAt(0.0, mix(t["body"], to, abs(lean)))
            fall.setColorAt(1.0, mix(t["body"], to, abs(lean) * 0.72))
            p.setPen(QPen(mix(t["body"], to, abs(lean) * 0.86), 0.4))
            p.setBrush(fall)                    # (the pen: no seam between two)
            p.drawPolygon(face)
        p.restore()
        front = QtGui.QLinearGradient(0, BY, 0, BY + 125)
        front.setColorAt(0.0, mix(t["body"], t["light"], 0.35))
        front.setColorAt(0.7, t["body"])
        front.setColorAt(1.0, mix(t["body"], t["shade"], 0.2))
        p.setPen(Qt.NoPen)
        p.setBrush(front)
        p.drawPath(FACE)
        p.save()                                # the infrared window, in the
        p.setTransform(IR_T, True)              # upper face of the nose
        glows = self.clock.elapsed() < self.ir_until
        window = mix(t["ir"], "#ff4660", 0.75) if glows else t["ir"]
        p.setBrush(mix(window, "#ffffff", 0.16) if self.hover == "ir" else window)
        p.drawRoundedRect(IR_WINDOW, 1.8, 1.8)
        p.setBrush(faded("#ffffff", 50))        # the light on its plastic
        p.drawRoundedRect(QRectF(IR_WINDOW.x() + 3, IR_WINDOW.y() + 0.7,
                                 IR_WINDOW.width() - 6, 0.9), 0.4, 0.4)
        p.restore()
        socket = mix(t["port"], "#ffffff", 0.22 if self.hover == "gpio" else 0)
        for row in GPIO_ROWS:                   # the GPIO sockets, in the top
            p.setBrush(socket)
            p.drawRoundedRect(row, 1.2, 1.2)
        for x in GPIO_PINS:
            p.setBrush(QColor("#050506"))
            p.drawRect(QRectF(x - 2.0, BY + 1.2, 4.0, 2.5))
            p.setBrush(t["pin"])                # (a glint of the contact in it)
            p.drawRect(QRectF(x - 0.8, BY + 2.6, 1.6, 1.1))
        p.setBrush(t["port"])                   # the slots in its edges
        p.save()
        p.setTransform(USB_T, True)
        p.drawRoundedRect(QRectF(-2.6, -5.4, 3.0, 10.8), 1.2, 1.2)
        p.restore()
        p.drawRoundedRect(QRectF(SD_X - 2, BY + BH - 2.6, SD_W + 4, 2.6), 1, 1)
        p.setPen(QPen(t["edge"], 0.9))
        p.setBrush(Qt.NoBrush)
        p.drawPath(BODY)

    def draw_screen(self, p, t, k):
        dark = self.power != "on" or self.blank or self.frame is None
        light = self.backlight / 255
        bg, fg = mix(UNLIT_BG, LIT_BG, light), mix(UNLIT_FG, LIT_FG, light)
        p.setPen(Qt.NoPen)
        p.setBrush(t["bezel"])
        p.drawRoundedRect(BEZEL, 7.5, 7.5)
        p.setBrush(QColor(DARK) if dark else bg)    # lit all over, not only
        p.drawRoundedRect(GLASS, 4.6, 4.6)          # behind the pixels
        if dark:
            if self.hint and not self.blank:
                font = QtGui.QFont(self.font())
                font.setPixelSize(9)
                font.setBold(True)
                p.setFont(font)
                p.setPen(QColor("#b98f5e"))
                p.drawText(LCD.adjusted(4, 8, -4, -34), Qt.AlignCenter,
                           self.hint[0])
                font.setPixelSize(6)
                font.setBold(False)
                p.setFont(font)
                p.setPen(QColor("#8a6c49"))
                p.drawText(LCD.adjusted(4, 28, -4, -4),
                           Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                           "\n".join(self.hint[1:]))
            return
        img = QtGui.QImage(self.frame, 128, 64, 16, QtGui.QImage.Format_Mono)
        img.setColorTable([bg.rgb(), fg.rgb()])
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.drawImage(LCD, img)
        if self.grid and k >= 3:                # the gaps between its pixels
            gap = QColor(bg)
            gap.setAlpha(70)
            p.setRenderHint(QPainter.Antialiasing, False)
            p.setPen(QPen(gap, 0))
            for i in range(1, 128):
                p.drawLine(QPointF(LCD.x() + i, LCD.y()),
                           QPointF(LCD.x() + i, LCD.bottom()))
            for i in range(1, 64):
                p.drawLine(QPointF(LCD.x(), LCD.y() + i),
                           QPointF(LCD.right(), LCD.y() + i))
            p.setRenderHint(QPainter.Antialiasing, True)
        if self.paused:
            self.draw_paused(p, fg)

    def draw_paused(self, p, colour):
        """Time stands still, or the firmware does (it has halted): said on
        the glass above the pixels, which are the firmware's, and stay as it
        left them"""
        text = {"paused": "PAUSED", "halted": "HALTED",
                "debug": "STOPPED BY A DEBUGGER"}.get(self.paused, "STOPPED")
        font = QtGui.QFont(self.font())
        font.setPixelSize(6)
        font.setBold(True)
        font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 0.6)
        p.setFont(font)
        width = QtGui.QFontMetricsF(font).horizontalAdvance(text)
        x = LCD.center().x() - (width + 7) / 2
        y = (GLASS.top() + LCD.top()) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        for bar in (0, 2.8):
            p.drawRect(QRectF(x + bar, y - 2.4, 1.6, 4.8))
        p.setPen(colour)
        p.drawText(QRectF(x + 7, y - 4, width + 2, 8),
                   Qt.AlignLeft | Qt.AlignVCenter, text)

    def draw_led(self, p, t):
        led = self.led if self.power == "on" and not self.blank else 0
        p.setPen(Qt.NoPen)
        if led:
            colour = QColor(led)
            level = max(colour.red(), colour.green(), colour.blue()) / 255
            glow = QtGui.QRadialGradient(LED, 12)
            glow.setColorAt(0, faded(colour, round(170 * level)))
            glow.setColorAt(1, faded(colour, 0))
            p.setBrush(glow)
            p.drawEllipse(LED, 12, 12)
            p.setBrush(mix("#ffffff", colour, 0.55))
        else:
            p.setBrush(mix(t["shade"], "#000000", 0.45))
        p.drawEllipse(LED, 2.7, 2.7)

    def key_brush(self, t, name, centre, radius):
        """A button's colour, lit from the upper left as its plastic is"""
        colour = (t["key_down"] if self.pressed(name) else
                  t["key_hover"] if self.hover == name else t["key"])
        shine = QtGui.QRadialGradient(
            QPointF(centre.x() - radius * 0.35, centre.y() - radius * 0.45),
            radius * 1.7)
        shine.setColorAt(0, mix(colour, "#ffffff", 0.10))
        shine.setColorAt(1, mix(colour, "#000000", 0.10))
        return shine

    def draw_keys(self, p, t):
        p.setPen(QPen(faded(t["edge"], 150), 0.7))      # what they sit in
        p.setBrush(mix(t["body"], t["shade"], 0.16))
        p.drawPath(HOLLOW)
        p.setPen(QPen(t["key_edge"], 1.0))
        p.setBrush(self.key_brush(t, "pad", PAD, PAD_R))
        p.drawEllipse(PAD, PAD_R, PAD_R)
        p.setPen(Qt.NoPen)                      # the quarter under a finger
        for name, start in SECTORS.items():
            if self.pressed(name) or self.hover == name:
                p.setBrush(t["key_down" if self.pressed(name) else "key_hover"])
                p.drawPath(ring_sector(PAD, PAD_R - 0.5, OK_R + 0.6, start, 90))
        p.setPen(QPen(t["glyph"], 1.15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)                  # arrows, cut into the plastic
        for name, (dx, dy) in ARROWS.items():
            push = 0.7 if self.pressed(name) else 0
            tip, base, half = 32.5 - push, 25.0 - push, 5.4
            p.drawPolygon(QtGui.QPolygonF([
                QPointF(PAD.x() + dx * tip, PAD.y() + dy * tip),
                QPointF(PAD.x() + dx * base - dy * half,
                        PAD.y() + dy * base + dx * half),
                QPointF(PAD.x() + dx * base + dy * half,
                        PAD.y() + dy * base - dx * half)]))
        p.setPen(QPen(t["key_edge"], 1.3))
        p.setBrush(self.key_brush(t, "ok", PAD, OK_R))
        p.drawEllipse(PAD, OK_R, OK_R)
        p.setPen(QPen(t["key_edge"], 1.0))
        p.setBrush(self.key_brush(t, "back", BACK, BACK_R))
        p.drawEllipse(BACK, BACK_R, BACK_R)
        x, y = BACK.x() + 0.4, BACK.y() + (0.6 if self.pressed("back") else 0)
        hook = QPainterPath()                   # an arrow that turns back
        hook.moveTo(x - 4.2, y - 4.2)
        hook.lineTo(x + 1.6, y - 4.2)
        hook.arcTo(QRectF(x - 2.1, y - 4.2, 7.4, 7.4), 90, -180)
        hook.lineTo(x - 5.8, y + 3.2)
        hook.moveTo(x - 3.0, y + 0.3)
        hook.lineTo(x - 6.0, y + 3.2)
        hook.lineTo(x - 3.0, y + 6.1)
        p.setPen(QPen(t["glyph"], 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(hook)

    def draw_marks(self, p, t):
        """Its name on the bottom face, and the speaker's sign by the nose"""
        p.setPen(Qt.NoPen)
        p.setBrush(t["accent"])
        p.drawPath(NAME)
        font = QtGui.QFont(self.font())
        font.setPixelSize(6)
        font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 0.4)
        p.setFont(font)
        p.setPen(t["text"])
        name = QtGui.QFontMetricsF(font).elidedText(self.name, Qt.ElideRight, 56)
        p.drawText(QRectF(BX + 62, BY + 130, 60, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, name)
        sounding = self.tone and self.power == "on" and not self.muted
        if not (sounding or self.muted):
            return
        cx, cy = BX + 43.0, BY + 52.0
        colour = t["accent"] if sounding else t["text"]
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawPolygon(QtGui.QPolygonF([
            QPointF(cx - 5, cy - 1.4), QPointF(cx - 3.2, cy - 1.4),
            QPointF(cx - 0.8, cy - 3.4), QPointF(cx - 0.8, cy + 3.4),
            QPointF(cx - 3.2, cy + 1.4), QPointF(cx - 5, cy + 1.4)]))
        p.setPen(QPen(colour, 1.0, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        if self.muted:
            p.drawLine(QPointF(cx + 1.2, cy - 2), QPointF(cx + 5.2, cy + 2))
            p.drawLine(QPointF(cx + 1.2, cy + 2), QPointF(cx + 5.2, cy - 2))
        else:
            for r in (2.4, 4.6, 6.8):
                p.drawArc(QRectF(cx - 1.4 - r, cy - r, 2 * r, 2 * r),
                          -40 * 16, 80 * 16)
