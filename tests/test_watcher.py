import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bambu_watcher import (
    Detection,
    PersistentState,
    PrintStateMachine,
    audio_path_from_config,
    classify,
    load_state,
    normalize_text,
    phrase_score,
    save_state,
    validate_audio_file,
)


class StateMachineTests(unittest.TestCase):
    def make_machine(self, initial="waiting", matches=2):
        self.alerts = 0
        self.saved = []

        def alert():
            self.alerts += 1

        machine = PrintStateMachine(
            PersistentState(initial),
            matches,
            alert,
            lambda state: self.saved.append((state.state, state.finished_streak)),
            logging.getLogger("test"),
        )
        return machine

    def test_finished_does_not_alert_until_printing_was_seen(self):
        machine = self.make_machine()
        machine.observe("finished")
        machine.observe("finished")
        self.assertEqual(self.alerts, 0)
        self.assertEqual(machine.data.state, "waiting")

    def test_two_finished_matches_alert_once(self):
        machine = self.make_machine()
        machine.observe("printing")
        machine.observe("finished")
        self.assertEqual(self.alerts, 0)
        machine.observe("finished")
        self.assertEqual(self.alerts, 1)
        self.assertEqual(machine.data.state, "finished")
        machine.observe("finished")
        machine.observe("finished")
        self.assertEqual(self.alerts, 1)

    def test_new_print_rearms_after_finished(self):
        machine = self.make_machine(initial="finished")
        machine.observe("printing")
        machine.observe("finished")
        machine.observe("finished")
        self.assertEqual(self.alerts, 1)

    def test_unreadable_breaks_completion_streak_without_disarming(self):
        machine = self.make_machine()
        machine.observe("printing")
        machine.observe("finished")
        machine.observe("unreadable")
        self.assertEqual(machine.data.state, "printing")
        self.assertEqual(machine.data.finished_streak, 0)
        machine.observe("finished")
        self.assertEqual(self.alerts, 0)


class MatchingTests(unittest.TestCase):
    CONFIG = {
        "active_phrase": "Printing",
        "completion_phrase": "Finished",
        "similarity_threshold": 80,
    }

    def test_normalization(self):
        self.assertEqual(normalize_text("  FINISHED!\n"), "finished")

    def test_fuzzy_matching_tolerates_one_bad_character(self):
        self.assertGreaterEqual(phrase_score("Status: Fin1shed", "Finished"), 80)

    @patch("bambu_watcher.phrase_score")
    def test_classification_uses_scores(self, score):
        score.side_effect = [20, 95]
        category, _, _ = classify(Detection("Fin1shed", 80, True), self.CONFIG)
        self.assertEqual(category, "finished")

    def test_low_confidence_is_unreadable(self):
        category, _, _ = classify(Detection("Finished", 10, False), self.CONFIG)
        self.assertEqual(category, "unreadable")


class PersistenceTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, PersistentState("printing", 1))
            loaded = load_state(path, logging.getLogger("test"))
            self.assertEqual(loaded, PersistentState("printing", 1))
            self.assertEqual(json.loads(path.read_text())["state"], "printing")


class AudioConfigurationTests(unittest.TestCase):
    def test_mp3_and_wav_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("alert.mp3", "alert.wav"):
                path = Path(directory) / filename
                path.touch()
                validate_audio_file(path)

    def test_unsupported_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert.ogg"
            path.touch()
            with self.assertRaisesRegex(ValueError, "wav or .mp3"):
                validate_audio_file(path)

    def test_legacy_wav_path_remains_compatible(self):
        config_path = Path("C:/watcher/config.json")
        resolved = audio_path_from_config({"wav_path": "alert.wav"}, config_path)
        self.assertEqual(resolved, Path("C:/watcher/alert.wav"))


if __name__ == "__main__":
    unittest.main()
