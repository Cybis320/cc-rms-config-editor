import pytest

from config_editor.audit import audit, migrate, unified_diff
from config_editor.configfile import ConfigFile

TEMPLATE = """; template header

[System]

; The assigned station ID
stationID: XX0001

; WGS84 +N (degrees)
latitude: 43.19301

; new option
flux_stage_slots: 2

; empty by default
time_server:

; public_latitude: 0.0

[Capture]

; device URL
device: rtsp://192.168.42.10:554/x

fps: 25.0

quota_management_enabled: false

[Timelapse]

timelapse_frames_crf: 25
"""

STATION = """; my header
[System]

stationID: US005B
latitude: 33.58

; star_gate_factor: 3.0

[Capture]

device: rtsp://192.168.42.102:554/x
fps: 24.98 ; measured
quota_management_disabled: false
old_bogus_option: 1
star_gate_factor: 2.5

[Colors]

meteor_color: red
"""

KNOWN = {"stationid", "latitude", "flux_stage_slots", "time_server", "public_latitude",
         "device", "fps", "quota_management_enabled", "timelapse_frames_crf",
         "star_gate_factor", "meteor_color"}


@pytest.fixture
def files(tmp_path):
    t = tmp_path / ".configTemplate"
    t.write_text(TEMPLATE)
    s = tmp_path / "US005B" / ".config"
    s.parent.mkdir()
    s.write_text(STATION)
    return ConfigFile.load(s), ConfigFile.load(t)


def test_audit(files):
    station, template = files
    a = audit(station, template, KNOWN)
    assert [(d["section"], d["key"], d["template_value"]) for d in a["missing"]] == [
        ("Capture", "quota_management_enabled", "false"),
        ("System", "flux_stage_slots", "2"),
        ("System", "time_server", ""),
        ("Timelapse", "timelapse_frames_crf", "25"),
    ]
    assert [d["key"] for d in a["unknown"]] == ["old_bogus_option", "quota_management_disabled"]
    assert [d["key"] for d in a["extra"]] == ["star_gate_factor", "meteor_color"]
    # commented-out: only names RMS/template know, so "; my header" style lines never show
    assert [(d["section"], d["key"], d["value"]) for d in a["disabled"]] == [("System", "star_gate_factor", "3.0")]


def test_audit_without_reader_or_template(files):
    station, template = files
    a = audit(station, None, None)
    assert a == {"missing": [], "unknown": [], "disabled": [], "extra": []}
    a = audit(station, template, None)
    assert a["unknown"] == [] and len(a["missing"]) == 4
    assert [d["key"] for d in a["extra"]] == ["old_bogus_option", "quota_management_disabled", "star_gate_factor", "meteor_color"]


def test_migrate(files):
    station, template = files
    new_lines, log = migrate(station, template, KNOWN)
    text = "\n".join(new_lines) + "\n"
    cf = ConfigFile(path=station.path, lines=new_lines)
    cf._index()
    # template layout, station values where they differ
    assert cf.sections == ["System", "Capture", "Timelapse", "Colors"]
    assert cf.get("System", "stationID").value == "US005B"
    assert cf.get("System", "latitude").value == "33.58"
    assert cf.get("System", "flux_stage_slots").value == "2"        # added from the template
    assert cf.get("System", "time_server").value == ""
    assert cf.get("Capture", "fps").value == "24.98"
    assert "fps: 24.98\n" in text                                    # template line, station value
    # legacy rename folded in
    assert cf.get("Capture", "quota_management_enabled").value == "true"
    assert cf.get("Capture", "quota_management_disabled") is None
    # known extras preserved in their section, unknown dropped
    assert cf.get("Capture", "star_gate_factor").value == "2.5"
    assert cf.get("Colors", "meteor_color").value == "red"
    assert cf.get("Capture", "old_bogus_option") is None
    assert "; The following options were preserved but are not in the template\nstar_gate_factor: 2.5\n" in text
    assert text.count("preserved but are not in the template") == 2
    assert new_lines[-1].startswith("; Migrated to the .configTemplate layout by config-editor on ")
    # comments come from the template
    assert "; new option" in text and "; my header" not in text
    assert any("DROPPED" in l and "old_bogus_option" in l for l in log)
    assert any("legacy option renamed" in l for l in log)
    assert any("kept '24.98'" in l for l in log)
    # idempotent: migrating the result changes nothing but the trailer
    again, _ = migrate(cf, template, KNOWN)
    strip = lambda ls: [l for l in ls if not l.startswith("; Migrated to the")]
    assert strip(again) == strip(new_lines)


def test_unified_diff(files):
    station, template = files
    new_lines, _ = migrate(station, template, KNOWN)
    d = unified_diff(station.lines, new_lines, "x")
    assert d.startswith("--- x\n+++ x (migrated)\n")
    assert "-quota_management_disabled: false" in d
    assert "+quota_management_enabled: true" in d


def test_fleet_migrate_and_audit(tmp_path):
    from config_editor.fleet import Fleet, discover
    rms = tmp_path / "RMS"
    (rms / "RMS").mkdir(parents=True)
    (rms / ".configTemplate").write_text(TEMPLATE)
    (rms / "RMS" / "ConfigReader.py").write_text(
        "\n".join('parser.has_option(section, "%s")' % k for k in KNOWN if k != "stationid"))
    root = tmp_path / "Stations"
    for sid in ("US005A", "US005B"):
        (root / sid).mkdir(parents=True)
        text = STATION.replace("US005B", sid)
        if sid == "US005B":
            text = text.replace("star_gate_factor: 2.5", "; star_gate_factor: 2.5")
        (root / sid / ".config").write_text(text)
    fleet = Fleet.load(discover(root, rms), rms)
    assert fleet.known and "stationid" in fleet.known and "fps" in fleet.known

    m = fleet.matrix()
    assert m["template"].endswith(".configTemplate") and m["rms_known"]
    assert [s["name"] for s in m["sections"]] == ["System", "Capture", "Timelapse", "Colors"]
    rows = {(s["name"], r["key"]): r for s in m["sections"] for r in s["options"]}
    assert rows[("System", "flux_stage_slots")]["template"] == "2"
    assert rows[("System", "flux_stage_slots")]["missing"] == 2
    assert rows[("Capture", "old_bogus_option")]["known"] is False
    assert rows[("Capture", "fps")]["known"] is True
    # a commented-out value shows against the missing cell; options only ever commented out get no row
    assert rows[("Capture", "star_gate_factor")]["values"] == {"US005A": "2.5", "US005B": None}
    assert rows[("Capture", "star_gate_factor")]["disabled"] == {"US005B": "2.5"}
    assert rows[("Capture", "star_gate_factor")]["template"] is None
    assert ("System", "star_gate_factor") not in rows

    a = fleet.audit()
    assert set(a["stations"]) == {"US005A", "US005B"}
    assert len(a["stations"]["US005A"]["missing"]) == 4
    assert [d["key"] for d in a["stations"]["US005B"]["disabled"]] == ["star_gate_factor", "star_gate_factor"]

    dry = fleet.migrate(["US005A"])
    assert dry["US005A"]["changed"] and not dry["US005A"]["written"]
    assert "stationID: US005B" not in (root / "US005A" / ".config").read_text()
    done = set()
    res = fleet.migrate(["US005A", "US005B"], apply=True, backup_done=done)
    assert all(r["written"] for r in res.values()) and done == {"US005A", "US005B"}
    assert (root / "US005A" / ".config").read_text().startswith("; template header")
    assert fleet.files["US005A"].get("Capture", "quota_management_enabled").value == "true"
    assert len(list((root / "US005A").glob(".config.bak.*"))) == 1
    # now clean
    a = fleet.audit()["stations"]["US005A"]
    assert a["missing"] == [] and a["unknown"] == []
    res = fleet.migrate(["US005A"], apply=True, backup_done=done)
    assert not res["US005A"]["changed"] and not res["US005A"]["written"]

    with pytest.raises(KeyError):
        fleet.migrate(["nope"])
