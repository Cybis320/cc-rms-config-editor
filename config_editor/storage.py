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
import re
import statistics
import threading
import time
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
                    "page": "/storage",
                    "message": "the %d station%s on it may hold %.0f GB (rms_data_quota) and each reserves room for "
                               "its next capture (%.0f GB together): %.0f GB on a %.0f GB disk. The quotas alone do not "
                               "make room, so RMS's free-space loop deletes the rest at capture start, from whichever "
                               "station starts first (and if the bitrate is higher than declared, the disk fills)"
                               % (n, "s" if n > 1 else "", g["sum_quota_gb"], g["sum_reserve_gb"],
                                  g["sum_quota_gb"] + g["sum_reserve_gb"], g["total_gb"])})
    return out


# --- simulator inputs ----------------------------------------------------------
#
# The Storage page simulates RMS's cleanup in the browser; the server only says
# what each station is set to and how big each kind of data is per night. Only raw
# video depends on the camera's bitrate, which the operator declares; the rest is
# measured from what is on disk (and editable on the page).

# Options the simulator reads, with ConfigReader's defaults (used when a file lacks one)
SIM_OPTIONS = {
    "raw_video_save": False, "continuous_capture": False, "save_frames": True,
    "raw_video_bitrate_mbps": None, "extra_space_gb": 6.0,
    "capt_dirs_to_keep": 8, "arch_dirs_to_keep": 20, "bz2_files_to_keep": 20,
    "frame_days_to_keep": 4, "video_days_to_keep": 2, "times_days_to_keep": 8, "logdays_to_keep": 30,
    "quota_management_enabled": False, "rms_data_quota": None, "arch_dir_quota": None,
    "bz2_files_quota": None, "continuous_capture_quota": None, "log_files_quota": None,
}
SIM_SECTION = "Capture"

_SESSION_RE = re.compile(r"_(\d{8})_(\d{6})")
# Measuring walks the newest night directories; on a cold spinning disk that takes a
# minute, so it runs in the background and the results are kept across restarts.
MEASURE_CACHE = Path(os.environ.get("CONFIG_EDITOR_MEASURED") or
                     (Path.home() / ".local" / "state" / "config-editor" / "measured.json"))
MEASURE_TTL = 6 * 3600
_measured: dict | None = None     # "data_dir|stationID" -> {"at": epoch, "sizes": {...}}
_measuring = threading.Lock()


def _size(path: Path) -> int:
    """Bytes under ``path`` (a file or a tree); 0 when it vanishes meanwhile."""
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, _dirs, names in os.walk(path):
            for n in names:
                try:
                    total += os.lstat(os.path.join(root, n)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _sessions(dir_path: Path, sid: str) -> list[Path]:
    """Night directories (``<ID>_YYYYMMDD_HHMMSS_...``) oldest first, as getNightDirs."""
    try:
        return sorted((p for p in dir_path.iterdir()
                       if p.is_dir() and p.name.startswith(sid + "_") and _SESSION_RE.search(p.name)),
                      key=lambda p: p.name)
    except OSError:
        return []


def _median_gb(values: list[int]) -> float | None:
    return statistics.median(values) / GB if values else None


def measure(data_dir: str, sid: str, cfg: dict) -> dict:
    """Per-night sizes of everything but raw video, from the most recent data.

    ``captured_gb`` etc. are per NIGHT (all sessions of it together); ``sessions`` is
    how many capture sessions (night directories) a night makes: every restart
    starts a new one, and the *_dirs_to_keep limits count directories, not nights.
    The newest directory is skipped: it may still be filling. None when there is
    nothing to measure.
    """
    root = Path(data_dir).expanduser()
    out: dict = {}

    capt = _sessions(root / cfg.get("captured_dir", "CapturedFiles"), sid)
    # sessions per night: directories started within the last 14 nights / nights spanned
    days = sorted({_SESSION_RE.search(p.name).group(1) for p in capt})
    recent_days = days[-14:]
    recent = [p for p in capt if _SESSION_RE.search(p.name).group(1) in recent_days]
    sessions = (len(recent) / len(recent_days)) if recent_days else None
    out["sessions"] = sessions
    per_session = _median_gb([_size(p) for p in capt[-6:-1]])
    out["captured_gb"] = per_session * sessions if per_session is not None and sessions else None

    arch_dir = root / cfg.get("archived_dir", "ArchivedFiles")
    arch = _sessions(arch_dir, sid)
    per_session = _median_gb([_size(p) for p in arch[-6:-1]])
    out["archived_gb"] = per_session * sessions if per_session is not None and sessions else None

    bz2: dict = {}
    try:
        for p in arch_dir.iterdir():
            if p.is_file() and p.name.startswith(sid + "_") and p.name.endswith(".bz2"):
                m = _SESSION_RE.search(p.name)
                if m:
                    bz2.setdefault(m.group(0), []).append(p.stat().st_size)
    except OSError:
        pass
    groups = [sum(v) for _k, v in sorted(bz2.items())][-6:-1]
    out["bz2_gb"] = _median_gb(groups) * sessions if groups and sessions else None
    out["bz2_files_per_session"] = round(statistics.median(len(v) for v in bz2.values())) if bz2 else 2

    # processed frames: <ID>_<from>_to_<to>_frames*.{tar,mp4,json} at the top of FramesFiles,
    # one set per session; frame_days_to_keep counts their distinct start dates
    frames_dir = root / cfg.get("frame_dir", "FramesFiles")
    fsets: dict = {}
    try:
        for p in frames_dir.iterdir():
            if p.is_file() and "_to_" in p.name:
                fsets.setdefault(p.name.split("_to_")[0], []).append(p.stat().st_size)
    except OSError:
        pass
    fdays: dict = {}
    for k, v in fsets.items():
        m = re.search(r"(\d{8})-\d{6}$", k)
        if m:
            fdays[m.group(1)] = fdays.get(m.group(1), 0) + sum(v)
    vals = [v for _k, v in sorted(fdays.items())][-6:-1]
    out["frames_gb"] = _median_gb(vals)

    # frame-time archives: one per day under TimeFiles/<year>/
    times_dir = root / cfg.get("times_dir", "TimeFiles")
    tdays = []
    try:
        for year in sorted(times_dir.iterdir()):
            if year.is_dir():
                tdays += [_size(p) for p in sorted(year.iterdir())]
    except OSError:
        pass
    out["times_gb"] = _median_gb(tdays[-6:-1])

    # logs: size of the station's log dir per distinct day of log file
    log_dir = root / cfg.get("log_dir", "logs")
    ldays: dict = {}
    try:
        for p in log_dir.iterdir():
            m = re.search(r"_(\d{8})_", p.name)
            if p.is_file() and m:
                ldays[m.group(1)] = ldays.get(m.group(1), 0) + p.stat().st_size
    except OSError:
        pass
    out["logs_gb"] = _median_gb([v for _k, v in sorted(ldays.items())][-8:-1])

    return out


def _cache() -> dict:
    global _measured
    if _measured is None:
        try:
            _measured = json.loads(MEASURE_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _measured = {}
    return _measured


def _jobs(fleet) -> list[tuple[str, str, str, dict]]:
    """(cache key, data_dir, stationID, dir names) per station."""
    jobs = []
    for t in fleet.targets:
        if t.shared:
            continue
        cf = fleet.files[t.id]

        def raw(opt, section="Capture"):
            e = cf.get(section, opt)
            return e.value if e is not None and e.value != "" else None

        data_dir = raw("data_dir") or "~/RMS_data"
        sid = raw("stationID", "System") or t.id
        dirs = {k: raw(k) or d for k, d in (("captured_dir", "CapturedFiles"), ("archived_dir", "ArchivedFiles"),
                                            ("frame_dir", "FramesFiles"), ("times_dir", "TimeFiles"),
                                            ("log_dir", "logs"))}
        jobs.append(("%s|%s" % (data_dir, sid), data_dir, sid, dirs))
    return jobs


def stale(fleet) -> bool:
    cache = _cache()
    now = time.time()
    return any(k not in cache or now - cache[k]["at"] > MEASURE_TTL for k, *_ in _jobs(fleet))


def measuring() -> bool:
    return _measuring.locked()


def measure_all(fleet) -> None:
    """Measure every station and persist the results; one run at a time."""
    if not _measuring.acquire(blocking=False):
        return
    try:
        cache = _cache()
        for key, data_dir, sid, dirs in _jobs(fleet):
            cache[key] = {"at": time.time(), "sizes": measure(data_dir, sid, dirs)}
        try:
            MEASURE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MEASURE_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    finally:
        _measuring.release()


def measure_in_background(fleet, force: bool = False) -> bool:
    """Start measuring when results are missing or old (or ``force``); True if running."""
    if (force or stale(fleet)) and not measuring():
        threading.Thread(target=measure_all, args=(fleet,), daemon=True).start()
        time.sleep(0.05)
    return measuring()


def _parse(value, default):
    if value is None:
        return default
    if isinstance(default, bool):
        return _truthy(value)
    n = _num(value)
    return n if n is not None else default


def sim_inputs(fleet, disk=disk_of) -> dict:
    """Everything the Storage page needs, per disk: size, the stations on it with
    their settings (parsed, ConfigReader defaults filled in) and the last measured
    size of each kind of data per night (None until measured)."""
    cache = _cache()
    jobs = {t.id: j for t, j in zip([t for t in fleet.targets if not t.shared], _jobs(fleet))}
    groups: dict = {}
    for t in fleet.targets:
        if t.shared:
            continue
        cf = fleet.files[t.id]

        def raw(opt, section=SIM_SECTION):
            e = cf.get(section, opt)
            return e.value if e is not None and e.value != "" else None

        s = station(t.id, cf, t.path.parent)
        settings = {k: _parse(raw(k), d) for k, d in SIM_OPTIONS.items()}
        present = {k: raw(k) is not None for k in SIM_OPTIONS}
        hit = cache.get(jobs[t.id][0])
        d = disk(s["data_dir"])
        key = d["key"] if d else ("?", s["data_dir"])
        g = groups.setdefault(key, {"mount": d["mount"] if d else None,
                                    "total_gb": d["total_gb"] if d else None,
                                    "free_gb": d["free_gb"] if d else None, "stations": []})
        g["stations"].append({
            "id": t.id, "data_dir": s["data_dir"], "settings": settings, "present": present,
            "night_hours": s["night_hours"], "ff_gb": s["ff_gb"], "camera_mbps": s["camera_mbps"],
            "measured": hit["sizes"] if hit else None, "measured_at": hit["at"] if hit else None,
        })
    return {"disks": list(groups.values()), "options": SIM_OPTIONS, "section": SIM_SECTION,
            "measuring": measuring()}
