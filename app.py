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

import requests
import sib_api_v3_sdk
from dotenv import load_dotenv
from flask import Flask, jsonify, request

# ===== Odoo Integration =====
from odoo_service import create_odoo_student, normalize_gender

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

# إعدادات Moodle
MOODLE_COURSE_ID = _int_env("MOODLE_COURSE_ID", "20")   # كورس تحديد المستوى
MOODLE_STUDENT_ROLE_ID = 5
MOODLE_GROUP_MALE = _int_env("MOODLE_GROUP_MALE", "2")
MOODLE_GROUP_FEMALE = _int_env("MOODLE_GROUP_FEMALE", "1")
# يجبر الطالب على تغيير كلمة المرور عند أول دخول
FORCE_PASSWORD_CHANGE = os.getenv("FORCE_PASSWORD_CHANGE", "true").strip().lower() != "false"

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


# ----- Brevo -----
def send_email(to_email, to_name, subject, body):
    """يرجع True لو الإيميل اتبعت، وFalse لو فشل لأي سبب (مع تسجيل السبب)"""
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = BREVO_API_KEY

        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )
        message = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": to_email, "name": to_name}],
            sender={"email": SENDER_EMAIL, "name": SENDER_NAME},
            subject=subject,
            html_content=body,
        )
        api_instance.send_transac_email(message, _request_timeout=REQUEST_TIMEOUT)
        logger.info("Email sent to %s", _mask_email(to_email))
        return True
    except Exception as exc:  # ApiException و timeouts وأي خطأ شبكة
        logger.error("Email error for %s (%s): %s", _mask_email(to_email), type(exc).__name__, exc)
        return False


# ----- قوالب الإيميلات (كل قيمة جاية من المستخدم بتتعمل escape) -----
_WRAP_OPEN = '<div style="font-family: Arial; direction: rtl; text-align: right;">'
_LTR = 'style="direction: ltr; unicode-bidi: embed;"'


def _student_code_html(student_code):
    if not student_code:
        return ""
    return f"<p><b>كود الطالب:</b> <span {_LTR}>{html.escape(student_code)}</span></p>"


def _exists_body(first_name, student_code):
    return f"""
    {_WRAP_OPEN}
        <h2>مرحباً {html.escape(first_name)}،</h2>
        <p>أنت مسجل بالفعل في <b>Fajr Online</b>.</p>
        {_student_code_html(student_code)}
        <p>يمكنك الدخول من هنا:
        <a href="https://learn.fajr.com">learn.fajr.com</a></p>
        <p>لو نسيت الباسورد:
        <a href="https://learn.fajr.com/login/forgot_password.php">اضغط هنا</a></p>
        <p>لو في مشكلة تواصل معنا على info@fajr.com</p>
    </div>
    """


def _welcome_body(first_name, email, password, student_code, placement_slot):
    if placement_slot:
        placement_html = (
            f"<p>موعد اختبار تحديد المستوى الذي اخترته: "
            f"<b><span {_LTR}>{html.escape(placement_slot)}</span></b></p>"
        )
    else:
        placement_html = "<p>موعد اختبار تحديد المستوى: حسب الموعد الذي اخترته في نموذج التسجيل.</p>"

    change_note = (
        "<p>ستُطلب منك تغيير كلمة المرور عند أول دخول.</p>" if FORCE_PASSWORD_CHANGE else ""
    )

    return f"""
    {_WRAP_OPEN}
        <h2>مرحباً {html.escape(first_name)}،</h2>
        <p>تم تسجيلك بنجاح في <b>Fajr Online</b>!</p>
        <hr>
        {_student_code_html(student_code)}
        <h3>بيانات الدخول:</h3>
        <p><b>Username:</b> <span {_LTR}>{html.escape(email)}</span></p>
        <p><b>Password:</b> <span {_LTR}>{html.escape(password)}</span></p>
        {change_note}
        <p><b>رابط الدخول:</b>
        <a href="https://learn.fajr.com">learn.fajr.com</a></p>
        <hr>
        {placement_html}
        <p>لو في أي استفسار تواصل معنا على info@fajr.com</p>
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
    logger.info("Webhook received, fields: %s", sorted(data.keys()))

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
    try:
        odoo_result = create_odoo_student({
            "first_name": first_name,
            "last_name": last_name,
            "arabic_name": arabic_name,
            "mobile": mobile,
            "email": email,
            "birth_date": birth_date,
            "gender": gender,
        })
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
        if not send_email(email, first_name, "أنت مسجل بالفعل في Fajr Online",
                          _exists_body(first_name, student_code)):
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
    if not send_email(email, first_name, "مرحباً بك في Fajr Online 🎉",
                      _welcome_body(first_name, email, password, student_code, placement_slot)):
        # كلمة المرور مش متخزنة في أي مكان: لازم يعمل "نسيت كلمة المرور" أو يتم إعادة تعيينها
        warnings.append("email_failed")

    return _response("success" if not warnings else "partial", warnings, user_id, odoo_result)


if __name__ == '__main__':
    # للتشغيل المحلي فقط - على السيرفر بيشتغل gunicorn
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='127.0.0.1', port=int(os.getenv('PORT', 5003)))
