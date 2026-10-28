# blackbox

A flight recorder for a Linux machine that turns off without a trace.

When a machine loses power or trips a hardware limit, the system journal usually loses its last 5 to 30 seconds, because they were still waiting in memory to be written to disk. You see the log stop mid-line and nothing else. `blackbox` writes one line every 2 seconds and forces each line to disk before taking the next, so the record runs up to the last 2 seconds before the cut.

Each line holds:

- load average and CPU busy %
- every temperature the kernel exposes (hwmon sensors and thermal zones), and fan speeds
- CPU clock (average and highest)
- power draw per RAPL zone: CPU package, cores, memory and, on most Intel laptops, `psys`, the whole platform
- kernel thermal throttle events since the last sample
- why the CPU is held back right now, from the Intel limit-reason register: `thermal`, `prochot`, `vr_therm` (the voltage regulator is too hot), `vr_tdc` (it hit its current limit), `pl1`/`pl2` (power limits), and the CPU's critical-temperature signal
- AC and battery state, and whether a discrete GPU is awake
- every 10 seconds, the 3 processes using the most CPU, with their systemd unit (never their command line, which can hold secrets)

It reads only. It never changes a setting or writes a register.

## Install

Python 3.9+ and systemd. Standard library only.

```sh
git clone https://github.com/taktekhq/blackbox
cd blackbox
sudo ./install.sh
```

This puts `blackbox` in `/usr/local/bin` (set `PREFIX` to change it), installs and starts `blackbox.service` as root, and loads the `msr` module at boot. Root is needed for the power readings and the limit-reason register; everything else works without it.

The record lives in `/var/log/blackbox`, one file per day. Finished days are gzipped (about 4 MB each at the default rate) and days older than 14 are deleted.

## Use

```sh
blackbox report                  # what happened before each unclean end, plus heat and power over the last 24 h
blackbox report --seconds 120    # show more samples before each end
blackbox report --hours 72
sudo blackbox now                # one sample, every sensor sorted hottest first
```

`report` sorts each stretch of recording into one of four:

- **stopped**: the service was stopped, including a normal shutdown.
- **machine died**: the next recording is in a new boot and nothing was stopped. Power loss, a hardware thermal trip, or a freeze that someone ended with the power button.
- **restarted**: same boot, no stop. The recorder was killed (the OOM killer, for one) or crashed.
- **running**: the current stretch. `report` says if it has stopped writing.

For each death it prints the last 60 seconds of samples, one line each. A made-up example:

```
21:02:36  load  15.2  busy  88%  cpu  99C  pch_cannonlake/temp1 96C  fan 5400  package-0 52W psys 131W  2600MHz  thr +410  [thermal,vr_therm]  top: chrome 310%, node 120%, python3 95%
```

Reading it:

- Temperatures climbing to the limit (CPU at its TjMax, `pkg:critical`, a board sensor near its trip point) right before the end point to heat.
- `psys` near the power adapter's rating with no battery to cover peaks points to the adapter cutting out under load.
- Everything normal up to the last sample points away from the machine: mains power, the cable, or a hand on the button.

Options for `record`: `--interval` (seconds, default 2), `--keep-days` (14), `--top-every` (scan processes every N samples, 5), `--dir` (or `BLACKBOX_DIR`).

## Limits

- The limit-reason register is Intel 6th generation (Skylake) and later. On other CPUs those fields are missing and the rest still works.
- A cut can still lose the sample being written at that moment, so the record ends at most one interval before the machine did.
- The record is local. If the disk dies with the machine, so does the record.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT, Taktek and Nizar Mahmoud.
