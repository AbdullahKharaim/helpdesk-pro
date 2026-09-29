import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from app import create_app


VALID_TICKET = {
    "title": "طابعة تدريب لا تستجيب",
    "description": "وصف تجريبي للمشكلة",
    "category": "أجهزة",
    "requester_name": "مستخدم تجريبي",
}


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = os.path.join(self.temp_dir.name, "tickets.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.database})
        self.client = self.app.test_client()

    def count_tickets(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def test_create_and_reopen(self):
        empty = self.client.get("/")
        self.assertIn("لا توجد بلاغات".encode(), empty.data)

        response = self.client.post("/tickets/new", data=VALID_TICKET)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/tickets/1")

        detail = self.client.get(response.headers["Location"])
        self.assertIn(VALID_TICKET["title"].encode(), detail.data)
        self.assertIn("جديد".encode(), detail.data)

        # A fresh application instance must read the same saved row from SQLite.
        reopened = create_app({"TESTING": True, "DATABASE": self.database})
        listing = reopened.test_client().get("/")
        self.assertIn(VALID_TICKET["title"].encode(), listing.data)
        self.assertIn(b"/tickets/1", listing.data)
        with closing(sqlite3.connect(self.database)) as db:
            ticket_id, status, created_at = db.execute(
                "SELECT id, status, created_at FROM tickets"
            ).fetchone()
        self.assertEqual(ticket_id, 1)
        self.assertEqual(status, "جديد")
        self.assertTrue(created_at.endswith("+00:00"))

    def test_rejects_invalid_data_and_keeps_values(self):
        invalid_cases = (
            ("title", "  ", "أدخل عنوان البلاغ."),
            ("title", "س" * 121, "يجب ألا يتجاوز العنوان 120 حرفًا."),
            ("description", "  ", "أدخل وصف المشكلة."),
            ("description", "س" * 2001, "يجب ألا يتجاوز الوصف 2000 حرف."),
            ("category", "", "اختر تصنيفًا للبلاغ."),
            ("category", "غير معروف", "اختر تصنيفًا من القائمة."),
            ("requester_name", "  ", "أدخل اسمًا تجريبيًا لمقدم الطلب."),
            ("requester_name", "س" * 81, "يجب ألا يتجاوز الاسم 80 حرفًا."),
        )
        for field, value, message in invalid_cases:
            with self.subTest(field=field, value=value[:20]):
                data = {**VALID_TICKET, field: value}
                response = self.client.post("/tickets/new", data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message.encode(), response.data)
                if field != "title":
                    self.assertIn(VALID_TICKET["title"].encode(), response.data)
                if value.strip():
                    self.assertIn(value.encode(), response.data)
                self.assertEqual(self.count_tickets(), 0)

    def test_limits_apply_after_trimming(self):
        data = {**VALID_TICKET, "title": "  " + "س" * 120 + "  "}
        response = self.client.post("/tickets/new", data=data)
        self.assertEqual(response.status_code, 302)
        with closing(sqlite3.connect(self.database)) as db:
            title = db.execute("SELECT title FROM tickets").fetchone()[0]
        self.assertEqual(title, "س" * 120)


if __name__ == "__main__":
    unittest.main()
