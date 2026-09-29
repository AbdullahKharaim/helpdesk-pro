import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, abort, g, redirect, render_template, request, url_for


CATEGORIES = ("أجهزة", "برامج", "شبكة", "أخرى")
FIELD_LIMITS = {"title": 120, "description": 2000, "requester_name": 80}
NOTE_LIMIT = 1000
NEXT_STATUS = {"جديد": "قيد المعالجة", "قيد المعالجة": "تم الحل"}
STATUS_ERROR = "انتقال الحالة غير متاح."


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(DATABASE=os.path.join(app.instance_path, "helpdesk.sqlite3"))
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    with app.app_context():
        get_db().execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('أجهزة', 'برامج', 'شبكة', 'أخرى')),
                requester_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'جديد' CHECK (status IN ('جديد', 'قيد المعالجة', 'تم الحل')),
                created_at TEXT NOT NULL
            )
            """
        )
        get_db().execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        get_db().commit()

    @app.get("/")
    def ticket_list():
        tickets = get_db().execute(
            "SELECT id, title, category, requester_name, status, created_at "
            "FROM tickets ORDER BY id DESC"
        ).fetchall()
        return render_template("ticket_list.html", tickets=tickets)

    @app.route("/tickets/new", methods=["GET", "POST"])
    def ticket_new():
        values = {key: "" for key in (*FIELD_LIMITS, "category")}
        errors = {}

        if request.method == "POST":
            values = {key: request.form.get(key, "") for key in values}
            cleaned = {key: value.strip() for key, value in values.items()}

            if not cleaned["title"]:
                errors["title"] = "أدخل عنوان البلاغ."
            elif len(cleaned["title"]) > FIELD_LIMITS["title"]:
                errors["title"] = "يجب ألا يتجاوز العنوان 120 حرفًا."

            if not cleaned["description"]:
                errors["description"] = "أدخل وصف المشكلة."
            elif len(cleaned["description"]) > FIELD_LIMITS["description"]:
                errors["description"] = "يجب ألا يتجاوز الوصف 2000 حرف."

            if not cleaned["category"]:
                errors["category"] = "اختر تصنيفًا للبلاغ."
            elif cleaned["category"] not in CATEGORIES:
                errors["category"] = "اختر تصنيفًا من القائمة."

            if not cleaned["requester_name"]:
                errors["requester_name"] = "أدخل اسمًا تجريبيًا لمقدم الطلب."
            elif len(cleaned["requester_name"]) > FIELD_LIMITS["requester_name"]:
                errors["requester_name"] = "يجب ألا يتجاوز الاسم 80 حرفًا."

            if not errors:
                cursor = get_db().execute(
                    """
                    INSERT INTO tickets (title, description, category, requester_name, status, created_at)
                    VALUES (?, ?, ?, ?, 'جديد', ?)
                    """,
                    (
                        cleaned["title"],
                        cleaned["description"],
                        cleaned["category"],
                        cleaned["requester_name"],
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                get_db().commit()
                return redirect(url_for("ticket_detail", ticket_id=cursor.lastrowid))

        return render_template(
            "ticket_new.html", values=values, errors=errors, categories=CATEGORIES
        )

    @app.get("/tickets/<int:ticket_id>")
    def ticket_detail(ticket_id):
        ticket = load_ticket(ticket_id)
        return render_ticket_detail(ticket)

    def load_ticket(ticket_id):
        ticket = get_db().execute(
            "SELECT id, title, description, category, requester_name, status, created_at "
            "FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        if ticket is None:
            abort(404)
        return ticket

    def render_ticket_detail(ticket, note_body="", note_error=None, status_error=None):
        notes = get_db().execute(
            "SELECT id, body, created_at FROM ticket_notes "
            "WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
            (ticket["id"],),
        ).fetchall()
        return render_template(
            "ticket_detail.html",
            ticket=ticket,
            next_status=NEXT_STATUS.get(ticket["status"]),
            notes=notes,
            note_body=note_body,
            note_error=note_error,
            status_error=status_error,
        )

    @app.post("/tickets/<int:ticket_id>/status")
    def ticket_status(ticket_id):
        ticket = load_ticket(ticket_id)
        next_status = NEXT_STATUS.get(ticket["status"])
        if next_status is None or request.form.get("next_status") != next_status:
            return render_ticket_detail(ticket, status_error=STATUS_ERROR), 400

        cursor = get_db().execute(
            "UPDATE tickets SET status = ? WHERE id = ? AND status = ?",
            (next_status, ticket_id, ticket["status"]),
        )
        get_db().commit()
        if cursor.rowcount != 1:
            ticket = load_ticket(ticket_id)
            return render_ticket_detail(ticket, status_error=STATUS_ERROR), 409

        return redirect(url_for("ticket_detail", ticket_id=ticket_id), code=303)

    @app.post("/tickets/<int:ticket_id>/notes")
    def ticket_note(ticket_id):
        ticket = load_ticket(ticket_id)
        note_body = request.form.get("body", "")
        cleaned = note_body.strip()
        if not cleaned:
            return render_ticket_detail(
                ticket, note_body=note_body, note_error="اكتب ملاحظة المعالجة."
            ), 400
        if len(cleaned) > NOTE_LIMIT:
            return render_ticket_detail(
                ticket,
                note_body=note_body,
                note_error="يجب ألا تتجاوز الملاحظة 1000 حرف.",
            ), 400

        get_db().execute(
            "INSERT INTO ticket_notes (ticket_id, body, created_at) VALUES (?, ?, ?)",
            (ticket_id, cleaned, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        get_db().commit()
        return redirect(url_for("ticket_detail", ticket_id=ticket_id), code=303)

    return app


app = create_app()


if __name__ == "__main__":
    app.run()
