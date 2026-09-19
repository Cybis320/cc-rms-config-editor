"""The fleet view: every station config on this machine side by side.

Discovers the station ``.config`` files, merges their options into one
section/option matrix and applies edits to any subset of them. Also pulls the
option types (int / float / bool / str) out of RMS's own ``ConfigReader.py`` so
the UI can warn about a value RMS would choke on.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audit as auditmod
from .configfile import ConfigFile

DEFAULT_STATIONS_DIR = Path.home() / "source" / "Stations"
DEFAULT_RMS_DIR = Path.home() / "source" / "RMS"


@dataclass
class Target:
    id: str            # column label: the station folder name
    path: Path
    shared: bool = False  # the optional ~/source/Stations/.config base layer


def discover(stations_dir: Path = DEFAULT_STATIONS_DIR, rms_dir: Path = DEFAULT_RMS_DIR,
             extra: list[Path] | None = None, include_root: bool = False) -> list[Target]:
    """One target per ``<stations_dir>/*/.config``; the RMS root config when there are none.

    A ``.config`` directly in ``stations_dir`` (the shared base of a layered multicam
    layout) is listed first, flagged ``shared``. ``include_root`` adds the RMS root
    ``.config`` as a last column even when station folders exist (MigrateConfig always
    migrates it, since add_GStation copies it to new stations).
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
    if include_root or not any(not t.shared for t in targets):
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
    known: set[str] | None = None          # every option ConfigReader.py reads
    template_path: Path | None = None      # <rms_dir>/.configTemplate
    template: ConfigFile | None = None

    @classmethod
    def load(cls, targets: list[Target], rms_dir: Path = DEFAULT_RMS_DIR) -> "Fleet":
        fleet = cls(targets=targets, types=option_types(rms_dir),
                    known=auditmod.known_options(rms_dir),
                    template_path=auditmod.template_path(rms_dir))
        fleet.reload()
        return fleet

    def reload(self) -> None:
        self.files = {t.id: ConfigFile.load(t.path) for t in self.targets}
        self.template = ConfigFile.load(self.template_path) if self.template_path else None

    def changed_on_disk(self) -> bool:
        watched = [(t.path, self.files.get(t.id)) for t in self.targets]
        if self.template_path:
            watched.append((self.template_path, self.template))
        for path, cf in watched:
            try:
                if cf is None or path.stat().st_mtime != cf.mtime:
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
        # The template (when there is one) sets the canonical section and option order
        sources = ([self.template] if self.template is not None else []) + [self.files[t] for t in order]
        for cf in sources:
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
                te = self.template.get(s, key) if self.template is not None else None
                if te is not None and te.help and not help_text:
                    help_text = te.help
                disabled = {tid: self.files[tid].disabled[(s, key)] for tid in order
                            if values[tid] is None and (s, key) in self.files[tid].disabled}
                rows.append({
                    "name": spelling,
                    "key": key,
                    "type": self.types.get(key, "str"),
                    "help": help_text,
                    "values": values,
                    "distinct": len(set(present)),
                    "missing": len(order) - len(present),
                    "template": None if te is None else te.value,
                    "known": None if self.known is None else (key in self.known),
                    "disabled": disabled,
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
        return {
            "files": files,
            "sections": out_sections,
            "checks": self.checks(),
            "template": None if self.template is None else str(self.template_path),
            "rms_known": self.known is not None,
        }

    # --- cross-option sanity checks --------------------------------------

    def checks(self) -> list[dict]:
        """Combinations RMS accepts but that misbehave at runtime, per station.

        Mirrors RMS.DeleteOldObservations: with quota management on, the captured
        directories get rms_data_quota minus the archive, bz2, continuous-capture and
        log quotas; at or below zero RMS logs "No quota allocation remains" and stops
        managing CapturedFiles by quota.
        """
        out = []
        for t in self.targets:
            cf = self.files[t.id]

            def val(opt, default=None):
                e = cf.get("Capture", opt)
                return e.value if e is not None and e.value != "" else default

            enabled = str(val("quota_management_enabled", "false")).strip().lower() in ("1", "true", "yes", "on")
            if not enabled:
                continue
            try:
                parts = {k: float(val(k)) for k in ("rms_data_quota", "arch_dir_quota", "bz2_files_quota",
                                                    "continuous_capture_quota", "log_files_quota")
                         if val(k) is not None}
            except ValueError:
                continue
            if "rms_data_quota" not in parts:
                continue
            left = parts["rms_data_quota"] - sum(v for k, v in parts.items() if k != "rms_data_quota")
            if left <= 0:
                out.append({"station": t.id, "option": "rms_data_quota", "section": "Capture",
                            "message": "no quota left for captured directories: rms_data_quota %g minus the "
                                       "archive, bz2, continuous-capture and log quotas leaves %g GB (RMS will "
                                       "warn and stop managing CapturedFiles by quota)" % (parts["rms_data_quota"], left)})
        return out

    # --- audit / migrate -------------------------------------------------

    def audit(self) -> dict:
        """Per-target audit (see audit.audit) plus the template/ConfigReader status."""
        return {
            "template": None if self.template is None else str(self.template_path),
            "rms_known": self.known is not None,
            "stations": {t.id: auditmod.audit(self.files[t.id], self.template, self.known)
                         for t in self.targets},
            "template_audit": auditmod.audit_template(self.template, self.known),
        }

    def dedupe(self, tid: str, section: str, option: str,
               backup_done: set[str] | None = None, backup: bool = True) -> dict:
        """Comment out all but the last copy of a duplicated option in one file."""
        if tid not in self.files:
            raise KeyError("unknown target: %s" % tid)
        cf = ConfigFile.load(self.files[tid].path)
        n = cf.dedupe(section, option)
        bak = None
        if n:
            do_backup = backup and (backup_done is None or tid not in backup_done)
            bak = cf.save(backup=do_backup)
            if backup_done is not None:
                backup_done.add(tid)
            self.files[tid] = cf
        return {"removed": n, "backup": None if bak is None else str(bak)}

    def migrate(self, ids: list[str], apply: bool = False, recent: bool = False,
                backup_done: set[str] | None = None, backup: bool = True) -> dict:
        """Rebuild the given targets on the template layout; write only with ``apply``.

        ``recent`` also resets the options in audit.RECENT_DEFAULTS to the template
        value (MigrateConfig -r). On apply the log is appended to
        ``<stationID>_MigrateConfig.log`` beside the file, as MigrateConfig does.
        Returns ``{id: {diff, log, changed, written, backup}}``.
        """
        if self.template is None:
            raise ValueError("no .configTemplate found (run RMS_Update.sh, or pass --rms-dir)")
        unknown = [tid for tid in ids if tid not in self.files]
        if unknown:
            raise KeyError("unknown target(s): %s" % ", ".join(unknown))
        result = {}
        for tid in ids:
            cf = ConfigFile.load(self.files[tid].path)
            new_lines, log = auditmod.migrate(cf, self.template, self.known, recent=recent)
            # the trailer carries a timestamp; ignore it when deciding "changed"
            changed = [l for l in cf.lines if not l.startswith("; Migrated to the")] != \
                      [l for l in new_lines if not l.startswith("; Migrated to the")]
            entry = {"diff": auditmod.unified_diff(cf.lines, new_lines, str(cf.path)),
                     "log": log, "changed": changed, "written": False, "backup": None}
            if apply and changed:
                cf.lines = new_lines
                cf._index()
                do_backup = backup and (backup_done is None or tid not in backup_done)
                bak = cf.save(backup=do_backup)
                if backup_done is not None:
                    backup_done.add(tid)
                entry["written"] = True
                entry["backup"] = None if bak is None else str(bak)
                self.files[tid] = cf
                self._append_migrate_log(cf, log, bak)
            result[tid] = entry
        return result

    @staticmethod
    def _append_migrate_log(cf: ConfigFile, log: list[str], backup: Path | None) -> None:
        sid = cf.get("System", "stationID")
        name = "%s_MigrateConfig.log" % (sid.value if sid and sid.value else cf.path.parent.name)
        try:
            with open(cf.path.parent / name, "a", encoding="utf-8") as fh:
                fh.write("\n=== Migration Log: %s (config-editor) ===\n\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("Input: %s\n" % cf.path)
                if backup is not None:
                    fh.write("Backup: %s\n" % backup)
                fh.write("\n".join("  " + l for l in log) + "\n\nMigration applied successfully.\n")
        except OSError:
            pass  # the log is a courtesy; the migration itself already succeeded

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
