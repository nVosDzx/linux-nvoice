# Installing nVoice on Linux

Setup takes **a few minutes plus one reboot**. Most of that is downloading the speech model (~640 MB) and the Python libraries (~150 MB), so it depends on your internet speed.

## Before you start

| You need | Details |
|---|---|
| A Linux distro | **Fedora**, **Ubuntu / Debian / Mint / Pop!_OS**, **Arch / Manjaro / EndeavourOS**, or **openSUSE**. Others work too if you install the packages yourself (see [Other distros](#other-distros)). |
| Desktop | Any. Works on Wayland (KDE, GNOME…) and X11. The tray icon needs a system tray (built into KDE, XFCE, Cinnamon and Ubuntu; plain GNOME needs an extension). The pill is see-through glass everywhere; the frosted blur behind it is KDE Plasma only. Tested on Fedora 44 with KDE Plasma (Wayland). |
| CPU | 64-bit Intel/AMD or ARM. No graphics card needed. |
| Python | 3.10 or newer. Already installed on almost every distro. Check with `python3 --version`. |
| Disk | ~1 GB |
| A microphone | 🎤 |

## Install

### 1. Get the code

With git:
```bash
git clone https://github.com/nVosDzx/linux-nvoice.git
cd linux-nvoice
```
Or download the ZIP from [the project page](https://github.com/nVosDzx/linux-nvoice) (green **Code** button → **Download ZIP**), extract it, open a terminal inside the folder, and run `bash setup.sh` (ZIP downloads can lose the "executable" flag).

### 2. Run the installer

```bash
./setup.sh
```
It asks for your password once, to install system packages and set up keyboard access.

Want it faster on an older computer? Use the smaller model instead (less accurate, ~120 MB):
```bash
./setup.sh --tiny
```

### 3. Reboot

This is required the first time. It gives your user the keyboard permission that `setup.sh` just added. Logging out and back in sometimes works, but a reboot is the sure way.

### 4. Use it

Open **nVoice** from your app launcher or the desktop icon, or type `nvoice` in a terminal. It runs in the background with a tray icon 🎙️, so you can close the terminal.

Wait a couple of seconds for the "Loading model…" pill to disappear. Then click into any text box, **hold Ctrl+Alt**, speak, and **release**. 🎉

| Action | Keys |
|---|---|
| Record | hold **Ctrl+Alt** |
| Stop and paste | release |
| Cancel | press any other key while holding |
| Quit | tray icon → **Quit nVoice**, **Ctrl+Alt+Q**, or `nvoice --quit` |

Other commands: `nvoice --no-llm` skips the optional LLM cleanup. `nvoice -f` stays in the terminal and shows the log live, which is handy if something isn't working.

## Optional: LLM cleanup

nVoice can tidy up your text by removing "um"s and fixing punctuation, using a free local AI model:

1. Install [Ollama](https://ollama.com/download/linux): `curl -fsSL https://ollama.com/install.sh | sh`
2. Download the model: `ollama pull qwen3:4b-instruct`
3. Run `nvoice`. It detects Ollama automatically.

## Customizing

Open `config.yaml` in any text editor. Every setting has a comment. The most useful ones:

- `paste.combo: "ctrl+shift+v"` if you mostly dictate into a **terminal**, since terminals paste with Ctrl+Shift+V
- `hotkey.hold` to use a different key combination
- `stt.*` to switch between the `small` and `tiny` models
- `ui.glass_opacity` to make the pill more see-through (0) or more solid (1)

## Troubleshooting

| Problem | Fix |
|---|---|
| `no readable keyboards in /dev/input` | You haven't rebooted since running `setup.sh`. Check with `id -nG`, which should list `input`. |
| `can't open /dev/uinput`: text is copied but not pasted | Same fix: reboot. |
| Nothing is pasted in the terminal | Set `paste.combo: "ctrl+shift+v"` in `config.yaml`. |
| `nvoice: command not found` | `~/.local/bin` isn't on your PATH. Run `./nvoice` from the project folder, or add `export PATH="$HOME/.local/bin:$PATH"` to your `~/.bashrc`. |
| Nothing gets transcribed | Check your microphone in your system's sound settings. nVoice uses the default input device. |
| `nVoice is already running` | It's already running in the background. Look for the tray icon, or stop it with `nvoice --quit`. |
| No tray icon (GNOME) | Plain GNOME has no tray. Install the "AppIndicator and KStatusNotifierItem Support" extension (Ubuntu already includes it). nVoice still works without it; quit with Ctrl+Alt+Q. |
| Something fails silently | Check the log: `~/.local/state/nvoice/nvoice.log`, or run `nvoice -f` to watch it live. |

## Other distros

Install these with your package manager, then run `SKIP_PACKAGES=1 ./setup.sh`:

`portaudio` · `wl-clipboard` · `xclip` · `xcb-util-cursor` (sometimes called `libxcb-cursor0`) · `python3` with `venv` · `curl` · `bzip2`

## Uninstall

```bash
nvoice --quit                          # stop it first
rm -rf ~/path/to/linux-nvoice          # the project folder (includes .venv and models)
rm ~/.local/bin/nvoice ~/.local/share/applications/nvoice.desktop ~/Desktop/nvoice.desktop
rm -rf ~/.local/state/nvoice
sudo rm /etc/udev/rules.d/80-nvoice-uinput.rules /etc/modules-load.d/nvoice-uinput.conf
sudo gpasswd -d "$USER" input          # optional: remove keyboard access (takes effect after reboot)
```
