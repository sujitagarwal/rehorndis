# ReHoRNDIS

Userspace RNDIS USB-tethering driver for macOS (Apple Silicon). Pure Python + libusb + a `utun`
interface. No kext, no DriverKit, no SIP changes.

Android USB tethering (RNDIS) gives a Mac no network interface out of the box. The classic fix,
[HoRNDIS](https://github.com/jwise/HoRNDIS) by Joshua Wise, is a kernel extension that no longer
loads on modern macOS and is unmaintained. ReHoRNDIS is an independent userspace reimplementation
(written from the public MS-RNDIS spec, using HoRNDIS as a reference for message layouts). It is
not affiliated with or endorsed by the HoRNDIS project.

## Install (one line)

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/sujitagarwal/rehorndis/main/install.sh)"
```

Installs libusb (via Homebrew), a private Python venv in `~/.rehorndis`, the `rehorndis` command,
and a menu bar app that launches at login.

## Use

1. Plug in the phone and enable **USB tethering**.
2. Click the **USB** item in the menu bar and choose **Start**. macOS asks for admin approval
   (Touch ID or password) once per start. Stop restores routes and DNS.

| Icon | Meaning |
|---|---|
| ⚪ USB | idle (no phone, or phone ready) |
| 🟡 USB | connecting / stopping |
| 🟢 USB | connected |
| 🔴 USB | error (the menu says why) |

Menu options: **Troubleshoot…** (stops leftovers, removes stale network entries, flushes DNS, resets
the phone's USB connection, then shows a report), **Launch at login**, **Connect when phone is
plugged in**.

Terminal: `rehorndis` (open the menu bar app), `rehorndis run [-v]` (headless, asks for sudo),
`rehorndis update`, `rehorndis uninstall`.

## Install (manual)

```sh
brew install libusb
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run from a checkout

- Menu bar app: `.venv/bin/python app.py`
- Headless: `sudo .venv/bin/python main.py` (`-v` for debug logs). Ctrl+C restores routes and DNS.

Flags: `--verbose`, `--no-default-route` (bring up the link only), `--dump-packets`, `--repair`,
`--vid/--pid` (hex; default: auto-detect any RNDIS phone), `--stage N` (stop early, see below).

## Bring-up stages

| `--stage` | Verifies |
|---|---|
| 1 | device found, descriptors printed, interfaces claimed |
| 2 | RNDIS INITIALIZE + permanent MAC query |
| 3 | packet filter set, raw frames logged for 20 s |
| 4 | DHCP lease from the phone |
| (none) | utun bridge, ARP, routes, DNS |

Then test: `ping 8.8.8.8` and `curl https://example.com`.

## Layout

- `usbio.py` descriptors, interface claim, control/interrupt/bulk transfers
- `rndis.py` MS-RNDIS init/query/set, keepalive, packet-msg wrap/unwrap
- `dhcp.py` DHCP client over raw frames
- `arp.py` ARP responder + resolver
- `tun.py` utun device, ifconfig/route/scutil config and cleanup
- `main.py` tunnel engine: CLI, the L3<->L2 bridge, worker mode, `--repair`
- `control.py` unix-socket channel between the root worker and the app
- `app.py` menu bar app (runs as you; only the worker is elevated)

## Notes

- DNS is set via a transient `scutil` entry (`State:/Network/Service/rehorndis/DNS`), not
  persisted by `networksetup`; it is removed on exit.
- Default route is installed as `0.0.0.0/1` + `128.0.0.0/1` on the utun, so your existing default
  route is never modified.
- IPv4 only. If the phone's `bulk` claim fails, the exact libusb error is printed, and a device
  reset + retry is attempted.
