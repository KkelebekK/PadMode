import unittest

from padmode.actions import ACTIONS, ActionContext, ActionRunner


def runner(installed=("playerctl", "wpctl"), volume_step=5):
    """A runner with a fake PATH and a fake spawner, so nothing is executed."""
    spawned = []
    run = ActionRunner(
        ActionContext(volume_step=volume_step),
        spawner=spawned.append,
        which=lambda binary: f"/usr/bin/{binary}" if binary in installed else None,
    )
    return run, spawned


class TestArgvBuilding(unittest.TestCase):
    def test_media_actions(self):
        run, _ = runner()
        self.assertEqual(run.argv_for("media.next"), ["playerctl", "next"])
        self.assertEqual(run.argv_for("media.play_pause"), ["playerctl", "play-pause"])

    def test_mute_actions_target_the_right_device(self):
        run, _ = runner()
        self.assertIn("@DEFAULT_SINK@", run.argv_for("audio.toggle_mute"))
        self.assertIn("@DEFAULT_SOURCE@", run.argv_for("mic.toggle_mute"))

    def test_volume_step_comes_from_config(self):
        run, _ = runner(volume_step=17)
        self.assertIn("17%+", run.argv_for("audio.volume_up"))
        self.assertIn("17%-", run.argv_for("audio.volume_down"))

    def test_volume_up_is_capped_but_volume_down_is_not(self):
        run, _ = runner()
        up = run.argv_for("audio.volume_up")
        self.assertEqual(up[up.index("-l") + 1], "1.0")
        self.assertNotIn("-l", run.argv_for("audio.volume_down"))

    def test_every_action_builds_argv_starting_with_its_binary(self):
        run, _ = runner()
        for name, spec in ACTIONS.items():
            with self.subTest(action=name):
                self.assertEqual(run.argv_for(name)[0], spec.binary)


class TestGracefulDegradation(unittest.TestCase):
    def test_missing_tool_disables_only_its_own_actions(self):
        # The promise from CLAUDE.md §4: no playerctl still means working volume.
        run, _ = runner(installed=("wpctl",))
        self.assertIsNone(run.argv_for("media.next"))
        self.assertIsNotNone(run.argv_for("audio.volume_up"))

    def test_running_a_disabled_action_is_a_no_op_not_a_crash(self):
        run, spawned = runner(installed=())
        self.assertFalse(run.run("media.next"))
        self.assertEqual(spawned, [])

    def test_missing_tool_is_warned_about_once(self):
        run, _ = runner(installed=())
        with self.assertLogs("padmode.actions.runner", level="WARNING") as captured:
            run.run("media.next")
            run.run("media.previous")
            run.run("media.play_pause")
        self.assertEqual(len(captured.records), 1)

    def test_probe_result_is_cached(self):
        calls = []

        def which(binary):
            calls.append(binary)
            return "/usr/bin/playerctl"

        run = ActionRunner(ActionContext(), spawner=lambda _a: None, which=which)
        run.available("playerctl")
        run.available("playerctl")
        self.assertEqual(calls, ["playerctl"])

    def test_unknown_action_does_not_raise(self):
        run, spawned = runner()
        with self.assertLogs("padmode.actions.runner", level="WARNING"):
            self.assertFalse(run.run("media.teleport"))
        self.assertEqual(spawned, [])

    def test_spawn_failure_is_contained(self):
        def boom(_argv):
            raise OSError("no such file")

        run = ActionRunner(ActionContext(), spawner=boom, which=lambda b: "/usr/bin/x")
        with self.assertLogs("padmode.actions.runner", level="WARNING"):
            self.assertFalse(run.run("media.next"))


class TestRawCommands(unittest.TestCase):
    def test_raw_command_is_spawned_verbatim(self):
        run, spawned = runner(installed=("notify-send",))
        self.assertTrue(run.run(["notify-send", "hello world"]))
        self.assertEqual(spawned, [["notify-send", "hello world"]])

    def test_raw_command_is_probed_by_its_first_word(self):
        run, spawned = runner(installed=())
        self.assertFalse(run.run(["definitely-not-installed", "x"]))
        self.assertEqual(spawned, [])


class TestPreflight(unittest.TestCase):
    def test_preflight_reports_each_disabled_binding(self):
        run, _ = runner(installed=("wpctl",))
        bindings = {"RB": "media.next", "LB": "media.previous", "X": "audio.toggle_mute"}
        with self.assertLogs("padmode.actions.runner", level="WARNING") as captured:
            run.preflight(bindings)
        self.assertEqual(len(captured.records), 2)

    def test_preflight_is_silent_when_everything_is_installed(self):
        run, _ = runner()
        with self.assertNoLogs("padmode.actions.runner", level="WARNING"):
            run.preflight({"RB": "media.next", "X": "audio.toggle_mute"})
