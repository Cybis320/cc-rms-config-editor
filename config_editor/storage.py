"""Storage planning: what the quotas hold at the operator's bitrate, and whether the
stations sharing a disk fit on it together.

The raw video budget is the operator's ``raw_video_bitrate_mbps``, never a
measurement: after a change of camera settings the video already on disk says
nothing about the next capture. Everything else mirrors
RMS.DeleteOldObservations.deleteOldObservations, which reserves, per station and
at capture start only:

    FF files      night duration x fps / 256 x width x height x 4 bytes
    raw video     capture duration x bitrate (the whole day with continuous_capture)
    frames        3 GB when save_frames is on
    extra         extra_space_gb

Quotas are enforced at capture start only, and every station checks the free
space of the shared disk as if it were alone on it. So a disk is safe when every
station can sit at its rms_data_quota and still have its reserve free:

    sum(rms_data_quota) + sum(reserve) <= disk size

GB throughout are RMS's GB: 1024**3 bytes.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

GB = 1024 ** 3
FRAMES_GB = 3            # RMS's fixed allowance for save_frames
SUN_ALT = -5.43          # RMS starts capture with the Sun 5:26 below the horizon
DISK_MARGIN = 0.03       # left out of the suggestion: filesystem overhead, logs, databases

QUOTAS = ("rms_data_quota", "arch_dir_quota", "bz2_files_quota", "continuous_capture_quota", "log_files_quota")


def longest_night_hours(lat: float, sun_alt: float = SUN_ALT) -> float:
    """Hours between sunset and sunrise (Sun below ``sun_alt``) at the winter solstice."""
    phi = math.radians(lat)
    dec = math.radians(-23.44 if lat >= 0 else 23.44)
    cos_h = (math.sin(math.radians(sun_alt)) - math.sin(phi) * math.sin(dec)) / (math.cos(phi) * math.cos(dec))
    if cos_h >= 1:
        return 24.0      # polar night
    if cos_h <= -1:
        return 0.0
    return 24 - 2 * math.degrees(math.acos(cos_h)) / 15


def camera_bitrate_mbps(config_dir: Path, settings_path: str | None) -> float | None:
    """The encoder BitRate (kbit/s) in the station's camera_settings.json, as Mbit/s.

    Only a hint for the operator: cameras often write well above their nominal CBR.
    Resolved as ConfigReader does: the configured path, else camera_settings.json
    beside the config.
    """
    candidates = []
    if settings_path:
        p = Path(settings_path).expanduser()
        candidates.append(p if p.is_absolute() else config_dir / p)
    candidates.append(config_dir / "camera_settings.json")
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rates = []

        def walk(node):
            if isinstance(node, list):
                if len(node) >= 2 and node[-2] == "BitRate":
                    try:
                        rates.append(float(node[-1]) / 1000)
                    except (TypeError, ValueError):
                        pass
                for x in node:
                    walk(x)
            elif isinstance(node, dict):
                for x in node.values():
                    walk(x)

        walk(data)
        return max(rates) if rates else None
    return None


def disk_of(path: str) -> dict | None:
    """``{key, mount, total_gb, free_gb}`` for the filesystem holding ``path``.

    Walks up to the nearest existing directory (a data_dir RMS has not created yet
    lives where its parent does). Free space is what RMS sees: f_bavail.
    """
    p = Path(path).expanduser()
    while not p.exists():
        if p.parent == p:
            return None
        p = p.parent
    try:
        dev = p.stat().st_dev
        mount = p
        while mount.parent != mount and mount.parent.stat().st_dev == dev:
            mount = mount.parent
        st = os.statvfs(p)
    except OSError:
        return None
    return {"key": dev, "mount": str(mount),
            "total_gb": st.f_blocks * st.f_frsize / GB, "free_gb": st.f_bavail * st.f_frsize / GB}


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def station(tid: str, cf, config_dir: Path) -> dict:
    """One station's reserve and what its quotas hold, from its config alone."""
    def val(section, opt, default=None):
        e = cf.get(section, opt)
        return e.value if e is not None and e.value != "" else default

    lat = _num(val("System", "latitude")) or 0.0
    fps = _num(val("Capture", "fps")) or 25.0
    width = _num(val("Capture", "width")) or 1280
    height = _num(val("Capture", "height")) or 720
    continuous = _truthy(val("Capture", "continuous_capture", "false"))
    raw_video = _truthy(val("Capture", "raw_video_save", "false"))
    save_frames = _truthy(val("Capture", "save_frames", "true"))
    extra = _num(val("Capture", "extra_space_gb")) or 6.0
    mbps = _num(val("Capture", "raw_video_bitrate_mbps"))
    if mbps is not None and mbps <= 0:
        mbps = None

    night_h = longest_night_hours(lat)
    video_h = 24.0 if continuous else night_h
    ff_gb = night_h * 3600 * fps / 256 * width * height * 4 / GB
    video_gb = video_h * 3600 * mbps * 1e6 / 8 / GB if (raw_video and mbps) else 0.0
    frames_gb = FRAMES_GB if save_frames else 0.0

    quotas = {k: _num(val("Capture", k)) for k in QUOTAS}
    quota_on = _truthy(val("Capture", "quota_management_enabled", "false")) and \
        all(v is not None for v in quotas.values())
    capt = None
    if quota_on:
        capt = quotas["rms_data_quota"] - sum(v for k, v in quotas.items() if k != "rms_data_quota")
    video_days = None
    if quota_on and video_gb:
        video_days = quotas["continuous_capture_quota"] / video_gb

    return {
        "id": tid,
        "data_dir": val("Capture", "data_dir", "~/RMS_data"),
        "continuous": continuous,
        "raw_video_save": raw_video,
        "mbps": mbps,
        "camera_mbps": camera_bitrate_mbps(config_dir, val("Capture", "camera_settings_path")) if raw_video else None,
        "needs_mbps": raw_video and not mbps,
        "night_hours": night_h,
        "video_hours": video_h,
        "video_gb": video_gb,
        "ff_gb": ff_gb,
        "frames_gb": frames_gb,
        "extra_gb": extra,
        "reserve_gb": ff_gb + video_gb + frames_gb + extra,
        "quota_on": quota_on,
        "quotas": quotas,
        "capt_gb": capt,
        "video_days": video_days,
        "video_days_to_keep": _num(val("Capture", "video_days_to_keep")),
    }


def plan(fleet, disk=disk_of) -> dict:
    """Per-disk groups of stations, each with its budget and, when it can be judged, a
    suggestion: one rms_data_quota for every station that makes the disk fit, with the
    difference taken from (or given to) continuous_capture_quota so the captured,
    archive, bz2 and log allowances stay as they are.
    """
    groups: dict = {}
    for t in fleet.targets:
        if t.shared:
            continue
        s = station(t.id, fleet.files[t.id], t.path.parent)
        d = disk(s["data_dir"])
        key = d["key"] if d else ("?", s["data_dir"])
        g = groups.setdefault(key, {"disk": d, "stations": []})
        g["stations"].append(s)

    out = []
    for g in groups.values():
        st, d = g["stations"], g["disk"]
        judgeable = d is not None and all(s["quota_on"] and not s["needs_mbps"] for s in st)
        sum_quota = sum(s["quotas"]["rms_data_quota"] for s in st) if judgeable else None
        sum_reserve = sum(s["reserve_gb"] for s in st)
        entry = {"mount": d["mount"] if d else None,
                 "total_gb": d["total_gb"] if d else None,
                 "free_gb": d["free_gb"] if d else None,
                 "stations": st,
                 "judgeable": judgeable,
                 "sum_quota_gb": sum_quota,
                 "sum_reserve_gb": sum_reserve,
                 "fits": None, "suggestion": None}
        if judgeable:
            entry["fits"] = sum_quota + sum_reserve <= d["total_gb"]
            per = math.floor((d["total_gb"] * (1 - DISK_MARGIN) - sum_reserve) / len(st))
            sug = {}
            for s in st:
                cc = s["quotas"]["continuous_capture_quota"] + per - s["quotas"]["rms_data_quota"]
                sug[s["id"]] = {"rms_data_quota": per,
                                "continuous_capture_quota": math.floor(cc),
                                "video_days": (cc / s["video_gb"]) if s["video_gb"] and cc > 0 else None}
            entry["suggestion"] = {"per_station_gb": per, "margin": DISK_MARGIN,
                                   "ok": per > 0 and all(v["continuous_capture_quota"] > 0 for v in sug.values()),
                                   "stations": sug}
        out.append(entry)
    return {"disks": out}


def checks(fleet, disk=disk_of) -> list[dict]:
    """Banner warnings: disks where the stations' quotas and reserves cannot all fit."""
    out = []
    for g in plan(fleet, disk)["disks"]:
        if g["fits"] is not False:
            continue
        n = len(g["stations"])
        out.append({"station": "disk %s" % g["mount"], "option": "rms_data_quota", "section": "Capture",
                    "panel": "storage",
                    "message": "the %d station%s on it may hold %.0f GB (rms_data_quota) and each reserves room for "
                               "its next capture (%.0f GB together): %.0f GB on a %.0f GB disk. RMS enforces the "
                               "quotas only when capture starts, so the disk fills during capture — see Storage"
                               % (n, "s" if n > 1 else "", g["sum_quota_gb"], g["sum_reserve_gb"],
                                  g["sum_quota_gb"] + g["sum_reserve_gb"], g["total_gb"])})
    return out
