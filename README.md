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
| `--rms-dir` | `~/source/RMS` | Its `.config` is used when there are no station folders; its `.configTemplate` and `ConfigReader.py` drive the template column, audit and migration |
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

When RMS's `.configTemplate` exists (it is written by `RMS_Update.sh`), it is
shown as a last, italic *template* column. Any cell that differs from the
template value is dotted-underlined, **only ≠ template** filters down to those
rows (your customisations, value by value — not to be confused with the
**Audit**, which compares which *options* exist), and the editor drawer offers
*fill every station with it* for the template value. Options that `ConfigReader.py` never reads are tagged **not in RMS**
(RMS silently ignores them). A cell that is commented out in a file shows the
old value after a `;`.

Click a row to edit. The drawer shows the comment from the config as help, the
type RMS will read the value as, and one input per station. Type a value into
**value for every station** and *Fill every station* to put it in every box,
or edit stations individually; **unset** marks a box for commenting out. Boxes
only stage the change: the footer counts what is pending, and nothing is
written until **Save to files** (Enter). Pressing Enter in the fill box does
both at once. Values that RMS could not parse as the expected type are flagged
but not blocked. Esc closes.

Only the lines that actually change are rewritten; comments, ordering and
everything else in the file are preserved byte for byte. An option that is
absent from a file is added at the end of its section (re-enabling a commented
`; option:` line if there is one). The first change to each file in a server
session snapshots it first as `.config.bak.<timestamp>` beside it, the same
naming RMS's `MigrateConfig` uses.

If a file changes on disk while you have the page open (another editor, a
migration), a banner appears; a save against a stale copy is refused and the
values refreshed so nothing is clobbered.

## Audit

The **Audit** button runs the checks of RMS's `Utils/AuditConfig.py` for every
station at once and makes each finding actionable:

| Finding | Meaning | Action offered |
| --- | --- | --- |
| **Missing** | in `.configTemplate`, not in the file — RMS uses its built-in default | *add* (with the template value), *add all missing* per station or everywhere |
| **Not implemented in RMS** | `ConfigReader.py` never reads it — a typo or a leftover, ignored by RMS | *comment out* |
| **Commented out** | a `; option: value` line for an option RMS knows | *enable* (with its old value) |
| **Not in the template** | RMS knows it but the template lacks it (an alpha feature, say); a migration keeps it | — |
| **Duplicate** | the same option twice in one section — RMS's strict parser refuses to start; the last copy is the one shown and edited | *keep last* (comments the earlier copies out) |

A last block is AuditConfig's `--dev` report: options `ConfigReader.py` reads
that the template does not carry (with AuditConfig's list of deprecated and
internal options omitted, and template options that are shown commented out
ignored), and template options RMS no longer reads.

## Migrate

The **Migrate** button does what `python -m Utils.MigrateConfig -u` does,
per station and with a preview: each file is rebuilt on the template's layout
(its comments, ordering and new options), every value you changed from the
template default is carried over, options you commented out stay commented
out, options RMS knows but the template lacks are
appended to their section under a marker comment, unknown options are dropped
and listed, and the legacy `quota_management_disabled` is folded into
`quota_management_enabled` (or dropped when the new option is already there).
Duplicated options collapse to their last copy. **Preview** shows the migration
log and the exact unified diff for each selected station; **Apply** rewrites
the selected files after a `.config.bak.<timestamp>` snapshot and appends the
log to `<stationID>_MigrateConfig.log` beside the file, as MigrateConfig does.
A second migration of an up-to-date file is a no-op. The *recently changed
defaults* checkbox is MigrateConfig's `-r`: it resets `star_catalog_file` to
the template value as well.

Where the result differs from MigrateConfig, it is deliberate and follows what
RMS actually does at runtime: option names are matched case-insensitively as
`ConfigReader` reads them (MigrateConfig would reset an `ELEVATION:` line to the
template default), and when both `quota_management_disabled` and
`quota_management_enabled` are present the legacy value is carried over because
ConfigReader reads it last. An option you commented out stays commented out,
with its old value, so RMS keeps using its built-in default (MigrateConfig
re-enables it with the template value). `scripts/compare_with_rms.py` runs
both tools over a set of edge cases and reports every difference.

MigrateConfig also migrates the RMS root `.config` (the one `add_GStation`
copies to new stations). Start the editor with `--include-root` to get it as an
extra *RMS* column and include it in audits and migrations.

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

```bash
config-editor audit                    # missing / unknown / commented-out / extra, per station
config-editor migrate                  # dry run: what a migration would change
config-editor migrate --diff           # ... with the unified diff per station
config-editor migrate --apply          # rewrite every station on the template layout
config-editor migrate --apply --stations US005A
config-editor migrate --apply --recent  # MigrateConfig -r: reset star_catalog_file to the template value
config-editor audit --include-root      # the RMS root .config as an extra column
```

## Tests

```bash
python -m pytest tests
```
