# Deskrab plugin for Claude Code

Deskrab is a desk companion: a pixel crab on a small display that shows what Claude Code is doing right now (thinking, running commands, editing, waiting for your permission), with your 5-hour and 7-day usage limits underneath.

This plugin feeds the display. Its hooks send each Claude Code event to a small Python bridge, which talks to the display over USB serial or your home Wi-Fi. Nothing leaves your computer and your local network.

## Requirements

- Python 3.8 or newer on the computer. Nothing else to install: pyserial is bundled.
- The Deskrab display, connected by USB (Wi-Fi can be set up afterwards).

## Install

```
/plugin marketplace add ricardomdcj/deskrab-plugin
/plugin install deskrab@deskrab
/deskrab:setup
```

`/deskrab:setup` finds your Python, installs the status line (an existing status line keeps working), checks that the display answers, and offers Wi-Fi and the display language (Portuguese or English). Open a new session afterwards.

## Update

```
/plugin marketplace update deskrab
/plugin update deskrab@deskrab
```

Each plugin version carries the matching firmware version. When the display's firmware is older, the details screen shows it in yellow; update it from https://ricardomdcj.github.io/deskrab-plugin/ (Chrome or Edge, display on USB).

## Uninstall

Ask Claude to uninstall Deskrab (the setup skill restores your previous status line), then run `/plugin uninstall deskrab@deskrab`.
