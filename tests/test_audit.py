import pytest

from config_editor.audit import audit, audit_template, migrate, unified_diff
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
    assert a == {"missing": [], "unknown": [], "disabled": [], "extra": [], "duplicate": []}
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


# --- edge cases MigrateConfig calls out --------------------------------------

DUP = """[Capture]
fps: 25
; a comment
fps: 24.98
quota_management_disabled: true
quota_management_enabled: false

[Calibration]
star_catalog_file: BSC5
"""


def test_duplicates_detected_last_wins_and_dedupe(tmp_path):
    p = tmp_path / ".config"
    p.write_text(DUP)
    cf = ConfigFile.load(p)
    assert cf.get("Capture", "fps").value == "24.98"
    assert cf.duplicates == {("Capture", "fps"): [1]}
    a = audit(cf, None, None)
    assert a["duplicate"] == [{"section": "Capture", "name": "fps", "key": "fps", "value": "24.98", "copies": 2}]
    assert cf.dedupe("Capture", "fps") == 1
    assert cf.duplicates == {} and cf.lines[1] == "; fps: 25"
    assert cf.dedupe("Capture", "fps") == 0
    # set() edits the surviving copy
    cf.set("Capture", "fps", "30")
    assert cf.lines[3] == "fps: 30"


def test_migrate_dedupes_legacy_both_present_and_recent(tmp_path):
    t = tmp_path / ".configTemplate"
    t.write_text("[Capture]\nfps: 25.0\nquota_management_enabled: false\n\n[Calibration]\nstar_catalog_file: gaia_dr2_mag_11.5.npy\n")
    p = tmp_path / ".config"
    p.write_text(DUP)
    cf, tpl = ConfigFile.load(p), ConfigFile.load(t)
    known = {"fps", "quota_management_enabled", "star_catalog_file"}
    new, log = migrate(cf, tpl, known)
    text = "\n".join(new)
    assert text.count("\nfps:") == 1 and "fps: 24.98" in text
    # the file already had the new option: the legacy one is dropped, not converted
    assert "quota_management_enabled: false" in text and "quota_management_disabled" not in text
    assert any("superseded by quota_management_enabled" in l for l in log)
    assert any("2 copies" in l and "fps" in l for l in log)
    assert "star_catalog_file: BSC5" in text                         # kept without --recent
    new, log = migrate(cf, tpl, known, recent=True)
    assert "star_catalog_file: gaia_dr2_mag_11.5.npy" in "\n".join(new)
    assert any("RECENT template default" in l for l in log)


def test_legacy_quota_true_becomes_enabled_false(tmp_path):
    t = tmp_path / ".configTemplate"
    t.write_text("[Capture]\nquota_management_enabled: false\n")
    p = tmp_path / ".config"
    p.write_text("[Capture]\nquota_management_disabled: true\n")
    new, _ = migrate(ConfigFile.load(p), ConfigFile.load(t), None)
    assert "quota_management_enabled: false" in new
    p.write_text("[Capture]\nquota_management_disabled: no\n")
    new, _ = migrate(ConfigFile.load(p), ConfigFile.load(t), None)
    assert "quota_management_enabled: true" in new


def test_set_inserts_before_trailing_comment_after_migration(tmp_path):
    t = tmp_path / ".configTemplate"
    t.write_text("[Colors]\nmeteor_color: red\n")
    p = tmp_path / ".config"
    p.write_text("[Colors]\nmeteor_color: blue\n")
    cf, tpl = ConfigFile.load(p), ConfigFile.load(t)
    new, _ = migrate(cf, tpl, None)
    cf.lines = new
    cf._index()
    cf.set("Colors", "sporadic_color", "gray")
    assert cf.lines[-1].startswith("; Migrated to the")
    i = cf.lines.index("sporadic_color: gray")
    assert cf.lines[i - 2:i] == ["meteor_color: blue", ""]
    # and a section holding only comments still takes the option after them
    cf2 = ConfigFile(path=p, lines=["[X]", "; only a comment", "", "[Y]", "a: 1"])
    cf2._index()
    cf2.set("X", "b", "2")
    assert cf2.lines[:4] == ["[X]", "; only a comment", "", "b: 2"]


def test_migrate_keeps_station_newline_and_empty_values(tmp_path):
    t = tmp_path / ".configTemplate"
    t.write_text("[Build]\nwin_pc_weave:\nlinux_pc_weave: -O3\n")
    p = tmp_path / ".config"
    p.write_bytes(b"[Build]\r\nwin_pc_weave:\r\nlinux_pc_weave: -O3 -march=native\r\n")
    cf, tpl = ConfigFile.load(p), ConfigFile.load(t)
    new, log = migrate(cf, tpl, None)
    cf.lines = new
    cf.save(backup=False)
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"win_pc_weave:\r\n" in raw and b"linux_pc_weave: -O3 -march=native\r\n" in raw
    assert not any("win_pc_weave" in l for l in log)   # equal empty values are not "kept"


def test_fleet_include_root_and_migrate_log(tmp_path):
    from config_editor.fleet import Fleet, discover
    rms = tmp_path / "RMS"
    (rms / "RMS").mkdir(parents=True)
    (rms / ".configTemplate").write_text(TEMPLATE)
    (rms / ".config").write_text(STATION.replace("US005B", "XX0001"))
    (rms / "RMS" / "ConfigReader.py").write_text("")
    root = tmp_path / "Stations"
    (root / "US005A").mkdir(parents=True)
    (root / "US005A" / ".config").write_text(STATION.replace("US005B", "US005A"))
    assert [t.id for t in discover(root, rms)] == ["US005A"]
    targets = discover(root, rms, include_root=True)
    assert [t.id for t in targets] == ["US005A", "RMS"]
    fleet = Fleet.load(targets, rms)
    res = fleet.migrate(["US005A", "RMS"], apply=True, backup=False)
    assert all(r["written"] for r in res.values())
    log = (root / "US005A" / "US005A_MigrateConfig.log").read_text()
    assert "Migration Log" in log and "kept '33.58'" in log and "applied successfully" in log
    assert (rms / "XX0001_MigrateConfig.log").exists()


def test_audit_template_dev_report(files):
    _, template = files
    known = KNOWN | {"hot_pixels_file", "mask", "brightness", "public_latitude"}
    ta = audit_template(template, known)
    # mask/brightness are on the omit list; public_latitude is commented out in the template
    assert ta["reader_not_in_template"] == ["hot_pixels_file", "meteor_color", "star_gate_factor"]
    assert ta["template_not_in_reader"] == []
    ta = audit_template(template, KNOWN - {"fps"})
    assert ta["template_not_in_reader"] == ["fps"]
    assert audit_template(None, KNOWN) == {"reader_not_in_template": [], "template_not_in_reader": []}
