"""
odoo_service.py
الاتصال بـ Odoo وإنشاء Student جديد (op.student) بعد التسجيل في Moodle
"""
import xmlrpc.client
import os
import logging
from dotenv import load_dotenv

# تحميل المتغيرات من ملف .env
load_dotenv()

logger = logging.getLogger(__name__)

ODOO_URL = os.getenv("ODOO_URL", "https://sys.fajr.com").strip()
ODOO_DB = os.getenv("ODOO_DB", "Fajr").strip()
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "develop_register@fajr.com").strip()
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "").strip()
ODOO_TIMEOUT = int(os.getenv("ODOO_TIMEOUT", "30"))  # ثواني

# ===== Defaults الثابتة =====
ODOO_CATEGORY_ID = 40    # Online Group (op.category) - مؤكد ✅
ODOO_PRICELIST_ID = 75   # Group OnLine (product.pricelist) - مؤكد ✅

# كود الطالب (زي FM-2005-NCR-001634) بيتاخد من حقل "gr_no" في op.student
# (ده الحقل القياسي في OpenEduCat لرقم/كود الطالب التسلسلي)
# لو عندك حقل تاني بالاسم ده، غيّر "gr_no" في find_student_by_email و create_odoo_student

# الفرع: مش موديل منفصل زي ما افترضنا الأول
# لقينا إن "branch" في op.student بيشاور على account.analytic.account
# TODO: حدّث الـ ID ده بعد ما تشغل find_branch_id.py وتعرف رقم فرع الـ Online بالظبط
ODOO_BRANCH_ID = 548  # <-- حط هنا الـ ID بتاع فرع Online

# Gender mapping بين الفورم و Odoo
# عدّلها حسب الـ selection values الفعلية في op.student (غالبا 'm' / 'f')
GENDER_MAP = {
    "male": "m",
    "female": "f",
    "0": "m",   # لو جاي زي Moodle group id
    "1": "f",
}


class OdooConnectionError(Exception):
    pass


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


def _proxy(path):
    """ServerProxy مع timeout عشان الـ worker على Render مايفضلش معلق"""
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

    common = _proxy("/xmlrpc/2/common")
    if _uid_cache is None:
        _uid_cache = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not _uid_cache:
            raise OdooConnectionError("Odoo authentication failed — تأكد من ODOO_PASSWORD")

    models = _proxy("/xmlrpc/2/object")
    return _uid_cache, models


def _normalize_gender(raw_gender):
    if raw_gender is None:
        return False
    key = str(raw_gender).strip().lower()
    return GENDER_MAP.get(key, False)


def _normalize_date(raw_date):
    """
    Odoo بياخد التاريخ بصيغة YYYY-MM-DD
    لو الفورم بيبعت صيغة تانية (مثلا DD/MM/YYYY) عدّل هنا
    """
    if not raw_date:
        return False
    raw_date = raw_date.strip()
    # لو جاي خلاص بصيغة YYYY-MM-DD سيبه زي ما هو
    if len(raw_date) == 10 and raw_date[4] == "-":
        return raw_date
    # محاولة تحويل من DD/MM/YYYY أو DD-MM-YYYY
    for sep in ["/", "-"]:
        parts = raw_date.split(sep)
        if len(parts) == 3 and len(parts[2]) == 4:
            day, month, year = parts
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    logger.warning(f"Couldn't normalize date format: {raw_date}")
    return False


def find_student_by_email(email):
    """
    يدور على طالب موجود بالفعل بنفس الإيميل في Odoo عشان نتجنب تكرار
    """
    uid, models = _get_connection()
    existing = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "op.student", "search_read",
        [[["email", "=", email]]],
        {"fields": ["id", "name", "email", "name_seq"], "limit": 1},
    )
    return existing[0] if existing else None


def create_odoo_student(form_data):
    """
    form_data المتوقع فيها (من الفورم):
        first_name, last_name, arabic_name (اختياري), mobile,
        email, birth_date, gender

    بترجع dict فيها success / odoo_student_id / error
    """
    try:
        first_name = (form_data.get("first_name") or "").strip()
        last_name = (form_data.get("last_name") or "").strip()
        email = (form_data.get("email") or "").strip().lower()
        mobile = (form_data.get("mobile") or "").strip()
        birth_date_raw = form_data.get("birth_date")
        gender_raw = form_data.get("gender")

        if not email or not first_name:
            return {"success": False, "error": "email أو first_name مفقودين"}

        # تجنب تكرار الطالب لو موجود بالفعل
        existing = find_student_by_email(email)
        if existing:
            logger.info(f"Odoo: الطالب موجود بالفعل id={existing['id']}")
            return {
                "success": True,
                "odoo_student_id": existing["id"],
                "gr_no": existing.get("name_seq") or "",
                "already_existed": True,
            }

        arabic_name = form_data.get("arabic_name")
        full_name = arabic_name.strip() if arabic_name else f"{first_name} {last_name}".strip()

        student_vals = {
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "arabic_name": arabic_name.strip() if arabic_name else False,
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

        # gr_no بيتحدد تلقائي بواسطة الـ sequence في Odoo وقت الـ create
        # فبنعمل read تاني بعد الإنشاء عشان نجيبه
        gr_no = ""
        try:
            created = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "op.student", "read",
                [[student_id]],
                {"fields": ["name_seq"]},
            )
            if created:
                gr_no = created[0].get("name_seq") or ""
        except xmlrpc.client.Fault as e:
            logger.warning(f"⚠️ ماقدرش يجيب name_seq بعد الإنشاء: {e}")

        logger.info(f"✅ Odoo: تم إنشاء الطالب id={student_id} gr_no={gr_no} email={email}")
        return {
            "success": True,
            "odoo_student_id": student_id,
            "gr_no": gr_no,
            "already_existed": False,
        }

    except xmlrpc.client.Fault as e:
        logger.error(f"❌ Odoo XML-RPC Fault: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"❌ Odoo unexpected error: {e}")
        return {"success": False, "error": str(e)}