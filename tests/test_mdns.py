"""Names on the network (baabaa.local): the mDNS wire format, and what the responder answers. No network:
the responder writes to a stand-in for its socket."""

import asyncio
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baabaa.server import mdns  # noqa: E402

IP = "192.168.50.20"
PEER = ("192.168.50.9", mdns.PORT)


class Sent:
    """Stands in for the socket: keeps what the responder sends, parsed."""

    def __init__(self, then=None):
        self.out, self.then = [], then

    def sendto(self, data, to):
        self.out.append((mdns.parse(data), to))
        if self.then:
            self.then(data)

    def close(self):
        pass


def query(*questions, ident=0, answers=(), authority=()):
    return mdns.message(questions=questions, answers=answers, authority=authority, ident=ident, flags=0)


def a_record(name, ip, ttl=mdns.TTL, flush=True):
    return mdns.record(name, mdns.A, socket.inet_aton(ip), ttl, flush)


def answers(msg, section="answer"):
    return [(r["name"], r["type"], r["class"], r["ttl"], r["data"]) for r in msg["records"] if r["section"] == section]


class TestNames(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(mdns.clean_names(["Baabaa.local", " ai ", "baabaa"]), ["baabaa", "ai"])
        for bad in ("", "two words", "-x", "x-", "a" * 64, "dots.in.it", "ünï"):
            with self.assertRaises(ValueError, msg=bad):
                mdns.clean_names([bad])


class TestWire(unittest.TestCase):
    def test_round_trip(self):
        data = mdns.message(answers=[a_record("baabaa.local", IP)], additional=[mdns.record("baabaa.local", mdns.NSEC, mdns.nsec("baabaa.local"), 120)],
                            questions=[("baabaa.local", mdns.A, mdns.IN)], ident=7)
        m = mdns.parse(data)
        self.assertEqual((m["id"], m["flags"], m["questions"]), (7, 0x8400, [("baabaa.local", 1, 1)]))
        self.assertEqual(answers(m), [("baabaa.local", 1, 0x8001, 120, socket.inet_aton(IP))])
        (nsec,) = answers(m, "additional")
        self.assertEqual(nsec[4], mdns.encode_name("baabaa.local") + b"\x00\x01\x40")  # A, and nothing else

    def test_compressed_names(self):
        # A and AAAA for the same name, the second a pointer to the first, the way phones ask
        data = (struct.pack("!6H", 0, 0, 2, 0, 0, 0) + mdns.encode_name("BAABAA.local") + struct.pack("!2H", 1, 0x8001)
                + b"\xc0\x0c" + struct.pack("!2H", 28, 1))
        self.assertEqual(mdns.parse(data)["questions"], [("baabaa.local", 1, 0x8001), ("baabaa.local", 28, 1)])

    def test_bad_messages(self):
        head = struct.pack("!6H", 0, 0, 1, 0, 0, 0)
        for data in (b"", b"\0" * 11, head + b"\xc0\x0c\0\1\0\1",  # a pointer to itself
                     head + b"\x05abc", head + b"\x40abc\0\0\1\0\1", head + mdns.encode_name("x.local") + b"\0"):
            with self.assertRaises(ValueError, msg=data):
                mdns.parse(data)


class TestAnswers(unittest.TestCase):
    def setUp(self):
        self.r = mdns.Responder(IP, accept=lambda host: host.startswith("192.168.50."))
        self.r.transport = self.sent = Sent()
        self.r.names = ["baabaa.local", "ai.local"]

    def ask(self, data, src=PEER):
        self.sent.out.clear()
        self.r.datagram_received(data, src)
        return self.sent.out

    def test_address(self):
        ((m, to),) = self.ask(query(("BAABAA.local", mdns.A, mdns.IN | mdns.TOP)))
        self.assertEqual((to, m["id"], m["flags"], m["questions"]), ((mdns.GROUP, mdns.PORT), 0, 0x8400, []))
        self.assertEqual(answers(m), [("baabaa.local", mdns.A, 0x8001, mdns.TTL, socket.inet_aton(IP))])
        self.assertEqual([r[1] for r in answers(m, "additional")], [mdns.NSEC])  # so nobody waits for IPv6

    def test_no_ipv6(self):
        ((m, _),) = self.ask(query(("ai.local", mdns.AAAA, mdns.IN)))
        self.assertEqual([(r[0], r[1]) for r in answers(m)], [("ai.local", mdns.NSEC)])

    def test_ordinary_resolver(self):
        ((m, to),) = self.ask(query(("baabaa.local", mdns.A, mdns.IN), ident=0x1234), src=("192.168.50.9", 40000))
        self.assertEqual((to, m["id"], m["questions"]), (("192.168.50.9", 40000), 0x1234, [("baabaa.local", 1, 1)]))
        self.assertEqual(answers(m), [("baabaa.local", mdns.A, 1, mdns.LEGACY_TTL, socket.inet_aton(IP))])

    def test_silent(self):
        self.assertEqual(self.ask(query(("other.local", mdns.A, mdns.IN))), [])
        self.assertEqual(self.ask(query(("baabaa.local", mdns.A, mdns.IN)), src=("10.9.9.9", mdns.PORT)), [])  # not this network
        self.assertEqual(self.ask(b"\x00\x01garbage"), [])
        self.assertEqual(self.ask(mdns.message(answers=[a_record("baabaa.local", "192.168.50.77")])), [])  # a response

    def test_known_answer(self):
        self.assertEqual(self.ask(query(("baabaa.local", mdns.A, mdns.IN), answers=[a_record("baabaa.local", IP)])), [])
        self.assertEqual(len(self.ask(query(("baabaa.local", mdns.A, mdns.IN), answers=[a_record("baabaa.local", IP, ttl=30)]))), 1)

    def test_once_a_second_except_probes(self):
        self.assertEqual(len(self.ask(query(("baabaa.local", mdns.A, mdns.IN)))), 1)
        self.assertEqual(self.ask(query(("baabaa.local", mdns.A, mdns.IN))), [])
        probe = query(("baabaa.local", mdns.ANY, mdns.IN), authority=[a_record("baabaa.local", "192.168.50.77", flush=False)])
        self.assertEqual(len(self.ask(probe)), 1)  # another device wants the name: defended at once

    def test_goodbye(self):
        self.r.close()
        ((m, to),) = self.sent.out
        self.assertEqual({(r[0], r[3]) for r in answers(m)}, {("baabaa.local", 0), ("ai.local", 0)})


class TestClaiming(unittest.TestCase):
    def claim(self, base="baabaa", other=None):
        """Claims `base`; `other(probe, responder)` plays another device that hears each probe."""
        r = mdns.Responder(IP, accept=lambda host: True)
        r.PROBE_GAP = 0.005
        r.transport = Sent(then=(lambda data: other(mdns.parse(data), r)) if other else None)
        return asyncio.run(r._claim(base)), r

    def test_free(self):
        name, r = self.claim()
        self.assertEqual(name, "baabaa.local")
        probes = [m for m, _ in r.transport.out]
        self.assertEqual(len(probes), 3)
        self.assertEqual(probes[0]["questions"], [("baabaa.local", mdns.ANY, mdns.IN)])
        self.assertEqual(answers(probes[0], "authority"), [("baabaa.local", mdns.A, mdns.IN, mdns.TTL, socket.inet_aton(IP))])

    def test_own_probes_heard_back(self):
        echo = lambda probe, r: r.datagram_received(mdns.message(questions=probe["questions"], flags=0, authority=[  # noqa: E731
            a_record("baabaa.local", IP, flush=False)]), (IP, mdns.PORT))
        self.assertEqual(self.claim(other=echo)[0], "baabaa.local")

    def test_taken(self):
        def owner(probe, r):  # a device that has baabaa.local answers
            if probe["questions"][0][0] == "baabaa.local":
                r.datagram_received(mdns.message(answers=[a_record("baabaa.local", "192.168.50.77")]), ("192.168.50.77", mdns.PORT))
        name, _ = self.claim(other=owner)
        self.assertEqual(name, "baabaa-2.local")

    def test_probing_at_the_same_time(self):
        def rival(ip):
            def probe_too(probe, r):
                name = probe["questions"][0][0]
                if name == "baabaa.local":
                    r.datagram_received(query((name, mdns.ANY, mdns.IN), authority=[a_record(name, ip, flush=False)]), (ip, mdns.PORT))
            return probe_too
        self.assertEqual(self.claim(other=rival("192.168.50.99"))[0], "baabaa-2.local")  # the later address wins
        self.assertEqual(self.claim(other=rival("192.168.50.5"))[0], "baabaa.local")

    def test_nothing_to_claim(self):
        self.assertEqual(asyncio.run(mdns.Responder(IP).start([])), [])


if __name__ == "__main__":
    unittest.main()
