"""
TLS Certificate Checker for Prism.

Checks SSL/TLS certificates on remote hosts and reports expiry status.
Uses only Python standard library (ssl, socket).
"""

import logging
import socket
import ssl
from datetime import datetime, timezone

logger = logging.getLogger("prism.tls_checker")


def _parse_cert_name(cert_dict, field):
    """Extract the CN (Common Name) from a certificate subject or issuer tuple."""
    for rdn in cert_dict.get(field, ()):
        for attr_type, attr_value in rdn:
            if attr_type == "commonName":
                return attr_value
    return ""


def _fetch_cert(host, port, timeout, verify=True):
    """Connect to host and return the peer certificate dict."""
    if verify:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            return ssock.getpeercert(binary_form=not verify)


def check_certificate(host, port=443, timeout=10, expiry_threshold=30):
    """Check the TLS certificate for a given host.

    Args:
        host: Hostname or IP to connect to.
        port: TCP port (default 443).
        timeout: Connection timeout in seconds.
        expiry_threshold: Days remaining below which status is "expiring".

    Returns:
        dict with keys: subject, issuer, not_before, not_after,
        days_remaining, status, error.
    """
    result = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "days_remaining": 0,
        "status": "error",
        "error": None,
    }

    cert = None
    # First try with full verification
    try:
        logger.debug("Connecting to %s:%d with certificate verification", host, port)
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        # Likely self-signed or untrusted CA — retry without verification
        logger.warning(
            "Certificate verification failed for %s:%d (%s), retrying without verification",
            host, port, exc,
        )
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    # getpeercert() returns empty dict when CERT_NONE, need binary
                    der = ssock.getpeercert(binary_form=True)
                    # Re-parse via load_der_x509_certificate equivalent using ssl
                    cert = ssl.DER_cert_to_PEM_cert(der)
                    # Decode the DER cert to get structured data
                    # We need to reconnect or parse the DER ourselves.
                    # Use ssl._ssl._test_decode_cert workaround is not portable.
                    # Instead, parse the PEM through a temporary context.
                    import tempfile
                    import os

                    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pem")
                    try:
                        with os.fdopen(tmp_fd, "w") as f:
                            f.write(cert)
                        # Use undocumented but widely available helper
                        cert = ssl._ssl._test_decode_cert(tmp_path)
                    finally:
                        os.unlink(tmp_path)
        except Exception as inner_exc:
            logger.error("Failed to retrieve certificate from %s:%d: %s", host, port, inner_exc)
            result["error"] = str(inner_exc)
            return result
    except (socket.timeout, socket.gaierror, ConnectionRefusedError, OSError) as exc:
        logger.error("Connection to %s:%d failed: %s", host, port, exc)
        result["error"] = str(exc)
        return result
    except Exception as exc:
        logger.error("Unexpected error checking %s:%d: %s", host, port, exc)
        result["error"] = str(exc)
        return result

    if not cert:
        result["error"] = "No certificate returned"
        return result

    # Parse certificate fields
    try:
        result["subject"] = _parse_cert_name(cert, "subject")
        result["issuer"] = _parse_cert_name(cert, "issuer")

        not_before_str = cert.get("notBefore", "")
        not_after_str = cert.get("notAfter", "")

        # Python ssl returns dates like 'Jun 11 00:00:00 2025 GMT'
        date_fmt = "%b %d %H:%M:%S %Y %Z"

        not_before = datetime.strptime(not_before_str, date_fmt).replace(tzinfo=timezone.utc)
        not_after = datetime.strptime(not_after_str, date_fmt).replace(tzinfo=timezone.utc)

        result["not_before"] = not_before.isoformat()
        result["not_after"] = not_after.isoformat()

        now = datetime.now(timezone.utc)
        delta = not_after - now
        result["days_remaining"] = delta.days

        if delta.days < 0:
            result["status"] = "expired"
        elif delta.days < expiry_threshold:
            result["status"] = "expiring"
        else:
            result["status"] = "valid"

        logger.info(
            "Certificate for %s:%d — subject=%s, status=%s, days_remaining=%d",
            host, port, result["subject"], result["status"], result["days_remaining"],
        )
    except Exception as exc:
        logger.error("Error parsing certificate from %s:%d: %s", host, port, exc)
        result["error"] = str(exc)
        result["status"] = "error"

    return result


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.DEBUG)
    target = sys.argv[1] if len(sys.argv) > 1 else "google.com"
    info = check_certificate(target)
    print(json.dumps(info, indent=2))
