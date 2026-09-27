import json
import math

import pytest

from config_editor import storage
from config_editor.fleet import Fleet, discover

GB = storage.GB

CONFIG = """[System]
stationID: {sid}
latitude: 33.58

[Capture]
data_dir: {data}
width: 1920
height: 1080
fps: 25.0
save_frames: true
continuous_capture: true
raw_video_save: true
{mbps}extra_space_gb: 120
video_days_to_keep: 14
quota_management_enabled: true
rms_data_quota: 965
arch_dir_quota: 10
bz2_files_quota: 10
continuous_capture_quota: 681
log_files_quota: 0.2
"""


def fleet_of(tmp_path, n=6, mbps="27", total_gb=7392):
    root = tmp_path / "Stations"
    for i in range(n):
        sid = "US005%s" % "ABCDEF"[i]
        (root / sid).mkdir(parents=True)
        (root / sid / ".config").write_text(CONFIG.format(
            sid=sid, data=tmp_path / "RMS_data" / sid,
            mbps="raw_video_bitrate_mbps: %s\n" % mbps if mbps else ""))
    fleet = Fleet.load(discover(root, tmp_path / "no-rms"), tmp_path / "no-rms")
    disk = lambda path: {"key": 1, "mount": str(tmp_path / "RMS_data"), "total_gb": total_gb, "free_gb": 100.0}
    return fleet, disk


def test_reserve_mirrors_rms(tmp_path):
    fleet, disk = fleet_of(tmp_path, n=1)
    s = storage.plan(fleet, disk)["disks"][0]["stations"][0]
    assert s["video_gb"] == pytest.approx(86400 * 27e6 / 8 / GB)        # continuous: a whole day
    assert s["video_gb"] == pytest.approx(271.57, abs=0.01)             # what the patched RMS logs
    assert s["ff_gb"] == pytest.approx(s["night_hours"] * 3600 * 25 / 256 * 1920 * 1080 * 4 / GB)
    assert s["reserve_gb"] == pytest.approx(s["video_gb"] + s["ff_gb"] + 3 + 120)
    assert s["video_days"] == pytest.approx(681 / s["video_gb"])        # ~2.5 days, not video_days_to_keep 14
    assert s["capt_gb"] == pytest.approx(965 - 681 - 10 - 10 - 0.2)


def test_night_only_capture_budgets_the_night(tmp_path):
    fleet, disk = fleet_of(tmp_path, n=1)
    fleet.apply("Capture", "continuous_capture", {"US005A": "false"}, backup=False)
    s = storage.plan(fleet, disk)["disks"][0]["stations"][0]
    assert s["video_hours"] == s["night_hours"] < 24
    assert s["video_gb"] == pytest.approx(s["night_hours"] * 3600 * 27e6 / 8 / GB)


def test_longest_night():
    assert 12 < storage.longest_night_hours(33.58) < 14.5
    assert storage.longest_night_hours(-33.58) == pytest.approx(storage.longest_night_hours(33.58))
    assert storage.longest_night_hours(80) == 24.0
    assert storage.longest_night_hours(0) == pytest.approx(12 - 2 * 5.43 / 15 / math.cos(math.radians(23.44)), abs=0.05)


def test_shared_disk_overcommitted_is_flagged_and_suggestion_fits(tmp_path):
    fleet, disk = fleet_of(tmp_path)
    g = storage.plan(fleet, disk)["disks"][0]
    assert g["judgeable"] and g["fits"] is False
    assert g["sum_quota_gb"] == 6 * 965
    checks = storage.checks(fleet, disk)
    assert len(checks) == 1 and checks[0]["option"] == "rms_data_quota" and "6 stations" in checks[0]["message"]

    sug = g["suggestion"]
    assert sug["ok"]
    per = sug["per_station_gb"]
    assert 6 * per + g["sum_reserve_gb"] <= 7392 * (1 - storage.DISK_MARGIN)
    a = sug["stations"]["US005A"]
    assert a["rms_data_quota"] == per
    assert a["continuous_capture_quota"] == 681 + per - 965   # the captured/archive/log split is kept

    for sid, v in sug["stations"].items():
        for k in ("rms_data_quota", "continuous_capture_quota"):
            fleet.apply("Capture", k, {sid: str(v[k])}, backup=False)
    assert storage.plan(fleet, disk)["disks"][0]["fits"] is True
    assert storage.checks(fleet, disk) == []


def test_without_declared_bitrate_nothing_is_judged(tmp_path):
    fleet, disk = fleet_of(tmp_path, mbps=None)
    g = storage.plan(fleet, disk)["disks"][0]
    assert all(s["needs_mbps"] for s in g["stations"])
    assert not g["judgeable"] and g["fits"] is None and g["suggestion"] is None
    assert storage.checks(fleet, disk) == []


def test_fleet_checks_include_the_disk(tmp_path, monkeypatch):
    fleet, disk = fleet_of(tmp_path)
    monkeypatch.setattr(storage, "disk_of", disk)
    monkeypatch.setattr(storage.checks, "__defaults__", (disk,))
    assert any(c["station"].startswith("disk ") for c in fleet.matrix()["checks"])


def test_camera_bitrate_hint(tmp_path):
    (tmp_path / "camera_settings.json").write_text(json.dumps({"init": [
        ["SetParam", "Encode", "Video", "BitRateControl", "CBR"],
        ["SetParam", "Encode", "Video", "BitRate", "21504"]]}))
    assert storage.camera_bitrate_mbps(tmp_path, "./missing.json") == pytest.approx(21.504)
    assert storage.camera_bitrate_mbps(tmp_path / "nowhere", None) is None


def test_disk_of_walks_up_to_an_existing_dir(tmp_path):
    d = storage.disk_of(str(tmp_path / "not" / "yet"))
    assert d is not None and d["total_gb"] > 0
    assert d["key"] == storage.disk_of(str(tmp_path))["key"]


def _write(path, nbytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * nbytes)


def test_measure_per_night_sizes(tmp_path):
    data = tmp_path / "RMS_data" / "XX0001"
    MB = 1024 ** 2
    # six nights, the 3rd restarted once (two directories); the newest dir is still filling
    nights = ["20260920_020000_1", "20260921_020000_1", "20260922_020000_1", "20260922_060000_1",
              "20260923_020000_1", "20260924_020000_1", "20260925_020000_1"]
    for i, n in enumerate(nights):
        _write(data / "CapturedFiles" / ("XX0001_" + n) / "FF.fits", 10 * MB if i < len(nights) - 1 else 1 * MB)
        _write(data / "ArchivedFiles" / ("XX0001_" + n) / "a.fits", 2 * MB)
        _write(data / "ArchivedFiles" / ("XX0001_%s_imgdata.tar.bz2" % n), 1 * MB)
        _write(data / "ArchivedFiles" / ("XX0001_%s_metadata.tar.bz2" % n), 1 * MB)
    for h in range(8):   # stills: 20 MB an hour, the newest hour still filling
        _write(data / "FramesFiles" / "2026" / "20260925-268" / ("20260925-268_%02d" % h) / "a.png", (20 if h < 7 else 5) * MB)
    for d in ("20260921", "20260922", "20260923", "20260924", "20260925"):
        _write(data / "FramesFiles" / ("XX0001_%s-020000_to_%s-120000_frames_timelapse.tar" % (d, d)), 3 * MB)
    m = storage.measure(str(data), "XX0001", {})
    assert m["sessions"] == pytest.approx(7 / 6)
    assert m["captured_gb"] == pytest.approx(10 * MB / GB * 7 / 6)          # per night, both sessions
    assert m["archived_gb"] == pytest.approx(2 * MB / GB * 7 / 6)
    assert m["bz2_gb"] == pytest.approx(2 * MB / GB * 7 / 6)
    assert m["bz2_files_per_session"] == 2
    assert m["frames_gb"] == pytest.approx(3 * MB / GB)
    assert m["stills_gb_per_hour"] == pytest.approx(20 * MB / GB)
    assert m["times_gb"] is None and m["logs_gb"] is None                    # nothing there


def test_sim_inputs_and_background_measure(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MEASURE_CACHE", tmp_path / "measured.json")
    monkeypatch.setattr(storage, "_measured", None)
    fleet, disk = fleet_of(tmp_path, n=2)
    fleet.apply("Capture", "capt_dirs_to_keep", {"US005A": "14"}, backup=False)
    r = storage.sim_inputs(fleet, disk)
    g = r["disks"][0]
    assert len(g["stations"]) == 2 and g["total_gb"] == 7392
    a, b = g["stations"]
    assert a["measured"] is None                                             # not measured yet
    assert a["settings"]["raw_video_bitrate_mbps"] == 27 and a["settings"]["continuous_capture"] is True
    assert a["settings"]["capt_dirs_to_keep"] == 14 and b["settings"]["capt_dirs_to_keep"] == 8   # RMS default
    assert a["present"]["capt_dirs_to_keep"] and not b["present"]["capt_dirs_to_keep"]
    assert storage.stale(fleet)
    storage.measure_all(fleet)
    assert not storage.stale(fleet)
    r = storage.sim_inputs(fleet, disk)
    assert r["disks"][0]["stations"][0]["measured"] is not None
    assert (tmp_path / "measured.json").is_file()


def test_old_measurements_are_not_used(tmp_path, monkeypatch):
    """A cache written before a size was measured must not feed the page zeros."""
    fleet, disk = fleet_of(tmp_path, n=1)
    key = storage._jobs(fleet)[0][0]
    cache = tmp_path / "measured.json"
    cache.write_text(json.dumps({key: {"at": __import__("time").time(), "sizes": {"captured_gb": 30.0}}}))
    monkeypatch.setattr(storage, "MEASURE_CACHE", cache)
    monkeypatch.setattr(storage, "_measured", None)
    assert storage.stale(fleet)
    assert storage.sim_inputs(fleet, disk)["disks"][0]["stations"][0]["measured"] is None
    storage.measure_all(fleet)
    assert not storage.stale(fleet)
    assert "stills_gb_per_hour" in storage.sim_inputs(fleet, disk)["disks"][0]["stations"][0]["measured"]
