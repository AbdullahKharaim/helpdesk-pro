import importlib
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from html.parser import HTMLParser
from unittest.mock import patch

from itsdangerous import TimestampSigner

# Fail before opening any database if import-time initialization returns.
with patch("sqlite3.connect", side_effect=AssertionError("Importing app must not open a database")):
    from app import (
        CLIENT_UNKNOWN,
        CSRF_EXPIRED,
        CSRF_REJECTED,
        NOTES_FULL,
        RATE_LIMITED,
        TICKETS_FULL,
        create_app,
        saudi_time,
    )


VALID_TICKET = {
    "title": "طابعة تدريب لا تستجيب",
    "description": "وصف تجريبي للمشكلة",
    "category": "أجهزة",
    "requester_name": "مستخدم تجريبي",
}
TOKEN_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
DEPLOYMENT_VARIABLES = ("HELPDESK_ENV", "HELPDESK_SECRET_KEY", "HELPDESK_DATABASE", "FLASK_DEBUG")


def form_token(client, path="/tickets/new", **request_options):
    """Read the CSRF token a page issues to this client's session."""
    match = TOKEN_PATTERN.search(client.get(path, **request_options).get_data(as_text=True))
    if match is None:
        raise AssertionError(f"{path} has no CSRF token")
    return match.group(1)


def post_form(client, path, data, token=None, **request_options):
    """Submit a form the way a browser does: with the token from the same session."""
    if token is None:
        token = form_token(client, **request_options)
    return client.post(path, data={**data, "csrf_token": token}, **request_options)


class TimeParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.times = []
        self.current_time = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == "time":
            self.current_time = (dict(attrs), "")

    def handle_data(self, data):
        if self.current_time is not None:
            attrs, value = self.current_time
            self.current_time = (attrs, value + data)

    def handle_endtag(self, tag):
        if tag == "time":
            self.times.append(self.current_time)
            self.current_time = None


class ImportIsolationTests(unittest.TestCase):
    def test_import_does_not_open_database(self):
        with patch(
            "sqlite3.connect",
            side_effect=AssertionError("Importing app must not open a database"),
        ) as connect:
            importlib.reload(importlib.import_module("app"))
        connect.assert_not_called()


class AppTestCase(unittest.TestCase):
    """Each test gets its own temporary SQLite file; the application database is never opened."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = os.path.join(self.temp_dir.name, "tickets.sqlite3")
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, **config):
        return create_app({"TESTING": True, "DATABASE": self.database, **config})

    def query_one(self, sql, params=()):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(sql, params).fetchone()[0]

    def count_tickets(self):
        return self.query_one("SELECT COUNT(*) FROM tickets")

    def count_notes(self):
        return self.query_one("SELECT COUNT(*) FROM ticket_notes")

    def ticket_status(self, ticket_id=1):
        return self.query_one("SELECT status FROM tickets WHERE id = ?", (ticket_id,))

    def create_ticket(self, client=None):
        response = post_form(client or self.client, "/tickets/new", VALID_TICKET)
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"]


class TicketTests(AppTestCase):
    def test_saudi_time_crosses_midnight(self):
        cases = (
            ("2026-09-30T20:59:59+00:00", "2026-09-30 23:59"),
            ("2026-09-30T21:00:00+00:00", "2026-10-01 00:00"),
            ("2026-12-31T23:45:17+00:00", "2027-01-01 02:45"),
            ("2028-02-28T22:15:00+00:00", "2028-02-29 01:15"),
        )
        for stored, expected in cases:
            with self.subTest(stored=stored):
                self.assertEqual(saudi_time(stored), expected)

    def test_saudi_time_display_preserves_existing_data_and_order(self):
        first_ticket = self.create_ticket()
        second_ticket = self.create_ticket()
        ticket_time = "2026-09-30T23:45:17+00:00"
        second_ticket_time = "2026-09-30T20:00:00+00:00"
        note_time = "2026-09-30T21:05:59+00:00"
        earlier_note_time = "2026-09-30T20:59:00+00:00"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE tickets SET created_at = ? WHERE id = 1", (ticket_time,))
            db.execute("UPDATE tickets SET created_at = ? WHERE id = 2", (second_ticket_time,))
            db.executemany(
                "INSERT INTO ticket_notes (ticket_id, body, created_at) VALUES (1, ?, ?)",
                (
                    ("ملاحظة بعد منتصف الليل", note_time),
                    ("ملاحظة قبل منتصف الليل", earlier_note_time),
                    ("ملاحظة بنفس الوقت", note_time),
                ),
            )
            db.commit()
            tickets_before = db.execute("SELECT * FROM tickets ORDER BY id").fetchall()
            notes_before = db.execute("SELECT * FROM ticket_notes ORDER BY id").fetchall()

        reopened = create_app({"TESTING": True, "DATABASE": self.database})
        client = reopened.test_client()
        listing = client.get("/")
        detail = client.get(first_ticket)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        listing_page = listing.get_data(as_text=True)
        detail_page = detail.get_data(as_text=True)

        for page, expected_times in (
            (listing_page, (
                (second_ticket_time, "2026-09-30 23:00"),
                (ticket_time, "2026-10-01 02:45"),
            )),
            (detail_page, (
                (ticket_time, "2026-10-01 02:45"),
                (earlier_note_time, "2026-09-30 23:59"),
                (note_time, "2026-10-01 00:05"),
                (note_time, "2026-10-01 00:05"),
            )),
        ):
            with self.subTest(page="listing" if page == listing_page else "detail"):
                times = TimeParser(page).times
                self.assertEqual(
                    [(attrs.get("datetime"), value) for attrs, value in times],
                    list(expected_times),
                )
                self.assertTrue(all(attrs.get("dir") == "ltr" for attrs, _ in times))
                self.assertIn("بتوقيت السعودية", page)

        self.assertLess(listing_page.index(second_ticket), listing_page.index(first_ticket))
        note_positions = [detail_page.index(body) for body in (
            "ملاحظة قبل منتصف الليل", "ملاحظة بعد منتصف الليل", "ملاحظة بنفس الوقت"
        )]
        self.assertEqual(note_positions, sorted(note_positions))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM tickets ORDER BY id").fetchall(), tickets_before
            )
            self.assertEqual(
                db.execute("SELECT * FROM ticket_notes ORDER BY id").fetchall(), notes_before
            )

    def test_create_and_reopen(self):
        empty = self.client.get("/")
        self.assertIn("لا توجد بلاغات".encode(), empty.data)

        response = post_form(self.client, "/tickets/new", VALID_TICKET)
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
                response = post_form(self.client, "/tickets/new", data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message.encode(), response.data)
                if field != "title":
                    self.assertIn(VALID_TICKET["title"].encode(), response.data)
                if value.strip():
                    self.assertIn(value.encode(), response.data)
                self.assertEqual(self.count_tickets(), 0)

    def test_limits_apply_after_trimming(self):
        data = {**VALID_TICKET, "title": "  " + "س" * 120 + "  "}
        response = post_form(self.client, "/tickets/new", data)
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

        first = post_form(self.client, status_url, {"next_status": "قيد المعالجة"})
        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers["Location"], detail_url)
        processing_detail = self.client.get(detail_url)
        self.assertIn('name="next_status" value="تم الحل"'.encode(), processing_detail.data)
        self.assertNotIn("الانتقال إلى قيد المعالجة".encode(), processing_detail.data)
        self.assertIn("قيد المعالجة".encode(), self.client.get("/").data)

        second = post_form(self.client, status_url, {"next_status": "تم الحل"})
        self.assertEqual(second.status_code, 303)
        solved_detail = self.client.get(detail_url)
        self.assertIn("تم الحل".encode(), solved_detail.data)
        self.assertNotIn('name="next_status"'.encode(), solved_detail.data)
        self.assertIn("تم الحل".encode(), self.client.get("/").data)

    def test_status_persists_after_reopening_app(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        post_form(self.client, status_url, {"next_status": "قيد المعالجة"})

        reopened = create_app({"TESTING": True, "DATABASE": self.database})
        reopened_client = reopened.test_client()
        self.assertIn("قيد المعالجة".encode(), reopened_client.get(detail_url).data)
        self.assertIn("قيد المعالجة".encode(), reopened_client.get("/").data)
        self.assertEqual(self.ticket_status(), "قيد المعالجة")

        post_form(reopened_client, status_url, {"next_status": "تم الحل"})
        reopened_again = create_app({"TESTING": True, "DATABASE": self.database})
        self.assertIn("تم الحل".encode(), reopened_again.test_client().get(detail_url).data)
        self.assertIn("تم الحل".encode(), reopened_again.test_client().get("/").data)

    def test_rejects_invalid_status_transition(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        for target in ("تم الحل", "جديد", ""):
            with self.subTest(target=target):
                response = post_form(self.client, status_url, {"next_status": target})
                self.assertEqual(response.status_code, 400)
                self.assertIn("انتقال الحالة غير متاح.".encode(), response.data)
                self.assertEqual(self.ticket_status(), "جديد")

        post_form(self.client, status_url, {"next_status": "قيد المعالجة"})
        repeated = post_form(self.client, status_url, {"next_status": "قيد المعالجة"})
        self.assertEqual(repeated.status_code, 400)
        self.assertIn("انتقال الحالة غير متاح.".encode(), repeated.data)

    def test_rejects_transition_after_solved(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        post_form(self.client, status_url, {"next_status": "قيد المعالجة"})
        post_form(self.client, status_url, {"next_status": "تم الحل"})

        response = post_form(self.client, status_url, {"next_status": "جديد"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("انتقال الحالة غير متاح.".encode(), response.data)
        self.assertNotIn('name="next_status"'.encode(), response.data)
        self.assertEqual(self.ticket_status(), "تم الحل")

    def test_note_persists_after_reopening_app(self):
        detail_url = self.create_ticket()
        response = post_form(self.client, detail_url + "/notes", {"body": "  فحص تجريبي  "})
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
            response = post_form(self.client, first_ticket + "/notes", {"body": body})
            self.assertEqual(response.status_code, 303)

        second_ticket = self.create_ticket()
        post_form(self.client, second_ticket + "/notes", {"body": "ملاحظة بلاغ آخر"})

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
                response = post_form(self.client, detail_url + "/notes", {"body": body})
                self.assertEqual(response.status_code, 400)
                page = response.get_data(as_text=True)
                self.assertIn(error, page)
                self.assertIn(">" + body + "</textarea>", page)
                self.assertEqual(self.count_notes(), 0)

        accepted = post_form(self.client, detail_url + "/notes", {"body": "  " + "س" * 1000 + "  "})
        self.assertEqual(accepted.status_code, 303)
        with closing(sqlite3.connect(self.database)) as db:
            body = db.execute("SELECT body FROM ticket_notes").fetchone()[0]
        self.assertEqual(body, "س" * 1000)

    def test_can_add_note_after_ticket_is_solved(self):
        detail_url = self.create_ticket()
        status_url = detail_url + "/status"
        post_form(self.client, status_url, {"next_status": "قيد المعالجة"})
        post_form(self.client, status_url, {"next_status": "تم الحل"})

        response = post_form(self.client, detail_url + "/notes", {"body": "تمت المعالجة"})
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
        response = post_form(upgraded_client, detail_url + "/notes", {"body": "بعد التحديث"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("بعد التحديث".encode(), upgraded_client.get(detail_url).data)
        self.assertEqual(self.count_tickets(), 1)


class CsrfTests(AppTestCase):
    def test_every_form_carries_a_token(self):
        new_form = self.client.get("/tickets/new").get_data(as_text=True)
        self.assertEqual(len(TOKEN_PATTERN.findall(new_form)), 1)
        detail_url = self.create_ticket()
        self.assertEqual(len(TOKEN_PATTERN.findall(self.client.get(detail_url).get_data(as_text=True))), 2)
        post_form(self.client, detail_url + "/status", {"next_status": "قيد المعالجة"})
        post_form(self.client, detail_url + "/status", {"next_status": "تم الحل"})
        # A solved ticket has no status form left, only the note form.
        self.assertEqual(len(TOKEN_PATTERN.findall(self.client.get(detail_url).get_data(as_text=True))), 1)

    def test_missing_token_is_rejected_on_every_form(self):
        response = self.client.post("/tickets/new", data=VALID_TICKET)
        self.assertEqual(response.status_code, 400)
        page = response.get_data(as_text=True)
        self.assertIn(CSRF_REJECTED, page)
        self.assertIn(VALID_TICKET["title"], page)
        self.assertEqual(self.count_tickets(), 0)

        detail_url = self.create_ticket()
        response = self.client.post(detail_url + "/status", data={"next_status": "قيد المعالجة"})
        self.assertEqual(response.status_code, 400)
        self.assertIn(CSRF_REJECTED, response.get_data(as_text=True))
        self.assertEqual(self.ticket_status(), "جديد")

        response = self.client.post(detail_url + "/notes", data={"body": "ملاحظة دون رمز"})
        self.assertEqual(response.status_code, 400)
        page = response.get_data(as_text=True)
        self.assertIn(CSRF_REJECTED, page)
        self.assertIn(">ملاحظة دون رمز</textarea>", page)
        self.assertEqual(self.count_notes(), 0)

    def test_wrong_or_foreign_token_is_rejected(self):
        token = form_token(self.client)
        other_token = form_token(self.app.test_client())
        tampered = ("A" if token[0] != "A" else "B") + token[1:]
        for bad_token in ("not-a-token", tampered, other_token):
            with self.subTest(token=bad_token[:12]):
                response = post_form(self.client, "/tickets/new", VALID_TICKET, token=bad_token)
                self.assertEqual(response.status_code, 400)
                self.assertIn(CSRF_REJECTED, response.get_data(as_text=True))

        # A genuine token is useless without the session cookie it was issued with.
        response = post_form(self.app.test_client(), "/tickets/new", VALID_TICKET, token=token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.count_tickets(), 0)

    def test_expired_token_explains_and_offers_a_fresh_form(self):
        detail_url = self.create_ticket()
        issued_long_ago = int(time.time()) - self.app.config["CSRF_TIME_LIMIT"] - 60
        with patch.object(TimestampSigner, "get_timestamp", return_value=issued_long_ago):
            old_token = form_token(self.client)

        for path, data in (
            ("/tickets/new", VALID_TICKET),
            (detail_url + "/status", {"next_status": "قيد المعالجة"}),
            (detail_url + "/notes", {"body": "ملاحظة بنموذج قديم"}),
        ):
            with self.subTest(path=path):
                response = post_form(self.client, path, data, token=old_token)
                self.assertEqual(response.status_code, 400)
                self.assertIn(CSRF_EXPIRED, response.get_data(as_text=True))
        self.assertEqual((self.count_tickets(), self.ticket_status(), self.count_notes()), (1, "جديد", 0))

        response = post_form(self.client, "/tickets/new", VALID_TICKET, token=old_token)
        page = response.get_data(as_text=True)
        self.assertIn(VALID_TICKET["title"], page)
        fresh_token = TOKEN_PATTERN.search(page).group(1)
        self.assertNotEqual(fresh_token, old_token)
        resubmitted = post_form(self.client, "/tickets/new", VALID_TICKET, token=fresh_token)
        self.assertEqual(resubmitted.status_code, 302)


class LimitTests(AppTestCase):
    def run_together(self, app, path, data, clients, address=None):
        """Send one submission per client at the same moment and return the status codes."""
        prepared = []
        for index in range(clients):
            client = app.test_client()
            environ = {"REMOTE_ADDR": address or f"10.1.0.{index + 1}"}
            prepared.append((client, environ, form_token(client, environ_base=environ)))
        barrier = threading.Barrier(clients)
        statuses, failures = [], []

        def submit(client, environ, token):
            try:
                barrier.wait(10)
                response = client.post(path, data={**data, "csrf_token": token}, environ_base=environ)
                statuses.append(response.status_code)
            except Exception as error:  # surfaced below instead of dying silently in a thread
                failures.append(repr(error))

        threads = [threading.Thread(target=submit, args=item) for item in prepared]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(failures, [])
        self.assertEqual(len(statuses), clients)
        return statuses

    def test_oversized_request_is_refused_before_saving(self):
        detail_url = self.create_ticket()
        token = form_token(self.client)
        for path, data in (
            ("/tickets/new", {**VALID_TICKET, "description": "س" * 40000}),
            (detail_url + "/notes", {"body": "س" * 40000}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, data={**data, "csrf_token": token})
                self.assertEqual(response.status_code, 413)
                self.assertIn("حجم الطلب أكبر من المسموح", response.get_data(as_text=True))
        self.assertEqual((self.count_tickets(), self.count_notes()), (1, 0))

    def test_rate_limit_is_per_client_and_resets_after_the_window(self):
        app = self.make_app(WRITE_RATE_LIMIT=3, WRITE_RATE_WINDOW=60)
        client = app.test_client()
        # Forged submissions are refused before they can use up a visitor's quota.
        for _ in range(5):
            self.assertEqual(client.post("/tickets/new", data=VALID_TICKET).status_code, 400)
        for _ in range(3):
            self.assertEqual(post_form(client, "/tickets/new", VALID_TICKET).status_code, 302)

        response = post_form(client, "/tickets/new", VALID_TICKET)
        self.assertEqual(response.status_code, 429)
        page = response.get_data(as_text=True)
        self.assertIn(RATE_LIMITED, page)
        self.assertIn(VALID_TICKET["title"], page)
        self.assertTrue(1 <= int(response.headers["Retry-After"]) <= 60)
        self.assertEqual(self.count_tickets(), 3)

        other_visitor = {"environ_base": {"REMOTE_ADDR": "10.0.0.2"}}
        self.assertEqual(post_form(app.test_client(), "/tickets/new", VALID_TICKET, **other_visitor).status_code, 302)
        with patch("time.time", return_value=time.time() + 61):
            self.assertEqual(post_form(client, "/tickets/new", VALID_TICKET).status_code, 302)
        self.assertEqual(self.count_tickets(), 5)

    def test_status_and_note_forms_share_the_rate_limit(self):
        app = self.make_app(WRITE_RATE_LIMIT=2)
        client = app.test_client()
        detail_url = self.create_ticket(client)
        self.assertEqual(post_form(client, detail_url + "/notes", {"body": "ملاحظة"}).status_code, 303)

        status = post_form(client, detail_url + "/status", {"next_status": "قيد المعالجة"})
        self.assertEqual(status.status_code, 429)
        self.assertIn(RATE_LIMITED, status.get_data(as_text=True))
        note = post_form(client, detail_url + "/notes", {"body": "ملاحظة زائدة"})
        self.assertEqual(note.status_code, 429)
        self.assertIn(">ملاحظة زائدة</textarea>", note.get_data(as_text=True))
        self.assertEqual((self.ticket_status(), self.count_notes()), ("جديد", 1))

    def test_rate_limit_stores_no_raw_address(self):
        post_form(self.client, "/tickets/new", VALID_TICKET, environ_base={"REMOTE_ADDR": "198.51.100.7"})
        with closing(sqlite3.connect(self.database)) as db:
            clients = [row[0] for row in db.execute("SELECT client FROM write_attempts")]
        self.assertEqual(len(clients), 1)
        self.assertNotIn("198.51.100.7", clients[0])
        self.assertRegex(clients[0], r"^[0-9a-f]{64}$")

    def test_ticket_cap_keeps_input_and_explains(self):
        app = self.make_app(MAX_TICKETS=2)
        client = app.test_client()
        for _ in range(2):
            self.create_ticket(client)
        response = post_form(client, "/tickets/new", VALID_TICKET)
        self.assertEqual(response.status_code, 409)
        page = response.get_data(as_text=True)
        self.assertIn(TICKETS_FULL.format(limit=2), page)
        self.assertIn(VALID_TICKET["title"], page)
        self.assertEqual(self.count_tickets(), 2)

    def test_note_cap_applies_per_ticket(self):
        app = self.make_app(MAX_NOTES_PER_TICKET=2)
        client = app.test_client()
        first, second = self.create_ticket(client), self.create_ticket(client)
        for body in ("الأولى", "الثانية"):
            self.assertEqual(post_form(client, first + "/notes", {"body": body}).status_code, 303)
        response = post_form(client, first + "/notes", {"body": "الثالثة"})
        self.assertEqual(response.status_code, 409)
        page = response.get_data(as_text=True)
        self.assertIn(NOTES_FULL.format(limit=2), page)
        self.assertIn(">الثالثة</textarea>", page)
        self.assertEqual(post_form(client, second + "/notes", {"body": "على بلاغ آخر"}).status_code, 303)
        self.assertEqual(self.count_notes(), 3)

    def test_concurrent_creates_never_pass_the_ticket_cap(self):
        app = self.make_app(MAX_TICKETS=3)
        statuses = self.run_together(app, "/tickets/new", VALID_TICKET, clients=12)
        self.assertEqual(sorted(statuses), [302] * 3 + [409] * 9)
        self.assertEqual(self.count_tickets(), 3)

    def test_concurrent_notes_never_pass_the_note_cap(self):
        app = self.make_app(MAX_NOTES_PER_TICKET=2)
        detail_url = self.create_ticket(app.test_client())
        statuses = self.run_together(app, detail_url + "/notes", {"body": "ملاحظة متزامنة"}, clients=8)
        self.assertEqual(sorted(statuses), [303] * 2 + [409] * 6)
        self.assertEqual(self.count_notes(), 2)

    def test_concurrent_submissions_share_one_rate_limit(self):
        app = self.make_app(WRITE_RATE_LIMIT=4)
        statuses = self.run_together(app, "/tickets/new", VALID_TICKET, clients=10, address="203.0.113.50")
        self.assertEqual(sorted(statuses), [302] * 4 + [429] * 6)
        self.assertEqual(self.count_tickets(), 4)


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = os.path.join(self.temp_dir.name, "data", "helpdesk.sqlite3")
        self.secret = secrets.token_hex(32)

    @contextmanager
    def environment(self, **values):
        """Run with exactly these deployment variables, whatever the developer's shell has."""
        with patch.dict(os.environ, values):
            for name in DEPLOYMENT_VARIABLES:
                if name not in values:
                    os.environ.pop(name, None)
            yield

    def production(self, **extra):
        return self.environment(
            HELPDESK_ENV="production",
            HELPDESK_SECRET_KEY=self.secret,
            HELPDESK_DATABASE=self.database,
            **extra,
        )

    def test_production_refuses_to_start_without_a_strong_secret(self):
        for secret in (None, "", "short-secret"):
            variables = {"HELPDESK_ENV": "production", "HELPDESK_DATABASE": self.database}
            if secret is not None:
                variables["HELPDESK_SECRET_KEY"] = secret
            with self.subTest(secret=secret), self.environment(**variables):
                with self.assertRaises(RuntimeError):
                    create_app()
        self.assertFalse(os.path.exists(self.database))

    def test_database_path_must_be_absolute_and_explicit_in_production(self):
        with self.environment(HELPDESK_ENV="production", HELPDESK_SECRET_KEY=self.secret):
            with self.assertRaises(RuntimeError):
                create_app({"DATABASE": self.database})
        for variables in (
            {"HELPDESK_ENV": "production", "HELPDESK_SECRET_KEY": self.secret},
            {},
        ):
            with self.subTest(mode=variables.get("HELPDESK_ENV", "local")):
                with self.environment(HELPDESK_DATABASE=os.path.join("data", "helpdesk.sqlite3"), **variables):
                    with self.assertRaises(RuntimeError):
                        create_app()
        self.assertFalse(os.path.exists(self.database))

    def test_production_settings_and_full_cycle_over_https(self):
        with self.production(FLASK_DEBUG="1"):
            app = create_app()
        self.assertFalse(app.debug)
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(app.config["DATABASE"], self.database)
        self.assertTrue(os.path.exists(self.database))

        client = app.test_client()
        # As on PythonAnywhere: the load balancer appends the visitor address it saw.
        https = {"base_url": "https://demo.example", "headers": {"X-Forwarded-For": "203.0.113.20"}}
        cookie = client.get("/tickets/new", **https).headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(flag, cookie)

        created = post_form(client, "/tickets/new", VALID_TICKET, **https)
        self.assertEqual(created.status_code, 302)
        detail_url = created.headers["Location"]
        for next_status in ("قيد المعالجة", "تم الحل"):
            response = post_form(client, detail_url + "/status", {"next_status": next_status}, **https)
            self.assertEqual(response.status_code, 303)
        self.assertEqual(post_form(client, detail_url + "/notes", {"body": "أُغلق في وضع النشر"}, **https).status_code, 303)
        page = client.get(detail_url, **https).get_data(as_text=True)
        self.assertIn("تم الحل", page)
        self.assertIn("أُغلق في وضع النشر", page)

    def production_submitter(self, **config):
        with self.production():
            app = create_app(config)

        def submit(forwarded_for):
            options = {"base_url": "https://demo.example", "headers": {"X-Forwarded-For": forwarded_for}}
            return post_form(app.test_client(), "/tickets/new", VALID_TICKET, **options)

        return submit

    def count_tickets(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]

    def test_production_rate_limit_trusts_only_the_load_balancer_hop(self):
        submit = self.production_submitter(WRITE_RATE_LIMIT=1)
        self.assertEqual(submit("198.51.100.1, 203.0.113.7").status_code, 302)
        # A forged first entry is not a new visitor; only the address the balancer saw counts.
        self.assertEqual(submit("192.0.2.99, 203.0.113.7").status_code, 429)
        self.assertEqual(submit("203.0.113.8").status_code, 302)
        self.assertEqual(submit("2001:db8::1").status_code, 302)
        self.assertEqual(self.count_tickets(), 3)

    def test_malformed_forwarded_prefix_cannot_reset_the_rate_limit(self):
        # Regression: with ProxyFix an unclosed quote made Werkzeug's list parsing fail, the
        # address fell back to the shared load balancer, and a second ticket got through.
        submit = self.production_submitter(WRITE_RATE_LIMIT=1)
        self.assertEqual(submit("198.51.100.1, 203.0.113.7").status_code, 302)
        self.assertEqual(submit("192.0.2.99, 203.0.113.7").status_code, 429)
        for prefix in ('"192.0.2.99', '"', '"unclosed, "also', "not-an-ip, ;;"):
            with self.subTest(prefix=prefix):
                self.assertEqual(submit(f"{prefix}, 203.0.113.7").status_code, 429)
        self.assertEqual(self.count_tickets(), 1)

    def test_production_refuses_submissions_without_a_valid_last_hop(self):
        submit = self.production_submitter()
        for forwarded_for in ("", "203.0.113.7, not-an-ip", '203.0.113.7, "', "203.0.113.7,"):
            with self.subTest(forwarded_for=forwarded_for):
                response = submit(forwarded_for)
                self.assertEqual(response.status_code, 400)
                page = response.get_data(as_text=True)
                self.assertIn(CLIENT_UNKNOWN, page)
                self.assertIn(VALID_TICKET["title"], page)
        self.assertEqual(self.count_tickets(), 0)

    def test_local_mode_keeps_plain_http_working(self):
        with self.environment():
            first = create_app({"DATABASE": self.database})
            second = create_app({"DATABASE": self.database})
        self.assertFalse(first.config["PRODUCTION"])
        self.assertFalse(first.debug)
        self.assertFalse(first.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(len(first.config["SECRET_KEY"]), 64)
        self.assertNotEqual(first.config["SECRET_KEY"], second.config["SECRET_KEY"])

        client = first.test_client()
        cookie = client.get("/tickets/new").headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(post_form(client, "/tickets/new", VALID_TICKET).status_code, 302)

    def test_local_mode_reads_an_absolute_database_path_from_the_environment(self):
        with self.environment(HELPDESK_DATABASE=self.database):
            app = create_app()
        self.assertEqual(app.config["DATABASE"], self.database)
        self.assertTrue(os.path.exists(self.database))


class PageTests(AppTestCase):
    def test_routes_and_arabic_error_pages(self):
        for path in ("/", "/tickets/new", "/static/style.css"):
            with self.subTest(path=path):
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
        detail_url = self.create_ticket()
        self.assertEqual(self.client.get(detail_url).status_code, 200)

        for path in ("/tickets/999", "/no-such-page"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                page = response.get_data(as_text=True)
                self.assertIn("الصفحة غير موجودة", page)
                self.assertIn('lang="ar" dir="rtl"', page)

        response = self.client.get(detail_url + "/status")
        self.assertEqual(response.status_code, 405)
        self.assertIn("لا يمكن فتح هذا الرابط مباشرة", response.get_data(as_text=True))
        self.assertIn("POST", response.headers["Allow"])

        missing = post_form(self.client, "/tickets/999/notes", {"body": "لبلاغ غير موجود"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.count_notes(), 0)

    def test_shared_demo_notice_is_on_every_page(self):
        detail_url = self.create_ticket()
        for path in ("/", "/tickets/new", detail_url, "/tickets/999"):
            with self.subTest(path=path):
                self.assertIn("عرض تجريبي مشترك", self.client.get(path).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
