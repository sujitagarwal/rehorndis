"""RNDIS control protocol and data framing (MS-RNDIS). All fields little-endian."""
import errno
import logging
import struct
import threading
import time

import usb.core

log = logging.getLogger("rndis")

MSG_PACKET, MSG_INIT, MSG_HALT, MSG_QUERY, MSG_SET = 0x1, 0x2, 0x3, 0x4, 0x5
MSG_RESET, MSG_INDICATE, MSG_KEEPALIVE = 0x6, 0x7, 0x8
CMPLT = 0x80000000

OID_802_3_PERMANENT_ADDRESS = 0x01010101
OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E

PKT_DIRECTED, PKT_MULTICAST, PKT_BROADCAST = 0x1, 0x2, 0x8

PACKET_HDR_LEN = 44  # REMOTE_NDIS_PACKET_MSG header; data_offset field = 44 - 8


class RndisError(Exception):
    pass


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


class Rndis:
    def __init__(self, link):
        self.usb = link
        self.mac = None
        self.max_transfer = 16384
        self.max_packets = 1
        self.stop = threading.Event()
        self._req = 0
        self._tx_lock = threading.Lock()
        self._ctl_thread = None

    def _rid(self):
        self._req += 1
        return self._req

    # ---- control channel ----
    def _fetch(self, wait_ms):
        """Wait for RESPONSE_AVAILABLE on the interrupt EP, then GET_ENCAPSULATED_RESPONSE."""
        self.usb.read_notification(wait_ms)
        try:
            data = self.usb.get_response()
        except usb.core.USBError as e:
            log.debug("get_response: %r", e)
            return None
        return data or None

    def _await(self, want, rid, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._fetch(300)
            if msg is None or len(msg) < 12:
                continue
            log.debug("ctl rx: %s", msg.hex())
            if u32(msg, 0) == want and u32(msg, 8) == rid:
                return msg
            self._handle_control(msg)
        raise RndisError(f"timeout waiting for message type {want:#x}")

    def _handle_control(self, msg):
        t = u32(msg, 0)
        if t == MSG_KEEPALIVE:
            self.usb.send_command(struct.pack("<IIII", CMPLT | MSG_KEEPALIVE, 16, u32(msg, 8), 0))
            log.debug("keepalive answered")
        elif t == MSG_INDICATE:
            log.info("indicate status: %s", msg.hex())
        else:
            log.debug("ignoring control msg type %#x", t)

    def initialize(self):
        rid = self._rid()
        self.usb.send_command(struct.pack("<6I", MSG_INIT, 24, rid, 1, 0, self.max_transfer))
        r = self._await(CMPLT | MSG_INIT, rid)
        if len(r) < 52:
            raise RndisError(f"short INIT completion: {r.hex()}")
        (status, major, minor, flags, medium, max_pkts, max_xfer, align, _afo, _afs) = \
            struct.unpack_from("<10I", r, 12)
        if status != 0:
            raise RndisError(f"INIT failed, status {status:#x}")
        self.max_packets, self.max_transfer = max_pkts, max_xfer
        print(f"RNDIS init OK: v{major}.{minor} flags={flags:#x} medium={medium} "
              f"max_packets/msg={max_pkts} max_transfer={max_xfer} align=2^{align}")

    def query(self, oid):
        rid = self._rid()
        self.usb.send_command(struct.pack("<7I", MSG_QUERY, 28, rid, oid, 0, 20, 0))
        r = self._await(CMPLT | MSG_QUERY, rid)
        status, ilen, ioff = struct.unpack_from("<3I", r, 12)
        if status != 0:
            raise RndisError(f"QUERY {oid:#x} failed, status {status:#x}")
        return r[8 + ioff:8 + ioff + ilen]

    def set(self, oid, value):
        rid = self._rid()
        self.usb.send_command(struct.pack("<7I", MSG_SET, 28 + len(value), rid, oid, len(value), 20, 0) + value)
        r = self._await(CMPLT | MSG_SET, rid)
        status = u32(r, 12)
        if status != 0:
            raise RndisError(f"SET {oid:#x} failed, status {status:#x}")

    def query_mac(self):
        mac = self.query(OID_802_3_PERMANENT_ADDRESS)
        if len(mac) != 6:
            raise RndisError(f"bad MAC length: {mac.hex()}")
        self.mac = mac
        print("RNDIS permanent MAC:", ":".join(f"{b:02x}" for b in mac))
        return mac

    def set_packet_filter(self, flags=PKT_DIRECTED | PKT_MULTICAST | PKT_BROADCAST):
        self.set(OID_GEN_CURRENT_PACKET_FILTER, struct.pack("<I", flags))
        print(f"packet filter set: {flags:#x}")

    def start_control_thread(self):
        """Service interrupt notifications (keepalives, status indications) in background."""
        self._ctl_thread = threading.Thread(target=self._control_loop, daemon=True)
        self._ctl_thread.start()

    def _control_loop(self):
        while not self.stop.is_set():
            try:
                n = self.usb.read_notification(500)
                if n is None:
                    continue
                msg = self.usb.get_response()
                if msg and len(msg) >= 12:
                    self._handle_control(msg)
            except usb.core.USBError as e:
                if e.errno == errno.ENODEV:
                    if not self.stop.is_set():
                        log.error("device gone")
                    self.stop.set()
                    return
                log.debug("control loop: %r", e)
                time.sleep(0.2)

    def close(self):
        self.stop.set()
        try:
            self.usb.send_command(struct.pack("<3I", MSG_HALT, 12, self._rid()))
        except usb.core.USBError:
            pass

    # ---- data channel ----
    @staticmethod
    def _wrap(frame):
        n = len(frame)
        return struct.pack("<11I", MSG_PACKET, PACKET_HDR_LEN + n, PACKET_HDR_LEN - 8, n,
                           0, 0, 0, 0, 0, 0, 0) + frame

    def send_frame(self, frame):
        self.send_frames([frame])

    def send_frames(self, frames):
        """Send frames in as few bulk transfers as the device allows (packets/message and size caps)."""
        batch, size = [], 0
        for f in frames:
            msg = self._wrap(f)
            if batch and (len(batch) >= self.max_packets or size + len(msg) > self.max_transfer):
                self._write(batch)
                batch, size = [], 0
            batch.append(msg)
            size += len(msg)
        if batch:
            self._write(batch)

    def _write(self, msgs):
        data = b"".join(msgs)
        with self._tx_lock:
            self.usb.bulk_write(data)
            if len(data) % self.usb.bulk_out.wMaxPacketSize == 0:
                self.usb.bulk_write(b"")  # zero-length packet terminates the transfer

    def read_raw(self, timeout_ms=200):
        """One bulk read of raw RNDIS data (may hold several packet messages), or None on timeout."""
        return self.usb.bulk_read(max(self.max_transfer, 16384), timeout_ms)

    def recv_frames(self, timeout_ms=200):
        """One bulk read -> list of Ethernet frames (a transfer can carry several packet msgs)."""
        data = self.read_raw(timeout_ms)
        return self.unwrap(data) if data else []

    @staticmethod
    def unwrap(data):
        frames, off = [], 0
        while off + 8 <= len(data):
            mtype, mlen = struct.unpack_from("<II", data, off)
            if mlen < 8 or off + mlen > len(data):
                log.warning("malformed RNDIS data msg at %d (type=%#x len=%d of %d)", off, mtype, mlen, len(data))
                break
            if mtype == MSG_PACKET and mlen >= PACKET_HDR_LEN:
                doff, dlen = struct.unpack_from("<II", data, off + 8)
                start = off + 8 + doff
                if start + dlen <= off + mlen:
                    frames.append(data[start:start + dlen])
            else:
                log.debug("non-packet msg on bulk: type=%#x", mtype)
            off += mlen
        return frames
