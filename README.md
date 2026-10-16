# rice-dash

A Nord-themed system dashboard for Linux, in two flavours:

- **`dash.py`** is a terminal dashboard (TUI) built with [Textual](https://textual.textualize.io/). Run it in a terminal pane.
- **`dash-overlay.py`** is a fullscreen animated overlay built with PyQt6 and QML. It pops up over your desktop, shows the same kind of stats, and closes with a keypress.

Both are single files. There is no virtualenv to set up: [`uv`](https://docs.astral.sh/uv/) reads the dependencies from the header of each script and installs them on first run.

## What it shows

| Panel | Details |
| --- | --- |
| Clock | Big clock with seconds, date, user@host, uptime |
| CPU | Load gauge, frequency, temperature, load average, history graph, per-core bars |
| Memory and disks | RAM gauge, swap and disk usage bars |
| Network | Live download and upload rates with history graphs |
| Now playing | Track, artist and progress of whatever is playing (needs `playerctl`) |
| Top processes | The busiest processes, grouped by name |
| Pulse | A flowing wave that speeds up and grows with CPU load |

## Requirements

- Linux with Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Optional: `playerctl` for the now-playing panel, and the JetBrains Mono font (the overlay falls back to your default font without it)

On Arch-based systems:

```sh
sudo pacman -S --needed uv playerctl ttf-jetbrains-mono
```

## Run it

```sh
git clone https://github.com/HonourableAF/rice-dash.git
cd rice-dash

uv run dash.py
uv run dash-overlay.py
```

### Terminal dashboard (`dash.py`)

Press `q` to quit.

### Overlay (`dash-overlay.py`)

- Close it with `Esc`, `q`, or a click.
- Run the command again while it is open to close it. This means one keyboard shortcut can both open and close it.

**Bind it to a key on KDE Plasma:** System Settings, then Keyboard, then Shortcuts, then Add Command, and use:

```sh
uv run /path/to/rice-dash/dash-overlay.py
```

**Blur behind the overlay (KDE):** the overlay registers the app ID `rice-dash`. Create a KWin Window Rule that matches that window class and set Blur to Force Yes.

**Optional desktop entry:** if you see a harmless warning about the desktop portal not finding the app ID, create `~/.local/share/applications/rice-dash.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=rice-dash
Exec=uv run /path/to/rice-dash/dash-overlay.py
Icon=utilities-system-monitor
Categories=System;Monitor;
```

## Customising

- Colours are plain hex values. In `dash.py` they are constants at the top of the file. In `dash-overlay.py` they appear inside the QML block.
- The overlay's panels are the `Card` items in the QML block, so adding or removing one means editing a single block.

## Performance notes

The overlay redraws the screen about 15 times a second while it is open, so it uses some CPU. The percentage you see depends heavily on your power profile: on battery or in balanced mode the CPU runs at a lower clock, so the same work shows as a higher percentage than in performance mode. It does not mean the overlay costs more energy there.

## How it works

- `psutil` provides CPU, memory, disk, network and process stats.
- The overlay collects them once a second in Python and hands them to QML as a single object.
- A second launch of the overlay talks to the first over a local socket, which is how the toggle works.
