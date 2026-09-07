import unittest

from iptvprovider import m3u

SAMPLE = """\ufeff#EXTM3U name="Example TV" url-tvg="https://a/epg.xml,https://b/x.xml"
#EXTM3U x-tvg-url="https://c/third.xml"
#UNLOOP-AUTH:1 url="https://upstream/unloop" token="leak"
# --- a comment that must not become a directive
#EXTINF:-1 tvg-id="news.1" tvg-logo="https://img/n.png" group-title="News, Sport",Example News, HD
https://cdn/news.m3u8
#EXTVLCOPT:http-user-agent=ExampleApp/2.1
#EXTINF:-1 group-title="Sport",Sport 1
https://cdn/sport1.m3u8
#EXTGRP:Docs
#EXTINF:-1,Explorer
https://cdn/explorer.m3u8
#KODIPROP:inputstream.adaptive.license_type=clearkey
#EXTINF:0 tvg-id="news.1" group-title="Dup",Dup
https://cdn/dup.mpd
https://cdn/bare.m3u8
#EXTINF:-1 tvg-name="Amp &amp; Co",Amp
https://cdn/amp.m3u8|User-Agent=X&Referer=https://r/
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.playlist = m3u.parse(SAMPLE)

    def test_header(self):
        self.assertEqual(self.playlist.header_attrs, {"name": "Example TV"})
        self.assertEqual(self.playlist.epg_urls, ["https://a/epg.xml", "https://b/x.xml", "https://c/third.xml"])

    def test_upstream_auth_dropped(self):
        self.assertEqual(len(self.playlist.warnings), 1)
        self.assertNotIn("UNLOOP", "\n".join(line for e in self.playlist.entries for line in e.extras))

    def test_name_with_comma_after_quoted_attributes(self):
        first = self.playlist.entries[0]
        self.assertEqual(first.name, "Example News, HD")
        self.assertEqual(first.group, "News, Sport")
        self.assertEqual(first.tvg_id, "news.1")

    def test_directives_attach_to_next_url_only(self):
        sport = self.playlist.entries[1]
        self.assertEqual(sport.extras, ["#EXTVLCOPT:http-user-agent=ExampleApp/2.1"])
        self.assertEqual(self.playlist.entries[2].extras, [])

    def test_extgrp_names_next_entry_only(self):
        self.assertEqual(self.playlist.entries[2].group, "Docs")
        self.assertEqual(self.playlist.entries[3].group, "Dup")

    def test_comment_is_not_a_directive(self):
        self.assertEqual(self.playlist.entries[0].extras, [])

    def test_bare_url_gets_a_name(self):
        self.assertEqual(self.playlist.entries[4].name, "Channel 1")

    def test_pipe_suffix_and_entities(self):
        amp = self.playlist.entries[5]
        self.assertEqual(amp.attr("tvg-name"), "Amp & Co")
        self.assertTrue(amp.url.endswith("|User-Agent=X&Referer=https://r/"))

    def test_match_keys_are_unique_and_prefer_ids(self):
        keys = [e.match_key for e in self.playlist.entries]
        self.assertEqual(len(keys), len(set(keys)))
        # news.1 appears twice, so neither may claim the id key.
        self.assertFalse(any(k == "id:news.1" for k in keys))
        self.assertEqual(self.playlist.entries[1].match_key, "n:sport|sport 1")


class RenderTests(unittest.TestCase):
    def test_round_trip_entry(self):
        entry = m3u.parse(SAMPLE).entries[0]
        lines = m3u.render_entry(entry.name, entry.url, entry.duration, entry.attrs, entry.extras)
        self.assertEqual(
            lines,
            [
                '#EXTINF:-1 tvg-id="news.1" tvg-logo="https://img/n.png" group-title="News, Sport",Example News, HD',
                "https://cdn/news.m3u8",
            ],
        )

    def test_set_attr_keeps_key_spelling(self):
        attrs = {"Group-Title": "Old", "tvg-id": "x"}
        m3u.set_attr(attrs, "group-title", "New")
        m3u.set_attr(attrs, "tvg-id", "")
        self.assertEqual(attrs, {"Group-Title": "New"})

    def test_auth_line(self):
        line = m3u.render_auth("https://p/unloop", "tok", poll=120, grace=0, on_error="deny")
        self.assertEqual(line, '#UNLOOP-AUTH:1 url="https://p/unloop" token="tok" poll="120" grace="0" on-error="deny"')

    def test_header(self):
        self.assertEqual(m3u.render_header("X", ["https://a", "https://b"]), '#EXTM3U name="X" url-tvg="https://a,https://b"')
        self.assertEqual(m3u.render_header(), "#EXTM3U")

    def test_quotes_in_values_are_neutralised(self):
        self.assertEqual(m3u.quote('a"b'), '"a\'b"')


if __name__ == "__main__":
    unittest.main()
