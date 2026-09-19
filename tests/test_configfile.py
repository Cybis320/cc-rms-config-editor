from pathlib import Path

import pytest

from config_editor.configfile import ConfigFile
from config_editor.fleet import Fleet, Target, check_value, discover

SAMPLE = """; header comment
; second line

[System]

; The assigned station ID
stationID: XX0001

; WGS84 +N (degrees)
latitude: 33.5

; Mean sea level (meters)
; Two lines of help.
elevation: 443.518

; public_latitude: 0.0

[Capture]

; device URL
device: rtsp://192.168.42.101:554/x

fps: 25 ; inline comment


[FireballDetection]

k1: 7.0

[MeteorDetection]

k1: 3.5
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / ".config"
    p.write_text(SAMPLE)
    return p


def test_parse_keeps_layout_and_help(cfg):
    cf = ConfigFile.load(cfg)
    assert cf.sections == ["System", "Capture", "FireballDetection", "MeteorDetection"]
    e = cf.get("System", "stationid")
    assert e.option == "stationID" and e.value == "XX0001"
    assert cf.get("System", "elevation").help == "Mean sea level (meters)\nTwo lines of help."
    assert cf.get("Capture", "fps").value == "25"
    # same option name in two sections stays distinct
    assert cf.get("FireballDetection", "k1").value == "7.0"
    assert cf.get("MeteorDetection", "k1").value == "3.5"
    # commented-out option is not an entry
    assert cf.get("System", "public_latitude") is None
    assert cf.text() == SAMPLE


def test_set_existing_rewrites_only_that_line(cfg):
    cf = ConfigFile.load(cfg)
    assert cf.set("System", "elevation", "450") is True
    assert cf.set("System", "elevation", "450") is False  # unchanged
    assert cf.get("System", "elevation").value == "450"
    out = cf.text()
    assert out.count("\n") == SAMPLE.count("\n")
    assert "elevation: 450\n" in out
    assert out.replace("elevation: 450\n", "elevation: 443.518\n") == SAMPLE


def test_set_keeps_inline_comment(cfg):
    cf = ConfigFile.load(cfg)
    cf.set("Capture", "fps", "24.98")
    assert "fps: 24.98 ; inline comment\n" in cf.text()


def test_set_reenables_commented_option(cfg):
    cf = ConfigFile.load(cfg)
    cf.set("System", "public_latitude", "33.58")
    assert "public_latitude: 33.58\n" in cf.text()
    assert "; public_latitude" not in cf.text()
    assert cf.get("System", "public_latitude").value == "33.58"


def test_set_appends_missing_option_to_section(cfg):
    cf = ConfigFile.load(cfg)
    cf.set("Capture", "protocol", "udp")
    lines = cf.lines
    i = lines.index("protocol: udp")
    assert lines[i - 1] == ""
    assert lines[i - 2].startswith("fps:")
    assert lines[i + 1] == ""  # the two blank lines before [FireballDetection] survive
    assert cf.get("Capture", "protocol").value == "udp"
    assert cf.get("Capture", "fps").line_no < i


def test_set_creates_missing_section(cfg):
    cf = ConfigFile.load(cfg)
    cf.set("Timelapse", "timelapse_generate_captured", "true")
    assert cf.text().endswith("[Timelapse]\n\ntimelapse_generate_captured: true\n")


def test_unset_comments_out(cfg):
    cf = ConfigFile.load(cfg)
    assert cf.unset("System", "elevation") is True
    assert cf.get("System", "elevation") is None
    assert "; elevation: 443.518\n" in cf.text()
    # and set() brings it straight back on the same line
    cf.set("System", "elevation", "1")
    assert "\nelevation: 1\n" in cf.text()


def test_rejects_semicolon(cfg):
    cf = ConfigFile.load(cfg)
    with pytest.raises(ValueError):
        cf.set("System", "elevation", "1 ; no")


def test_save_is_atomic_with_backup(cfg):
    cf = ConfigFile.load(cfg)
    cf.set("System", "elevation", "1")
    bak = cf.save()
    assert bak.exists() and bak.read_text() == SAMPLE
    assert cfg.read_text() == cf.text()
    assert not list(cfg.parent.glob(".config.*.tmp"))
    assert cf.save(backup=False) is None


def test_crlf_roundtrip(tmp_path):
    p = tmp_path / ".config"
    p.write_bytes(b"[System]\r\nstationID: A\r\n")
    cf = ConfigFile.load(p)
    cf.set("System", "stationID", "B")
    cf.save(backup=False)
    assert p.read_bytes() == b"[System]\r\nstationID: B\r\n"


# --- fleet -----------------------------------------------------------------

def make_stations(tmp_path, n=3, **overrides):
    root = tmp_path / "Stations"
    for i in range(n):
        sid = "XX000%d" % (i + 1)
        d = root / sid
        d.mkdir(parents=True)
        text = SAMPLE.replace("XX0001", sid)
        for k, v in overrides.get(sid, {}).items():
            text = text.replace("%s: " % k, "%s: %s ;;" % (k, v))  # marker, rewritten below
            text = "\n".join(
                ("%s: %s" % (k, v)) if l.startswith("%s: %s ;;" % (k, v)) else l for l in text.split("\n"))
        (d / ".config").write_text(text)
    return root


def test_discover_and_matrix(tmp_path):
    root = make_stations(tmp_path, XX0002={"latitude": "34.0"})
    targets = discover(root, tmp_path / "no-rms")
    assert [t.id for t in targets] == ["XX0001", "XX0002", "XX0003"]
    fleet = Fleet.load(targets, tmp_path / "no-rms")
    m = fleet.matrix()
    assert [f["id"] for f in m["files"]] == ["XX0001", "XX0002", "XX0003"]
    assert m["files"][1]["station_id"] == "XX0002"
    sys_opts = {r["key"]: r for r in m["sections"][0]["options"]}
    assert sys_opts["latitude"]["distinct"] == 2
    assert sys_opts["latitude"]["values"] == {"XX0001": "33.5", "XX0002": "34.0", "XX0003": "33.5"}
    assert sys_opts["elevation"]["distinct"] == 1 and sys_opts["elevation"]["missing"] == 0
    assert sys_opts["elevation"]["help"].startswith("Mean sea level")


def test_discover_falls_back_to_rms_root(tmp_path):
    rms = tmp_path / "RMS"
    rms.mkdir()
    (rms / ".config").write_text(SAMPLE)
    targets = discover(tmp_path / "nowhere", rms)
    assert [t.id for t in targets] == ["RMS"]


def test_discover_shared_base_first(tmp_path):
    root = make_stations(tmp_path, n=2)
    (root / ".config").write_text(SAMPLE)
    targets = discover(root, tmp_path / "no-rms")
    assert [(t.id, t.shared) for t in targets] == [("shared", True), ("XX0001", False), ("XX0002", False)]


def test_apply_writes_subset_and_backs_up_once_per_session(tmp_path):
    root = make_stations(tmp_path)
    fleet = Fleet.load(discover(root, tmp_path / "no-rms"), tmp_path / "no-rms")
    done = set()
    r = fleet.apply("System", "elevation", {"XX0001": "500", "XX0002": "500"}, backup_done=done)
    assert r["written"] == ["XX0001", "XX0002"] and not r["conflict"]
    assert set(r["backups"]) == {"XX0001", "XX0002"}
    assert done == {"XX0001", "XX0002"}
    assert (root / "XX0001" / ".config").read_text().count("elevation: 500") == 1
    assert "elevation: 443.518" in (root / "XX0003" / ".config").read_text()
    # second write in the same session: no new backup, and unchanged files are skipped
    r = fleet.apply("System", "elevation", {"XX0001": "501", "XX0002": "500"}, backup_done=done)
    assert r["written"] == ["XX0001"] and r["backups"] == {}
    assert len(list((root / "XX0001").glob(".config.bak.*"))) == 1
    # unset comments out
    r = fleet.apply("System", "elevation", {"XX0003": None}, backup=False)
    assert r["written"] == ["XX0003"]
    assert "; elevation: 443.518" in (root / "XX0003" / ".config").read_text()
    assert fleet.matrix()["sections"][0]["options"][2]["missing"] == 1


def test_apply_detects_stale_file(tmp_path):
    root = make_stations(tmp_path, n=1)
    fleet = Fleet.load(discover(root, tmp_path / "no-rms"), tmp_path / "no-rms")
    stale = fleet.files["XX0001"].mtime - 10
    r = fleet.apply("System", "elevation", {"XX0001": "1"}, expect_mtimes={"XX0001": stale})
    assert r["conflict"] == ["XX0001"] and r["written"] == []
    assert "elevation: 443.518" in (root / "XX0001" / ".config").read_text()


def test_apply_unknown_target(tmp_path):
    root = make_stations(tmp_path, n=1)
    fleet = Fleet.load(discover(root, tmp_path / "no-rms"), tmp_path / "no-rms")
    with pytest.raises(KeyError):
        fleet.apply("System", "elevation", {"nope": "1"})


def test_check_value():
    assert check_value("12", "int") is None
    assert check_value("1.5", "int")
    assert check_value("1.5", "float") is None
    assert check_value("yes", "bool") is None
    assert check_value("maybe", "bool")
    assert check_value("anything", None) is None
    assert check_value("", "int") is None


def test_quota_check(tmp_path):
    root = tmp_path / "Stations"
    (root / "XX0001").mkdir(parents=True)
    (root / "XX0001" / ".config").write_text(
        "[System]\nstationID: XX0001\n[Capture]\nquota_management_enabled: true\nrms_data_quota: 965\n"
        "arch_dir_quota: 10\nbz2_files_quota: 10\ncontinuous_capture_quota: 965\nlog_files_quota: 0.2\n")
    fleet = Fleet.load(discover(root, tmp_path / "no-rms"), tmp_path / "no-rms")
    checks = fleet.matrix()["checks"]
    assert len(checks) == 1 and checks[0]["station"] == "XX0001" and "-20.2 GB" in checks[0]["message"]
    fleet.apply("Capture", "continuous_capture_quota", {"XX0001": "680"}, backup=False)
    assert fleet.checks() == []
    fleet.apply("Capture", "quota_management_enabled", {"XX0001": "false"}, backup=False)
    fleet.apply("Capture", "continuous_capture_quota", {"XX0001": "965"}, backup=False)
    assert fleet.checks() == []   # quota management off: RMS never looks at the split
