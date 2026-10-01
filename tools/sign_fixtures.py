#!/usr/bin/env python3
"""Generate the pre-signed DNSSEC records the validation cases carry.

The fake upstream (runner/upstream.py) answers with literal RR strings and
does not sign anything.  A DNSSEC case therefore has its RRSIG / DNSKEY / DS /
NSEC records written out in the YAML, and this script is where they come
from.  It is not run by the test suite.

    uv run --with cryptography python tools/sign_fixtures.py > tools/fixtures.txt

Keys are ECDSA P-256 (algorithm 13) test keys kept in tools/keys/.  They
protect nothing: they sign names under example. that exist only inside the
harness.  If a key file is missing, a new key is generated and saved, and
every record derived from it changes, so the cases have to be updated too.

ECDSA signatures are randomised, so two runs print different RRSIGs.  Both
are valid; regenerate only when a record set changes.

Signatures are valid from 2026-09-01 to 2046-09-01, so the cases do not
start failing as time passes.  `expired` signatures cover 2020-01-01 to
2020-02-01.
"""

import pathlib

import dns.dnssec
import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.rrset
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KEYS = pathlib.Path(__file__).resolve().parent / "keys"
VALID = ("20260901000000", "20460901000000")
EXPIRED = ("20200101000000", "20200201000000")
TTL = 300


def load_key(label: str) -> ec.EllipticCurvePrivateKey:
    path = KEYS / f"{label}.pem"
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


class Zone:
    def __init__(self, origin: str, label: str):
        self.origin = dns.name.from_text(origin)
        self.key = load_key(label)
        self.dnskey = dns.dnssec.make_dnskey(
            self.key.public_key(), dns.dnssec.Algorithm.ECDSAP256SHA256, flags=257
        )

    def rrset(self, text: str) -> dns.rrset.RRset:
        """`"www 300 IN A 192.0.2.1"` lines sharing owner and type -> RRset."""
        lines = [ln.split() for ln in text.strip().splitlines()]
        name, _, rdclass, rdtype = lines[0][:4]
        return dns.rrset.from_text_list(
            name, TTL, rdclass, rdtype, [" ".join(ln[4:]) for ln in lines]
        )

    def sign(self, rrset: dns.rrset.RRset, window=VALID) -> dns.rrset.RRset:
        sig = dns.dnssec.sign(
            rrset,
            self.key,
            self.origin,
            self.dnskey,
            inception=window[0],
            expiration=window[1],
        )
        out = dns.rrset.RRset(rrset.name, rrset.rdclass, dns.rdatatype.RRSIG)
        out.add(sig, TTL)
        return out

    def dnskey_rrset(self) -> dns.rrset.RRset:
        out = dns.rrset.RRset(self.origin, dns.rdataclass.IN, dns.rdatatype.DNSKEY)
        out.add(self.dnskey, TTL)
        return out

    def ds_rrset(self) -> dns.rrset.RRset:
        out = dns.rrset.RRset(self.origin, dns.rdataclass.IN, dns.rdatatype.DS)
        out.add(dns.dnssec.make_ds(self.origin, self.dnskey, "SHA256"), TTL)
        return out


def show(title: str, *rrsets: dns.rrset.RRset) -> None:
    print(f"## {title}")
    for rrset in rrsets:
        for line in rrset.to_text().splitlines():
            print(line)
    print()


def main() -> None:
    signed = Zone("signed.example.", "signed.example")
    parent = Zone("example.", "example")
    child = Zone("corp.example.", "corp.example")

    # --- signed.example.: a zone that is its own trust anchor -------------
    show("signed.example. trust anchor (DS)", signed.ds_rrset())
    key = signed.dnskey_rrset()
    show("signed.example. DNSKEY", key, signed.sign(key))

    www = signed.rrset("www.signed.example. 300 IN A 192.0.2.80")
    show("www.signed.example. A (valid)", www, signed.sign(www))

    # What a correctly signed zone answers to "www.signed.example. DS": the
    # name exists, is not a delegation, and has no DS.
    ssoa = signed.rrset(
        "signed.example. 300 IN SOA ns.signed.example. host.signed.example. "
        "1 3600 900 604800 300"
    )
    show("signed.example. SOA", ssoa, signed.sign(ssoa))
    wnsec = signed.rrset("www.signed.example. 300 IN NSEC signed.example. A RRSIG NSEC")
    show("www.signed.example. NSEC (not a delegation)", wnsec, signed.sign(wnsec))

    # Tampered: the signature covers 192.0.2.81, the case serves 192.0.2.99.
    bad = signed.rrset("bad.signed.example. 300 IN A 192.0.2.81")
    show("bad.signed.example. A (signature is for 192.0.2.81)", signed.sign(bad))

    old = signed.rrset("expired.signed.example. 300 IN A 192.0.2.82")
    show("expired.signed.example. A (expired)", old, signed.sign(old, EXPIRED))

    # Two addresses, one private: private-address removes one RR, and the
    # signature then no longer matches what is left.
    mixed = signed.rrset(
        "mixed.signed.example. 300 IN A 192.0.2.83\n"
        "mixed.signed.example. 300 IN A 192.168.1.10"
    )
    show("mixed.signed.example. A (public + private)", mixed, signed.sign(mixed))

    # --- example.: the public parent of the internal zone corp.example. ---
    show("example. trust anchor (DS)", parent.ds_rrset())
    pkey = parent.dnskey_rrset()
    show("example. DNSKEY", pkey, parent.sign(pkey))

    soa = parent.rrset(
        "example. 300 IN SOA ns.example. host.example. 1 3600 900 604800 300"
    )
    show("example. SOA", soa, parent.sign(soa))

    # (a) The public side has a DS for corp.example.
    ds = child.ds_rrset()
    show("corp.example. DS in example. (public side has a DS)", ds, parent.sign(ds))

    # (b) The public side has an insecure delegation: no DS, proven by NSEC.
    deleg = parent.rrset("corp.example. 300 IN NSEC example. NS RRSIG NSEC")
    show("corp.example. NSEC (insecure delegation)", deleg, parent.sign(deleg))

    # (c) The public side does not have the name at all.
    apex = parent.rrset("example. 300 IN NSEC example. NS SOA RRSIG NSEC DNSKEY")
    show("example. NSEC (nothing below the apex)", apex, parent.sign(apex))

    # --- corp.example.: signed internally -----------------------------------
    ckey = child.dnskey_rrset()
    show("corp.example. DNSKEY", ckey, child.sign(ckey))
    cwww = child.rrset("www.corp.example. 300 IN A 192.0.2.90")
    show("www.corp.example. A (signed by corp.example.)", cwww, child.sign(cwww))


if __name__ == "__main__":
    main()
