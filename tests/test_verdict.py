import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

_tmp = tempfile.mkdtemp(prefix="iptvprovider-test-")
os.environ["IPTV_DATA_DIR"] = _tmp

from iptvprovider import config, db, subscribers  # noqa: E402  (after env)
from iptvprovider.web import create_app  # noqa: E402

config.DATA_DIR = db.DB_PATH.parent  # keep the fixture directory in sync


class VerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_tmp, ignore_errors=True)

    def setUp(self):
        db.execute("DELETE FROM subscribers")
        db.execute("DELETE FROM activity")
        db.set_settings({"public_base_url": "https://panel.test", "warn_days": 5, "renew_url": "", "stream_headers": ""})
        self.sid = subscribers.create("Ali", 30, 2)
        self.token = subscribers.get(self.sid)["token"]

    def ask(self, device="d70ff1974ff64351", **extra):
        body = {"v": 1, "token": self.token, "device_id": device, "reason": "playlist", "session_id": "s1"}
        body.update(extra)
        response = self.client.post("/unloop", data=json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        return response.get_json()

    def test_ok_and_device_slots(self):
        first = self.ask()
        self.assertEqual(first["state"], "ok")
        self.assertEqual(first["device"], {"registered": True, "slot": 1, "limit": 2})
        self.assertEqual(self.ask(device="aaaa000000000002")["device"]["slot"], 2)
        third = self.ask(device="cccc000000000003")
        self.assertEqual(third["state"], "block")
        self.assertEqual(third["device"], {"registered": False, "limit": 2})
        self.assertTrue(third["action_url"].startswith("https://panel.test/me/"))
        # The same device again keeps its slot rather than taking a new one.
        self.assertEqual(self.ask()["device"]["slot"], 1)

    def test_removing_a_device_frees_the_slot(self):
        self.ask()
        self.ask(device="aaaa000000000002")
        subscribers.remove_device(self.sid, "d70ff1974ff64351")
        self.assertEqual(self.ask(device="cccc000000000003")["device"]["slot"], 1)

    def test_expired(self):
        subscribers.update(self.sid, expires_at="2020-01-01T00:00:00Z")
        reply = self.ask()
        self.assertEqual(reply["state"], "expired")
        self.assertIn("01 January 2020", reply["message"])
        self.assertEqual(reply["action_label"], "Scan to renew")

    def test_warn_before_expiry(self):
        subscribers.update(self.sid, expires_at=db.iso(db.utcnow() + timedelta(days=2)))
        reply = self.ask()
        self.assertEqual(reply["state"], "warn")
        self.assertIn("day", reply["message"])

    def test_blocked_with_own_message(self):
        subscribers.update(self.sid, status="blocked", message="Call us")
        reply = self.ask()
        self.assertEqual(reply["state"], "block")
        self.assertEqual(reply["message"], "Call us")

    def test_warned_by_admin(self):
        subscribers.update(self.sid, status="warned", message="Maintenance tonight")
        self.assertEqual(self.ask()["message"], "Maintenance tonight")

    def test_unknown_token_is_a_block_not_an_error(self):
        response = self.client.post("/unloop", json={"token": "nope", "device_id": "x" * 16})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "block")

    def test_json_without_content_type_is_still_read(self):
        body = json.dumps({"token": self.token, "device_id": "d70ff1974ff64351"})
        response = self.client.post("/unloop", data=body, content_type="text/plain")
        self.assertEqual(response.get_json()["state"], "ok")

    def test_stream_headers_setting(self):
        db.set_settings({"stream_headers": '{"X-Play-Token": "abc", "Host": "evil"}', "stream_headers_ttl": 120})
        reply = self.ask()
        self.assertEqual(reply["stream"], {"headers": {"X-Play-Token": "abc"}, "expires_in": 120})

    def test_playlist_carries_auth_line_and_refuses_when_expired(self):
        response = self.client.get(f"/p/{self.token}.m3u")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'#UNLOOP-AUTH:1 url="https://panel.test/unloop" token="{self.token}"', text)
        subscribers.update(self.sid, expires_at="2020-01-01T00:00:00Z")
        refused = self.client.get(f"/p/{self.token}.m3u").get_data(as_text=True)
        self.assertNotIn("#EXTINF", refused)

    def test_activity_is_logged(self):
        self.ask()
        row = db.one("SELECT * FROM activity ORDER BY id DESC")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["subscriber_id"], self.sid)


if __name__ == "__main__":
    unittest.main()
