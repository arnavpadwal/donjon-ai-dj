"""Small, dependency-free logic checks. Run: python -m unittest discover tests"""
from __future__ import annotations

import os
import struct
import tempfile
import unittest

from backend.autodj import AppState, harmonic_score
from backend.audio import MAGIC
from backend.config import ROOM_CODE
from backend.db import Database
from backend.library import camelot_for, clean_text
from backend.spotify import SpotifyService


class LogicTests(unittest.TestCase):
    def test_camelot(self):
        self.assertEqual(camelot_for("A minor"), "8A")
        self.assertEqual(camelot_for("Am"), "8A")
        self.assertEqual(camelot_for("C major"), "8B")
        self.assertEqual(camelot_for("F#m"), "11A")
        self.assertEqual(camelot_for("8A"), "8A")
        self.assertEqual(camelot_for("garbage"), "")

    def test_harmony(self):
        self.assertEqual(harmonic_score("8A", "8A"), 1.0)
        self.assertGreater(harmonic_score("8A", "9A"), harmonic_score("8A", "3B"))

    def test_room_code_and_text(self):
        self.assertEqual(len(ROOM_CODE), 6)
        self.assertTrue(ROOM_CODE.isdigit())
        self.assertEqual(clean_text("𝐊𝐚𝐥𝐚 Chashma 🕶️"), "Kala Chashma")
        self.assertEqual(SpotifyService.playlist_id(
            "https://open.spotify.com/playlist/abc123XYZ"), "abc123XYZ")

    def test_queue_priority_and_move(self):
        db = Database(os.path.join(tempfile.mkdtemp(), "q.db"))
        a = db.add_track("/tmp/a.mp3", "djaaaaaaaa", "A")
        b = db.add_track("/tmp/b.mp3", "djbbbbbbbb", "B")
        state = AppState("123456")
        qa = state.add_queue(a, "one")
        qb = state.add_queue(b, "two")
        state.move_queue(qb["uid"], -1)
        self.assertEqual(state.queue[0]["uid"], qb["uid"])
        state.play_next(qa["uid"])
        self.assertEqual(state.queue[0]["uid"], qa["uid"])
        self.assertTrue(state.queue[0]["priority"])
        self.assertEqual(state.pop_queue()["uid"], qa["uid"])

    def test_keymap_avoids_altgr_and_library_collisions(self):
        from backend.config import KEYMAP, KEYMAP_FILE
        # X11 delivers ctrl+alt+digits/symbols/F-keys as AltGr/VT keys, so
        # Mixxx never sees the intended shortcut.
        for key, seq in KEYMAP.items():
            self.assertFalse(
                seq.startswith("ctrl+alt+") and (
                    seq.split("+")[-1].isdigit() or seq.split("+")[-1].startswith("F")),
                f"{key} uses an AltGr-colliding key {seq}")
        # The library's "activate selected track" must never share a key with
        # the crossfader, or a blend would load a random track.
        self.assertEqual(KEYMAP["go_tracks"], "alt+shift+w")
        rendered = KEYMAP_FILE.format(**KEYMAP)
        self.assertIn("GoToItem alt+shift+w", rendered)
        self.assertIn("LoadSelectedTrack alt+shift+l", rendered)
        self.assertIn("LoadSelectedTrack alt+shift+n", rendered)

    def test_frame_header(self):
        frame = struct.pack("<4sId", MAGIC, 7, 1234567890.5) + b"\x00" * 3840
        magic, seq, ts = struct.unpack("<4sId", frame[:16])
        self.assertEqual(magic, b"DONJ")
        self.assertEqual(seq, 7)
        self.assertAlmostEqual(ts, 1234567890.5)

    def test_db_and_queue(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "test.db"))
            tr = db.add_track("/tmp/x.mp3", "djdeadbeef", "Test", "Artist",
                              duration=120, bpm=124, camelot="8A")
            self.assertTrue(tr["id"])
            self.assertEqual(db.list_tracks("Test")[0]["token"], "djdeadbeef")
            db.update_analysis("djdeadbeef", 126, "A minor", "8A", 121)
            self.assertEqual(db.track(tr["id"])["bpm"], 126)

            state = AppState("DONJON")
            item = state.add_queue(db.track(tr["id"]), "Guest")
            self.assertEqual(state.vote(item["uid"], "cid1"), 1)
            self.assertEqual(state.vote(item["uid"], "cid1"), 1)
            popped = state.pop_queue()
            self.assertEqual(popped["uid"], item["uid"])


if __name__ == "__main__":
    unittest.main()
