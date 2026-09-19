"""CLI entry point: python -m config_editor

With no sub-command it starts the web UI. ``list``, ``get`` and ``set`` give the
same view and edits from a terminal or a script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .fleet import DEFAULT_RMS_DIR, DEFAULT_STATIONS_DIR, Fleet, check_value, discover


def _add_location_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stations-dir", type=Path, default=DEFAULT_STATIONS_DIR,
                   help="multicam station folders (default: %s)" % DEFAULT_STATIONS_DIR)
    p.add_argument("--rms-dir", type=Path, default=DEFAULT_RMS_DIR,
                   help="RMS checkout; its .config is used when there are no station folders "
                        "and its ConfigReader.py gives the option types (default: %s)" % DEFAULT_RMS_DIR)
    p.add_argument("--config", type=Path, action="append", default=[], metavar="PATH",
                   help="an extra .config to show as its own column (repeatable)")


def _load(args: argparse.Namespace) -> Fleet:
    targets = discover(args.stations_dir, args.rms_dir, args.config)
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
    result = fleet.apply(section, key, {i: value for i in ids}, backup=not args.no_backup)
    for i in ids:
        state = "written" if i in result["written"] else "unchanged"
        bak = result["backups"].get(i)
        print("%-10s %s%s" % (i, state, ("  (backup: %s)" % bak) if bak else ""))


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

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "serve")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
