"""The fleet view: every station config on this machine side by side.

Discovers the station ``.config`` files, merges their options into one
section/option matrix and applies edits to any subset of them. Also pulls the
option types (int / float / bool / str) out of RMS's own ``ConfigReader.py`` so
the UI can warn about a value RMS would choke on.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .configfile import ConfigFile

DEFAULT_STATIONS_DIR = Path.home() / "source" / "Stations"
DEFAULT_RMS_DIR = Path.home() / "source" / "RMS"


@dataclass
class Target:
    id: str            # column label: the station folder name
    path: Path
    shared: bool = False  # the optional ~/source/Stations/.config base layer


def discover(stations_dir: Path = DEFAULT_STATIONS_DIR, rms_dir: Path = DEFAULT_RMS_DIR,
             extra: list[Path] | None = None) -> list[Target]:
    """One target per ``<stations_dir>/*/.config``; the RMS root config when there are none.

    A ``.config`` directly in ``stations_dir`` (the shared base of a layered multicam
    layout) is listed first, flagged ``shared``.
    """
    targets: list[Target] = []
    stations_dir = Path(stations_dir).expanduser()
    base = stations_dir / ".config"
    if base.is_file():
        targets.append(Target("shared", base, shared=True))
    if stations_dir.is_dir():
        for d in sorted(p for p in stations_dir.iterdir() if p.is_dir()):
            cfg = d / ".config"
            if cfg.is_file():
                targets.append(Target(d.name, cfg))
    if not any(not t.shared for t in targets):
        root = Path(rms_dir).expanduser() / ".config"
        if root.is_file():
            targets.append(Target("RMS", root))
    for p in extra or []:
        p = Path(p).expanduser().resolve()
        if p.is_file() and all(t.path.resolve() != p for t in targets):
            label = p.parent.name if p.name == ".config" else p.name
            targets.append(Target(label, p))
    return targets


_TYPE_RE = re.compile(r'parser\.get(int|float|boolean)\(\s*section\s*,\s*["\'](\w+)["\']')


def option_types(rms_dir: Path = DEFAULT_RMS_DIR) -> dict[str, str]:
    """``{option_lower: 'int'|'float'|'bool'}`` scraped from RMS/ConfigReader.py.

    Options read with a plain ``parser.get`` are strings and are not listed.
    Best effort: an empty dict when RMS is not where we expect it.
    """
    src = Path(rms_dir).expanduser() / "RMS" / "ConfigReader.py"
    types: dict[str, str] = {}
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return types
    for kind, name in _TYPE_RE.findall(text):
        types[name.lower()] = "bool" if kind == "boolean" else kind
    return types


def check_value(value: str, kind: str | None) -> str | None:
    """Return a warning if ``value`` does not parse as ``kind`` (None when fine)."""
    if not kind or value == "":
        return None
    v = value.strip()
    try:
        if kind == "int":
            int(v)
        elif kind == "float":
            float(v)
        elif kind == "bool":
            if v.lower() not in ("1", "yes", "true", "on", "0", "no", "false", "off"):
                raise ValueError
    except ValueError:
        return "RMS reads this as %s; %r is not one" % (kind, value)
    return None


@dataclass
class Fleet:
    targets: list[Target]
    files: dict[str, ConfigFile] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, targets: list[Target], rms_dir: Path = DEFAULT_RMS_DIR) -> "Fleet":
        fleet = cls(targets=targets, types=option_types(rms_dir))
        fleet.reload()
        return fleet

    def reload(self) -> None:
        self.files = {t.id: ConfigFile.load(t.path) for t in self.targets}

    def changed_on_disk(self) -> bool:
        for t in self.targets:
            cf = self.files.get(t.id)
            try:
                if cf is None or t.path.stat().st_mtime != cf.mtime:
                    return True
            except OSError:
                return True
        return False

    # --- the matrix ------------------------------------------------------

    def matrix(self) -> dict:
        """JSON-ready view: files, then sections with options and per-file values."""
        order = [t.id for t in self.targets]
        sections: list[str] = []
        options: dict[str, list[tuple[str, str]]] = {}   # section -> [(key, spelling)]
        for tid in order:
            cf = self.files[tid]
            for s in cf.sections:
                if s not in sections:
                    sections.append(s)
                    options[s] = []
            for (s, key), e in cf.entries.items():
                if key not in [k for k, _ in options[s]]:
                    options[s].append((key, e.option))

        out_sections = []
        for s in sections:
            rows = []
            for key, spelling in options[s]:
                values: dict[str, str | None] = {}
                help_text = ""
                for tid in order:
                    e = self.files[tid].get(s, key)
                    values[tid] = None if e is None else e.value
                    if e is not None and e.help and not help_text:
                        help_text = e.help
                present = [v for v in values.values() if v is not None]
                rows.append({
                    "name": spelling,
                    "key": key,
                    "type": self.types.get(key, "str"),
                    "help": help_text,
                    "values": values,
                    "distinct": len(set(present)),
                    "missing": len(order) - len(present),
                })
            out_sections.append({"name": s, "options": rows})

        files = []
        for t in self.targets:
            cf = self.files[t.id]
            sid = cf.get("System", "stationID")
            files.append({
                "id": t.id,
                "path": str(t.path),
                "mtime": cf.mtime,
                "shared": t.shared,
                "station_id": sid.value if sid else None,
            })
        return {"files": files, "sections": out_sections}

    # --- editing ---------------------------------------------------------

    def apply(self, section: str, option: str, values: dict[str, str | None],
              expect_mtimes: dict[str, float] | None = None,
              backup_done: set[str] | None = None, backup: bool = True) -> dict:
        """Write ``values`` (``{target_id: value | None}``) for one option.

        ``None`` comments the option out. Files are re-read from disk first; if
        ``expect_mtimes`` is given and a file changed since the caller last looked,
        nothing is written and a ``conflict`` list is returned instead. ``backup_done``
        is a set of target ids already snapshotted this session: a file is backed up
        the first time it is written and the id added to the set. ``backup=False``
        skips the snapshot altogether.
        """
        unknown = [tid for tid in values if tid not in self.files]
        if unknown:
            raise KeyError("unknown target(s): %s" % ", ".join(unknown))

        fresh = {tid: ConfigFile.load(self.files[tid].path) for tid in values}
        if expect_mtimes:
            conflict = [tid for tid, cf in fresh.items()
                        if tid in expect_mtimes and abs(cf.mtime - float(expect_mtimes[tid])) > 1e-6]
            if conflict:
                return {"written": [], "conflict": conflict, "backups": {}}

        written: list[str] = []
        backups: dict[str, str] = {}
        for tid, value in values.items():
            cf = fresh[tid]
            changed = cf.unset(section, option) if value is None else cf.set(section, option, str(value))
            if not changed:
                continue
            do_backup = backup and (backup_done is None or tid not in backup_done)
            bak = cf.save(backup=do_backup)
            if backup_done is not None:
                backup_done.add(tid)
            if bak is not None:
                backups[tid] = str(bak)
            written.append(tid)
            self.files[tid] = cf
        return {"written": written, "conflict": [], "backups": backups}


def code_version(repo_dir: Path) -> str:
    """Short git description of the running checkout, for the UI footer."""
    try:
        out = subprocess.run(["git", "-C", str(repo_dir), "log", "-1", "--format=%h %cs"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"
