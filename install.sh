#!/usr/bin/env bash
# padmode installer.
#
# Two modes, on purpose (CLAUDE.md §2):
#
#   ./install.sh              prompt before each change, explaining what and why
#   ./install.sh --print-steps  change nothing, print the commands to run by hand
#
# Nothing here needs root except the udev rule and the group change, and both
# are asked for separately so you can decline either one.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/padmode"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/padmode"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/padmode.service"
HEROIC_PATH_UNIT="$UNIT_DIR/padmode-heroic.path"
HEROIC_SERVICE_UNIT="$UNIT_DIR/padmode-heroic.service"
UDEV_RULE="/etc/udev/rules.d/99-padmode.rules"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APPS_DIR/io.github.KkelebekK.PadMode.Settings.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
DESKTOP_SHORTCUT="$DESKTOP_DIR/padmode-settings.desktop"
MIN_PYTHON="3.11"

# Heroic keeps its library cache in one of two places, depending on whether it
# was installed natively or as a Flatpak. Prints the store_cache directory, or
# nothing if Heroic is not installed.
heroic_cache() {
    local candidate
    for candidate in \
        "$HOME/.config/heroic" \
        "$HOME/.var/app/com.heroicgameslauncher.hgl/config/heroic"
    do
        if [ -d "$candidate/store_cache" ]; then
            printf '%s\n' "$candidate/store_cache"
            return 0
        fi
    done
    return 1
}

ASSUME_YES=0
PRINT_ONLY=0

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
usage: install.sh [options]

  --print-steps   Print the manual commands and exit. Changes nothing.
  --yes, -y       Don't prompt; accept every step.
  --uninstall     Remove padmode (leaves your config alone).
  --help, -h      This message.
EOF
}

# Ask before doing anything the user might not want. Returns 1 on "no".
confirm() {
    local prompt="$1"
    [ "$ASSUME_YES" = 1 ] && return 0
    local reply
    read -r -p "$prompt [Y/n] " reply </dev/tty || return 1
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

check_prereqs() {
    command -v python3 >/dev/null || die "python3 not found"
    python3 - "$MIN_PYTHON" <<'EOF' || die "padmode needs Python $MIN_PYTHON or newer"
import sys
want = tuple(int(p) for p in sys.argv[1].split("."))
sys.exit(0 if sys.version_info[:2] >= want else 1)
EOF

    if ! python3 -c "import evdev" 2>/dev/null; then
        warn "python-evdev is not installed. padmode cannot run without it."
        warn "  Arch     : sudo pacman -S python-evdev"
        warn "  Debian   : sudo apt install python3-evdev"
        warn "  Fedora   : sudo dnf install python3-evdev"
        confirm "Continue anyway?" || exit 1
    fi

    for tool in playerctl wpctl; do
        command -v "$tool" >/dev/null || warn \
            "optional: '$tool' not found -- the bindings that use it will be disabled"
    done

    # Which of these is the right one depends on the session, and padmode
    # already knows the rules -- so check only that *some* window tool exists
    # and point at 'padmode windows' for the specifics.
    # Only unlock needs it, and unlock is off unless asked for -- but finding
    # out at the lock screen would be the worst moment to.
    command -v gdbus >/dev/null || warn \
        "optional: 'gdbus' not found -- unlock with the pad will be disabled"

    python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Adw' 2>/dev/null || warn \
        "optional: GTK 4 + libadwaita (python-gobject, libadwaita) not found -- the settings window will not open"

    if ! command -v kdotool >/dev/null && ! command -v xdotool >/dev/null; then
        warn "optional: no window tool found (kdotool on KDE Wayland, xdotool"
        warn "  on X11) -- app overlays will be disabled. Nothing else changes."
    fi
}

print_steps() {
    cat <<EOF
$(bold "Manual installation")

1. Install the package and a launcher:

   mkdir -p "$LIB_DIR" "$BIN_DIR"
   cp -r "$SRC_DIR/padmode" "$LIB_DIR/"

   cat > "$BIN" <<'LAUNCHER'
   #!/bin/sh
   PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
   export PYTHONPATH
   exec python3 -m padmode "\$@"
   LAUNCHER
   chmod +x "$BIN"

   mkdir -p "$APPS_DIR"
   sed "s|@PADMODE@|$BIN|" packaging/io.github.KkelebekK.PadMode.Settings.desktop.in > "$DESKTOP_FILE"

   (A launcher rather than pip: most distros now ship a PEP 668
   externally-managed Python where 'pip install --user' refuses to run.
   This approach uses your distro's python-evdev and works everywhere.)

2. Allow access to /dev/uinput (needed from v0.2; doing it now avoids a second
   logout later):

   sudo cp "$SRC_DIR/packaging/99-padmode.rules" "$UDEV_RULE"
   sudo udevadm control --reload-rules
   sudo udevadm trigger

3. Join the 'input' group so padmode can read your gamepad:

   sudo usermod -aG input "$USER"
   # then log out and back in -- group changes need a fresh session

4. Install and start the service:

   mkdir -p "$UNIT_DIR"
   sed 's|@PADMODE@|$BIN|' "$SRC_DIR/packaging/padmode.service.in" > "$UNIT"
   systemctl --user daemon-reload
   systemctl --user enable --now padmode.service

5. Optional -- regenerate frontend metadata when your Heroic library changes:

   sed 's|@HEROIC_CACHE@|$HOME/.config/heroic/store_cache|' \\
       "$SRC_DIR/packaging/padmode-heroic.path.in" > "$HEROIC_PATH_UNIT"
   sed 's|@PADMODE@|$BIN|' \\
       "$SRC_DIR/packaging/padmode-heroic.service.in" > "$HEROIC_SERVICE_UNIT"
   systemctl --user daemon-reload
   systemctl --user enable --now padmode-heroic.path

   (Flatpak Heroic keeps its cache in
   \$HOME/.var/app/com.heroicgameslauncher.hgl/config/heroic/store_cache
   instead. Or just run 'padmode metadata heroic' by hand.)

6. Optional -- a desktop shortcut to the settings window:

   cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
   chmod +x "$DESKTOP_SHORTCUT"

7. Check it:

   padmode devices
   systemctl --user status padmode.service
EOF
}

install_package() {
    bold "1. Installing padmode to $LIB_DIR"
    mkdir -p "$LIB_DIR" "$BIN_DIR"
    rm -rf "${LIB_DIR:?}/padmode"
    cp -r "$SRC_DIR/padmode" "$LIB_DIR/"
    find "$LIB_DIR/padmode" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

    # A launcher rather than pip: most distros now ship a PEP 668
    # externally-managed Python, where 'pip install --user' simply refuses.
    # This works everywhere and uses the distro's python-evdev.
    cat > "$BIN" <<EOF
#!/bin/sh
# Generated by padmode's install.sh
PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -m padmode "\$@"
EOF
    chmod +x "$BIN"
    echo "   installed launcher: $BIN"

    # The settings window's menu entry. Harmless without GTK: the window
    # says what it needs and exits.
    mkdir -p "$APPS_DIR"
    sed "s|@PADMODE@|$BIN|" \
        "$SRC_DIR/packaging/io.github.KkelebekK.PadMode.Settings.desktop.in" > "$DESKTOP_FILE"
    echo "   menu entry: $DESKTOP_FILE"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "   $BIN_DIR is not on your PATH -- add it to use 'padmode' directly" ;;
    esac
}

install_udev() {
    bold "2. udev rule for /dev/uinput"
    echo "   Needed from v0.2 for desktop mode (gamepad as mouse). Installing it"
    echo "   now means you only have to log out once. Requires sudo."
    echo "   Writes: $UDEV_RULE"
    if confirm "   Install the udev rule?"; then
        sudo cp "$SRC_DIR/packaging/99-padmode.rules" "$UDEV_RULE"
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        echo "   done"
    else
        echo "   skipped -- desktop mode will not work until this is installed"
    fi
}

install_group() {
    bold "3. Group membership"
    if id -nG | tr ' ' '\n' | grep -qx input; then
        echo "   already in the 'input' group, nothing to do"
        return
    fi
    echo "   padmode reads /dev/input/event*, which requires the 'input' group."
    echo "   Runs: sudo usermod -aG input $USER"
    if confirm "   Add $USER to the 'input' group?"; then
        sudo usermod -aG input "$USER"
        warn "   done -- you must log out and back in for this to take effect"
    else
        echo "   skipped -- padmode will not see your gamepad until you do this"
    fi
}

install_service() {
    bold "4. systemd user service"
    echo "   Installs $UNIT and starts it at login."
    echo "   This is a *user* service. Nothing runs as root."
    if confirm "   Install and enable the service?"; then
        mkdir -p "$UNIT_DIR"
        sed "s|@PADMODE@|$BIN|" "$SRC_DIR/packaging/padmode.service.in" > "$UNIT"
        systemctl --user daemon-reload
        systemctl --user enable --now padmode.service
        echo "   done"
    else
        echo "   skipped -- run 'padmode run' by hand to try it"
    fi
}

install_heroic() {
    local cache
    if ! cache="$(heroic_cache)"; then
        return 0   # Heroic isn't installed; nothing to offer
    fi
    bold "5. Heroic library metadata (optional)"
    echo "   Found Heroic: $cache"
    echo "   padmode can regenerate your frontend's metadata whenever you"
    echo "   install or remove a game, by watching that directory."
    echo "   Installs $HEROIC_PATH_UNIT and .service"
    echo "   Writes metadata and art to ~/Games/pegasus-heroic"
    if confirm "   Install the metadata watcher?"; then
        mkdir -p "$UNIT_DIR"
        sed "s|@HEROIC_CACHE@|$cache|" \
            "$SRC_DIR/packaging/padmode-heroic.path.in" > "$HEROIC_PATH_UNIT"
        sed "s|@PADMODE@|$BIN|" \
            "$SRC_DIR/packaging/padmode-heroic.service.in" > "$HEROIC_SERVICE_UNIT"
        systemctl --user daemon-reload
        systemctl --user enable --now padmode-heroic.path
        echo "   done -- run it now with: padmode metadata heroic"
    else
        echo "   skipped -- run 'padmode metadata heroic' by hand when you like"
    fi
}

install_shortcut() {
    bold "6. Desktop shortcut (optional)"
    echo "   The settings window is in your application menu and its search"
    echo "   already, and Guide + R3 opens it. This also puts an icon on the"
    echo "   desktop. Writes: $DESKTOP_SHORTCUT"
    if [ ! -f "$DESKTOP_FILE" ]; then
        echo "   skipped -- the menu entry from step 1 is missing"
        return
    fi
    if confirm "   Add a desktop shortcut?"; then
        mkdir -p "$DESKTOP_DIR"
        cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
        # Desktops only launch shortcuts that are marked executable.
        chmod +x "$DESKTOP_SHORTCUT"
        echo "   done"
    else
        echo "   skipped"
    fi
}

uninstall() {
    bold "Uninstalling padmode"
    systemctl --user disable --now padmode.service 2>/dev/null || true
    systemctl --user disable --now padmode-heroic.path 2>/dev/null || true
    rm -f "$UNIT" "$BIN" "$HEROIC_PATH_UNIT" "$HEROIC_SERVICE_UNIT" "$DESKTOP_FILE" \
        "$DESKTOP_SHORTCUT"
    rm -rf "${LIB_DIR:?}/padmode"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed the service, launcher and package"
    echo
    echo "Left alone on purpose:"
    echo "  - your config at ${XDG_CONFIG_HOME:-$HOME/.config}/padmode/"
    echo "  - the udev rule ($UDEV_RULE) -- remove with: sudo rm $UDEV_RULE"
    echo "  - your 'input' group membership -- other tools may rely on it"
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --print-steps) PRINT_ONLY=1 ;;
            -y|--yes) ASSUME_YES=1 ;;
            --uninstall) uninstall; exit 0 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done

    if [ "$PRINT_ONLY" = 1 ]; then
        print_steps
        exit 0
    fi

    bold "padmode installer"
    echo "Nothing is changed without asking. To see the commands instead of"
    echo "running them:  ./install.sh --print-steps"
    echo
    check_prereqs
    install_package
    install_udev
    install_group
    install_service
    install_heroic
    install_shortcut

    echo
    bold "Done."
    echo "  padmode devices   # confirm your gamepad is detected"
    echo "  padmode config    # show the effective configuration"
    echo "  padmode actions   # list what you can bind"
    echo "  padmode windows   # whether app overlays can work on this desktop"
    echo
    echo "Hold GUIDE and press RB/LB/A/X/Y or d-pad up/down."
    echo "Logs: journalctl --user -u padmode -f"
}

main "$@"
