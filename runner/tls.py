"""Issue the throwaway certificates a TLS-terminating fake upstream needs.

A case that gives an upstream `tls: {name: ...}` wants Unbound's upstream TLS
(`forward-tls-upstream`) to complete a handshake, so that what Unbound does
with the certificate can be observed: accepted, or refused because the name
or the CA does not match.

Everything is made per case with the `openssl` command, in the case's
temporary directory:

* `harness-ca.pem` -- the CA certificate, next to unbound.conf, so a case
  writes `tls-cert-bundle: "harness-ca.pem"` to trust it, or leaves the line
  out to see what Unbound does without a CA.
* `_tls/<name>.pem` / `_tls/<name>.key` -- the server certificate and key for
  the name the case asked for, with that name as its subjectAltName.

Nothing here is a secret worth keeping; the directory is deleted with the
case.
"""

import pathlib
import ssl
import subprocess

CA_FILE = "harness-ca.pem"
_EC = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]


def _openssl(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(
        ["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60
    )


def make_ca(tmpdir: pathlib.Path) -> None:
    """Write the CA certificate to tmpdir and its key to tmpdir/_tls."""
    (tmpdir / "_tls").mkdir(exist_ok=True)
    if (tmpdir / CA_FILE).exists():
        return
    _openssl(
        "req",
        "-x509",
        *_EC,
        "-keyout",
        "_tls/ca.key",
        "-out",
        CA_FILE,
        "-subj",
        "/CN=unbound-behavior-tests throwaway CA",
        "-days",
        "2",
        cwd=tmpdir,
    )


def server_context(tmpdir: pathlib.Path, name: str) -> ssl.SSLContext:
    """A server-side TLS context with a certificate for `name`."""
    make_ca(tmpdir)
    ext = tmpdir / "_tls" / f"{name}.ext"
    ext.write_text(f"subjectAltName=DNS:{name}\n", encoding="utf-8")
    _openssl(
        "req",
        *_EC,
        "-keyout",
        f"_tls/{name}.key",
        "-out",
        f"_tls/{name}.csr",
        "-subj",
        f"/CN={name}",
        cwd=tmpdir,
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        f"_tls/{name}.csr",
        "-CA",
        CA_FILE,
        "-CAkey",
        "_tls/ca.key",
        "-CAcreateserial",
        "-out",
        f"_tls/{name}.pem",
        "-days",
        "2",
        "-extfile",
        f"_tls/{name}.ext",
        cwd=tmpdir,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        tmpdir / "_tls" / f"{name}.pem", tmpdir / "_tls" / f"{name}.key"
    )
    return ctx
