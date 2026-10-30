"""USB layer: find the phone, dump descriptors, claim RNDIS interfaces, bulk/control I/O."""
import errno
import logging
import sys

import usb.core
import usb.util

log = logging.getLogger("usbio")

# RNDIS control interface: class E0 / sub 1 / proto 3. Data: CDC Data 0x0A.
CLS_WIRELESS, CLS_CDC_DATA = 0xE0, 0x0A
RNDIS_TRIPLE = (CLS_WIRELESS, 0x01, 0x03)

# CDC class-specific control requests
SEND_ENCAPSULATED_COMMAND = 0x00
GET_ENCAPSULATED_RESPONSE = 0x01


def _ep_dir(ep):
    return "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"


def _ep_type(ep):
    return {0: "control", 1: "isoc", 2: "bulk", 3: "interrupt"}[usb.util.endpoint_type(ep.bmAttributes)]


def dump_descriptors(dev):
    """Print device + every config/interface/endpoint descriptor."""
    print(f"Device {dev.idVendor:04x}:{dev.idProduct:04x} bcdUSB={dev.bcdUSB:#06x} "
          f"class={dev.bDeviceClass:#04x} sub={dev.bDeviceSubClass:#04x} proto={dev.bDeviceProtocol:#04x} "
          f"maxpkt0={dev.bMaxPacketSize0}")
    for fn in ("manufacturer", "product", "serial_number"):
        try:
            print(f"  {fn}: {getattr(dev, fn)}")
        except Exception as e:  # string descriptors may be unreadable
            print(f"  {fn}: <{e}>")
    for cfg in dev:
        print(f"  Config {cfg.bConfigurationValue}: {cfg.bNumInterfaces} interfaces, "
              f"attrs={cfg.bmAttributes:#04x} maxpower={cfg.bMaxPower * 2}mA")
        for intf in cfg:
            print(f"    Interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting}: "
                  f"class={intf.bInterfaceClass:#04x} sub={intf.bInterfaceSubClass:#04x} "
                  f"proto={intf.bInterfaceProtocol:#04x} neps={intf.bNumEndpoints}")
            for ep in intf:
                print(f"      EP {ep.bEndpointAddress:#04x} {_ep_dir(ep):3} {_ep_type(ep):9} "
                      f"maxpkt={ep.wMaxPacketSize} interval={ep.bInterval}")
            extra = bytes(intf.extra_descriptors) if intf.extra_descriptors else b""
            if extra:
                print(f"      class-specific descriptors: {extra.hex()}")


def is_rndis(dev):
    """True if any interface of the device is RNDIS (class E0/01/03). Needs no root, no open()."""
    try:
        for cfg in dev:
            for intf in cfg:
                if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == RNDIS_TRIPLE:
                    return True
    except (usb.core.USBError, ValueError, NotImplementedError):
        pass
    return False


def find_rndis_device(vid=None, pid=None, bus=None, address=None):
    """The device at bus/address, else the one matching vid/pid, else the first RNDIS device."""
    if bus is not None and address is not None:
        return usb.core.find(bus=bus, address=address)
    if vid is not None and pid is not None:
        return usb.core.find(idVendor=vid, idProduct=pid)
    for dev in usb.core.find(find_all=True) or []:
        if is_rndis(dev):
            return dev
    return None


_names = {}  # (bus, address, vid, pid) -> product name; reading it needs a brief open()


def list_rndis_devices():
    """All attached RNDIS phones: [{bus, address, vid, pid, name}], stable order."""
    out = []
    for dev in usb.core.find(find_all=True) or []:
        if not is_rndis(dev):
            continue
        key = (dev.bus, dev.address, dev.idVendor, dev.idProduct)
        if key not in _names:
            try:
                _names[key] = dev.product or f"{dev.idVendor:04x}:{dev.idProduct:04x}"
            except (usb.core.USBError, ValueError, NotImplementedError):
                _names[key] = f"USB device {dev.idVendor:04x}:{dev.idProduct:04x}"
            finally:
                usb.util.dispose_resources(dev)  # don't keep the phone open between polls
        out.append(dict(bus=dev.bus, address=dev.address, vid=dev.idVendor, pid=dev.idProduct, name=_names[key]))
    return sorted(out, key=lambda d: (d["bus"], d["address"]))


class UsbLink:
    """Claims the RNDIS control+data interfaces and exposes raw transfers."""

    def __init__(self, vid=None, pid=None, bus=None, address=None):
        self.vid, self.pid, self.bus, self.address = vid, pid, bus, address
        self.dev = None
        self.ctrl_if = self.data_if = None
        self.intr_in = self.bulk_in = self.bulk_out = None
        self._claimed = []

    def open(self):
        self.dev = find_rndis_device(self.vid, self.pid, self.bus, self.address)
        if self.dev is None:
            sys.exit("no RNDIS device found (phone plugged in, USB tethering on?)")
        dump_descriptors(self.dev)
        try:
            cfg = self.dev.get_active_configuration()
        except usb.core.USBError as e:
            sys.exit(f"get_active_configuration failed: {e!r}")
        self._pick_interfaces(cfg)
        self._claim()
        return self

    def _pick_interfaces(self, cfg):
        for intf in cfg:
            if intf.bInterfaceClass == CLS_WIRELESS and self.ctrl_if is None:
                self.ctrl_if = intf
            elif intf.bInterfaceClass == CLS_CDC_DATA and intf.bNumEndpoints and self.data_if is None:
                self.data_if = intf
        if not (self.ctrl_if and self.data_if):
            sys.exit("could not find RNDIS control (0xE0) + data (0x0A) interfaces in active config")
        for ep in self.ctrl_if:
            if _ep_type(ep) == "interrupt" and _ep_dir(ep) == "IN":
                self.intr_in = ep
        for ep in self.data_if:
            if _ep_type(ep) != "bulk":
                continue
            if _ep_dir(ep) == "IN":
                self.bulk_in = ep
            else:
                self.bulk_out = ep
        if not (self.bulk_in and self.bulk_out):
            sys.exit("data interface lacks bulk IN/OUT endpoints")
        print(f"\nUsing: ctrl_if={self.ctrl_if.bInterfaceNumber} "
              f"intr_in={self.intr_in.bEndpointAddress:#04x}" if self.intr_in else
              f"\nUsing: ctrl_if={self.ctrl_if.bInterfaceNumber} intr_in=None")
        print(f"       data_if={self.data_if.bInterfaceNumber} "
              f"bulk_in={self.bulk_in.bEndpointAddress:#04x} bulk_out={self.bulk_out.bEndpointAddress:#04x}")

    def _claim(self):
        for intf in (self.ctrl_if, self.data_if):
            n = intf.bInterfaceNumber
            try:
                if self.dev.is_kernel_driver_active(n):
                    log.info("detaching kernel driver from interface %d", n)
                    self.dev.detach_kernel_driver(n)
            except (usb.core.USBError, NotImplementedError) as e:
                log.debug("kernel driver check/detach on %d: %r", n, e)
            try:
                usb.util.claim_interface(self.dev, n)
            except usb.core.USBError as e:
                if e.errno == errno.EACCES:
                    # Another process holds the phone (often ReHoRNDIS itself). Resetting would
                    # disrupt it, so report instead of retrying.
                    sys.exit(f"claim_interface({n}) failed: {e!r} (errno={e.errno}). Another program "
                             "has the phone open; turn ReHoRNDIS off or close it and try again.")
                print(f"claim_interface({n}) failed: {e!r} (errno={e.errno}); resetting device and retrying")
                try:
                    self.dev.reset()
                    usb.util.dispose_resources(self.dev)
                    usb.util.claim_interface(self.dev, n)
                except usb.core.USBError as e2:
                    sys.exit(f"claim_interface({n}) failed after reset: {e2!r} (errno={e2.errno})")
            self._claimed.append(n)
            print(f"claimed interface {n}")

    def close(self):
        if self.dev is None:
            return
        for n in self._claimed:
            try:
                usb.util.release_interface(self.dev, n)
            except usb.core.USBError:
                pass
        usb.util.dispose_resources(self.dev)
        self.dev = None

    # --- transfers (used from step b on) ---
    def send_command(self, data, timeout=1000):
        return self.dev.ctrl_transfer(0x21, SEND_ENCAPSULATED_COMMAND, 0, self.ctrl_if.bInterfaceNumber,
                                      data, timeout)

    def get_response(self, length=1025, timeout=1000):
        return bytes(self.dev.ctrl_transfer(0xA1, GET_ENCAPSULATED_RESPONSE, 0, self.ctrl_if.bInterfaceNumber,
                                            length, timeout))

    def read_notification(self, timeout=1000):
        if not self.intr_in:
            return None
        try:
            return bytes(self.dev.read(self.intr_in.bEndpointAddress, self.intr_in.wMaxPacketSize, timeout))
        except usb.core.USBTimeoutError:
            return None

    def bulk_read(self, length=16384, timeout=100):
        try:
            return bytes(self.dev.read(self.bulk_in.bEndpointAddress, length, timeout))
        except usb.core.USBTimeoutError:
            return None

    def bulk_write(self, data, timeout=1000):
        return self.dev.write(self.bulk_out.bEndpointAddress, data, timeout)
