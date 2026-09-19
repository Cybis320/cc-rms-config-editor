# config_editor

See and set RMS `.config` options across every station on a machine, side by
side. One table, one column per station: options that are identical everywhere
are muted, options that differ are highlighted, and a click on any row edits it
for all stations (or any subset) at once — no more opening six files to change
the elevation.

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-config-editor/master/scripts/deploy.sh | bash
```

Clones (or updates) the repo into `~/source/CC_Utils/config_editor`, installs
the package into the RMS virtualenv at `~/vRMS` (or a local `.venv` if there
isn't one), and puts an **RMS Config Editor** icon on the Desktop and in the app
menu. Idempotent — re-run the same command any time.

Overridable via environment: `CC_DEST` (checkout location), `CC_VENV`
(virtualenv to install into), `CC_REPO_URL`.

To uninstall:

```bash
pkill -f 'config_editor --port' || true
rm -f ~/.local/share/applications/rms-config-editor.desktop \
      "$(xdg-user-dir DESKTOP)/rms-config-editor.desktop"
rm -rf ~/source/CC_Utils/config_editor
```

## Self-update

The desktop launcher fast-forwards the checkout from GitHub every time it is
clicked (a few seconds at most, skipped silently when offline). If that pulled
new code while the server was already running, the server is restarted so the
update takes effect immediately, and a desktop notification says so. Set
`CC_NO_SELFUPDATE=1` to skip it. A checkout with local commits or edits is never
touched. The running version is shown top-right in the page.

## Desktop icon

```bash
./scripts/install-desktop.sh
```

Clicking the icon starts the server if it isn't already running, then opens the
editor in your browser; clicking it again just reopens the tab. The server keeps
running after the browser closes — stop it with `pkill -f 'config_editor --port'`.
Its output goes to `~/.local/state/config-editor.log`.

## Run from a terminal

```bash
cd ~/source/CC_Utils/config_editor
python3 -m config_editor
```

Then open <http://localhost:8421>. Pure stdlib, no dependencies.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Use `0.0.0.0` to reach it from another machine on the LAN |
| `--port` | `8421` | |
| `--stations-dir` | `~/source/Stations` | One column per `<dir>/*/.config` |
| `--rms-dir` | `~/source/RMS` | Its `.config` is used when there are no station folders; its `ConfigReader.py` gives the option types |
| `--config PATH` | | Show an extra `.config` as its own column (repeatable) |

## Which files

- **Multicam**: every `~/source/Stations/<ID>/.config`, one column each, labelled
  by folder name. If the file's `stationID` disagrees with the folder, the column
  header says so.
- **Single-cam**: `~/source/RMS/.config` when there are no station folders.
- A `.config` directly in `~/source/Stations` (the shared base of a layered
  multicam layout) shows as a first column labelled *shared*.

## Using it

The sidebar lists the sections; the badge shows how many options in each differ
between stations. Pick a section, or *All sections*, and use the filter box
(`/` focuses it) to search option names *and* values — `192.168` finds every
camera URL.

Each row is tagged:

- **same** — identical in every station (values muted).
- **N values** — differs; cells that hold the majority value are plain, the
  odd ones out are highlighted amber.
- **N missing** — set in some stations, absent in others (absent cells show `—`
  and RMS uses its built-in default there).

Toggle **only varying** to get a diff of the whole machine at a glance.

Click a row to edit. The drawer shows the comment from the config as help, the
type RMS will read the value as, and one input per station. Type a value into
**value for every station** and *Apply to all* (or just press Enter there) to
set it everywhere, or edit stations individually. **unset** comments the option
out in that file. Values that RMS could not parse as the expected type are
flagged but not blocked. Enter saves, Esc closes.

Only the lines that actually change are rewritten; comments, ordering and
everything else in the file are preserved byte for byte. An option that is
absent from a file is added at the end of its section (re-enabling a commented
`; option:` line if there is one). The first change to each file in a server
session snapshots it first as `.config.bak.<timestamp>` beside it, the same
naming RMS's `MigrateConfig` uses.

If a file changes on disk while you have the page open (another editor, a
migration), a banner appears; a save against a stale copy is refused and the
values refreshed so nothing is clobbered.

## Command line

The same view and edits from a terminal or a script:

```bash
config-editor list --varying           # every option that differs or is missing somewhere
config-editor get elevation            # one line per station
config-editor set elevation 443.5      # write it to every station
config-editor set elevation 443.5 --stations US005A,US005B
config-editor set star_gate_factor --unset
config-editor get MeteorDetection.k1   # k1 exists in two sections: say which
```

`set` refuses a value that fails the type check unless `--force`, and takes a
`.config.bak.<timestamp>` snapshot unless `--no-backup`.

## Tests

```bash
python -m pytest tests
```
