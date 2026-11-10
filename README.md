# herdr-tui

A terminal front-end for herdr that replaces the removed TUI, so you can follow agents and manage spaces and tabs from a phone over SSH.

## How it works

`herdr-tui` uses two interfaces:

- **`herdr-app ls` and `herdr-app do`**: the Herdr window's own structure and commands, the same ones its keys and menus use. Spaces and tabs appear in the window's order, and every change you make shows up on the desktop at once.
- **`hc` (herdr-core)**: the core that runs every terminal and agent. It handles peek, send, interrupt and attach.

If the Herdr app isn't running, the list falls back to `hc` alone and structure changes are disabled. Press `o` to open the app; this needs you to be logged in on the Mac.

## Install

It needs only the system `python3` (3.9 or later); there are no dependencies.

```sh
git clone https://github.com/<you>/herdr-tui-support.git ~/herdr-tui
ln -sf ~/herdr-tui/herdr-tui.py ~/.local/bin/herdr-tui
```

## Using it from your phone

1. Turn on SSH on the Mac: System Settings → General → Sharing → Remote Login.
2. To reach the Mac from outside your home network, install [Tailscale](https://tailscale.com) on both the Mac and the phone.
3. Connect with an SSH app (Termius, Blink or Prompt), then run `herdr-tui`.

### Open it automatically on SSH login

Add this block to the end of `~/.zshrc`:

```zsh
# herdr-tui: open the Herdr dashboard on interactive SSH logins (q drops you to this shell).
# Skipped for scp/rsync/`ssh host cmd`, nested shells, tmux, and terminals inside herdr.
# Turn off: touch ~/.config/herdr-tui-off
if [[ -n $SSH_CONNECTION && -o interactive && -t 0 && -t 1 && $SHLVL -le 1 \
      && -z $HC_TERMINAL_ID && -z $TMUX && -z $HERDR_TUI_RUNNING \
      && ! -e ~/.config/herdr-tui-off && -x ~/.local/bin/herdr-tui ]]; then
  HERDR_TUI_RUNNING=1 ~/.local/bin/herdr-tui
fi
```

It opens only for interactive SSH logins; press `q` to drop to your normal shell. It does not run in these cases:

- `scp` or `rsync`
- `ssh host cmd`
- nested shells or tmux
- terminals inside herdr (`$HC_TERMINAL_ID` is set)

To turn it off: `touch ~/.config/herdr-tui-off`

## Keys

Press `?` in the app for the full key list.

**Agents**

| Key | Action |
|---|---|
| `⏎` | Attach (`ctrl+\` comes back) |
| `p` | Peek |
| `s` | Send |
| `i` | Interrupt |
| `!` | Jump to the next blocked or done agent |

**Structure**

On a space row, these keys act on the space; on an agent row, they act on that agent's tab.

| Key | Action |
|---|---|
| `c` | New space: a folder (Tab completes), then claude, a shell or another command, then an optional first message |
| `n` | New tab in this space |
| `R` | Rename |
| `J` / `K` | Move down / up |
| `X` | Close (asks first) |
| `⏎` on a space | Fold or unfold it |

**Peek**

| Key | Action |
|---|---|
| `1`–`9` | Pick a dialog option |
| `⏎` | Press Enter |
| `x` | Press Esc |
| `s` | Send |
| `i` | Interrupt |
| `a` | Attach |
| `q` | Back |

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `HERDR_TUI_REFRESH` | `2` | Seconds between refreshes |
| `HC` | found on PATH, else `~/.local/bin/` | Path to `hc` |
| `HERDR_APP` | found on PATH, else `~/.local/bin/` | Path to `herdr-app` |
| `HERDR_APP_BUNDLE` | `~/Applications/Herdr.app` | The app that `o` opens |

## License

MIT. See [LICENSE](LICENSE).
