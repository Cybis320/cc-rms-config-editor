#!/usr/bin/env python3
"""Run RMS's own Utils/MigrateConfig and Utils/AuditConfig next to config_editor's
audit/migrate over a set of edge-case station configs, and report every difference.

    ~/vRMS/bin/python scripts/compare_with_rms.py [--rms-dir ~/source/RMS] [--base <.config>]

Needs an RMS checkout with .configTemplate and RMS/ConfigReader.py. Nothing is
written outside a temporary directory. Migration results are compared three
ways: the attributes RMS's ConfigReader parses from each output, the text with
trailers ignored, and the kept/dropped/preserved decisions in the logs. Audits
are compared as sets of option names per finding.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from config_editor.audit import audit, known_options, migrate  # noqa: E402
from config_editor.configfile import ConfigFile  # noqa: E402

TRAILER = re.compile(r"^; (Reformated by|Migrated to the)")
SKIP = {"config_file_name", "config_file_path", "loaded_config_files"}


def build_cases(base: str) -> dict[str, str]:
    """Edge cases MigrateConfig/AuditConfig call out, derived from one real config."""
    c = {"baseline": base}
    c["duplicate"] = base.replace("fps: 24.982397\n", "fps: 24.982397\n\nfps: 25\n", 1) \
        if "fps: 24.982397\n" in base else re.sub(r"^(fps: .*)$", r"\1\n\1", base, count=1, flags=re.M)
    c["legacy_true"] = re.sub(r"^quota_management_enabled: .*$", "quota_management_disabled: true", base, flags=re.M)
    c["legacy_both"] = re.sub(r"^quota_management_enabled: .*$",
                              "quota_management_enabled: false\nquota_management_disabled: false", base, flags=re.M)
    c["extras"] = base.replace("[StarExtraction]\n", "[StarExtraction]\n\nhot_pixels_filter: true\nbogus_option: 1\n", 1) \
        + "\n[Custom]\n\nml_model_file: my.keras\nnot_real: 2\n"
    c["oddities"] = re.sub(r"^gamma: (.*)$", r"gamma: \1 ; keep", base, flags=re.M) \
        .replace("\nelevation:", "\nELEVATION:", 1) \
        .replace("linux_pc_weave: -O3", "linux_pc_weave:")
    c["defaults"] = base.replace("protocol: udp", "protocol: tcp").replace("\ncatalog_mag_limit:", "\n; catalog_mag_limit:", 1)
    c["recent"] = re.sub(r"^star_catalog_file: .*$", "star_catalog_file: BSC5", base, flags=re.M)
    return c


def parse_with_rms(rms_dir: Path, src: Path, tag: str) -> dict:
    d = src.parent / ("parse_" + tag)
    d.mkdir(exist_ok=True)
    shutil.copy(src, d / ".config")
    sys.path.insert(0, str(rms_dir))
    import RMS.ConfigReader as cr  # noqa: E402
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = cr.parse(str(d / ".config"))
    return {k: v for k, v in vars(cfg).items() if k not in SKIP}


def rms_audit_sets(text: str) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OPTIONS MISSING IN .CONFIG"):
            cur = "missing"
        elif s.startswith("OPTIONS COMMENTED OUT"):
            cur = "disabled"
        elif s.startswith("OPTIONS IN .CONFIG FILE NOT IMPLEMENTED"):
            cur = "unknown"
        elif s.startswith("=") or s.startswith("There are no"):
            cur = None
        elif s.startswith("- ") and cur:
            out.setdefault(cur, set()).add(s[2:])
    return {k: sorted(v) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rms-dir", type=Path, default=Path.home() / "source" / "RMS")
    ap.add_argument("--base", type=Path, help="station .config to derive the cases from "
                    "(default: the first ~/source/Stations/*/.config, else <rms-dir>/.config)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory and print its path")
    args = ap.parse_args()

    rms = args.rms_dir.expanduser().resolve()
    py = sys.executable
    base_path = args.base
    if base_path is None:
        found = sorted((Path.home() / "source" / "Stations").glob("*/.config"))
        base_path = found[0] if found else rms / ".config"
    base = Path(base_path).read_text()
    tpl = ConfigFile.load(rms / ".configTemplate")
    known = known_options(rms)

    tmp = Path(tempfile.mkdtemp(prefix="cfg-compare-"))
    differences = 0
    for name, text in build_cases(base).items():
        d = tmp / ("case_" + name)
        d.mkdir()
        (d / ".config").write_text(text)
        recent = name == "recent"

        # RMS's tools
        cmd = [py, "-m", "Utils.MigrateConfig", "-i", str(d / ".config"), "-o", str(d / "configRMS")]
        if recent:
            cmd.append("-r")
        r = subprocess.run(cmd, cwd=rms, capture_output=True, text=True)
        (d / "rms_migrate.log").write_text(r.stdout + r.stderr)
        r = subprocess.run([py, "-m", "Utils.AuditConfig", str(d / ".config"), "--template", str(rms / ".configTemplate"),
                            "--configreader", str(rms / "RMS" / "ConfigReader.py")], cwd=rms, capture_output=True, text=True)
        (d / "rms_audit.txt").write_text(r.stdout + r.stderr)

        # ours
        cf = ConfigFile.load(d / ".config")
        lines, log = migrate(cf, tpl, known, recent=recent)
        (d / "configMine").write_text("\n".join(lines) + "\n")
        mine_audit = {k: sorted(x["key"] for x in v) for k, v in audit(cf, tpl, known).items()}

        print("=" * 72)
        print(name)
        if not (d / "configRMS").exists():
            print("  RMS MigrateConfig produced no output:", (d / "rms_migrate.log").read_text().strip().splitlines()[-1:])
            differences += 1
        else:
            try:
                a = parse_with_rms(rms, d / "configRMS", "rms")
                b = parse_with_rms(rms, d / "configMine", "mine")
                diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
                print("  parsed attributes RMS vs mine:", diff if diff else "identical")
                differences += bool(diff)
            except Exception as exc:  # noqa: BLE001
                print("  parse error:", type(exc).__name__, exc)
                differences += 1
            ra = [l.rstrip() for l in (d / "configRMS").read_text().splitlines() if not TRAILER.match(l)]
            mb = [l.rstrip() for l in lines if not TRAILER.match(l)]
            while ra and not ra[-1]:
                ra.pop()
            while mb and not mb[-1]:
                mb.pop()
            td = [l for l in difflib.unified_diff(ra, mb, "RMS", "mine", n=0, lineterm="")
                  if not l.startswith(("---", "+++", "@@"))]
            print("  text differences (trailers ignored): %d" % len(td))
            for l in td[:10]:
                print("    " + l)
        rl = (d / "rms_migrate.log").read_text()
        print("  decisions RMS : kept=%d ignored=%d preserved=%d" % (rl.count("=> kept"), rl.count("IGNORING"), rl.count("PRESERVING")))
        print("  decisions mine: kept=%d dropped=%d preserved=%d" % (sum("=> kept" in l for l in log), sum("DROPPED" in l for l in log), sum("=> preserved" in l for l in log)))

        ra_sets = rms_audit_sets((d / "rms_audit.txt").read_text())
        if "Traceback" in (d / "rms_audit.txt").read_text():
            print("  audit: RMS AuditConfig crashed (%s)" % (d / "rms_audit.txt").read_text().strip().splitlines()[-1][:80])
        for k in ("missing", "unknown", "disabled"):
            rv, mv = ra_sets.get(k, []), mine_audit.get(k, [])
            print("  audit %-8s %s" % (k, "same" if rv == mv else "RMS=%s mine=%s" % (rv, mv)))
            differences += rv != mv
        print("  mine also: extra=%s duplicate=%s" % (mine_audit.get("extra"), mine_audit.get("duplicate")))

    print("=" * 72)
    print("%d difference(s); see the per-case notes above." % differences)
    if args.keep:
        print("kept:", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
