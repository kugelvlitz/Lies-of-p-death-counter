import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import lop_bot as bot


def string(value, wide=False):
    width = 2 if wide else 1
    size = (len(value) + 1) * (-1 if wide else 1)
    return struct.pack("<i", size) + value.encode("utf-16-le" if wide else "ascii") + bytes(width)


def save(count, *, kind="IntProperty", guid=False, wide=False):
    fmt = "<q" if kind == "Int64Property" else "<i"
    value = struct.pack(fmt, count)
    # A minimal synthetic container; not a real game save.
    return (b"GVAS" + string("YouDieCount", wide) + string(kind, wide)
            + struct.pack("<II", len(value), 0) + bytes([guid])
            + (bytes(16) if guid else b"") + value + string("None"))


class SaveReaderTests(unittest.TestCase):
    def test_integer_types_guid_and_encodings(self):
        for kind in ("IntProperty", "Int64Property"):
            for guid in (False, True):
                for wide in (False, True):
                    with self.subTest(kind=kind, guid=guid, wide=wide):
                        self.assertEqual(bot.read_deaths(save(42, kind=kind, guid=guid, wide=wide)), 42)
        self.assertEqual(bot.read_deaths(save(0)), 0)

    def test_missing_corrupt_and_ambiguous_values_do_not_become_zero(self):
        good = save(7)
        broken = [b"", b"not a save", b"GVAS", good[:-len(string("None")) - 1],
                  save(-1), good + good[4:]]
        for data in broken:
            with self.subTest(data=data):
                with self.assertRaises(bot.SaveUnavailable):
                    bot.read_deaths(data)

    def test_tag_size_and_guid_are_validated(self):
        original = save(3)
        position = 4 + len(string("YouDieCount")) + len(string("IntProperty"))
        for index, value in ((position, 8), (position + 4, 1), (position + 8, 2)):
            data = bytearray(original)
            data[index] = value
            with self.assertRaises(bot.SaveUnavailable):
                bot.read_deaths(data)

    @patch.object(bot.time, "sleep")
    def test_follows_alternating_buffers_and_ignores_other_characters(self, _sleep):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = directory / "SaveData-1_Character_1.sav"
            second = directory / "SaveData-1_Character_2.sav"
            other = directory / "SaveData-2_Character_1.sav"
            for path, count, stamp in ((first, 3, 10), (second, 4, 20), (other, 500, 30)):
                path.write_bytes(save(count))
                os.utime(path, (stamp, stamp))
            self.assertEqual(bot.sample(directory, 1), (second, 4))
            first.write_bytes(save(5))
            os.utime(first, (40, 40))
            self.assertEqual(bot.sample(directory, 1), (first, 5))

    @patch.object(bot.time, "sleep")
    def test_in_progress_write_is_not_published(self, _sleep):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "SaveData-1_Character_1.sav"
            path.write_bytes(save(5))
            with patch.object(bot, "signature", side_effect=[(1, 100, 1), (2, 100, 1)]):
                with self.assertRaises(bot.SaveUnavailable):
                    bot.sample(directory, 1)


class PublisherTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123456/testing_token"

    @patch.object(bot, "discord_request")
    def test_create_edit_restart_and_no_duplicate_updates(self, request):
        request.return_value = {"id": "456"}
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "config.state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            self.assertTrue(publisher.publish(1))
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertFalse(publisher.publish(1))
            self.assertEqual(request.call_count, 1)
            self.assertTrue(publisher.publish(2))
            self.assertEqual(request.call_args.args[1], "PATCH")
            self.assertEqual(request.call_args.args[2]["allowed_mentions"], {"parse": []})
            restarted = bot.Publisher(self.URL, state, "character1")
            restarted.publish(2)
            self.assertEqual(request.call_args.args[1], "PATCH")
            self.assertNotIn("testing_token", state.read_text())
            # The absolute save count can legitimately decrease after a restore.
            restarted.publish(0)
            self.assertIn("Deaths: 0", request.call_args.args[2]["content"])

    @patch.object(bot, "discord_request")
    def test_deleted_message_is_recreated(self, request):
        request.side_effect = [{"id": "456"}, bot.MissingMessage(), {"id": "789"}]
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            publisher.publish(1)
            publisher.publish(2)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["POST", "PATCH", "POST"])
            self.assertEqual(publisher.message_id, "789")

    @patch.object(bot, "discord_request")
    def test_failed_update_is_retried_and_latest_total_sent(self, request):
        request.side_effect = [{"id": "456"}, bot.RetryLater(5, "offline"), {"id": "456"}]
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            publisher.publish(1)
            with self.assertRaises(bot.RetryLater):
                publisher.publish(2)
            self.assertEqual(publisher.last_count, 1)
            publisher.publish(3)
            self.assertIn("Deaths: 3", request.call_args.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_new_source_does_not_reuse_other_characters_message(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            bot.Publisher(self.URL, state, "character1").publish(9)
            bot.Publisher(self.URL, state, "character2").publish(1)
            self.assertEqual(request.call_args.args[1], "POST")

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_only_multiples_of_fifty_create_announcements(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            publisher.publish(49)
            request.reset_mock()
            for count in (50, 50, 51, 99, 100, 100):
                publisher.publish(count)
            methods = [call.args[1] for call in request.call_args_list]
            self.assertEqual(methods, ["PATCH", "POST", "PATCH", "PATCH", "PATCH", "POST"])
            posts = [call.args[2] for call in request.call_args_list if call.args[1] == "POST"]
            self.assertIn("**50 deaths!**", posts[0]["content"])
            self.assertIn("**100 deaths!**", posts[1]["content"])
            for payload in posts:
                self.assertEqual(payload["allowed_mentions"], {"parse": []})

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_skipped_thresholds_are_announced_in_order(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            publisher.publish(49)
            request.reset_mock()
            publisher.publish(151)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["PATCH", "POST", "POST", "POST"])
            for call, threshold in zip(request.call_args_list[1:], (50, 100, 150)):
                self.assertIn(f"**{threshold} deaths!**", call.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_restart_and_save_rollback_do_not_repeat_milestones(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(1)
            publisher.publish(100)
            request.reset_mock()
            restarted = bot.Publisher(self.URL, state, "character1")
            for count in (100, 20, 50, 100, 149):
                restarted.publish(count)
            self.assertTrue(all(call.args[1] == "PATCH" for call in request.call_args_list))
            restarted.publish(150)
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertIn("**150 deaths!**", request.call_args.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_failed_milestone_retries_even_when_counter_is_unchanged(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(49)
            request.side_effect = [{"id": "456"}, bot.RetryLater(5, "offline")]
            with self.assertRaises(bot.RetryLater):
                publisher.publish(50)
            self.assertEqual(publisher.last_count, 50)
            self.assertEqual(json.loads(state.read_text())["last_milestone"], 0)
            request.reset_mock(side_effect=True)
            self.assertTrue(publisher.publish(50))
            request.assert_called_once()
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertFalse(publisher.publish(50))
            self.assertEqual(json.loads(state.read_text())["last_milestone"], 50)

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_partial_catchup_resumes_after_restart_without_repeating_posts(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(1)
            request.side_effect = [{"id": "456"}, {"id": "457"}, bot.RetryLater(5, "offline")]
            with self.assertRaises(bot.RetryLater):
                publisher.publish(151)
            self.assertEqual(json.loads(state.read_text())["last_milestone"], 50)
            request.reset_mock(side_effect=True)
            bot.Publisher(self.URL, state, "character1").publish(151)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["PATCH", "POST", "POST"])
            for call, threshold in zip(request.call_args_list[1:], (100, 150)):
                self.assertIn(f"**{threshold} deaths!**", call.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_initial_sync_and_old_state_do_not_announce_past_milestones(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(127)
            request.assert_called_once()
            self.assertEqual(publisher.last_milestone, 100)
            # Migrate a state file created by the previous version of the bot.
            state.write_text(json.dumps({"key": publisher.key, "message_id": "456"}))
            request.reset_mock()
            migrated = bot.Publisher(self.URL, state, "character1")
            migrated.publish(127)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[1], "PATCH")
            migrated.publish(150)
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertIn("**150 deaths!**", request.call_args.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_initial_outage_preserves_baseline_for_later_crossing(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            request.side_effect = bot.RetryLater(5, "offline")
            with self.assertRaises(bot.RetryLater):
                publisher.publish(49)
            request.reset_mock(side_effect=True)
            bot.Publisher(self.URL, state, "character1").publish(51)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["POST", "POST"])
            self.assertIn("**50 deaths!**", request.call_args.args[2]["content"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_next_death_creates_one_new_counter_then_edits_it_after_restart(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(12)
            request.reset_mock()
            publisher.queue_new_message(12)
            request.assert_not_called()
            waiting = bot.Publisher(self.URL, state, "character1")
            self.assertFalse(waiting.publish(12))
            request.assert_not_called()
            request.return_value = {"id": "789"}
            waiting.publish(13)
            self.assertEqual(request.call_args.args[1], "POST")
            self.assertEqual(json.loads(state.read_text())["message_id"], "789")
            self.assertIsNone(json.loads(state.read_text())["new_message_after"])
            waiting.publish(14)
            self.assertEqual(request.call_args.args[:2], (self.URL + "/messages/789", "PATCH"))
            restarted = bot.Publisher(self.URL, state, "character1")
            restarted.publish(14)
            self.assertEqual(request.call_args.args[:2], (self.URL + "/messages/789", "PATCH"))
            self.assertEqual(sum(call.args[1] == "POST" for call in request.call_args_list), 1)

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_pending_new_message_survives_failure_and_keeps_milestone_progress(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(99)
            publisher.queue_new_message(99)
            request.side_effect = bot.RetryLater(5, "offline")
            with self.assertRaises(bot.RetryLater):
                publisher.publish(100)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["message_id"], "456")
            self.assertEqual(saved["new_message_after"], 99)
            self.assertEqual(saved["last_milestone"], 50)
            request.reset_mock(side_effect=True)
            request.side_effect = [{"id": "789"}, {"id": "790"}]
            bot.Publisher(self.URL, state, "character1").publish(100)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["POST", "POST"])
            saved = json.loads(state.read_text())
            self.assertEqual(saved["message_id"], "789")
            self.assertEqual(saved["last_milestone"], 100)
            self.assertIsNone(saved["new_message_after"])

    @patch.object(bot, "discord_request", return_value={"id": "456"})
    def test_pending_new_message_waits_for_count_to_increase(self, request):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(12)
            publisher.queue_new_message(12)
            request.reset_mock()
            for count in (12, 10, 11, 12):
                self.assertFalse(publisher.publish(count))
            request.assert_not_called()
            publisher.publish(14)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[1], "POST")

    @patch.object(bot, "urlopen")
    def test_rate_limit_retry_after_and_network_recovery(self, open_url):
        open_url.side_effect = HTTPError(self.URL, 429, "rate limit", {}, io.BytesIO(b'{"retry_after": 2.5}'))
        with self.assertRaises(bot.RetryLater) as raised:
            bot.discord_request(self.URL, "POST", {})
        self.assertEqual(raised.exception.seconds, 2.5)
        open_url.side_effect = URLError("sensitive URL must not be printed")
        with self.assertRaises(bot.RetryLater) as raised:
            bot.discord_request(self.URL, "POST", {})
        self.assertNotIn("sensitive", str(raised.exception))

    @patch.object(bot, "urlopen")
    def test_deleted_webhook_stops_instead_of_recreating_forever(self, open_url):
        open_url.side_effect = HTTPError(self.URL, 404, "unknown webhook", {}, io.BytesIO(b'{"code": 10015}'))
        with self.assertRaises(ValueError) as raised:
            bot.discord_request(self.URL, "PATCH", {})
        self.assertNotIn("testing_token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
