#!/usr/bin/env python3
"""Certificates and an iTAK data package for the TAK add-on's encrypted TAK server (tak_link.py).

    python tak/tak_certs.py --host 172.20.10.4                    # the laptop's IP on the hotspot / router
    python tak/tak_stack.py --config code/park.toml               # then serves TLS on 8089 with these

iTAK won't save a plain-TCP server: on Save it enrolls for a certificate (TLS to port 8446) and gives up
when there's no real TAK server. So we act like one: a tiny certificate authority on this laptop signs
the server's certificate and the phone's, and a data package (a .zip iTAK imports) carries the phone's
certificate, the CA to trust and the server address ("<host>:8089:ssl"). Import it in iTAK and the
server is added with nothing to type and no enrollment.

Writes to [out] (default tak/runs, which git ignores: runs/ in .gitignore):
    ca.pem, ca.key                  the certificate authority (keep ca.key private)
    server.pem, server.key          for the TLS server (tak_link / tak_stack)
    share/AFTERGLOW-iTAK.zip        the data package for the phone: truststore.p12, itak.p12, config.pref
Only the share/ folder is meant to leave this laptop. The .p12 password is in config.pref inside the
package, as TAK packages do, so treat the zip like a key: send it only to your own phone.
"""
import argparse
import datetime as dt
import ipaddress
import uuid
import zipfile
from pathlib import Path

TAK = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", action="append", required=True,
                    help="address the phone uses to reach this laptop (repeatable): an IP or a hostname")
    ap.add_argument("--port", type=int, default=8089, help="TLS port in the package (default 8089, TAK's usual)")
    ap.add_argument("--out", default=str(TAK / "runs"))
    ap.add_argument("--password", default="atakatak", help="for the .p12 files (TAK's usual default)")
    ap.add_argument("--name", default="AFTERGLOW", help="server name shown in iTAK")
    a = ap.parse_args()

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    out = Path(a.out)
    (out / "share").mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AFTERGLOW"),
                          x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def cert(subject, pub, issuer, issuer_key, days, ca=False, eku=None, san=None):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        return b.sign(issuer_key, hashes.SHA256())

    ca_key = key()
    ca = cert(name("AFTERGLOW local CA"), ca_key.public_key(), name("AFTERGLOW local CA"), ca_key, 3650, ca=True)
    san = []
    for h in a.host:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san.append(x509.DNSName(h))
    srv_key = key()
    srv = cert(name(a.host[0]), srv_key.public_key(), ca.subject, ca_key, 825,
               eku=[ExtendedKeyUsageOID.SERVER_AUTH], san=san)
    cli_key = key()
    cli = cert(name("itak"), cli_key.public_key(), ca.subject, ca_key, 825, eku=[ExtendedKeyUsageOID.CLIENT_AUTH])

    pem = serialization.Encoding.PEM
    nokey = serialization.NoEncryption()
    raw = serialization.PrivateFormat.TraditionalOpenSSL
    (out / "ca.pem").write_bytes(ca.public_bytes(pem))
    (out / "ca.key").write_bytes(ca_key.private_bytes(pem, raw, nokey))
    (out / "server.pem").write_bytes(srv.public_bytes(pem) + ca.public_bytes(pem))
    (out / "server.key").write_bytes(srv_key.private_bytes(pem, raw, nokey))

    # iOS reads .p12 files with the old SHA1 / 3DES algorithms more reliably than the modern AES default
    enc = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
           .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC).hmac_hash(hashes.SHA1())
           .build(a.password.encode()))
    trust = pkcs12.serialize_key_and_certificates(b"afterglow-ca", None, None, [ca], enc)
    client = pkcs12.serialize_key_and_certificates(b"itak", cli_key, cli, [ca], enc)

    host = a.host[0]
    pref = f"""<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
  <preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{a.name}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{host}:{a.port}:ssl</entry>
    <entry key="caLocation0" class="class java.lang.String">cert/truststore.p12</entry>
    <entry key="caPassword0" class="class java.lang.String">{a.password}</entry>
    <entry key="certificateLocation0" class="class java.lang.String">cert/itak.p12</entry>
    <entry key="clientPassword0" class="class java.lang.String">{a.password}</entry>
    <entry key="enrollForCertificateWithTrust0" class="class java.lang.Boolean">false</entry>
    <entry key="useAuth0" class="class java.lang.Boolean">false</entry>
  </preference>
  <preference version="1" name="com.atakmap.app_preferences">
    <entry key="displayServerConnectionWidget" class="class java.lang.Boolean">true</entry>
    <entry key="caLocation" class="class java.lang.String">cert/truststore.p12</entry>
    <entry key="caPassword" class="class java.lang.String">{a.password}</entry>
    <entry key="certificateLocation" class="class java.lang.String">cert/itak.p12</entry>
    <entry key="clientPassword" class="class java.lang.String">{a.password}</entry>
  </preference>
</preferences>
"""
    manifest = f"""<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uuid.uuid4()}"/>
    <Parameter name="name" value="{a.name}-iTAK"/>
    <Parameter name="onReceiveDelete" value="true"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="config.pref"/>
    <Content ignore="false" zipEntry="truststore.p12"/>
    <Content ignore="false" zipEntry="itak.p12"/>
  </Contents>
</MissionPackageManifest>
"""
    pkg = out / "share" / f"{a.name}-iTAK.zip"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST/manifest.xml", manifest)
        z.writestr("config.pref", pref)
        z.writestr("truststore.p12", trust)
        z.writestr("itak.p12", client)
    print(f"server cert for {', '.join(a.host)} and CA in {out}")
    print(f"iTAK data package: {pkg}  (server {host}:{a.port}, SSL)")


if __name__ == "__main__":
    main()
