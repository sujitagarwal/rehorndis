#!/usr/bin/env python3
"""RNDIS USB tethering daemon for macOS. Run: sudo python3 main.py"""
import argparse
import logging
import select
import signal
import socket
import struct
import threading
import time

import usb.core

import arp
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

    def _send(self, frame):
        if self.args.dump_packets:
            print("TX", describe(frame), "|", hexdump(frame))
        self.rn.send_frame(frame)

    # ---- phone -> Mac ----
    def rx_loop(self):
        while not self.stop.is_set():
            try:
                frames = self.rn.recv_frames(200)
            except usb.core.USBError as e:
                log.error("bulk read failed: %r", e)
                self.stop.set()
                return
            for f in frames:
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
            pkt = self.tn.read()
            if not pkt or pkt[0] >> 4 != 4 or len(pkt) < 20:
                continue
            mac = self._next_hop_mac(pkt[16:20])
            if mac is None:
                continue  # ARP in flight; TCP/ICMP will retry
            self._send(mac + self.mac + b"\x08\x00" + pkt)


def run_menubar(ifname, lease, stop):
    """Show a menu bar item until `stop` is set or the user picks Quit. Blocks on the main thread."""
    import AppKit
    import rumps
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon
    def stop_loop():
        # NSApp.terminate would exit() the process and skip our cleanup; stop_ makes app.run() return.
        app = AppKit.NSApp()
        app.stop_(None)
        app.postEvent_atStart_(AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0), True)  # wake the loop
    def quit_(_):
        stop.set()
        stop_loop()
    app = rumps.App("ReHoRNDIS", title="⇄ USB", quit_button=None)
    app.menu = [f"{ifname}: {lease.ip}", f"Gateway: {lease.router}", f"DNS: {', '.join(lease.dns)}",
                None, rumps.MenuItem("Quit", callback=quit_)]
    # also lets Python run so Ctrl+C / SIGTERM handlers fire inside the Cocoa loop
    rumps.Timer(lambda _: stop.is_set() and stop_loop(), 1).start()
    app.run()


def stage_log_frames(rn, seconds=20):
    print(f"\nLogging raw frames for {seconds}s (replug/toggle tethering to provoke traffic)...")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for f in rn.recv_frames(300):
            print("RX", describe(f), "|", hexdump(f, 64))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-default-route", action="store_true", help="don't route 0/0 via the phone")
    ap.add_argument("--dump-packets", action="store_true")
    ap.add_argument("--no-menubar", action="store_true", help="don't show the menu bar status item")
    ap.add_argument("--vid", type=lambda s: int(s, 16), default=usbio.VID)
    ap.add_argument("--pid", type=lambda s: int(s, 16), default=usbio.PID)
    ap.add_argument("--stage", type=int, default=99,
                    help="stop after step: 1=claim 2=init+MAC 3=raw frames 4=DHCP (default: run all)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s")

    link = usbio.UsbLink(args.vid, args.pid).open()
    rn = rndis.Rndis(link)
    tn = net = None
    try:
        if args.stage <= 1:
            return
        rn.initialize()
        mac = rn.query_mac()
        if args.stage <= 2:
            return
        rn.set_packet_filter()
        rn.start_control_thread()
        if args.stage == 3:
            return stage_log_frames(rn)

        dh = dhcp.DhcpClient(rn, mac)
        lease = dh.acquire()
        print(f"\nDHCP lease: ip={lease.ip} mask={lease.mask} gw={lease.router} dns={lease.dns} "
              f"lease={lease.lease_time}s server_mac={lease.server_mac.hex(':')}")
        if args.stage <= 4:
            return

        tn = tun.Utun()
        br = Bridge(rn, tn, mac, lease, dh, args)
        rxt = threading.Thread(target=br.rx_loop, daemon=True)
        rxt.start()
        net = tun.NetConfig(tn.name, lease, not args.no_default_route)
        net.apply()
        gw_mac = br.arp.resolve(br.gw, 5)
        print(f"{tn.name} up: {lease.ip} via {socket.inet_ntoa(br.gw)} ({gw_mac.hex(':') if gw_mac else 'ARP unresolved!'})")
        signal.signal(signal.SIGINT, lambda *_: rn.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: rn.stop.set())
        print("Running. Ctrl+C to stop.")
        try:
            if args.no_menubar:
                raise ImportError
            import rumps  # noqa: F401
        except ImportError:
            args.no_menubar = True
        if not args.no_menubar:
            worker = threading.Thread(target=br.tx_loop, daemon=True)
            worker.start()
            run_menubar(tn.name, lease, rn.stop)
            rn.stop.set()
            worker.join(3)
        else:
            br.tx_loop()
    finally:
        print("\nCleaning up...")
        rn.stop.set()
        if net:
            net.restore()
        rn.close()
        if tn:
            tn.close()
        link.close()


if __name__ == "__main__":
    main()
