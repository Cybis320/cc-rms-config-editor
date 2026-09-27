// The storage simulator's engine against RMS's rules, worked by hand.
//   node tests/ui/sim.test.js
const S = require('../../config_editor/static/storage-sim.js');
let failed = 0;
const ok = (cond, what) => { console.log((cond ? 'PASS ' : 'FAIL ') + what); if (!cond) failed++; };
const near = (a, b, tol = 0.051) => Math.abs(a - b) <= tol;

// US005x today: 27 Mbps continuous, 1080p, six stations on a 7,392 GB disk
const base = { stations: 6, disk_gb: 7392, other_gb: 0, raw_video_save: true, continuous_capture: true, save_frames: true,
  mbps: 27, night_hours: 13.26, ff_gb: 36, extra_space_gb: 120, sessions: 1, bz2_files_per_session: 2,
  captured_gb: 36, archived_gb: 0.4, bz2_gb: 0.2, frames_gb: 0.2, times_gb: 0.014, logs_gb: 0.012,
  capt_dirs_to_keep: 14, arch_dirs_to_keep: 30, bz2_files_to_keep: 30, frame_days_to_keep: 14, video_days_to_keep: 14,
  times_days_to_keep: 14, logdays_to_keep: 30, quota_management_enabled: true, rms_data_quota: 764, arch_dir_quota: 10,
  bz2_files_quota: 10, continuous_capture_quota: 480, log_files_quota: 0.2 };
const run = (over, days = 74) => S.simulate({ ...base, ...over }, days);

ok(near(S.videoPerDay(base), 271.57, 0.01), 'a day of 27 Mbps is 271.57 GB, as the patched RMS logs');
ok(near(S.reserve(base), 36 + 120 + 271.57 + 3, 0.01), 'reserve = FF + extra + video + 3 GB frames');
ok(near(S.capturedAllowance(base), 764 - 10 - 10 - 480 - 0.2, 1e-9), 'captured allowance = rms_data_quota minus the other four');

let r = run({});
ok(r.lost_per_day === 0, 'the suggested split never fills the disk');
ok(near(r.kept.video.min, 480 / 271.57, 0.02), 'video keeps continuous_capture_quota / a day of video: 1.77 days');
ok(r.binding.video.includes('continuous_capture_quota') && !r.binding.video.includes('free space'), 'video is trimmed by the quota, not the free-space loop');
ok(r.kept.capt.min === 7, 'captured: 263.8 GB / 36 GB a night = 7 nights');
ok(r.binding.capt[0] === 'captured allowance', '... limited by the captured allowance');
ok(r.kept.bz2.min === 15, 'bz2_files_to_keep 30 files = 15 nights (2 files a night)');
ok(r.binding.bz2[0] === 'bz2_files_to_keep', '... limited by bz2_files_to_keep');
ok(r.binding.logs[0] === 'log_files_quota' && near(r.kept.logs.min, 0.2 / 0.012, 0.5), 'log_files_quota 0.2 GB keeps ~16 days, not logdays_to_keep 30');

r = run({ rms_data_quota: 965, continuous_capture_quota: 681 });
ok(r.binding.video.includes('free space'), 'today\'s quotas: the free-space loop has to delete video');
ok(r.kept.video.max - r.kept.video.min > 0.9, '... unevenly: the first station to start frees room for all');

r = run({ mbps: 40 });
ok(r.lost_per_day === 0, 'at 40 Mbps the quotas still keep six days of writes on the disk');
r = run({ mbps: 60 });
ok(r.lost_per_day > 0, 'at 60 Mbps six stations each see room, together write more than there is: capture is lost');

r = run({ continuous_capture_quota: 0 });
ok(r.kept.video.min === 0 && r.binding.video.includes('continuous_capture_quota'), 'continuous_capture_quota 0 deletes all video (objectsToDeleteByTime)');

r = run({ rms_data_quota: 400 });
ok(r.captured_allowance < 0 && !r.binding.capt.includes('captured allowance'), 'captured allowance <= 0: CapturedFiles not managed by quota');

r = run({ arch_dir_quota: null });
ok(!r.quota_on && !r.binding.video.includes('continuous_capture_quota'), 'one quota unset: quota management off entirely');

r = run({ sessions: 2, quota_management_enabled: false, rms_data_quota: 965, continuous_capture_quota: 681, stations: 1 });
ok(near(r.kept.capt.min, 7, 0.51), 'two sessions a night: capt_dirs_to_keep 14 is 7 nights');

r = run({ quota_management_enabled: false, video_days_to_keep: 3, stations: 1 });
ok(near(r.kept.video.min, 2, 0.05) && r.binding.video[0] === 'video_days_to_keep', 'video_days_to_keep 3 with continuous capture (today\'s dir counts) keeps 2 full days');
r = run({ quota_management_enabled: false, video_days_to_keep: 3, stations: 1, continuous_capture: false });
ok(near(r.kept.video.min, 3, 0.05), 'night-only capture: video_days_to_keep 3 keeps 3 nights');

// stills waiting for the daily timelapse
const st = { stills_gb_per_hour: 0.8 };
ok(near(S.stillsBacklog({ ...base, ...st }), 0.8 * 25, 1e-9), 'continuous: a day of stills plus the hour of the frames step');
ok(near(S.stillsBacklog({ ...base, ...st, continuous_capture: false }), 0.8 * 14.26, 1e-9), 'night only: the night of stills plus an hour');
ok(S.stillsBacklog({ ...base, ...st, save_frames: false }) === 0, 'save_frames off: no stills');
r = run({ ...st, continuous_capture_quota: 15 });
ok(r.stills_cut, 'continuous_capture_quota below the stills backlog deletes stills before the timelapse');
r = run(st);
ok(!r.stills_cut && !r.binding.stills.length, 'stills are the newest files: the quota and the loop leave them alone');

// auto-tune
const tb = { ...base, ...st, rms_data_quota: 965, continuous_capture_quota: 681, extra_space_gb: 120 };
const opts = { nice_days: 14, log_days: 30, margin: 0.03, capt_nights: null };
let a = S.autotune(tb, opts);
ok(a.ok, 'auto-tune finds a fit for six 27 Mbps stations');
ok(a.result.lost_per_day === 0 && !Object.values(a.result.binding).some((x) => x.includes('free space')), '... nothing lost, and the free-space loop never deletes');
ok(Math.abs(a.result.kept.video.min - a.result.kept.video.max) < 0.01, '... every station keeps the same');
ok(a.N === Math.round(a.D) && a.result.kept.capt.min === a.N, '... as many captured nights as raw video days');
ok(a.settings.extra_space_gb >= S.stillsBacklog(tb) - 3, '... extra_space_gb covers the stills RMS does not count');
ok(a.settings.arch_dirs_to_keep === 14 && a.settings.bz2_files_to_keep === 28 && a.settings.logdays_to_keep === 30, '... operator data by count: 14 nights, 28 bz2 files, 30 days of logs');
let more = S.tuneFor(tb, opts, a.D + 0.3);
let rm = S.simulate({ ...more.q, other_gb: 0.03 * tb.disk_gb }, 74);
ok(rm.lost_per_day > 0 || Object.values(rm.binding).some((x) => x.includes('free space')) || rm.kept.capt.min < more.N, '... and it is the most: 0.3 day more no longer fits');
a = S.autotune(tb, { ...opts, capt_nights: 14 });
ok(a.ok && a.result.kept.capt.min === 14 && a.D < S.autotune(tb, opts).D, 'fixed 14 captured nights: fewer raw video days');
a = S.autotune({ ...tb, stations: 12 }, opts);
ok(!a.ok && /one day/.test(a.reason), 'twelve such stations on the disk: under a day of video each — no fit, said so');

process.exit(failed ? 1 : 0);
