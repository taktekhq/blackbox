#!/usr/bin/env python3
"""blackbox: a flight recorder for a Linux machine that turns off without a trace.

Every 2 seconds it appends one JSON line with load, temperatures, fans, clock speed,
power draw, throttle reasons and the busiest processes, and fsyncs it. The system
journal loses its last seconds on a power cut; this file keeps them.
`blackbox report` shows the samples right before each unclean end.
"""
import argparse
import datetime
import glob
import gzip
import json
import os
import signal
import struct
import sys
import time

VERSION = "0.1.0"
DEFAULT_DIR = "/var/log/blackbox"

# MSR_CORE_PERF_LIMIT_REASONS (0x64F), Intel 6th gen and later. Each sticky log bit is its status bit + 16.
LIMIT_REASONS = {0: "prochot", 1: "thermal", 4: "residency", 5: "ratl", 6: "vr_therm", 7: "vr_tdc",
                 8: "other", 10: "pl1", 11: "pl2", 12: "max_turbo", 13: "turbo_atten"}
# IA32_PACKAGE_THERM_STATUS (0x1B1) status bits.
PKG_THERM = {0: "thermal", 2: "prochot", 4: "critical", 10: "power_limit"}
# Sensors that measure the CPU itself, left out of "hottest other sensor".
CPU_SENSORS = ("coretemp/", "k10temp/", "zenpower/", "x86_pkg_temp", "B0D4", "TCPU")


class Sys:
    """Reads /proc, /sys and /dev under a root directory, so tests can use a fake tree."""

    def __init__(self, root="/"):
        self.root = root

    def path(self, p):
        return os.path.join(self.root, p.lstrip("/"))

    def read(self, p):
        try:
            with open(self.path(p)) as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def num(self, p):
        try:
            return int(self.read(p))
        except (TypeError, ValueError):
            return None

    def glob(self, pattern):
        return ["/" + os.path.relpath(g, self.root) for g in sorted(glob.glob(self.path(pattern)))]

    def rdmsr(self, cpu, reg):
        try:
            fd = os.open(self.path(f"/dev/cpu/{cpu}/msr"), os.O_RDONLY)
        except OSError:
            return None
        try:
            data = os.pread(fd, 8, reg)
        except OSError:
            return None
        finally:
            os.close(fd)
        return struct.unpack("<Q", data)[0] if len(data) == 8 else None


def stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def bits(value, names, shift=0):
    return [n for b, n in names.items() if value >> (b + shift) & 1]


def unique(d, key, where):
    return key if key not in d else f"{key}@{where}"


def hwmon(s):
    """Temperatures (C) and fan speeds (rpm) from every hwmon device that is awake."""
    temps, fans = {}, {}
    for h in s.glob("/sys/class/hwmon/hwmon*"):
        if s.read(h + "/device/power/runtime_status") == "suspended":
            continue  # reading a sleeping GPU's sensor can wake it up
        name, where = s.read(h + "/name") or os.path.basename(h), os.path.basename(h)
        for f in s.glob(h + "/temp*_input"):
            v = s.num(f)
            if v is None:
                continue
            base = f[: -len("_input")]
            key = f"{name}/{s.read(base + '_label') or os.path.basename(base)}"
            temps[unique(temps, key, where)] = round(v / 1000, 1)
        for f in s.glob(h + "/fan*_input"):
            v = s.num(f)
            if v is not None:
                fans[unique(fans, f"{name}/{os.path.basename(f)[: -len('_input')]}", where)] = v
    return temps, fans


def zones(s):
    """ACPI and platform thermal zones (C), keyed by type."""
    out = {}
    for z in s.glob("/sys/class/thermal/thermal_zone*"):
        v, kind = s.num(z + "/temp"), s.read(z + "/type")
        if v is not None and kind:
            out[unique(out, kind, os.path.basename(z))] = round(v / 1000, 1)
    return out


def cpu_times(s):
    """(busy, total) jiffies from the first line of /proc/stat."""
    text = s.read("/proc/stat")
    if not text:
        return None
    f = [int(x) for x in text.splitlines()[0].split()[1:9]]
    return sum(f) - f[3] - f[4], sum(f)


def rapl(s):
    """Energy counter (uJ) of every RAPL zone, with the value it wraps at."""
    out = {}
    for z in s.glob("/sys/class/powercap/intel-rapl:*"):
        name, e = s.read(z + "/name"), s.num(z + "/energy_uj")
        if name and e is not None:
            out[unique(out, name, os.path.basename(z))] = (e, s.num(z + "/max_energy_range_uj") or 0)
    return out


def watts(before, after, dt):
    """Average power per RAPL zone between two rapl() readings taken dt seconds apart."""
    out = {}
    if dt <= 0:
        return out
    for name, (e1, wrap) in before.items():
        if name in after:
            e2 = after[name][0]
            out[name] = round((e2 - e1 if e2 >= e1 else e2 + wrap - e1) / 1e6 / dt, 1)
    return out


def power_limits(s):
    """The package's long and short term power limits (W), as the firmware set them."""
    out = {}
    for c in ("0", "1"):
        name = s.read(f"/sys/class/powercap/intel-rapl:0/constraint_{c}_name")
        uw = s.num(f"/sys/class/powercap/intel-rapl:0/constraint_{c}_power_limit_uw")
        if name and uw is not None:
            out[name] = uw / 1e6
    return out


def throttle_counts(s):
    """(core, package) thermal throttle events counted by the kernel since boot."""
    core = sum(s.num(p) or 0 for p in s.glob("/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/core_throttle_count"))
    return core, s.num("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count") or 0


def clocks(s):
    """(average, highest) current CPU clock in MHz."""
    f = [s.num(p) for p in s.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    f = [x for x in f if x]
    return [round(sum(f) / len(f) / 1000), round(max(f) / 1000)] if f else None


def load(s):
    p = (s.read("/proc/loadavg") or "").split()
    return [float(x) for x in p[:3]] if len(p) >= 3 else None


def memory(s):
    m = {}
    for line in (s.read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if v.split():
            m[k] = int(v.split()[0])
    if "MemAvailable" not in m:
        return None
    return {"avail_mb": m["MemAvailable"] // 1024, "swap_mb": (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) // 1024}


def supplies(s):
    """Online flag of each mains or USB supply; capacity and status of each battery."""
    out = {}
    for p in s.glob("/sys/class/power_supply/*"):
        name = os.path.basename(p)
        if s.read(p + "/type") == "Battery":
            out[name] = f"{s.read(p + '/capacity')}% {s.read(p + '/status')}"
        else:
            out[name] = s.num(p + "/online")
    return out


def gpus(s):
    """Runtime power state (active, suspended) of each GPU except the one driving the boot display."""
    out = {}
    for d in s.glob("/sys/bus/pci/devices/*"):
        if (s.read(d + "/class") or "").startswith("0x03") and s.read(d + "/boot_vga") != "1":
            out[os.path.basename(d)] = s.read(d + "/power/runtime_status")
    return out


def limits(s):
    """Why the CPU is held back right now, and what is logged since boot. Intel only, needs root."""
    r, p = s.rdmsr(0, 0x64F), s.rdmsr(0, 0x1B1)
    out = {}
    if r is not None:
        out["now"] = bits(r, LIMIT_REASONS)
        out["logged"] = bits(r, LIMIT_REASONS, 16)
    if p is not None:
        out["pkg"] = bits(p, PKG_THERM)
    return out or None


def proc_ticks(s):
    """CPU ticks used so far by every process: {pid: (comm, ticks)}."""
    out = {}
    for p in s.glob("/proc/[0-9]*/stat"):
        raw = s.read(p)
        if not raw:
            continue
        try:
            end = raw.rindex(")")
            f = raw[end + 2:].split()
            out[int(p.split("/")[2])] = (raw[raw.index("(") + 1: end], int(f[11]) + int(f[12]))
        except (ValueError, IndexError):
            continue
    return out


def top(before, after, dt, hz, n=3):
    """The n processes that used the most CPU between two proc_ticks() readings: [comm, pid, % of one CPU]."""
    rows = []
    for pid, (comm, ticks) in after.items():
        used = ticks - before[pid][1] if pid in before else ticks
        if used > 0:
            rows.append((used, pid, comm))
    rows.sort(reverse=True)
    return [[comm, pid, round(used / hz / dt * 100)] for used, pid, comm in rows[:n]] if dt > 0 else []


def unit(s, pid):
    """The systemd unit or scope a process runs in (never its command line, which can hold secrets)."""
    lines = (s.read(f"/proc/{pid}/cgroup") or "").splitlines()
    if not lines:
        return None
    return lines[0].rpartition(":")[2].rstrip("/").rpartition("/")[2] or None


class Recorder:
    def __init__(self, s, top_every=5):
        self.s, self.top_every, self.n = s, top_every, 0
        self.prev = None   # (monotonic, cpu_times, rapl, throttle_counts) of the last sample
        self.procs = None  # (monotonic, proc_ticks) of the last process scan
        self.hz = os.sysconf("SC_CLK_TCK")

    def sample(self):
        s, now = self.s, time.monotonic()
        cur = (now, cpu_times(s), rapl(s), throttle_counts(s))
        temps, fans = hwmon(s)
        out = {"t": stamp(), "load": load(s), "temp": temps, "zone": zones(s), "fan": fans, "mhz": clocks(s),
               "mem": memory(s), "supply": supplies(s), "gpu": gpus(s), "limits": limits(s)}
        if self.prev:
            dt = now - self.prev[0]
            if cur[1] and self.prev[1] and cur[1][1] > self.prev[1][1]:
                out["busy"] = round(100 * (cur[1][0] - self.prev[1][0]) / (cur[1][1] - self.prev[1][1]), 1)
            out["watts"] = watts(self.prev[2], cur[2], dt)
            out["throttle"] = [cur[3][0] - self.prev[3][0], cur[3][1] - self.prev[3][1]]
        self.prev = cur
        if self.n % self.top_every == 0:
            ticks = proc_ticks(s)
            if self.procs:
                out["top"] = [row + [unit(s, row[1])] for row in top(self.procs[1], ticks, now - self.procs[0], self.hz)]
            self.procs = (now, ticks)
        self.n += 1
        return out


def ends_clean(path):
    """True when the file is empty or ends with a newline. A power cut can leave half a line."""
    with open(path, "rb") as f:
        if f.seek(0, os.SEEK_END) == 0:
            return True
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prune(directory, keep_days, today):
    """Gzip every finished day and delete days older than keep_days."""
    for name in os.listdir(directory):
        try:
            day = datetime.date.fromisoformat(name.split(".")[0])
        except ValueError:
            continue
        path = os.path.join(directory, name)
        if (today - day).days >= keep_days:
            os.remove(path)
        elif day < today and name.endswith(".jsonl"):
            with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                dst.write(src.read())
            os.remove(path)


class Log:
    """Appends JSON lines to DIR/YYYY-MM-DD.jsonl and fsyncs each one before returning."""

    def __init__(self, directory, keep_days=14, today=datetime.date.today):
        self.dir, self.keep_days, self.today = directory, keep_days, today
        self.day, self.f = None, None
        os.makedirs(directory, exist_ok=True)

    def write(self, record):
        if self.today() != self.day:
            self._open(self.today())
        self.f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())

    def _open(self, day):
        if self.f:
            self.f.close()
        self.day, path = day, os.path.join(self.dir, day.isoformat() + ".jsonl")
        new = not os.path.exists(path)
        torn = not new and not ends_clean(path)
        self.f = open(path, "a")
        if torn:
            self.f.write("\n")
        if new:
            fsync_dir(self.dir)
        prune(self.dir, self.keep_days, day)

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


def record(args):
    s, log = Sys(args.root), Log(args.dir, args.keep_days)
    rec, stopping = Recorder(s, args.top_every), []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, _frame: stopping.append(signal.Signals(signum).name))
    log.write({"t": stamp(), "event": "start", "version": VERSION, "interval": args.interval,
               "boot_id": s.read("/proc/sys/kernel/random/boot_id"), "power_limits_w": power_limits(s)})
    nxt = time.monotonic()
    while not stopping:
        try:
            log.write(rec.sample())
        except Exception as e:  # one bad read must not stop the recorder; the error is logged, not hidden
            log.write({"t": stamp(), "event": "error", "error": repr(e)[:300]})
        nxt = max(nxt + args.interval, time.monotonic())
        while not stopping and time.monotonic() < nxt:
            time.sleep(max(0.0, min(0.2, nxt - time.monotonic())))
    log.write({"t": stamp(), "event": "stop", "signal": stopping[0]})
    log.close()


# Reading the record back.

def records(directory):
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".jsonl", ".jsonl.gz")):
            continue
        opener = gzip.open if name.endswith(".gz") else open
        with opener(os.path.join(directory, name), "rt", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue  # half a line or zeros left by a power cut
                if isinstance(r, dict) and "t" in r:
                    yield r


def runs(recs):
    """Split the record into recorder runs, each beginning at a start event."""
    out = []
    for r in recs:
        if r.get("event") == "start":
            out.append({"start": r, "samples": [], "stop": None, "errors": 0})
        elif out and r.get("event") == "stop":
            out[-1]["stop"] = r
        elif out and r.get("event") == "error":
            out[-1]["errors"] += 1
        elif out and "event" not in r:
            out[-1]["samples"].append(r)
    return out


def endings(rs):
    """How each run ended: stopped, died (a new boot with no stop), restarted (same boot, no stop) or running."""
    out = []
    for i, run in enumerate(rs):
        nxt = rs[i + 1] if i + 1 < len(rs) else None
        if run["stop"]:
            kind = "stopped"
        elif nxt is None:
            kind = "running"
        elif nxt["start"].get("boot_id") != run["start"].get("boot_id"):
            kind = "died"
        else:
            kind = "restarted"
        out.append((kind, run, nxt))
    return out


def when(r):
    return datetime.datetime.fromisoformat(r["t"])


def cpu_temp(r):
    temps = r.get("temp") or {}
    for k, v in temps.items():
        if k.startswith("coretemp/Package"):
            return v
    return (r.get("zone") or {}).get("x86_pkg_temp") or max(temps.values(), default=None)


def hottest_other(r):
    vals = {**(r.get("zone") or {}), **(r.get("temp") or {})}
    vals = {k: v for k, v in vals.items() if 0 < v < 150 and not k.startswith(CPU_SENSORS)}
    return max(vals.items(), key=lambda kv: kv[1], default=None)


def platform_watts(r):
    w = r.get("watts") or {}
    return w.get("psys", w.get("package-0"))


def row(r):
    """One sample as one line a person can read."""
    parts = [r["t"][11:19]]
    if r.get("load"):
        parts.append(f"load {r['load'][0]:5.1f}")
    if r.get("busy") is not None:
        parts.append(f"busy {r['busy']:3.0f}%")
    if cpu_temp(r) is not None:
        parts.append(f"cpu {cpu_temp(r):3.0f}C")
    other = hottest_other(r)
    if other:
        parts.append(f"{other[0]} {other[1]:.0f}C")
    if r.get("fan"):
        parts.append(f"fan {max(r['fan'].values())}")
    w = r.get("watts") or {}
    if w:
        parts.append(" ".join(f"{k} {v:.0f}W" for k, v in w.items() if k in ("psys", "package-0")))
    if r.get("mhz"):
        parts.append(f"{r['mhz'][0]}MHz")
    if r.get("throttle"):
        parts.append(f"thr +{r['throttle'][0]}")
    lim = r.get("limits") or {}
    flags = lim.get("now", []) + [f"pkg:{x}" for x in lim.get("pkg", []) if x != "thermal"]
    if flags:
        parts.append("[" + ",".join(flags) + "]")
    awake = [k for k, v in (r.get("gpu") or {}).items() if v == "active"]
    if awake:
        parts.append("gpu on: " + ",".join(awake))
    if r.get("top"):
        parts.append("top: " + ", ".join(f"{t[0]} {t[2]}%" for t in r["top"]))
    return "  ".join(parts)


def summary(samples):
    """Heat and power over a set of samples."""
    temps = [(cpu_temp(r), r) for r in samples if cpu_temp(r) is not None]
    power = [(platform_watts(r), r) for r in samples if platform_watts(r) is not None]
    lines = [f"{len(samples)} samples, {samples[0]['t']} to {samples[-1]['t']}"]
    if temps:
        hot, at = max(temps, key=lambda x: x[0])
        share = sum(1 for t, _ in temps if t >= 95) / len(temps)
        lines.append(f"CPU: max {hot:.0f}C at {at['t'][11:19]}, at or above 95C {share:.0%} of the time")
    if power:
        top_w, at = max(power, key=lambda x: x[0])
        p99 = sorted(p for p, _ in power)[int(0.99 * (len(power) - 1))]
        lines.append(f"Power: max {top_w:.0f}W at {at['t'][11:19]}, 99th percentile {p99:.0f}W")
    for flag in ("prochot", "vr_therm", "vr_tdc"):
        n = sum(1 for r in samples if flag in ((r.get("limits") or {}).get("now") or []))
        if n:
            lines.append(f"{flag} active in {n} samples")
    crit = sum(1 for r in samples if "critical" in ((r.get("limits") or {}).get("pkg") or []))
    if crit:
        lines.append(f"CPU critical temperature signal in {crit} samples")
    return lines


def report(args):
    if not os.path.isdir(args.dir):
        sys.exit(f"blackbox: no record at {args.dir}")
    rs = runs(records(args.dir))
    if not rs:
        sys.exit(f"blackbox: {args.dir} holds no samples yet")
    for kind, run, nxt in endings(rs):
        if kind not in ("died", "restarted") or not run["samples"]:
            continue
        last = run["samples"][-1]
        gap = (when(nxt["start"]) - when(last)).total_seconds() / 60
        what = "MACHINE DIED" if kind == "died" else "RECORDER RESTARTED (same boot: killed or crashed)"
        print(f"\n{what}: last sample {last['t']}, recording again {gap:.0f} min later")
        recent = [r for r in run["samples"] if (when(last) - when(r)).total_seconds() <= args.seconds]
        for r in recent:
            print("  " + row(r))
        window = [r for r in run["samples"] if (when(last) - when(r)).total_seconds() <= 300]
        print("  last 5 minutes: " + "; ".join(summary(window)[1:]))
    samples = [r for run in rs for r in run["samples"]]
    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(hours=args.hours)
    recent = [r for r in samples if when(r) >= cutoff]
    print(f"\nLast {args.hours} h:")
    for line in summary(recent) if recent else ["no samples"]:
        print("  " + line)
    errors = sum(run["errors"] for run in rs)
    if errors:
        print(f"  {errors} samples failed; their error events are in the record")
    kind, run, _ = endings(rs)[-1]
    if kind == "running" and run["samples"]:
        age = (datetime.datetime.now().astimezone() - when(run["samples"][-1])).total_seconds()
        interval = run["start"].get("interval") or 2
        state = "recording" if age < 3 * interval + 5 else f"NOT recording: last sample {age:.0f} s ago"
        print(f"\nRecorder: {state}")
        print("  " + row(run["samples"][-1]))
    elif kind == "stopped":
        print(f"\nRecorder: stopped at {run['stop']['t']}")


def now(args):
    rec = Recorder(Sys(args.root), top_every=1)
    rec.sample()
    time.sleep(1)
    r = rec.sample()
    print(row(r))
    for k, v in sorted({**r["zone"], **r["temp"]}.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5.1f}C  {k}")
    print("  watts: " + json.dumps(r.get("watts")) + "  limits: " + json.dumps(r.get("limits")))


def main(argv=None):
    p = argparse.ArgumentParser(prog="blackbox", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="sample forever and fsync each line (run it as a root service)")
    rec.add_argument("--interval", type=float, default=2.0, help="seconds between samples (2)")
    rec.add_argument("--keep-days", type=int, default=14, help="days of record to keep (14)")
    rec.add_argument("--top-every", type=int, default=5, help="scan processes every N samples (5)")
    rep = sub.add_parser("report", help="show the samples before each unclean end, and heat over the last hours")
    rep.add_argument("--seconds", type=float, default=60, help="seconds of samples to show before each end (60)")
    rep.add_argument("--hours", type=float, default=24, help="hours to summarize (24)")
    nw = sub.add_parser("now", help="print one sample")
    for sp in (rec, rep):
        sp.add_argument("--dir", default=os.environ.get("BLACKBOX_DIR", DEFAULT_DIR), help=f"record directory ({DEFAULT_DIR})")
    for sp in (rec, nw):
        sp.add_argument("--root", default="/", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    {"record": record, "report": report, "now": now}[args.cmd](args)


if __name__ == "__main__":
    main()
