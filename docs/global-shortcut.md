# Global shortcut

`screenguide toggle` shows the panel, or hides it when it is showing; the floating bar above it
goes and comes with it. If no panel is running, that launch becomes the panel. Every later launch,
`screenguide panel` included, hands its command to the running panel and exits. Hiding the panel
keeps the goal and the guide where they were; showing it again puts the cursor in the goal field
when the panel is asking for one. ⋮ → Quit ends the panel.

`screenguide next` asks the running panel for the next step, as **Next step** does, but from the
app you are guiding: the focus stays where it is, so a menu you just opened is still open when the
screen is captured. While the panel is already working on a step, or has no step to follow (no
goal yet, or done), the command is ignored. With no panel running it prints an error and starts
none, since there is no goal to follow. A step it asks for while the panel is folded or hidden
waits there, as one from the button would.

Bind each command to a key in GNOME so that it runs from any app.

## GNOME custom shortcut

In Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts, add one:

- **Name:** `screenguide`
- **Command:** the full path to `screenguide`, followed by `toggle`. GNOME does not read your
  shell profile, so a virtualenv or `~/.local/bin` may not be on its `PATH`. `command -v
  screenguide` prints the path.
- **Shortcut:** for example Ctrl+Alt+G.

Then add a second one the same way, with the name `screenguide next`, the command ending in `next`
instead of `toggle`, and a shortcut such as Ctrl+Alt+N.

Or from a terminal, with the `screenguide` you want on your `PATH`:

```sh
key=org.gnome.settings-daemon.plugins.media-keys
base=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
bind() {  # bind <slot> <name> <command> <keys>
  path="$base/$1/"
  gsettings set "$key.custom-keybinding:$path" name "$2"
  gsettings set "$key.custom-keybinding:$path" command "$3"
  gsettings set "$key.custom-keybinding:$path" binding "$4"
  # Add the shortcut to the list GNOME reads, keeping any you already have.
  current=$(gsettings get "$key" custom-keybindings)
  case "$current" in
    *"$path"*) ;;
    "@as []") gsettings set "$key" custom-keybindings "['$path']" ;;
    *) gsettings set "$key" custom-keybindings "${current%]}, '$path']" ;;
  esac
}
if bin=$(command -v screenguide); then
  bind screenguide 'screenguide' "$bin toggle" '<Control><Alt>g'
  bind screenguide-next 'screenguide next' "$bin next" '<Control><Alt>n'
else
  echo "screenguide is not on PATH" >&2
fi
```

A panel started by the shortcut does not see variables set in your shell profile either. Keep the
Anthropic credentials where a GNOME process can read them: `ant auth login`, or
`ANTHROPIC_API_KEY=...` in `~/.config/environment.d/screenguide.conf`, loaded as the
README describes.
The same goes for `SCREENGUIDE_*` settings, which also fit in `~/.config/screenguide/config.toml`.

## Why not the GlobalShortcuts portal

On Ubuntu 26.04 with GNOME 50 (xdg-desktop-portal 1.21.1, xdg-desktop-portal-gnome 50.0),
`org.freedesktop.portal.GlobalShortcuts` version 1 works, with two limits:

- `CreateSession` from an unsandboxed process fails with `An app id is required`. The app first
  has to call `org.freedesktop.host.portal.Registry.Register`, which only accepts an app id with
  an installed `.desktop` file whose `Exec` resolves.
- After that, `BindShortcuts` shows GNOME's dialog and the bound key sends `Activated` from any
  window, but only while the process that holds the portal session runs. The portal cannot start
  the panel.

The custom shortcut has neither limit, so it is the path for now.
