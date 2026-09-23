# Library metadata generators

Reference for `padmode metadata`. These are **not** part of the daemon and must
never be imported by it (CLAUDE.md §2, §7). They are batch tools that read a
game library and write frontend metadata.

Heroic is built (v0.4). Steam is partly built: reading the library works,
the rest is specified below and not built.

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

**Partly built.** Reading the library, locating art and filtering are done
(`metadata/steam.py`, `metadata/vdf.py`); the output below is still
specification, and nothing is reachable from `padmode metadata` yet.

**Finding Steam:** `~/.steam/root` first — Steam keeps that link pointing at
its real install — then `~/.local/share/Steam`, then the Flatpak's
`~/.var/app/com.valvesoftware.Steam/.local/share/Steam`.

**Source:** `steamapps/appmanifest_*.acf`, one per app, in Valve's KeyValues
text format (VDF, not JSON). The reader is written by hand: a dependency for it
would break the stdlib-only promise (CLAUDE.md §2). It was run over every text
`.vdf`/`.acf` Steam wrote on the development machine — 299 files, all parsed.
That run found the one thing the fixtures had not: **30 of them start with a
UTF-8 byte-order mark** (Steam's controller templates), which Python does not
count as whitespace, so an unstripped BOM reads as a key and misaligns every
pair after it. 87 use CRLF, which needs nothing.

**Library folders:** games on another drive are not under the Steam directory
at all. `steamapps/libraryfolders.vdf` lists every library; each numbered entry
holds a `path`. An older layout mapped the number straight to the path and
mixed in non-numeric bookkeeping keys — not seen here, accepted anyway. Steam's
own directory is always read, even when that file is missing or broken, and a
library whose directory is gone (an unmounted drive) is skipped with a warning.

**Installed means a bit, not a value.** A manifest carries `StateFlags`; bit 4
is "all files on disk". Compare with `& 4`, never `== 4`: Proton Experimental
reads **6** here — installed, with an update waiting.

Measured on the development machine: 16 manifests, 16 installed, read in
20 ms. **Nine of the sixteen are not games** (four Proton builds, four Steam
Linux Runtimes, Steamworks Common Redistributables) — which is the filter's
job, below, and why it cannot be an afterthought.

**Art:** `<steam>/appcache/librarycache/`, and only there — a library on
another drive has no cache of its own. Nothing is downloaded: the art is
already on disk, and `find_art` only locates it. Measured over the 572 app
directories in the cache on the development machine:

| Role | File name | Size | Apps |
|---|---|---|---|
| cover (portrait) | `library_600x900.jpg` **or** `library_capsule.jpg` | 300x450 | 199 + 73 |
| background | `library_hero.jpg` | 1920x620 | 262 |
| logo | `logo.png` | 640x360 | 249 |
| header | `library_header.jpg` **or** `header.jpg` | 460x215 | 130 + 385 |

- **The cover has two names**, and no app has both. `library_capsule.jpg` is
  the newer one. The "600x900" in the older name is not the file's size.
- **Three layouts, and one app can mix them.** Loose in `<appid>/`; one file
  per hashed subdirectory, `<appid>/<sha1>/<name>` — every
  `library_capsule.jpg` is stored this way; and the flat
  `<appid>_library_600x900.jpg` that nearly every guide still describes, which
  **not one file on this machine uses**. Dice & Fold has its cover hashed and
  its background loose, so the layout is decided per file, not per app.
- A loose `<sha1>.jpg` beside the art is a 32x32 icon, never a cover.
- All 1903 images in the cache start with a valid JPEG/PNG signature, and no
  app has two files for one role. `find_art` still passes over empty files
  (what Steam leaves mid-write) and takes the newest where there are two.
- The binary `assetcache.vdf` at the top of the cache indexes these files. It
  is not read: walking the directories finds the same thing without a second,
  binary, parser.

On the real library, all 7 games get a cover and a background. **None of the
9 non-games has a cover** (one has a background). That makes "no cover" a
tempting filter signal — but a game Steam has not shown yet has no art cached
either, so it would vanish until it was. Recorded for the filter, not used.

**Open for the output step:** point Pegasus at the files in Steam's cache, or
copy them next to the metadata (as Heroic does)? Steam's cache is not
permanent — hashed directory names change when art is updated, and the index
records a `last_cleanup_time` — so a reference into it can go stale between
runs. Copying costs a validation step (a partial copy is a broken cover until
someone deletes it, which is the Heroic lesson again).

**Filtering:** Proton builds, the Steam Linux Runtimes and the Steamworks
redistributables are installed apps like any other, and would show up as fake
games. On the development machine they are **9 of 16** installed apps.
`skip_reason` asks, in order, and the first answer wins:

1. **The user's ignore list.** `true` hides any app; `false` shows one that
   anything below would hide. Good defaults, fully overridable — the same
   shape as bindings, where `false` takes a default back.
2. **A default list** by appid, for apps with no structural sign of being a
   tool. Today that is one entry: 228980, Steamworks Common Redistributables.
3. **A `toolmanifest.vdf` in the install directory.** That file tells Steam
   how to run games *through* the app — it is what makes Proton Proton.
   Measured: 8 of 8 Proton builds and runtimes have it, 0 of 7 games do. It
   catches the next Proton with no one adding its name or appid anywhere,
   which a name pattern would not.
4. **A name ending in "Dedicated Server"**, Valve's naming for game servers,
   which install like apps and have no tool manifest. None is installed now,
   but one was (its directory is still in `steamapps/common`).

Result on the real library: 7 games kept, 9 skipped — 8 as compatibility
tools, the redistributables by the default list. Skipped apps come back with
their reason, so the output step can show them: an app hidden by mistake
should be one line away from being noticed.

Considered and not used: the binary `appinfo.vdf` knows each app's real type
(game, tool, …) but is a second, binary, undocumented format to parse and
keep up with; and "has no cover", which fits all 9 here but would also hide
any game Steam has not displayed yet.

The ignore list lives in padmode's own config file:

```toml
[metadata.steam.ignore]
228980 = false    # show the redistributables after all
1234567 = true    # hide this one
```

It is read by the generator, **not** by the daemon's config loader, which
ignores `[metadata]` entirely (a test holds it to that). A typo in the list
should stop the generator with a message, not stop the daemon: the pad has
nothing to do with it. Keys are appids — the number in the store URL and in
`appmanifest_<appid>.acf` — and values must be booleans; anything else is an
error, as it is everywhere else in the config.

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
