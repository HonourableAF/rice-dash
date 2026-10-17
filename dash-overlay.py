#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["PyQt6>=6.5", "psutil>=5.9"]
# ///
"""rice-dash overlay: a fullscreen, animated Nord dashboard (PyQt6 + QML).

    uv run dash-overlay.py

Run it again while it is open to close it (so one keyboard shortcut toggles it).
Close with: Esc, q, or a click.  Optional: playerctl for the now-playing card.
"""
from __future__ import annotations

import getpass
import platform
import socket
import sys
import threading
import time
from collections import deque

import psutil
from PyQt6.QtCore import QByteArray, QObject, QProcess, QTimer, QUrl, pyqtProperty, pyqtSignal, qInstallMessageHandler
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtQml import QQmlApplicationEngine

SERVER_NAME = "rice-dash-overlay"
IS_WIN = sys.platform == "win32"
SMOKE = "--smoke" in sys.argv
SKIP_NICS = ("docker", "veth", "br-", "virbr", "loopback", "vmware", "virtualbox", "isatap", "teredo")
QML_ERRORS = ("TypeError", "ReferenceError", "is not a type", "Cannot assign", "Unable to assign", "Cannot read property", "is not defined")
BAD_MESSAGES: list[str] = []
MEDIA_FMT = "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}\t{{position}}\t{{mpris:length}}"
MEDIA_OFF = {"on": False, "playing": False, "title": "", "sub": "", "frac": 0.0, "time": "", "note": "nothing playing"}


def human(n: float) -> str:
    if n < 1024:
        return f"{n:.0f}B"
    for unit in "KMGT":
        n /= 1024
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}"
    return ""


def mmss(us: int) -> str:
    s = int(us // 1_000_000)
    return f"{s // 60}:{s % 60:02d}"


def norm(hist: deque, floor: float) -> list[float]:
    hi = max(max(hist, default=0.0), floor)
    return [v / hi for v in hist]


def cpu_model() -> str:
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            return platform.processor()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def message_handler(_mode, _ctx, msg: str) -> None:
    print(msg, file=sys.stderr)
    if any(k in msg for k in QML_ERRORS):
        BAD_MESSAGES.append(msg)


class Stats(QObject):
    """Collects system stats once a second and hands them to QML as one map."""

    dataChanged = pyqtSignal()
    toggleRequested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._d: dict = {}
        self.n = 0
        self.cpu_hist: deque[float] = deque([0.0] * 61, maxlen=61)
        self.down_hist: deque[float] = deque([0.0] * 61, maxlen=61)
        self.up_hist: deque[float] = deque([0.0] * 61, maxlen=61)
        self.procs: list[dict] = []
        self.disks: list[dict] = []
        self.media = dict(MEDIA_OFF)
        self.no_playerctl = False
        self.win_media = dict(MEDIA_OFF)
        if IS_WIN:
            threading.Thread(target=self.win_media_loop, daemon=True).start()
        self.freq_s = self.temp_s = "—"

        self.model = cpu_model()
        try:
            self.who = f"{getpass.getuser()}@{socket.gethostname()}"
        except Exception:
            self.who = socket.gethostname()

        self.net_last = (time.monotonic(), *self.net_counters())
        psutil.cpu_percent(percpu=True)

        self.proc = QProcess(self)
        self.proc.finished.connect(self.media_done)
        self.proc.errorOccurred.connect(self.media_error)

        self.tick()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        QTimer.singleShot(350, self.tick)

    @pyqtProperty("QVariantMap", notify=dataChanged)
    def data(self) -> dict:
        return self._d

    @staticmethod
    def net_counters() -> tuple[int, int]:
        rx = tx = 0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if name == "lo" or name.lower().startswith(SKIP_NICS):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        return rx, tx

    @staticmethod
    def temp() -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for key in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
            if temps.get(key):
                return temps[key][0].current
        return None

    @staticmethod
    def top_procs(n: int = 4) -> list[dict]:
        agg: dict[str, list[float]] = {}
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            i = p.info
            if i.get("pid") == 0:
                continue
            a = agg.setdefault(i.get("name") or "?", [0.0, 0.0, 0])
            a[0] += i.get("cpu_percent") or 0.0
            a[1] += i.get("memory_percent") or 0.0
            a[2] += 1
        top = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)[:n]
        return [
            {
                "name": f"{name} ×{int(c)}" if c > 1 else name,
                "cpu": f"{cpu:.0f}%",
                "mem": f"{mem:.1f}%",
                "frac": min(cpu, 100.0) / 100,
            }
            for name, (cpu, mem, c) in top
        ]

    @staticmethod
    def read_disks(limit: int = 2) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        pseudo = {"squashfs", "tmpfs", "devtmpfs", "overlay", "efivarfs", "vfat"}
        for p in psutil.disk_partitions():
            mp = p.mountpoint
            if p.device in seen or p.fstype in pseudo or not p.fstype:
                continue
            if mp.startswith(("/snap", "/boot", "/var/lib/docker", "/var/lib/containers")):
                continue
            if mp.startswith("/run") and not mp.startswith("/run/media"):
                continue
            try:
                u = psutil.disk_usage(mp)
            except OSError:
                continue
            seen.add(p.device)
            out.append(
                {
                    "name": mp if len(mp) <= 14 else "…" + mp[-13:],
                    "frac": u.percent / 100,
                    "text": f"{human(u.used)} / {human(u.total)}",
                }
            )
            if len(out) == limit:
                break
        return out

    def win_media_loop(self) -> None:
        try:
            import asyncio

            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
        except ImportError:
            self.win_media = {**MEDIA_OFF, "note": "media: see README"}
            return

        async def read() -> dict:
            mgr = await Manager.request_async()
            ses = mgr.get_current_session()
            if ses is None:
                return dict(MEDIA_OFF)
            props = await ses.try_get_media_properties_async()
            info = ses.get_playback_info()
            line = ses.get_timeline_properties()
            end = line.end_time.total_seconds() - line.start_time.total_seconds()
            pos = line.position.total_seconds() - line.start_time.total_seconds()
            frac = pos / end if end > 0 else 0.0
            tm = f"{mmss(int(pos * 1_000_000))} / {mmss(int(end * 1_000_000))}" if end > 0 else ""
            player = (ses.source_app_user_model_id or "").removesuffix(".exe")
            return {
                "on": True,
                "playing": int(info.playback_status) == 4,
                "title": props.title or "Unknown",
                "sub": f"{props.artist or '—'}  ·  {player}",
                "frac": max(0.0, min(1.0, frac)),
                "time": tm,
                "note": "",
            }

        while True:
            try:
                self.win_media = asyncio.run(read())
            except Exception:
                self.win_media = dict(MEDIA_OFF)
            time.sleep(2)

    def poll_media(self) -> None:
        if self.no_playerctl or self.proc.state() != QProcess.ProcessState.NotRunning:
            return
        self.proc.start("playerctl", ["metadata", "--format", MEDIA_FMT])

    def media_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self.no_playerctl = True
            self.media = {**MEDIA_OFF, "note": "install playerctl"}

    def media_done(self, code: int, _status) -> None:
        out = bytes(self.proc.readAllStandardOutput().data()).decode(errors="replace").strip()
        if code != 0 or not out:
            self.media = dict(MEDIA_OFF)
            return
        status, player, artist, title, pos, length = (out.split("\t") + [""] * 6)[:6]
        frac, tm = 0.0, ""
        if pos.isdigit() and length.isdigit() and int(length) > 0:
            frac = int(pos) / int(length)
            tm = f"{mmss(int(pos))} / {mmss(int(length))}"
        self.media = {
            "on": True,
            "playing": status == "Playing",
            "title": title or "Unknown",
            "sub": f"{artist or '—'}  ·  {player}",
            "frac": frac,
            "time": tm,
            "note": "",
        }

    def tick(self) -> None:
        self.n += 1
        now = time.localtime()

        per = psutil.cpu_percent(percpu=True)
        cpu = sum(per) / max(len(per), 1)
        self.cpu_hist.append(cpu / 100)
        if self.n % 3 == 1:
            try:
                freq = psutil.cpu_freq()
                self.freq_s = f"{freq.current / 1000:.2f} GHz" if freq else "—"
            except (OSError, NotImplementedError):
                self.freq_s = "—"
            t = self.temp()
            self.temp_s = f"{t:.0f}°C" if t is not None else "—"
        freq_s, temp_s = self.freq_s, self.temp_s

        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        used = vm.total - vm.available

        t_now = time.monotonic()
        rx, tx = self.net_counters()
        t0, rx0, tx0 = self.net_last
        dt = max(t_now - t0, 1e-3)
        down, up = max(rx - rx0, 0) / dt, max(tx - tx0, 0) / dt
        self.net_last = (t_now, rx, tx)
        self.down_hist.append(down)
        self.up_hist.append(up)

        if self.n % 3 == 1 or not self.procs:
            self.procs = self.top_procs()
        if self.n % 10 == 1 or not self.disks:
            self.disks = self.read_disks()
        if IS_WIN:
            self.media = self.win_media
        elif self.n % 2 == 0:
            self.poll_media()

        secs = int(time.time() - psutil.boot_time())
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        uptime = (f"{d}d " if d else "") + f"{h}h {rem // 60:02d}m"

        self._d = {
            "clockH": time.strftime("%H", now),
            "clockM": time.strftime("%M", now),
            "clockS": time.strftime("%S", now),
            "day": time.strftime("%A", now),
            "date": time.strftime("%d %B %Y", now),
            "who": self.who,
            "uptime": uptime,
            "cpu": cpu / 100,
            "cpuText": f"{cpu:.0f}%",
            "cpuHist": list(self.cpu_hist),
            "cores": [p / 100 for p in per],
            "model": self.model,
            "freq": freq_s,
            "temp": temp_s,
            "load": f"load {psutil.getloadavg()[0]:.2f}",
            "ram": used / vm.total if vm.total else 0.0,
            "ramPct": f"{used / vm.total * 100:.0f}%" if vm.total else "—",
            "ramText": f"{human(used)} / {human(vm.total)}",
            "swap": sw.used / sw.total if sw.total else 0.0,
            "swapText": f"{human(sw.used)} / {human(sw.total)}",
            "disks": self.disks,
            "down": f"{human(down)}/s",
            "up": f"{human(up)}/s",
            "downTotal": f"{human(rx)} since boot",
            "upTotal": f"{human(tx)} since boot",
            "downHist": norm(self.down_hist, 64 * 1024),
            "upHist": norm(self.up_hist, 64 * 1024),
            "procs": self.procs,
            "media": self.media,
        }
        self.dataChanged.emit()


QML = r'''
import QtQuick
import QtQuick.Window

Window {
    id: win
    visible: true
    visibility: Window.FullScreen
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    title: "rice-dash"

    property var d: stats.data
    property bool open: false
    property bool closing: false
    property real energy: 0
    property real t: Date.now()

    function heat(f) { return f < 0.5 ? "#a3be8c" : f < 0.75 ? "#ebcb8b" : f < 0.9 ? "#d08770" : "#bf616a" }
    function dismiss() { if (closing) return; closing = true; open = false; quitTimer.start() }

    Timer { id: quitTimer; interval: 650; onTriggered: Qt.quit() }
    Timer { interval: 90; running: true; onTriggered: win.open = true }
    Timer {
        interval: 66; running: win.open; repeat: true
        onTriggered: { win.energy += (Math.min(1, win.d.cpu || 0) - win.energy) * 0.1; win.t = Date.now() }
    }
    Connections { target: stats; function onToggleRequested() { win.dismiss() } }

    component HBar: Item {
        id: hb
        property real value: 0
        property real shown: value
        property color col: "#88c0d0"
        property real u: 1
        height: 8 * u
        Behavior on shown { enabled: Math.abs(hb.value - hb.shown) > 0.05; NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
        Rectangle { anchors.fill: parent; radius: height / 2; color: "#434c5e" }
        Rectangle {
            height: parent.height; radius: height / 2; color: hb.col
            width: Math.max(height, parent.width * Math.max(0, Math.min(1, hb.shown)))
        }
    }

    component BarRow: Item {
        id: br
        property string name: ""
        property string info: ""
        property real value: 0
        property color col: "#88c0d0"
        property real u: 1
        height: 38 * u
        Text { text: br.name; color: "#d8dee9"; font.pixelSize: 14 * br.u; font.family: "JetBrains Mono" }
        Text { text: br.info; color: "#7b88a1"; font.pixelSize: 13 * br.u; font.family: "JetBrains Mono"; anchors.right: parent.right }
        HBar { anchors.bottom: parent.bottom; width: parent.width; value: br.value; col: br.col; u: br.u }
    }

    component Gauge: Item {
        id: g
        property real value: 0
        property real shown: value
        property real u: 1
        property string big: ""
        property string label: ""
        Behavior on shown { enabled: Math.abs(g.value - g.shown) > 0.03; NumberAnimation { duration: 500; easing.type: Easing.OutCubic } }
        onShownChanged: cv.requestPaint()
        onWidthChanged: cv.requestPaint()
        function heat(f) { return f < 0.5 ? "#a3be8c" : f < 0.75 ? "#ebcb8b" : f < 0.9 ? "#d08770" : "#bf616a" }
        Canvas {
            id: cv
            anchors.fill: parent
            renderTarget: Canvas.FramebufferObject
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var cx = width / 2, cy = height / 2
                var lw = 13 * g.u, r = Math.min(width, height) / 2 - lw
                var a0 = Math.PI * 0.75, span = Math.PI * 1.5
                ctx.lineWidth = lw
                ctx.lineCap = "round"
                ctx.strokeStyle = "#434c5e"
                ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span); ctx.stroke()
                var v = Math.max(0, Math.min(1, g.shown))
                if (v > 0.004) {
                    var grad = ctx.createLinearGradient(0, height, width, 0)
                    grad.addColorStop(0, "#5e81ac")
                    grad.addColorStop(1, g.heat(v))
                    ctx.strokeStyle = grad
                    ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span * v); ctx.stroke()
                }
            }
        }
        Column {
            anchors.centerIn: parent
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: g.big; color: "#eceff4"; font.family: "JetBrains Mono"
                font.pixelSize: Math.min(g.width, g.height) * 0.26; font.weight: Font.Light
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: g.label; color: "#7b88a1"; font.family: "JetBrains Mono"
                font.pixelSize: 12 * g.u; font.letterSpacing: 3
            }
        }
    }

    component AreaGraph: Item {
        id: ag
        property var samples: []
        property color col: "#88c0d0"
        property int span: 60
        property real step: width / (span - 1)
        property real clock: 0
        property real born: 0
        property real off: step * (1 - Math.min(1, Math.max(0, (clock - born) / 950)))
        clip: true
        onSamplesChanged: born = Date.now()

        Canvas {
            id: cv
            x: -ag.step + ag.off
            width: ag.width + ag.step
            height: ag.height
            renderTarget: Canvas.FramebufferObject
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            Connections { target: ag; function onSamplesChanged() { cv.requestPaint() } }

            function curve(ctx, pts) {
                for (var i = 1; i < pts.length; i++) {
                    var mx = (pts[i - 1][0] + pts[i][0]) / 2, my = (pts[i - 1][1] + pts[i][1]) / 2
                    ctx.quadraticCurveTo(pts[i - 1][0], pts[i - 1][1], mx, my)
                }
                ctx.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1])
            }

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var samples = ag.samples
                var n = samples ? samples.length : 0
                if (n < 2) return
                var pts = []
                for (var i = 0; i < n; i++)
                    pts.push([width - (n - 1 - i) * ag.step, height - 3 - samples[i] * (height - 8)])
                var c = ag.col
                var grad = ctx.createLinearGradient(0, 0, 0, height)
                grad.addColorStop(0, Qt.rgba(c.r, c.g, c.b, 0.55))
                grad.addColorStop(1, Qt.rgba(c.r, c.g, c.b, 0.0))
                ctx.beginPath(); ctx.moveTo(pts[0][0], height); ctx.lineTo(pts[0][0], pts[0][1])
                curve(ctx, pts)
                ctx.lineTo(pts[n - 1][0], height); ctx.closePath()
                ctx.fillStyle = grad; ctx.fill()
                ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1])
                curve(ctx, pts)
                ctx.lineWidth = 2; ctx.strokeStyle = c; ctx.stroke()
            }
        }
    }

    component Card: Rectangle {
        id: card
        property string title: ""
        property int order: 0
        property real u: 1
        property bool open: false
        property bool shown: false
        default property alias content: body.data
        radius: 20 * u
        color: Qt.rgba(0.231, 0.259, 0.322, 0.66)
        border.width: 1
        border.color: hov.containsMouse ? "#88c0d0" : "#4c566a"
        Behavior on border.color { ColorAnimation { duration: 200 } }
        opacity: shown ? 1 : 0
        scale: shown ? 1 : 0.88
        Behavior on opacity { NumberAnimation { duration: 450 } }
        Behavior on scale { NumberAnimation { duration: 750; easing.type: Easing.OutBack; easing.overshoot: 1.6 } }
        transform: Translate {
            y: card.shown ? 0 : 70 * card.u
            Behavior on y { NumberAnimation { duration: 750; easing.type: Easing.OutCubic } }
        }
        onOpenChanged: { if (open) delay.start(); else shown = false }
        Timer { id: delay; interval: 260 + card.order * 95; onTriggered: card.shown = true }
        MouseArea { id: hov; anchors.fill: parent; hoverEnabled: true }
        Text {
            text: card.title; x: 20 * card.u; y: 16 * card.u; color: "#88c0d0"
            font.family: "JetBrains Mono"; font.pixelSize: 14 * card.u; font.bold: true; font.letterSpacing: 3
        }
        Item { id: body; anchors.fill: parent; anchors.margins: 20 * card.u; anchors.topMargin: 52 * card.u }
    }

    Item {
        id: root
        anchors.fill: parent
        focus: true
        Keys.onEscapePressed: win.dismiss()
        Keys.onPressed: function(e) { if (e.key === Qt.Key_Q) win.dismiss() }

        Rectangle {
            anchors.fill: parent
            opacity: win.open ? 0.9 : 0
            Behavior on opacity { NumberAnimation { duration: 550; easing.type: Easing.OutCubic } }
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#2e3440" }
                GradientStop { position: 1.0; color: "#232831" }
            }
        }

        Canvas {
            id: ambient
            anchors.fill: parent
            opacity: win.open ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 1200 } }
            onWidthChanged: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var blobs = [[0.18, 0.2, 0.5, "136,192,208", 0.16], [0.85, 0.85, 0.55, "180,142,173", 0.12], [0.6, 0.1, 0.4, "94,129,172", 0.14]]
                for (var i = 0; i < blobs.length; i++) {
                    var b = blobs[i], r = Math.max(width, height) * b[2]
                    var g = ctx.createRadialGradient(width * b[0], height * b[1], 0, width * b[0], height * b[1], r)
                    g.addColorStop(0, "rgba(" + b[3] + "," + b[4] + ")")
                    g.addColorStop(1, "rgba(" + b[3] + ",0)")
                    ctx.fillStyle = g
                    ctx.fillRect(0, 0, width, height)
                }
            }
        }

        MouseArea { anchors.fill: parent; onClicked: win.dismiss() }

        Rectangle {
            id: sweep
            width: parent.width; height: 2; color: "#88c0d0"; opacity: 0
            Rectangle {
                anchors.bottom: parent.top; width: parent.width; height: 160
                gradient: Gradient {
                    GradientStop { position: 0.0; color: "#0088c0d0" }
                    GradientStop { position: 1.0; color: "#2688c0d0" }
                }
            }
        }
        ParallelAnimation {
            id: sweepAnim
            NumberAnimation { target: sweep; property: "y"; from: 0; to: root.height; duration: 800; easing.type: Easing.InOutQuad }
            SequentialAnimation {
                NumberAnimation { target: sweep; property: "opacity"; from: 0; to: 0.9; duration: 140 }
                PauseAnimation { duration: 520 }
                NumberAnimation { target: sweep; property: "opacity"; to: 0; duration: 140 }
            }
        }
        Connections { target: win; function onOpenChanged() { if (win.open) sweepAnim.restart() } }

        Canvas {
            id: wave; renderTarget: Canvas.FramebufferObject
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            height: root.height * 0.22
            property real phase: 0
            opacity: win.open ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 1100 } }
            Connections {
                target: win
                function onTChanged() { wave.phase += 0.07 * (1 + 2 * win.energy); wave.requestPaint() }
            }
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var layers = [
                    { f: 0.0105, s: 1.00, k: 1.00, off: 0.0, rgb: "94,129,172",  a: 0.50 },
                    { f: 0.0170, s: 1.50, k: 0.65, off: 3.1, rgb: "136,192,208", a: 0.36 }
                ]
                var amp = height * (0.10 + 0.25 * win.energy)
                for (var li = 0; li < layers.length; li++) {
                    var L = layers[li]
                    var grad = ctx.createLinearGradient(0, 0, 0, height)
                    grad.addColorStop(0, "rgba(" + L.rgb + "," + L.a + ")")
                    grad.addColorStop(1, "rgba(" + L.rgb + ",0)")
                    ctx.beginPath()
                    ctx.moveTo(0, height)
                    for (var x = 0; x <= width; x += 10) {
                        var y = height * 0.5
                            + Math.sin(x * L.f + phase * L.s + L.off) * amp * L.k
                            + Math.sin(x * L.f * 2.3 - phase * L.s * 1.4) * amp * L.k * 0.35
                        ctx.lineTo(x, y)
                    }
                    ctx.lineTo(width, height)
                    ctx.closePath()
                    ctx.fillStyle = grad
                    ctx.fill()
                }
            }
        }

        Item {
            id: stage
            anchors.centerIn: parent
            width: Math.min(parent.width * 0.9, 1900)
            height: Math.min(parent.height * 0.88, 1050)
            readonly property real u: Math.max(0.6, Math.min(width / 1600, height / 900))
            readonly property real gap: 18 * u

            Item {
                id: header
                width: parent.width
                height: parent.height * 0.24
                property bool shown: false
                opacity: shown ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 700 } }
                transform: Translate {
                    y: header.shown ? 0 : -50 * stage.u
                    Behavior on y { NumberAnimation { duration: 900; easing.type: Easing.OutExpo } }
                }
                Connections {
                    target: win
                    function onOpenChanged() { if (win.open) hdrDelay.start(); else header.shown = false }
                }
                Timer { id: hdrDelay; interval: 160; onTriggered: header.shown = true }

                Row {
                    id: clockRow
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    Text { text: win.d.clockH; color: "#eceff4"; font.family: "JetBrains Mono"; font.pixelSize: 150 * stage.u; font.weight: Font.Light }
                    Text {
                        id: colon; text: ":"; color: "#88c0d0"; font.family: "JetBrains Mono"; font.pixelSize: 150 * stage.u; font.weight: Font.Light
                        Behavior on opacity { NumberAnimation { duration: 220 } }
                        Timer { interval: 650; running: win.open; repeat: true; onTriggered: colon.opacity = colon.opacity > 0.5 ? 0.15 : 1.0 }
                    }
                    Text { text: win.d.clockM; color: "#eceff4"; font.family: "JetBrains Mono"; font.pixelSize: 150 * stage.u; font.weight: Font.Light }
                    Text {
                        text: win.d.clockS; color: "#88c0d0"; font.family: "JetBrains Mono"; font.pixelSize: 54 * stage.u
                        leftPadding: 16 * stage.u
                        y: clockRow.height - height - 34 * stage.u
                    }
                }

                Column {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6 * stage.u
                    Text { anchors.right: parent.right; text: win.d.day; color: "#eceff4"; font.family: "JetBrains Mono"; font.pixelSize: 40 * stage.u; font.weight: Font.DemiBold }
                    Text { anchors.right: parent.right; text: win.d.date; color: "#d8dee9"; font.family: "JetBrains Mono"; font.pixelSize: 24 * stage.u }
                    Text { anchors.right: parent.right; text: win.d.who; color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 18 * stage.u }
                    Text { anchors.right: parent.right; text: "up " + win.d.uptime; color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 18 * stage.u }
                }
            }

            Item {
                id: cards
                y: header.height + stage.gap * 0.5
                width: parent.width
                height: parent.height * 0.5
                readonly property real cw: (width - 3 * stage.gap) / 4

                Card {
                    order: 0; title: "CPU"; u: stage.u; open: win.open
                    x: 0; width: cards.cw; height: cards.height
                    Gauge {
                        id: cpuGauge
                        u: stage.u; value: win.d.cpu; big: win.d.cpuText; label: "LOAD"
                        width: Math.min(parent.width * 0.5, parent.height * 0.46); height: width
                    }
                    Column {
                        anchors.left: cpuGauge.right; anchors.leftMargin: 16 * stage.u
                        anchors.right: parent.right
                        anchors.verticalCenter: cpuGauge.verticalCenter
                        spacing: 6 * stage.u
                        Text { width: parent.width; text: win.d.model; elide: Text.ElideRight; color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 12 * stage.u }
                        Text { text: win.d.freq; color: "#eceff4"; font.family: "JetBrains Mono"; font.pixelSize: 22 * stage.u }
                        Text { text: win.d.temp; color: "#ebcb8b"; font.family: "JetBrains Mono"; font.pixelSize: 22 * stage.u }
                        Text { text: win.d.load; color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 15 * stage.u }
                    }
                    AreaGraph {
                        anchors.left: parent.left; anchors.right: parent.right
                        y: cpuGauge.height + 14 * stage.u
                        height: parent.height * 0.2
                        clock: win.t; samples: win.d.cpuHist; col: "#88c0d0"
                    }
                    Row {
                        id: coreRow
                        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                        height: parent.height * 0.13
                        spacing: 3 * stage.u
                        Repeater {
                            model: win.d.cores.length
                            delegate: Item {
                                width: (coreRow.width - coreRow.spacing * (win.d.cores.length - 1)) / win.d.cores.length
                                height: coreRow.height
                                Rectangle {
                                    id: coreBar
                                    property real cur: 0
                                    anchors.bottom: parent.bottom; width: parent.width; radius: 3 * stage.u
                                    height: Math.max(3 * stage.u, cur * parent.height)
                                    color: win.heat(win.d.cores[index])
                                    Connections { target: win; function onTChanged() { coreBar.cur += (win.d.cores[index] - coreBar.cur) * 0.5 } }
                                }
                            }
                        }
                    }
                }

                Card {
                    order: 1; title: "MEMORY  ·  DISKS"; u: stage.u; open: win.open
                    x: cards.cw + stage.gap; width: cards.cw; height: cards.height
                    Gauge {
                        id: ramGauge
                        u: stage.u; value: win.d.ram; big: win.d.ramPct; label: "RAM"
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Math.min(parent.width * 0.5, parent.height * 0.42); height: width
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: ramGauge.height + 4 * stage.u
                        text: win.d.ramText; color: "#d8dee9"; font.family: "JetBrains Mono"; font.pixelSize: 16 * stage.u
                    }
                    Column {
                        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                        spacing: 10 * stage.u
                        BarRow { width: parent.width; u: stage.u; name: "swap"; info: win.d.swapText; value: win.d.swap; col: "#b48ead" }
                        Repeater {
                            model: win.d.disks.length
                            delegate: BarRow {
                                width: parent.width; u: stage.u
                                name: win.d.disks[index].name; info: win.d.disks[index].text; value: win.d.disks[index].frac
                                col: win.heat(win.d.disks[index].frac)
                            }
                        }
                    }
                }

                Card {
                    order: 2; title: "NETWORK"; u: stage.u; open: win.open
                    x: 2 * (cards.cw + stage.gap); width: cards.cw; height: cards.height
                    Item {
                        id: dl
                        width: parent.width; height: parent.height / 2 - 6 * stage.u
                        Row {
                            id: dlHead
                            spacing: 10 * stage.u
                            Text { text: "↓"; color: "#88c0d0"; font.pixelSize: 34 * stage.u; font.family: "JetBrains Mono" }
                            Text { text: win.d.down; color: "#eceff4"; font.pixelSize: 34 * stage.u; font.weight: Font.DemiBold; font.family: "JetBrains Mono" }
                        }
                        Text { anchors.right: parent.right; anchors.verticalCenter: dlHead.verticalCenter; text: win.d.downTotal; color: "#7b88a1"; font.pixelSize: 12 * stage.u; font.family: "JetBrains Mono" }
                        AreaGraph {
                            anchors.top: dlHead.bottom; anchors.topMargin: 6 * stage.u
                            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                            clock: win.t; samples: win.d.downHist; col: "#88c0d0"
                        }
                    }
                    Item {
                        width: parent.width; height: parent.height / 2 - 6 * stage.u
                        y: parent.height / 2 + 6 * stage.u
                        Row {
                            id: ulHead
                            spacing: 10 * stage.u
                            Text { text: "↑"; color: "#b48ead"; font.pixelSize: 34 * stage.u; font.family: "JetBrains Mono" }
                            Text { text: win.d.up; color: "#eceff4"; font.pixelSize: 34 * stage.u; font.weight: Font.DemiBold; font.family: "JetBrains Mono" }
                        }
                        Text { anchors.right: parent.right; anchors.verticalCenter: ulHead.verticalCenter; text: win.d.upTotal; color: "#7b88a1"; font.pixelSize: 12 * stage.u; font.family: "JetBrains Mono" }
                        AreaGraph {
                            anchors.top: ulHead.bottom; anchors.topMargin: 6 * stage.u
                            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                            clock: win.t; samples: win.d.upHist; col: "#b48ead"
                        }
                    }
                }

                Card {
                    id: mediaCard
                    order: 3; title: "NOW PLAYING"; u: stage.u; open: win.open
                    x: 3 * (cards.cw + stage.gap); width: cards.cw; height: cards.height * 0.46
                    Row {
                        id: eq
                        anchors.right: parent.right; anchors.top: parent.top
                        height: 28 * stage.u; spacing: 4 * stage.u
                        Repeater {
                            model: 6
                            delegate: Rectangle {
                                width: 5 * stage.u; radius: 2 * stage.u; color: "#88c0d0"
                                property real lvl: 0.2
                                height: Math.max(4 * stage.u, (win.d.media.playing ? lvl : 0.18) * eq.height)
                                y: eq.height - height
                                Timer {
                                    interval: 150 + index * 25; repeat: true; running: !!win.d.media.playing
                                    onTriggered: parent.lvl = 0.25 + Math.random() * 0.75
                                }
                            }
                        }
                    }
                    Column {
                        anchors.left: parent.left; anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        spacing: 7 * stage.u
                        Text {
                            width: parent.width; elide: Text.ElideRight
                            text: win.d.media.on ? win.d.media.title : win.d.media.note
                            color: win.d.media.on ? "#eceff4" : "#7b88a1"
                            font.family: "JetBrains Mono"; font.pixelSize: 24 * stage.u; font.weight: Font.DemiBold
                        }
                        Text { width: parent.width; elide: Text.ElideRight; visible: !!win.d.media.on; text: win.d.media.sub; color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 14 * stage.u }
                        HBar { width: parent.width; visible: !!win.d.media.on; value: win.d.media.frac; u: stage.u }
                        Text { visible: !!win.d.media.on; text: win.d.media.time; color: "#d8dee9"; font.family: "JetBrains Mono"; font.pixelSize: 13 * stage.u }
                    }
                }

                Card {
                    order: 4; title: "TOP PROCESSES"; u: stage.u; open: win.open
                    x: 3 * (cards.cw + stage.gap); y: cards.height * 0.46 + stage.gap
                    width: cards.cw; height: cards.height - cards.height * 0.46 - stage.gap
                    Column {
                        width: parent.width
                        spacing: 7 * stage.u
                        Repeater {
                            model: win.d.procs.length
                            delegate: Item {
                                width: parent.width; height: 24 * stage.u
                                Text {
                                    width: parent.width * 0.58; elide: Text.ElideRight; text: win.d.procs[index].name
                                    color: "#d8dee9"; font.family: "JetBrains Mono"; font.pixelSize: 15 * stage.u
                                }
                                Text {
                                    anchors.right: memT.left; anchors.rightMargin: 16 * stage.u
                                    text: win.d.procs[index].cpu; color: win.heat(win.d.procs[index].frac)
                                    font.family: "JetBrains Mono"; font.pixelSize: 15 * stage.u; font.bold: true
                                }
                                Text {
                                    id: memT; anchors.right: parent.right; text: win.d.procs[index].mem; color: "#7b88a1"
                                    font.family: "JetBrains Mono"; font.pixelSize: 15 * stage.u
                                }
                            }
                        }
                    }
                }
            }

        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom; anchors.bottomMargin: 14
            text: "esc  ·  q  ·  click to close"
            color: "#7b88a1"; font.family: "JetBrains Mono"; font.pixelSize: 12; font.letterSpacing: 2
            opacity: win.open ? 0.6 : 0
            Behavior on opacity { NumberAnimation { duration: 900 } }
        }
    }
}
'''


def finish_smoke(app: QGuiApplication, stats: Stats) -> None:
    d = stats._d
    ok = "cpu" in d and not BAD_MESSAGES
    keys = ("cpuText", "ramPct", "down", "model", "uptime", "temp")
    print("smoke:", "ok" if ok else "FAILED", {k: d.get(k) for k in keys})
    for m in BAD_MESSAGES:
        print("qml error:", m)
    app.exit(0 if ok else 1)


def main() -> int:
    if SMOKE:
        qInstallMessageHandler(message_handler)
    app = QGuiApplication([a for a in sys.argv if a != "--smoke"])
    app.setApplicationName("rice-dash")
    app.setDesktopFileName("rice-dash")

    server = None
    if not SMOKE:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(150):
            sock.write(b"toggle")
            sock.flush()
            sock.waitForBytesWritten(300)
            return 0
        QLocalServer.removeServer(SERVER_NAME)
        server = QLocalServer()
        server.listen(SERVER_NAME)

    stats = Stats()
    if server is not None:
        server.newConnection.connect(lambda: (server.nextPendingConnection(), stats.toggleRequested.emit()))

    engine = QQmlApplicationEngine()
    engine.quit.connect(app.quit)
    engine.rootContext().setContextProperty("stats", stats)
    engine.loadData(QByteArray(QML.encode()), QUrl("file:///rice-dash.qml"))
    if not engine.rootObjects():
        return 1
    if SMOKE:
        QTimer.singleShot(3000, lambda: finish_smoke(app, stats))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
