"""Tests for blackbox.py. Stdlib only: every reading comes from a fake /sys, /proc and /dev tree in a temp dir."""
import contextlib
import datetime
import gzip
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import blackbox  # noqa: E402


def put(root, path, text):
    p = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def put_msr(root, values):
    """A sparse file standing in for /dev/cpu/0/msr: 8 little-endian bytes at each register's offset."""
    p = os.path.join(root, "dev/cpu/0/msr")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        for reg, value in values.items():
            f.seek(reg)
            f.write(struct.pack("<Q", value))


def sample(t, **fields):
    return {"t": t, **fields}


class Readings(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.s = blackbox.Sys(self.root)

    def test_hwmon_reads_labels_fans_and_skips_sleeping_devices(self):
        put(self.root, "/sys/class/hwmon/hwmon0/name", "coretemp")
        put(self.root, "/sys/class/hwmon/hwmon0/temp1_input", "98000")
        put(self.root, "/sys/class/hwmon/hwmon0/temp1_label", "Package id 0")
        put(self.root, "/sys/class/hwmon/hwmon1/name", "dell_smm")
        put(self.root, "/sys/class/hwmon/hwmon1/temp1_input", "85500")
        put(self.root, "/sys/class/hwmon/hwmon1/fan1_input", "5416")
        put(self.root, "/sys/class/hwmon/hwmon2/name", "nouveau")
        put(self.root, "/sys/class/hwmon/hwmon2/temp1_input", "50000")
        put(self.root, "/sys/class/hwmon/hwmon2/device/power/runtime_status", "suspended")
        temps, fans = blackbox.hwmon(self.s)
        self.assertEqual(temps, {"coretemp/Package id 0": 98.0, "dell_smm/temp1": 85.5})
        self.assertEqual(fans, {"dell_smm/fan1": 5416})

    def test_duplicate_sensor_names_stay_apart(self):
        for h in ("hwmon0", "hwmon1"):
            put(self.root, f"/sys/class/hwmon/{h}/name", "nvme")
            put(self.root, f"/sys/class/hwmon/{h}/temp1_input", "40000")
            put(self.root, f"/sys/class/hwmon/{h}/temp1_label", "Composite")
        temps, _ = blackbox.hwmon(self.s)
        self.assertEqual(sorted(temps), ["nvme/Composite", "nvme/Composite@hwmon1"])

    def test_watts_handles_counter_wrap(self):
        wrap = 262143328850
        before = {"psys": (wrap - 1_000_000, wrap), "core": (5_000_000, wrap)}
        after = {"psys": (1_000_000, wrap), "core": (105_000_000, wrap)}
        self.assertEqual(blackbox.watts(before, after, 2.0), {"psys": 1.0, "core": 50.0})

    def test_limit_reasons_decode_status_and_log_bits(self):
        put_msr(self.root, {0x64F: (1 << 1) | (1 << (6 + 16)), 0x1B1: 1 << 4})
        self.assertEqual(blackbox.limits(self.s), {"now": ["thermal"], "logged": ["vr_therm"], "pkg": ["critical"]})

    def test_no_msr_means_no_limits(self):
        self.assertIsNone(blackbox.limits(self.s))

    def test_proc_ticks_survives_odd_process_names(self):
        rest = " ".join(["S"] + ["0"] * 10 + ["70", "30"] + ["0"] * 30)
        put(self.root, "/proc/42/stat", f"42 (tmux: (server)) {rest}")
        self.assertEqual(blackbox.proc_ticks(self.s), {42: ("tmux: (server)", 100)})

    def test_top_ranks_by_cpu_used_in_the_window(self):
        before = {1: ("a", 100), 2: ("b", 0)}
        after = {1: ("a", 300), 2: ("b", 50), 3: ("new", 10), 4: ("idle", 0)}
        self.assertEqual(blackbox.top(before, after, 1.0, 100), [["a", 1, 200], ["b", 2, 50], ["new", 3, 10]])

    def test_gpus_skip_the_boot_display(self):
        put(self.root, "/sys/bus/pci/devices/0000:00:02.0/class", "0x030000")
        put(self.root, "/sys/bus/pci/devices/0000:00:02.0/boot_vga", "1")
        put(self.root, "/sys/bus/pci/devices/0000:01:00.0/class", "0x030200")
        put(self.root, "/sys/bus/pci/devices/0000:01:00.0/boot_vga", "0")
        put(self.root, "/sys/bus/pci/devices/0000:01:00.0/power/runtime_status", "suspended")
        put(self.root, "/sys/bus/pci/devices/0000:3b:00.0/class", "0x028000")
        self.assertEqual(blackbox.gpus(self.s), {"0000:01:00.0": "suspended"})

    def test_unit_is_the_last_cgroup_part(self):
        put(self.root, "/proc/7/cgroup", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/farm.service\n")
        self.assertEqual(blackbox.unit(self.s, 7), "farm.service")
        self.assertIsNone(blackbox.unit(self.s, 8))

    def test_recorder_computes_deltas_from_the_second_sample(self):
        put(self.root, "/proc/stat", "cpu 100 0 0 900 0 0 0 0 0 0\n")
        put(self.root, "/sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count", "10")
        rec = blackbox.Recorder(self.s)
        first = rec.sample()
        self.assertNotIn("busy", first)
        put(self.root, "/proc/stat", "cpu 160 0 0 940 0 0 0 0 0 0\n")
        put(self.root, "/sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count", "25")
        second = rec.sample()
        self.assertEqual(second["busy"], 60.0)
        self.assertEqual(second["throttle"], [15, 0])


class Log(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.day = datetime.date(2026, 10, 28)

    def test_every_line_is_fsynced(self):
        log = blackbox.Log(self.dir, today=lambda: self.day)
        with mock.patch.object(blackbox.os, "fsync", wraps=os.fsync) as fsync:
            log.write({"t": "a"})
            log.write({"t": "b"})
        log.close()
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_half_line_from_a_power_cut_does_not_eat_the_next_record(self):
        path = os.path.join(self.dir, "2026-10-28.jsonl")
        with open(path, "w") as f:
            f.write('{"t":"2026-10-28T21:02:36+02:00"}\n{"t":"2026-10-28T21:0')
        log = blackbox.Log(self.dir, today=lambda: self.day)
        log.write({"t": "2026-10-28T21:32:00+02:00", "event": "start"})
        log.close()
        got = [r["t"] for r in blackbox.records(self.dir)]
        self.assertEqual(got, ["2026-10-28T21:02:36+02:00", "2026-10-28T21:32:00+02:00"])

    def test_prune_gzips_finished_days_and_drops_old_ones(self):
        for name in ("2026-10-01.jsonl", "2026-10-27.jsonl", "2026-10-28.jsonl", "notes.txt"):
            put(self.dir, name, '{"t":"x"}\n')
        blackbox.prune(self.dir, 14, self.day)
        self.assertEqual(sorted(os.listdir(self.dir)), ["2026-10-27.jsonl.gz", "2026-10-28.jsonl", "notes.txt"])
        with gzip.open(os.path.join(self.dir, "2026-10-27.jsonl.gz"), "rt") as f:
            self.assertEqual(f.read(), '{"t":"x"}\n')


class Report(unittest.TestCase):
    def write(self, recs):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "2026-10-28.jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        return d

    def test_endings_tell_death_from_stop_and_restart(self):
        recs = [
            {"t": "2026-10-28T20:00:00+02:00", "event": "start", "boot_id": "A"},
            sample("2026-10-28T20:00:02+02:00"),
            {"t": "2026-10-28T20:00:04+02:00", "event": "start", "boot_id": "A"},
            sample("2026-10-28T20:00:06+02:00"),
            {"t": "2026-10-28T21:32:00+02:00", "event": "start", "boot_id": "B"},
            sample("2026-10-28T21:32:02+02:00"),
            {"t": "2026-10-28T21:40:00+02:00", "event": "stop", "signal": "SIGTERM"},
            {"t": "2026-10-28T21:45:00+02:00", "event": "start", "boot_id": "C"},
        ]
        kinds = [k for k, _, _ in blackbox.endings(blackbox.runs(recs))]
        self.assertEqual(kinds, ["restarted", "died", "stopped", "running"])

    def test_report_shows_the_last_samples_before_a_death(self):
        hot = {"coretemp/Package id 0": 99.0, "pch_cannonlake/temp1": 96.0}
        recs = [
            {"t": "2026-10-28T21:00:00+02:00", "event": "start", "boot_id": "A", "interval": 2},
            sample("2026-10-28T21:00:10+02:00", load=[3.0, 3, 3], temp={"coretemp/Package id 0": 70.0}),
            sample("2026-10-28T21:02:36+02:00", load=[15.2, 14, 12], temp=hot, watts={"psys": 131.0},
                   limits={"now": ["thermal", "vr_therm"], "pkg": ["thermal", "critical"]}),
            {"t": "2026-10-28T21:32:41+02:00", "event": "start", "boot_id": "B", "interval": 2},
            sample("2026-10-28T21:32:43+02:00"),
        ]
        out = io.StringIO()
        args = mock.Mock(dir=self.write(recs), seconds=60, hours=24)
        with contextlib.redirect_stdout(out):
            blackbox.report(args)
        text = out.getvalue()
        self.assertIn("MACHINE DIED: last sample 2026-10-28T21:02:36+02:00, recording again 30 min later", text)
        self.assertIn("21:02:36  load  15.2  cpu  99C  pch_cannonlake/temp1 96C  psys 131W", text)
        self.assertIn("[thermal,vr_therm,pkg:critical]", text)
        self.assertNotIn("21:00:10", text.split("last 5 minutes")[0])

    def test_summary_counts_heat_and_power(self):
        samples = [sample(f"2026-10-28T21:00:0{i}+02:00", temp={"coretemp/Package id 0": t}, watts={"psys": w})
                   for i, (t, w) in enumerate([(90.0, 80.0), (96.0, 120.0), (97.0, 100.0), (80.0, 60.0)])]
        lines = blackbox.summary(samples)
        self.assertIn("CPU: max 97C at 21:00:02, at or above 95C 50% of the time", lines)
        self.assertIn("Power: max 120W at 21:00:01, 99th percentile 100W", lines)


if __name__ == "__main__":
    unittest.main()
