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
//   (stills) with save_frames, the frames of the day pile up as PNG/JPG under
//                          FramesFiles/<year>/<day>/<hour> until the daily frames step
//                          turns them into the timelapse (uploaded) and deletes them:
//                          about a day of stills is always on disk. The free-space loop
//                          and frame_days_to_keep only see finished timelapses, but
//                          continuous_capture_quota deletes by file time across video,
//                          frames and times: stills are as old as the video beside them,
//                          so a quota under ~25 h of video + stills deletes the oldest
//                          stills before their timelapse is made. RMS's reserve counts
//                          3 GB for them.
//   4. free-space loop     until the disk has room for the next capture — FF files for the
//                          night, raw video, 3 GB of frames and extra_space_gb — delete one
//                          video day, one frames day, one captured dir, one archived dir, one
//                          times day, round and round; then bz2 files. Every station checks
//                          the free space of the shared disk as if it were alone on it.
//
// Sizes are in RMS's GB (1024^3 bytes).

(function (root) {
  const GB = 1024 ** 3;
  const CATS = ['video', 'stills', 'frames', 'times', 'capt', 'arch', 'bz2', 'logs'];

  // Raw video one capture writes: the whole day with continuous_capture, else the night
  function videoPerDay(p) {
    if (!p.raw_video_save || !p.mbps) return 0;
    return p.mbps * 1e6 / 8 * 3600 * (p.continuous_capture ? 24 : p.night_hours) / GB;
  }

  // What deleteOldObservations keeps free for the next capture
  function reserve(p) {
    return p.ff_gb + p.extra_space_gb + videoPerDay(p) + (p.save_frames ? 3 : 0);
  }

  // Stills waiting for the daily timelapse: the capture hours plus an hour for the step itself
  function stillsBacklog(p) {
    if (!p.save_frames || !p.stills_gb_per_hour) return 0;
    return p.stills_gb_per_hour * ((p.continuous_capture ? 24 : p.night_hours) + 1);
  }

  // The smallest continuous_capture_quota that keeps every still until its timelapse:
  // the oldest waits ~25 h, and the video, frame times and timelapses written beside it
  // count toward the same quota
  function stillsSafeQuota(p) {
    const b = stillsBacklog(p);
    if (!b) return 0;
    const hours = (p.continuous_capture ? 24 : p.night_hours) + 1;
    return b + videoPerDay(p) * hours / (p.continuous_capture ? 24 : p.night_hours) + (p.times_gb || 0) + (p.frames_gb || 0);
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
    // keeps at most q GB. Files of one day are interleaved in time (video, stills and
    // frame times are written side by side), so the day the cut falls in loses the same
    // oldest share of each; every older day goes entirely.
    const timeQuota = (s, cats, q, rule, day) => {
      const byDay = new Map();
      for (const c of cats) for (const it of s.items[c]) {
        if (!byDay.has(it.day)) byDay.set(it.day, []);
        byDay.get(it.day).push([it, c]);
      }
      let acc = 0;
      for (const d of [...byDay.keys()].sort((a, b) => b - a)) {
        const group = byDay.get(d);
        const g = group.reduce((a, [it]) => a + it.gb, 0);
        if (acc + g <= q) { acc += g; continue; }
        const f = Math.max(0, q - acc) / g;
        acc = q;
        for (const [it, c] of group) {
          if (f * it.gb > 1e-9) { total -= it.gb * (1 - f); it.gb *= f; deletions.push({ day, cat: c, rule }); }
          else drop(s, c, s.items[c].indexOf(it), rule, day);
        }
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
          timeQuota(s, ['stills', 'frames', 'times', 'video'], p.continuous_capture_quota, 'continuous_capture_quota', day);
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
      const backlog = stillsBacklog(p);
      for (const s of st) {
        // the frames step turned yesterday's stills into the timelapse and deleted them
        for (const it of s.items.stills.splice(0)) total -= it.gb;
        s.acc += sessions;
        const k = Math.floor(s.acc + 1e-9);
        s.acc -= k;
        const add = [];
        if (vday) add.push(['video', vday]);
        if (backlog) add.push(['stills', backlog]);
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
      stills_gb: stillsBacklog(p),
      stills_cut: deletions.some((d) => d.cat === 'stills' && d.day >= days - 14),
      overflow_days: last.filter((d) => d.lost > 0).length,
      peak_gb: Math.max(...last.map((d) => d.peak)),
      usable_gb: usable,
    };
  }

  // --- auto-tune ----------------------------------------------------------------
  //
  // Simplified: every station on the disk alike. Raw video and CapturedFiles are the
  // science that is not uploaded and is lost on deletion; everything else (archives,
  // bz2, timelapses, frame times, logs) is uploaded or only for the operator and gets a
  // fixed number of days. The precious two get all that is left once the stills
  // backlog, the next capture's reserve and a margin are set aside — the most for
  // which the simulation shows nothing lost to a full disk and the free-space loop
  // never deleting (so the quotas do it, evenly on every station).
  //
  // o: { nice_days, log_days, margin, capt_nights (null: as many nights as raw video days) }
  function tuneFor(p, o, D) {
    const q = { ...p };
    const sess = Math.max(0.1, p.sessions || 1), bpf = p.bz2_files_per_session || 2;
    const vday = videoPerDay(p);
    const up = (x, step = 1) => Math.ceil(x / step - 1e-9) * step;
    q.quota_management_enabled = true;
    // operator data: nice_days of each, quotas with 25% slack so the counts do the deleting
    q.arch_dirs_to_keep = up(o.nice_days * sess);
    q.arch_dir_quota = Math.max(1, up(p.archived_gb * o.nice_days * 1.25));
    q.bz2_files_to_keep = up(o.nice_days * sess * bpf);
    q.bz2_files_quota = Math.max(1, up(p.bz2_gb * o.nice_days * 1.25));
    q.logdays_to_keep = o.log_days;
    q.log_files_quota = Math.max(0.1, up(p.logs_gb * o.log_days * 1.5, 0.1));
    // RMS reserves 3 GB for frames: add what the stills backlog needs beyond it, and room
    // for the night's archive being built
    q.extra_space_gb = up(Math.max(0, stillsBacklog(p) - 3) + p.archived_gb + p.bz2_gb + 5);
    // precious: D days of raw video, N nights of CapturedFiles
    const N = o.capt_nights != null ? o.capt_nights : Math.max(1, Math.round(D));
    const perSession = p.captured_gb / sess;
    const capt = (N * sess + 0.5) * perSession;           // objectsToDelete keeps whole dirs under the allowance
    q.capt_dirs_to_keep = up(N * sess) + 1;
    // timelapses and frame times share continuous_capture_quota, which deletes by age
    // across video, frames and times: they live as long as the raw video, no longer
    const tdays = vday ? up(D) : o.nice_days;
    q.frame_days_to_keep = tdays;
    q.times_days_to_keep = tdays;
    const others = (p.save_frames ? p.frames_gb * tdays : 0) + p.times_gb * tdays;
    // never below 25 h of video + stills: the oldest still must live to its timelapse
    const cc = up(Math.max(vday * D + stillsBacklog(p) + others + 1, stillsSafeQuota(p)));
    q.continuous_capture_quota = vday || p.save_frames ? cc : 1;
    // one day looser than the quota keeps (plus today's directory with continuous capture)
    q.video_days_to_keep = vday ? up(D) + (p.continuous_capture ? 1 : 0) : p.video_days_to_keep;
    q.rms_data_quota = up(capt + q.arch_dir_quota + q.bz2_files_quota + q.continuous_capture_quota + q.log_files_quota);
    return { q, D, N };
  }

  function autotune(p, o) {
    const sp = { ...p, other_gb: (p.other_gb || 0) + (o.margin || 0) * p.disk_gb };
    const days = Math.min(240, Math.max(45, Math.ceil(Math.max(o.nice_days, o.log_days) * 2 + 14)));
    const vday = videoPerDay(p);
    const good = (D) => {
      const t = tuneFor(p, o, D);
      const r = simulate({ ...t.q, other_gb: sp.other_gb }, days);
      const loop = Object.values(r.binding).some((rules) => rules.includes('free space'));
      return { ok: r.lost_per_day === 0 && !loop && !r.stills_cut && r.kept.capt.min >= t.N - 0.01, t, r };
    };
    // D: days of raw video (or nights of CapturedFiles without raw video); largest that passes
    // at least a day: less, and last night's raw video can be gone before anyone looks
    const MIN = 1;
    let lo = MIN, hi = 60, best = null;
    const g0 = good(MIN);
    if (!g0.ok) return { ok: false, reason: 'no room: not even one day of raw video and one captured night fit next to the operator data and the reserve', result: g0.r };
    best = g0;
    for (let i = 0; i < 16; i++) {
      const mid = (lo + hi) / 2;
      const g = good(mid);
      if (g.ok) { best = g; lo = mid; } else hi = mid;
    }
    // keep a round number of tenths
    const D = Math.floor(best.t.D * 10) / 10;
    const final = good(Math.max(MIN, D));
    return { ok: true, settings: (final.ok ? final : best).t.q, D: (final.ok ? final : best).t.D, N: (final.ok ? final : best).t.N,
             result: (final.ok ? final : best).r, video: !!vday };
  }

  // The raw video / CapturedFiles trade-off: how many captured nights the tune can keep
  // (1 .. the most that still leaves a day of raw video), the split with as many nights
  // as video days, and what one captured night costs in video.
  function tradeoff(p, o) {
    const vday = videoPerDay(p);
    const at = (n) => autotune(p, { ...o, capt_nights: n });
    const one = at(1);
    if (!one.ok) return { ok: false, reason: one.reason };
    const match = autotune(p, { ...o, capt_nights: null });
    if (!vday) return { ok: true, video: false, min: 1, max: match.N, match: match.N, night_cost_days: 0 };
    const cost = p.captured_gb / vday;            // days of video per captured night
    let max = Math.min(365, 1 + Math.floor((one.D - 1) / cost));
    while (max > 1 && !at(max).ok) max--;
    return { ok: true, video: true, min: 1, max, match: Math.min(max, match.N), night_cost_days: cost };
  }

  const api = { simulate, tradeoff, reserve, videoPerDay, stillsBacklog, stillsSafeQuota, quotaOn, capturedAllowance, autotune, tuneFor, GB, CATS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.StorageSim = api;
})(this);
