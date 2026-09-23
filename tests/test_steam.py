"""Reading a Steam library: the KeyValues reader, the manifests, the art, and
telling the games from everything else.

Every fixture is text written here, shaped after files Steam wrote on the
maintainer's machine. The reader was also run over every text ``.vdf`` and
``.acf`` in that Steam install -- 299 files, all parsed -- and two of the
cases below (the byte-order mark, CRLF) are what that run turned up.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from padmode import config
from padmode.metadata import steam, vdf

#: Trimmed from a real manifest: the nested depot blocks are kept because they
#: are the part a naive reader gets wrong.
MANIFEST = '''"AppState"
{
\t"appid"\t\t"227300"
\t"universe"\t\t"1"
\t"name"\t\t"Euro Truck Simulator 2"
\t"StateFlags"\t\t"4"
\t"installdir"\t\t"Euro Truck Simulator 2"
\t"InstalledDepots"
\t{
\t\t"227301"
\t\t{
\t\t\t"manifest"\t\t"5602165946209005165"
\t\t\t"size"\t\t"38115657699"
\t\t}
\t}
\t"UserConfig"
\t{
\t\t"language"\t\t"english"
\t}
}
'''


def manifest(appid, name="A Game", flags=4, installdir=None):
    installdir = name if installdir is None else installdir
    return (
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n'
        f'\t"StateFlags"\t\t"{flags}"\n\t"installdir"\t\t"{installdir}"\n}}\n'
    )


class TestKeyValues(unittest.TestCase):
    def test_a_real_manifest(self):
        data = vdf.loads(MANIFEST)
        state = data["appstate"]
        self.assertEqual(state["appid"], "227300")
        self.assertEqual(state["name"], "Euro Truck Simulator 2")
        self.assertEqual(
            state["installeddepots"]["227301"]["size"], "38115657699"
        )
        self.assertEqual(state["userconfig"], {"language": "english"})

    def test_keys_are_lowercased_and_values_are_not(self):
        data = vdf.loads('"LibraryFolders" { "Path" "/Mnt/Games" }')
        self.assertEqual(data, {"libraryfolders": {"path": "/Mnt/Games"}})

    def test_bare_tokens(self):
        self.assertEqual(
            vdf.loads("root { key value other 12 }"),
            {"root": {"key": "value", "other": "12"}},
        )

    def test_comments_are_skipped(self):
        text = '// header\n"a" // after a key\n{\n"b" "c" // trailing\n}\n'
        self.assertEqual(vdf.loads(text), {"a": {"b": "c"}})

    def test_a_slash_inside_a_quoted_string_is_not_a_comment(self):
        self.assertEqual(vdf.loads('"url" "https://x//y"'), {"url": "https://x//y"})

    def test_escapes(self):
        text = r'"name" "say \"hi\"\tnow\\then\n"'
        self.assertEqual(vdf.loads(text), {"name": 'say "hi"\tnow\\then\n'})

    def test_an_unknown_escape_keeps_its_backslash(self):
        # An unescaped Windows path must survive.
        self.assertEqual(vdf.loads(r'"p" "C:\Games"'), {"p": r"C:\Games"})

    def test_a_byte_order_mark_is_dropped(self):
        # Steam's controller templates start with one. Left in, it reads as a
        # bare key and every pair after it shifts by one.
        text = '\ufeff"controller_mappings"\r\n{\r\n\t"version"\t\t"3"\r\n}\r\n'
        self.assertEqual(
            vdf.loads(text), {"controller_mappings": {"version": "3"}}
        )

    def test_a_repeated_key_keeps_its_last_value(self):
        self.assertEqual(vdf.loads('"a" "1" "A" "2"'), {"a": "2"})

    def test_conditionals_are_dropped(self):
        text = '"root" { "a" "1" [$WIN32] "b" [$LINUX] "2" }'
        self.assertEqual(vdf.loads(text), {"root": {"a": "1", "b": "2"}})

    def test_empty_text(self):
        self.assertEqual(vdf.loads(""), {})
        self.assertEqual(vdf.loads("  // nothing\n"), {})

    def test_errors_say_which_line(self):
        for text, line in (
            ('"a"\n{\n"b" "c"\n', 3),  # never closed
            ('"a" "b"\n}\n', 2),  # stray close
            ('"a" "b"\n"c"\n', 2),  # key with no value
            ('"a" "b"\n"c" "d\ne\n', 2),  # quote never closed
            ('{ "a" "b" }', 1),  # block with no key
        ):
            with self.subTest(text=text):
                with self.assertRaises(vdf.VDFError) as caught:
                    vdf.loads(text)
                self.assertEqual(caught.exception.line, line)

    def test_an_error_is_a_value_error(self):
        # Callers that already catch ValueError for malformed input keep working.
        self.assertTrue(issubclass(vdf.VDFError, ValueError))

    def test_a_newline_inside_a_string_is_counted(self):
        with self.assertRaises(vdf.VDFError) as caught:
            vdf.loads('"a" "one\ntwo"\n"b"')
        self.assertEqual(caught.exception.line, 3)


class SteamDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.steam = self.library("Steam")

    def library(self, name):
        """A directory holding an empty ``steamapps``."""
        (self.tmp / name / "steamapps").mkdir(parents=True)
        return self.tmp / name

    def install(self, library, appid, **fields):
        (library / "steamapps" / f"appmanifest_{appid}.acf").write_text(
            manifest(appid, **fields)
        )

    def folders(self, text):
        (self.steam / "steamapps" / "libraryfolders.vdf").write_text(text)


class TestFindingSteam(SteamDirCase):
    def test_an_explicit_directory_wins(self):
        self.assertEqual(steam.find_steam_dir(self.steam), self.steam)

    def test_an_explicit_directory_without_steamapps_is_not_steam(self):
        self.assertIsNone(steam.find_steam_dir(self.tmp))

    def test_candidates_are_tried_in_order(self):
        other = self.library("Other")
        with mock.patch.object(
            steam, "STEAM_CANDIDATES", (str(self.tmp / "absent"), str(other), str(self.steam))
        ):
            self.assertEqual(steam.find_steam_dir(), other)

    def test_a_link_is_resolved(self):
        # ~/.steam/root is a link; library dedup compares real paths.
        link = self.tmp / "root"
        link.symlink_to(self.steam)
        self.assertEqual(steam.find_steam_dir(link), self.steam)

    def test_nothing_found(self):
        with mock.patch.object(steam, "STEAM_CANDIDATES", (str(self.tmp / "absent"),)):
            self.assertIsNone(steam.find_steam_dir())


class TestLibraryFolders(SteamDirCase):
    def test_no_file_means_steam_alone(self):
        self.assertEqual(steam.library_dirs(self.steam), [self.steam])

    def test_the_current_layout(self):
        drive = self.library("drive")
        self.folders(
            f'"libraryfolders" {{ "0" {{ "path" "{self.steam}" "apps" {{ }} }}'
            f' "1" {{ "path" "{drive}" "label" "" }} }}'
        )
        self.assertEqual(steam.library_dirs(self.steam), [self.steam, drive])

    def test_the_older_layout(self):
        drive = self.library("drive")
        self.folders(
            f'"LibraryFolders" {{ "TimeNextStatsReport" "1627894872" '
            f'"ContentStatsID" "-4012853473395834475" "1" "{drive}" }}'
        )
        self.assertEqual(steam.library_dirs(self.steam), [self.steam, drive])

    def test_steam_itself_is_listed_once(self):
        # Listed through a link, as ~/.steam/root would be.
        link = self.tmp / "root"
        link.symlink_to(self.steam)
        self.folders(f'"libraryfolders" {{ "0" {{ "path" "{link}" }} }}')
        self.assertEqual(steam.library_dirs(self.steam), [self.steam])

    def test_a_missing_drive_is_skipped_with_a_warning(self):
        drive = self.library("drive")
        self.folders(
            f'"libraryfolders" {{ "1" {{ "path" "{self.tmp / "unmounted"}" }}'
            f' "2" {{ "path" "{drive}" }} }}'
        )
        with self.assertLogs(steam.log, "WARNING") as logs:
            self.assertEqual(steam.library_dirs(self.steam), [self.steam, drive])
        self.assertIn("unmounted", logs.output[0])

    def test_a_broken_file_still_reads_steam(self):
        self.folders('"libraryfolders" { "0" { "path" ')
        with self.assertLogs(steam.log, "WARNING"):
            self.assertEqual(steam.library_dirs(self.steam), [self.steam])

    def test_a_file_without_the_block_still_reads_steam(self):
        self.folders('"something" "else"')
        with self.assertLogs(steam.log, "WARNING"):
            self.assertEqual(steam.library_dirs(self.steam), [self.steam])

    def test_entries_without_a_path_are_ignored(self):
        self.folders('"libraryfolders" { "0" { "label" "" } "1" { "path" { } } }')
        self.assertEqual(steam.library_dirs(self.steam), [self.steam])


class TestManifests(SteamDirCase):
    def read(self, text):
        path = self.steam / "steamapps" / "appmanifest_1.acf"
        path.write_text(text)
        return steam.read_manifest(path, self.steam)

    def test_fields(self):
        app = self.read(MANIFEST)
        self.assertEqual(app, steam.App(
            appid=227300,
            name="Euro Truck Simulator 2",
            installdir="Euro Truck Simulator 2",
            library=self.steam,
            state_flags=4,
        ))
        self.assertTrue(app.installed)

    def test_installed_is_a_bit_not_a_value(self):
        for flags, installed in ((4, True), (6, True), (2, False), (0, False)):
            with self.subTest(flags=flags):
                app = self.read(manifest(1, flags=flags))
                self.assertIs(app.installed, installed)

    def test_unreadable_flags_are_not_installed(self):
        self.assertFalse(self.read(manifest(1, flags="soon")).installed)

    def test_a_missing_name_falls_back_to_the_directory_then_the_appid(self):
        self.assertEqual(
            self.read('"AppState" { "appid" "7" "installdir" "Seven" }').name,
            "Seven",
        )
        self.assertEqual(self.read('"AppState" { "appid" "7" }').name, "Steam app 7")

    def test_unusable_manifests_are_skipped_with_a_warning(self):
        for text in (
            '"AppState" { "appid" ',  # truncated
            '"AppState" { "name" "No id" }',
            '"AppState" { "appid" "abc" }',
            '"AppState" { "appid" { } }',
            '"AppState" "flat"',
            "",
        ):
            with self.subTest(text=text):
                with self.assertLogs(steam.log, "WARNING"):
                    self.assertIsNone(self.read(text))


class TestInstalledApps(SteamDirCase):
    def test_only_installed_apps(self):
        self.install(self.steam, 10, name="Ready")
        self.install(self.steam, 20, name="Needs an update", flags=6)
        self.install(self.steam, 30, name="Not installed", flags=2)
        names = {app.name for app in steam.installed_apps(self.steam)}
        self.assertEqual(names, {"Ready", "Needs an update"})

    def test_every_library(self):
        drive = self.library("drive")
        self.folders(f'"libraryfolders" {{ "1" {{ "path" "{drive}" }} }}')
        self.install(self.steam, 10, name="Here")
        self.install(drive, 20, name="There")
        apps = {app.name: app.library for app in steam.installed_apps(self.steam)}
        self.assertEqual(apps, {"Here": self.steam, "There": drive})

    def test_an_app_in_two_libraries_is_listed_once(self):
        drive = self.library("drive")
        self.folders(f'"libraryfolders" {{ "1" {{ "path" "{drive}" }} }}')
        self.install(self.steam, 10, name="Original")
        self.install(drive, 10, name="Stale copy")
        apps = steam.installed_apps(self.steam)
        self.assertEqual([app.name for app in apps], ["Original"])

    def test_one_broken_manifest_costs_only_itself(self):
        self.install(self.steam, 10, name="Fine")
        (self.steam / "steamapps" / "appmanifest_20.acf").write_text('"AppState" {')
        with self.assertLogs(steam.log, "WARNING"):
            apps = steam.installed_apps(self.steam)
        self.assertEqual([app.name for app in apps], ["Fine"])

    def test_runtimes_are_not_filtered_yet(self):
        # They are apps to Steam. Dropping them is the filter's job, not this.
        self.install(self.steam, 1493710, name="Proton Experimental", flags=6)
        self.assertEqual(len(steam.installed_apps(self.steam)), 1)


class TestArt(SteamDirCase):
    """Layouts are the ones found in the real cache; see ``steam.find_art``."""

    def art(self, relative, mtime=None, data=b"\xff\xd8\xff image"):
        path = self.steam / steam.ART_CACHE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def find(self, appid=10):
        return steam.find_art(self.steam, appid)

    def test_loose_in_the_app_directory(self):
        cover = self.art("10/library_600x900.jpg")
        background = self.art("10/library_hero.jpg")
        logo = self.art("10/logo.png")
        header = self.art("10/header.jpg")
        self.assertEqual(self.find(), steam.Art(cover, background, logo, header))

    def test_in_hashed_subdirectories(self):
        # Every library_capsule.jpg in the real cache is stored this way.
        cover = self.art("10/711e39803402def82a90e1f31578c64744f952f3/library_capsule.jpg")
        background = self.art("10/3eeb9970d8a8b6634d114b43e0edba78a7ee1eb6/library_hero.jpg")
        header = self.art("10/9f07297252126692448208dc3e9d00fbcd690759/library_header.jpg")
        art = self.find()
        self.assertEqual(
            (art.cover, art.background, art.header), (cover, background, header)
        )

    def test_one_app_can_mix_both(self):
        # Measured: Dice & Fold's cover is hashed, its background loose.
        cover = self.art("10/b5d4f63c530eba50441f7c433964f04f5f7d4448/library_600x900.jpg")
        background = self.art("10/library_hero.jpg")
        art = self.find()
        self.assertEqual((art.cover, art.background), (cover, background))

    def test_the_flat_layout_the_guides_describe(self):
        cover = self.art("10_library_600x900.jpg")
        self.assertEqual(self.find().cover, cover)

    def test_the_icon_is_not_a_cover(self):
        self.art("10/adc18a4fc9adc0330144b76d61cbda68bb2394a0.jpg")
        self.assertEqual(self.find(), steam.Art())

    def test_nothing_cached(self):
        self.assertEqual(self.find(), steam.Art())
        (self.steam / steam.ART_CACHE / "10").mkdir(parents=True)
        self.assertEqual(self.find(), steam.Art())

    def test_another_apps_art_is_not_borrowed(self):
        self.art("100/library_600x900.jpg")
        self.art("100_library_600x900.jpg")
        self.art("10x/library_600x900.jpg")
        self.assertIsNone(self.find(10).cover)

    def test_an_empty_file_is_passed_over(self):
        # What Steam leaves while it is still writing.
        self.art("10/library_600x900.jpg", mtime=2_000_000_000, data=b"")
        cover = self.art("10/abc/library_capsule.jpg", mtime=1_000_000_000)
        self.assertEqual(self.find().cover, cover)

    def test_the_newest_copy_wins(self):
        old = self.art("10/library_hero.jpg", mtime=1_000_000_000)
        new = self.art("10/abc/library_hero.jpg", mtime=2_000_000_000)
        self.assertEqual(self.find().background, new)
        os.utime(old, (3_000_000_000, 3_000_000_000))
        self.assertEqual(self.find().background, old)

    def test_a_tie_keeps_the_loose_file(self):
        loose = self.art("10/library_hero.jpg", mtime=1_000_000_000)
        self.art("10/abc/library_hero.jpg", mtime=1_000_000_000)
        self.assertEqual(self.find().background, loose)

    def test_a_directory_with_the_right_name_is_not_art(self):
        (self.steam / steam.ART_CACHE / "10" / "library_hero.jpg").mkdir(parents=True)
        self.assertIsNone(self.find().background)


class TestTellingGamesApart(SteamDirCase):
    def app(self, appid=10, name="A Game", installdir=None, library=None):
        installdir = name if installdir is None else installdir
        library = library or self.steam
        (library / "steamapps" / "common" / installdir).mkdir(parents=True, exist_ok=True)
        return steam.App(appid, name, installdir, library, 4)

    def tool(self, appid=20, name="Proton 12.0", **kwargs):
        app = self.app(appid, name, **kwargs)
        install = app.library / "steamapps" / "common" / app.installdir
        (install / steam.TOOL_MANIFEST).write_text('"manifest" { "version" "2" }')
        return app

    def test_a_game_is_kept(self):
        self.assertIsNone(steam.skip_reason(self.app(), {}))

    def test_a_compatibility_tool_is_recognised_by_its_manifest(self):
        # Not by name: a Proton that does not exist yet is caught too.
        self.assertEqual(steam.skip_reason(self.tool(), {}), "compatibility tool")

    def test_the_tool_manifest_is_looked_for_in_the_apps_own_library(self):
        drive = self.library("drive")
        self.tool(installdir="Shared")  # same directory name, other library
        app = self.app(installdir="Shared", library=drive)
        self.assertIsNone(steam.skip_reason(app, {}))

    def test_no_install_directory_is_not_a_tool(self):
        # Otherwise steamapps/common itself would be asked for a manifest.
        (self.steam / "steamapps" / "common").mkdir(parents=True, exist_ok=True)
        (self.steam / "steamapps" / "common" / steam.TOOL_MANIFEST).write_text("")
        app = steam.App(10, "Nameless", "", self.steam, 4)
        self.assertFalse(steam.is_tool(app))

    def test_the_redistributables_are_ignored_by_default(self):
        app = self.app(228980, "Steamworks Common Redistributables", "Steamworks Shared")
        self.assertEqual(
            steam.skip_reason(app, {}), steam.DEFAULT_IGNORE[228980]
        )

    def test_a_dedicated_server(self):
        app = self.app(name="StarRupture Dedicated Server")
        self.assertEqual(steam.skip_reason(app, {}), "dedicated server")
        # The suffix, not the words anywhere.
        self.assertIsNone(steam.skip_reason(self.app(name="Dedicated Server Tycoon"), {}))

    def test_the_user_can_hide_anything(self):
        self.assertEqual(
            steam.skip_reason(self.app(appid=10), {10: True}), "ignored in config"
        )

    def test_the_user_can_show_anything(self):
        # false beats the default list, the tool check and the name check alike.
        for app in (
            self.app(228980, "Steamworks Common Redistributables", "Shared"),
            self.tool(),
            self.app(30, "X Dedicated Server"),
        ):
            with self.subTest(app=app.name):
                self.assertIsNone(steam.skip_reason(app, {app.appid: False}))

    def test_another_apps_entry_changes_nothing(self):
        self.assertEqual(steam.skip_reason(self.tool(appid=20), {10: False}),
                         "compatibility tool")

    def test_split_keeps_order_and_says_why(self):
        game, tool, server = (
            self.app(1, "B Game"), self.tool(2), self.app(3, "Z Dedicated Server")
        )
        other = self.app(4, "A Game")
        games, skipped = steam.split_games([game, tool, server, other], {})
        self.assertEqual(games, [game, other])
        self.assertEqual(
            skipped, [(tool, "compatibility tool"), (server, "dedicated server")]
        )


class TestTheIgnoreList(SteamDirCase):
    def parse(self, table):
        return steam.parse_ignore({"metadata": {"steam": {"ignore": table}}})

    def test_absent_is_empty(self):
        self.assertEqual(steam.parse_ignore({}), {})
        self.assertEqual(steam.parse_ignore({"metadata": {}}), {})
        self.assertEqual(steam.parse_ignore({"metadata": {"steam": {}}}), {})

    def test_appids_and_booleans(self):
        self.assertEqual(
            self.parse({"228980": False, "1234567": True}),
            {228980: False, 1234567: True},
        )

    def test_mistakes_are_errors_not_silence(self):
        for data, says in (
            ({"metadata": []}, "[metadata]"),
            ({"metadata": {"steam": 1}}, "[metadata.steam]"),
            ({"metadata": {"steam": {"ignroe": {}}}}, "ignroe"),
            ({"metadata": {"steam": {"ignore": [228980]}}}, "table of appid"),
            ({"metadata": {"steam": {"ignore": {"Proton": True}}}}, "not an appid"),
            ({"metadata": {"steam": {"ignore": {"10": 1}}}}, "true (hide it)"),
            ({"metadata": {"steam": {"ignore": {"10": "yes"}}}}, "true (hide it)"),
        ):
            with self.subTest(data=data):
                with self.assertRaises(ValueError) as caught:
                    steam.parse_ignore(data)
                self.assertIn(says, str(caught.exception))

    def test_a_missing_file_is_an_empty_list(self):
        self.assertEqual(steam.load_ignore(self.tmp / "absent.toml"), {})

    def test_from_the_config_file(self):
        path = self.tmp / "config.toml"
        path.write_text(
            "[general]\nvolume_step = 5\n\n"
            "[metadata.steam.ignore]\n228980 = false\n1234567 = true\n"
        )
        self.assertEqual(steam.load_ignore(path), {228980: False, 1234567: True})

    def test_file_errors_name_the_file(self):
        path = self.tmp / "config.toml"
        for text in ("[metadata.steam.ignore\n", "[metadata.steam.ignore]\nx = true\n"):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError) as caught:
                    steam.load_ignore(path)
                self.assertIn(str(path), str(caught.exception))

    def test_the_daemon_does_not_choke_on_it(self):
        # The section lives in the daemon's file. If config.parse ever starts
        # rejecting tables it does not know, a Steam ignore list would stop the
        # pad from working -- the one outcome this split exists to prevent.
        data = {"metadata": {"steam": {"ignore": {"228980": False}}}}
        self.assertEqual(config.parse(data), config.Config())


if __name__ == "__main__":
    unittest.main()
