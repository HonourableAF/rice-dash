#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.80", "psutil>=5.9"]
# ///
"""rice-dash: a Nord-themed terminal dashboard.

    uv run dash.py        (textual + psutil are installed automatically)

Keys:  q = quit
Optional: playerctl (for the now-playing panel)
"""
from __future__ import annotations

import asyncio
import getpass
import math
import os
import socket
import time
from collections import deque

import psutil
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

# ── Nord palette ─────────────────────────────────────────────────────────
BG, BG2, LINE, MUTED = "#2e3440", "#3b4252", "#4c566a", "#7b88a1"
FG, FG_HI = "#d8dee9", "#eceff4"
CYAN, ICE, SKY, BLUE = "#8fbcbb", "#88c0d0", "#81a1c1", "#5e81ac"
RED, ORANGE, YELLOW, GREEN, PURPLE = "#bf616a", "#d08770", "#ebcb8b", "#a3be8c", "#b48ead"

BLOCKS = "▁▂▃▄▅▆▇█"
PARTIAL = " ▏▎▍▌▋▊▉"


# ── drawing helpers ──────────────────────────────────────────────────────
def heat(f: float) -> str:
    """Green → red as a fraction climbs."""
    return GREEN if f < 0.5 else YELLOW if f < 0.75 else ORANGE if f < 0.9 else RED


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


def bar(frac: float, width: int, color: str | None = None) -> Text:
    """Smooth horizontal bar with 1/8-cell resolution."""
    width = max(width, 1)
    frac = max(0.0, min(frac, 1.0))
    c = color or heat(frac)
    full = frac * width
    n = int(full)
    rem = int((full - n) * 8)
    t = Text(no_wrap=True)
    t.append("█" * n, style=c)
    if n < width:
        t.append(PARTIAL[rem] if rem else "░", style=c if rem else LINE)
        t.append("░" * (width - n - 1), style=LINE)
    return t


def area(values, width: int, rows: int, hi: float, colors: list[str]) -> list[Text]:
    """Multi-row filled graph. `colors` is bottom → top, one per row."""
    width = max(width, 1)
    vals = list(values)[-width:]
    vals = [0.0] * (width - len(vals)) + vals
    hi = max(hi, 1e-9)
    levels = [max(0.0, min(v / hi, 1.0)) * rows * 8 for v in vals]
    lines = []
    for r in range(rows - 1, -1, -1):
        line = Text(no_wrap=True)
        for lv in levels:
            x = lv - r * 8
            ch = "█" if x >= 8 else (("▁" if r == 0 else " ") if x < 1 else BLOCKS[int(x) - 1])
            line.append(ch, style=colors[r])
        lines.append(line)
    return lines


def meter(name: str, used: float, total: float, w: int, pad: int) -> Text:
    frac = used / total if total else 0.0
    suffix = f" {frac * 100:3.0f}%  {human(used)}/{human(total)}"
    bw = max(4, w - pad - 1 - len(suffix))
    t = Text(no_wrap=True)
    t.append(f"{name:<{pad}} ", style=MUTED)
    t.append_text(bar(frac, bw))
    t.append(suffix, style=FG)
    return t


def gradient(n: int, stops: tuple[str, ...] = (BLUE, SKY, ICE, CYAN)) -> list[str]:
    """n colors interpolated bottom → top across the stops."""
    rgb = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in stops]
    out = []
    for k in range(n):
        pos = k / max(n - 1, 1) * (len(rgb) - 1)
        i = min(int(pos), len(rgb) - 2)
        f = pos - i
        mixed = [round(a + (b - a) * f) for a, b in zip(rgb[i], rgb[i + 1])]
        out.append("#{:02x}{:02x}{:02x}".format(*mixed))
    return out


def lines_to_text(lines: list[Text]) -> Text:
    return Text("\n").join(lines)


# ── panel base ───────────────────────────────────────────────────────────
class Panel(Static):
    LABEL = ""
    INTERVAL = 1.0

    def on_mount(self) -> None:
        self.border_title = f" {self.LABEL} "
        self.setup()
        self.set_interval(self.INTERVAL, self.tick)
        self.call_after_refresh(self.tick)

    @property
    def w(self) -> int:
        return max(12, self.content_size.width or 40)

    def setup(self) -> None:
        pass

    def tick(self) -> None:
        pass


# ── panels ───────────────────────────────────────────────────────────────
class CpuPanel(Panel):
    LABEL = "cpu"

    def setup(self) -> None:
        self.hist: deque[float] = deque(maxlen=400)
        self.model = ""
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        self.model = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
        psutil.cpu_percent(percpu=True)  # prime the counters

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

    def tick(self) -> None:
        per = psutil.cpu_percent(percpu=True)
        total = sum(per) / max(len(per), 1)
        self.hist.append(total)
        self.app.cpu_load = total
        w = self.w

        head = Text(no_wrap=True)
        head.append(f"{total:5.1f}%", style=f"bold {heat(total / 100)}")
        try:
            freq = psutil.cpu_freq()
        except (OSError, NotImplementedError):
            freq = None
        if freq:
            head.append(f"  {freq.current / 1000:.2f} GHz", style=FG)
        t = self.temp()
        if t is not None:
            head.append(f"  {t:.0f}°C", style=heat((t - 40) / 50))
        head.append(f"  load {os.getloadavg()[0]:.2f}", style=MUTED)

        cores = Text("cores ", style=MUTED, no_wrap=True)
        for p in per:
            cores.append(BLOCKS[round(p / 100 * 7)], style=heat(p / 100))

        lines: list[Text] = []
        if self.model:
            m = Text(self.model, style=MUTED, no_wrap=True)
            m.truncate(w, overflow="ellipsis")
            lines.append(m)
        lines += [head, *area(self.hist, w, 3, 100, [BLUE, SKY, ICE]), cores]
        self.update(lines_to_text(lines))


class MemPanel(Panel):
    LABEL = "memory"

    def tick(self) -> None:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        w = self.w
        self.update(
            lines_to_text(
                [
                    meter("ram", vm.total - vm.available, vm.total, w, 4),
                    meter("swap", sw.used, sw.total, w, 4),
                ]
            )
        )


class DiskPanel(Panel):
    LABEL = "disks"
    INTERVAL = 10.0
    PSEUDO = {"squashfs", "tmpfs", "devtmpfs", "overlay", "efivarfs", "vfat"}
    SKIP = ("/snap", "/boot", "/var/lib/docker", "/var/lib/containers")

    def tick(self) -> None:
        seen: set[str] = set()
        lines: list[Text] = []
        for p in psutil.disk_partitions():
            mp = p.mountpoint
            if p.device in seen or p.fstype in self.PSEUDO or mp.startswith(self.SKIP):
                continue
            if mp.startswith("/run") and not mp.startswith("/run/media"):
                continue
            try:
                u = psutil.disk_usage(mp)
            except OSError:
                continue
            seen.add(p.device)
            name = mp if len(mp) <= 10 else "…" + mp[-9:]
            lines.append(meter(name, u.used, u.total, self.w, 10))
            if len(lines) == 5:
                break
        self.update(lines_to_text(lines) if lines else Text("no disks found", style=MUTED))


class NetPanel(Panel):
    LABEL = "network"
    IGNORE = ("docker", "veth", "br-", "virbr")

    def counters(self) -> tuple[int, int]:
        rx = tx = 0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if name == "lo" or name.startswith(self.IGNORE):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        return rx, tx

    def setup(self) -> None:
        self.down: deque[float] = deque(maxlen=400)
        self.up: deque[float] = deque(maxlen=400)
        rx, tx = self.counters()
        self.last = (time.monotonic(), rx, tx)

    def tick(self) -> None:
        now = time.monotonic()
        rx, tx = self.counters()
        t0, rx0, tx0 = self.last
        dt = max(now - t0, 1e-3)
        d, u = max(rx - rx0, 0) / dt, max(tx - tx0, 0) / dt
        self.last = (now, rx, tx)
        self.down.append(d)
        self.up.append(u)
        w, floor = self.w, 64 * 1024

        def head(arrow: str, color: str, rate: float, total: int) -> Text:
            return Text.assemble(
                (f"{arrow} ", color),
                (f"{human(rate)}/s", f"bold {FG_HI}"),
                (f"   {human(total)} since boot", MUTED),
            )

        lines = [
            head("↓", ICE, d, rx),
            *area(self.down, w, 2, max(max(self.down), floor), [ICE, ICE]),
            head("↑", PURPLE, u, tx),
            *area(self.up, w, 2, max(max(self.up), floor), [PURPLE, PURPLE]),
        ]
        self.update(lines_to_text(lines))


class MediaPanel(Panel):
    LABEL = "now playing"
    FMT = "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}\t{{position}}\t{{mpris:length}}"

    async def tick(self) -> None:  # type: ignore[override]
        w = self.w
        try:
            proc = await asyncio.create_subprocess_exec(
                "playerctl", "metadata", "--format", self.FMT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.update(Text("install playerctl to enable this panel", style=MUTED))
            return
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 2)
        except asyncio.TimeoutError:
            proc.kill()
            return
        if proc.returncode != 0 or not out.strip():
            self.update(Text("♪ nothing playing", style=MUTED))
            return

        parts = (out.decode(errors="replace").rstrip("\n").split("\t") + [""] * 6)[:6]
        status, player, artist, title, pos, length = parts
        icon = {"Playing": "▶", "Paused": "⏸"}.get(status, "■")
        color = ICE if status == "Playing" else MUTED

        top = Text.assemble((f"{icon} ", color), (title or "unknown", f"bold {FG_HI}"))
        top.no_wrap = True
        top.truncate(w, overflow="ellipsis")
        sub = Text(f"{artist or '—'}  ·  {player}", style=MUTED, no_wrap=True)
        sub.truncate(w, overflow="ellipsis")
        lines = [top, sub]

        if pos.isdigit() and length.isdigit() and int(length) > 0:
            p, ln = int(pos), int(length)
            tm = f" {mmss(p)}/{mmss(ln)}"
            t = Text(no_wrap=True)
            t.append_text(bar(p / ln, w - len(tm), ICE))
            t.append(tm, style=FG)
            lines.append(t)
        self.update(lines_to_text(lines))


class ProcPanel(Panel):
    LABEL = "top"
    INTERVAL = 2.0
    N = 5

    def tick(self) -> None:
        agg: dict[str, list[float]] = {}
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            i = p.info
            a = agg.setdefault(i.get("name") or "?", [0.0, 0.0, 0])
            a[0] += i.get("cpu_percent") or 0.0
            a[1] += i.get("memory_percent") or 0.0
            a[2] += 1
        top = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)[: self.N]

        nw = max(6, self.w - 14)
        lines = [Text(f"{'process':<{nw}} {'cpu%':>6}{'mem%':>7}", style=MUTED, no_wrap=True)]
        for name, (cpu, mem, n) in top:
            label = f"{name} ×{int(n)}" if n > 1 else name
            lines.append(
                Text.assemble(
                    (f"{label[:nw]:<{nw}} ", FG),
                    (f"{cpu:6.1f}", heat(min(cpu, 100) / 100)),
                    (f"{mem:7.1f}", FG),
                )
            )
        self.update(lines_to_text(lines))


class WavePanel(Panel):
    """Decorative animation that speeds up and grows with CPU load."""

    LABEL = "pulse"
    INTERVAL = 0.1
    def setup(self) -> None:
        self.phase = 0.0
        self.energy = 0.0

    def tick(self) -> None:
        load = self.app.cpu_load / 100
        self.energy += (load - self.energy) * 0.1
        self.phase += 0.1 * (1.0 + 3.0 * self.energy)
        amp = 0.35 + 0.65 * self.energy
        w, p = self.w, self.phase
        vals = [
            (
                math.sin(x * 0.20 + p * 1.7)
                + math.sin(x * 0.07 - p)
                + math.sin(x * 0.45 + p * 2.3) * 0.4
                + 2.4
            ) / 4.8 * amp
            for x in range(w)
        ]
        rows = max(3, self.content_size.height or 5)
        self.update(lines_to_text(area(vals, w, rows, 1.0, gradient(rows))))


# ── top bar with a big clock ─────────────────────────────────────────────
DIGITS = {
    "0": ["╭─╮", "│ │", "╰─╯"],
    "1": [" ╷ ", " │ ", " ╵ "],
    "2": ["╶─╮", "╭─╯", "╰─╴"],
    "3": ["╶─╮", " ─┤", "╶─╯"],
    "4": ["╷ ╷", "╰─┤", "  ╵"],
    "5": ["╭─╴", "╰─╮", "╶─╯"],
    "6": ["╭─╴", "├─╮", "╰─╯"],
    "7": ["╶─╮", "  │", "  ╵"],
    "8": ["╭─╮", "├─┤", "╰─╯"],
    "9": ["╭─╮", "╰─┤", "╶─╯"],
    ":": ["·", " ", "·"],
}


def big(s: str) -> list[str]:
    rows = ["", "", ""]
    for ch in s:
        glyph = DIGITS.get(ch, ["   "] * 3)
        for i in range(3):
            rows[i] += glyph[i] + " "
    return rows


class TopBar(Static):
    def on_mount(self) -> None:
        try:
            self.who = f"{getpass.getuser()}@{socket.gethostname()}"
        except Exception:
            self.who = socket.gethostname()
        self.set_interval(1.0, self.tick)
        self.tick()

    def tick(self) -> None:
        now = time.localtime()
        up = int(time.time() - psutil.boot_time())
        d, rem = divmod(up, 86400)
        h, rem = divmod(rem, 3600)
        uptime = (f"{d}d " if d else "") + f"{h}h {rem // 60:02d}m"

        clock = lines_to_text(
            [Text(r, style=f"bold {ICE}", no_wrap=True) for r in big(time.strftime("%H:%M:%S", now))]
        )
        mid = Text.assemble(
            (time.strftime("%A", now) + "\n", f"bold {FG_HI}"),
            (time.strftime("%d %B %Y", now) + "\n", FG),
            (self.who, MUTED),
        )
        right = Text.assemble(("up ", MUTED), (uptime, FG_HI), ("\n", ""), ("q", f"bold {ICE}"), (" quit", MUTED))

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", vertical="middle")
        grid.add_column(justify="center", ratio=1, vertical="middle")
        grid.add_column(justify="right", vertical="middle")
        grid.add_row(clock, mid, right)
        self.update(grid)


# ── app ──────────────────────────────────────────────────────────────────
class Dash(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("q", "quit", "Quit")]
    cpu_load: float = 0.0

    CSS = """
    $bg: #2e3440;
    $bg2: #3b4252;
    $line: #4c566a;
    $ice: #88c0d0;
    $fg: #d8dee9;

    Screen { background: $bg; color: $fg; }
    TopBar { height: 5; padding: 1 2; background: $bg2; }
    #cols { height: auto; }
    .col { width: 1fr; height: auto; }
    .panel {
        width: 1fr;
        height: auto;
        margin: 0 1;
        padding: 0 1;
        background: $bg;
        border: round $line;
        border-title-color: $ice;
        border-title-style: bold;
    }
    WavePanel.panel { height: 1fr; min-height: 7; }
    """

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="cols"):
            with Vertical(classes="col"):
                yield CpuPanel(classes="panel")
                yield MemPanel(classes="panel")
                yield DiskPanel(classes="panel")
            with Vertical(classes="col"):
                yield NetPanel(classes="panel")
                yield MediaPanel(classes="panel")
                yield ProcPanel(classes="panel")
        yield WavePanel(classes="panel")


if __name__ == "__main__":
    Dash().run()
