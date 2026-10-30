"""Minimal DHCP client that runs over the RNDIS link (raw Ethernet/IP/UDP frames)."""
import logging
import random
import socket
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger("dhcp")

MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK = 1, 2, 3, 4, 5, 6
BCAST_MAC = b"\xff" * 6


@dataclass
class Lease:
    ip: str
    mask: str
    router: str
    dns: list
    lease_time: int
    t1: int
    server_id: bytes
    server_mac: bytes
    acquired: float = field(default_factory=time.monotonic)

    def renewed(self, lease_time):
        self.acquired, self.lease_time, self.t1 = time.monotonic(), lease_time, lease_time // 2

    @property
    def renew_at(self):
        return self.acquired + self.t1


def _csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _wrap(payload, src_ip, dst_ip, src_mac):
    udp = struct.pack("!HHHH", 68, 67, 8 + len(payload), 0) + payload  # UDP csum 0 = none (legal in IPv4)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, src_ip, dst_ip)
    ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
    return BCAST_MAC + src_mac + b"\x08\x00" + ip + udp


def _parse_opts(b):
    opts, i = {}, 0
    while i < len(b):
        c = b[i]
        if c == 255:
            break
        if c == 0:
            i += 1
            continue
        if i + 1 >= len(b):
            break
        opts[c] = b[i + 2:i + 2 + b[i + 1]]
        i += 2 + b[i + 1]
    return opts


class DhcpClient:
    def __init__(self, rndis, mac):
        self.rndis, self.mac, self.xid = rndis, mac, 0

    def _send(self, mtype, extra=b"", ciaddr=b"\0" * 4):
        hdr = struct.pack("!BBBBIHH4s4s4s4s16s64s128s4s", 1, 1, 6, 0, self.xid, 0, 0x8000,
                          ciaddr, b"\0" * 4, b"\0" * 4, b"\0" * 4, self.mac, b"", b"", MAGIC)
        opts = (bytes([53, 1, mtype, 61, 7, 1]) + self.mac + bytes([55, 7, 1, 3, 6, 51, 54, 58, 59])
                + extra + b"\xff")
        payload = (hdr + opts).ljust(300, b"\0")
        self.rndis.send_frame(_wrap(payload, b"\0" * 4, b"\xff" * 4, self.mac))

    def handle(self, frame):
        """Parse a frame; return dict if it is a DHCP reply for our current xid, else None."""
        if len(frame) < 14 + 20 + 8 + 240 or frame[12:14] != b"\x08\x00" or frame[23] != 17:
            return None
        udp = 14 + (frame[14] & 0xF) * 4
        if struct.unpack_from("!H", frame, udp + 2)[0] != 68:
            return None
        b = frame[udp + 8:]
        if len(b) < 240 or b[0] != 2 or b[236:240] != MAGIC or struct.unpack_from("!I", b, 4)[0] != self.xid:
            return None
        opts = _parse_opts(b[240:])
        if 53 not in opts:
            return None
        return dict(type=opts[53][0], yiaddr=b[16:20], siaddr=b[20:24], opts=opts, src_mac=frame[6:12])

    def _wait(self, want, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.rndis.stop.is_set():
            for f in self.rndis.recv_frames(300):
                m = self.handle(f)
                if m and m["type"] == want:
                    return m
                if m and m["type"] == NAK:
                    return None
        return None

    def acquire(self, attempts=5):
        for n in range(attempts):
            if self.rndis.stop.is_set():
                raise InterruptedError("stopped")
            self.xid = random.getrandbits(32)
            log.info("DHCP DISCOVER (try %d)", n + 1)
            self._send(DISCOVER)
            offer = self._wait(OFFER, 4)
            if not offer:
                continue
            sid = offer["opts"].get(54, offer["siaddr"])[:4]
            log.info("DHCP OFFER %s from %s", socket.inet_ntoa(offer["yiaddr"]), socket.inet_ntoa(sid))
            self._send(REQUEST, bytes([50, 4]) + offer["yiaddr"] + bytes([54, 4]) + sid)
            ack = self._wait(ACK, 4)
            if ack:
                return self._lease(ack, sid)
        raise TimeoutError("no DHCP lease from phone")

    @staticmethod
    def _lease(m, sid):
        o, ntoa = m["opts"], socket.inet_ntoa
        lt = struct.unpack("!I", o[51])[0] if 51 in o else 3600
        t1 = struct.unpack("!I", o[58])[0] if 58 in o else lt // 2
        dns = [ntoa(o[6][i:i + 4]) for i in range(0, len(o.get(6, b"")) - 3, 4)]
        return Lease(ip=ntoa(m["yiaddr"]), mask=ntoa(o[1][:4]) if 1 in o else "255.255.255.0",
                     router=ntoa(o[3][:4]) if 3 in o else None, dns=dns, lease_time=lt, t1=t1,
                     server_id=sid, server_mac=m["src_mac"])

    def send_renew(self, lease):
        self.xid = random.getrandbits(32)
        self._send(REQUEST, ciaddr=socket.inet_aton(lease.ip))
