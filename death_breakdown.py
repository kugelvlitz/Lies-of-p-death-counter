"""Persistent death attribution and Discord rendering; no game or network writes."""

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re


class TrackingConfigurationError(ValueError):
    pass


def _label(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or any(ord(c) < 32 for c in value):
        raise TrackingConfigurationError(f"tracking.{field} must be null or a nonempty label of at most 100 characters.")
    return value.strip()


class TrackingSettings:
    def __init__(self, data=None):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise TrackingConfigurationError("tracking must be a JSON object.")
        self.section = _label(data.get("section_override"), "section_override")
        self.boss = _label(data.get("boss_override"), "boss_override")
        labels = data.get("location_labels", {})
        if not isinstance(labels, dict) or any(not isinstance(key, str) or not key for key in labels):
            raise TrackingConfigurationError("tracking.location_labels must map location IDs to labels.")
        self.labels = {key: _label(value, "location_labels") for key, value in labels.items()}
        if any(value is None for value in self.labels.values()):
            raise TrackingConfigurationError("Location labels cannot be null.")

    def context(self, location):
        location = location or {}
        raw = location.get("location") or location.get("checkpoint")
        automatic = self.labels.get(raw)
        if not automatic and raw:
            automatic = re.sub(r"^(LD_|LV_)", "", raw).replace("_", " ")
        return {
            "section": self.section or automatic or "Unknown section",
            "boss": self.boss,
            "location": location.get("location"),
            "checkpoint": location.get("checkpoint"),
            "attribution": "manual" if self.section or self.boss else ("auto" if raw else "unknown"),
        }


class TrackingConfigLoader:
    """Reload only the tracking settings, validating edits before using them."""
    def __init__(self, path):
        self.path = Path(path)
        self.signature = None
        self.settings = None

    def get(self):
        def signature():
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size, stat.st_ino
        before = signature()
        if before != self.signature:
            try:
                config = json.loads(self.path.read_text())
                settings = TrackingSettings(config.get("tracking"))
            except (ValueError, AttributeError):
                raise TrackingConfigurationError("Invalid tracking configuration; waiting for config.json to be corrected.") from None
            if before != signature():
                raise TrackingConfigurationError("Configuration is being edited; waiting.")
            self.settings = settings
            self.signature = before
        return self.settings


def _escape(text):
    text = str(text).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", text)


class DeathBreakdown:
    def __init__(self, state=None):
        self.baseline = None
        self.high_water = None
        self.current_count = None
        self.entries = []
        if state is not None:
            try:
                self.baseline = state["baseline"]
                self.high_water = state["high_water"]
                self.current_count = state["current_count"]
                self.entries = state["entries"]
                if not isinstance(self.entries, list):
                    raise ValueError
                if self.baseline is None:
                    if self.high_water is not None or self.current_count is not None or self.entries:
                        raise ValueError
                else:
                    if any(type(value) is not int or value < 0 for value in (self.baseline, self.high_water, self.current_count)):
                        raise ValueError
                    end = self.baseline
                    for entry in self.entries:
                        if type(entry["first"]) is not int or type(entry["last"]) is not int:
                            raise ValueError
                        if entry["first"] != end + 1 or entry["last"] < entry["first"]:
                            raise ValueError
                        if not isinstance(entry["section"], str) or not entry["section"]:
                            raise ValueError
                        if entry["boss"] is not None and not isinstance(entry["boss"], str):
                            raise ValueError
                        end = entry["last"]
                    if end != self.high_water:
                        raise ValueError
                    if self.current_count > self.high_water:
                        raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ValueError("Unreadable death breakdown state; restore the state file instead of resetting history.") from None

    def to_dict(self):
        return {"baseline": self.baseline, "high_water": self.high_water,
                "current_count": self.current_count, "entries": self.entries}

    def observe(self, count, location, settings):
        if type(count) is not int or count < 0:
            raise ValueError("Death count must be a nonnegative integer.")
        if self.baseline is None:
            self.baseline = self.high_water = self.current_count = count
            return True
        changed = self.current_count != count
        self.current_count = count
        if count > self.high_water:
            context = settings.context(location)
            # A single snapshot cannot establish where several missed deaths
            # happened. Manual labels can explicitly assign such a batch.
            if count - self.high_water > 1 and context["attribution"] != "manual":
                context.update(section="Unattributed (missed saves)", boss=None, attribution="missed")
            self.entries.append({
                "first": self.high_water + 1,
                "last": count,
                **context,
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            self.high_water = count
            changed = True
        return changed

    def groups(self):
        totals = defaultdict(int)
        for entry in self.entries:
            totals[(entry["section"], entry["boss"])] += entry["last"] - entry["first"] + 1
        return sorted(totals.items(), key=lambda item: (-item[1], item[0][0], item[0][1] or ""))

    def render(self, limit=None):
        if self.baseline is None:
            return "**Lies of P — Death breakdown**\nWaiting for a valid save."
        lines = [
            "**Lies of P — Death breakdown**",
            f"Saved total: **{self.current_count}**",
            f"Earlier deaths (section unknown): **{self.baseline}**",
            f"Recorded since tracking began: **{self.high_water - self.baseline}**",
            "",
            "**Section / boss**",
        ]
        rows = self.groups()
        note = "\n\nAutomatic sections use the saved location. Boss names require a config override."
        if self.current_count < self.high_water:
            note += f"\nSave rollback detected; history retains deaths through {self.high_water}."
        if not rows:
            lines.append("No new deaths recorded yet.")
        for index, ((section, boss), total) in enumerate(rows):
            row = f"• {_escape(section)} · {_escape(boss) if boss else 'Boss unspecified'}: **{total}**"
            # Reserve room for an aggregate of any omitted rows and the note.
            if limit is not None and len("\n".join(lines + [row])) + len(note) + 150 > limit:
                remaining = rows[index:]
                lines.append(f"• Other groups: **{sum(value for _, value in remaining)}** deaths across {len(remaining)} groups.")
                lines.append("Full breakdown: run python3 lop_bot.py --breakdown locally.")
                break
            lines.append(row)
        return "\n".join(lines) + note
