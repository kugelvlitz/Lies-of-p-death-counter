#!/usr/bin/env python3
"""Read Lies of P saves and maintain a Discord counter and death breakdown.

Python 3.10+, standard library only. See README.md for setup and format sources.
Game saves are opened only for reading; only config-adjacent state is written.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from death_breakdown import DeathBreakdown, TrackingConfigLoader, TrackingConfigurationError


APP_DIR = Path(__file__).resolve().parent
MILESTONE_STEP = 50


class SaveUnavailable(ValueError):
    """No reliable counter can currently be read."""


class RetryLater(Exception):
    def __init__(self, seconds, message):
        self.seconds = seconds
        super().__init__(message)


class MissingMessage(Exception):
    pass


def fstring(data, offset):
    """Decode a bounded Unreal FString (ANSI or UTF-16-LE)."""
    try:
        length = struct.unpack_from("<i", data, offset)[0]
        if not 0 < abs(length) <= 512:
            raise ValueError("Invalid string length")
        width = 1 if length > 0 else 2
        end = offset + 4 + abs(length) * width
        if end > len(data) or data[end - width:end] != bytes(width):
            raise ValueError("Incomplete string")
        value = data[offset + 4:end - width].decode("ascii" if width == 1 else "utf-16-le")
        return value, end
    except (ValueError, struct.error) as exc:
        raise SaveUnavailable("Incomplete or unsupported save string.") from exc


def read_deaths(data):
    """Read the named integer tag, validating its size, index and GUID flag.

    This deliberately supports tagged, uncompressed GVAS only, and never guesses
    zero if the field is absent. It is a targeted reader, not a full GVAS parser.
    """
    if not data.startswith(b"GVAS"):
        raise SaveUnavailable("Not an uncompressed GVAS save; unsupported format.")
    values = []
    for name in ("YouDieCount", "YouDieCount_0"):
        for encoding, width in (("ascii", 1), ("utf-16-le", 2)):
            encoded = name.encode(encoding) + bytes(width)
            prefix = struct.pack("<i", len(name) + 1 if width == 1 else -(len(name) + 1))
            needle = prefix + encoded
            offset = 0
            while (offset := data.find(needle, offset)) != -1:
                position = offset + len(needle)
                offset += 1
                kind, position = fstring(data, position)
                formats = {"IntProperty": (4, "<i"), "Int64Property": (8, "<q")}
                if kind not in formats:
                    raise SaveUnavailable("Unsupported death-counter property type.")
                size, fmt = formats[kind]
                if position + 9 > len(data):
                    raise SaveUnavailable("Incomplete death-counter tag.")
                payload_size, array_index = struct.unpack_from("<II", data, position)
                flag = data[position + 8]
                if payload_size != size or array_index != 0 or flag not in (0, 1):
                    raise SaveUnavailable("Unsupported death-counter tag layout.")
                position += 9 + (16 if flag else 0)
                if position + size > len(data):
                    raise SaveUnavailable("Incomplete death-counter value.")
                value = struct.unpack_from(fmt, data, position)[0]
                if value < 0:
                    raise SaveUnavailable("Invalid negative death count.")
                values.append(value)
    if len(values) != 1:
        raise SaveUnavailable(
            "Death field absent or ambiguous. A new save may not store it until "
            "the first death; otherwise this save format needs inspection."
        )
    return values[0]


def read_name_property(data, name):
    """Read an optional named FString tag without treating other save strings as tags."""
    values = []
    for encoding, width in (("ascii", 1), ("utf-16-le", 2)):
        needle = struct.pack("<i", (len(name) + 1) * (1 if width == 1 else -1)) + name.encode(encoding) + bytes(width)
        offset = 0
        while (offset := data.find(needle, offset)) != -1:
            position = offset + len(needle)
            offset += 1
            kind, position = fstring(data, position)
            if kind not in ("NameProperty", "StrProperty") or position + 9 > len(data):
                raise SaveUnavailable("Unsupported location property tag.")
            size, index = struct.unpack_from("<II", data, position)
            flag = data[position + 8]
            if index != 0 or flag not in (0, 1):
                raise SaveUnavailable("Unsupported location property layout.")
            position += 9 + (16 if flag else 0)
            if position + size > len(data):
                raise SaveUnavailable("Incomplete location property.")
            value, end = fstring(data, position)
            if end != position + size:
                raise SaveUnavailable("Invalid location property size.")
            values.append(value)
    if len(values) > 1:
        raise SaveUnavailable("Ambiguous location property.")
    return values[0] if values and values[0] not in ("", "None") else None


def read_context(data):
    context = {}
    for key, name in (("location", "LatestLocationName"), ("checkpoint", "RespawnTeleportObject")):
        try:
            context[key] = read_name_property(data, name)
        except SaveUnavailable:
            # Unsupported location metadata must not interrupt the death counter.
            context[key] = None
    return context


def signature(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def latest_save(directory, slot):
    files = list(directory.glob(f"SaveData-{slot}_Character_[12].sav"))
    if not files:
        raise SaveUnavailable(f"No character saves for slot {slot} in {directory}.")
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def sample(directory, slot, *, include_context=False):
    """Skip writes in progress and retry on the next polling cycle."""
    path = latest_save(directory, slot)
    before = signature(path)
    time.sleep(0.15)
    if signature(path) != before:
        raise SaveUnavailable("Save is being written; waiting.")
    data = path.read_bytes()
    if signature(path) != before or latest_save(directory, slot) != path:
        raise SaveUnavailable("Save changed during reading; waiting.")
    count = read_deaths(data)
    if include_context:
        return path, count, read_context(data)
    return path, count


def discover():
    """Search only known Steam library save locations, never Proton symlink trees."""
    roots = {
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    }
    for root in list(roots):
        try:
            text = (root / "steamapps/libraryfolders.vdf").read_text()
        except OSError:
            continue
        roots.update(Path(path.replace("\\\\", "\\")) for path in re.findall(r'"path"\s+"([^"]+)"', text))
    directories = set()
    for root in roots:
        base = root / "steamapps/common/Lies of P/LiesofP/Saved/SaveGames"
        if base.is_dir():
            for save in base.glob("*/SaveData-*_Character_[12].sav"):
                directories.add(save.parent.resolve())
    return sorted(directories)


def discord_request(url, method, payload):
    request = Request(url, data=json.dumps(payload).encode(), method=method, headers={
        "Content-Type": "application/json",
        "User-Agent": "LiesOfPDeathCounter/1.0",
    })
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (ValueError, UnicodeError):
            body = {}
        finally:
            exc.close()
        if exc.code == 429:
            try:
                delay = float(body.get("retry_after", 5))
            except (TypeError, ValueError):
                delay = 5
            if not math.isfinite(delay):
                delay = 5
            raise RetryLater(max(1, delay), "Discord rate limit; waiting before retry.") from None
        if exc.code == 404 and body.get("code") == 10008:
            raise MissingMessage from None
        if exc.code >= 500:
            raise RetryLater(15, "Discord server error; retrying.") from None
        # Do not print exceptions or response bodies: they can contain the secret URL.
        raise ValueError(f"Discord HTTP {exc.code}; check the webhook and channel permissions.") from None
    except (URLError, TimeoutError, OSError):
        raise RetryLater(15, "Cannot reach Discord; retrying.") from None


class Publisher:
    def __init__(self, url, state_path, source):
        if not isinstance(url, str) or not re.fullmatch(
            r"https://discord\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+", url
        ):
            raise ValueError("Set webhook_url to a copied Discord webhook URL in config.json.")
        self.url = url
        self.state_path = state_path
        self.key = hashlib.sha256((url + "|" + source).encode()).hexdigest()
        self.tracking_source_key = hashlib.sha256(source.encode()).hexdigest()
        self.message_id = None
        self.last_count = None
        self.last_milestone = None
        self.new_message_after = None
        self.breakdown_message_id = None
        self.breakdown = DeathBreakdown()
        self.last_breakdown_payload = None
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                message_id = state.get("message_id", "")
                if state.get("key") == self.key:
                    if re.fullmatch(r"\d+", str(message_id)):
                        self.message_id = str(message_id)
                    milestone = state.get("last_milestone")
                    if milestone is not None:
                        if type(milestone) is not int or milestone < 0 or milestone % MILESTONE_STEP:
                            raise ValueError("Invalid milestone state")
                        self.last_milestone = milestone
                    new_message_after = state.get("new_message_after")
                    if new_message_after is not None:
                        if type(new_message_after) is not int or new_message_after < 0:
                            raise ValueError("Invalid pending message state")
                        self.new_message_after = new_message_after
                    breakdown_message_id = state.get("breakdown_message_id")
                    if re.fullmatch(r"\d+", str(breakdown_message_id)):
                        self.breakdown_message_id = str(breakdown_message_id)
                if state.get("key") == self.key or state.get("tracking_source_key") == self.tracking_source_key:
                    self.breakdown = DeathBreakdown(state.get("death_breakdown"))
            except (ValueError, AttributeError):
                raise ValueError("Unreadable state file; see README troubleshooting.") from None

    def save_state(self):
        state = {
            "key": self.key,
            "message_id": self.message_id,
            "last_milestone": self.last_milestone,
            "new_message_after": self.new_message_after,
            "breakdown_message_id": self.breakdown_message_id,
            "death_breakdown": self.breakdown.to_dict(),
            "tracking_source_key": self.tracking_source_key,
        }
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        os.replace(temporary, self.state_path)

    def queue_new_message(self, count):
        self.new_message_after = count
        self.save_state()

    def publish_breakdown(self):
        payload = {"content": self.breakdown.render(limit=2000), "allowed_mentions": {"parse": []}}
        if payload == self.last_breakdown_payload:
            return False
        if self.breakdown_message_id:
            try:
                discord_request(f"{self.url}/messages/{self.breakdown_message_id}", "PATCH", payload)
            except MissingMessage:
                self.breakdown_message_id = None
        if not self.breakdown_message_id:
            result = discord_request(self.url + "?wait=true", "POST", payload)
            self.breakdown_message_id = str(result["id"])
        self.save_state()
        self.last_breakdown_payload = payload
        return True

    def publish(self, count, *, location=None, tracking=None):
        if tracking is not None and self.breakdown.observe(count, location, tracking):
            # Save the observed deaths before any Discord requests, including
            # when the webhook is offline or an update is rate limited.
            self.save_state()
        # Start from the first observed count, including when upgrading old
        # state. Persist before networking so an outage cannot lose crossings.
        if self.last_milestone is None:
            self.last_milestone = count // MILESTONE_STEP * MILESTONE_STEP
            self.save_state()
        updated = False
        replace_message = self.new_message_after is not None and count > self.new_message_after
        if count != self.last_count and (self.new_message_after is None or replace_message):
            payload = {
                "content": f"**Lies of P — First playthrough**\n💀 **Deaths: {count}**",
                "allowed_mentions": {"parse": []},
            }
            if self.message_id and not replace_message:
                try:
                    discord_request(f"{self.url}/messages/{self.message_id}", "PATCH", payload)
                except MissingMessage:
                    self.message_id = None
            if not self.message_id or replace_message:
                result = discord_request(self.url + "?wait=true", "POST", payload)
                self.message_id = str(result["id"])
                self.new_message_after = None
            self.save_state()
            self.last_count = count
            updated = True
        # Check even if the scoreboard is unchanged: a previous milestone POST
        # may have failed after the scoreboard was successfully edited.
        while self.last_milestone + MILESTONE_STEP <= count:
            milestone = self.last_milestone + MILESTONE_STEP
            discord_request(self.url + "?wait=true", "POST", {
                "content": f"🎭 💀 **{milestone} deaths!**\nKrat claims another 50. The first playthrough continues!",
                "allowed_mentions": {"parse": []},
            })
            self.last_milestone = milestone
            self.save_state()
            updated = True
        if tracking is not None:
            updated = self.publish_breakdown() or updated
        return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=APP_DIR / "config.json")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--discover", action="store_true", help="list Steam save directories, then exit")
    actions.add_argument("--check", action="store_true", help="read the count once without contacting Discord")
    actions.add_argument("--breakdown", action="store_true", help="print the full recorded breakdown without contacting Discord")
    actions.add_argument("--new-message-next-death", action="store_true",
                         help="queue a new counter message for the next death, then exit; stop the watcher first")
    args = parser.parse_args()
    if args.discover:
        directories = discover()
        for directory in directories:
            print(directory)
        if not directories:
            print("No saves found. Start a character first; see README for custom install paths.")
        return 0
    if args.breakdown:
        try:
            state = json.loads(args.config.resolve().with_suffix(".state.json").read_text())
            print(DeathBreakdown(state.get("death_breakdown")).render())
            return 0
        except (OSError, ValueError, AttributeError):
            print("Cannot read recorded breakdown. Check the state file.", file=sys.stderr)
            return 1
    try:
        config = json.loads(args.config.read_text())
        directory = Path(config["save_dir"]).expanduser().resolve()
        slot = config.get("character_slot", 1)
        interval = config.get("poll_seconds", 1)
        if type(slot) is not int or slot < 1:
            raise ValueError("character_slot must be a positive integer.")
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 1:
            raise ValueError("poll_seconds must be a finite number of at least 1.")
        if not directory.is_dir():
            raise ValueError("save_dir must point to the numbered folder containing your character saves.")
        if args.check:
            path, count, location = sample(directory, slot, include_context=True)
            print(f"{path.name}: {count} deaths")
            print(f"Saved location: {location['location'] or 'unavailable'}")
            print(f"Checkpoint: {location['checkpoint'] or 'unavailable'}")
            return 0
        state_path = args.config.resolve().with_suffix(".state.json")
        # Avoid accidentally placing any writable application state in the save tree.
        if directory == state_path.parent or directory in state_path.parents:
            raise ValueError("Keep config.json outside the game save directory.")
        publisher = Publisher(config.get("webhook_url"), state_path, f"{directory}|{slot}")
        if args.new_message_next_death:
            path, count = sample(directory, slot)
            publisher.queue_new_message(count)
            print(f"New counter message queued for the next death after {count} ({path.name}).")
            return 0
        print(f"Watching character {slot} in {directory}. Stop with Ctrl+C.", flush=True)
        tracking_config = TrackingConfigLoader(args.config)
        previous_warning = None
        while True:
            delay = interval
            try:
                settings = tracking_config.get()
                path, count, location = sample(directory, slot, include_context=True)
                if publisher.publish(count, location=location, tracking=settings):
                    print(f"Discord updated: {count} deaths ({path.name}).", flush=True)
                previous_warning = None
            except (SaveUnavailable, FileNotFoundError, TrackingConfigurationError) as exc:
                warning = str(exc)
                if warning != previous_warning:
                    print(warning, flush=True)
                previous_warning = warning
            except RetryLater as exc:
                print(str(exc), flush=True)
                delay = max(interval, exc.seconds)
            # Split long rate-limit waits so Ctrl+C remains responsive.
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            print("Invalid JSON in config or response; check config.json formatting.", file=sys.stderr)
        elif isinstance(exc, OSError):
            print("Cannot read configuration/save or write message state. Check paths and permissions.", file=sys.stderr)
        elif isinstance(exc, (KeyError, TypeError, AttributeError)):
            print("Invalid configuration or response. See config.example.json.", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
