---
name: setup
description: Set up the Deskrab desk display after installing the plugin, or fix it when the crab stops reacting or the usage bars freeze. Finds a working Python, saves it as the plugin's python option, installs the Deskrab status line (keeping any status line the user already had), and checks that the display answers over USB. Use when the user runs /deskrab:setup, says the Deskrab is not reacting, or asks to configure or uninstall it.
---

# Deskrab setup

Deskrab is a small USB display with a pixel crab that mirrors what Claude Code is doing. The plugin's hooks feed it. They need two things the plugin can't set on its own: the Python that runs the bridge, and the status line, which carries the 5h/7d usage limits.

Talk to the user in their language. Explain each step in one line before running it. Ask before changing `~/.claude/settings.json`; the bridge makes a backup first.

The bridge is `${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py`. It needs only the Python standard library; pyserial is bundled.

## 1. Find a Python 3.8+

Try these in order and keep the first that prints a real path and a version of 3.8 or newer:

```
python3 -c "import sys; print(sys.executable, sys.version_info[:2])"
python  -c "import sys; print(sys.executable, sys.version_info[:2])"
py -3   -c "import sys; print(sys.executable, sys.version_info[:2])"      # Windows launcher
```

- On Windows, reject any path under `\WindowsApps\`. That is the Microsoft Store shortcut, which doesn't run scripts from hooks.
- If none works, stop and tell the user to install Python 3 (python.org on Windows and macOS, the package manager on Linux), then run `/deskrab:setup` again.

Use the absolute `sys.executable` path from here on, called `PY` below.

## 2. Plug in the display

Ask the user to connect the Deskrab with the USB cable and wait a few seconds for the crab to appear. If another program has the serial port open (Arduino IDE, `idf.py monitor`, a serial terminal), it must be closed.

## 3. Run the setup

```
"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" setup
```

It saves `PY` as the plugin's `python` option, installs the status line (an existing one keeps showing; Deskrab only reads its data), removes hooks from an older manual install, and on Windows without Git Bash points Claude Code at another bash if one exists. Then it starts the bridge and reports whether the display was found.

Show the user the lines it printed, in plain words.

## 4. Check

- `display: conectado em ...` means it works. Tell the user to **open a new Claude Code session** (hooks and the status line load at session start) and send any prompt: the crab starts thinking.
- `display: nao encontrado`: check the cable (some USB cables are charge-only), close programs using the port, then run `"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" status`. On Linux the user may need to be in the `dialout` group (`sudo usermod -aG dialout $USER`, then log out and in). On Windows a CH340 driver may be needed if the port doesn't appear in Device Manager.
- To show every animation once: `"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" demo`.

The Claude desktop app runs the hooks but not the status line. There the crab works, and the 5h/7d bars show the last values a CLI session saw.

## 5. USB or Wi-Fi

Once the display works over USB, ask the user which connection they want:

- **USB** (default): nothing else to do. The cable carries data and power.
- **Wi-Fi**: the display joins their home network and the cable can go to any USB charger. The computer must be on the same network. The display only sees **2.4 GHz** networks.

For Wi-Fi, ask for the network name and password. Tell the user the password will pass through this conversation. If they'd rather not share it, they can run the same command in their own terminal without `--password`, and it will ask for it there. With the display still on the USB cable:

```
"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" wifi --ssid "NETWORK" --password "PASSWORD"
```

The network goes to the display over the cable, along with a pairing code that only this computer receives. Then the command waits up to 40 s for the display to connect.

- `ok: display no Wi-Fi ...` means it worked. Tell the user they can move the display to a charger; the crab reconnects within a few seconds.
- `rede nao encontrada`: wrong name, or a 5 GHz-only network.
- `senha errada`: wrong password. Run it again.
- To go back to the cable: `"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" usb` (with the display plugged in, so its Wi-Fi is turned off too).

If the router later gives the display another IP address, the bridge finds it again by itself (UDP broadcast on port 7778). Firewalls that block local traffic can stop that. The display listens on TCP port 7777.

## Linux and macOS notes

- Linux: the user must be in the `dialout` group to open the serial port (`sudo usermod -aG dialout $USER`, then log out and back in). If ModemManager grabs the port right after it's plugged in, wait a few seconds.
- If the display restarts every time the bridge connects over USB, tell the user it's harmless (the board resets when the port opens on some systems). Wi-Fi mode avoids it.
- macOS: the display shows up as `/dev/cu.usbserial-*` or `/dev/cu.wchusbserial-*`. Recent macOS versions include the CH340 driver.

## Firmware updates

New firmware is installed from the browser, with nothing to install: https://ricardomdcj.github.io/deskrab-plugin/ (Chrome or Edge on a computer, display on the USB cable). Before flashing, stop the bridge so it releases the port: `"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" pause`, and afterwards `resume`.

## Uninstall

```
"PY" "${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py" uninstall
```

This restores the previous status line and stops the bridge. Then the user runs `/plugin uninstall deskrab@deskrab`. The bridge's data stays in `~/.deskrab` and can be deleted.
