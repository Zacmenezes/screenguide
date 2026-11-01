# screenguide

Step-by-step guidance for any task in the Linux desktop interface, in the spirit of RuneLite's
Quest Helper plugin. The user says what they want to do; the app shows where to click and what
to fill in.

**Invariant:** the user performs every action. The app only captures the screen and shows the
way; code that clicks, types or acts on the user's behalf never enters this project.

## How to work here

1. Each session implements one GitHub issue. Read the whole issue and its dependencies.
2. Create the branch `<issue-number>-<slug>` from an up-to-date `main`.
3. Deliver only what the acceptance criteria ask for. Out-of-scope findings become draft issues
   for the maintainer to approve.
4. Open a PR with `Closes #<number>`. Every change reaches `main` through a PR.

Done means: every acceptance criterion is verified, tests and lint pass, the end-to-end flow
still works, and the "How to test manually" steps were run or declared pending in the PR.

Creating labels, milestones or issues on GitHub requires the maintainer's prior approval.

**Conventions:** Conventional Commits (`feat(capture): ...`). Python 3.11+, `src/` layout,
`pyproject.toml`, `ruff`, `pytest`, structured logging, MIT license. Code, commits, issues and
docs are in English; guidance shown to the user follows the language of the typed goal.

## Architecture

Method: **tracer bullet**. A thin slice crosses every layer; then each layer evolves on its own
behind an interface, starting with a single implementation. Every change keeps the flow working
end to end. Add a second implementation when an issue asks for it, and not before.

| Layer | Label | Responsibility |
| --- | --- | --- |
| `ScreenCapturer` | `layer:capture` | Returns the screen image, always on command |
| `ElementDetector` | `layer:detection` | Finds elements and their bboxes (OCR at first) |
| `GroundingStrategy` | `layer:grounding` | Set-of-mark: numbers the boxes and maps the chosen number back to a bbox |
| `ModelProvider` | `layer:model` | Calls the model; returns the reply and the usage the API reports. Anthropic first; later Gemini, OpenAI, Ollama |
| `GuideSession` | `layer:session` | Holds goal and history; drives the loop until `done` |
| PySide6 panel | `layer:ui` | Shows instruction and crop; takes user input |
| `UsageTracker` | `layer:usage` | Records tokens, cost, latency; enforces the budget |

Flow: global shortcut → panel → goal → capture → detection → marking → model → crop with arrow
and instruction → user acts and presses "next" → recapture, until `done`.

The model always answers with a box **number**. Every coordinate comes from the detector.

**The panel shows the target inside its own crop**, so the app uses only coordinates of the
captured image. Global screen coordinates stay out of the MVP: Wayland does not give them to
clients.

### Model response contract

```json
{"element_id": 12, "target_label": "Layout",
 "instruction": "Click the Layout tab in the top menu, between Design and References.",
 "done": false, "confidence": 0.8}
```

- `instruction` always combines the element's name with a position reference, so the step stays
  useful if the arrow is slightly off.
- `element_id` is `null` when the target has no box (an icon with no text, for example) and when
  `done` is `true`. The panel then shows only the instruction.
- Invalid JSON: one retry, with its usage also recorded; then a clear error in the panel.
- A question in "Update flow / Ask about this step" goes out with the session context and comes
  back as text (`{"answer": "..."}`) or as a corrected step in the format above.

## Interface

The list below is the visual acceptance criterion. The screenshot it was written from lives at
`local/ui-reference.png`, git-ignored and present only on the maintainer's machine; open it when
it exists. It is a layout and behaviour reference only: the pictured product's logo, name and
visual identity stay out. Each `layer:ui` issue states which of these parts it delivers:

- **Floating bar:** pill at top centre with "Guide me +", "Hide" and a "⋮" menu.
- **Panel:** dark and semi-transparent, showing the app behind it; close (×), goal and
  "New Task" in the header.
- **Highlighted instruction:** short text with `target_label` in colour and a position reference.
- **Crop with arrow:** enlarged region of the screenshot with an arrow on the element; a
  "Thinking" state while the model works.
- **Footer:** "Next step", the "Update flow / Ask about this step" field and a send button.
- **Metrics (our addition):** discreet line with tokens and cost for the step and the session.
- **Shortcuts (our addition):** keys for "Next step" and for showing/hiding the panel.

M1 delivers the panel, instruction, crop with arrow and "Next step". The rest goes to M3.

## Metrics

- Tokens come from the API. A field the API does not report stays `null` rather than estimated.
- Also record image resolution, latency and estimated cost.
- Prices live only in `~/.config/screenguide/pricing.toml`, with an example versioned in the
  repo. Model missing from the table → unknown cost. Ollama → cost 0.
- Log in `~/.local/share/screenguide/usage`; `screenguide usage` summarises by day, session,
  provider and model.
- `max_usd_per_session` is optional and asks for confirmation when the limit is reached.

## Evaluation

`eval/` uses the same pipeline as the app. A hit: the chosen element intersects the correct bbox.
It compares hit rate, tokens, cost, latency and cost per hit, by provider, model and strategy.
Dataset in `cases.jsonl` (`{image, goal, history, correct_bbox}`).

## Configuration and privacy

- Configuration: `~/.config/screenguide/config.toml` and `SCREENGUIDE_*` variables.
- Capture only on command, one per step.
- Screenshots stay in memory by default; saving to disk is an explicit option.
- The panel indicates when a screenshot goes to an external provider.
- API keys live in config or the environment. Real dataset screenshots stay in `.gitignore`.

## Platform

Primary target: **Ubuntu 26.04, GNOME 50, Wayland**. GNOME offers neither layer-shell nor
wlr-screencopy (`grim` fails), so each need has its own path:

| Need | GNOME Wayland (hypothesis to validate) | X11 / KDE / wlroots |
| --- | --- | --- |
| Capture | `org.freedesktop.portal.Screenshot` portal | Portal, or X11/`grim` |
| Always on top | Panel through XWayland (`QT_QPA_PLATFORM=xcb`) | Window hint or layer-shell |
| Global shortcut | `GlobalShortcuts` portal, or a GNOME shortcut running `screenguide toggle` | Portal or system shortcut |

Hide the panel before capturing, so it stays out of the screenshot.

## Out of scope for the MVP

Live full-screen overlay, automatic detection of a completed step, community guides, Windows and
macOS.
