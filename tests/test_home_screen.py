"""Home-screen metadata is exercised through the actual static HTTP routes."""

import struct
import unittest
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from dayplan.api import app


class HeadTags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = {}
        self.meta = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link":
            self.links[attrs.get("rel")] = attrs
        elif tag == "meta":
            self.meta[attrs.get("name")] = attrs.get("content")


class HomeScreenTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.tags = HeadTags(self.client.get("/").text)

    def test_manifest_declares_same_origin_standalone_app(self):
        self.assertIn("manifest", self.tags.links)
        response = self.client.get(self.tags.links["manifest"]["href"])
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest["name"], "dayplan")
        self.assertEqual(manifest["short_name"], "dayplan")
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def assert_png(self, url, size):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", response.content[16:24]), (size, size))

    def test_manifest_icons_are_real_pngs(self):
        self.assertIn("manifest", self.tags.links)
        manifest = self.client.get(self.tags.links["manifest"]["href"]).json()
        sizes = set()
        for icon in manifest["icons"]:
            size = int(icon["sizes"].split("x")[0])
            sizes.add(size)
            self.assertEqual(icon["type"], "image/png")
            self.assert_png(icon["src"], size)
        self.assertTrue({192, 512}.issubset(sizes))

    def test_ios_uses_raster_touch_icon_and_standalone_metadata(self):
        self.assertIn("apple-touch-icon", self.tags.links)
        self.assert_png(self.tags.links["apple-touch-icon"]["href"], 180)
        self.assertEqual(self.tags.meta["apple-mobile-web-app-capable"], "yes")
        self.assertEqual(self.tags.meta["apple-mobile-web-app-title"], "dayplan")
        self.assertEqual(self.tags.meta["apple-mobile-web-app-status-bar-style"], "default")
        self.assertIn("viewport-fit=cover", self.tags.meta["viewport"])

    def test_document_and_unversioned_icons_revalidate(self):
        self.assertEqual(self.client.get("/").headers["cache-control"], "no-cache")
        self.assertIn("apple-touch-icon", self.tags.links)
        response = self.client.get(self.tags.links["apple-touch-icon"]["href"])
        self.assertEqual(response.headers["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
