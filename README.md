# ReHoRNDIS

USB tethering for Android phones on macOS (Apple Silicon). A userspace RNDIS driver in Python:
libusb plus a `utun` interface. No kernel extension, no DriverKit, no SIP changes.

Android's USB tethering uses RNDIS, and macOS has no driver for it, so the phone connects but no
network interface appears. The classic fix, [HoRNDIS](https://github.com/jwise/HoRNDIS) by Joshua
Wise, is a kernel extension that no longer loads on modern macOS and is unmaintained. ReHoRNDIS is
an independent userspace reimplementation, written from the public MS-RNDIS spec with HoRNDIS as a
reference for message layouts. It is not affiliated with or endorsed by the HoRNDIS project.

## Install

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/sujitagarwal/rehorndis/main/install.sh)"
```

Requires macOS on Apple Silicon, [Homebrew](https://brew.sh) and Python 3.9+. The installer adds
libusb (via Homebrew), a private Python environment in `~/.rehorndis`, the `rehorndis` command in
`/usr/local/bin` (asks for sudo once), and a menu bar app that starts at login.

Update with `rehorndis update`. Remove with `rehorndis uninstall` (stop the tunnel first; libusb is
left installed, remove it with `brew uninstall libusb`).

## Use

1. Plug in the phone and turn on **USB tethering** in its settings.
2. Open the **USB** item in the menu bar and flip the **ReHoRNDIS** switch on. macOS asks for admin
   approval (Touch ID or password) each time you turn it on.
3. Flip it off to disconnect. Routes and DNS are restored.

The menu lists every connected RNDIS phone by name. Click one to choose which phone the switch
controls (one phone can be connected at a time). The icon shows the state:

| Icon | Meaning |
|---|---|
| ⚪ USB | off (no phone, or phone ready) |
| 🟡 USB | connecting, turning off, or waiting for approval |
| 🟢 USB | connected |
| 🔴 USB | error (the menu says why) |

Other menu items:

- **Troubleshoot…** stops leftover workers, removes stale network entries, flushes DNS, resets the
  phone's USB connection, and shows a report. Use it if something was left in a bad state.
- **Launch at login** keeps the menu bar item across restarts. A crash restarts it; choosing Quit
  keeps it quit until the next login.
- **Connect when phone is plugged in** turns the tunnel on automatically (you still approve).

Terminal commands: `rehorndis` (open the menu bar app), `rehorndis run [-v]` (headless tunnel in the
terminal, asks for sudo, Ctrl+C to stop), `rehorndis app` (menu bar app in the foreground, shows
errors), `rehorndis update`, `rehorndis uninstall`.

## How it works

1. libusb claims the phone's RNDIS control and data interfaces (no root needed for this).
2. It runs the RNDIS handshake (initialize, MAC query, packet filter) and gets an IPv4 lease from
   the phone with a small DHCP client.
3. A `utun` interface carries IP packets; ARP and Ethernet framing are added and removed in
   between.
4. Routes and DNS are set with `ifconfig`, `route` and `scutil`, and removed again on exit.

Only the tunnel worker runs as root, started on demand through the macOS admin prompt. The menu bar
app runs as you and talks to the worker over a unix socket.

What it changes on your system while connected:

- A new `utunN` interface with the phone's IP.
- Two routes, `0.0.0.0/1` and `128.0.0.0/1`, so traffic prefers the phone. Your existing default
  route is never modified, and Wi-Fi is left as is.
- Network service entries in the dynamic store (`State:/Network/Service/rehorndis/DNS`, `.../IPv4`
  and `Setup:/Network/Service/rehorndis`). They make macOS, Safari and Firefox treat the Mac as
  online, and they are not saved to disk. They are removed on exit.

## Limitations

- IPv4 only. IPv6 traffic is not carried.
- One phone at a time.
- Throughput is limited by a Python implementation (one USB transfer per outgoing packet) and by
  USB 2.0, so expect less than the phone's own speed test.
- Tested on a Samsung phone (04e8:6863) on macOS 26 with Apple Silicon. Other Android phones that
  use standard RNDIS (interface class E0/01/03) should work but are untested.
- Needs admin approval to start, because creating a network interface and changing routes needs
  root.

## Tested devices

| Phone | macOS | Result |
|---|---|---|
| Samsung (04e8:6863, `SAMSUNG_Android`) | 26.6, Apple Silicon | Works |

Using another phone? Please [open a device report](../../issues/new?template=device-report.yml)
whether it works or not, so this table can grow.

## Alternatives

Other ways to get Android USB tethering on a modern Mac. Check each project's own page for current
status and requirements.

- [TetherKitNext](https://github.com/AA-EION/TetherKitNext): a signed, notarized app (DMG
  install, menu bar, command-line tool), no kernel extension. A good choice if you want a
  ready-made app. Based on [TetherKit](https://github.com/XiaoMiku01/TetherKit) by XiaoMiku01.
- [prostec-labs/TetherKit](https://github.com/prostec-labs/TetherKit): a DriverKit port of
  HoRNDIS. Check its README for release status.
- [iKeramat/HoRNDIS](https://github.com/iKeramat/HoRNDIS): a fork of the original kernel extension.
  Needs relaxed security settings (Recovery Mode steps) to load.

What ReHoRNDIS offers instead: a small, readable, MIT-licensed Python implementation you can audit
and modify, installed from a script rather than a signed app.

## Troubleshooting

- **"No phone"**: check the cable (it must carry data), that USB tethering is on, and the phone is
  unlocked. Then try **Troubleshoot…**.
- **Error or "Worker did not start"**: see `/var/log/rehorndis.log`. The menu bar app's own log is
  `~/.rehorndis/app.log`.
- **Internet stays broken after a crash**: choose **Troubleshoot…**, or reboot (routes die with the
  interface and the entries above are not persisted).
- **Run in the foreground to see everything**: `rehorndis run -v --dump-packets`.

## Run from a checkout

```sh
brew install libusb
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                  # menu bar app
sudo .venv/bin/python main.py -v         # headless tunnel (Ctrl+C restores routes and DNS)
./install.sh                             # install this checkout
```

`main.py` flags: `--verbose`, `--dump-packets`, `--no-default-route` (bring up the link without
routing everything through it), `--vid/--pid` (hex) or `--bus/--address` (choose a phone; default is
the first RNDIS device), `--repair` (clean up leftovers), `--worker --control-uid N` (mode used by
the app), `--stage N` (stop early for debugging):

| `--stage` | Verifies |
|---|---|
| 1 | device found, descriptors printed, interfaces claimed |
| 2 | RNDIS initialize and permanent MAC query |
| 3 | packet filter set, raw frames logged for 20 s |
| 4 | DHCP lease from the phone |
| (none) | utun bridge, ARP, routes, DNS |

Then test with `ping 8.8.8.8` and `curl https://example.com`.

## Layout

- `usbio.py` device discovery, descriptors, interface claim, control/interrupt/bulk transfers
- `rndis.py` MS-RNDIS init/query/set, keepalive, packet message wrap/unwrap
- `dhcp.py` DHCP client over raw frames
- `arp.py` ARP responder and resolver
- `tun.py` utun device, ifconfig/route/scutil setup and cleanup
- `main.py` tunnel engine: CLI, the IP-to-Ethernet bridge, worker mode, `--repair`
- `control.py` unix-socket channel between the worker and the app
- `app.py` menu bar app
- `install.sh`, `uninstall.sh` installer and remover

## License

MIT. See `LICENSE`.
