# nVoice for Linux: what it is and how it's organized

## What is it?

nVoice is a **free, 100% offline push-to-talk dictation tool** for Linux.

1. Put your cursor in any text box: browser, editor, chat app, anything.
2. **Hold Ctrl+Alt** and speak. A small animated pill appears at the bottom of the screen and reacts to your voice.
3. **Release** the keys. A moment later, what you said is typed where your cursor is.

It runs quietly in the background with a **tray icon**, like Discord or Steam. Start it from the app launcher, the desktop icon, or by typing `nvoice` in a terminal. The terminal can be closed right away.

## Why does it exist?

Typing is slow, and most voice-typing tools send your voice to someone's server, need an account, or cost money every month. nVoice runs **entirely on your own computer**:

- **Private.** Your audio never leaves your machine. There's no internet connection after setup.
- **Free.** No account, subscription or API key.
- **Works everywhere.** It pastes into any app, on Wayland and on X11.

nVoice started as a Windows tool and was rebuilt from scratch for Linux.

## How it works

```
 hold Ctrl+Alt ──► microphone recording ──► Whisper (speech → text) ──► [optional] local LLM cleanup ──► paste at cursor
                   (pill shows your voice)   runs on your CPU            removes "um/uh", fixes punctuation
```

- **Speech-to-text** uses OpenAI's open-source **Whisper** model, run by **sherpa-onnx**, a fast offline speech engine.
- **Hotkeys** are read straight from the keyboard device (`/dev/input`). That's why they work in every window, even on Wayland, where normal apps can't listen to global keys.
- **Pasting** puts the text on your clipboard, then presses Ctrl+V through a virtual keyboard (`/dev/uinput`). Your previous clipboard content is put back afterwards.
- **Background + tray:** typing `nvoice` starts a detached copy of the program and returns to the prompt straight away. The tray icon (graphite when idle, blue while listening, indigo while transcribing) has a menu to turn LLM cleanup on or off, open the settings or the log, and quit.
- **LLM cleanup (optional)** can send the raw text to a local [Ollama](https://ollama.com) model that tidies it up. If Ollama isn't running, or you start with `nvoice --no-llm`, this step is skipped.

## Files in this project

| File / folder | What it's for | Should you edit it? |
|---|---|---|
| `nvoice.py` | **The whole program.** Everything nVoice does lives here (see the sections below). | Only if you want to change how it works |
| `config.yaml` | **Settings:** hotkey, mic options, which Whisper model to use, the paste key, LLM options. Each line has a comment explaining it. | ✅ Yes, this is the file to customize |
| `setup.sh` | **One-time installer.** Installs the system packages, gives your user keyboard access, creates the Python environment, downloads the Whisper model, and creates the `nvoice` command. Safe to run again. | No |
| `nvoice` | **Launcher script.** Starts `nvoice.py` with the project's own Python, so you can type `nvoice` from any folder. `setup.sh` links it into `~/.local/bin`. | No |
| `icons/nvoice.png` | The **app logo**, used in the launcher, on the desktop and in notifications. | Optional |
| `icons/nvoice-tray.png`, `nvoice-active.png`, `nvoice-processing.png` | The **tray icons**: graphite when idle, blue while listening, indigo while transcribing. | Optional |
| `requirements.txt` | The list of Python libraries nVoice needs. `setup.sh` installs them into `.venv`. | No |
| `models/` | The **Whisper speech models** (`small` = more accurate, `tiny` = faster). `setup.sh` downloads them. They're big, so they aren't shared through git. | No |
| `docs/preview.png` | The preview image shown at the top of the README. | No |
| `.venv/` | The **isolated Python environment**: a private copy of Python and the libraries, used only by nVoice. It doesn't touch your system Python. Delete it and re-run `setup.sh` to rebuild it. | No |
| `.gitignore` | Tells git not to upload `.venv/`, `models/` and `__pycache__/`, which are big or rebuilt automatically. | No |
| `README.md` | Short overview and usage cheat sheet. | |
| `install.md` | Step-by-step install guide for any Linux machine. | |
| `description.md` | This file. | |
| `LICENSE` | MIT license: anyone can use, change and share nVoice for free. | |

### Inside `nvoice.py`

The program is split into clearly labelled sections:

| Section | What it does |
|---|---|
| **UI Config / Floating Pill** (`VoicePill`) | Draws the animated pill: translucent frosted glass (on KDE it blurs whatever is behind it), with white-to-sky-blue bars that move with your voice. It shows an indigo wave while transcribing. You can drag it anywhere. |
| **Audio** (`Recorder`) | Records from your microphone. It keeps the last half-second *before* you press the keys, so your first word is never cut off. |
| **Speech-to-text** (`Transcriber`) | Loads Whisper and turns audio into text. It splits recordings longer than 30 s and drops non-speech labels like `[BLANK_AUDIO]`. |
| **Optional LLM cleanup** | Sends text to Ollama for tidying, if enabled and running. |
| **Paste** (`Paster`) | Copies the text to the clipboard (`wl-copy` on Wayland, `xclip` on X11), presses Ctrl+V through the virtual keyboard, then puts your old clipboard back. |
| **Global hotkey** (`HotkeyListener`) | Watches all keyboards for Ctrl+Alt. Pressing another key while holding them (like Ctrl+Alt+T) cancels the recording, so your normal shortcuts keep working. Ctrl+Alt+Q quits. |
| **Launcher** (top of the file) | Handles the command line (`--no-llm`, `--quit`, `-f`). Unless `-f` is given, it starts a detached background copy of nVoice, waits until it's up (or shows its error), and gives the terminal back. It loads none of the heavy libraries, so it's quick. |
| **Tray** (`Tray`) | The tray icon and its menu: status, LLM cleanup on/off, open settings, open log, quit. |
| **App** (`NVoice`, `main`) | Connects everything: loads the model in the background, handles start/stop/cancel, and makes sure only one copy of nVoice runs at a time. Shows a desktop notification if something goes wrong, since there's no terminal to print to. |

## What is `__pycache__`?

You may see a `__pycache__/` folder appear. **It's normal and harmless.**

When Python *imports* a `.py` file, it saves a pre-compiled version of it (a `.pyc` file) in `__pycache__/`, so the next import starts slightly faster. It wasn't there at first because running `nvoice` directly doesn't create it. It only appears when another script imports `nvoice.py`, as the tests during development did.

- It's created and updated automatically.
- You can delete it any time. Python simply recreates it when needed.
- It's in `.gitignore`, so it never gets shared.

## Changes made outside this folder

`setup.sh` changes a few things on your system. They're listed here so nothing is hidden (`install.md` explains how to undo them):

| Change | Why |
|---|---|
| Your user is added to the `input` group | So nVoice can see Ctrl+Alt in every app |
| `/etc/udev/rules.d/80-nvoice-uinput.rules` | Lets the `input` group use `/dev/uinput`, the virtual keyboard that presses Ctrl+V |
| `/etc/modules-load.d/nvoice-uinput.conf` | Loads the `uinput` kernel module at boot |
| `~/.local/bin/nvoice` (a shortcut) | So you can type `nvoice` from anywhere |
| `~/.local/share/applications/nvoice.desktop` | The **nVoice** entry in your app launcher (right-click it for "Quit nVoice") |
| `~/Desktop/nvoice.desktop` | The desktop icon (skip it with `NO_DESKTOP_ICON=1 ./setup.sh`) |
| `~/.local/state/nvoice/nvoice.log` | The log of the latest run (written by nVoice, not setup.sh) |

> **Security note:** being in the `input` group lets *any* program running as your user read the keyboard directly. Every global push-to-talk tool on Wayland needs this, but you should know about it.
