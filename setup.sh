#!/usr/bin/env bash
# One-time setup for nVoice. Safe to re-run.
# Supports Fedora, Ubuntu/Debian/Mint/Pop!_OS, Arch/Manjaro/EndeavourOS, openSUSE.
#
#   ./setup.sh            installs the "small" model (best accuracy, ~640 MB download)
#   ./setup.sh --tiny     installs the "tiny" model instead (faster, ~120 MB download)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

MODEL=small
[[ ${1:-} == "--tiny" ]] && MODEL=tiny

echo "==> System packages"
if [[ ${SKIP_PACKAGES:-0} == 1 ]]; then
    echo "    skipped (SKIP_PACKAGES=1)"
elif command -v dnf &>/dev/null; then
    sudo dnf install -y portaudio wl-clipboard xclip xcb-util-cursor python3 curl bzip2
elif command -v apt-get &>/dev/null; then
    sudo apt-get update
    sudo apt-get install -y libportaudio2 wl-clipboard xclip libxcb-cursor0 python3 python3-venv curl bzip2
elif command -v pacman &>/dev/null; then
    sudo pacman -S --needed --noconfirm portaudio wl-clipboard xclip xcb-util-cursor python curl bzip2
elif command -v zypper &>/dev/null; then
    sudo zypper install -y libportaudio2 wl-clipboard xclip libxcb-cursor0 python3 curl bzip2
else
    echo "!! Unknown distro. Install these yourself, then re-run with: SKIP_PACKAGES=1 ./setup.sh"
    echo "   portaudio, wl-clipboard, xclip, xcb-util-cursor, python3 (with venv), curl, bzip2"
    exit 1
fi

echo "==> Keyboard access (read hotkeys from /dev/input, send paste via /dev/uinput)"
NEED_RELOGIN=0
if ! id -nG "$USER" | grep -qw input; then
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
fi
RULE=/etc/udev/rules.d/80-nvoice-uinput.rules
if [[ ! -f $RULE ]]; then
    echo 'KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee "$RULE" >/dev/null
    echo uinput | sudo tee /etc/modules-load.d/nvoice-uinput.conf >/dev/null
    sudo modprobe uinput
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput
fi

echo "==> Python virtualenv (.venv)"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Whisper model ($MODEL.en)"
MODEL_DIR="models/$MODEL"
if [[ -f $MODEL_DIR/$MODEL.en-encoder.int8.onnx ]]; then
    echo "    already downloaded"
else
    mkdir -p "$MODEL_DIR"
    NAME="sherpa-onnx-whisper-$MODEL.en"
    curl -L --fail --progress-bar \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$NAME.tar.bz2" |
        tar -xjf - -C "$MODEL_DIR" --strip-components=1 \
            "$NAME/$MODEL.en-encoder.int8.onnx" \
            "$NAME/$MODEL.en-decoder.int8.onnx" \
            "$NAME/$MODEL.en-tokens.txt"
fi
if [[ $MODEL == tiny ]]; then
    sed -i 's#models/small/small\.en#models/tiny/tiny.en#' config.yaml
    echo "    config.yaml now points at the tiny model"
fi

echo "==> 'nvoice' command"
mkdir -p "$HOME/.local/bin"
chmod +x "$DIR/nvoice"
ln -sf "$DIR/nvoice" "$HOME/.local/bin/nvoice"

echo "==> App launcher entry (+ desktop icon)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/nvoice.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=nVoice
GenericName=Voice Dictation
Comment=Offline push-to-talk dictation: hold Ctrl+Alt to talk
Exec=$DIR/nvoice
Icon=$DIR/icons/nvoice.png
Terminal=false
StartupNotify=false
Categories=Utility;Accessibility;
Keywords=voice;dictation;speech;whisper;transcribe;
Actions=quit;

[Desktop Action quit]
Name=Quit nVoice
Exec=$DIR/nvoice --quit
DESKTOP
command -v update-desktop-database &>/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [[ -d $DESKTOP_DIR && ${NO_DESKTOP_ICON:-0} != 1 ]]; then
    cp "$APPS/nvoice.desktop" "$DESKTOP_DIR/nvoice.desktop"
    chmod +x "$DESKTOP_DIR/nvoice.desktop"
    command -v gio &>/dev/null && gio set "$DESKTOP_DIR/nvoice.desktop" metadata::trusted true 2>/dev/null || true
fi

echo
echo "Done."
if [[ $NEED_RELOGIN == 1 ]]; then
    echo "!! You were added to the 'input' group. REBOOT (or log out and back in) before running nvoice."
else
    echo "Start nVoice from your app menu or desktop icon, or run: nvoice   (or: nvoice --no-llm)"
fi
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "!! ~/.local/bin is not on your PATH. Run nvoice as: $DIR/nvoice"
fi
