"""Minimal ARP: answer requests for our IP, resolve next-hop MACs."""
import struct
import threading
import time

ETH_ARP = 0x0806
BCAST = b"\xff" * 6
ZERO_IP = b"\0" * 4


def _pkt(op, smac, sip, tmac, tip, eth_dst):
    return (eth_dst + smac + struct.pack("!H", ETH_ARP)
            + struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, smac, sip, tmac, tip))


def parse(frame):
    """-> (op, sender_mac, sender_ip, target_mac, target_ip) or None"""
    if len(frame) < 42 or frame[12:14] != b"\x08\x06":
        return None
    _h, ptype, _hl, _pl, op, sha, spa, tha, tpa = struct.unpack_from("!HHBBH6s4s6s4s", frame, 14)
    return None if ptype != 0x0800 else (op, sha, spa, tha, tpa)


class ArpTable:
    def __init__(self, mac, send):
        self.mac, self.send = mac, send
        self.ip = ZERO_IP
        self.table = {}
        self._cond = threading.Condition()
        self._last_req = {}

    def learn(self, ip, mac):
        with self._cond:
            self.table[ip] = mac
            self._cond.notify_all()

    def handle(self, frame):
        """Process an ARP frame; returns True if it was ARP."""
        p = parse(frame)
        if not p:
            return False
        op, sha, spa, _tha, tpa = p
        if spa != ZERO_IP:
            self.learn(spa, sha)
        if op == 1 and self.ip != ZERO_IP and tpa == self.ip:
            self.send(_pkt(2, self.mac, self.ip, sha, spa, sha))
        return True

    def request(self, ip):
        self.send(_pkt(1, self.mac, self.ip, b"\0" * 6, ip, BCAST))

    def lookup(self, ip):
        """Non-blocking: cached MAC, else fire a (rate-limited) request and return None."""
        mac = self.table.get(ip)
        if mac is None and time.monotonic() - self._last_req.get(ip, 0) > 1.0:
            self._last_req[ip] = time.monotonic()
            self.request(ip)
        return mac

    def resolve(self, ip, timeout=5.0):
        """Blocking; needs another thread feeding handle()."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while ip not in self.table and time.monotonic() < deadline:
                self.request(ip)
                self._cond.wait(1.0)
            return self.table.get(ip)
