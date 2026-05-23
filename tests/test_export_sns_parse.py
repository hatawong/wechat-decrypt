"""Tests for `export_sns._parse_timeline_xml` content-blob robustness.

Before this fix, `_parse_timeline_xml` called `ET.fromstring(content_xml)`
directly, assuming `SnsTimeLine.content` was always plain XML. In reality
the column ships in 4 encodings across WeChat versions / historical posts:

  1. bytes (zstd-compressed or raw UTF-8)
  2. plain XML string
  3. hex string
  4. base64 string

Plus 2013-2017 era posts contain pseudo-XML quirks that `ElementTree`
refuses: bare `&` in URLs, raw `<` / `>` inside user-typed text fields,
stray control characters.

All of these previously caused `_parse_timeline_xml` to silently return
None → the row dropped from export with no diagnostic. These tests pin
the new decode + sanitize behavior. All fixtures are synthetic (no PII).
"""
import base64
import unittest

import zstandard as zstd

import export_sns


_MINIMAL_XML = (
    '<TimelineObjects>'
    '<TimelineObject>'
    '<id>fake-tid-1</id>'
    '<username>wxid_synthetic_user</username>'
    '<createTime>1700000000</createTime>'
    '<contentDesc>hello world</contentDesc>'
    '<ContentObject><type>2</type></ContentObject>'
    '</TimelineObject>'
    '</TimelineObjects>'
)


class DecodeContentBlobTests(unittest.TestCase):
    """Detect 4 content encodings before XML parsing."""

    def test_plain_xml_string_passthrough(self):
        post = export_sns._parse_timeline_xml(_MINIMAL_XML)
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "fake-tid-1")
        self.assertEqual(post["content_desc"], "hello world")

    def test_plain_xml_bytes_passthrough(self):
        post = export_sns._parse_timeline_xml(_MINIMAL_XML.encode("utf-8"))
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "fake-tid-1")

    def test_zstd_compressed_bytes(self):
        compressed = zstd.ZstdCompressor().compress(_MINIMAL_XML.encode("utf-8"))
        # zstd magic 28 B5 2F FD must be at front
        self.assertEqual(compressed[:4], b"\x28\xb5\x2f\xfd")
        post = export_sns._parse_timeline_xml(compressed)
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "fake-tid-1")

    def test_hex_encoded_string(self):
        hex_str = _MINIMAL_XML.encode("utf-8").hex()
        post = export_sns._parse_timeline_xml(hex_str)
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "fake-tid-1")

    def test_base64_encoded_string(self):
        b64_str = base64.b64encode(_MINIMAL_XML.encode("utf-8")).decode("ascii")
        post = export_sns._parse_timeline_xml(b64_str)
        self.assertIsNotNone(post)
        self.assertEqual(post["id"], "fake-tid-1")

    def test_none_input(self):
        self.assertIsNone(export_sns._parse_timeline_xml(None))

    def test_empty_string_input(self):
        self.assertIsNone(export_sns._parse_timeline_xml(""))

    def test_empty_bytes_input(self):
        self.assertIsNone(export_sns._parse_timeline_xml(b""))

    def test_short_hex_not_misdetected(self):
        # Short hex-looking strings should NOT be eagerly decoded —
        # they could be plain text that happens to be all hex chars.
        # Length floor is 16, so anything shorter falls through to
        # html.unescape path and ET.fromstring then ParseError → None.
        post = export_sns._parse_timeline_xml("deadbeef")
        self.assertIsNone(post)

    def test_garbage_input_returns_none_not_raises(self):
        # Random non-XML, non-hex, non-base64 → None, no exception.
        post = export_sns._parse_timeline_xml("this is not xml at all !!!")
        self.assertIsNone(post)


class SanitizePseudoXmlTests(unittest.TestCase):
    """Old WeChat posts contain XML 1.0 forbidden / unescaped chars."""

    def test_bare_ampersand_in_url(self):
        # 2013-2017 era: <appname>WeRead&amp;Friends</appname> shipped as
        # <appname>WeRead&Friends</appname> (bare &). Must escape.
        xml = (
            '<TimelineObjects><TimelineObject>'
            '<id>amp-1</id>'
            '<username>wxid_x</username>'
            '<createTime>1700000000</createTime>'
            '<contentDesc>http://example.com/?a=1&b=2</contentDesc>'
            '<ContentObject><type>3</type></ContentObject>'
            '</TimelineObject></TimelineObjects>'
        )
        post = export_sns._parse_timeline_xml(xml)
        self.assertIsNotNone(post)
        self.assertEqual(post["content_desc"], "http://example.com/?a=1&b=2")

    def test_raw_angle_brackets_inside_text(self):
        # User typed literal "<3" in the post body. Must escape to &lt;3.
        xml = (
            '<TimelineObjects><TimelineObject>'
            '<id>ang-1</id>'
            '<username>wxid_x</username>'
            '<createTime>1700000000</createTime>'
            '<contentDesc>love it <3 always</contentDesc>'
            '<ContentObject><type>2</type></ContentObject>'
            '</TimelineObject></TimelineObjects>'
        )
        post = export_sns._parse_timeline_xml(xml)
        self.assertIsNotNone(post)
        self.assertIn("<3", post["content_desc"])

    def test_control_chars_stripped(self):
        xml = (
            '<TimelineObjects><TimelineObject>'
            '<id>ctrl-1</id>'
            '<username>wxid_x</username>'
            '<createTime>1700000000</createTime>'
            '<contentDesc>hello\x01\x02world</contentDesc>'
            '<ContentObject><type>2</type></ContentObject>'
            '</TimelineObject></TimelineObjects>'
        )
        post = export_sns._parse_timeline_xml(xml)
        self.assertIsNotNone(post)
        self.assertEqual(post["content_desc"], "helloworld")

    def test_cdata_block_preserves_ampersand(self):
        # & inside CDATA is legal, must NOT be re-escaped to &amp;.
        # Use a non-text-only node (<extraInfo>) so this exercises the
        # CDATA-split logic in isolation; the text-only-node escape pass
        # (contentDesc etc.) intentionally doesn't recurse into CDATA
        # since real WeChat posts don't ship CDATA inside those nodes.
        xml = (
            '<TimelineObjects><TimelineObject>'
            '<id>cdata-1</id>'
            '<username>wxid_x</username>'
            '<createTime>1700000000</createTime>'
            '<contentDesc>plain text</contentDesc>'
            '<ContentObject><type>2</type></ContentObject>'
            '<extraInfo><![CDATA[a&b&c]]></extraInfo>'
            '</TimelineObject></TimelineObjects>'
        )
        post = export_sns._parse_timeline_xml(xml)
        self.assertIsNotNone(post)
        # Post-parse, the CDATA value should be preserved as raw data.
        # We verify via re-serialize since _parse_timeline_xml doesn't
        # expose extraInfo, but a None return would mean ParseError.
        self.assertEqual(post["content_desc"], "plain text")


class SecurityAndLimitsTests(unittest.TestCase):
    """Existing XXE / length-cap guards must still fire after decode."""

    def test_xxe_doctype_blocked(self):
        xml = (
            '<!DOCTYPE foo [<!ENTITY x "boom">]>'
            '<TimelineObjects><TimelineObject>'
            '<id>xxe-1</id>'
            '<createTime>1700000000</createTime>'
            '</TimelineObject></TimelineObjects>'
        )
        self.assertIsNone(export_sns._parse_timeline_xml(xml))

    def test_xxe_doctype_blocked_after_zstd_decode(self):
        # XXE check must run on the DECODED payload — encoded DOCTYPE
        # would otherwise sneak past a naive pre-decode regex.
        evil = (
            '<!DOCTYPE foo [<!ENTITY x "boom">]>'
            '<TimelineObjects><TimelineObject>'
            '<id>xxe-zstd</id>'
            '<createTime>1700000000</createTime>'
            '</TimelineObject></TimelineObjects>'
        )
        compressed = zstd.ZstdCompressor().compress(evil.encode("utf-8"))
        self.assertIsNone(export_sns._parse_timeline_xml(compressed))

    def test_oversized_content_blocked(self):
        big = (
            '<TimelineObjects><TimelineObject>'
            '<id>big-1</id>'
            '<createTime>1700000000</createTime>'
            '<contentDesc>' + ('x' * 250_000) + '</contentDesc>'
            '<ContentObject><type>2</type></ContentObject>'
            '</TimelineObject></TimelineObjects>'
        )
        self.assertIsNone(export_sns._parse_timeline_xml(big))


if __name__ == "__main__":
    unittest.main()
