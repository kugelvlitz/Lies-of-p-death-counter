# Lies of P → Discord death counter (Linux / Steam Deck)

This Python program reads the death count stored in your local Steam save and
keeps a **Discord counter message** updated:

> **Lies of P — First playthrough**  
> 💀 **Deaths: 17**

Regular changes edit that same message. At **50, 100, 150, ... deaths**, the
watcher also posts a separate milestone announcement:

> 🎭 💀 **50 deaths!**  
> Krat claims another 50. The first playthrough continues!

It also maintains a **second message with a section / boss breakdown**. Sections
are grouped from the saved location automatically; you can set a manual section
or boss label in the configuration. Both messages are edited as deaths arrive.

The flow is: game autosave → Python reads `YouDieCount` → Discord webhook
creates/edits the message. A webhook provides the posting and editing needed
here without a Developer Portal application, bot token, slash commands, or
Discord gateway connection. It is not a full interactive Discord bot.

The watcher polls once per second by default. Updates arrive after the game
writes the death to disk, plus polling/network delay. This is near real time,
not a guaranteed update at the instant the death screen appears. Run it on the
computer playing the game, with Internet access, while you play. No hosting
service is needed.

## 1. Start your character and find the save

Launch the game, create your first character, and let it save. In Steam, use
**Lies of P → Properties → Installed Files → Browse** to find the game folder.
The usual save location inside it is:

```text
LiesofP/Saved/SaveGames/<numeric-save-id>/
```

The relevant files look like:

```text
SaveData-1_Character_1.sav
SaveData-1_Character_2.sav
```

`SaveData-1` means character slot 1. The last `1` / `2` are alternating save
buffers for that character. The script follows the newest of the two; it does
not add their counts together or switch to another character.

You can also ask the script to locate saves in known Steam libraries:

```bash
cd /home/david/Documents/lop_bot
python3 lop_bot.py --discover
```

On another computer or Steam Deck, first copy this project to a folder in your
home directory and `cd` there instead. Use Desktop Mode and Konsole on Deck.
`python3 --version` should report Python 3.10 or newer. No pip packages are
required. If Python is missing on your Linux distribution, install Python 3
using that distribution's package manager.

Discovery supports normal Steam installs, Flatpak Steam, and additional
libraries listed in Steam's `libraryfolders.vdf`. For a custom/non-Steam
installation, locate the game folder yourself and use its save directory.

## 2. Create the Discord webhook

1. Create or choose an ordinary server text channel, such as `#lies-of-p-deaths`.
2. Open **Server Settings → Integrations → Webhooks → New Webhook / Create Webhook**.
3. Name it **P's Death Counter**, select your channel, and save.
4. Click **Copy Webhook URL**.

You need **Manage Webhooks** permission to create it; a server administrator can
create it for you if necessary. This version targets an ordinary text channel,
not a forum post/thread. Keep the copied URL private: it grants permission to
post through that webhook. You don't need to paste it into this chat.

## 3. Configure the script

In the project folder:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Open `config.json` in a text editor and replace:

- `save_dir`: the full path to the **numeric folder containing the `.sav` files**,
  using the result of `--discover` or Steam's Browse button.
- `webhook_url`: the copied Discord webhook URL, keeping the surrounding quotes.
- `character_slot`: leave as `1` for `SaveData-1_Character_*.sav`.

Keep `poll_seconds` at `1`. Keep this project/config outside the game save folder.
The real configuration is ignored by Git. Only run one watcher per config at
a time.

## 4. Check locally, then publish

First check the save reader **without sending anything to Discord**:

```bash
python3 lop_bot.py --check
```

Expected output once the counter is present:

```text
SaveData-1_Character_2.sav: 1 deaths
```

A fresh save may omit the counter before your first death. In that case, the
script reports an absent field instead of assuming zero. Let your first death
happen naturally, wait for the game to finish saving/respawning, and check again.
If the field remains absent, stop here: this game/save version needs inspection.

Then start the watcher:

```bash
python3 lop_bot.py
```

It posts the current total and edits that same message whenever the total
changes. You can pin the message manually. Leave the terminal open while
playing; **Ctrl+C** stops the watcher. Alternatively, use the permanent user
service below to run it in the background. On Deck, you can stay in Desktop Mode
to run both the game and watcher. Switching modes can end your user session.

After a death, compare the terminal and Discord: both should increase by one
after saving. Repeated save writes must leave the total unchanged. Restarting
the watcher should update the existing message rather than create another.

The script saves its Discord message ID in `config.state.json` beside the
configuration, along with milestone progress. Keep that file between sessions.
Nothing is added to the game
folder; game saves are only read. The count reflects that character's saved
total, including deaths before starting the watcher. Restoring an older game
save can reduce the displayed count. It is not a separate lifetime death log.

Milestones use absolute saved totals (50, 100, 150, ...). On the very first run,
the current count becomes the baseline; old milestones are not announced. The
same applies when upgrading a state file from before milestone support. After
that, crossing a threshold triggers an announcement even if polling skips its
exact count, such as 49 → 51. A jump across several thresholds announces each
in order. Confirmed announcements are saved individually, so restarts and
restoring an older game save do not repeat them. Milestones reached while the
watcher is stopped are caught up on its next run if its state file is retained.

## Section and boss breakdown

The watcher creates a separate breakdown message once and saves its ID in
`config.state.json`. Later deaths update that same message. Detailed history,
including death-number ranges, saved location IDs, labels and recording times,
is saved in `death_breakdown` inside that file before Discord requests are made.

The game exposes `LatestLocationName` and `RespawnTeleportObject` in the current
save format. The watcher uses the saved location (or checkpoint if needed) for
automatic section labels, for example `LD_Factory_Zone` becomes `Factory Zone`.
This is **saved-location attribution**, not a proven cause of death. The save
may already describe your respawn location when the death count changes.

Boss-room flags in the inspected save stay set for multiple previously visited
rooms, so they do not reliably identify an active boss attempt. Boss names
therefore use a manual override instead of guessing from progression flags.
In `config.json`, use:

```json
"tracking": {
  "section_override": null,
  "boss_override": null,
  "location_labels": {}
}
```

- Leave `section_override` null for automatic section grouping. Set a string
  such as `"My current section"` to assign future deaths to that section.
- Before attempting a boss, set `boss_override` to its name, for example
  `"Current boss"`. **Set it back to null when you stop fighting that boss.**
- Optionally rename automatic sections with `location_labels`, for example
  `{"LD_Factory_Zone": "Factory"}`. `python3 lop_bot.py --check` displays the
  location and checkpoint IDs from your current save.

Tracking settings reload while the watcher runs, so these edits do not require
a service restart. They apply to deaths recorded after the edit; old entries
retain their original labels. Invalid JSON/settings temporarily pause polling
until corrected, rather than assigning deaths with an invalid label. Other
configuration changes still require a restart.

Earlier deaths cannot be reconstructed from a total count: the first saved
total at feature activation is shown as **earlier deaths, section unknown**.
If several deaths are missed between observations, automatic tracking places
that batch under **Unattributed (missed saves)** rather than assuming they all
happened in the last location. An explicit manual section/boss label applies
to the batch instead. Unlabelled bosses appear as **Boss unspecified**, which
does not imply that those deaths occurred outside a boss fight.

The history keeps a high-water mark of the game's numbered deaths, so retrying,
restarting, or restoring an older save does not duplicate previously recorded
death numbers. After a rollback, the displayed current total may be lower than
the retained history; new entries resume when the saved total exceeds the
previous maximum. Use separate config/state files for separate characters or
independent playthroughs.

Discord messages have a length limit. If the breakdown grows too long, its
remaining groups are summarized without losing local details. Print the full
report without contacting Discord with:

```bash
python3 lop_bot.py --breakdown
```

## Permanent user service (manual start)

The service file in `systemd/lop-bot.service` assumes this project lives at
`~/Documents/lop_bot` and uses `/usr/bin/python3`. Adjust `WorkingDirectory` and
`ExecStart` if you move the project or install it elsewhere.

To install it once from the project directory:

```bash
mkdir -p ~/.config/systemd/user
install -m 644 systemd/lop-bot.service ~/.config/systemd/user/lop-bot.service
systemctl --user daemon-reload
```

After logging in following any reboot, start it when you want to play:

```bash
systemctl --user start lop-bot
```

This runs in the background and keeps using the same `config.json` and
`config.state.json`, preserving your counter message and milestone history.
It is installed permanently but **not enabled to start automatically**.
`--user` selects your account's services; no `sudo` is needed.

Useful commands:

```bash
systemctl --user status lop-bot
systemctl --user stop lop-bot
systemctl --user restart lop-bot
journalctl --user -u lop-bot -f
```

Use `restart` after editing the script or configuration. Don't launch a separate
terminal watcher while the service is running. The service normally ends when
your user session ends or the computer shuts down; start it again next time.

To switch to a fresh counter message on your next death:

```bash
systemctl --user stop lop-bot
python3 lop_bot.py --new-message-next-death
systemctl --user start lop-bot
```

Run this from the project directory. It records the current saved count and
waits for an increase before posting a new counter. That new message's ID is
saved for subsequent edits, including after restarts. The old message is left
as it is, and 50-death milestone tracking continues. This is a one-time switch;
the pending request survives restarts and failed sends until it succeeds.

## Troubleshooting and limitations

- **No saves found:** the character has not saved yet, or the game is in a
  custom location. Use Steam's Browse button and set `save_dir` manually.
- **Save is being written:** leave the watcher running; it retries on the next
  cycle without publishing an uncertain value.
- **Absent/unsupported field:** try again after a death and completed autosave.
  This is a targeted reader for tagged, uncompressed GVAS with an integer
  `YouDieCount` property. It deliberately rejects other layouts. Game updates
  may require changes to the reader.
- **HTTP 401/403/404:** check the webhook URL and that the webhook still exists
  and has access to the chosen channel. A deleted webhook is a terminal error.
  A deleted counter message is recreated when Discord reports Unknown Message.
- **Connection failures / HTTP 429 / server errors:** the watcher retries and
  sends the latest saved count after reconnecting. It honors Discord's
  `retry_after`; intermediate counts need not each be published.
- **Unreadable state:** stop the script and inspect/restore `config.state.json`.
  Removing it makes the next run create a new message, so remove the old
  Discord counter manually if desired. It also resets milestone tracking to the
  next run's baseline.
- **An extra message after a connection timeout:** Discord might have accepted
  a counter or milestone POST even if its response was lost. Remove the
  duplicate. Confirmed milestone posts are persisted before sending the next.
- **Frozen count:** ensure the watcher is running on the machine with the
  active saves, that the configured character matches, and that autosaving has
  finished. A remote computer's cloud copy is not a live source.
- **Notifications:** the counter and breakdown are edited instead of creating
  new channel posts per death. Only 50-death milestones create separate
  announcements. None of these messages ping users or roles.

## Validation and sources

Run the offline checks with:

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic GVAS tags and mocked Discord responses. They cover integer
formats, malformed/missing fields, save-buffer selection, restart behavior,
deleted messages, failed updates, location parsing, section/boss attribution,
live config edits, breakdown persistence, milestone crossings/retries/restarts, and
rate limits. **An actual save and live
Discord webhook still need the local check and in-game validation above.**

Format references (no external executable is required):

- [Save editor's death-field mapping](https://github.com/xcier/Lies-of-P-Save-Editor/blob/main/app/tabs/character_tab.py):
  `CharacterSaveData_0` → `YouDieCount_0` in its uesave JSON representation.
- [Save watcher's Lies of P format notes](https://github.com/tvhdev/er-save-watcher):
  uncompressed GVAS and alternating character buffers. That separate program
  restores saves; this project implements only reading and Discord publishing.
- [Discord webhook setup](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).
- [Discord webhook API](https://github.com/discord/discord-api-docs/blob/main/developers/resources/webhook.mdx):
  execute with `wait=true`, then edit by message ID.
- [Discord rate limits](https://github.com/discord/discord-api-docs/blob/main/developers/topics/rate-limits.mdx).
