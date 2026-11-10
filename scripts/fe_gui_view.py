"""A Flipper, drawn: the LCD in a body, with buttons to click.

FlipperView is the widget in the middle of the window (fe_gui.py).  It shows
what the front-panel link reports: the picture on the LCD, the backlight, the
LED, the vibration motor, the speaker.  It turns the mouse and the keyboard
into button presses, and it has a USB plug and a microSD card to click.

The drawing is this project's own (a rounded body, a screen, a round pad), in
units of one LCD pixel, so it is sharp at any zoom.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import math

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

# ---- where everything is, in LCD pixels

VIEW_W, VIEW_H = 350, 156
BX, BY, BW, BH = 22.0, 16.0, 290.0, 124.0
BODY = QRectF(BX, BY, BW, BH)
BODY_R = 24.0
BEZEL = QRectF(BX + 14, BY + 14, 148, 84)       # the dark glass
LCD = QRectF(BX + 24, BY + 24, 128, 64)
PAD = QPointF(BX + 222, BY + 58)                # the ring of four, around OK
PAD_R, PAD_R_IN, OK_R = 38.0, 16.0, 13.5
BACK = QPointF(BX + 268, BY + 102)
BACK_R = 10.0
LED = QPointF(BX + 178, BY + 20)
USB_Y = BY + 62                                 # the port, on the right edge
USB_OUT = 11.0                                  # how far an unplugged plug is
SD_X, SD_W, SD_H = BX + 48, 26.0, 16.0          # the card, in the bottom edge
SD_OUT = (2.5, 12.0)                            # how far it shows: in, pulled

ZONES = {       # what is not round: where a click means it
    "usb": QRectF(BX + BW - 4, USB_Y - 11, VIEW_W - BX - BW + 4, 22),
    "sd": QRectF(SD_X - 4, BY + BH - 6, SD_W + 8, VIEW_H - BY - BH + 6),
    "gpio": QRectF(BX + 148, BY - 6, 118, 10),
    "ir": QRectF(BX - 6, BY + 24, 14, BH - 48),
}

THEMES = {
    "white": dict(
        body="#f6f3ec", body2="#e4e0d5", edge="#b9b4a7", rim="#ffffff",
        bezel="#17171b", key="#ff8a1f", key_hover="#ff9d42", key_down="#d96c06",
        key_edge="#b85a05", glyph="#7a3a00", text="#8f897b", accent="#ff8a1f",
        port="#3b3b40", pin="#c9a227", ir="#3a1418"),
    "black": dict(
        body="#303033", body2="#1d1d20", edge="#0b0b0d", rim="#47474c",
        bezel="#0a0a0c", key="#414146", key_hover="#515157", key_down="#29292d",
        key_edge="#121214", glyph="#ff8a1f", text="#85858c", accent="#ff8a1f",
        port="#0a0a0c", pin="#c9a227", ir="#2a0c10"),
    "mocha": dict(
        body="#313244", body2="#1e1e2e", edge="#11111b", rim="#45475a",
        bezel="#11111b", key="#cba6f7", key_hover="#d9bdfa", key_down="#a67fe0",
        key_edge="#7f5fb5", glyph="#1e1e2e", text="#a6adc8", accent="#fab387",
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


def ring_sector(centre, r_out, r_in, start, span):
    out = QRectF(centre.x() - r_out, centre.y() - r_out, 2 * r_out, 2 * r_out)
    inn = QRectF(centre.x() - r_in, centre.y() - r_in, 2 * r_in, 2 * r_in)
    path = QPainterPath()
    path.arcMoveTo(out, start)
    path.arcTo(out, start, span)
    path.arcTo(inn, start + span, -span)
    path.closeSubpath()
    return path


def lcd_image(frame, bg=LIT_BG, fg=LIT_FG):
    """The frame of the panel link as a 128x64 picture."""
    frame = frame if frame and len(frame) == 1024 else bytes(1024)
    img = QtGui.QImage(frame, 128, 64, 16, QtGui.QImage.Format_Mono)
    img.setColorTable([QColor(bg).rgb(), QColor(fg).rgb()])
    return img.convertToFormat(QtGui.QImage.Format_RGB32)   # (its own copy)


def app_icon():
    """A small Flipper for the task bar."""
    pix = QtGui.QPixmap(128, 128)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#b9b4a7"), 3))
    p.setBrush(QColor("#f6f3ec"))
    p.drawRoundedRect(QRectF(6, 34, 116, 60), 20, 20)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#17171b"))
    p.drawRoundedRect(QRectF(16, 44, 58, 40), 6, 6)
    p.setBrush(QColor(LIT_BG))
    p.drawRect(QRectF(21, 49, 48, 30))
    p.setBrush(QColor("#ff8a1f"))
    p.drawEllipse(QPointF(96, 60), 15, 15)
    p.setBrush(QColor("#f6f3ec"))
    p.drawEllipse(QPointF(96, 60), 6.5, 6.5)
    p.setBrush(QColor("#ff8a1f"))
    p.drawEllipse(QPointF(96, 60), 4.5, 4.5)
    p.drawEllipse(QPointF(112, 82), 5, 5)
    p.end()
    return QtGui.QIcon(pix)


class FlipperView(QtWidgets.QWidget):
    key = QtCore.Signal(str, bool)      # a button went down (True) or up
    usb_clicked = QtCore.Signal()
    sd_clicked = QtCore.Signal()
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
        for name, rect in ZONES.items():
            if rect.contains(x, y):
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

    def mouseMoveEvent(self, e):
        zone = self.zone_at(e.position())
        if zone in ("gpio", "ir"):
            zone = None                 # they are there to be seen
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
            "gpio": "The GPIO pins: nothing is connected to them here",
            "ir": "The infrared window",
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
        body = QPainterPath()
        body.addRoundedRect(BODY, BODY_R, BODY_R)
        self.draw_behind(p, t)
        self.draw_cable(p, t)
        p.setPen(Qt.NoPen)                      # the shadow it casts
        for grow, alpha in ((4, 5), (3, 8), (2, 12), (1, 16)):
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(BODY.adjusted(-grow, 1.5 - grow, grow, 2.5 + grow),
                              BODY_R + grow, BODY_R + grow)
        shade = QtGui.QLinearGradient(0, BY, 0, BY + BH)
        shade.setColorAt(0, t["body"])
        shade.setColorAt(1, t["body2"])
        p.setPen(QPen(t["edge"], 1.0))
        p.setBrush(shade)
        p.drawPath(body)
        p.setPen(QPen(t["rim"], 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(BODY.adjusted(2.2, 2.2, -2.2, -2.2),
                          BODY_R - 2.2, BODY_R - 2.2)
        p.save()                                # its dark end: the IR window
        p.setClipPath(body)
        p.fillRect(QRectF(BX, BY, 8.5, BH), t["ir"])
        p.restore()
        p.setPen(QPen(t["edge"], 1.0))
        p.drawPath(body)
        p.setPen(Qt.NoPen)                      # the slots in its edges
        p.setBrush(t["port"])
        p.drawRoundedRect(QRectF(BX + BW - 2.4, USB_Y - 4.5, 2.4, 9), 1, 1)
        p.drawRoundedRect(QRectF(SD_X - 2, BY + BH - 2.4, SD_W + 4, 2.4), 1, 1)
        self.draw_screen(p, t, k)
        self.draw_led(p, t)
        self.draw_keys(p, t)
        self.draw_marks(p, t)

    def draw_behind(self, p, t):
        """What sticks out from behind the body: pins, the card, IR light"""
        p.setPen(Qt.NoPen)
        p.setBrush(t["port"])
        p.drawRoundedRect(QRectF(BX + 150, BY - 3.6, 114, 8), 1.5, 1.5)
        p.setBrush(t["pin"])
        for i in range(18):
            p.drawEllipse(QPointF(BX + 156 + i * 6, BY - 1.7), 0.9, 0.9)
        if self.sd is not None:
            out = SD_OUT[0 if self.sd else 1]
            card = QRectF(SD_X, BY + BH + out - SD_H, SD_W, SD_H)
            p.setBrush(QColor("#4a4c55" if self.hover == "sd" else "#34363d"))
            p.drawRoundedRect(card, 1.6, 1.6)
            if not self.sd:
                p.setBrush(QColor("#c8cad2"))
                p.drawRoundedRect(QRectF(card.x() + 3, card.bottom() - 8.5,
                                         SD_W - 6, 5.5), 0.8, 0.8)
        if self.clock.elapsed() < self.ir_until:
            p.setBrush(Qt.NoBrush)
            centre = QPointF(BX + 2, BY + BH / 2)
            for i, r in enumerate((9, 14, 19)):
                p.setPen(QPen(QColor(255, 70, 90, 210 - 60 * i), 1.6,
                              Qt.SolidLine, Qt.RoundCap))
                p.drawArc(QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r),
                          145 * 16, 70 * 16)

    def draw_cable(self, p, t):
        """The USB plug in the right edge, or a little way out of it"""
        x = BX + BW + (USB_OUT if self.cable == "unplugged" else 0)
        hover = self.hover == "usb"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#c3c6cd"))           # the metal that goes in
        p.drawRoundedRect(QRectF(x - 6, USB_Y - 3.6, 8, 7.2), 1.6, 1.6)
        wire = QPainterPath()
        wire.moveTo(x + 13, USB_Y)
        wire.cubicTo(x + 24, USB_Y, VIEW_W - 8, USB_Y + 6, VIEW_W + 4, USB_Y + 26)
        p.setPen(QPen(QColor("#2b2c31"), 4.4, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawPath(wire)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#5a5d66" if hover else "#44464d"))
        p.drawRoundedRect(QRectF(x, USB_Y - 6.5, 15, 13), 2.6, 2.6)
        cx = x + 7.5
        if self.cable == "charger":             # a bolt: power, no data
            bolt = QtGui.QPolygonF([QPointF(cx + 1.6, USB_Y - 4.4),
                                    QPointF(cx - 2.8, USB_Y + 0.9),
                                    QPointF(cx - 0.4, USB_Y + 0.9),
                                    QPointF(cx - 1.6, USB_Y + 4.4),
                                    QPointF(cx + 2.8, USB_Y - 0.9),
                                    QPointF(cx + 0.4, USB_Y - 0.9)])
            p.setBrush(QColor("#f9d949"))
            p.drawPolygon(bolt)
        else:                                   # data: a line between two ends
            mark = QColor("#e8e9ee" if self.cable == "computer" else "#8b8e97")
            p.setPen(QPen(mark, 1.1, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx - 3.6, USB_Y), QPointF(cx + 2.2, USB_Y))
            p.setPen(Qt.NoPen)
            p.setBrush(mark)
            p.drawEllipse(QPointF(cx - 3.8, USB_Y), 1.3, 1.3)
            p.drawPolygon(QtGui.QPolygonF([QPointF(cx + 4.6, USB_Y),
                                           QPointF(cx + 1.8, USB_Y - 1.9),
                                           QPointF(cx + 1.8, USB_Y + 1.9)]))

    def draw_screen(self, p, t, k):
        p.setPen(Qt.NoPen)
        p.setBrush(t["bezel"])
        p.drawRoundedRect(BEZEL, 7, 7)
        p.fillRect(LCD.adjusted(-1.5, -1.5, 1.5, 1.5), QColor("#050505"))
        if self.power != "on" or self.blank or self.frame is None:
            p.fillRect(LCD, QColor(DARK))
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
        light = self.backlight / 255
        bg, fg = mix(UNLIT_BG, LIT_BG, light), mix(UNLIT_FG, LIT_FG, light)
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

    def draw_led(self, p, t):
        led = self.led if self.power == "on" and not self.blank else 0
        p.setPen(Qt.NoPen)
        if led:
            colour = QColor(led)
            level = max(colour.red(), colour.green(), colour.blue()) / 255
            glow = QtGui.QRadialGradient(LED, 11)
            inner = QColor(colour)
            inner.setAlpha(round(170 * level))
            outer = QColor(colour)
            outer.setAlpha(0)
            glow.setColorAt(0, inner)
            glow.setColorAt(1, outer)
            p.setBrush(glow)
            p.drawEllipse(LED, 11, 11)
            p.setBrush(mix("#ffffff", colour, 0.55))
        else:
            p.setBrush(mix(t["body2"], "#000000", 0.25))
        p.drawEllipse(LED, 2.6, 2.6)

    def key_brush(self, t, name):
        if self.pressed(name):
            return t["key_down"]
        return t["key_hover"] if self.hover == name else t["key"]

    def draw_keys(self, p, t):
        p.setPen(Qt.NoPen)
        for name, start in SECTORS.items():
            p.setBrush(self.key_brush(t, name))
            p.drawPath(ring_sector(PAD, PAD_R, PAD_R_IN, start, 90))
        p.setPen(QPen(t["key_edge"], 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(PAD, PAD_R, PAD_R)
        p.drawEllipse(PAD, PAD_R_IN, PAD_R_IN)
        for angle in (45, 135, 225, 315):
            c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            p.drawLine(QPointF(PAD.x() + c * PAD_R_IN, PAD.y() + s * PAD_R_IN),
                       QPointF(PAD.x() + c * PAD_R, PAD.y() + s * PAD_R))
        p.setPen(Qt.NoPen)
        p.setBrush(t["glyph"])
        for name, (dx, dy) in ARROWS.items():
            push = 0.6 if self.pressed(name) else 0
            tip = QPointF(PAD.x() + dx * (30.5 - push), PAD.y() + dy * (30.5 - push))
            base = QPointF(PAD.x() + dx * (24.5 - push), PAD.y() + dy * (24.5 - push))
            p.drawPolygon(QtGui.QPolygonF([
                tip, QPointF(base.x() - dy * 4.8, base.y() + dx * 4.8),
                QPointF(base.x() + dy * 4.8, base.y() - dx * 4.8)]))
        p.setPen(QPen(t["key_edge"], 0.8))
        p.setBrush(self.key_brush(t, "ok"))
        p.drawEllipse(PAD, OK_R, OK_R)
        p.setBrush(self.key_brush(t, "back"))
        p.drawEllipse(BACK, BACK_R, BACK_R)
        x, y = BACK.x(), BACK.y() + (0.5 if self.pressed("back") else 0)
        hook = QPainterPath()                   # an arrow that turns back
        hook.moveTo(x - 3.2, y - 3.2)
        hook.lineTo(x + 1.2, y - 3.2)
        hook.arcTo(QRectF(x - 1.6, y - 3.2, 5.6, 5.6), 90, -180)
        hook.lineTo(x - 4.4, y + 2.4)
        hook.moveTo(x - 2.3, y + 0.2)
        hook.lineTo(x - 4.6, y + 2.4)
        hook.lineTo(x - 2.3, y + 4.6)
        p.setPen(QPen(t["glyph"], 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(hook)

    def draw_marks(self, p, t):
        """Its name under the screen, and the speaker's sign over it"""
        font = QtGui.QFont(self.font())
        font.setPixelSize(7)
        font.setBold(True)
        font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.4)
        p.setFont(font)
        p.setPen(t["text"])
        p.drawText(QRectF(BX + 16, BY + 100, 90, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, "FLIPPEREMU")
        font.setBold(False)
        font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 0.4)
        p.setFont(font)
        name = QtGui.QFontMetricsF(font).elidedText(self.name, Qt.ElideRight, 64)
        p.drawText(QRectF(BX + 82, BY + 100, 80, 14),
                   Qt.AlignRight | Qt.AlignVCenter, name)
        sounding = self.tone and self.power == "on" and not self.muted
        if not (sounding or self.muted):
            return
        cx, cy = BX + 21.0, BY + 7.5
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
