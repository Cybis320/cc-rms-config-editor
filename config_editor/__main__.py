"""CLI entry point: python -m config_editor

With no sub-command it starts the web UI. ``list``, ``get`` and ``set`` give the
same view and edits from a terminal or a script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .fleet import DEFAULT_RMS_DIR, DEFAULT_STATIONS_DIR, Fleet, check_value, discover, journal


def _add_location_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stations-dir", type=Path, default=DEFAULT_STATIONS_DIR,
                   help="multicam station folders (default: %s)" % DEFAULT_STATIONS_DIR)
    p.add_argument("--rms-dir", type=Path, default=DEFAULT_RMS_DIR,
                   help="RMS checkout; its .config is used when there are no station folders "
                        "and its ConfigReader.py gives the option types (default: %s)" % DEFAULT_RMS_DIR)
    p.add_argument("--config", type=Path, action="append", default=[], metavar="PATH",
                   help="an extra .config to show as its own column (repeatable)")
    p.add_argument("--include-root", action="store_true",
                   help="also show the RMS root .config (the one add_GStation copies) when station folders exist")


def _load(args: argparse.Namespace) -> Fleet:
    targets = discover(args.stations_dir, args.rms_dir, args.config, include_root=args.include_root)
    if not targets:
        raise SystemExit("no .config found under %s or %s" % (args.stations_dir, args.rms_dir))
    return Fleet.load(targets, args.rms_dir)


def _find(fleet: Fleet, name: str) -> tuple[str, str]:
    """Resolve 'option' or 'Section.option' to (section, option_key)."""
    if "." in name:
        section, option = name.split(".", 1)
        return section, option.lower()
    key = name.lower()
    hits = sorted({s for cf in fleet.files.values() for (s, k) in cf.entries if k == key})
    if not hits:
        raise SystemExit("option %r not found in any config" % name)
    if len(hits) > 1:
        raise SystemExit("%r is in several sections (%s): use Section.%s" % (name, ", ".join(hits), name))
    return hits[0], key


def cmd_list(args: argparse.Namespace) -> None:
    fleet = _load(args)
    m = fleet.matrix()
    ids = [f["id"] for f in m["files"]]
    for c in m["checks"]:
        print("WARNING %s: %s" % (c["station"], c["message"]))
    print("%-32s %s" % ("option", "  ".join(ids)))
    for sec in m["sections"]:
        rows = sec["options"]
        if args.varying:
            rows = [r for r in rows if r["distinct"] > 1 or r["missing"]]
        if not rows:
            continue
        print("[%s]" % sec["name"])
        for r in rows:
            cells = ["-" if r["values"][i] is None else r["values"][i] for i in ids]
            same = r["distinct"] == 1 and not r["missing"]
            if same:
                print("  %-30s = %s" % (r["name"], cells[0]))
            else:
                print("  %-30s %s" % (r["name"], "  |  ".join(cells)))


def cmd_get(args: argparse.Namespace) -> None:
    fleet = _load(args)
    section, key = _find(fleet, args.option)
    for t in fleet.targets:
        e = fleet.files[t.id].get(section, key)
        print("%-10s %s" % (t.id, "-" if e is None else e.value))


def cmd_set(args: argparse.Namespace) -> None:
    fleet = _load(args)
    section, key = _find(fleet, args.option)
    ids = [t.id for t in fleet.targets if not t.shared] if not args.stations else \
        [s.strip() for s in args.stations.split(",") if s.strip()]
    unknown = [i for i in ids if i not in fleet.files]
    if unknown:
        raise SystemExit("unknown station(s): %s" % ", ".join(unknown))
    value = None if args.unset else args.value
    if value is None and not args.unset:
        raise SystemExit("give a VALUE or --unset")
    warn = check_value(value or "", fleet.types.get(key))
    if warn and not args.force:
        raise SystemExit(warn + " (use --force to write it anyway)")
    journal("cli  config-editor set %s" % " ".join(sys.argv[2:]))
    result = fleet.apply(section, key, {i: value for i in ids}, backup=not args.no_backup)
    for i in ids:
        state = "written" if i in result["written"] else "unchanged"
        bak = result["backups"].get(i)
        print("%-10s %s%s" % (i, state, ("  (backup: %s)" % bak) if bak else ""))
    for c in fleet.checks():
        print("WARNING %s: %s" % (c["station"], c["message"]))


def cmd_audit(args: argparse.Namespace) -> None:
    fleet = _load(args)
    rep = fleet.audit()
    print("template:     %s" % (rep["template"] or "NOT FOUND (missing-option checks skipped)"))
    print("ConfigReader: %s" % ("found" if rep["rms_known"] else "NOT FOUND (unknown-option checks skipped)"))
    labels = [("duplicate", "DUPLICATE (RMS refuses to start on these; the last copy is the one shown)"),
              ("missing", "MISSING (in the template, not in the file; RMS default applies)"),
              ("unknown", "NOT IMPLEMENTED IN RMS (ignored by RMS)"),
              ("disabled", "COMMENTED OUT (RMS default applies)"),
              ("extra", "NOT IN THE TEMPLATE (known to RMS; a migration keeps them)")]
    ta = rep["template_audit"]
    if ta["reader_not_in_template"] or ta["template_not_in_reader"]:
        print()
        print("== .configTemplate vs ConfigReader.py (developer view)")
        if ta["reader_not_in_template"]:
            print("   READ BY RMS BUT NOT IN THE TEMPLATE (deprecated/internal ones omitted)")
            for k in ta["reader_not_in_template"]:
                print("     %s" % k)
        if ta["template_not_in_reader"]:
            print("   IN THE TEMPLATE BUT NOT READ BY RMS")
            for k in ta["template_not_in_reader"]:
                print("     %s" % k)
    for tid, a in rep["stations"].items():
        print()
        print("== %s" % tid)
        if not any(a[k] for k, _ in labels):
            print("   clean")
        for k, label in labels:
            if not a[k]:
                continue
            print("   %s" % label)
            for d in a[k]:
                v = d.get("template_value", d.get("value"))
                print("     [%s] %s%s" % (d["section"], d["name"], "" if v is None else ": " + v))


def cmd_migrate(args: argparse.Namespace) -> None:
    fleet = _load(args)
    ids = [t.id for t in fleet.targets if not t.shared] if not args.stations else \
        [s.strip() for s in args.stations.split(",") if s.strip()]
    result = fleet.migrate(ids, apply=args.apply, recent=args.recent, backup=not args.no_backup)
    for tid, r in result.items():
        print("== %s: %s" % (tid, "written" if r["written"] else
                             "would change" if r["changed"] else "already matches the template"))
        for line in r["log"]:
            print("   " + line)
        if r["backup"]:
            print("   backup: %s" % r["backup"])
        if args.diff and r["changed"]:
            print(r["diff"])
    if not args.apply and any(r["changed"] for r in result.values()):
        print("\nDry run. Re-run with --apply to write (add --diff to see the exact changes).")


def cmd_storage(args: argparse.Namespace) -> None:
    from . import storage
    fleet = _load(args)
    for g in storage.plan(fleet)["disks"]:
        print("== disk %s: %s GB, %s GB free" % (
            g["mount"] or "?", "?" if g["total_gb"] is None else "%.0f" % g["total_gb"],
            "?" if g["free_gb"] is None else "%.0f" % g["free_gb"]))
        print("   %-8s %6s %8s %9s %9s %8s %7s" % ("station", "Mbps", "video/d", "reserve", "rms_quota", "cc_quota", "days"))
        for s in g["stations"]:
            print("   %-8s %6s %8s %9.0f %9s %8s %7s" % (
                s["id"],
                "?" if s["needs_mbps"] else ("-" if not s["mbps"] else "%g" % s["mbps"]),
                "%.0f" % s["video_gb"] if s["video_gb"] else "-",
                s["reserve_gb"],
                "%.0f" % s["quotas"]["rms_data_quota"] if s["quota_on"] else "off",
                "%.0f" % s["quotas"]["continuous_capture_quota"] if s["quota_on"] else "-",
                "%.1f" % s["video_days"] if s["video_days"] is not None else "-"))
        missing = [s["id"] for s in g["stations"] if s["needs_mbps"]]
        if missing:
            hints = ["%s camera %g" % (s["id"], s["camera_mbps"]) for s in g["stations"] if s["camera_mbps"]]
            print("   raw_video_save is on but raw_video_bitrate_mbps is not set for %s%s: set it to judge this disk "
                  "(config-editor set Capture.raw_video_bitrate_mbps N)" % (", ".join(missing),
                                                                   " (encoder setting: %s)" % ", ".join(hints) if hints else ""))
        if not g["judgeable"]:
            if not missing:
                print("   quota management is off (or incomplete) for some station: nothing to judge")
            continue
        total = g["sum_quota_gb"] + g["sum_reserve_gb"]
        print("   quotas %.0f + reserves %.0f = %.0f GB of %.0f: %s" % (
            g["sum_quota_gb"], g["sum_reserve_gb"], total, g["total_gb"], "fits" if g["fits"] else "DOES NOT FIT"))
        sug = g["suggestion"]
        if sug["ok"]:
            print("   suggestion (%.0f%% margin): rms_data_quota %d per station, continuous_capture_quota adjusted by the "
                  "same amount%s" % (sug["margin"] * 100, sug["per_station_gb"],
                                     " (%s)" % ", ".join("%s %d" % (k, v["continuous_capture_quota"]) for k, v in sug["stations"].items())))
        else:
            print("   no quota split fits: the reserves alone (extra_space_gb, a day of video) leave no room — "
                  "lower the bitrate or extra_space_gb, or move stations to another disk")


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import serve
    serve(_load(args), args.host, args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="config-editor",
        description="See and set RMS .config options across every station on this machine.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="start the web UI (the default)")
    _add_location_args(p)
    p.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 for the LAN)")
    p.add_argument("--port", type=int, default=8421)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("list", help="print every option with its value per station")
    _add_location_args(p)
    p.add_argument("--varying", action="store_true", help="only options that differ or are missing somewhere")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="print one option's value per station")
    _add_location_args(p)
    p.add_argument("option", help="option name, or Section.option if the name is in several sections")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="write one option to every station (or a subset)")
    _add_location_args(p)
    p.add_argument("option")
    p.add_argument("value", nargs="?")
    p.add_argument("--stations", help="comma-separated station ids (default: all)")
    p.add_argument("--unset", action="store_true", help="comment the option out (RMS default applies)")
    p.add_argument("--force", action="store_true", help="write even if the value fails the type check")
    p.add_argument("--no-backup", action="store_true", help="skip the .config.bak.<timestamp> snapshot")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("audit", help="compare every station with .configTemplate and ConfigReader.py")
    _add_location_args(p)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("migrate", help="rebuild station configs on the .configTemplate layout, keeping custom values")
    _add_location_args(p)
    p.add_argument("--stations", help="comma-separated station ids (default: all)")
    p.add_argument("--apply", action="store_true", help="write the files (default: dry run)")
    p.add_argument("--diff", action="store_true", help="print the unified diff per station")
    p.add_argument("--recent", action="store_true",
                   help="also reset recently changed defaults (star_catalog_file) to the template value, like MigrateConfig -r")
    p.add_argument("--no-backup", action="store_true", help="skip the .config.bak.<timestamp> snapshot")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("storage", help="what the quotas hold at the declared bitrate, and whether each disk fits")
    _add_location_args(p)
    p.set_defaults(func=cmd_storage)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "serve")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
