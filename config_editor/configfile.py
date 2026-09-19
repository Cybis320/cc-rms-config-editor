"""Layout-preserving reader/writer for an RMS ``.config`` file.

RMS configs are INI-style files where the comments *are* the documentation, so
the usual ``configparser`` round trip (which throws every comment away) is not an
option. This module keeps the file as a list of lines and only ever touches the
single line that holds the option being changed. Everything else -- comments,
blank lines, ordering, the odd ``; option: value`` that is commented out -- is
written back byte for byte.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# RMS writes "option: value"; RawConfigParser also accepts "option = value".
OPTION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*([:=])(.*)$")
COMMENT_RE = re.compile(r"^\s*[;#]")
INLINE_COMMENT = ";"  # what RMS.ConfigReader strips from values


@dataclass
class Entry:
    section: str
    option: str          # as spelled in the file (RMS reads it case-insensitively)
    value: str
    line_no: int         # 0-based index into ConfigFile.lines
    help: str = ""       # comment block directly above the option


@dataclass
class ConfigFile:
    path: Path
    lines: list[str] = field(default_factory=list)
    mtime: float = 0.0
    sections: list[str] = field(default_factory=list)
    entries: dict[tuple[str, str], Entry] = field(default_factory=dict)
    newline: str = "\n"

    # --- reading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str) -> "ConfigFile":
        path = Path(path)
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="surrogateescape")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        if lines and lines[-1] == "":
            lines.pop()  # trailing newline; restored on save
        cf = cls(path=path, lines=lines, mtime=path.stat().st_mtime, newline=newline)
        cf._index()
        return cf

    def _index(self) -> None:
        self.sections = []
        self.entries = {}
        section = None
        pending_help: list[str] = []
        for i, line in enumerate(self.lines):
            m = SECTION_RE.match(line)
            if m:
                section = m.group(1).strip()
                if section not in self.sections:
                    self.sections.append(section)
                pending_help = []
                continue
            if not line.strip():
                pending_help = []
                continue
            if COMMENT_RE.match(line):
                pending_help.append(line.strip()[1:].strip())
                continue
            m = OPTION_RE.match(line)
            if m and section is not None:
                option = m.group(1)
                value = m.group(3).split(INLINE_COMMENT, 1)[0].strip()
                help_text = "\n".join(pending_help).strip()
                # Lines like "; Weblog and PerfMonitor" / "; -----" are headings, not help.
                key = (section, option.lower())
                self.entries[key] = Entry(section, option, value, i, help_text)
            pending_help = []

    def get(self, section: str, option: str) -> Entry | None:
        return self.entries.get((section, option.lower()))

    # --- editing (in memory) ---------------------------------------------

    def set(self, section: str, option: str, value: str) -> bool:
        """Set ``option`` in ``section`` to ``value``. Returns True if the file changed.

        An existing line is rewritten in place (keeping its spelling, separator and any
        inline comment). A missing option is added: a commented-out ``; option: x`` in
        the section is re-enabled if there is one, otherwise the option is appended at
        the end of the section. A missing section is appended to the file.
        """
        value = value.strip()
        if INLINE_COMMENT in value:
            raise ValueError("value may not contain ';' (RMS treats it as a comment)")
        if "\n" in value or "\r" in value:
            raise ValueError("value may not span lines")

        entry = self.get(section, option)
        if entry is not None:
            if entry.value == value:
                return False
            self.lines[entry.line_no] = self._rewrite(self.lines[entry.line_no], value)
            entry.value = value
            return True

        start, end = self._section_span(section)
        if start is None:
            # New section at the end of the file
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.append("")
            self.lines.append("[%s]" % section)
            self.lines.append("")
            self.lines.append("%s: %s" % (option, value))
            self._index()
            return True

        # Re-enable a commented-out copy of the option if the section has one
        pat = re.compile(r"^\s*[;#]\s*%s\s*[:=]" % re.escape(option), re.IGNORECASE)
        for i in range(start, end):
            if pat.match(self.lines[i]):
                self.lines[i] = "%s: %s" % (self._spelling(self.lines[i], option), value)
                self._index()
                return True

        # Otherwise append after the last non-blank line of the section
        insert_at = end
        while insert_at > start and not self.lines[insert_at - 1].strip():
            insert_at -= 1
        self.lines[insert_at:insert_at] = ["", "%s: %s" % (option, value)]
        self._index()
        return True

    def unset(self, section: str, option: str) -> bool:
        """Comment the option out so RMS falls back to its built-in default."""
        entry = self.get(section, option)
        if entry is None:
            return False
        self.lines[entry.line_no] = "; " + self.lines[entry.line_no]
        self._index()
        return True

    @staticmethod
    def _rewrite(line: str, value: str) -> str:
        m = OPTION_RE.match(line)
        assert m is not None
        name, sep, rest = m.group(1), m.group(2), m.group(3)
        comment = ""
        if INLINE_COMMENT in rest:
            comment = " " + INLINE_COMMENT + rest.split(INLINE_COMMENT, 1)[1].rstrip()
        return "%s%s %s%s" % (name, sep, value, comment)

    @staticmethod
    def _spelling(line: str, fallback: str) -> str:
        m = re.match(r"^\s*[;#]\s*([A-Za-z_][A-Za-z0-9_]*)", line)
        return m.group(1) if m else fallback

    def _section_span(self, section: str) -> tuple[int | None, int | None]:
        start = None
        for i, line in enumerate(self.lines):
            m = SECTION_RE.match(line)
            if not m:
                continue
            if start is not None:
                return start, i
            if m.group(1).strip() == section:
                start = i + 1
        if start is None:
            return None, None
        return start, len(self.lines)

    # --- writing ---------------------------------------------------------

    def text(self) -> str:
        return self.newline.join(self.lines) + self.newline

    def save(self, backup: bool = True) -> Path | None:
        """Write atomically (temp file + rename), keeping mode and ownership.

        With ``backup`` the previous content is copied to ``<name>.bak.<timestamp>``
        beside the file first. Returns the backup path (or None).
        """
        backup_path = None
        if backup and self.path.exists():
            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = self.path.with_name(self.path.name + ".bak." + stamp)
            n = 1
            while backup_path.exists():
                backup_path = self.path.with_name("%s.bak.%s_%d" % (self.path.name, stamp, n))
                n += 1
            backup_path.write_bytes(self.path.read_bytes())

        data = self.text().encode("utf-8", errors="surrogateescape")
        fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                st = self.path.stat()
                os.chmod(tmp, st.st_mode & 0o7777)
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
            except FileNotFoundError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.mtime = self.path.stat().st_mtime
        return backup_path
