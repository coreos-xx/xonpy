#!/usr/bin/env python3
"""Live X.on viewer and guided recorder: electrode map + scrolling traces.

  python tools/xon_gui.py                      # connect, stream EEG, record
  python tools/xon_gui.py --protocol touch     # start the touch test right away
  python tools/xon_gui.py --gain 1 -l head_g1
  python tools/xon_gui.py --replay recordings/X.csv [--speed 2]   # play back a recording

Takes the same options as xon_record.py. Protocols can also be started from the buttons.
Space continues a step that waits, Enter in the note field adds a note.
The map is a top view, nose up: the left of the picture is the wearer's left.
"""
import asyncio, math, signal, sys, time

import numpy as np
import pyqtgraph as pg
import qasync
from PyQt6 import QtCore, QtGui, QtWidgets

from xonpy import protocol as xp
import xon_record as xr

# top-view positions, head radius 1, nose at y = -1
POS = {"F3": (-0.40, -0.45), "F4": (0.40, -0.45),
       "C3": (-0.52, 0.0), "Cz": (0.0, 0.0), "C4": (0.52, 0.0),
       "P3": (-0.40, 0.45), "P4": (0.40, 0.45)}
# electrodes worth watching during each head task
HINTS = {"blink": ("F3", "F4"), "look_left": ("F3", "F4"), "look_right": ("F3", "F4"),
         "eyes_closed": ("P3", "P4")}
TARGET, NEXT, HINT = QtGui.QColor("#2979ff"), QtGui.QColor("#2979ff"), QtGui.QColor("#ff9100")
GOOD, PART, SAT = QtGui.QColor("#43a047"), QtGui.QColor("#fbc02d"), QtGui.QColor("#9e9e9e")
WINDOW_S = 8
MAINS_HZ = 50


class Biquad:
    """RBJ biquad, vectorised over channels, transposed direct form II."""

    def __init__(self, kind, f0, fs, n, q=0.7071):
        w = 2 * math.pi * f0 / fs
        cw, alpha = math.cos(w), math.sin(w) / (2 * q)
        if kind == "notch":
            b = [1, -2 * cw, 1]
        else:  # highpass
            b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a0 = 1 + alpha
        self.b = [x / a0 for x in b]
        self.a1, self.a2 = -2 * cw / a0, (1 - alpha) / a0
        self.z1, self.z2 = np.zeros(n), np.zeros(n)

    def __call__(self, x):
        b0, b1, b2 = self.b
        y = b0 * x + self.z1
        self.z1 = b1 * x - self.a1 * y + self.z2
        self.z2 = b2 * x - self.a2 * y
        return y


class HeadMap(QtWidgets.QWidget):
    """Top-view electrode map. Fill = signal state, blue ring = touch now, orange = watch."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 340)
        self.quality = {name: None for name in POS}   # fraction of saturated samples
        self.target, self.next, self.hints = None, None, ()
        self.phase = 0.0

    def tick(self):
        self.phase = (self.phase + 0.08) % (2 * math.pi)
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height() - 40
        r = min(w, h) * 0.40
        cx, cy = w / 2, h / 2 + 10
        pal = self.palette()
        fg = pal.color(QtGui.QPalette.ColorRole.WindowText)
        p.setPen(QtGui.QPen(fg, 2))
        # nose, ears, head
        nose = QtGui.QPolygonF([QtCore.QPointF(cx - 0.12 * r, cy - 0.99 * r),
                                QtCore.QPointF(cx, cy - 1.15 * r),
                                QtCore.QPointF(cx + 0.12 * r, cy - 0.99 * r)])
        p.drawPolyline(nose)
        for sx in (-1, 1):
            p.drawEllipse(QtCore.QPointF(cx + sx * 1.04 * r, cy), 0.07 * r, 0.2 * r)
        p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        font = p.font()
        font.setPointSizeF(9)
        p.setFont(font)
        font.setBold(True)
        p.setFont(font)
        for sx, text in ((-1, "L"), (1, "R")):
            p.drawText(QtCore.QRectF(cx + sx * 1.2 * r - 10, cy - 0.45 * r, 20, 20),
                       QtCore.Qt.AlignmentFlag.AlignCenter, text)

        er = 0.16 * r
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        for i, (name, (x, y)) in enumerate(POS.items()):
            c = QtCore.QPointF(cx + x * r, cy + y * r)
            q = self.quality[name]
            fill = SAT if q is None or q > 0.5 else PART if q > 0.05 else GOOD
            p.setPen(QtGui.QPen(fg, 1.5))
            p.setBrush(fill)
            p.drawEllipse(c, er, er)
            if name == self.target:
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.setPen(QtGui.QPen(TARGET, 3 + 4 * pulse))
                p.drawEllipse(c, er * (1.35 + 0.25 * pulse), er * (1.35 + 0.25 * pulse))
            elif name == self.next:
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.setPen(QtGui.QPen(NEXT, 2, QtCore.Qt.PenStyle.DashLine))
                p.drawEllipse(c, er * 1.4, er * 1.4)
            if name in self.hints:
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.setPen(QtGui.QPen(HINT, 3))
                p.drawEllipse(c, er * 1.4, er * 1.4)
            p.setPen(QtGui.QColor("black"))
            font.setPointSizeF(10)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QtCore.QRectF(c.x() - er, c.y() - er, 2 * er, 1.3 * er),
                       QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignBottom, name)
            font.setPointSizeF(7)
            font.setBold(False)
            p.setFont(font)
            p.drawText(QtCore.QRectF(c.x() - er, c.y() + 0.3 * er, 2 * er, 0.7 * er),
                       QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop, f"ch{i + 1}")

        # legend
        p.setPen(fg)
        font.setPointSizeF(8)
        p.setFont(font)
        y0 = self.height() - 34
        x0 = 8
        for color, text in ((GOOD, "clean"), (PART, "partly saturated"), (SAT, "saturated / open")):
            p.setBrush(color)
            p.drawEllipse(QtCore.QPointF(x0 + 6, y0 + 6), 5, 5)
            p.drawText(QtCore.QPointF(x0 + 15, y0 + 10), text)
            x0 += 22 + p.fontMetrics().horizontalAdvance(text)
        p.drawText(QtCore.QPointF(8, y0 + 26), "Top view, nose up. Left of the picture = wearer's left.")


class Traces(pg.GraphicsLayoutWidget):
    """Eight stacked scrolling traces with optional display filters and marker lines."""

    def __init__(self):
        super().__init__()
        self.plots, self.curves = [], []
        for i, name in enumerate(xp.CHANNEL_NAMES):
            pl = self.addPlot(row=i, col=0)
            pl.setLabel("left", f"ch{i + 1} {name}")
            pl.showGrid(x=True, y=False, alpha=0.3)
            pl.setMouseEnabled(x=False, y=True)
            pl.getAxis("left").setWidth(70)
            pl.hideButtons()
            if i < 7:
                pl.getAxis("bottom").setStyle(showValues=False)
            if i:
                pl.setXLink(self.plots[0])
            self.curves.append(pl.plot(pen=pg.mkPen(pg.intColor(i, 9), width=1)))
            self.plots.append(pl)
        self.plots[-1].setLabel("bottom", "time", "s")
        self.markers = []   # (t, [lines])
        self.setup(250)

    def setup(self, fs):
        self.fs = fs
        self.n = int(WINDOW_S * fs)
        self.buf = np.full((self.n, 8), np.nan)
        self.count = 0
        self.last_good = np.zeros(8)
        self.make_filters(True, True)

    def make_filters(self, notch, hp):
        self.filters = ([Biquad("notch", MAINS_HZ, self.fs, 8, q=20)] if notch else []) + \
                       ([Biquad("highpass", 1.0, self.fs, 8)] if hp else [])

    def add(self, ch):
        x = np.asarray(ch, float)
        sat = (x == 32767) | (x == -32768)
        x = np.where(sat, self.last_good, x)
        self.last_good = x
        for f in self.filters:
            x = f(x)
        self.buf[self.count % self.n] = np.where(sat, np.nan, x)
        self.count += 1

    def saturation(self, n):
        """Fraction of saturated samples per channel over the last n samples."""
        n = min(n, self.count)
        if not n:
            return [None] * 8
        idx = np.arange(self.count - n, self.count) % self.n
        return np.isnan(self.buf[idx]).mean(0)

    def marker(self, label):
        t = self.count / self.fs
        lines = []
        for i, pl in enumerate(self.plots):
            ln = pg.InfiniteLine(t, angle=90, pen=pg.mkPen("#2979ff", style=QtCore.Qt.PenStyle.DashLine),
                                 label=label if i == 0 else None,
                                 labelOpts={"position": 0.9, "color": "#2979ff", "anchors": [(0, 0), (0, 0)]})
            pl.addItem(ln)
            lines.append(ln)
        self.markers.append((t, lines))

    def redraw(self):
        if not self.count:
            return
        n = min(self.count, self.n)
        idx = np.arange(self.count - n, self.count) % self.n
        t = np.arange(self.count - n, self.count) / self.fs
        for i, c in enumerate(self.curves):
            c.setData(t, self.buf[idx, i], connect="finite")
        now = self.count / self.fs
        self.plots[0].setXRange(max(0, now - WINDOW_S), max(WINDOW_S, now), padding=0)
        while self.markers and self.markers[0][0] < now - WINDOW_S:
            for pl, ln in zip(self.plots, self.markers.pop(0)[1]):
                pl.removeItem(ln)


class Window(QtWidgets.QMainWindow):
    """Also acts as the Recorder's UI (step/status/packet/message/started/stopped)."""

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.rec = xr.Recorder(args, self)
        self.rec.stop_after_protocol = False
        self.running = False
        self.close_requested = False
        self.step_info = None   # (prompt, secs, start time)

        self.setWindowTitle("X.on recorder")
        self.map = HeadMap()
        self.traces = Traces()
        self.prompt = QtWidgets.QLabel("starting ...")
        self.prompt.setWordWrap(True)
        f = self.prompt.font()
        f.setPointSize(15)
        f.setBold(True)
        self.prompt.setFont(f)
        self.prompt.setMinimumHeight(70)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(10)

        def button(text, slot):
            b = QtWidgets.QPushButton(text)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            return b
        self.btn_touch = button("Touch test", lambda: self.rec.start_protocol("touch"))
        self.btn_head = button("Head tasks", lambda: self.rec.start_protocol("head"))
        self.btn_cont = button("Continue (Space)", lambda: self.rec.proceed(""))
        self.btn_stop = button("Stop", self.rec.stop)
        self.note = QtWidgets.QLineEdit()
        self.note.setPlaceholderText("note, Enter to add")
        self.note.returnPressed.connect(self.add_note)
        self.chk_notch = QtWidgets.QCheckBox(f"{MAINS_HZ} Hz notch")
        self.chk_hp = QtWidgets.QCheckBox("1 Hz highpass")
        for c in (self.chk_notch, self.chk_hp):
            c.setChecked(True)
            c.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            c.toggled.connect(lambda _: self.traces.make_filters(self.chk_notch.isChecked(),
                                                                 self.chk_hp.isChecked()))
        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.map, 3)
        left.addWidget(self.prompt)
        left.addWidget(self.progress)
        row = QtWidgets.QHBoxLayout()
        for b in (self.btn_touch, self.btn_head, self.btn_cont, self.btn_stop):
            row.addWidget(b)
        left.addLayout(row)
        left.addWidget(self.note)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("display:"))
        row.addWidget(self.chk_notch)
        row.addWidget(self.chk_hp)
        row.addStretch()
        left.addLayout(row)
        left.addWidget(self.status_lbl)
        left.addWidget(self.log, 1)
        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(460)
        split = QtWidgets.QSplitter()
        split.addWidget(lw)
        split.addWidget(self.traces)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.resize(1400, 850)
        self.set_controls(False)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(33)

    # --- Recorder UI interface -------------------------------------------
    def started(self, rec):
        self.traces.setup(rec.rate)
        self.running = True
        self.set_controls(True)
        self.prompt.setText("Streaming. Start the touch test or the head tasks.")

    def stopped(self, rec):
        self.running = False
        self.set_controls(False)
        self.map.target = self.map.next = None
        self.map.hints = ()
        self.prompt.setText("Stopped.")
        self.step_info = None

    def step(self, i, steps):
        if i is None:
            self.step_info = None
            self.map.target = self.map.next = None
            self.map.hints = ()
            self.prompt.setText("Protocol finished. Start another one or Stop.")
            return
        label, secs, prompt = steps[i]
        self.traces.marker(label)
        self.map.target = label[6:] if label.startswith("touch_") else None
        self.map.next = None
        if not self.map.target:
            nxt = next((l for l, _, _ in steps[i + 1:] if l.startswith("touch_")), None)
            if nxt and steps[i + 1][0] == nxt:
                self.map.next = nxt[6:]
        self.map.hints = HINTS.get(label, ())
        self.prompt.setText(prompt.replace("Press ENTER", "Press SPACE"))
        self.step_info = (secs, time.monotonic())
        QtWidgets.QApplication.beep()

    def status(self, line):
        self.status_lbl.setText(line.replace(" | ", "\n"))

    def packet(self, pkt):
        for s in pkt.samples:
            self.traces.add(s.ch)

    def message(self, text):
        self.log.appendPlainText(text)

    # --- GUI ---------------------------------------------------------------
    def set_controls(self, on):
        for w in (self.btn_touch, self.btn_head, self.btn_cont, self.btn_stop, self.note):
            w.setEnabled(on)

    def add_note(self):
        if self.note.text().strip():
            self.rec.note(self.note.text().strip())
        self.note.clear()
        self.note.clearFocus()

    def keyPressEvent(self, e):
        if e.key() in (QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.rec.proceed("")
        else:
            super().keyPressEvent(e)

    def refresh(self):
        self.traces.redraw()
        q = self.traces.saturation(self.traces.fs // 2)
        for i, name in enumerate(POS):
            self.map.quality[name] = q[i]
        self.map.tick()
        if self.step_info:
            secs, t0 = self.step_info
            if secs:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(1000 * min(1, (time.monotonic() - t0) / secs)))
            else:
                self.progress.setRange(0, 0)   # busy: waiting for Space
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)

    def closeEvent(self, e):
        if self.running:
            self.close_requested = True
            self.rec.stop()
            e.ignore()
        else:
            e.accept()

    async def replay(self, fn, speed):
        """Feed a recording through the same UI path, paced like the original."""
        self.rec.stop_after_protocol = True
        for b in (self.btn_touch, self.btn_head, self.btn_cont):
            b.hide()
        self.message(f"replaying {fn} at {speed}x")
        rows = list(xp.read_rows(fn))
        self.rec.rate = 250
        for _, kind, data in rows:
            if kind == "event" and data.startswith("read e2951401"):
                self.rec.rate = int.from_bytes(bytes.fromhex(data.split()[2]), "little", signed=True)
        self.rec.done = asyncio.Event()
        self.started(self.rec)
        steps = []
        t_start, w_start = rows[0][0], time.monotonic()
        for t, kind, data in rows:
            if self.rec.done.is_set():
                break
            delay = (t - t_start) / speed - (time.monotonic() - w_start)
            if delay > 0:
                await asyncio.sleep(delay)
            if kind == "notify":
                pkt = self.rec.stats.feed(bytes.fromhex(data))
                if pkt:
                    self.packet(pkt)
            elif kind == "marker":
                steps.append((data, None, data))
                self.step(len(steps) - 1, steps)
                self.step_info = None
            elif kind in ("note", "event"):
                self.message(f"{kind}: {data}")
            if time.monotonic() - getattr(self, "_last_stat", 0) > 1:
                self._last_stat = time.monotonic()
                self.status(self.rec.stats.line(speed))
        self.stopped(self.rec)
        self.prompt.setText("Replay finished.")

    async def session(self):
        try:
            if self.args.replay:
                await self.replay(self.args.replay, self.args.speed)
            else:
                await self.rec.run()
        except Exception as ex:
            self.message(f"error: {ex}")
            self.prompt.setText(str(ex))
        if self.close_requested:
            self.close()


def main():
    ap = xr.make_parser(__doc__)
    ap.add_argument("--replay", help="play back a recording CSV instead of connecting")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = Window(args)
    win.show()
    signal.signal(signal.SIGINT, lambda *_: win.close())
    closed = asyncio.Event()
    app.lastWindowClosed.connect(closed.set)
    with loop:
        loop.create_task(win.session())
        loop.run_until_complete(closed.wait())


if __name__ == "__main__":
    main()
