# Library metadata generators

Reference for `padmode metadata`. These are **not** part of the daemon and must
never be imported by it (CLAUDE.md §2, §7). They are batch tools that read a
game library and write frontend metadata.

Heroic is built (v0.4). Steam is specified below and not built.

---

## Shape

Both generators do the same four things:

1. Read an installed-games list from a launcher's own cache.
2. Filter it (skip runtimes, tools, and anything the user has ignored).
3. Download or locate cover art and background art.
4. Write a frontend metadata file.

Properties the working Heroic version had, all kept:

- **Idempotent.** Re-running skips art that is already downloaded, so it is
  cheap to run on every library change. Measured on the real library here: a
  second run over three installed games takes 0.1 s and fetches nothing.
- **Validating.** Confirm downloaded bytes are actually an image before saving.
  Launcher CDNs return HTML error pages with a 200 status — and an error page
  saved as `.jpg` is a broken cover *forever*, because every later run sees a
  file of non-zero size and skips it.
- **Retrying.** Art downloads fail transiently; retry before giving up on an
  entry rather than aborting the run.
- **Overridable paths.** Input and output locations come from flags or the
  environment, never hardcoded.

## Heroic (Epic / GOG / Amazon)

```bash
padmode metadata heroic [--heroic-dir PATH] [--out PATH]
                        [--collection NAME] [--dry-run]
```

Flags win, then `$HEROIC_CONFIG_DIR` / `$PEGASUS_HEROIC_OUT`, then the
defaults (autodetect, and `~/Games/pegasus-heroic`). `--dry-run` prints the
metadata and touches nothing — the fastest way to see what a change does.

**Source:** `~/.config/heroic/store_cache/*_library.json`, or
`~/.var/app/com.heroicgameslauncher.hgl/config/heroic/...` for the Flatpak.
Three files, one per store. Keep entries where `is_installed` is true. Heroic
has written both a bare list and a dict wrapping one under `library`; both are
accepted. A store you do not use is present but empty, and one unreadable file
is skipped with a warning rather than costing you the other two libraries.

**Art fields:** `art_square` is the cover (portrait box art); `art_cover` is
the background (landscape hero image). The names read backwards, so this was
confirmed against the real CDN rather than assumed: the files that came down
were 1200x1600 and 2560x1440 respectively.

**Launch line:** Heroic registers a URL handler, so entries launch through it
rather than through an executable path:

```
xdg-open "heroic://launch?appName=<app_id>&runner=<legendary|gog|nile>"
```

The runner value differs per store: `legendary` for Epic, `gog` for GOG, `nile`
for Amazon. It is present in the library JSON.

**Refusing to write an empty file.** Zero installed games is an error and
writes nothing. Overwriting good metadata with an empty collection because
Heroic happened to be mid-write is worse than doing nothing.

**Staying current:** `packaging/padmode-heroic.path` watches the three
`*_library.json` files and triggers the generator whenever a game is installed
or removed. `install.sh` offers it, but only if it finds Heroic. The triggered
service waits 3 s before running — Heroic writes the cache in more than one
step, and reading it too early gets a partial file.

## Steam

**Not built.** This section is the specification.

**Source:** `~/.local/share/Steam/steamapps/appmanifest_*.acf` (VDF, not JSON).
A small VDF reader has to be written by hand: adding a dependency for it would
break the stdlib-only promise (CLAUDE.md §2).

**Art:** `~/.local/share/Steam/appcache/librarycache/`. **Newer Steam versions
use a subdirectory per appid**, not the flat `<appid>_library_600x900.jpg`
naming that nearly every guide and blog post still assumes. Handle the
per-appid layout as the primary case. Nothing is downloaded: the art is already
on disk, so this generator only locates it.

**Filtering:** Proton builds, Steam Linux Runtime, Steamworks Common
Redistributables and similar appear as ordinary entries and must be dropped, or
they show up as fake games. Use a heuristic plus a user-editable ignore list —
good defaults, fully overridable, same as bindings.

**Reuse:** `metadata/pegasus.py` already writes the file; a Steam generator
supplies `pegasus.Game` entries and nothing else. That split is why the writer
is its own module.

## Pegasus output notes

Only relevant to the Pegasus writer; other frontends will want their own.

- Pegasus accepts several metadata filenames. We write `metadata.pegasus.txt`
  and only that one. A mismatch between docs and code here wastes an afternoon.
- Each game needs a real file to exist, or Pegasus hides it when
  `general.verify-files` is on (the default). The generator touches an empty
  `games/<key>.heroic` per entry for exactly this reason.
- Values are single-line. Store descriptions routinely contain newlines, so
  they are collapsed; a description that wraps turns the rest of itself into
  unparseable keys.
- Each game directory is registered separately in Pegasus's own settings; the
  generator writes files but cannot register the directory itself. It prints
  the path to add.
- If installing Pegasus to test against, the AUR package is
  `pegasus-frontend-git`. **Not** `pegasus-frontend-stable-git`, which builds a
  tagged release from October 2024 rather than current code.
- Game directories are registered in `~/.config/pegasus-frontend/game_dirs.txt`
  (one path per line), not in `settings.txt`.
- Its window class is `org.pegasus-frontend.pegasus-fe`, so `class = "pegasus"`
  in `[frontend]` matches it.

### Confirming output really is valid

Pegasus does not report how many games it loaded, so "it found the file" proves
very little on its own. What it *does* report, verified against
`pegasus-frontend alpha16-105-g6b322063`:

| Metadata | Log |
|---|---|
| `file:` points at nothing | `[w] Game file ... doesn't seem to exist`, then `[w] The collection 'Heroic' has no valid games, ignored` |
| `file:` exists | neither warning |

So the check is the **absence** of both lines, and it is only meaningful
because the failing case was run too. Generated output for a real 3-game Heroic
library produced neither. Note `[w] The collection 'GOG' has no valid games` is
Pegasus's own GOG provider and has nothing to do with the generated file.

Run it isolated so a test cannot disturb a real config:

```bash
XDG_CONFIG_HOME=/tmp/pgtest pegasus-fe
```

Pegasus ignores `SIGTERM`; use `timeout -s KILL` or it will outlive the script.
