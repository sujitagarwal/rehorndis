#!/usr/bin/env python3
"""ReHoRNDIS tunnel daemon (needs root). The menu bar UI is app.py; this is the engine."""
import argparse
import logging
import os
import queue
import select
import signal
import socket
import struct
import sys
import threading
import time

import usb.core

import arp
import control
import dhcp
import rndis
import tun
import usbio

log = logging.getLogger("main")


def hexdump(b, n=96):
    return b[:n].hex(" ") + (" ..." if len(b) > n else "")


def describe(f):
    """One-line summary of an Ethernet frame."""
    if len(f) < 14:
        return f"runt {len(f)}B"
    dst, src, et = f[0:6].hex(":"), f[6:12].hex(":"), struct.unpack_from("!H", f, 12)[0]
    s = f"{src}>{dst} type={et:#06x} len={len(f)}"
    if et == 0x0806 and (p := arp.parse(f)):
        s += f" ARP op={p[0]} {socket.inet_ntoa(p[2])}->{socket.inet_ntoa(p[4])}"
    elif et == 0x0800 and len(f) >= 34:
        s += f" IPv4 proto={f[23]} {socket.inet_ntoa(f[26:30])}->{socket.inet_ntoa(f[30:34])}"
    return s


class Bridge:
    """utun (L3) <-> RNDIS (L2). RX runs in a thread, TX in the caller's thread."""

    def __init__(self, rn, tn, mac, lease, dh, args):
        self.rn, self.tn, self.mac, self.lease, self.dh, self.args = rn, tn, mac, lease, dh, args
        self.stop = rn.stop
        self.ip = socket.inet_aton(lease.ip)
        self.mask = struct.unpack("!I", socket.inet_aton(lease.mask))[0]
        self.gw = socket.inet_aton(lease.router or socket.inet_ntoa(lease.server_id))
        self.arp = arp.ArpTable(mac, self._send)
        self.arp.ip = self.ip
        self.arp.learn(lease.server_id, lease.server_mac)
        self._renew_after = lease.renew_at
        self.rx_bytes = self.tx_bytes = 0

    def stats(self):
        return {"rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes}

    def _send(self, frame):
        self._send_many([frame])

    def _send_many(self, frames):
        for f in frames:
            self.tx_bytes += len(f)
            if self.args.dump_packets:
                print("TX", describe(f), "|", hexdump(f))
        self.rn.send_frames(frames)

    # ---- phone -> Mac ----
    def rx_loop(self):
        """Two stages so USB reads overlap packet handling: this thread only reads, rx_process unwraps."""
        q = queue.Queue(maxsize=256)
        threading.Thread(target=self.rx_process, args=(q,), daemon=True).start()
        while not self.stop.is_set():
            try:
                data = self.rn.read_raw(200)
            except usb.core.USBError as e:
                log.error("bulk read failed: %r", e)
                self.stop.set()
                return
            if data:
                q.put(data)

    def rx_process(self, q):
        while not self.stop.is_set():
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                continue
            for f in self.rn.unwrap(data):
                self.rx_bytes += len(f)
                if self.args.dump_packets:
                    print("RX", describe(f), "|", hexdump(f))
                self._on_frame(f)

    def _on_frame(self, f):
        if len(f) < 14:
            return
        et = f[12:14]
        if et == b"\x08\x06":
            self.arp.handle(f)
        elif et == b"\x08\x00":
            m = self.dh.handle(f)
            if m:
                if m["type"] == dhcp.ACK:
                    lt = struct.unpack("!I", m["opts"].get(51, b"\0\0\x0e\x10"))[0]
                    self.lease.renewed(lt)
                    self._renew_after = self.lease.renew_at
                    log.info("DHCP lease renewed (%ds)", lt)
                return
            ip = f[14:]
            tot = struct.unpack_from("!H", ip, 2)[0] if len(ip) >= 20 else 0
            if tot and tot <= len(ip):
                self.tn.write(ip[:tot])  # drop Ethernet padding

    # ---- Mac -> phone ----
    def _next_hop_mac(self, dst):
        if dst == b"\xff\xff\xff\xff":
            return arp.BCAST
        if 224 <= dst[0] <= 239:
            return b"\x01\x00\x5e" + bytes([dst[1] & 0x7F]) + dst[2:4]
        d, me = struct.unpack("!I", dst)[0], struct.unpack("!I", self.ip)[0]
        hop = dst if (d & self.mask) == (me & self.mask) else self.gw
        return self.arp.lookup(hop)

    def tx_loop(self):
        while not self.stop.is_set():
            if time.monotonic() > self._renew_after:
                log.info("DHCP renew")
                self.dh.send_renew(self.lease)
                self._renew_after = time.monotonic() + 15
            if not select.select([self.tn], [], [], 1.0)[0]:
                continue
            frames = []
            while len(frames) < self.rn.max_packets:  # drain what is queued, send as one transfer
                try:
                    pkt = self.tn.read()
                except BlockingIOError:
                    break
                if not pkt or pkt[0] >> 4 != 4 or len(pkt) < 20:
                    continue
                mac = self._next_hop_mac(pkt[16:20])
                if mac is None:
                    continue  # ARP in flight; TCP/ICMP will retry
                frames.append(mac + self.mac + b"\x08\x00" + pkt)
            if frames:
                self._send_many(frames)


def stage_log_frames(rn, seconds=20):
    print(f"\nLogging raw frames for {seconds}s (replug/toggle tethering to provoke traffic)...")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for f in rn.recv_frames(300):
            print("RX", describe(f), "|", hexdump(f, 64))


def tunnel(args, stop, ctl=None, ready_flag=None):
    """Bring the link up and bridge until `stop` is set. Always cleans up routes/DNS/USB."""
    def status(**kw):
        if ctl:
            ctl.update(**kw)

    link = usbio.UsbLink(args.vid, args.pid, args.bus, args.address).open()
    rn = rndis.Rndis(link)
    rn.stop = stop
    d = link.dev
    status(phone=dict(bus=d.bus, address=d.address, vid=d.idVendor, pid=d.idProduct,
                      name=usbio._names.get((d.bus, d.address, d.idVendor, d.idProduct)) or str(d.product)))
    tn = net = None
    try:
        if args.stage <= 1:
            return
        status(detail="RNDIS init")
        rn.initialize()
        mac = rn.query_mac()
        if args.stage <= 2:
            return
        rn.set_packet_filter()
        rn.start_control_thread()
        if args.stage == 3:
            return stage_log_frames(rn)

        status(detail="DHCP")
        dh = dhcp.DhcpClient(rn, mac)
        lease = dh.acquire()
        print(f"\nDHCP lease: ip={lease.ip} mask={lease.mask} gw={lease.router} dns={lease.dns} "
              f"lease={lease.lease_time}s server_mac={lease.server_mac.hex(':')}")
        if args.stage <= 4:
            return

        tun.cleanup_stale()
        tn = tun.Utun()
        br = Bridge(rn, tn, mac, lease, dh, args)
        threading.Thread(target=br.rx_loop, daemon=True).start()
        net = tun.NetConfig(tn.name, lease, not args.no_default_route)
        net.apply()
        gw_mac = br.arp.resolve(br.gw, 5)
        print(f"{tn.name} up: {lease.ip} via {socket.inet_ntoa(br.gw)} "
              f"({gw_mac.hex(':') if gw_mac else 'ARP unresolved!'})")
        if ctl:
            ctl.stats_fn = br.stats
            status(state="connected", iface=tn.name, ip=lease.ip, gateway=socket.inet_ntoa(br.gw),
                   dns=lease.dns, detail="")
        if ready_flag is not None:
            ready_flag["bridge"] = True
        print("Running. Ctrl+C to stop.")
        br.tx_loop()
    finally:
        print("\nCleaning up...")
        stop.set()
        status(state="stopping")
        if net:
            net.restore()
        rn.close()
        if tn:
            tn.close()
        link.close()


def worker(args, stop):
    """Headless mode driven by app.py: report state over the control socket, exit on 'stop'/unplug."""
    if control.request(args.control_uid, "status"):
        sys.exit("a worker is already running")
    ctl = control.ControlServer(args.control_uid, stop)
    ctl.start()
    try:
        tunnel(args, stop, ctl)
    except (Exception, SystemExit) as e:  # keep the socket up briefly so the UI can show why
        log.error("worker failed: %s", e)
        if not stop.is_set():
            ctl.update(state="error", message=str(e) or e.__class__.__name__)
            stop.wait(8)
    finally:
        ctl.close()


def repair():
    """Undo whatever a previous run left behind. Prints a human-readable report. Needs root."""
    import glob
    import subprocess
    if os.geteuid() != 0:
        print("Not running as root: cannot fix everything.")
    me = str(os.getpid())
    pids = [p for p in subprocess.run(["pgrep", "-f", "main.py --worker"], capture_output=True,
                                      text=True).stdout.split() if p != me]
    if pids:
        subprocess.run(["kill", "-TERM", *pids])
        for _ in range(8):  # let the worker restore routes/DNS itself
            time.sleep(0.5)
            pids = [p for p in pids if subprocess.run(["kill", "-0", p], capture_output=True).returncode == 0]
            if not pids:
                break
        if pids:
            subprocess.run(["kill", "-KILL", *pids])
        print("Stopped a leftover ReHoRNDIS worker.")
    socks = glob.glob(os.path.join(os.environ.get("REHORNDIS_SOCK_DIR", "/var/run"), "rehorndis-*.sock"))
    for sp in socks:
        try:
            os.unlink(sp)
        except OSError as e:
            print(f"Could not remove {sp}: {e}")
    if socks:
        print(f"Removed {len(socks)} stale control socket(s).")
    keys = tun.stale_keys()
    if keys:
        tun.cleanup_stale()
        left = tun.stale_keys()
        print(f"Removed {len(keys) - len(left)} stale network entr{'y' if len(keys) == 1 else 'ies'}."
              + (f" Could not remove: {left}" if left else ""))
    tun.run("dscacheutil", "-flushcache")
    tun.run("killall", "-HUP", "mDNSResponder")
    print("Flushed DNS cache.")
    if not (pids or socks or keys):
        print("Nothing was left over from a previous run.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-default-route", action="store_true", help="don't route 0/0 via the phone")
    ap.add_argument("--dump-packets", action="store_true")
    ap.add_argument("--vid", type=lambda s: int(s, 16), default=None, help="hex; default: auto-detect RNDIS")
    ap.add_argument("--pid", type=lambda s: int(s, 16), default=None)
    ap.add_argument("--bus", type=int, default=None, help="USB bus of the phone to use (with --address)")
    ap.add_argument("--address", type=int, default=None, help="USB address of the phone to use")
    ap.add_argument("--stage", type=int, default=99,
                    help="stop after step: 1=claim 2=init+MAC 3=raw frames 4=DHCP (default: run all)")
    ap.add_argument("--repair", action="store_true", help="clean up leftovers of a previous run, then exit")
    ap.add_argument("--worker", action="store_true", help="used by the menu bar app")
    ap.add_argument("--control-uid", type=int, help="user allowed to control the worker (with --worker)")
    args = ap.parse_args()
    if args.worker and args.control_uid is None:
        ap.error("--worker needs --control-uid")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s")
    if args.repair:
        return repair()

    user = int(os.environ.get("SUDO_UID", os.getuid()))  # the app talks to the worker as the real user
    if not args.worker and control.request(user, "status"):
        sys.exit("ReHoRNDIS is already running (turn it off in the menu bar first).")

    stop = threading.Event()
    state = {"bridge": False}

    def on_signal(*_):
        stop.set()
        if not state["bridge"]:
            raise KeyboardInterrupt  # still setting up: don't wait for blocking steps
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        worker(args, stop) if args.worker else tunnel(args, stop, ready_flag=state)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
