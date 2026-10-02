import hmac
import ipaddress
import math
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


CATEGORIES = ("أجهزة", "برامج", "شبكة", "أخرى")
FIELD_LIMITS = {"title": 120, "description": 2000, "requester_name": 80}
NOTE_LIMIT = 1000
NEXT_STATUS = {"جديد": "قيد المعالجة", "قيد المعالجة": "تم الحل"}
STATUS_ERROR = "انتقال الحالة غير متاح."
SAUDI_TIMEZONE = timezone(timedelta(hours=3))

CSRF_FIELD = "csrf_token"
CSRF_EXPIRED = "انتهت صلاحية النموذج لأنه بقي مفتوحًا مدة طويلة، ولم يُحفظ شيء. راجع البيانات ثم أعد الإرسال."
CSRF_REJECTED = (
    "تعذّر التحقق من مصدر النموذج، ولم يُحفظ شيء. "
    "أعد الإرسال من هذه الصفحة، وتأكد من تفعيل ملفات تعريف الارتباط في المتصفح."
)
CLIENT_UNKNOWN = "تعذّر تحديد مصدر الطلب، ولم يُحفظ شيء. حدّث الصفحة ثم أعد المحاولة."
RATE_LIMITED = "أُرسلت طلبات كثيرة خلال وقت قصير، ولم يُحفظ شيء. انتظر قليلًا ثم أعد المحاولة."
TICKETS_FULL = "وصل العرض التجريبي إلى الحد الأقصى للبلاغات ({limit})، فلا يمكن إضافة بلاغ جديد الآن."
NOTES_FULL = "وصل هذا البلاغ إلى الحد الأقصى للملاحظات ({limit})، فلا يمكن إضافة ملاحظة جديدة."
ERROR_PAGES = {
    400: ("تعذّر قراءة الطلب", "وصل الطلب بصيغة غير متوقعة، ولم يُحفظ شيء. ارجع إلى الصفحة السابقة وأعد المحاولة."),
    404: ("الصفحة غير موجودة", "لم نجد الصفحة أو البلاغ المطلوب. تأكد من الرابط أو ارجع إلى قائمة البلاغات."),
    405: ("لا يمكن فتح هذا الرابط مباشرة", "هذا الرابط يستقبل النماذج فقط. ارجع إلى البلاغات وتابع من صفحة البلاغ."),
    413: ("حجم الطلب أكبر من المسموح", "البيانات المرسلة أكبر من الحد المسموح، ولم يُحفظ شيء. اختصر النص ثم أعد المحاولة."),
    500: ("حدث خطأ غير متوقع", "تعذّر إكمال الطلب الآن. حاول مرة أخرى بعد قليل."),
}


def saudi_time(value):
    """Format a stored UTC timestamp for display in Saudi time only."""
    return datetime.fromisoformat(value).astimezone(SAUDI_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def create_app(test_config=None):
    app = Flask(__name__)
    app.jinja_env.filters["saudi_time"] = saudi_time
    production = os.environ.get("HELPDESK_ENV", "").strip().lower() == "production"
    app.config.from_mapping(
        PRODUCTION=production,
        SECRET_KEY=os.environ.get("HELPDESK_SECRET_KEY", ""),
        DATABASE=os.environ.get("HELPDESK_DATABASE")
        or os.path.join(app.instance_path, "helpdesk.sqlite3"),
        # The largest valid form is a few KB; bigger bodies are refused before parsing.
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Secure cookies need HTTPS, so local runs on http://127.0.0.1 keep working without them.
        SESSION_COOKIE_SECURE=production,
        CSRF_TIME_LIMIT=60 * 60,
        WRITE_RATE_LIMIT=10,
        WRITE_RATE_WINDOW=60,
        MAX_TICKETS=200,
        MAX_NOTES_PER_TICKET=30,
    )
    if test_config:
        app.config.update(test_config)

    if app.config["PRODUCTION"]:
        if len(app.config["SECRET_KEY"] or "") < 32:
            raise RuntimeError("وضع النشر يتطلب HELPDESK_SECRET_KEY بطول 32 حرفًا على الأقل.")
        if not os.environ.get("HELPDESK_DATABASE"):
            raise RuntimeError("وضع النشر يتطلب مسارًا مطلقًا لقاعدة SQLite في HELPDESK_DATABASE.")
        app.debug = False
    elif not app.config["SECRET_KEY"]:
        # Local runs only: a per-process key keeps forms working; open forms expire on restart.
        app.config["SECRET_KEY"] = secrets.token_hex(32)

    if not os.path.isabs(app.config["DATABASE"]):
        raise RuntimeError(f"مسار قاعدة البيانات يجب أن يكون مطلقًا: {app.config['DATABASE']}")
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @contextmanager
    def write_transaction():
        """Serialize writers so a limit check and the write it guards cannot interleave."""
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        db.commit()

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
        # Recent form submissions per client for rate limiting. `client` is an HMAC of the
        # address, never the raw IP, and rows older than the window are deleted on each write.
        get_db().execute(
            """
            CREATE TABLE IF NOT EXISTS write_attempts (
                client TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        get_db().execute(
            "CREATE INDEX IF NOT EXISTS write_attempts_by_client ON write_attempts (client, created_at)"
        )
        get_db().commit()

    def csrf_serializer():
        return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="helpdesk-csrf")

    def csrf_token():
        """Signed, timestamped token tied to a random value kept in the session cookie."""
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return csrf_serializer().dumps(session["csrf"])

    app.jinja_env.globals["csrf_token"] = csrf_token

    def csrf_error():
        """Arabic reason when the submitted token is missing, expired, or not this session's."""
        sent = request.form.get(CSRF_FIELD, "")
        expected = session.get("csrf")
        if not sent or not expected:
            return CSRF_REJECTED
        try:
            value = csrf_serializer().loads(sent, max_age=app.config["CSRF_TIME_LIMIT"])
        except SignatureExpired:
            return CSRF_EXPIRED
        except BadSignature:
            return CSRF_REJECTED
        if not isinstance(value, str) or not hmac.compare_digest(value, expected):
            return CSRF_REJECTED
        return None

    def client_address():
        """The visitor address for rate limiting, or None when it cannot be trusted.

        Locally this is the socket address. Behind PythonAnywhere only the last
        X-Forwarded-For entry is guaranteed: the load balancer appends the address it
        received the request from, while earlier entries come from the client. The raw
        header is split on commas instead of parsed, because a client-supplied unclosed
        quote makes list parsing fail and would leave the shared load-balancer address.
        """
        if not app.config["PRODUCTION"]:
            return request.remote_addr or "unknown"
        last_hop = request.headers.get("X-Forwarded-For", "").rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(last_hop))
        except ValueError:
            return None

    def rate_limit_wait(address):
        """Record this submission; return seconds to wait if the client is over its limit."""
        limit, window = app.config["WRITE_RATE_LIMIT"], app.config["WRITE_RATE_WINDOW"]
        client = hmac.new(app.config["SECRET_KEY"].encode(), address.encode(), "sha256").hexdigest()
        now = time.time()
        with write_transaction() as db:
            db.execute("DELETE FROM write_attempts WHERE created_at <= ?", (now - window,))
            used, oldest = db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM write_attempts WHERE client = ?", (client,)
            ).fetchone()
            if used >= limit:
                return max(1, math.ceil(oldest + window - now))
            db.execute("INSERT INTO write_attempts (client, created_at) VALUES (?, ?)", (client, now))
        return 0

    def request_problem():
        """Check CSRF, the visitor address, then the write rate; returns (message, status, headers) or None."""
        message = csrf_error()
        if message:
            return message, 400, {}
        address = client_address()
        if address is None:
            # Refuse rather than fall back to the load balancer address shared by everyone.
            return CLIENT_UNKNOWN, 400, {}
        wait = rate_limit_wait(address)
        if wait:
            return RATE_LIMITED, 429, {"Retry-After": str(wait)}
        return None

    def render_error(error):
        title, message = ERROR_PAGES[error.code]
        headers = [(key, value) for key, value in error.get_headers() if key.lower() != "content-type"]
        page = render_template("error.html", code=error.code, title=title, message=message)
        return page, error.code, headers

    for code in ERROR_PAGES:
        app.register_error_handler(code, render_error)

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
            problem = request_problem()
            if problem:
                message, status, headers = problem
                return render_ticket_new(values, form_error=message), status, headers
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
                with write_transaction() as db:
                    full = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] >= app.config["MAX_TICKETS"]
                    if not full:
                        cursor = db.execute(
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
                if full:
                    message = TICKETS_FULL.format(limit=app.config["MAX_TICKETS"])
                    return render_ticket_new(values, form_error=message), 409
                return redirect(url_for("ticket_detail", ticket_id=cursor.lastrowid))

        return render_ticket_new(values, errors)

    def render_ticket_new(values, errors=None, form_error=None):
        return render_template(
            "ticket_new.html",
            values=values,
            errors=errors or {},
            form_error=form_error,
            categories=CATEGORIES,
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

    def render_ticket_detail(
        ticket, note_body="", note_error=None, status_error=None, note_form_error=None
    ):
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
            note_form_error=note_form_error,
            status_error=status_error,
        )

    @app.post("/tickets/<int:ticket_id>/status")
    def ticket_status(ticket_id):
        ticket = load_ticket(ticket_id)
        problem = request_problem()
        if problem:
            message, status, headers = problem
            return render_ticket_detail(ticket, status_error=message), status, headers
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
        problem = request_problem()
        if problem:
            message, status, headers = problem
            return render_ticket_detail(ticket, note_body=note_body, note_form_error=message), status, headers
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

        with write_transaction() as db:
            full = db.execute(
                "SELECT COUNT(*) FROM ticket_notes WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()[0] >= app.config["MAX_NOTES_PER_TICKET"]
            if not full:
                db.execute(
                    "INSERT INTO ticket_notes (ticket_id, body, created_at) VALUES (?, ?, ?)",
                    (ticket_id, cleaned, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
        if full:
            message = NOTES_FULL.format(limit=app.config["MAX_NOTES_PER_TICKET"])
            return render_ticket_detail(ticket, note_body=note_body, note_form_error=message), 409
        return redirect(url_for("ticket_detail", ticket_id=ticket_id), code=303)

    return app


if __name__ == "__main__":
    create_app().run()
