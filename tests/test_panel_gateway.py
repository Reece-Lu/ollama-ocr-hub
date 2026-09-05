import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import panel_gateway


class PanelGatewayTests(unittest.TestCase):
    def test_real_peer_overwrites_spoofed_forwarded_ip(self):
        connection = SimpleNamespace(
            client=SimpleNamespace(host="192.168.3.42"),
            headers={
                "host": "192.168.3.20:8501",
                "connection": "upgrade",
                "x-forwarded-for": "8.8.8.8",
            },
            url=SimpleNamespace(scheme="http"),
        )

        headers = panel_gateway._forward_headers(connection)

        self.assertEqual(headers["x-forwarded-for"], "192.168.3.42")
        self.assertEqual(headers["x-real-ip"], "192.168.3.42")
        self.assertEqual(headers["x-forwarded-host"], "192.168.3.20:8501")
        self.assertNotIn("connection", headers)


if __name__ == "__main__":
    unittest.main()
