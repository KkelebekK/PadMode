# Library metadata generators (v0.4)

Reference for the `contrib/metadata/` commands. These are **not** part of the
daemon and must never be imported by it (CLAUDE.md §2). They are batch tools
that read a game library and write frontend metadata.

This is a specification for work not yet done. It is written down because it
was learned by building a working version once, and re-deriving it is
expensive.

---

## Shape

Both generators do the same four things:

1. Read an installed-games list from a launcher's own cache.
2. Filter it (skip runtimes, tools, and anything the user has ignored).
3. Download or locate cover art and background art.
4. Write a frontend metadata file.

Properties the working Heroic version had, all worth keeping:

- **Idempotent.** Re-running skips art that is already downloaded, so it is
  cheap to run on every library change.
- **Validating.** Confirm downloaded bytes are actually an image before saving.
  Launcher CDNs return HTML error pages with a 200 status.
- **Retrying.** Art downloads fail transiently; retry before giving up on an
  entry rather than aborting the run.
- **Overridable paths.** Input and output locations come from config or the
  environment, never hardcoded.

## Heroic (Epic / GOG / Amazon)

**Source:** `~/.config/heroic/store_cache/*_library.json` — three files, one per
store. Keep entries where `is_installed` is true.

**Art fields:** `art_square` is the cover (portrait box art); `art_cover` is the
background (landscape hero image).

**Launch line:** Heroic registers a URL handler, so entries launch through it
rather than through an executable path:

```
xdg-open "heroic://launch?appName=<app_id>&runner=<legendary|gog|nile>"
```

The runner value differs per store: `legendary` for Epic, `gog` for GOG, `nile`
for Amazon. It is present in the library JSON.

**Staying current:** a systemd *path* unit watching the three `*_library.json`
files regenerates metadata whenever a game is installed or removed. Give the
triggered service a short delay before it runs — Heroic writes the cache in
more than one step, and reading it too early gets a partial file.

## Steam

Not built yet. See CLAUDE.md §9 for the open decisions.

**Source:** `~/.local/share/Steam/steamapps/appmanifest_*.acf` (VDF, not JSON).

**Art:** `~/.local/share/Steam/appcache/librarycache/`. **Newer Steam versions
use a subdirectory per appid**, not the flat `<appid>_library_600x900.jpg`
naming that nearly every guide and blog post still assumes. Handle the
per-appid layout as the primary case.

**Filtering:** Proton builds, Steam Linux Runtime, Steamworks Common
Redistributables and similar appear as ordinary entries and must be dropped, or
they show up as fake games. Use a heuristic plus a user-editable ignore list —
good defaults, fully overridable, same as bindings.

## Pegasus output notes

Only relevant to the Pegasus writer; other frontends will want their own.

- Pegasus accepts several metadata filenames, and both `metadata.pegasus.txt`
  and `pegasus.metadata.txt` work. Pick one and be consistent; a mismatch
  between docs and code here wastes an afternoon.
- Each game directory is registered separately in Pegasus's own settings; the
  generator writes files but cannot register the directory itself.
- If installing Pegasus to test against, the AUR package is
  `pegasus-frontend-git`. **Not** `pegasus-frontend-stable-git`, which builds a
  tagged release from October 2024 rather than current code.
