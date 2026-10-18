# rice-dash

[![rice-dash overlay](screenshots/overlay.png)](screenshots/overlay.png)

A Nord-themed system dashboard, in three parts. Pick the one that fits your system:

| Version | Files | Runs on | Built with |
| --- | --- | --- | --- |
| **Quickshell overlay** (recommended on Linux) | [`quickshell/`](quickshell/) | Linux with a Wayland session | QML only |
| **PyQt overlay** | [`dash-overlay.py`](dash-overlay.py) | Linux (X11 or Wayland) and Windows | Python, PyQt6, QML |
| **Terminal dashboard** | [`dash.py`](dash.py) | Linux | Python, [Textual](https://textual.textualize.io/) |

The two overlays look the same and show the same stats. The Quickshell one is a port of the PyQt one with the Python layer removed, so it is lighter, but Quickshell only exists for Wayland. That is why Windows uses the PyQt version.

Jump to: [Linux](#linux) · [Windows](#windows) · [Customising](#customising) · [Performance notes](#performance-notes)

## What it shows

| Panel | Details |
| --- | --- |
| Clock | Big clock with seconds, date, user@host, uptime |
| CPU | Load gauge, frequency, temperature, load average, history graph, per-core bars |
| Memory and disks | RAM gauge, swap and disk usage bars |
| Network | Live download and upload rates with history graphs |
| Now playing | Track, artist and progress of whatever is playing |
| Processes | Busiest processes grouped by name. In the Quickshell overlay the list scrolls with the mouse wheel or the Up and Down arrow keys. |
| Pulse | A wave along the bottom that speeds up and grows with CPU load. It is animated in the PyQt overlay. The Quickshell overlay draws it once and keeps it still to save CPU. |

## Linux

Everything below needs the [`uv`](https://docs.astral.sh/uv/) package manager for the Python versions. On Arch-based systems use `sudo pacman -S --needed uv`. On other distributions:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the JetBrains Mono font as well, or the layout falls back to another font. On Arch-based systems: `sudo pacman -S --needed ttf-jetbrains-mono`.

### Quickshell overlay

A fullscreen, animated overlay that runs as a layer-shell window. It needs no Python at all: stats come straight from `/proc`, plus small `top`, `df` and `playerctl` calls. It only polls while it is open, and everything is released when it closes.

Tested on KDE Plasma 6 (Wayland) on EndeavourOS. It should work on other Wayland compositors that support the layer-shell protocol, but those are untested.

**Requirements**

- A Wayland session
- [Quickshell](https://quickshell.org/) (on Arch-based systems: `yay -S quickshell`)
- Optional: `playerctl` for the now-playing card

**Install**

```
git clone https://github.com/HonourableAF/rice-dash.git
cd rice-dash

mkdir -p ~/.config/quickshell
cp quickshell/{shell,Dash,Stats}.qml ~/.config/quickshell/
install -Dm755 quickshell/dash-toggle ~/.local/bin/dash-toggle
```

This uses Quickshell's default config location, so it replaces any `~/.config/quickshell/shell.qml` you already have. Make sure `~/.local/bin` is on your `PATH`.

**Use it**

Run `dash-toggle`. It opens the overlay, and running it again closes it. You can also close it with `Esc`, `q`, or a click. Scroll the process list with the mouse wheel or the Up and Down keys.

To bind it to a key on KDE Plasma: System Settings, then Keyboard, then Shortcuts, then Add New, then Command or Script, and enter the full path, for example `/home/you/.local/bin/dash-toggle`.

On Hyprland (untested), add this to `hyprland.conf` and run `hyprctl reload`:

```
bind = SUPER SHIFT, D, exec, ~/.local/bin/dash-toggle
```

To try it without installing the script:

```
quickshell
quickshell ipc call dash toggle
```

The second command closes the overlay from another terminal.

**Files**

| File | Job |
| --- | --- |
| `shell.qml` | Entry point. Starts the overlay and exposes the `toggle` IPC call. |
| `Dash.qml` | The overlay window and every panel. Colours and layout live here. |
| `Stats.qml` | Collects the system stats once a second and hands them to `Dash.qml`. |
| `dash-toggle` | Closes the overlay if it is running, otherwise starts it. |

**How it works**

- `Stats.qml` reads `/proc/stat`, `/proc/meminfo`, `/proc/net/dev`, `/proc/loadavg`, `/proc/uptime` and `/proc/cpuinfo`, and finds the CPU temperature sensor under `/sys/class/hwmon`.
- Processes come from `top`, disks from `df`, and now-playing from `playerctl`.
- `dash-toggle` first tries `quickshell ipc call dash toggle`. If nothing is running, it starts Quickshell instead.

**Bring the wave back to life**

The pulse wave is the most expensive thing on screen because it repaints about 15 times a second. To make it animate again, find this line in `Dash.qml`:

```
function onTChanged() { wave.phase += 0.07 * (1 + 2 * win.energy) }
```

and change it to:

```
function onTChanged() { wave.phase += 0.07 * (1 + 2 * win.energy); wave.requestPaint() }
```

**Troubleshooting**

- **Nothing appears:** run `quickshell` in a terminal and read the output. Warnings about missing icon themes are harmless.
- **Now playing says "nothing playing":** install `playerctl` and start a media player.
- **Keys do nothing:** click anywhere on the overlay to close it, or run `pkill quickshell`.
- **The shortcut does nothing:** use the full path to `dash-toggle`, because some launchers do not have `~/.local/bin` on their `PATH`.

### PyQt overlay

A single file that `uv` runs, installing its dependencies on first use. There is no virtualenv to set up.

```
git clone https://github.com/HonourableAF/rice-dash.git
cd rice-dash
uv run dash-overlay.py
```

- Close it with `Esc`, `q`, or a click.
- Run the command again while it is open to close it, so one keyboard shortcut can both open and close it.
- For the now-playing card, install `playerctl`.
- On KDE Plasma, bind `uv run /path/to/rice-dash/dash-overlay.py` as a Command shortcut.

Optional extras:

- **Blur behind the overlay (KDE):** the overlay registers the app ID `rice-dash`. Create a KWin Window Rule that matches that window class and set Blur to Force Yes.
- **Desktop entry:** if you see a harmless warning about the desktop portal not finding the app ID, create `~/.local/share/applications/rice-dash.desktop`:

```
[Desktop Entry]
Type=Application
Name=rice-dash
Exec=uv run /path/to/rice-dash/dash-overlay.py
Icon=utilities-system-monitor
Categories=System;Monitor;
```

**How it works:** `psutil` provides the stats, the overlay collects them once a second in Python and hands them to QML as a single object, and a second launch talks to the first over a local socket, which is how the toggle works.

### Terminal dashboard

`dash.py` is a single file, run the same way:

```
uv run dash.py
```

Press `q` to quit. Colours are constants at the top of the file. It needs Python 3.10 or newer and has not been tested on Windows.

## Windows

The PyQt overlay runs on Windows. The Quickshell overlay does not, because Quickshell only exists for Wayland.

This has been tested in a Windows 11 virtual machine, not on real Windows hardware yet. If something looks wrong, please open an issue.

### Run it

Open PowerShell and install the tools:

```
winget install astral-sh.uv
winget install Git.Git
winget install JetBrains.JetBrainsMono
```

Close PowerShell and open a new window, so it can find the new programs. Then:

```
git clone https://github.com/HonourableAF/rice-dash.git
cd rice-dash
uv run dash-overlay.py
```

The first run downloads Python and the dependencies, so give it a minute. Close the overlay with `Esc`, `q`, or a click.

### Now playing

The now-playing card needs a few extra packages, and they need Python 3.12 because they may not support the newest Python yet. Run this to turn it on:

```
uv run --python 3.12 --with winrt-runtime --with winrt-Windows.Media.Control --with winrt-Windows.Foundation --with winrt-Windows.Foundation.Collections dash-overlay.py
```

Check the spelling of the last package: it is `Collections`, with an s. Only apps that register with the Windows media controls show up, such as Edge, Chrome, Spotify and Media Player. Without the packages, the card says "media: see README".

### Keyboard shortcut

Install [AutoHotkey](https://www.autohotkey.com/) v2 and run a script like this one. Running it again closes the overlay:

```
^!d::Run 'uv run "C:\path\to\rice-dash\dash-overlay.py"', , 'Hide'
```

`^!d` means Ctrl+Alt+D. For the now-playing version, put the long command from the section above inside the quotes instead.

### Differences from Linux

- **CPU temperature** shows a dash, because Windows does not expose it without extra tools.
- **Processes** leave out the System Idle Process.
- **CPU model** is read from the registry.
- **Network** ignores virtual adapters such as VMware, VirtualBox and Hyper-V.

### Troubleshooting

- **The overlay is black, flickers, or is very slow** (common in virtual machines or on old graphics): turn on software rendering. In PowerShell: `$env:QT_QUICK_BACKEND = "software"`. In Git Bash: put `QT_QUICK_BACKEND=software` in front of the command. In cmd: `set QT_QUICK_BACKEND=software`.
- **`uv` or `git` is not recognised:** open a new terminal window after installing them.
- **"No solution found" with the media command:** a package name is misspelled. Compare it against the command above.
- **A fullscreen window with a see-through background can render oddly** on some setups. Software rendering sometimes helps.

## Customising

- Colours are plain hex values. In `dash.py` they are constants at the top of the file. In the overlays they appear inside the QML (`quickshell/Dash.qml`, or the QML block in `dash-overlay.py`).
- The overlay panels are `Card` items, so adding or removing one means editing a single block.
- To change what is measured in the Quickshell version, edit `quickshell/Stats.qml`. For the PyQt version, edit the `Stats` class in `dash-overlay.py`.

## Performance notes

The overlays redraw the screen about 15 times a second while open, so they use some CPU. The percentage you see depends heavily on your power profile: on battery or in balanced mode the CPU runs at a lower clock, so the same work shows as a higher percentage than in performance mode. It does not mean the overlay costs more energy there.

The Quickshell version avoids running Python and PyQt, and it only polls while it is open. Measure both on your own machine to compare.

## Development

`dash-overlay.py --smoke` starts the overlay without a screen, collects stats for three seconds, and exits with an error if the QML throws any errors. On Linux:

```
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software uv run dash-overlay.py --smoke
```

A GitHub Actions workflow runs the same check on a Windows machine for every push.
