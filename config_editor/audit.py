"""Audit a station config against RMS's template and ConfigReader, and migrate it.

The same checks as RMS's ``Utils/AuditConfig.py``, and the same merge rules as
``Utils/MigrateConfig.py``, but per station and with the result handed back as
data (so the UI can act on it) instead of printed.

- **missing**: in ``.configTemplate`` but not in the station file (RMS uses the
  built-in default there).
- **unknown**: in the station file but not read anywhere in ``ConfigReader.py``
  (RMS ignores it; a typo or a leftover).
- **disabled**: a ``; option: value`` line for an option RMS knows.
- **extra**: in the station file and known to RMS, but not in the template
  (a migration keeps it).

A migration rebuilds the file on the template's layout: every template line is
copied, the station's value replaces the template value where it differs,
known-but-not-in-template options are appended to their section, unknown ones
are dropped and reported, and the legacy ``quota_management_disabled`` is folded
into ``quota_management_enabled``.
"""

from __future__ import annotations

import difflib
import re
import time
from pathlib import Path

from .configfile import INLINE_COMMENT, OPTION_RE, SECTION_RE, ConfigFile

# Same pattern as Utils/AuditConfig.extractConfigOptions: every option name that
# ConfigReader.py reads, whether guarded by has_option or read directly.
_READER_RE = re.compile(
    r'parser\.(?:has_option|get(?:boolean|int|float)?)\([^,]+,\s*["\'](\w+)["\']')

LEGACY_RENAMES = {
    # old option -> (new option, value mapping); MigrateConfig's special case
    "quota_management_disabled": ("quota_management_enabled", {"true": "false", "false": "true"}),
}


def known_options(rms_dir: Path) -> set[str] | None:
    """Lower-case names of every option ConfigReader.py reads; None if it isn't there."""
    src = Path(rms_dir).expanduser() / "RMS" / "ConfigReader.py"
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    names = {m.lower() for m in _READER_RE.findall(text)}
    names.add("stationid")
    return names


def template_path(rms_dir: Path) -> Path | None:
    p = Path(rms_dir).expanduser() / ".configTemplate"
    return p if p.is_file() else None


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def audit(station: ConfigFile, template: ConfigFile | None, known: set[str] | None) -> dict:
    """Compare one station file with the template and ConfigReader (see module doc)."""
    out = {"missing": [], "unknown": [], "disabled": [], "extra": []}
    if template is not None:
        for (sec, key), te in template.entries.items():
            if (sec, key) in station.entries:
                continue
            if (sec, key) in station.disabled:
                continue  # reported under disabled
            out["missing"].append({"section": sec, "name": te.option, "key": key,
                                   "template_value": te.value, "help": te.help})
    for (sec, key), e in station.entries.items():
        if known is not None and key not in known:
            out["unknown"].append({"section": sec, "name": e.option, "key": key, "value": e.value})
        elif template is not None and (sec, key) not in template.entries:
            out["extra"].append({"section": sec, "name": e.option, "key": key, "value": e.value})
    trusted = set()
    if known:
        trusted |= known
    if template is not None:
        trusted |= {k for _, k in template.entries}
    for (sec, key), value in station.disabled.items():
        if key in trusted and (sec, key) not in station.entries:
            out["disabled"].append({"section": sec, "name": key, "key": key, "value": value})
    for lst in out.values():
        lst.sort(key=lambda d: (d["section"], d["key"]))
    return out


def migrate(station: ConfigFile, template: ConfigFile, known: set[str] | None,
            tool: str = "config-editor") -> tuple[list[str], list[str]]:
    """Rebuild ``station`` on the template layout. Returns (new_lines, log)."""
    log: list[str] = []

    # The station's values, with the legacy renames applied
    values: dict[tuple[str, str], tuple[str, str]] = {}   # (sec, key) -> (spelling, value)
    for (sec, key), e in station.entries.items():
        values[(sec, key)] = (e.option, e.value)
    for old, (new, mapping) in LEGACY_RENAMES.items():
        for (sec, key) in list(values):
            if key != old:
                continue
            _, v = values.pop((sec, key))
            if (sec, new) not in values:
                nv = "false" if _truthy(v) else "true" if old.endswith("disabled") else mapping.get(v.lower(), v)
                values[(sec, new)] = (new, nv)
                log.append("[%s] %s: %s -> %s: %s (legacy option renamed)" % (sec, old, v, new, nv))

    out: list[str] = []
    used: set[tuple[str, str]] = set()
    section = None
    kept = 0
    for line in template.lines:
        line = line.rstrip()   # MigrateConfig strips template lines too
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            out.append(line)
            continue
        m = OPTION_RE.match(line)
        if m and section is not None and not line.lstrip().startswith((";", "#")):
            key = (section, m.group(1).lower())
            tval = m.group(3).split(INLINE_COMMENT, 1)[0].strip()
            if key in values:
                used.add(key)
                _, sval = values[key]
                if sval != tval:
                    out.append(ConfigFile._rewrite(line, sval))
                    log.append("[%s] %s: template default %r => kept %r" % (section, m.group(1), tval, sval))
                    kept += 1
                    continue
            out.append(line)
            continue
        out.append(line)
    log.append("%d customized option(s) carried over" % kept)

    # Options the template doesn't have: keep the ones RMS knows, drop the rest
    preserved: dict[str, list[tuple[str, str]]] = {}
    for key, (spelling, value) in values.items():
        if key in used:
            continue
        sec, k = key
        if known is not None and k not in known:
            log.append("[%s] %s: %s => DROPPED (not supported by RMS)" % (sec, spelling, value))
            continue
        preserved.setdefault(sec, []).append((spelling, value))
        log.append("[%s] %s: %s => preserved (not in the template, supported by RMS)" % (sec, spelling, value))

    for sec, items in preserved.items():
        start, end = _span(out, sec)
        if start is None:
            if out and out[-1].strip():
                out.append("")
            out.append("")
            out.append("[%s]" % sec)
            start = end = len(out)
        insert_at = end
        while insert_at > start and not out[insert_at - 1].strip():
            insert_at -= 1
        block = ["", "; The following options were preserved but are not in the template"]
        block += ["%s: %s" % (spelling, value) for spelling, value in items]
        out[insert_at:insert_at] = block

    while out and not out[-1].strip():
        out.pop()
    out += ["", "; Migrated to the .configTemplate layout by %s on %s"
            % (tool, time.strftime("%Y-%m-%d %H:%M:%S"))]
    return out, log


def _span(lines: list[str], section: str) -> tuple[int | None, int | None]:
    start = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip() == section:
            start = i + 1
    if start is None:
        return None, None
    return start, len(lines)


def unified_diff(old_lines: list[str], new_lines: list[str], name: str) -> str:
    return "".join(difflib.unified_diff(
        [l + "\n" for l in old_lines], [l + "\n" for l in new_lines],
        fromfile=name, tofile=name + " (migrated)", n=2))
