import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "hub.db")

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp_dir.cleanup()

    def test_init_migrates_existing_requests_table(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.executescript(
                """
                CREATE TABLE requests (
                  id INTEGER PRIMARY KEY,
                  name TEXT,
                  model TEXT,
                  image_count INTEGER DEFAULT 0,
                  image_bytes INTEGER DEFAULT 0,
                  status TEXT NOT NULL,
                  enqueued_at REAL,
                  started_at REAL,
                  finished_at REAL,
                  queue_ms INTEGER,
                  process_ms INTEGER,
                  out_chars INTEGER,
                  error TEXT
                );
                """
            )
        db.init_db()
        with db.connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(requests)")
            }
        self.assertIn("client_ip", columns)

    def test_client_ip_is_logged_and_aggregated(self):
        db.init_db()
        rid = db.log_enqueue("张三", "deepseek-ocr", 2, 100, "192.168.3.42")
        db.log_start(rid)
        db.log_finish(rid, "ok", 123)

        row = db.recent(1)[0]
        self.assertEqual(row["client_ip"], "192.168.3.42")
        summary = db.stats_by_client()[0]
        self.assertEqual(summary["client_ip"], "192.168.3.42")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["images"], 2)
        self.assertEqual(summary["ok"], 1)

    def test_issue_key_for_ip_is_normalized_and_idempotent(self):
        db.init_db()

        first, is_new = db.issue_key_for_ip("::ffff:192.168.3.42")
        again, is_new_again = db.issue_key_for_ip("192.168.3.42")

        self.assertTrue(is_new)
        self.assertFalse(is_new_again)
        self.assertEqual(first, again)
        self.assertEqual(db.lookup_key(first)["name"], "192.168.3.42")

        with self.assertRaisesRegex(ValueError, "有效的本机 IP"):
            db.issue_key_for_ip("not-an-ip")


if __name__ == "__main__":
    unittest.main()
