import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from death_breakdown import DeathBreakdown, TrackingConfigLoader, TrackingConfigurationError, TrackingSettings
import lop_bot as bot


def string(value, wide=False):
    return struct.pack("<i", (len(value) + 1) * (-1 if wide else 1)) + value.encode("utf-16-le" if wide else "ascii") + bytes(2 if wide else 1)


def name_tag(name, value, wide=False, guid=False):
    payload = string(value, wide)
    return string(name, wide) + string("NameProperty", wide) + struct.pack("<II", len(payload), 0) + bytes([guid]) + (bytes(16) if guid else b"") + payload


class LocationTests(unittest.TestCase):
    def test_real_format_name_tags_and_checkpoint(self):
        for wide in (False, True):
            for guid in (False, True):
                data = b"GVAS" + name_tag("LatestLocationName", "LD_Factory_Zone", wide, guid) + name_tag("RespawnTeleportObject", "LD_Factory_ControlRoom", wide, guid)
                self.assertEqual(bot.read_context(data), {"location": "LD_Factory_Zone", "checkpoint": "LD_Factory_ControlRoom"})

    def test_missing_or_corrupt_location_does_not_break_counter(self):
        self.assertEqual(bot.read_context(b"GVAS"), {"location": None, "checkpoint": None})
        corrupt = name_tag("LatestLocationName", "LD_Factory_Zone")[:-1]
        self.assertEqual(bot.read_context(b"GVAS" + corrupt)["location"], None)
        duplicated = name_tag("LatestLocationName", "LD_Factory_Zone") * 2
        self.assertIsNone(bot.read_context(b"GVAS" + duplicated)["location"])


class AttributionTests(unittest.TestCase):
    def test_historical_deaths_are_unknown_and_new_deaths_are_grouped(self):
        history = DeathBreakdown()
        settings = TrackingSettings()
        location = {"location": "LD_Factory_Zone", "checkpoint": "LD_Factory_ControlRoom"}
        history.observe(66, location, settings)
        self.assertEqual(history.entries, [])
        history.observe(67, location, settings)
        history.observe(67, location, settings)
        history.observe(68, location, settings)
        self.assertEqual(history.groups(), [(("Factory Zone", None), 2)])
        self.assertEqual(history.baseline, 66)
        self.assertEqual(history.entries[0]["location"], "LD_Factory_Zone")
        self.assertIn("Earlier deaths (section unknown): **66**", history.render())

    def test_manual_boss_and_section_overrides_do_not_change_old_entries(self):
        history = DeathBreakdown()
        location = {"location": "LD_Factory_Zone"}
        history.observe(0, location, TrackingSettings())
        history.observe(1, location, TrackingSettings())
        history.observe(2, location, TrackingSettings({"section_override": "Factory", "boss_override": "Current boss"}))
        groups = dict(history.groups())
        self.assertEqual(groups[("Factory Zone", None)], 1)
        self.assertEqual(groups[("Factory", "Current boss")], 1)
        self.assertEqual(history.entries[-1]["attribution"], "manual")

    def test_checkpoint_fallback_aliases_and_missing_context(self):
        settings = TrackingSettings({"location_labels": {"LD_Factory_Zone": "Factory"}})
        self.assertEqual(settings.context({"location": "LD_Factory_Zone"})["section"], "Factory")
        self.assertEqual(settings.context({"checkpoint": "LD_Factory_ControlRoom"})["section"], "Factory ControlRoom")
        self.assertEqual(settings.context(None)["section"], "Unknown section")

    def test_missed_counts_are_not_assigned_to_the_last_observed_area(self):
        history = DeathBreakdown()
        settings = TrackingSettings()
        history.observe(10, None, settings)
        history.observe(13, {"location": "LD_Factory_Zone"}, settings)
        self.assertEqual(history.groups(), [(("Unattributed (missed saves)", None), 3)])
        history.observe(15, None, TrackingSettings({"boss_override": "Current boss"}))
        self.assertEqual(history.entries[-1]["boss"], "Current boss")
        self.assertEqual(history.entries[-1]["last"] - history.entries[-1]["first"] + 1, 2)

    def test_restart_and_rollback_do_not_duplicate_numbered_deaths(self):
        settings = TrackingSettings()
        history = DeathBreakdown()
        history.observe(10, None, settings)
        history.observe(11, None, settings)
        restored = DeathBreakdown(json.loads(json.dumps(history.to_dict())))
        restored.observe(11, None, settings)
        restored.observe(5, None, settings)
        self.assertIn("Save rollback detected", restored.render())
        restored.observe(11, None, settings)
        self.assertEqual(len(restored.entries), 1)
        restored.observe(12, None, settings)
        self.assertEqual(sum(total for _, total in restored.groups()), 2)

    def test_invalid_persisted_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            DeathBreakdown({"baseline": 1, "high_water": 3, "current_count": 3, "entries": []})

    def test_report_respects_discord_limit_and_full_local_report_keeps_all_groups(self):
        history = DeathBreakdown()
        history.observe(0, None, TrackingSettings())
        for number in range(1, 101):
            history.observe(number, None, TrackingSettings({"section_override": f"Section {number} " + "*" * 50, "boss_override": "Test boss"}))
        short = history.render(limit=2000)
        self.assertLessEqual(len(short), 2000)
        self.assertIn("Other groups", short)
        self.assertIn("Section 100", history.render())
        self.assertNotIn("*" * 50, short)

    def test_config_changes_reload_without_restart_and_invalid_edits_pause_tracking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"tracking": {"boss_override": "Boss A"}}')
            loader = TrackingConfigLoader(path)
            self.assertEqual(loader.get().boss, "Boss A")
            path.write_text('{"tracking": {"boss_override": "Boss B longer"}}')
            self.assertEqual(loader.get().boss, "Boss B longer")
            path.write_text('{"tracking":')
            with self.assertRaises(TrackingConfigurationError):
                loader.get()
            path.write_text('{"tracking": {"boss_override": null}}')
            self.assertIsNone(loader.get().boss)


class BreakdownPublisherTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123456/testing_token"

    @patch.object(bot, "discord_request")
    def test_breakdown_is_created_once_then_edited_and_reused_after_restart(self, request):
        request.side_effect = [{"id": "100"}, {"id": "200"}]
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(66, tracking=settings)
            self.assertEqual(publisher.message_id, "100")
            self.assertEqual(publisher.breakdown_message_id, "200")
            request.reset_mock(side_effect=True)
            request.return_value = {"id": "unused"}
            self.assertFalse(publisher.publish(66, tracking=settings))
            request.assert_not_called()
            publisher.publish(67, location={"location": "LD_Factory_Zone"}, tracking=settings)
            self.assertEqual([call.args[0] for call in request.call_args_list], [self.URL + "/messages/100", self.URL + "/messages/200"])
            request.reset_mock()
            restarted = bot.Publisher(self.URL, state, "character1")
            restarted.publish(67, tracking=settings)
            self.assertTrue(all(call.args[1] == "PATCH" for call in request.call_args_list))
            self.assertEqual(restarted.breakdown.groups(), [(("Factory Zone", None), 1)])

    @patch.object(bot, "discord_request")
    def test_tracking_is_persisted_even_when_discord_is_offline(self, request):
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            request.side_effect = [{"id": "100"}, {"id": "200"}, bot.RetryLater(5, "offline")]
            publisher.publish(66, tracking=settings)
            with self.assertRaises(bot.RetryLater):
                publisher.publish(67, location={"location": "LD_Factory_Zone"}, tracking=settings)
            self.assertEqual(json.loads(state.read_text())["death_breakdown"]["high_water"], 67)
            request.reset_mock(side_effect=True)
            request.return_value = {"id": "unused"}
            restarted = bot.Publisher(self.URL, state, "character1")
            restarted.publish(67, tracking=settings)
            self.assertEqual(len(restarted.breakdown.entries), 1)

    @patch.object(bot, "discord_request")
    def test_failed_breakdown_update_is_retried_when_total_is_unchanged(self, request):
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            request.side_effect = [{"id": "100"}, {"id": "200"}, {"id": "100"}, bot.RetryLater(5, "offline")]
            publisher.publish(66, tracking=settings)
            with self.assertRaises(bot.RetryLater):
                publisher.publish(67, tracking=settings)
            request.reset_mock(side_effect=True)
            request.return_value = {"id": "200"}
            publisher.publish(67, tracking=settings)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[:2], (self.URL + "/messages/200", "PATCH"))

    @patch.object(bot, "discord_request")
    def test_deleted_breakdown_is_recreated_without_resetting_history(self, request):
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            publisher = bot.Publisher(self.URL, Path(temporary) / "state.json", "character1")
            request.side_effect = [{"id": "100"}, {"id": "200"}, {"id": "100"}, bot.MissingMessage(), {"id": "300"}]
            publisher.publish(66, tracking=settings)
            publisher.publish(67, tracking=settings)
            self.assertEqual(publisher.breakdown_message_id, "300")
            self.assertEqual(len(publisher.breakdown.entries), 1)
            self.assertEqual(publisher.message_id, "100")

    @patch.object(bot, "discord_request")
    def test_pending_counter_switch_and_milestones_keep_separate_message_ids(self, request):
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            request.side_effect = [{"id": "100"}, {"id": "200"}]
            publisher.publish(49, tracking=settings)
            publisher.queue_new_message(49)
            request.side_effect = [{"id": "101"}, {"id": "milestone"}, {"id": "200"}]
            publisher.publish(50, tracking=settings)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["message_id"], "101")
            self.assertEqual(saved["breakdown_message_id"], "200")
            self.assertEqual(saved["last_milestone"], 50)
            self.assertEqual(saved["death_breakdown"]["high_water"], 50)

    @patch.object(bot, "discord_request", return_value={"id": "100"})
    def test_webhook_change_preserves_history_but_not_old_message_ids(self, request):
        settings = TrackingSettings()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            publisher = bot.Publisher(self.URL, state, "character1")
            publisher.publish(66, tracking=settings)
            publisher.publish(67, location={"location": "LD_Factory_Zone"}, tracking=settings)
            changed_hook = bot.Publisher(self.URL + "_new", state, "character1")
            self.assertIsNone(changed_hook.message_id)
            self.assertIsNone(changed_hook.breakdown_message_id)
            self.assertEqual(changed_hook.breakdown.groups(), [(("Factory Zone", None), 1)])
            other_character = bot.Publisher(self.URL, state, "character2")
            self.assertIsNone(other_character.breakdown.baseline)


if __name__ == "__main__":
    unittest.main()
