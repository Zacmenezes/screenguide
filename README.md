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

**Each step sends a screenshot of your screen to Anthropic.** Anything visible on it goes
too: open documents, messages, emails, passwords shown in plain text, notifications and other
windows. Close or cover what you do not want to share before you ask for a
step.

In detail, each step sends:

- the screenshot of one monitor, the one the panel names on its "Next capture" line, or of the whole
  desktop, every monitor included, if you pick "Whole desktop" in the ⋮ menu (see
  [Choose the monitor](#choose-the-monitor)), with numbered boxes drawn around the text found on
  it;
- the text found on the screen, as a numbered list;
- the goal you typed and the instructions of the earlier steps.

A step whose reply cannot be read is asked once more, which sends the same request again.

The bar and the panel turn invisible during each capture, so they stay out of the screenshot, and says
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

The systemd user manager reads this file when it starts, and logging out does not always stop
it. Load the file into the running manager, then log out and back in so GNOME picks it up:

```sh
systemctl --user daemon-reload
systemctl --user show-environment | grep -c ANTHROPIC_API_KEY  # prints 1
```

A panel that was already running keeps its old environment: end it with ⋮ → Quit first.

## Configuration

Settings live in `~/.config/screenguide/config.toml`. Each one can also be set with a
`SCREENGUIDE_*` variable, which wins over the file. All are optional.

| Setting | Variable | Default | Meaning |
| --- | --- | --- | --- |
| `model` | `SCREENGUIDE_MODEL` | `claude-haiku-5-5` | Anthropic model to call |
| `effort` | `SCREENGUIDE_EFFORT` | the model's own | `low`, `medium`, `high` and so on, as the model accepts |
| `monitor` | `SCREENGUIDE_MONITOR` | the panel's monitor; whole desktop outside the panel | Connector name of the one monitor to capture, from `screenguide monitors`. In the panel, the starting choice |
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

`screenguide toggle` shows the bar and the panel, starting them if they are not running, or hides
them when they are showing. Bind it to a key in GNOME: Settings → Keyboard → View and Customize Shortcuts → Custom
Shortcuts, then add one with

- **Name:** `screenguide`
- **Command:** the full path to the command, followed by `toggle`, such as
  `/home/you/screenguide/.venv/bin/screenguide toggle`. GNOME does not see your shell's `PATH`;
  `command -v screenguide` prints the path.
- **Shortcut:** for example Ctrl+Alt+G.

[docs/global-shortcut.md](docs/global-shortcut.md) does the same from a terminal and explains why
this is not the `GlobalShortcuts` portal.

## Guide a task

1. Press the shortcut. A bar opens at the top centre of your monitor, on top of your windows,
   with the panel below it. Drag either one to move both.
2. Type the goal, such as "Change the Gmail theme", and press Enter. The panel shows "Thinking…",
   steps out of the way while it captures the screen, and then shows the first step.
3. Read the instruction. The target's name is in colour, and the crop shows where it is. When
   the target has no text of its own, such as an icon, the panel shows only the instruction.
4. Do the step yourself, then press **Next step**. Repeat until the panel shows "Done".

Instructions follow the language of the goal you type. The bar stays on screen with the panel:

- **Hide** folds the panel away under the bar. The guide carries on, and a step that arrives
  meanwhile is waiting when you press **Guide me +** to unfold it.
- **New Task**, in the panel header, drops the current task and asks for a new goal. A step the
  dropped task was still working on never appears.
- **×** drops the task the same way and folds the panel; screenguide keeps running with the bar.
- **⋮** holds **Capture monitor** (see below), **Usage**, which shows what
  `screenguide usage` prints, and **Quit**.

The shortcut hides and shows the whole window, bar included, without losing the guide.

From a terminal, `screenguide panel` opens the same panel, and `screenguide guide --goal "..."`
runs the guide with no panel, printing each step.

### Choose the monitor

Each capture is cropped to one monitor, which the panel names on its "Next capture" line. By default it
is the monitor the panel is on when you ask for the step, so dragging the panel to another monitor
moves the next capture there. ⋮ → **Capture monitor** pins captures to one monitor, by connector and
name as `screenguide monitors` lists them, until you pick "Follow panel" again. "Whole desktop"
sends every monitor in one image. [`monitor`](#configuration) sets the starting choice.

A pinned monitor that is unplugged gives an error in the panel, and nothing is captured or sent.

## Usage and cost

Every model call is recorded in the usage log with its tokens, as the API reports them, image
size, latency and estimated cost. To summarise by day, session, provider and model:

```sh
screenguide usage
```

## Evaluation

`screenguide eval` runs saved cases through the same pipeline as the app, one step each, and
scores whether the chosen element hit the right box. A case is one line of a JSONL file:

```json
{"image": "images/settings.png", "goal": "Open settings", "history": ["Click File, top left."], "correct_bbox": {"x": 60, "y": 8, "width": 70, "height": 20}}
```

`image` is relative to the cases file; `history` holds the instructions already shown;
`correct_bbox` is in pixels of the original image, or `null` for a case not yet labelled. A hit
is a chosen box that overlaps `correct_bbox`. A malformed line or a missing image is reported
with its line number and skipped; an unlabelled case is skipped without a model call, and the
summary counts the two apart.

```sh
screenguide eval eval/cases.jsonl --limit 2
```

Each case sends one screenshot to the provider, two if the reply needs its retry, and the
terminal says so before the first call. `--model`, `--effort` and `--image-long-edge` override
the [configuration](#configuration) for one run. The run file goes to `runs/` next to the cases
file, named by its UTC start time: a first line with the run's settings, then one line per case
with its outcome (`hit`, `miss`, `no_box` or `error`), the chosen box, tokens, cost and latency.
The calls also go to the usage log under one session per run, so `screenguide usage` shows what
the run spent. Real screenshots in `eval/images/` and the runs in `eval/runs/` stay out of git.

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
- **"Whole desktop" makes wrong arrows likely on several monitors.** The whole desktop goes in
  one image, shrunk to fit `image_long_edge`, so the text gets small and the boxes many. In
  testing on three monitors, the instruction named the right element on one monitor while the
  arrow pointed at a box on another. `screenguide run` and `guide` capture the whole desktop
  unless `monitor` or `--monitor` is set.
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
