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

    def count_notes(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]

    def create_ticket(self):
        response = self.client.post("/tickets/new", data=VALID_TICKET)
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"]

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

    def test_status_advances_one_step_at_a_time(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"

        new_detail = self.client.get(detail_url)
        self.assertIn('name="next_status" value="قيد المعالجة"'.encode(), new_detail.data)
        self.assertNotIn("الانتقال إلى تم الحل".encode(), new_detail.data)

        first = self.client.post(status_url, data={"next_status": "قيد المعالجة"})
        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers["Location"], detail_url)
        processing_detail = self.client.get(detail_url)
        self.assertIn('name="next_status" value="تم الحل"'.encode(), processing_detail.data)
        self.assertNotIn("الانتقال إلى قيد المعالجة".encode(), processing_detail.data)
        self.assertIn("قيد المعالجة".encode(), self.client.get("/").data)

        second = self.client.post(status_url, data={"next_status": "تم الحل"})
        self.assertEqual(second.status_code, 303)
        solved_detail = self.client.get(detail_url)
        self.assertIn("تم الحل".encode(), solved_detail.data)
        self.assertNotIn('name="next_status"'.encode(), solved_detail.data)
        self.assertIn("تم الحل".encode(), self.client.get("/").data)

    def test_status_persists_after_reopening_app(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        self.client.post(status_url, data={"next_status": "قيد المعالجة"})

        reopened = create_app({"TESTING": True, "DATABASE": self.database})
        reopened_client = reopened.test_client()
        self.assertIn("قيد المعالجة".encode(), reopened_client.get(detail_url).data)
        self.assertIn("قيد المعالجة".encode(), reopened_client.get("/").data)
        with closing(sqlite3.connect(self.database)) as db:
            status = db.execute("SELECT status FROM tickets WHERE id = 1").fetchone()[0]
        self.assertEqual(status, "قيد المعالجة")

        reopened_client.post(status_url, data={"next_status": "تم الحل"})
        reopened_again = create_app({"TESTING": True, "DATABASE": self.database})
        self.assertIn("تم الحل".encode(), reopened_again.test_client().get(detail_url).data)
        self.assertIn("تم الحل".encode(), reopened_again.test_client().get("/").data)

    def test_rejects_invalid_status_transition(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        for target in ("تم الحل", "جديد", ""):
            with self.subTest(target=target):
                response = self.client.post(status_url, data={"next_status": target})
                self.assertEqual(response.status_code, 400)
                self.assertIn("انتقال الحالة غير متاح.".encode(), response.data)
                with closing(sqlite3.connect(self.database)) as db:
                    status = db.execute("SELECT status FROM tickets WHERE id = 1").fetchone()[0]
                self.assertEqual(status, "جديد")

        self.client.post(status_url, data={"next_status": "قيد المعالجة"})
        repeated = self.client.post(status_url, data={"next_status": "قيد المعالجة"})
        self.assertEqual(repeated.status_code, 400)
        self.assertIn("انتقال الحالة غير متاح.".encode(), repeated.data)

    def test_rejects_transition_after_solved(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        self.client.post(status_url, data={"next_status": "قيد المعالجة"})
        self.client.post(status_url, data={"next_status": "تم الحل"})

        response = self.client.post(status_url, data={"next_status": "جديد"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("انتقال الحالة غير متاح.".encode(), response.data)
        self.assertNotIn('name="next_status"'.encode(), response.data)
        with closing(sqlite3.connect(self.database)) as db:
            status = db.execute("SELECT status FROM tickets WHERE id = 1").fetchone()[0]
        self.assertEqual(status, "تم الحل")

    def test_note_persists_after_reopening_app(self):
        detail_url = self.create_ticket()
        response = self.client.post(detail_url + "/notes", data={"body": "  فحص تجريبي  "})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], detail_url)

        reopened = create_app({"TESTING": True, "DATABASE": self.database})
        detail = reopened.test_client().get(detail_url)
        self.assertIn("فحص تجريبي".encode(), detail.data)
        self.assertIn("ملاحظة رقم 1".encode(), detail.data)
        with closing(sqlite3.connect(self.database)) as db:
            note_id, ticket_id, body, created_at = db.execute(
                "SELECT id, ticket_id, body, created_at FROM ticket_notes"
            ).fetchone()
        self.assertEqual((note_id, ticket_id, body), (1, 1, "فحص تجريبي"))
        self.assertTrue(created_at.endswith("+00:00"))

    def test_notes_are_oldest_first_and_belong_to_their_ticket(self):
        first_ticket = self.create_ticket()
        for body in ("الملاحظة الأولى", "الملاحظة الثانية", "الملاحظة الثالثة"):
            response = self.client.post(first_ticket + "/notes", data={"body": body})
            self.assertEqual(response.status_code, 303)

        second_ticket = self.create_ticket()
        self.client.post(second_ticket + "/notes", data={"body": "ملاحظة بلاغ آخر"})

        first_page = self.client.get(first_ticket).get_data(as_text=True)
        positions = [first_page.index(body) for body in (
            "الملاحظة الأولى", "الملاحظة الثانية", "الملاحظة الثالثة"
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("ملاحظة بلاغ آخر", first_page)
        self.assertIn("ملاحظة بلاغ آخر".encode(), self.client.get(second_ticket).data)

    def test_rejects_empty_and_long_notes_without_losing_input(self):
        detail_url = self.create_ticket()
        for body, error in (
            ("   ", "اكتب ملاحظة المعالجة."),
            ("س" * 1001, "يجب ألا تتجاوز الملاحظة 1000 حرف."),
        ):
            with self.subTest(body_length=len(body)):
                response = self.client.post(detail_url + "/notes", data={"body": body})
                self.assertEqual(response.status_code, 400)
                page = response.get_data(as_text=True)
                self.assertIn(error, page)
                self.assertIn(">" + body + "</textarea>", page)
                self.assertEqual(self.count_notes(), 0)

        accepted = self.client.post(
            detail_url + "/notes", data={"body": "  " + "س" * 1000 + "  "}
        )
        self.assertEqual(accepted.status_code, 303)
        with closing(sqlite3.connect(self.database)) as db:
            body = db.execute("SELECT body FROM ticket_notes").fetchone()[0]
        self.assertEqual(body, "س" * 1000)

    def test_can_add_note_after_ticket_is_solved(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        self.client.post(status_url, data={"next_status": "قيد المعالجة"})
        self.client.post(status_url, data={"next_status": "تم الحل"})

        response = self.client.post(detail_url + "/notes", data={"body": "تمت المعالجة"})
        self.assertEqual(response.status_code, 303)
        detail = self.client.get(detail_url)
        self.assertIn("تم الحل".encode(), detail.data)
        self.assertIn("تمت المعالجة".encode(), detail.data)
        self.assertEqual(self.count_notes(), 1)

    def test_existing_ticket_survives_notes_table_initialization(self):
        detail_url = self.create_ticket()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE ticket_notes")
            db.commit()

        upgraded = create_app({"TESTING": True, "DATABASE": self.database})
        upgraded_client = upgraded.test_client()
        self.assertIn(VALID_TICKET["title"].encode(), upgraded_client.get(detail_url).data)
        response = upgraded_client.post(detail_url + "/notes", data={"body": "بعد التحديث"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("بعد التحديث".encode(), upgraded_client.get(detail_url).data)
        self.assertEqual(self.count_tickets(), 1)


if __name__ == "__main__":
    unittest.main()
