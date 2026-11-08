"""
odoo_service.py
الاتصال بـ Odoo وإنشاء Student جديد (op.student) بعد التسجيل في Moodle
"""
import datetime
import logging
import os
import re
import xmlrpc.client

from dotenv import load_dotenv

# تحميل المتغيرات من ملف .env
load_dotenv()

logger = logging.getLogger(__name__)


def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


# ===== إعدادات الاتصال: كلها من متغيرات البيئة، بدون قيم افتراضية داخل الكود =====
# (الريبو عام، فمانحطش هنا عنوان النظام ولا اسم القاعدة ولا اسم المستخدم)
ODOO_URL = os.getenv("ODOO_URL", "").strip().rstrip("/")
ODOO_DB = os.getenv("ODOO_DB", "").strip()
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "").strip()
# ممكن تحط هنا API key بدل كلمة المرور (Odoo بيقبل الاتنين في XML-RPC)
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "").strip()
ODOO_TIMEOUT = _int_env("ODOO_TIMEOUT", "30")  # ثواني

# ===== Defaults الثابتة (قسم الأون لاين) - ممكن تتغير من متغيرات البيئة =====
ODOO_CATEGORY_ID = _int_env("ODOO_CATEGORY_ID", "40")    # Online Group (op.category)
ODOO_PRICELIST_ID = _int_env("ODOO_PRICELIST_ID", "75")  # Group OnLine (product.pricelist)
# الفرع: "branch" في op.student بيشاور على account.analytic.account
# راجع إن الرقم ده هو فرع الـ Online فعلًا. اكتب 0 لو عايز تتجاهل الفرع.
ODOO_BRANCH_ID = _int_env("ODOO_BRANCH_ID", "548")

# اسم الحقل اللي فيه كود الطالب (زي FM-2005-NCR-001634) في op.student.
# الكود القديم كان بيقرأ name_seq، بينما التعليق القديم بيذكر gr_no.
# لو الكود الصحيح موجود في حقل تاني، غيّره من متغير البيئة ODOO_STUDENT_CODE_FIELD.
STUDENT_CODE_FIELD = os.getenv("ODOO_STUDENT_CODE_FIELD", "name_seq").strip() or "name_seq"

# صيغة التاريخ لو جاي بشكل 12/03/2005:
#   dmy = يوم/شهر/سنة (الافتراضي)   |   mdy = شهر/يوم/سنة
DATE_ORDER = os.getenv("BIRTH_DATE_ORDER", "dmy").strip().lower()


class OdooConnectionError(Exception):
    pass


# ===== تطبيع الجنس (مشترك مع app.py عشان نفس القاعدة تتطبق في Moodle و Odoo) =====
_MALE = {"male", "m", "ذكر"}
_FEMALE = {"female", "f", "أنثى", "انثى"}
_ODOO_GENDER = {"male": "m", "female": "f"}  # قيم الـ selection في op.student


def normalize_gender(raw_gender):
    """يرجع 'male' أو 'female' أو None لو القيمة مش مفهومة (بدون حساسية لحالة الأحرف)"""
    key = str(raw_gender or "").strip().lower()
    if key in _MALE:
        return "male"
    if key in _FEMALE:
        return "female"
    return None


def _normalize_gender(raw_gender):
    return _ODOO_GENDER.get(normalize_gender(raw_gender), False)


# ===== تطبيع التاريخ =====
# \d بتقبل الأرقام العربية كمان (٢٥/١٢/٢٠٠٥)
_ISO_DATE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$")
_LOCAL_DATE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[ T].*)?$")


def _normalize_date(raw_date):
    """
    Odoo بياخد التاريخ بصيغة YYYY-MM-DD. بيرجع False لو التاريخ مش مفهوم أو غير صحيح
    (مثلا 31/02/2005) بدل ما يبعت تاريخ غلط بصمت.
    """
    if not raw_date:
        return False
    text = str(raw_date).strip()

    match = _ISO_DATE.match(text)
    if match:
        year, month, day = (int(x) for x in match.groups())
    else:
        match = _LOCAL_DATE.match(text)
        if not match:
            logger.warning("Birth date ignored: unrecognized format")
            return False
        first, second, year = (int(x) for x in match.groups())
        day, month = (second, first) if DATE_ORDER == "mdy" else (first, second)

    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        logger.warning("Birth date ignored: not a valid calendar date")
        return False


# ===== الاتصال =====
class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=ODOO_TIMEOUT, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=ODOO_TIMEOUT, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def is_configured():
    return all([ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD])


def _proxy(path):
    """ServerProxy مع timeout عشان الـ worker مايفضلش معلق"""
    transport = (
        _TimeoutSafeTransport() if ODOO_URL.lower().startswith("https")
        else _TimeoutTransport()
    )
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}{path}", transport=transport)


_uid_cache = None


def _get_connection():
    """
    يرجع (uid, models_proxy) — بيعمل authenticate مرة ويكاش الـ uid
    """
    global _uid_cache

    if not is_configured():
        raise OdooConnectionError(
            "Odoo settings are incomplete (ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_PASSWORD)"
        )

    common = _proxy("/xmlrpc/2/common")
    if _uid_cache is None:
        _uid_cache = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not _uid_cache:
            _uid_cache = None
            raise OdooConnectionError("Odoo authentication failed - check ODOO_USERNAME / ODOO_PASSWORD")

    models = _proxy("/xmlrpc/2/object")
    return _uid_cache, models


def find_student_by_email(email):
    """
    يدور على طالب موجود بالفعل بنفس الإيميل في Odoo عشان نتجنب تكرار
    """
    uid, models = _get_connection()
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "op.student", "search_read",
        [[["email", "=", email]]],
        {"fields": ["id", "name", "email", STUDENT_CODE_FIELD], "limit": 1},
    )
    return existing[0] if existing else None


def create_odoo_student(form_data):
    """
    form_data المتوقع فيها (من الفورم):
        first_name, last_name, arabic_name (اختياري), mobile,
        email, birth_date, gender

    بترجع dict فيها success / odoo_student_id / gr_no / already_existed / error
    (الـ error للّوج فقط - مايتبعتش للي بيستدعي الـ webhook)
    """
    try:
        first_name = str(form_data.get("first_name") or "").strip()
        last_name = str(form_data.get("last_name") or "").strip()
        email = str(form_data.get("email") or "").strip().lower()
        mobile = str(form_data.get("mobile") or "").strip()
        arabic_name = str(form_data.get("arabic_name") or "").strip()
        birth_date_raw = form_data.get("birth_date")
        gender_raw = form_data.get("gender")

        if not email or not first_name:
            return {"success": False, "error": "email أو first_name مفقودين"}

        # تجنب تكرار الطالب لو موجود بالفعل
        existing = find_student_by_email(email)
        if existing:
            logger.info("Odoo: student already exists id=%s", existing["id"])
            return {
                "success": True,
                "odoo_student_id": existing["id"],
                "gr_no": existing.get(STUDENT_CODE_FIELD) or "",
                "already_existed": True,
            }

        full_name = arabic_name or f"{first_name} {last_name}".strip()

        student_vals = {
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "arabic_name": arabic_name or False,
            "mobile": mobile,
            "email": email,
            "birth_date": _normalize_date(birth_date_raw),
            "gender": _normalize_gender(gender_raw),
            "category_id": ODOO_CATEGORY_ID,
            "price_list": ODOO_PRICELIST_ID,
        }

        # نضيف الفرع بس لو الـ ID متحدد فعلا (عشان منبعتش False غلط)
        if ODOO_BRANCH_ID:
            student_vals["branch"] = ODOO_BRANCH_ID

        uid, models = _get_connection()
        student_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "op.student", "create",
            [student_vals],
        )

        # كود الطالب بيتحدد تلقائي بواسطة الـ sequence في Odoo وقت الـ create
        # فبنعمل read تاني بعد الإنشاء عشان نجيبه
        gr_no = ""
        try:
            created = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "op.student", "read",
                [[student_id]],
                {"fields": [STUDENT_CODE_FIELD]},
            )
            if created:
                gr_no = created[0].get(STUDENT_CODE_FIELD) or ""
        except xmlrpc.client.Fault as e:
            logger.warning("Could not read student code after creation: %s", e)

        logger.info("Odoo: student created id=%s", student_id)
        return {
            "success": True,
            "odoo_student_id": student_id,
            "gr_no": gr_no,
            "already_existed": False,
        }

    except xmlrpc.client.Fault as e:
        logger.error("Odoo XML-RPC fault: %s", e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Odoo unexpected error (%s): %s", type(e).__name__, e)
        return {"success": False, "error": str(e)}
