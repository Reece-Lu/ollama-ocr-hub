import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import proxy


class ClientIpTests(unittest.TestCase):
    def test_direct_peer_is_used_by_default(self):
        old = proxy.TRUST_PROXY_HEADERS
        proxy.TRUST_PROXY_HEADERS = False
        try:
            request = SimpleNamespace(
                client=SimpleNamespace(host="::ffff:192.168.3.12"),
                headers={"x-forwarded-for": "203.0.113.10"},
            )
            self.assertEqual(proxy.get_client_ip(request), "192.168.3.12")
        finally:
            proxy.TRUST_PROXY_HEADERS = old

    def test_first_forwarded_address_is_used_when_enabled(self):
        old = proxy.TRUST_PROXY_HEADERS
        proxy.TRUST_PROXY_HEADERS = True
        try:
            request = SimpleNamespace(
                client=SimpleNamespace(host="192.168.65.1"),
                headers={"x-forwarded-for": "192.168.3.21, 127.0.0.1"},
            )
            self.assertEqual(proxy.get_client_ip(request), "192.168.3.21")
        finally:
            proxy.TRUST_PROXY_HEADERS = old


if __name__ == "__main__":
    unittest.main()
