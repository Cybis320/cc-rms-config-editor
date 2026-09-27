// Day-by-day simulation of RMS's disk cleanup (RMS/DeleteOldObservations.py) for the
// stations sharing one disk. Pure: no DOM, so node can test it (tests/ui/sim.test.js).
//
// Each simulated day, every station in turn runs deleteOldObservations at capture
// start, then the day's data is written:
//
//   1. deleteOldLogfiles   logs older than logdays_to_keep
//   2. deleteOldDirs       *_dirs_to_keep / *_days_to_keep / bz2_files_to_keep, oldest first
//                          (these count DIRECTORIES or files: every restart is another
//                          captured and archived directory, and two bz2 files; with
//                          continuous capture video_days_to_keep counts today's directory)
//   3. deleteByQuota       only with quota_management_enabled and all five quotas set:
//                          captured dirs to rms_data_quota minus the other four,
//                          archived dirs to arch_dir_quota, bz2 files to bz2_files_quota
//                          (objectsToDelete: a quota of 0 means "not managed"), log files
//                          to log_files_quota and frames+times+video files to
//                          continuous_capture_quota (objectsToDeleteByTime: newest first,
//                          file by file, and a quota of 0 deletes everything)
//   4. free-space loop     until the disk has room for the next capture — FF files for the
//                          night, raw video, 3 GB of frames and extra_space_gb — delete one
//                          video day, one frames day, one captured dir, one archived dir, one
//                          times day, round and round; then bz2 files. Every station checks
//                          the free space of the shared disk as if it were alone on it.
//
// Sizes are in RMS's GB (1024^3 bytes).

(function (root) {
  const GB = 1024 ** 3;
  const CATS = ['video', 'frames', 'times', 'capt', 'arch', 'bz2', 'logs'];

  // Raw video one capture writes: the whole day with continuous_capture, else the night
  function videoPerDay(p) {
    if (!p.raw_video_save || !p.mbps) return 0;
    return p.mbps * 1e6 / 8 * 3600 * (p.continuous_capture ? 24 : p.night_hours) / GB;
  }

  // What deleteOldObservations keeps free for the next capture
  function reserve(p) {
    return p.ff_gb + p.extra_space_gb + videoPerDay(p) + (p.save_frames ? 3 : 0);
  }

  function quotaOn(p) {
    return !!p.quota_management_enabled &&
      ['rms_data_quota', 'arch_dir_quota', 'bz2_files_quota', 'continuous_capture_quota', 'log_files_quota']
        .every((k) => p[k] != null);
  }

  // rms_data_quota minus the other four; at or below 0 RMS warns and passes 0 (= not managed)
  function capturedAllowance(p) {
    if (!quotaOn(p)) return null;
    return p.rms_data_quota - p.arch_dir_quota - p.bz2_files_quota - p.continuous_capture_quota - p.log_files_quota;
  }

  function simulate(p, days) {
    const n = Math.max(1, Math.round(p.stations));
    const vday = videoPerDay(p);
    const need = reserve(p);
    const sessions = Math.max(0.1, p.sessions || 1);
    const bz2PerSession = p.bz2_files_per_session || 2;
    const qOn = quotaOn(p);
    const captQ = qOn ? Math.max(0, capturedAllowance(p)) : 0;
    const usable = p.disk_gb - (p.other_gb || 0);

    const st = [];
    for (let i = 0; i < n; i++) st.push({ items: Object.fromEntries(CATS.map((c) => [c, []])), acc: 0 });
    const deletions = [];        // {day, cat, rule}
    const series = [];           // per day: after cleanup (by category) and peak
    let total = 0;               // GB on disk, all stations

    const drop = (s, cat, idx, rule, day) => {
      const it = s.items[cat].splice(idx, 1)[0];
      total -= it.gb;
      deletions.push({ day, cat, rule });
    };
    const dropOldest = (s, cat, rule, day) => drop(s, cat, 0, rule, day);

    // objectsToDelete: newest first; once the running total passes the quota, that dir
    // and everything older goes. A quota of 0 is "not managed".
    const dirQuota = (s, cat, q, rule, day) => {
      if (!q) return;
      const list = s.items[cat];
      let acc = 0, cut = -1;
      for (let i = list.length - 1; i >= 0; i--) {
        acc += list[i].gb;
        if (acc > q) { cut = i; break; }
      }
      for (let i = cut; i >= 0; i--) drop(s, cat, i, rule, day);
    };

    // objectsToDeleteByTime: file by file, newest first, across the given categories;
    // keeps at most q GB (the file that crosses goes too — at day scale, a trim).
    const timeQuota = (s, cats, q, rule, day) => {
      const all = [];
      for (const c of cats) s.items[c].forEach((it) => all.push([it, c]));
      all.sort((a, b) => b[0].day - a[0].day || cats.indexOf(a[1]) - cats.indexOf(b[1]));
      let acc = 0;
      for (const [it, c] of all) {
        if (acc + it.gb <= q) { acc += it.gb; continue; }
        const keep = Math.max(0, q - acc);
        acc = q;
        if (keep > 1e-9) { total -= it.gb - keep; it.gb = keep; deletions.push({ day, cat: c, rule }); }
        else drop(s, c, s.items[c].indexOf(it), rule, day);
      }
    };

    const free = () => usable - total;

    for (let day = 0; day < days; day++) {
      let loopFailed = false;
      for (const s of st) {
        const I = s.items;
        // 1. logs by age
        if (p.logdays_to_keep > 0) while (I.logs.length && day - I.logs[0].day >= p.logdays_to_keep) dropOldest(s, 'logs', 'logdays_to_keep', day);
        // 2. counts, in deleteOldDirs' order
        const count = (cat, keep, rule, writing = 0) => { if (keep > 0) while (I[cat].length > keep - writing) dropOldest(s, cat, rule, day); };
        count('arch', p.arch_dirs_to_keep, 'arch_dirs_to_keep');
        count('capt', p.capt_dirs_to_keep, 'capt_dirs_to_keep');
        count('frames', p.frame_days_to_keep, 'frame_days_to_keep');
        // with continuous capture the day being written already has its directory, and counts
        count('video', p.video_days_to_keep, 'video_days_to_keep', p.continuous_capture ? 1 : 0);
        count('times', p.times_days_to_keep, 'times_days_to_keep');
        count('bz2', p.bz2_files_to_keep, 'bz2_files_to_keep');
        // 3. quotas
        if (qOn) {
          dirQuota(s, 'capt', captQ, 'captured allowance', day);
          dirQuota(s, 'arch', p.arch_dir_quota, 'arch_dir_quota', day);
          dirQuota(s, 'bz2', p.bz2_files_quota, 'bz2_files_quota', day);
          timeQuota(s, ['logs'], p.log_files_quota, 'log_files_quota', day);
          timeQuota(s, ['frames', 'times', 'video'], p.continuous_capture_quota, 'continuous_capture_quota', day);
        }
        // 4. free space for the next capture
        let guard = 0;
        while (free() <= need && guard++ < 100000) {
          let done = false;
          for (const cat of ['video', 'frames', 'capt', 'arch', 'times']) {
            if (I[cat].length) dropOldest(s, cat, 'free space', day);
            if (free() > need) { done = true; break; }
          }
          if (done) break;
          if (!['video', 'frames', 'capt', 'arch', 'times'].some((c) => I[c].length)) {
            if (I.bz2.length) dropOldest(s, 'bz2', 'free space', day);
            else { loopFailed = true; break; }
          }
        }
      }

      const after = { total };
      for (const c of CATS) after[c] = st.reduce((a, s) => a + s.items[c].reduce((b, it) => b + it.gb, 0), 0);

      // the day's writes; what does not fit is lost (capture stops with ENOSPC)
      let lost = 0;
      for (const s of st) {
        s.acc += sessions;
        const k = Math.floor(s.acc + 1e-9);
        s.acc -= k;
        const add = [];
        if (vday) add.push(['video', vday]);
        if (p.save_frames) add.push(['frames', p.frames_gb]);
        add.push(['times', p.times_gb], ['logs', p.logs_gb]);
        for (let j = 0; j < k; j++) {
          add.push(['capt', p.captured_gb / sessions], ['arch', p.archived_gb / sessions]);
          for (let b = 0; b < bz2PerSession; b++) add.push(['bz2', p.bz2_gb / sessions / bz2PerSession]);
        }
        for (const [cat, gb] of add) {
          const room = Math.max(0, free());
          const put = Math.min(gb, room);
          lost += gb - put;
          if (put > 0) { s.items[cat].push({ day, gb: put }); total += put; }
        }
      }
      series.push({ day, after, peak: total, lost, loopFailed });
    }

    // Retention at the end, just after cleanup: per station, in days (nights for dirs)
    const perDay = {
      video: vday, frames: null, times: null, logs: null,
      capt: sessions, arch: sessions, bz2: sessions * bz2PerSession,
    };
    const kept = {};
    for (const c of CATS) {
      const vals = st.map((s) => {
        const items = s.items[c];
        if (c === 'video') return vday ? items.reduce((a, it) => a + it.gb, 0) / vday - 1 : 0;
        if (c === 'capt' || c === 'arch' || c === 'bz2') return Math.max(0, items.length - (c === 'bz2' ? bz2PerSession * sessions : sessions)) / perDay[c];
        return Math.max(0, items.length - 1);
      });
      kept[c] = { min: Math.max(0, Math.min(...vals)), max: Math.max(0, Math.max(...vals)) };
    }
    // Which rule deleted each category lately (last 14 days, or never)
    const recent = deletions.filter((d) => d.day >= days - 14);
    const binding = {};
    for (const c of CATS) {
      const tally = {};
      for (const d of recent) if (d.cat === c) tally[d.rule] = (tally[d.rule] || 0) + 1;
      const rules = Object.entries(tally).sort((a, b) => b[1] - a[1]).map(([r]) => r);
      binding[c] = rules;
    }
    const last = series.slice(-14);
    return {
      series, kept, binding, need, vday, days, stations: n,
      captured_allowance: capturedAllowance(p), quota_on: qOn,
      lost_per_day: last.reduce((a, d) => a + d.lost, 0) / last.length,
      overflow_days: last.filter((d) => d.lost > 0).length,
      peak_gb: Math.max(...last.map((d) => d.peak)),
      usable_gb: usable,
    };
  }

  const api = { simulate, reserve, videoPerDay, quotaOn, capturedAllowance, GB, CATS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.StorageSim = api;
})(this);
