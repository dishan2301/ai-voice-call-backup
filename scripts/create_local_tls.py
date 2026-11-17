"""Create a private development CA and HTTPS certificate for local phone testing.

Run once with --host LAN_IP. Install only the CA certificate on your own phone.
Existing credentials are never overwritten. For public hosting use managed HTTPS.
"""
import argparse
import ipaddress
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=ipaddress.ip_address)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / 'secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    paths = [directory / name for name in ('local-ca.key', 'local-ca.crt', 'local-ca.cer', 'local-server.key', 'local-server.crt')]
    if any(path.exists() for path in paths):
        raise SystemExit('Local TLS files already exist. They have not been overwritten.')
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Morning Mail local development CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(args.host))])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(args.host),
                 x509.IPAddress(ipaddress.ip_address('127.0.0.1')), x509.DNSName('localhost')]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    blobs = [ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
             ca.public_bytes(serialization.Encoding.PEM), ca.public_bytes(serialization.Encoding.DER),
             key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
             cert.public_bytes(serialization.Encoding.PEM)]
    for path, blob in zip(paths, blobs):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle: handle.write(blob)
    print('Created local development certificates under secrets/. Install local-ca.cer only on your own test phone.')


if __name__ == '__main__': main()
