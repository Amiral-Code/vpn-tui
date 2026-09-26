"""Tests for vpn-tui.

    python3 -m unittest discover -s tests -v

The script has no .py extension, so it is loaded through SourceFileLoader
rather than imported. Importing it is side-effect free: main() only runs under
`if __name__ == "__main__"`.

Tests are in two groups. The offline ones stand up their own DNS server on
localhost or replace the module's run() with canned output, so they pass
anywhere, including CI. The live ones need an actual VPN up and skip
themselves when there isn't one -- they are the only way to check that the
code still agrees with the real nmcli, ip and resolvectl output.
"""

import importlib.machinery
import importlib.util
import os
import socket
import struct
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "vpn-tui")
vt = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("vpn_tui", importlib.machinery.SourceFileLoader("vpn_tui", SCRIPT)))
importlib.util.spec_from_loader("vpn_tui", importlib.machinery.SourceFileLoader("vpn_tui", SCRIPT)) \
    .loader.exec_module(vt)


# ── a DNS server we control, so the probe tests need no network ─────────────

class FakeDNS:
    """Answers A queries on 127.0.0.1 with a fixed rcode and answer count."""

    def __init__(self, rcode=0, answers=1, silent=False):
        self.rcode, self.answers, self.silent = rcode, answers, silent
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.sock.close()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data, peer = self.sock.recvfrom(512)
            except (OSError, socket.timeout):
                continue
            if self.silent:
                continue
            # echo the query back with the flags/counts the test asked for
            head = struct.pack(">HHHHHH", struct.unpack(">H", data[:2])[0],
                               0x8180 | self.rcode, 1, self.answers, 0, 0)
            body = data[12:]
            rr = b""
            for _ in range(self.answers):
                rr += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([10, 0, 0, 1])
            try:
                self.sock.sendto(head + body + rr, peer)
            except OSError:
                pass


def vpn_up():
    try:
        return any(r["state"] == "activated" for r in vt.list_vpns())
    except Exception:
        return False


def live_conn():
    r = next(r for r in vt.list_vpns() if r["state"] == "activated")
    c = vt.Conn(r["uuid"], r["name"])
    c.state = "activated"
    c.info = vt.show_conn(r["uuid"])
    c.data = vt.parse_kv_list(c.info.get("vpn.data", ""))
    return c


# ── offline ─────────────────────────────────────────────────────────────────

class TestDnsQuery(unittest.TestCase):
    def query(self, srv, **kw):
        """dns_query against a local server; the port is patched in."""
        real = socket.socket

        class Redirected(real):
            def sendto(self, data, addr):
                return super().sendto(data, ("127.0.0.1", srv.port))

        socket.socket = Redirected
        try:
            return vt.dns_query("127.0.0.1", "example.test", **kw)
        finally:
            socket.socket = real

    def test_answer_is_success(self):
        with FakeDNS(answers=2) as s:
            self.assertEqual(self.query(s), (True, "2 answers"))

    def test_single_answer_is_singular(self):
        with FakeDNS(answers=1) as s:
            self.assertEqual(self.query(s), (True, "1 answer"))

    def test_noerror_with_no_records_fails(self):
        with FakeDNS(answers=0) as s:
            self.assertEqual(self.query(s), (False, "no records"))

    def test_nxdomain_named(self):
        with FakeDNS(rcode=3, answers=0) as s:
            self.assertEqual(self.query(s), (False, "NXDOMAIN"))

    def test_servfail_and_refused_named(self):
        with FakeDNS(rcode=2, answers=0) as s:
            self.assertEqual(self.query(s), (False, "SERVFAIL"))
        with FakeDNS(rcode=5, answers=0) as s:
            self.assertEqual(self.query(s), (False, "REFUSED"))

    def test_silent_server_times_out_without_raising(self):
        with FakeDNS(silent=True) as s:
            ok, detail = self.query(s, timeout=0.3)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_unresolvable_host_is_reported_not_raised(self):
        ok, _ = vt.dns_query("no-such-host.invalid", "example.test", timeout=1)
        self.assertFalse(ok)


class TestDnsVerdict(unittest.TestCase):
    def test_no_resolved_says_nothing(self):
        self.assertEqual(vt.dns_verdict(["10.0.0.1"], None), ("", ""))

    def test_link_with_no_dns_is_an_error(self):
        state, _ = vt.dns_verdict(["10.0.0.1"], [])
        self.assertEqual(state, "err")

    def test_private_servers_present_is_active(self):
        self.assertEqual(vt.dns_verdict(["10.0.0.1"], ["10.0.0.1"])[0], "ok")

    def test_dropped_public_servers_are_not_a_fault(self):
        # the split-DNS fix: the profile pushes public resolvers too, and the
        # dispatcher deliberately keeps only the private pair
        state, _ = vt.dns_verdict(["10.0.0.1", "10.0.0.2", "1.1.1.1", "8.8.8.8"],
                                  ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(state, "ok")

    def test_missing_private_server_warns(self):
        state, _ = vt.dns_verdict(["10.0.0.1", "1.1.1.1"], ["1.1.1.1"])
        self.assertEqual(state, "warn")

    def test_all_public_profile_still_active(self):
        self.assertEqual(vt.dns_verdict(["1.1.1.1"], ["1.1.1.1"])[0], "ok")

    def test_unchecked_link_stays_quiet(self):
        self.assertEqual(vt.dns_verdict(["10.0.0.1"], ["9.9.9.9"], checked=False), ("", ""))


class TestDnsHijacked(unittest.TestCase):
    def test_public_answering_for_private_profile(self):
        self.assertTrue(vt.dns_hijacked(["10.0.0.1", "1.1.1.1"], "1.1.1.1"))

    def test_private_answering_is_fine(self):
        self.assertFalse(vt.dns_hijacked(["10.0.0.1", "1.1.1.1"], "10.0.0.1"))

    def test_all_public_profile_is_not_hijacked(self):
        self.assertFalse(vt.dns_hijacked(["1.1.1.1", "8.8.8.8"], "1.1.1.1"))

    def test_unknown_current_server_stays_quiet(self):
        self.assertFalse(vt.dns_hijacked(["10.0.0.1"], ""))


class TestResolvedCurrent(unittest.TestCase):
    """resolved_current shells out, so replace run() with canned output."""

    def patch(self, rc, out):
        real = vt.run
        vt.run = lambda *a, **k: (rc, out, "")
        self.addCleanup(lambda: setattr(vt, "run", real))

    def test_reads_the_current_server(self):
        self.patch(0, "Link 25 (tun0)\n  Current DNS Server: 10.10.10.111\n"
                      "       DNS Servers: 10.10.10.111 10.10.10.112\n")
        self.assertEqual(vt.resolved_current("tun0"), "10.10.10.111")

    def test_strips_the_dot_name_suffix(self):
        self.patch(0, "Current DNS Server: 1.0.0.1#cloudflare-dns.com\n")
        self.assertEqual(vt.resolved_current("tun0"), "1.0.0.1")

    def test_absent_line_gives_empty(self):
        self.patch(0, "Link 25 (tun0)\n  Protocols: -DefaultRoute\n")
        self.assertEqual(vt.resolved_current("tun0"), "")

    def test_failure_gives_empty(self):
        self.patch(1, "")
        self.assertEqual(vt.resolved_current("tun0"), "")

    def test_no_iface_does_not_shell_out(self):
        calls = []
        real = vt.run
        vt.run = lambda *a, **k: (calls.append(a), (0, "", ""))[1]
        self.addCleanup(lambda: setattr(vt, "run", real))
        self.assertEqual(vt.resolved_current(""), "")
        self.assertEqual(calls, [])


class TestStalled(unittest.TestCase):
    def test_fresh_connection_is_not_stalled(self):
        self.assertFalse(vt.Conn("u", "n").stalled)

    def test_grace_period_suppresses_the_warning(self):
        c = vt.Conn("u", "n")
        c.stall_since = time.time()
        self.assertFalse(c.stalled)

    def test_reported_once_past_the_grace_period(self):
        c = vt.Conn("u", "n")
        c.stall_since = time.time() - 6
        self.assertTrue(c.stalled)


class TestSample(unittest.TestCase):
    """sample() calls iface_for(), which shells out to ip; stub it."""

    def setUp(self):
        self.m = vt.Model.__new__(vt.Model)
        self.real = vt.iface_for
        self.addCleanup(lambda: setattr(vt, "iface_for", self.real))

    def conn(self, addr):
        c = vt.Conn("u", "n")
        c.state = "activated"
        c.info = {"IP4.ADDRESS[1]": addr + "/32"} if addr else {}
        return c

    def test_address_on_a_link_is_healthy(self):
        vt.iface_for = lambda a: "tun0"
        c = self.conn("10.1.1.6")
        self.m.sample(c, time.time())
        self.assertEqual(c.iface, "tun0")
        self.assertEqual(c.stall_since, 0.0)

    def test_address_on_no_link_records_a_stall(self):
        vt.iface_for = lambda a: ""
        c = self.conn("10.1.1.6")
        now = time.time()
        self.m.sample(c, now)
        self.assertEqual(c.iface, "")
        self.assertEqual(c.stall_since, now)

    def test_stall_timestamp_is_not_refreshed(self):
        vt.iface_for = lambda a: ""
        c = self.conn("10.1.1.6")
        now = time.time()
        self.m.sample(c, now)
        self.m.sample(c, now + 30)
        self.assertEqual(c.stall_since, now)

    def test_no_address_at_all_clears_a_stale_iface(self):
        vt.iface_for = lambda a: "tun0"
        c = self.conn("")
        c.iface, c.iface_addr = "tun0", "10.1.1.6"
        now = time.time()
        self.m.sample(c, now)
        self.assertEqual(c.iface, "")
        self.assertEqual(c.stall_since, now)

    def test_stall_clears_when_the_link_returns(self):
        vt.iface_for = lambda a: ""
        c = self.conn("10.1.1.6")
        self.m.sample(c, time.time())
        vt.iface_for = lambda a: "tun0"
        c.info = {"IP4.ADDRESS[1]": "10.1.1.7/32"}
        self.m.sample(c, time.time())
        self.assertEqual(c.stall_since, 0.0)
        self.assertFalse(c.stalled)


class TestLogs(unittest.TestCase):
    def setUp(self):
        import collections

        class FakeModel:
            def owner_of_pid(self, pid):
                return None

        lg = vt.Logs.__new__(vt.Logs)
        lg.model, lg.lock = FakeModel(), threading.Lock()
        lg.by_uuid = collections.defaultdict(lambda: collections.deque(maxlen=100))
        lg.all = collections.deque(maxlen=100)
        lg.version, lg.pid_owner, lg.pending = 0, {}, []
        lg.last_owner, lg.uuids = None, frozenset({"U1"})
        self.lg = lg

    @staticmethod
    def ev(ident, msg, pid=0, nm=None):
        e = {"SYSLOG_IDENTIFIER": ident, "MESSAGE": msg, "_BOOT_ID": "b",
             "__REALTIME_TIMESTAMP": "1700000000000000"}
        if pid:
            e["_PID"] = str(pid)
        if nm:
            e["NM_CONNECTION"] = nm
        return e

    def test_count_respects_the_window(self):
        now = time.time()
        for age in (7200, 1800, 600):
            self.lg.by_uuid["U1"].append((now - age, 0, "warn", "ping-restart"))
        self.assertEqual(self.lg.count("U1", now - 3600, "ping-restart"), 2)
        self.assertEqual(self.lg.count("U1", now - 86400, "ping-restart"), 3)

    def test_count_of_unknown_profile_is_zero(self):
        self.assertEqual(self.lg.count("nope", 0, "ping-restart"), 0)

    def test_vpn_plugin_denial_is_kept_as_an_error(self):
        self.lg._ingest(self.ev("dbus-broker",
                                "A security policy denied :1.1 ... NetworkManager.VPN.Plugin.SetIp4Config"))
        self.assertEqual(len(self.lg.all), 1)
        self.assertEqual(self.lg.all[-1][2], "err")
        self.assertTrue(self.lg.all[-1][3].startswith("D-Bus: "))

    def test_dbus_daemon_variant_also_kept(self):
        self.lg._ingest(self.ev("dbus-daemon",
                                "denied ... NetworkManager.VPN.Plugin.SetConfig"))
        self.assertEqual(len(self.lg.all), 1)

    def test_other_bus_traffic_is_dropped(self):
        for msg in ("A security policy denied :1.2 ... DBus.Monitoring.BecomeMonitor",
                    "Activating service name='org.freedesktop.systemd1'",
                    "Successfully activated service 'org.bluez'"):
            self.lg._ingest(self.ev("dbus-broker", msg))
        self.assertEqual(len(self.lg.all), 0)

    def test_denial_is_filed_under_the_profile_that_last_spoke(self):
        self.lg._ingest(self.ev("nm-openvpn", "Initialization Sequence Completed", pid=999))
        self.lg._ingest(self.ev("dbus-broker", "denied ... NetworkManager.VPN.Plugin.SetConfig"))
        filed = [m for _, _, _, m in self.lg.by_uuid["U1"] if m.startswith("D-Bus:")]
        self.assertEqual(len(filed), 1)

    def test_networkmanager_lines_still_ingest(self):
        self.lg._ingest(self.ev("NetworkManager", "<info>  [1.2] vpn[0x1]: starting openvpn", nm="U1"))
        lines = [m for _, _, _, m in self.lg.by_uuid["U1"] if m.startswith("NetworkManager:")]
        self.assertEqual(len(lines), 1)


class TestHealthPlan(unittest.TestCase):
    def test_empty_profile_degrades_to_one_check(self):
        c = vt.Conn("u", "n")
        c.state, c.info = "activated", {}
        plan = vt.health_plan(c)
        self.assertEqual(len(plan), 1)
        ok, detail = plan[0][1]()
        self.assertFalse(ok)
        self.assertIn("no address", detail)

    def test_stale_address_is_reported_as_the_fault(self):
        real = vt.iface_for
        vt.iface_for = lambda a: ""
        self.addCleanup(lambda: setattr(vt, "iface_for", real))
        c = vt.Conn("u", "n")
        c.state, c.info = "activated", {"IP4.ADDRESS[1]": "10.99.99.99/32"}
        ok, detail = vt.health_plan(c)[0][1]()
        self.assertFalse(ok)
        self.assertIn("reconnect", detail)

    def test_no_gateway_ping_check(self):
        # a point-to-point peer commonly ignores ICMP, so a silent one proves
        # nothing; probing it only produced false alarms on healthy tunnels
        c = vt.Conn("u", "n")
        c.state = "activated"
        c.info = {"IP4.ADDRESS[1]": "10.1.1.6/32", "IP4.GATEWAY": "10.1.1.5"}
        self.assertNotIn("Gateway", [label for label, _ in vt.health_plan(c)])


class TestCliReconnect(unittest.TestCase):
    """cli_reconnect drives nmcli; record the calls instead of making them."""

    def setUp(self):
        self.calls = []
        saved = {k: getattr(vt, k) for k in ("resolve", "nmcli", "run", "notify", "load_config")}
        self.addCleanup(lambda: [setattr(vt, k, v) for k, v in saved.items()])
        vt.nmcli = lambda *a, **k: (self.calls.append(a), (0, "", ""))[1]
        vt.run = lambda *a, **k: (self.calls.append(tuple(a[0])), (0, "", ""))[1]
        vt.notify = lambda *a, **k: None
        vt.load_config = lambda: {}

    def profile(self, state):
        vt.resolve = lambda t: {"uuid": "U1", "name": "vpn", "state": state}

    def test_unknown_profile_fails(self):
        vt.resolve = lambda t: None
        self.assertEqual(vt.cli_reconnect("nope"), 1)
        self.assertEqual(self.calls, [])

    def test_disconnected_profile_is_left_alone(self):
        self.profile("")
        self.assertEqual(vt.cli_reconnect("vpn"), 0)
        self.assertEqual(self.calls, [], "a deliberately down VPN must not be raised")

    def test_active_profile_is_cycled_after_the_network_is_up(self):
        self.profile("activated")
        self.assertEqual(vt.cli_reconnect("vpn"), 0)
        self.assertEqual(self.calls, [
            ("connection", "down", "uuid", "U1"),
            ("nm-online", "-q", "-t", "60"),      # wait before bringing it back
            ("connection", "up", "uuid", "U1"),
        ])

    def test_failure_to_come_back_up_is_reported(self):
        self.profile("activated")
        vt.nmcli = lambda *a, **k: (0, "", "") if a[1] == "down" else (4, "", "boom")
        self.assertEqual(vt.cli_reconnect("vpn"), 1)


# ── live: needs a VPN up ────────────────────────────────────────────────────

@unittest.skipUnless(vpn_up(), "no VPN connection is active")
class TestLive(unittest.TestCase):
    def test_resolved_current_matches_resolvectl(self):
        c = live_conn()
        iface = vt.iface_for(c.info.get("IP4.ADDRESS[1]", "").split("/")[0])
        if not iface:
            self.skipTest("the active profile holds no address")
        rc, out, _ = vt.run(["resolvectl", "status", iface], timeout=5)
        want = ""
        for line in (out or "").splitlines():
            if "Current DNS Server:" in line:
                want = line.split(":", 1)[1].split("#")[0].strip()
        self.assertEqual(vt.resolved_current(iface), want)

    def test_health_plan_runs_clean_on_a_live_tunnel(self):
        c = live_conn()
        for label, fn in vt.health_plan(c):
            ok, detail = fn()
            self.assertTrue(ok, f"{label}: {detail}")

    def test_interface_check_agrees_with_the_kernel(self):
        c = live_conn()
        addr = c.info.get("IP4.ADDRESS[1]", "").split("/")[0]
        ok, _ = vt.health_plan(c)[0][1]()
        self.assertEqual(ok, bool(vt.iface_for(addr)))


if __name__ == "__main__":
    unittest.main()
