import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("core.utils.security")

BLOCKED_HOSTNAMES: set[str] = {
    "metadata.google.internal",
    "metadata.internal",
    "instance-data",
}

HEX_COLOR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def sanitize_css_color(color_val: str | None, default: str = "#00F2FE") -> str:
    """Validates and returns safe hex color string, preventing style tag escapes and CSS injection."""
    if not color_val or not isinstance(color_val, str):
        return default
    cleaned = color_val.strip()
    if HEX_COLOR_PATTERN.match(cleaned):
        return cleaned
    return default


def is_ip_prohibited(ip_str: str, allow_private: bool = False) -> bool:
    """Checks whether an IP address is loopback, link-local, multicast, or private."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    if ip.is_loopback:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_reserved:
        return True
    if ip.is_unspecified:
        return True

    # Cloud metadata link-local address (169.254.169.254)
    if str(ip) == "169.254.169.254":
        return True

    if not allow_private and ip.is_private:
        return True

    return False


def validate_url_target(url: str, allow_private: bool = False) -> tuple[bool, str | None]:
    """
    Validates that a URL is safe to request:
    - Scheme must be http or https.
    - Hostname must not be a cloud metadata endpoint.
    - Resolved IP must not be loopback, link-local (169.254.169.254), or unauthorized private.
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string."

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL syntax: {e}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Prohibited URL scheme '{parsed.scheme}'. Only 'http' and 'https' are allowed."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL hostname is missing."

    hostname_lower = hostname.lower()
    if hostname_lower in BLOCKED_HOSTNAMES:
        return False, f"Access to internal metadata service '{hostname}' is blocked."

    # Check literal IP address
    try:
        if is_ip_prohibited(hostname_lower, allow_private=allow_private):
            return False, f"Access to restricted IP address '{hostname_lower}' is blocked."
        return True, None
    except ValueError:
        pass

    # Resolve domain to IP
    try:
        addr_info = socket.getaddrinfo(hostname_lower, None)
        for entry in addr_info:
            ip_str = entry[4][0]
            if is_ip_prohibited(ip_str, allow_private=allow_private):
                return False, f"Resolved IP '{ip_str}' for host '{hostname}' is restricted."
    except socket.gaierror:
        pass
    except Exception as e:
        logger.warning("DNS resolution check failed for %s: %s", hostname, e)

    return True, None


async def safe_redirect_hook(response: httpx.Response, allow_private: bool = False) -> None:
    """HTTPX response event hook that validates redirect targets against SSRF."""
    if response.is_redirect and "location" in response.headers:
        redirect_url = str(response.url.join(response.headers["location"]))
        valid, reason = validate_url_target(redirect_url, allow_private=allow_private)
        if not valid:
            raise ValueError(f"SSRF protection blocked redirect to: {redirect_url} ({reason})")
