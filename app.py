"""
Fajr Online - Student Registration Automation
Fluent Forms (WordPress) -> Flask webhook -> Odoo + Moodle + Brevo

التسجيل التلقائي بيتم لقسم الأون لاين فقط. طلبات الأون سايت بتتجاهل
(القسم بيتواصل معاهم يدويًا).
"""
import hmac
import html
import logging
import os
import re
import secrets
import threading
import time

import requests
import sib_api_v3_sdk
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

# ===== Odoo Integration =====
from odoo_service import create_odoo_student, normalize_gender
from reset_tokens import create_token, verify_token

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fajr")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # الفورم صغيرة: نرفض أي طلب ضخم


def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


MOODLE_URL = (os.getenv("MOODLE_URL") or "").strip().rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")

# مهلة لكل طلب خارجي (Moodle / Brevo) عشان الـ worker مايعلقش
REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", "20")

# إلزامي: الـ webhook بيرفض أي طلب مفيهوش نفس الـ secret (هيدر X-Webhook-Token)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# لو Odoo كان واقع/مشغول وقت التسجيل: نعيد محاولة إنشاء الطالب فيه بعد كام دقيقة
# (التسجيل في Moodle والإيميل بيتموا في كل الأحوال). ODOO_RETRY_MAX_ATTEMPTS=0 بيوقف الميزة
ODOO_RETRY_DELAY_MINUTES = _int_env("ODOO_RETRY_DELAY_MINUTES", "20")
ODOO_RETRY_MAX_ATTEMPTS = _int_env("ODOO_RETRY_MAX_ATTEMPTS", "3")

# إعدادات Moodle
MOODLE_COURSE_ID = _int_env("MOODLE_COURSE_ID", "20")   # كورس تحديد المستوى
MOODLE_STUDENT_ROLE_ID = 5
MOODLE_GROUP_MALE = _int_env("MOODLE_GROUP_MALE", "2")
MOODLE_GROUP_FEMALE = _int_env("MOODLE_GROUP_FEMALE", "1")
# يجبر الطالب على تغيير كلمة المرور عند أول دخول
FORCE_PASSWORD_CHANGE = os.getenv("FORCE_PASSWORD_CHANGE", "true").strip().lower() != "false"

# نسخة (BCC) من كل إيميل بيتبعت للطالب بتروح لإيميل الفريق عشان يتابعوا. اختياري: لو فاضي مفيش نسخة.
# بنحطه في متغير بيئة (مش في الكود) لأن الريبو عام.
TEAM_COPY_EMAIL = (os.getenv("TEAM_COPY_EMAIL") or "").strip()

# رابط "نسيت كلمة المرور" في إيميل الطالب المسجل سابقًا بيفتح صفحة في الخدمة دي (مش في Moodle).
# على Render المتغير RENDER_EXTERNAL_URL بيتضبط تلقائيًا. لو مفيش عنوان، بيرجع رابط Moodle القديم.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
RESET_LINK_MINUTES = _int_env("RESET_LINK_MINUTES", "60")        # صلاحية الرابط
RESET_COOLDOWN_MINUTES = _int_env("RESET_COOLDOWN_MINUTES", "10")  # أقل فترة بين إعادتي تعيين لنفس الإيميل

SENDER_EMAIL = "info@fajr.com"
SENDER_NAME = "Fajr Online"

# أسماء الحقول المقبولة من الـ WPCode (الأول هو الأساسي)
STUDY_MODE_KEYS = ("study_mode", "studyMode", "study_type")
PLACEMENT_KEYS = ("placement_slot", "placement_date", "exam_date")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_REQUIRED_ENV = {
    "MOODLE_URL": MOODLE_URL,
    "MOODLE_TOKEN": MOODLE_TOKEN,
    "BREVO_API_KEY": BREVO_API_KEY,
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
    "ODOO_URL": os.getenv("ODOO_URL"),
    "ODOO_DB": os.getenv("ODOO_DB"),
    "ODOO_USERNAME": os.getenv("ODOO_USERNAME"),
    "ODOO_PASSWORD": os.getenv("ODOO_PASSWORD"),
}
_missing = [name for name, value in _REQUIRED_ENV.items() if not value]
if _missing:
    logger.error("Missing environment variables: %s", ", ".join(_missing))


# =====================================================================
# Helpers
# =====================================================================
def _s(value):
    """يحوّل القيمة لنص منظف (نص/رقم فقط)، وغير كده يرجع نص فاضي"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _first_value(data, keys):
    for key in keys:
        value = _s(data.get(key))
        if value:
            return value
    return ""


def _mask_email(email):
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


# ----- قسم الدراسة: أون لاين فقط -----
_ONLINE_VALUES = {"online", "أونلاين", "اونلاين", "عنبعد"}
_ONSITE_VALUES = {"onsite", "حضوري"}


def classify_study_mode(raw):
    """'online' / 'onsite' / None (فاضي أو غير مفهوم). بدون حساسية للحالة أو المسافات أو الشرطة."""
    key = re.sub(r"[\s\-_]+", "", str(raw or "").strip().lower())
    if key in _ONLINE_VALUES:
        return "online"
    if key in _ONSITE_VALUES:
        return "onsite"
    return None


# ----- الأمان -----
def _is_authorized():
    # الأفضل إرسال الـ secret في الهيدر؛ الـ ?token= مدعوم لكن بيظهر في لوج السيرفر
    supplied = request.headers.get("X-Webhook-Token") or request.args.get("token") or ""
    return hmac.compare_digest(supplied.encode("utf-8"), WEBHOOK_SECRET.encode("utf-8"))


# كلمة مرور بدون حروف متشابهة (l/I/1/O/0) عشان بتتبعت في إيميل
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "@#$%*!?"


def generate_password(length=12):
    """كلمة مرور عشوائية آمنة (secrets) تحقق سياسة Moodle الافتراضية"""
    pools = (_LOWER, _UPPER, _DIGITS, _SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(max(length, 8) - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# ----- Moodle -----
class MoodleError(Exception):
    pass


def moodle_call(wsfunction, params):
    """
    ينفذ دالة Moodle Web Service عبر POST (التوكن والإيميل مايظهروش في الرابط).
    بيرمي MoodleError برسالة آمنة (بدون التوكن) في أي فشل.
    """
    payload = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    payload.update(params)

    try:
        response = requests.post(
            f"{MOODLE_URL}/webservice/rest/server.php",
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        # `from None`: رسالة الاستثناء الأصلي ممكن تحتوي على عنوان الطلب
        raise MoodleError(f"{wsfunction}: request failed ({type(exc).__name__})") from None

    try:
        result = response.json()
    except ValueError:
        raise MoodleError(
            f"{wsfunction}: non-JSON response (HTTP {response.status_code})"
        ) from None

    if isinstance(result, dict) and "exception" in result:
        code = result.get("errorcode") or result.get("exception")
        raise MoodleError(f"{wsfunction}: {code}")

    return result  # بعض الدوال (enrol / group) بترجع null عند النجاح


# ----- إعادة محاولة Odoo -----
# القائمة في الذاكرة بس: بتضيع لو الخدمة اتعمللها restart أو deploy قبل ما المحاولة تحصل.
# بيانات الطالب مابتتكتبش على القرص ولا في اللوج.
_retry_lock = threading.Lock()
_pending_odoo_retries = {}  # email -> Timer


def _schedule_odoo_retry(email, student, attempt):
    """يجدول المحاولة رقم attempt لإنشاء الطالب في Odoo (مرة واحدة بس لكل إيميل في نفس الوقت)"""
    if ODOO_RETRY_MAX_ATTEMPTS <= 0 or attempt > ODOO_RETRY_MAX_ATTEMPTS:
        return
    with _retry_lock:
        if email in _pending_odoo_retries:
            return
        timer = threading.Timer(
            ODOO_RETRY_DELAY_MINUTES * 60, _run_odoo_retry, args=(email, student, attempt)
        )
        timer.daemon = True
        _pending_odoo_retries[email] = timer
        timer.start()
    logger.warning(
        "Odoo retry %d/%d scheduled in %d min for %s",
        attempt, ODOO_RETRY_MAX_ATTEMPTS, ODOO_RETRY_DELAY_MINUTES, _mask_email(email),
    )


def _run_odoo_retry(email, student, attempt):
    with _retry_lock:
        _pending_odoo_retries.pop(email, None)
    try:
        result = create_odoo_student(student)
    except Exception:
        logger.exception("Odoo retry crashed")
        result = {"success": False}
    if result.get("success"):
        logger.info(
            "Odoo retry %d/%d succeeded for %s (id=%s)",
            attempt, ODOO_RETRY_MAX_ATTEMPTS, _mask_email(email), result.get("odoo_student_id"),
        )
        return
    if result.get("retryable") and attempt < ODOO_RETRY_MAX_ATTEMPTS:
        logger.warning("Odoo retry %d/%d failed for %s", attempt, ODOO_RETRY_MAX_ATTEMPTS, _mask_email(email))
        _schedule_odoo_retry(email, student, attempt + 1)
    else:
        logger.error(
            "Odoo retry GAVE UP for %s after %d attempt(s): add this student to Odoo manually",
            _mask_email(email), attempt,
        )


def _find_exact_user(found, email):
    """
    البحث في Moodle ممكن يرجع نتائج قريبة (مثلا _ في الإيميل ممكن تتعامل كـ wildcard)
    فبنتأكد من تطابق الإيميل تمامًا.
    """
    if not isinstance(found, dict):
        return None
    for user in found.get("users") or []:
        if str(user.get("email", email)).strip().lower() == email:
            return user
    return None


def _exact_matches(found, email):
    """كل حسابات Moodle اللي إيميلها يطابق تمامًا (ممكن يبقى أكتر من حساب بنفس الإيميل)"""
    if not isinstance(found, dict):
        return []
    return [
        user for user in found.get("users") or []
        if isinstance(user, dict) and str(user.get("email", "")).strip().lower() == email
    ]


# ----- Brevo -----
_EMAIL_SHAPE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _team_copy_address(student_email):
    """إيميل الفريق اللي هتروحله نسخة (BCC)، أو None لو مش مضبوط/غلط/هو نفس إيميل الطالب"""
    if not TEAM_COPY_EMAIL:
        return None
    if not _EMAIL_SHAPE.match(TEAM_COPY_EMAIL):
        logger.warning("TEAM_COPY_EMAIL is not a valid email address - no team copy is sent")
        return None
    if TEAM_COPY_EMAIL.lower() == str(student_email).strip().lower():
        return None
    return TEAM_COPY_EMAIL


def send_email(to_email, to_name, subject, body, team_copy=True):
    """
    يرجع True لو الإيميل اتبعت للطالب، وFalse لو فشل لأي سبب (مع تسجيل السبب).
    لو TEAM_COPY_EMAIL مضبوط بتروح نسخة BCC منه لإيميل الفريق (إلا لو team_copy=False)؛
    ولو الإرسال فشل بسببها بنعيد إرسال إيميل الطالب من غيرها عشان الطالب مايتأثرش.
    """
    team_copy = _team_copy_address(to_email) if team_copy else None
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = BREVO_API_KEY

        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )

        def _send(bcc):
            fields = dict(
                to=[{"email": to_email, "name": to_name}],
                sender={"email": SENDER_EMAIL, "name": SENDER_NAME},
                subject=subject,
                html_content=body,
            )
            if bcc:
                fields["bcc"] = [{"email": bcc}]
            api_instance.send_transac_email(
                sib_api_v3_sdk.SendSmtpEmail(**fields), _request_timeout=REQUEST_TIMEOUT
            )

        try:
            _send(team_copy)
        except Exception as exc:
            if not team_copy:
                raise
            logger.warning(
                "Email with team copy failed (%s) - retrying without the team copy", type(exc).__name__
            )
            _send(None)
            team_copy = None
        logger.info("Email sent to %s%s", _mask_email(to_email), " (+team copy)" if team_copy else "")
        return True
    except Exception as exc:  # ApiException و timeouts وأي خطأ شبكة
        logger.error("Email error for %s (%s): %s", _mask_email(to_email), type(exc).__name__, exc)
        return False


# ----- قوالب الإيميلات (إنجليزي لأن الطلاب دارسو عربية والفورم إنجليزي) -----
# كل قيمة جاية من المستخدم بتتعمل escape
_WRAP_OPEN = '<div style="font-family: Arial; direction: ltr; text-align: left;">'
_LTR = 'style="direction: ltr; unicode-bidi: embed;"'


def _student_code_html(student_code):
    if not student_code:
        return ""
    return f"<p><b>Student code:</b> <span {_LTR}>{html.escape(student_code)}</span></p>"


def _reset_link(email):
    """رابط إعادة تعيين كلمة المرور الموقّع، أو '' لو عنوان الخدمة العام مش مضبوط"""
    if not PUBLIC_BASE_URL or not WEBHOOK_SECRET:
        return ""
    token = create_token(WEBHOOK_SECRET, email, RESET_LINK_MINUTES * 60)
    return f"{PUBLIC_BASE_URL}/reset-password?t={token}"


def _exists_body(first_name, student_code, reset_url=""):
    if reset_url:
        forgot_html = (
            "<p>Forgot your password? "
            f'<a href="{html.escape(reset_url, quote=True)}">Click here</a> '
            f"to get a new temporary password by email (the link is valid for {RESET_LINK_MINUTES} minutes).</p>"
        )
    else:
        forgot_html = (
            '<p>Forgot your password? '
            '<a href="https://learn.fajr.com/login/forgot_password.php">Click here</a></p>'
        )
    return f"""
    {_WRAP_OPEN}
        <h2>Hello {html.escape(first_name)},</h2>
        <p>You are already registered at <b>Fajr Online</b>.</p>
        {_student_code_html(student_code)}
        <p>You can log in here:
        <a href="https://learn.fajr.com">learn.fajr.com</a></p>
        {forgot_html}
        <p>If you have any problem, contact us at info@fajr.com</p>
    </div>
    """


def _reset_password_body(first_name, username, password):
    change_note = (
        "<p>You will be asked to change your password the first time you log in.</p>" if FORCE_PASSWORD_CHANGE else ""
    )
    return f"""
    {_WRAP_OPEN}
        <h2>Hello {html.escape(first_name)},</h2>
        <p>Your <b>Fajr Online</b> password has been reset.</p>
        <p><b>Username:</b> <span {_LTR}>{html.escape(username)}</span></p>
        <p><b>New password:</b> <span {_LTR}>{html.escape(password)}</span></p>
        {change_note}
        <p><b>Login link:</b>
        <a href="https://learn.fajr.com">learn.fajr.com</a></p>
        <p>If you did not ask for this, contact us at info@fajr.com</p>
    </div>
    """


def _welcome_body(first_name, email, password, student_code, placement_slot):
    if placement_slot:
        placement_html = (
            f"<p>Your placement test time: "
            f"<b><span {_LTR}>{html.escape(placement_slot)}</span></b></p>"
        )
    else:
        placement_html = "<p>Your placement test time: as selected in the registration form.</p>"

    change_note = (
        "<p>You will be asked to change your password the first time you log in.</p>" if FORCE_PASSWORD_CHANGE else ""
    )

    return f"""
    {_WRAP_OPEN}
        <h2>Hello {html.escape(first_name)},</h2>
        <p>You have been successfully registered at <b>Fajr Online</b>!</p>
        <hr>
        {_student_code_html(student_code)}
        <h3>Your login details:</h3>
        <p><b>Username:</b> <span {_LTR}>{html.escape(email)}</span></p>
        <p><b>Password:</b> <span {_LTR}>{html.escape(password)}</span></p>
        {change_note}
        <p><b>Login link:</b>
        <a href="https://learn.fajr.com">learn.fajr.com</a></p>
        <hr>
        {placement_html}
        <p>If you have any questions, contact us at info@fajr.com</p>
    </div>
    """


def _public_odoo(odoo_result):
    """نتيجة Odoo المسموح بإرجاعها للمستدعي (بدون نصوص الأخطاء الداخلية)"""
    return {
        "success": bool(odoo_result.get("success")),
        "already_existed": bool(odoo_result.get("already_existed")),
        "odoo_student_id": odoo_result.get("odoo_student_id"),
    }


def _response(status, warnings, user_id, odoo_result):
    body = {"status": status, "user_id": user_id, "odoo": _public_odoo(odoo_result)}
    if warnings:
        body["warnings"] = warnings
    return jsonify(body)


# ----- إعادة تعيين كلمة المرور (الرابط اللي في إيميل الطالب المسجل سابقًا) -----
# الحالة في الذاكرة بس (كل worker له نسخته): الرابط موقّع ومؤقت، والـ nonce والـ cooldown
# طبقة إضافية ضد تكرار الاستخدام. كلمة المرور الجديدة بتتبعت بالإيميل لصاحب الحساب بس
# ومابتتعرضش في الصفحة ولا بتتسجل في اللوج.
_reset_lock = threading.Lock()
_used_reset_nonces = {}  # nonce -> وقت انتهاء الرابط
_last_reset = {}         # email -> وقت آخر إعادة تعيين


def _purge_reset_state(now):
    for nonce, expires in list(_used_reset_nonces.items()):
        if expires < now:
            del _used_reset_nonces[nonce]
    for email, stamp in list(_last_reset.items()):
        if now - stamp > RESET_COOLDOWN_MINUTES * 60:
            del _last_reset[email]


def _reset_password_for(email):
    """
    ينفذ إعادة التعيين فعليًا. بيرجع واحدة من:
    ok | contact (مينفعش أوتوماتيك) | unavailable (Moodle مش شغال) | email_failed
    """
    try:
        found = moodle_call("core_user_get_users", {
            "criteria[0][key]": "email",
            "criteria[0][value]": email,
        })
    except MoodleError as exc:
        logger.error("Password reset: Moodle lookup failed: %s", exc)
        return "unavailable"

    matches = _exact_matches(found, email)
    if len(matches) != 1:
        # صفر حسابات، أو أكتر من حساب بنفس الإيميل: مانخمنش أنهي حساب نغير باسورده
        logger.warning("Password reset refused: %d Moodle account(s) match the email", len(matches))
        return "contact"

    user = matches[0]
    user_id = user.get("id")
    if user_id is None or user.get("suspended") or user.get("auth", "manual") != "manual":
        logger.warning("Password reset refused for user id=%s (suspended or not a manual account)", user_id)
        return "contact"

    password = generate_password()
    params = {"users[0][id]": user_id, "users[0][password]": password}
    if FORCE_PASSWORD_CHANGE:
        params["users[0][preferences][0][type]"] = "auth_forcepasswordchange"
        params["users[0][preferences][0][value]"] = "1"
    try:
        moodle_call("core_user_update_users", params)
    except MoodleError as exc:
        logger.error("Password reset: Moodle update failed (user id=%s): %s", user_id, exc)
        return "unavailable"
    logger.info("Password reset: Moodle password changed (user id=%s)", user_id)

    first_name = _s(user.get("firstname")) or "there"
    body = _reset_password_body(first_name, _s(user.get("username")) or email, password)
    # بدون نسخة للفريق: الإيميل ده فيه كلمة المرور
    for _attempt in (1, 2):
        if send_email(email, first_name, "Your Fajr Online password has been reset", body, team_copy=False):
            return "ok"
    logger.error(
        "Password reset: the email could not be sent (user id=%s) - the Moodle password WAS changed", user_id
    )
    return "email_failed"


_RESET_CSS = (
    "body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px;color:#222}"
    ".card{max-width:460px;margin:40px auto;background:#fff;border-radius:10px;padding:28px;"
    "box-shadow:0 2px 8px rgba(0,0,0,.08)}"
    "h1{font-size:20px;margin:0 0 12px}p{line-height:1.5}"
    "button{background:#1a7f5a;color:#fff;border:0;border-radius:6px;padding:12px 20px;"
    "font-size:16px;cursor:pointer}"
)


def _reset_page(title, message, status=200, token=None):
    """صفحة بسيطة بالإنجليزي. الـ token (لو موجود) بيتحط في فورم POST بزرار تأكيد"""
    form = ""
    if token:
        form = (
            '<form method="post" action="/reset-password">'
            f'<input type="hidden" name="t" value="{html.escape(token, quote=True)}">'
            '<button type="submit">Send me a new password</button></form>'
        )
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)}</title><style>{_RESET_CSS}</style></head>"
        f'<body><div class="card"><h1>{html.escape(title)}</h1><p>{message}</p>{form}</div></body></html>'
    )
    response = Response(page, status=status, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


# =====================================================================
# Routes
# =====================================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    # ---- 1) الحماية (إلزامية) ----
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET is not configured - rejecting request")
        return jsonify({"status": "error", "message": "server_not_configured"}), 503
    if not _is_authorized():
        # بنسجل الرفض (بدون القيمة المرسلة) عشان نعرف لو الـ secret في WordPress مختلف عن Render
        logger.warning("Rejected: missing or invalid webhook token (compare WEBHOOK_SECRET in WordPress and Render)")
        return jsonify({"status": "forbidden"}), 403

    # ---- 2) قراءة البيانات ----
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    # بنسجل أسماء الحقول فقط (بدون بيانات الطالب)
    # الحقل الفاضي بيوصل كمفتاح بدون قيمة، فنفصله عشان مايبانش كأنه اتبعت
    filled = sorted(k for k, v in data.items() if v not in ("", None, [], {}))
    empty = sorted(k for k in data if k not in filled)
    logger.info("Webhook received, fields: %s, empty: %s", filled, empty)

    # ---- 3) التسجيل لقسم الأون لاين فقط ----
    raw_mode = _first_value(data, STUDY_MODE_KEYS)
    study_mode = classify_study_mode(raw_mode)
    if study_mode == "onsite":
        logger.info("Skipped: on-site submission (handled manually by the department)")
        return jsonify({"status": "skipped", "reason": "onsite"}), 200
    if study_mode is None:
        reason = "study_mode_unrecognized" if raw_mode else "study_mode_missing"
        logger.error("Rejected: %s (value=%r)", reason, raw_mode[:40])
        return jsonify({"status": "error", "message": reason}), 400

    # ---- 4) تنظيف البيانات ----
    first_name = _s(data.get("first_name"))
    last_name = _s(data.get("last_name"))
    email = _s(data.get("email")).lower()
    gender = _s(data.get("gender"))
    arabic_name = _s(data.get("arabic_name"))
    mobile = _s(data.get("mobile"))
    birth_date = _s(data.get("birth_date")) or _s(data.get("date_of_birth"))
    placement_slot = _first_value(data, PLACEMENT_KEYS)

    if not EMAIL_RE.match(email):
        logger.error("Rejected: missing or invalid email")
        return jsonify({"status": "error", "message": "a valid email is required"}), 400

    warnings = []

    # ---- 5) Odoo: إنشاء/تأكيد الطالب ----
    # بيحصل بغض النظر عن حالة الطالب في Moodle (وجوده في Moodle مش معناه إنه في Odoo)
    # خطأ Odoo مايوقفش التسجيل في Moodle، بس بيظهر في warnings واللوج
    odoo_student = {
        "first_name": first_name,
        "last_name": last_name,
        "arabic_name": arabic_name,
        "mobile": mobile,
        "email": email,
        "birth_date": birth_date,
        "gender": gender,
    }
    try:
        odoo_result = create_odoo_student(odoo_student)
    except Exception:
        logger.exception("Odoo call crashed")
        odoo_result = {"success": False}

    if odoo_result.get("success"):
        if odoo_result.get("already_existed"):
            logger.info("Odoo: student already existed (id=%s)", odoo_result.get("odoo_student_id"))
        else:
            logger.info("Odoo: student created (id=%s)", odoo_result.get("odoo_student_id"))
    else:
        logger.error("Odoo failed: %s", odoo_result.get("error"))
        warnings.append("odoo_failed")
        if odoo_result.get("retryable"):  # Odoo واقع/مشغول: هنعيد المحاولة بعد شوية
            _schedule_odoo_retry(email, odoo_student, 1)

    student_code = odoo_result.get("gr_no") or ""

    # ---- 6) Moodle: هل الطالب موجود؟ ----
    # لو البحث نفسه فشل مانكملش (مانفترضش إن الطالب مش موجود)
    try:
        found = moodle_call("core_user_get_users", {
            "criteria[0][key]": "email",
            "criteria[0][value]": email,
        })
    except MoodleError as exc:
        logger.error("Moodle lookup failed: %s", exc)
        return jsonify({"status": "error", "message": "moodle_unavailable"}), 502

    existing_user = _find_exact_user(found, email)
    if existing_user:
        user_id = existing_user["id"]
        logger.info("Moodle: user already exists (id=%s)", user_id)
        if not send_email(email, first_name, "You are already registered at Fajr Online",
                          _exists_body(first_name, student_code, _reset_link(email))):
            warnings.append("email_failed")
        return _response("exists", warnings, user_id, odoo_result)

    # ---- 7) Moodle: إنشاء مستخدم جديد ----
    password = generate_password()
    user_payload = {
        "users[0][firstname]": first_name,
        "users[0][lastname]": last_name,
        "users[0][email]": email,
        "users[0][username]": email,
        "users[0][password]": password,
    }
    if FORCE_PASSWORD_CHANGE:
        user_payload["users[0][preferences][0][type]"] = "auth_forcepasswordchange"
        user_payload["users[0][preferences][0][value]"] = "1"

    try:
        created = moodle_call("core_user_create_users", user_payload)
    except MoodleError as exc:
        logger.error("Moodle user creation failed: %s", exc)
        return jsonify({"status": "error", "message": "moodle_create_failed"}), 400

    if not isinstance(created, list) or not created:
        logger.error("Moodle user creation returned an unexpected response")
        return jsonify({"status": "error", "message": "moodle_create_failed"}), 400

    user_id = created[0]["id"]
    logger.info("Moodle: new user created (id=%s)", user_id)

    # ---- 8) التسجيل في كورس تحديد المستوى ----
    try:
        moodle_call("enrol_manual_enrol_users", {
            "enrolments[0][roleid]": MOODLE_STUDENT_ROLE_ID,
            "enrolments[0][userid]": user_id,
            "enrolments[0][courseid]": MOODLE_COURSE_ID,
        })
        logger.info("Moodle: enrolled in the placement course")
    except MoodleError as exc:
        logger.error("Moodle enrolment failed (user id=%s): %s", user_id, exc)
        warnings.append("enrol_failed")

    # ---- 9) الإضافة للـ Group حسب الجنس ----
    group_id = {
        "male": MOODLE_GROUP_MALE,
        "female": MOODLE_GROUP_FEMALE,
    }.get(normalize_gender(gender))

    if group_id is None:
        # مانحطش الطالب في مجموعة غلط: أحسن من مجموعة الإناث بالافتراض
        # بنسجل القيمة بصيغة ascii عشان الحروف الخفية (زي علامة RTL) تبان، ومقصوصة لـ 40 حرف
        logger.error(
            "Unrecognized gender value %s - user id=%s was NOT added to any group",
            ascii(gender[:40]), user_id,
        )
        warnings.append("gender_unknown")
    else:
        try:
            moodle_call("core_group_add_group_members", {
                "members[0][groupid]": group_id,
                "members[0][userid]": user_id,
            })
            logger.info("Moodle: added to group %s", group_id)
        except MoodleError as exc:
            logger.error("Moodle group assignment failed (user id=%s): %s", user_id, exc)
            warnings.append("group_failed")

    # ---- 10) إيميل الترحيب ----
    if not send_email(email, first_name, "Welcome to Fajr Online 🎉",
                      _welcome_body(first_name, email, password, student_code, placement_slot)):
        # كلمة المرور مش متخزنة في أي مكان: لازم يعمل "نسيت كلمة المرور" أو يتم إعادة تعيينها
        warnings.append("email_failed")

    return _response("success" if not warnings else "partial", warnings, user_id, odoo_result)


if __name__ == '__main__':
    # للتشغيل المحلي فقط - على السيرفر بيشتغل gunicorn
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='127.0.0.1', port=int(os.getenv('PORT', 5003)))


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    """
    GET: صفحة تأكيد بس (مفيهاش أي تغيير) عشان برامج فحص الروابط في الإيميل ماتشغلش إعادة التعيين.
    POST: بينفذ إعادة التعيين ويبعت كلمة المرور الجديدة بالإيميل لصاحب الحساب.
    """
    if not WEBHOOK_SECRET:
        return _reset_page("Service unavailable", "Please contact us at info@fajr.com.", 503)

    token = request.form.get("t", "") if request.method == "POST" else request.args.get("t", "")
    payload = verify_token(WEBHOOK_SECRET, token)
    invalid = (
        "This link is invalid, has expired, or has already been used. "
        "Please contact us at info@fajr.com and we will help you."
    )
    if payload is None:
        return _reset_page("Link not valid", invalid, 400)

    email, nonce = payload["e"], payload["n"]

    if request.method == "GET":
        with _reset_lock:
            if nonce in _used_reset_nonces:
                return _reset_page("Link not valid", invalid, 400)
        return _reset_page(
            "Reset your password",
            "Click the button below and we will email you a new temporary password. "
            "Your current password will stop working.",
            token=token,
        )

    # ---- POST ----
    now = time.time()
    with _reset_lock:
        _purge_reset_state(now)
        if nonce in _used_reset_nonces:
            return _reset_page("Link not valid", invalid, 400)
        if now - _last_reset.get(email, 0) < RESET_COOLDOWN_MINUTES * 60:
            logger.info("Password reset skipped: cooldown is active")
            return _reset_page(
                "Please check your email",
                "A new password was requested a moment ago. Check your inbox (and spam folder), "
                f"or try again in {RESET_COOLDOWN_MINUTES} minutes.",
                429,
            )
        _used_reset_nonces[nonce] = payload["x"]
        _last_reset[email] = now

    outcome = _reset_password_for(email)

    if outcome in ("unavailable", "email_failed"):
        # نسمح بمحاولة تانية بنفس الرابط (لسه صالح لحد انتهاء مدته)
        with _reset_lock:
            _used_reset_nonces.pop(nonce, None)
            _last_reset.pop(email, None)
        if outcome == "unavailable":
            return _reset_page(
                "Please try again later",
                "We could not reach the learning platform. Please try again in a few minutes "
                "or contact us at info@fajr.com.",
                503,
            )
        return _reset_page(
            "We could not send the email",
            "Please try again in a few minutes or contact us at info@fajr.com.",
            503,
        )
    if outcome == "contact":
        return _reset_page(
            "We could not reset this account automatically",
            "Please contact us at info@fajr.com and we will help you.",
            409,
        )
    return _reset_page(
        "Check your email",
        "We have sent a new temporary password to the email address of your account. "
        'Then log in at <a href="https://learn.fajr.com">learn.fajr.com</a>.',
    )
