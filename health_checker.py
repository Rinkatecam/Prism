"""Synthetic health check module for Prism.

Provides TCP and HTTP probes for monitoring server availability
using only Python standard library modules.
"""

import logging
import socket
import ssl
import time
import urllib.request

logger = logging.getLogger("prism.health_checker")


def tcp_probe(host, port, timeout=5):
    """Probe a TCP port and return availability status.

    Args:
        host: Hostname or IP address to connect to.
        port: TCP port number.
        timeout: Connection timeout in seconds.

    Returns:
        dict with keys: status, response_time_ms, error.
    """
    start = time.perf_counter()
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000
        conn.close()
        logger.debug("TCP probe %s:%d succeeded in %.1f ms", host, port, elapsed_ms)
        return {"status": "up", "response_time_ms": round(elapsed_ms, 2), "error": None}
    except socket.timeout:
        elapsed_ms = (time.perf_counter() - start) * 1000
        msg = f"Connection timed out after {timeout}s"
        logger.warning("TCP probe %s:%d timed out", host, port)
        return {"status": "down", "response_time_ms": round(elapsed_ms, 2), "error": msg}
    except ConnectionRefusedError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        msg = "Connection refused"
        logger.warning("TCP probe %s:%d refused", host, port)
        return {"status": "down", "response_time_ms": round(elapsed_ms, 2), "error": msg}
    except OSError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        msg = str(exc)
        logger.error("TCP probe %s:%d failed: %s", host, port, msg)
        return {"status": "down", "response_time_ms": round(elapsed_ms, 2), "error": msg}


def _ssl_context(verify_tls: bool = True) -> ssl.SSLContext:
    """The TLS context for an HTTPS health check.

    `verify_tls=True` is the default and validates the chain AND the hostname.
    Anything less proves that something answered on the port, not that it was
    the service you meant: a mis-issued or expired certificate, or a
    machine-in-the-middle, all read as a clean "up".

    `verify_tls=False` exists because internal endpoints with self-signed
    certificates are ordinary, and a monitor that turns a wave of them red is
    a monitor people switch off. It is set per check, in a row, by an operator
    who knows which endpoint it is — which is the difference between this and
    the unconditional `CERT_NONE` it replaces.

    ORDER MATTERS on the disable path: `check_hostname` must be cleared BEFORE
    `verify_mode` is lowered. Python raises `ValueError` if a context still
    requires hostname checking when verification is switched off, and the two
    lines look independent.
    """
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_check(host, port, path="/", use_ssl=False, expected_status=200,
               timeout=10, verify_tls=True):
    """Probe an HTTP(S) endpoint. Wrapper — see `_http_check` for the body.

    `tls_verified` is stamped onto the result HERE, in one place, rather than
    into each of the six return dicts in `_http_check`. Adding a key to five of
    six returns is precisely the "the rule was updated, the carriers were not"
    failure this repository keeps producing, and the one that would go
    unnoticed is an error path — which is exactly where a caller most wants to
    know whether the certificate was checked.

    `None` rather than `False` for a plain HTTP check: there was no certificate
    to verify, which is a different statement from "there was one and we did
    not look at it". This line is the only place that distinction is made —
    an earlier version also computed it inside `_http_check`, where nothing
    read it, so the comment explaining the rule sat on dead code while the
    live rule was here.
    """
    result = _http_check(host, port, path=path, use_ssl=use_ssl,
                         expected_status=expected_status, timeout=timeout,
                         verify_tls=verify_tls)
    result["tls_verified"] = bool(verify_tls) if use_ssl else None
    return result


def _http_check(host, port, path="/", use_ssl=False, expected_status=200,
                timeout=10, verify_tls=True):
    """Perform an HTTP health check and return status details.

    Args:
        host: Hostname or IP address.
        port: HTTP port number.
        path: URL path to request.
        use_ssl: Whether to use HTTPS.
        expected_status: HTTP status code that indicates a healthy service.
        timeout: Request timeout in seconds.

    Returns:
        dict with keys: status, response_time_ms, http_status, error.
    """
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}{path}"

    ctx = _ssl_context(verify_tls) if use_ssl else None

    start = time.perf_counter()
    try:
        response = urllib.request.urlopen(url, timeout=timeout, context=ctx)
        elapsed_ms = (time.perf_counter() - start) * 1000
        status_code = response.status
        response.close()

        if status_code == expected_status:
            logger.debug("HTTP check %s succeeded in %.1f ms (status %d)", url, elapsed_ms, status_code)
            return {
                "status": "up",
                "response_time_ms": round(elapsed_ms, 2),
                "http_status": status_code,
                "error": None,
            }
        else:
            msg = f"Expected status {expected_status}, got {status_code}"
            logger.warning("HTTP check %s: %s", url, msg)
            return {
                "status": "down",
                "response_time_ms": round(elapsed_ms, 2),
                "http_status": status_code,
                "error": msg,
            }
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        status_code = exc.code
        if status_code == expected_status:
            logger.debug("HTTP check %s matched expected status %d in %.1f ms", url, expected_status, elapsed_ms)
            return {
                "status": "up",
                "response_time_ms": round(elapsed_ms, 2),
                "http_status": status_code,
                "error": None,
            }
        msg = f"HTTP {status_code}: {exc.reason}"
        logger.warning("HTTP check %s failed: %s", url, msg)
        return {
            "status": "down",
            "response_time_ms": round(elapsed_ms, 2),
            "http_status": status_code,
            "error": msg,
        }
    except urllib.error.URLError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        msg = str(exc.reason)
        logger.error("HTTP check %s failed: %s", url, msg)
        return {
            "status": "down",
            "response_time_ms": round(elapsed_ms, 2),
            "http_status": None,
            "error": msg,
        }
    except OSError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        msg = str(exc)
        logger.error("HTTP check %s failed: %s", url, msg)
        return {
            "status": "down",
            "response_time_ms": round(elapsed_ms, 2),
            "http_status": None,
            "error": msg,
        }


def udp_probe(host, port, timeout=5):
    """Test if a UDP port responds. Sends an empty datagram and waits for response or ICMP unreachable."""
    start = time.perf_counter()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(b'', (host, port))
        try:
            sock.recvfrom(1024)
            elapsed = (time.perf_counter() - start) * 1000
            return {"status": "up", "response_time_ms": round(elapsed, 1), "error": None}
        except socket.timeout:
            # UDP timeout could mean port is open (no response) or filtered
            elapsed = (time.perf_counter() - start) * 1000
            return {"status": "up", "response_time_ms": round(elapsed, 1), "error": "No response (open|filtered)"}
        except ConnectionRefusedError:
            return {"status": "down", "response_time_ms": 0, "error": "Connection refused (port closed)"}
    except Exception as e:
        return {"status": "down", "response_time_ms": 0, "error": str(e)}
    finally:
        if sock:
            sock.close()


def icmp_ping(host, timeout=5):
    """Ping a host using OS ping command (ICMP requires raw sockets which need admin)."""
    import subprocess
    import platform

    start = time.perf_counter()
    try:
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        wait_val = str(timeout * 1000 if platform.system().lower() == 'windows' else timeout)
        cmd = ['ping', param, '1', '-w', wait_val, host]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        elapsed = (time.perf_counter() - start) * 1000
        if result.returncode == 0:
            return {"status": "up", "response_time_ms": round(elapsed, 1), "error": None}
        else:
            return {"status": "down", "response_time_ms": round(elapsed, 1), "error": "Ping failed"}
    except subprocess.TimeoutExpired:
        return {"status": "down", "response_time_ms": timeout * 1000, "error": "Ping timeout"}
    except Exception as e:
        return {"status": "down", "response_time_ms": 0, "error": str(e)}
