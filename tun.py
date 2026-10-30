"""macOS utun device (PF_SYSTEM socket) and host network configuration (ifconfig/route/scutil)."""
import fcntl
import logging
import socket
import struct
import subprocess

log = logging.getLogger("tun")

PF_SYSTEM, SYSPROTO_CONTROL, UTUN_OPT_IFNAME = 32, 2, 2
CTLIOCGINFO = 0xC0644E03
AF_INET_HDR = b"\x00\x00\x00\x02"  # utun prepends a 4-byte address family


class Utun:
    def __init__(self):
        s = socket.socket(PF_SYSTEM, socket.SOCK_DGRAM, SYSPROTO_CONTROL)
        info = fcntl.ioctl(s, CTLIOCGINFO, struct.pack("I96s", 0, b"com.apple.net.utun_control"))
        ctl_id = struct.unpack("I96s", info)[0]
        s.connect((ctl_id, 0))  # unit 0 = kernel picks utunN
        self.sock = s
        self.name = s.getsockopt(SYSPROTO_CONTROL, UTUN_OPT_IFNAME, 128).split(b"\0")[0].decode()

    def fileno(self):
        return self.sock.fileno()

    def read(self):
        """-> IP packet (AF header stripped), or None for non-IPv4."""
        d = self.sock.recv(65535)
        return d[4:] if d[:4] == AF_INET_HDR else None

    def write(self, ip_packet):
        self.sock.send(AF_INET_HDR + ip_packet)

    def close(self):
        self.sock.close()


def run(*cmd, stdin=None):
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    log.debug("%s -> rc=%d %s", " ".join(cmd), r.returncode, (r.stdout + r.stderr).strip())
    return r.returncode == 0


class NetConfig:
    """Applies interface IP, routes and DNS; restore() undoes exactly what apply() did."""
    SVC = "State:/Network/Service/rehorndis/DNS"
    SVC_IPV4 = "State:/Network/Service/rehorndis/IPv4"
    SVC_SETUP = "Setup:/Network/Service/rehorndis"
    SPLIT_ROUTES = ("0.0.0.0/1", "128.0.0.0/1")  # beat the existing default without replacing it

    def __init__(self, ifname, lease, default_route=True):
        self.ifname, self.lease, self.default_route = ifname, lease, default_route
        self._routes, self._dns, self._svc = [], False, False

    def apply(self):
        l = self.lease
        peer = l.router or socket.inet_ntoa(l.server_id)
        run("ifconfig", self.ifname, "inet", l.ip, peer, "netmask", l.mask, "mtu", "1500", "up")
        if self.default_route:
            for net in self.SPLIT_ROUTES:
                if run("route", "-n", "add", "-net", net, "-interface", self.ifname):
                    self._routes.append(net)
        if l.dns:
            script = ("d.init\nd.add ServerAddresses * " + " ".join(l.dns)
                      + '\nd.add SupplementalMatchDomains * ""\nset ' + self.SVC + "\n")
            self._dns = run("scutil", stdin=script)
            run("dscacheutil", "-flushcache")
        # Register as a network service so apps (Firefox, Safari) see the Mac as online
        # even with Wi-Fi off; without it macOS has no primary service and reports offline.
        script = (f"d.init\nd.add Addresses * {l.ip}\nd.add SubnetMasks * {l.mask}\n"
                  f"d.add Router {peer}\nd.add InterfaceName {self.ifname}\nset {self.SVC_IPV4}\n"
                  f"d.init\nd.add UserDefinedName rehorndis\nset {self.SVC_SETUP}\n")
        self._svc = run("scutil", stdin=script)
        log.info("configured %s ip=%s peer=%s routes=%s dns=%s", self.ifname, l.ip, peer, self._routes, l.dns)

    def restore(self):
        for net in self._routes:
            run("route", "-n", "delete", "-net", net, "-interface", self.ifname)
        if self._svc:
            run("scutil", stdin=f"remove {self.SVC_IPV4}\nremove {self.SVC_SETUP}\n")
        if self._dns:
            run("scutil", stdin=f"remove {self.SVC}\n")
            run("dscacheutil", "-flushcache")
        self._routes, self._dns, self._svc = [], False, False
