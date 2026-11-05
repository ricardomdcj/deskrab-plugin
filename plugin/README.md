# Deskrab plugin for Claude Code

Deskrab is a desk companion: a pixel crab on a small USB display that shows what Claude Code is doing right now (thinking, running commands, editing, waiting for your permission), with your 5-hour and 7-day usage limits underneath.

This plugin feeds the display. Its hooks send each Claude Code event to a small Python bridge, which talks to the display over USB serial. There is no network connection.

## Requirements

- Python 3.8 or newer on the computer. Nothing else to install: pyserial is bundled.
- The Deskrab display, connected by USB.

## Install

```
/plugin marketplace add ricardomdcj/deskrab-plugin
/plugin install deskrab@deskrab
/deskrab:setup
```

`/deskrab:setup` finds your Python, installs the status line (an existing status line keeps working) and checks that the display answers. Open a new session afterwards.

## Uninstall

Ask Claude to uninstall Deskrab (the setup skill restores your previous status line), then run `/plugin uninstall deskrab@deskrab`.
