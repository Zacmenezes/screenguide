# screenguide

Step-by-step guidance for any task in the Linux desktop interface. You type what you want to do,
such as "change the Gmail theme", and a panel shows the next step: a short instruction and an
enlarged crop of your screen with an arrow on the element to use. You do the step, press
**Next step**, and the panel works out the one after it, until the task is done.

**screenguide never acts for you.** It only captures the screen and shows the way; every click
and every keystroke is yours. No part of it clicks, types or acts on your behalf.

This is the first milestone (M1): one model provider (Anthropic), text-only element detection and
a simple panel. See [Known limits](#known-limits) before relying on it.

## What is sent to the model provider

**Each step sends a screenshot of your whole screen to Anthropic.** Anything visible on it goes
too: open documents, messages, emails, passwords shown in plain text, notifications, other
windows and other monitors. Close or cover what you do not want to share before you ask for a
step.

In detail, each step sends:

- the screenshot of the whole desktop, every monitor included, or of one monitor if you set
  [`monitor`](#configuration), with numbered boxes drawn around the text found on it;
- the text found on the screen, as a numbered list;
- the goal you typed and the instructions of the earlier steps.

A step whose reply cannot be read is asked once more, which sends the same request again.

The panel turns invisible during each capture, so it stays out of the screenshot, and says
"Sending a screenshot (…) to external provider anthropic (…)" each time one goes out. The screen
is captured only when you ask for a step, once per step. Screenshots stay in memory unless you
set [`screenshot_dir`](#configuration). Anthropic's own terms govern what happens to the data it
receives.

## Requirements

- **Tested only on Ubuntu 26.04, GNOME 50, Wayland.** Other desktops may work but have not been
  tried; see [Known limits](#known-limits).
- Python 3.11 to 3.14. The OCR engine has no Python 3.15 build yet.
- `python3-venv`, which a default Ubuntu install lacks, and `libxcb-cursor0`, which Qt needs to
  run the panel through XWayland:

  ```sh
  sudo apt install python3-venv libxcb-cursor0
  ```

- An Anthropic API key, from the [Claude Console](https://console.anthropic.com/).

## Install

```sh
git clone https://github.com/Zacmenezes/screenguide.git
cd screenguide
python3 -m venv .venv
.venv/bin/pip install .
```

This installs the `screenguide` command in `.venv/bin`. Check it with:

```sh
.venv/bin/screenguide --help
```

The examples below assume `.venv/bin` is on your `PATH` (`source .venv/bin/activate`).

## API key

The Anthropic SDK looks for credentials in this order: the `ANTHROPIC_API_KEY` variable, then a
profile saved by `ant auth login`.

A panel started from a keyboard shortcut does not read your shell profile, so an `export` in
`~/.bashrc` does not reach it. Either use `ant auth login`, or put the key where GNOME reads it:

```sh
mkdir -p ~/.config/environment.d
printf 'ANTHROPIC_API_KEY=%s\n' 'sk-ant-...' > ~/.config/environment.d/screenguide.conf
chmod 600 ~/.config/environment.d/screenguide.conf
```

GNOME reads this file at login, so log out and back in once.

## Configuration

Settings live in `~/.config/screenguide/config.toml`. Each one can also be set with a
`SCREENGUIDE_*` variable, which wins over the file. All are optional.

| Setting | Variable | Default | Meaning |
| --- | --- | --- | --- |
| `model` | `SCREENGUIDE_MODEL` | `claude-haiku-5-5` | Anthropic model to call |
| `effort` | `SCREENGUIDE_EFFORT` | the model's own | `low`, `medium`, `high` and so on, as the model accepts |
| `monitor` | `SCREENGUIDE_MONITOR` | whole desktop | Connector name of the one monitor to capture, from `screenguide monitors` |
| `image_long_edge` | `SCREENGUIDE_IMAGE_LONG_EDGE` | `2576` | Long edge, in pixels, of the image sent to the model |
| `screenshot_dir` | `SCREENGUIDE_SCREENSHOT_DIR` | unset: memory only | Directory where each capture is also saved |
| `usage_log` | `SCREENGUIDE_USAGE_LOG` | `~/.local/share/screenguide/usage` | Where usage is recorded |

For example:

```toml
model = "claude-haiku-5-5"
monitor = "DP-1"
```

### Pricing

Costs are estimated from `~/.config/screenguide/pricing.toml`, and only from there. Start from the
example in this repository and keep the prices up to date:

```sh
mkdir -p ~/.config/screenguide
cp pricing.example.toml ~/.config/screenguide/pricing.toml
```

Without that file, or for a model missing from it, the cost shows as unknown. Token counts are
recorded either way.

## Keyboard shortcut

`screenguide toggle` shows the panel, starting it if it is not running, or hides it when it is
showing. Bind it to a key in GNOME: Settings → Keyboard → View and Customize Shortcuts → Custom
Shortcuts, then add one with

- **Name:** `screenguide`
- **Command:** the full path to the command, followed by `toggle`, such as
  `/home/you/screenguide/.venv/bin/screenguide toggle`. GNOME does not see your shell's `PATH`;
  `command -v screenguide` prints the path.
- **Shortcut:** for example Ctrl+Alt+G.

[docs/global-shortcut.md](docs/global-shortcut.md) does the same from a terminal and explains why
this is not the `GlobalShortcuts` portal.

## Guide a task

1. Press the shortcut. The panel opens on top of your windows.
2. Type the goal, such as "Change the Gmail theme", and press Enter. The panel shows "Thinking…",
   steps out of the way while it captures the screen, and then shows the first step.
3. Read the instruction. The target's name is in colour, and the crop shows where it is. When
   the target has no text of its own, such as an icon, the panel shows only the instruction.
4. Do the step yourself, then press **Next step**. Repeat until the panel shows "Done".

The shortcut hides and shows the panel without losing the guide; × closes it. Instructions follow
the language of the goal you type.

From a terminal, `screenguide panel` opens the same panel, and `screenguide guide --goal "..."`
runs the guide with no panel, printing each step.

## Usage and cost

Every model call is recorded in the usage log with its tokens, as the API reports them, image
size, latency and estimated cost. To summarise by day, session, provider and model:

```sh
screenguide usage
```

## Other commands

| Command | What it does |
| --- | --- |
| `screenguide monitors` | Lists the monitors by connector name, for `monitor` |
| `screenguide run --image shot.png --goal "..."` | One step for a saved screenshot |
| `screenguide detect --image shot.png --output marked.png` | The text boxes found in an image, with no model call |
| `screenguide panel --demo-step --image shot.png` | The panel with made-up steps, with no capture and no model call |

## Known limits

- **Icons without text get no arrow.** Elements are found by OCR, so only text has a box. When
  the target is an icon, such as a gear or a profile picture, the panel shows the instruction
  alone, naming the icon and where it is ([#22](https://github.com/Zacmenezes/screenguide/issues/22)).
- **Tested platforms:** Ubuntu 26.04 with GNOME 50 on Wayland only. Capture uses the
  `org.freedesktop.portal.Screenshot` portal and the panel runs through XWayland to stay on top;
  X11, KDE and wlroots desktops are untested
  ([#19](https://github.com/Zacmenezes/screenguide/issues/19)).
- **The screen flashes on every capture,** with GNOME's screenshot animation
  ([#35](https://github.com/Zacmenezes/screenguide/issues/35)).
- **Steps inside an open menu:** clicking **Next step** takes focus from the app, which can close
  the menu you just opened before the capture sees it
  ([#37](https://github.com/Zacmenezes/screenguide/issues/37)).
- Anthropic is the only provider. There is no per-session budget yet
  ([#21](https://github.com/Zacmenezes/screenguide/issues/21)), and the panel shows no token or
  cost line; use `screenguide usage`.
- Each step is a full model call with a full-screen image. Check `screenguide usage` to see what
  a task costs with your model.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
```

See [CLAUDE.md](CLAUDE.md) for the architecture and the way work is organised.

## License

[MIT](LICENSE)
