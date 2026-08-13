"""The library metadata generators.

Nothing here touches the network: :func:`download` takes its fetcher as an
argument, and every test passes one that answers from a dict. A test that
reaches a launcher's CDN would be slow, flaky, and would fail in CI for
reasons that have nothing to do with the code.

The last class is the interesting one. CLAUDE.md §2 says the generators are
never imported by the daemon; that is checked here rather than trusted, since
an accidental import at the top of a module is exactly the kind of thing that
survives review.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from padmode.metadata import heroic, pegasus

REPO = Path(__file__).resolve().parent.parent

#: The smallest valid-looking bytes for each format we accept.
JPEG = b"\xff\xd8\xff" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 12
#: What a CDN actually returns when it fails: an error page, with a 200.
HTML = b"<!DOCTYPE html><html><body>404</body></html>"


def library(*games, wrapped=True):
    """A store cache file's contents, in either shape Heroic has written."""
    return {"library": list(games)} if wrapped else list(games)


def game(app_name="Game1", **fields):
    entry = {
        "app_name": app_name,
        "title": "A Game",
        "runner": "legendary",
        "is_installed": True,
    }
    entry.update(fields)
    return entry


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(_remove, self.tmp)

    def quietly(self, options, **kwargs):
        """Run the generator without its progress report joining the test run."""
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            return heroic.generate(options, **kwargs)

    def heroic_dir(self, **files):
        """A fake Heroic config directory holding the given store caches."""
        cache = self.tmp / "heroic" / "store_cache"
        cache.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (cache / f"{name}_library.json").write_text(json.dumps(data))
        return self.tmp / "heroic"


def _remove(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()


class TestReadingTheLibrary(TempDirCase):
    def test_only_installed_games_are_kept(self):
        config = self.heroic_dir(legendary=library(
            game("Installed"), game("NotInstalled", is_installed=False),
        ))
        found = heroic.installed_games(config)
        self.assertEqual([g["app_name"] for g in found], ["Installed"])

    def test_all_three_stores_are_read(self):
        config = self.heroic_dir(
            legendary=library(game("Epic")),
            gog=library(game("Gog", runner="gog")),
            nile=library(game("Amazon", runner="nile")),
        )
        found = heroic.installed_games(config)
        self.assertEqual(
            sorted(g["app_name"] for g in found), ["Amazon", "Epic", "Gog"]
        )

    def test_a_bare_list_is_accepted_as_well_as_a_wrapped_one(self):
        # Heroic has written both shapes.
        config = self.heroic_dir(legendary=library(game(), wrapped=False))
        self.assertEqual(len(heroic.installed_games(config)), 1)

    def test_an_empty_store_is_not_an_error(self):
        config = self.heroic_dir(legendary=library(game()), gog={})
        self.assertEqual(len(heroic.installed_games(config)), 1)

    def test_one_unreadable_file_does_not_cost_the_others(self):
        config = self.heroic_dir(legendary=library(game()))
        (config / "store_cache" / "gog_library.json").write_text("{not json")
        with self.assertLogs(heroic.log, "WARNING"):
            found = heroic.installed_games(config)
        self.assertEqual(len(found), 1)

    def test_the_config_dir_is_found_by_its_store_cache(self):
        config = self.heroic_dir(legendary=library(game()))
        self.assertEqual(heroic.find_config_dir(config), config)

    def test_a_directory_without_a_store_cache_is_not_heroic(self):
        self.assertIsNone(heroic.find_config_dir(self.tmp / "nope"))


class TestValidatingArt(unittest.TestCase):
    def test_real_image_headers_are_accepted(self):
        for data in (JPEG, PNG, WEBP):
            self.assertTrue(heroic.looks_like_image(data))

    def test_an_error_page_is_not_an_image(self):
        # The reason this check exists: a 200 response carrying HTML would
        # otherwise be saved as a .jpg and skipped by every later run.
        self.assertFalse(heroic.looks_like_image(HTML))

    def test_a_truncated_response_is_not_an_image(self):
        self.assertFalse(heroic.looks_like_image(b"\xff\xd8"))


class TestDownloading(TempDirCase):
    def fetcher(self, *responses):
        """A fetch that returns each response in turn, raising for exceptions."""
        remaining = list(responses)

        def fetch(url):
            value = remaining.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        self.remaining = remaining
        return fetch

    def test_a_valid_image_is_saved(self):
        dest = self.tmp / "cover.jpg"
        self.assertTrue(heroic.download(
            "http://x/a.jpg", dest, fetch=self.fetcher(JPEG), sleep=lambda _: None
        ))
        self.assertEqual(dest.read_bytes(), JPEG)

    def test_an_error_page_is_never_written(self):
        dest = self.tmp / "cover.jpg"
        self.assertFalse(heroic.download(
            "http://x/a.jpg", dest,
            fetch=self.fetcher(HTML, HTML, HTML), sleep=lambda _: None,
        ))
        self.assertFalse(dest.exists())

    def test_a_transient_failure_is_retried(self):
        dest = self.tmp / "cover.jpg"
        fetch = self.fetcher(OSError("connection reset"), JPEG)
        self.assertTrue(
            heroic.download("http://x/a.jpg", dest, fetch=fetch, sleep=lambda _: None)
        )
        self.assertEqual(dest.read_bytes(), JPEG)

    def test_it_gives_up_after_the_last_retry(self):
        dest = self.tmp / "cover.jpg"
        fetch = self.fetcher(*[OSError("nope")] * heroic.RETRIES)
        with self.assertLogs(heroic.log, "WARNING"):
            self.assertFalse(
                heroic.download("http://x/a.jpg", dest, fetch=fetch,
                                sleep=lambda _: None)
            )

    def test_art_already_on_disk_is_not_fetched_again(self):
        # Idempotence: re-running after installing one game must not re-download
        # the whole library's art.
        dest = self.tmp / "cover.jpg"
        dest.write_bytes(JPEG)

        def explode(url):
            raise AssertionError("should not have been fetched")

        self.assertTrue(heroic.download("http://x/a.jpg", dest, fetch=explode))

    def test_an_empty_file_is_fetched_again(self):
        dest = self.tmp / "cover.jpg"
        dest.touch()
        self.assertTrue(heroic.download(
            "http://x/a.jpg", dest, fetch=self.fetcher(JPEG), sleep=lambda _: None
        ))
        self.assertEqual(dest.read_bytes(), JPEG)

    def test_no_url_is_not_a_download(self):
        self.assertFalse(heroic.download(None, self.tmp / "cover.jpg"))


class TestGeneratingMetadata(TempDirCase):
    def generate(self, *games, **options):
        config = self.heroic_dir(legendary=library(*games))
        out = self.tmp / "out"
        self.status = self.quietly(
            heroic.Options(config_dir=config, out_dir=out, **options),
            fetch=lambda url: JPEG,
        )
        self.out = out
        text = out / pegasus.METADATA_FILE
        return text.read_text() if text.exists() else ""

    def test_a_game_becomes_an_entry(self):
        text = self.generate(game(title="Hollow Knight"))
        self.assertIn("collection: Heroic", text)
        self.assertIn("game: Hollow Knight", text)

    def test_the_launch_line_goes_through_heroics_url_handler(self):
        text = self.generate(game("abc123", runner="gog"))
        self.assertIn(
            'launch: xdg-open "heroic://launch?appName=abc123&runner=gog"', text
        )

    def test_art_is_referenced_relatively(self):
        text = self.generate(game(art_square="http://x/c.jpg",
                                  art_cover="http://x/b.jpg"))
        self.assertIn("assets.box_front: art/Game1_cover.jpg", text)
        self.assertIn("assets.background: art/Game1_bg.jpg", text)
        self.assertTrue((self.out / "art" / "Game1_cover.jpg").exists())

    def test_a_game_file_is_created_for_each_entry(self):
        # Pegasus hides entries whose file is missing.
        self.generate(game("abc123"))
        self.assertTrue((self.out / "games" / "abc123.heroic").exists())

    def test_an_app_id_that_is_not_filename_safe_is_sanitized(self):
        text = self.generate(game("weird/id:1"))
        self.assertIn("file: games/weird_id_1.heroic", text)
        # The launch line keeps the real id -- only the filename is sanitized.
        self.assertIn("appName=weird/id:1", text)

    def test_a_multiline_description_is_collapsed(self):
        text = self.generate(game(extra={"about": {"description": "One\nTwo"}}))
        self.assertIn("description: One Two", text)

    def test_a_description_that_merely_repeats_the_title_is_dropped(self):
        text = self.generate(
            game(title="Braid", extra={"about": {"description": "Braid"}})
        )
        self.assertNotIn("description:", text)

    def test_entries_are_sorted_by_title(self):
        text = self.generate(
            game("b", title="Zeta"), game("a", title="Alpha"),
        )
        self.assertLess(text.index("game: Alpha"), text.index("game: Zeta"))

    def test_an_entry_without_an_app_name_is_skipped(self):
        with self.assertLogs(heroic.log, "WARNING"):
            text = self.generate(game(app_name=None, title="Broken"))
        self.assertNotIn("Broken", text)

    def test_a_dry_run_writes_nothing(self):
        text = self.generate(game(), dry_run=True)
        self.assertEqual(text, "")
        self.assertFalse(self.out.exists())

    def test_a_dry_run_still_shows_the_metadata(self):
        self.generate(game(title="Braid"), dry_run=True)
        self.assertIn("game: Braid", self.output.getvalue())

    def test_no_heroic_installation_is_an_error_not_a_crash(self):
        with self.assertLogs(heroic.log, "ERROR"):
            status = self.quietly(
                heroic.Options(config_dir=self.tmp / "nope", out_dir=self.tmp)
            )
        self.assertEqual(status, 1)

    def test_an_empty_library_is_an_error_rather_than_an_empty_file(self):
        # Overwriting a good metadata file with an empty one because Heroic was
        # mid-write is worse than doing nothing.
        config = self.heroic_dir(legendary=library())
        with self.assertLogs(heroic.log, "ERROR"):
            status = self.quietly(
                heroic.Options(config_dir=config, out_dir=self.tmp / "out")
            )
        self.assertEqual(status, 1)
        self.assertFalse((self.tmp / "out" / pegasus.METADATA_FILE).exists())


class TestOptionResolution(unittest.TestCase):
    def test_a_flag_beats_the_environment(self):
        options = heroic.Options.resolve(out_dir=Path("/flag"))
        self.assertEqual(options.out_dir, Path("/flag"))

    def test_the_environment_beats_the_default(self):
        with mock.patch.dict(
            "os.environ", {"PEGASUS_HEROIC_OUT": "/env"}
        ):
            self.assertEqual(heroic.Options.resolve().out_dir, Path("/env"))

    def test_the_default_is_used_when_nothing_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                heroic.Options.resolve().out_dir,
                Path(heroic.DEFAULT_OUT).expanduser(),
            )


class TestPegasusOutput(unittest.TestCase):
    def test_optional_fields_are_omitted_rather_than_left_blank(self):
        entry = pegasus.Game(title="T", launch="x", file="games/t.heroic")
        rendered = entry.render()
        self.assertNotIn("developer:", rendered)
        self.assertNotIn("assets.", rendered)

    def test_the_collection_header_comes_first(self):
        text = pegasus.render(
            [pegasus.Game(title="T", launch="x", file="f")], "Heroic"
        )
        self.assertTrue(text.startswith("collection: Heroic"))


class TestSeparationFromTheDaemon(unittest.TestCase):
    """CLAUDE.md §2: the generators are never imported by the daemon."""

    def imports_of(self, module: str) -> set[str]:
        code = (
            f"import sys; import {module}; "
            "print([m for m in sys.modules if m.startswith('padmode.metadata')])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
            check=True,
        )
        return set(eval(out.stdout))  # noqa: S307 -- our own literal list

    def test_importing_the_daemon_does_not_pull_in_the_generators(self):
        self.assertEqual(self.imports_of("padmode.daemon"), set())

    def test_nor_does_building_the_command_line(self):
        # The parser is built on every invocation, including `padmode run`, so
        # declaring the generator's flags there would import it for everyone.
        self.assertEqual(
            self.imports_of("padmode.__main__"), set()
        )


class TestTheCommandLine(unittest.TestCase):
    def run_padmode(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "padmode", *args],
            cwd=REPO, capture_output=True, text=True, check=False,
        )

    def test_the_generator_is_reachable(self):
        result = self.run_padmode("metadata", "heroic", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dry-run", result.stdout)

    def test_the_documented_default_is_the_real_one(self):
        # Help text is documentation, and documentation that disagrees with the
        # code is the failure mode CLAUDE.md §4 calls out.
        result = self.run_padmode("metadata", "heroic", "--help")
        self.assertIn(heroic.DEFAULT_OUT, result.stdout)

    def test_metadata_without_a_generator_shows_what_is_available(self):
        result = self.run_padmode("metadata")
        self.assertEqual(result.returncode, 2)
        self.assertIn("heroic", result.stdout)


if __name__ == "__main__":
    unittest.main()
