"""Shared checks for public network destinations and redirect-free HTTP."""
import ipaddress
import urllib.request


def is_public_ip(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in ipaddress.ip_network("2001::/32"):
            return False  # Teredo can route to an untrusted embedded endpoint.
        if ip.ipv4_mapped:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour:
            return is_public_ip(ip.sixtofour)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xffffffff))
        if ip in ipaddress.ip_network("64:ff9b:1::/48"):
            # RFC 6052 /48: IPv4 occupies bits 48..63 and 72..87 (u octet skipped).
            embedded = ((int(ip) >> 64) & 0xffff) << 16 | ((int(ip) >> 40) & 0xffff)
            return is_public_ip(ipaddress.IPv4Address(embedded))
    return (ip.is_global and not ip.is_private and not ip.is_loopback
            and not ip.is_link_local and not ip.is_multicast
            and not ip.is_reserved and not ip.is_unspecified)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = urllib.request.build_opener(NoRedirect()).open
