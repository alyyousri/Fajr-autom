"""
اختبارات الـ webhook - كلها بمحاكاة كاملة (مفيش اتصال حقيقي بـ Moodle أو Odoo أو Brevo).

التشغيل من مجلد المشروع:
    python -m unittest discover -s tests -v
"""
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# متغيرات بيئة وهمية قبل استيراد التطبيق
os.environ.update({
    "MOODLE_URL": "https://moodle.test",
    "MOODLE_TOKEN": "TOK-123",
    "BREVO_API_KEY": "brevo-key",
    "WEBHOOK_SECRET": "s3cret",
    "ODOO_URL": "https://odoo.test",
    "ODOO_DB": "db",
    "ODOO_USERNAME": "bot@example.com",
    "ODOO_PASSWORD": "pw",
})

try:
    import sib_api_v3_sdk  # noqa: F401
except ImportError:  # المكتبة مش متثبتة (بيئة اختبار): نستبدلها بنسخة وهمية
    _sib = types.ModuleType("sib_api_v3_sdk")
    _sib.Configuration = _sib.ApiClient = _sib.SendSmtpEmail = _sib.TransactionalEmailsApi = object
    sys.modules["sib_api_v3_sdk"] = _sib

import requests  # noqa: E402

import app as A  # noqa: E402
import odoo_service as O  # noqa: E402


# ---------------------------------------------------------------- Fakes
class FakeResponse:
    def __init__(self, value=None, status_code=200, bad_json=False):
        self._value = value
        self.status_code = status_code
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._value


class FakeMoodle:
    """يحاكي Moodle Web Services (POST فقط)"""

    def __init__(self):
        self.calls = []
        self.users = []        # نتيجة البحث بالإيميل
        self.overrides = {}    # wsfunction -> قيمة JSON أو استثناء أو FakeResponse

    def post(self, url, data=None, timeout=None):
        function = data["wsfunction"]
        self.calls.append(dict(data))
        if function in self.overrides:
            outcome = self.overrides[function]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome if isinstance(outcome, FakeResponse) else FakeResponse(outcome)
        if function == "core_user_get_users":
            return FakeResponse({"users": self.users, "warnings": []})
        if function == "core_user_create_users":
            return FakeResponse([{"id": 99, "username": data["users[0][username]"]}])
        return FakeResponse(None)  # enrol / group بترجع null عند النجاح

    def functions(self):
        return [call["wsfunction"] for call in self.calls]

    def call(self, function):
        return next(call for call in self.calls if call["wsfunction"] == function)


class FakeMailAPI:
    sent = []
    error = None

    def __init__(self, client):
        pass

    def send_transac_email(self, message, **kwargs):
        if FakeMailAPI.error:
            raise FakeMailAPI.error
        FakeMailAPI.sent.append(message.kw)


class FakeMessage:
    def __init__(self, **kwargs):
        self.kw = kwargs


class FakeConfig:
    def __init__(self):
        self.api_key = {}


MOODLE_EXCEPTION = {"exception": "moodle_exception", "errorcode": "invalidparameter", "message": "boom"}

BASE = {
    "first_name": "Ali",
    "last_name": "Hassan",
    "email": "ali@example.com",
    "gender": "Male",
    "birth_date": "25/12/2005",
    "mobile": "+201000000000",
    "study_mode": "Online",
}


class WebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.moodle = FakeMoodle()
        FakeMailAPI.sent = []
        FakeMailAPI.error = None
        self.odoo = mock.Mock(return_value={
            "success": True, "odoo_student_id": 7, "gr_no": "FM-2005-NCR-001634", "already_existed": False,
        })

        patches = [
            mock.patch.object(A.requests, "post", self.moodle.post),
            mock.patch.object(A.requests, "get", side_effect=AssertionError("GET ممنوع: التوكن هيظهر في الرابط")),
            mock.patch.object(A, "create_odoo_student", self.odoo),
            mock.patch.object(A.sib_api_v3_sdk, "TransactionalEmailsApi", FakeMailAPI),
            mock.patch.object(A.sib_api_v3_sdk, "SendSmtpEmail", FakeMessage),
            mock.patch.object(A.sib_api_v3_sdk, "ApiClient", lambda cfg: None),
            mock.patch.object(A.sib_api_v3_sdk, "Configuration", FakeConfig),
            mock.patch.object(A, "WEBHOOK_SECRET", "s3cret"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = A.app.test_client()

    def send(self, _headers=None, _query="", _json=None, **fields):
        """يبعت للـ webhook. القيم تتدمج فوق BASE، والقيمة None بتشيل الحقل."""
        if _json is None:
            body = dict(BASE)
            body.update(fields)
            body = {k: v for k, v in body.items() if v is not None}
        else:
            body = _json
        headers = {"X-Webhook-Token": "s3cret"} if _headers is None else _headers
        return self.client.post("/webhook" + _query, json=body, headers=headers)


# ---------------------------------------------------------------- الحماية
class AuthTests(WebhookTestCase):
    def test_rejects_everything_when_secret_not_configured(self):
        with mock.patch.object(A, "WEBHOOK_SECRET", ""):
            response = self.send(_headers={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.moodle.calls, [])
        self.odoo.assert_not_called()

    def test_missing_token_is_forbidden_and_logged(self):
        with self.assertLogs("fajr", level="WARNING") as logs:
            response = self.send(_headers={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.moodle.calls, [])
        self.assertIn("Rejected", "\n".join(logs.output))

    def test_wrong_token_is_forbidden_and_value_not_logged(self):
        with self.assertLogs("fajr", level="WARNING") as logs:
            response = self.send(_headers={"X-Webhook-Token": "wrong-token-xyz"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Rejected", "\n".join(logs.output))
        self.assertNotIn("wrong-token-xyz", "\n".join(logs.output))  # القيمة المرسلة لا تُسجَّل

    def test_non_ascii_token_is_forbidden_not_500(self):
        self.assertEqual(self.send(_headers={}, _query="?token=%C3%A9").status_code, 403)

    def test_header_token_accepted(self):
        self.assertEqual(self.send().status_code, 200)

    def test_query_token_still_accepted(self):
        self.assertEqual(self.send(_headers={}, _query="?token=s3cret").status_code, 200)


# ---------------------------------------------------------------- أون لاين فقط
class StudyModeTests(WebhookTestCase):
    def test_onsite_is_skipped_without_touching_any_system(self):
        for value in ("On-site", "on site", "Onsite", "ON_SITE", " on-site "):
            with self.subTest(value=value):
                self.moodle.calls.clear()
                response = self.send(study_mode=value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["status"], "skipped")
                self.assertEqual(self.moodle.calls, [])
        self.odoo.assert_not_called()
        self.assertEqual(FakeMailAPI.sent, [])

    def test_online_variants_are_registered(self):
        for value in ("Online", "online", " ONLINE ", "أونلاين", "أون لاين"):
            with self.subTest(value=value):
                self.moodle.calls.clear()
                response = self.send(study_mode=value)
                self.assertEqual(response.get_json()["status"], "success")
                self.assertIn("core_user_create_users", self.moodle.functions())

    def test_missing_study_mode_is_rejected_loudly(self):
        response = self.send(study_mode=None)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "study_mode_missing")
        self.odoo.assert_not_called()
        self.assertEqual(self.moodle.calls, [])

    def test_unrecognized_study_mode_is_rejected(self):
        response = self.send(study_mode="hybrid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "study_mode_unrecognized")
        self.odoo.assert_not_called()

    def test_alternative_key_name_is_accepted(self):
        response = self.send(study_mode=None, studyMode="On-site")
        self.assertEqual(response.get_json()["status"], "skipped")


# ---------------------------------------------------------------- المدخلات
class InputTests(WebhookTestCase):
    def test_json_list_body_is_400_not_500(self):
        self.assertEqual(self.send(_json=["x"]).status_code, 400)

    def test_missing_or_invalid_email_is_400(self):
        for value in (None, "", "not-an-email", "a@b"):
            with self.subTest(email=value):
                self.assertEqual(self.send(email=value).status_code, 400)
        self.odoo.assert_not_called()

    def test_email_is_trimmed_and_lowercased_everywhere(self):
        self.send(email="  Ali@Example.COM ")
        create = self.moodle.call("core_user_create_users")
        self.assertEqual(create["users[0][username]"], "ali@example.com")
        self.assertEqual(create["users[0][email]"], "ali@example.com")
        self.assertEqual(self.moodle.call("core_user_get_users")["criteria[0][value]"], "ali@example.com")
        self.assertEqual(self.odoo.call_args[0][0]["email"], "ali@example.com")

    def test_moodle_is_only_called_with_post(self):
        self.send()  # لو استُخدم GET الاختبار هيفشل بـ AssertionError
        self.assertTrue(self.moodle.calls)

    def test_no_pii_in_logs(self):
        with self.assertLogs("fajr", level="INFO") as logs:
            self.send(mobile="+201234567890")
        text = "\n".join(logs.output)
        self.assertNotIn("+201234567890", text)
        self.assertNotIn("25/12/2005", text)
        self.assertNotIn("ali@example.com", text)  # الإيميل بيتسجل مخفي جزئيًا


# ---------------------------------------------------------------- الجنس والمجموعات
class GenderTests(WebhookTestCase):
    def group_of(self, response_gender):
        self.moodle.calls.clear()
        self.send(gender=response_gender)
        return self.moodle.call("core_group_add_group_members")["members[0][groupid]"]

    def test_gender_is_case_insensitive(self):
        for value in ("Male", "male", "MALE", " male "):
            with self.subTest(value=value):
                self.assertEqual(self.group_of(value), A.MOODLE_GROUP_MALE)
        for value in ("Female", "female", "FEMALE"):
            with self.subTest(value=value):
                self.assertEqual(self.group_of(value), A.MOODLE_GROUP_FEMALE)

    def test_gender_tolerates_invisible_characters_and_combined_labels(self):
        male = ("‏Male", "Male‎", "‫Male‬", "Male ", "Male (ذكر)", "ذكر", "Male - ذكر")
        female = ("‏Female", "Female‏", "Female (أنثى)", "أنثى", "انثى", "أنثي", "Female / انثى")
        for value in male:
            with self.subTest(value=value):
                self.assertEqual(self.group_of(value), A.MOODLE_GROUP_MALE)
        for value in female:
            with self.subTest(value=value):
                self.assertEqual(self.group_of(value), A.MOODLE_GROUP_FEMALE)

    def test_unknown_gender_is_not_put_in_any_group(self):
        response = self.send(gender="")
        body = response.get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIn("gender_unknown", body["warnings"])
        self.assertNotIn("core_group_add_group_members", self.moodle.functions())

    def test_ambiguous_gender_is_not_guessed(self):
        for value in ("Male/Female", "ذكر أنثى", "x", "1", "Other"):
            with self.subTest(value=value):
                self.assertIsNone(O.normalize_gender(value))

    def test_unrecognized_gender_value_is_logged_with_escapes(self):
        # عشان لو الفورم بتبعت حرف خفي نعرفه من اللوج
        with self.assertLogs("fajr", level="ERROR") as logs:
            self.send(gender="‏خيار")
        text = "\n".join(logs.output)
        self.assertIn("Unrecognized gender value", text)
        self.assertIn("\\u200f", text)

    def test_normalize_gender_helper(self):
        self.assertEqual(O.normalize_gender("Male"), "male")
        self.assertEqual(O.normalize_gender(" FEMALE "), "female")
        self.assertIsNone(O.normalize_gender(""))
        self.assertIsNone(O.normalize_gender("x"))
        self.assertEqual(O._normalize_gender("Male"), "m")
        self.assertEqual(O._normalize_gender("female"), "f")
        self.assertIs(O._normalize_gender("x"), False)


# ---------------------------------------------------------------- المسار الكامل
class FlowTests(WebhookTestCase):
    def test_full_online_flow(self):
        response = self.send()
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["user_id"], 99)
        self.assertNotIn("warnings", body)
        self.assertEqual(self.moodle.functions(), [
            "core_user_get_users", "core_user_create_users",
            "enrol_manual_enrol_users", "core_group_add_group_members",
        ])
        enrol = self.moodle.call("enrol_manual_enrol_users")
        self.assertEqual(enrol["enrolments[0][courseid]"], A.MOODLE_COURSE_ID)
        self.assertEqual(len(FakeMailAPI.sent), 1)
        self.assertIn("FM-2005-NCR-001634", FakeMailAPI.sent[0]["html_content"])

    def test_existing_moodle_user_gets_exists_email_only(self):
        self.moodle.users = [{"id": 5, "email": "ali@example.com"}]
        body = self.send().get_json()
        self.assertEqual(body["status"], "exists")
        self.assertEqual(body["user_id"], 5)
        self.assertNotIn("core_user_create_users", self.moodle.functions())
        self.assertEqual(len(FakeMailAPI.sent), 1)
        self.assertIn("مسجل بالفعل", FakeMailAPI.sent[0]["subject"])

    def test_lookup_requires_exact_email_match(self):
        # البحث ممكن يرجع إيميل قريب (_ كـ wildcard) - ده مش نفس الطالب
        self.moodle.users = [{"id": 5, "email": "johnXdoe@example.com"}]
        body = self.send(email="john_doe@example.com").get_json()
        self.assertEqual(body["status"], "success")
        self.assertIn("core_user_create_users", self.moodle.functions())


# ---------------------------------------------------------------- كلمة المرور
class PasswordTests(WebhookTestCase):
    def test_generated_passwords_meet_policy_and_are_unique(self):
        seen = set()
        for _ in range(300):
            password = A.generate_password()
            self.assertGreaterEqual(len(password), 12)
            self.assertRegex(password, r"[a-z]")
            self.assertRegex(password, r"[A-Z]")
            self.assertRegex(password, r"\d")
            self.assertRegex(password, r"[^A-Za-z0-9]")
            seen.add(password)
        self.assertEqual(len(seen), 300)

    def test_password_is_emailed_and_change_is_forced(self):
        self.send()
        create = self.moodle.call("core_user_create_users")
        self.assertIn(create["users[0][password]"], FakeMailAPI.sent[0]["html_content"])
        self.assertEqual(create["users[0][preferences][0][type]"], "auth_forcepasswordchange")
        self.assertEqual(create["users[0][preferences][0][value]"], "1")

    def test_force_change_can_be_disabled(self):
        with mock.patch.object(A, "FORCE_PASSWORD_CHANGE", False):
            self.send()
        self.assertNotIn("users[0][preferences][0][type]", self.moodle.call("core_user_create_users"))

    def test_password_is_never_logged(self):
        with self.assertLogs("fajr", level="DEBUG") as logs:
            self.send()
        password = self.moodle.call("core_user_create_users")["users[0][password]"]
        self.assertNotIn(password, "\n".join(logs.output))


# ---------------------------------------------------------------- الإيميل
class EmailTests(WebhookTestCase):
    def test_user_supplied_html_is_escaped(self):
        self.send(first_name='<a href="https://evil.test">اضغط</a>')
        html_content = FakeMailAPI.sent[0]["html_content"]
        self.assertNotIn('<a href="https://evil.test">', html_content)
        self.assertIn("&lt;a href=", html_content)

    def test_placement_slot_is_shown_when_provided(self):
        self.send(placement_slot="2026-11-20 10:00")
        html_content = FakeMailAPI.sent[0]["html_content"]
        self.assertIn("2026-11-20 10:00", html_content)
        self.assertNotIn("سيتم التواصل معك", html_content)  # الكلام القديم لم يعد صحيحًا

    def test_welcome_email_without_slot_has_no_outdated_promise(self):
        self.send()
        html_content = FakeMailAPI.sent[0]["html_content"]
        self.assertNotIn("سيتم التواصل معك", html_content)
        self.assertIn("حسب الموعد الذي اخترته", html_content)


# ---------------------------------------------------------------- الأخطاء
class FailureTests(WebhookTestCase):
    def test_lookup_failure_stops_before_creating_anything(self):
        self.moodle.overrides["core_user_get_users"] = {
            "exception": "moodle_exception", "errorcode": "invalidtoken", "message": "bad token",
        }
        response = self.send()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("core_user_create_users", self.moodle.functions())
        self.assertEqual(FakeMailAPI.sent, [])

    def test_moodle_down_does_not_leak_token(self):
        self.moodle.overrides["core_user_get_users"] = requests.ConnectionError(
            "HTTPSConnectionPool: Max retries exceeded with url: /x?wstoken=TOK-123"
        )
        with self.assertLogs("fajr", level="ERROR") as logs:
            response = self.send()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("TOK-123", response.get_data(as_text=True))
        self.assertNotIn("TOK-123", "\n".join(logs.output))

    def test_non_json_moodle_response_is_handled(self):
        self.moodle.overrides["core_user_get_users"] = FakeResponse(bad_json=True, status_code=502)
        self.assertEqual(self.send().status_code, 502)

    def test_user_creation_rejected_by_moodle(self):
        self.moodle.overrides["core_user_create_users"] = MOODLE_EXCEPTION
        response = self.send()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("enrol_manual_enrol_users", self.moodle.functions())
        self.assertEqual(FakeMailAPI.sent, [])
        self.assertNotIn("boom", response.get_data(as_text=True))

    def test_enrolment_failure_is_reported_not_hidden(self):
        self.moodle.overrides["enrol_manual_enrol_users"] = MOODLE_EXCEPTION
        body = self.send().get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIn("enrol_failed", body["warnings"])
        self.assertEqual(len(FakeMailAPI.sent), 1)  # الحساب اتعمل فلازم يوصله الإيميل

    def test_group_failure_is_reported(self):
        self.moodle.overrides["core_group_add_group_members"] = MOODLE_EXCEPTION
        body = self.send().get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIn("group_failed", body["warnings"])

    def test_email_failure_is_reported_for_any_exception(self):
        for error in (RuntimeError("timeout"), OSError("network down")):
            with self.subTest(error=error):
                FakeMailAPI.error = error
                response = self.send()
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["status"], "partial")
                self.assertIn("email_failed", body["warnings"])

    def test_odoo_failure_does_not_block_moodle_and_does_not_leak(self):
        self.odoo.return_value = {"success": False, "error": "Fault: internal secret detail"}
        response = self.send()
        body = response.get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIn("odoo_failed", body["warnings"])
        self.assertIn("core_user_create_users", self.moodle.functions())
        self.assertNotIn("internal secret detail", response.get_data(as_text=True))

    def test_odoo_exception_does_not_block_moodle(self):
        self.odoo.side_effect = RuntimeError("crash")
        body = self.send().get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIn("odoo_failed", body["warnings"])
        self.assertIn("core_user_create_users", self.moodle.functions())


# ---------------------------------------------------------------- odoo_service
class OdooServiceTests(unittest.TestCase):
    def test_date_formats(self):
        normalize = O._normalize_date
        self.assertEqual(normalize("2005-03-12"), "2005-03-12")
        self.assertEqual(normalize("12/03/2005"), "2005-03-12")      # يوم/شهر/سنة (الافتراضي)
        self.assertEqual(normalize("12-03-2005"), "2005-03-12")
        self.assertEqual(normalize("2005/3/12"), "2005-03-12")
        self.assertEqual(normalize("2005-03-12 00:00:00"), "2005-03-12")
        self.assertEqual(normalize("٢٥/١٢/٢٠٠٥"), "2005-12-25")        # أرقام عربية
        self.assertIs(normalize("31/02/2005"), False)                 # تاريخ مستحيل
        self.assertIs(normalize("garbage"), False)
        self.assertIs(normalize(""), False)
        self.assertIs(normalize(None), False)

    def test_month_first_order_is_configurable(self):
        with mock.patch.object(O, "DATE_ORDER", "mdy"):
            self.assertEqual(O._normalize_date("03/12/2005"), "2005-03-12")
        self.assertEqual(O._normalize_date("03/12/2005"), "2005-12-03")

    def test_not_configured_is_reported_as_failure_not_crash(self):
        with mock.patch.object(O, "ODOO_URL", ""):
            result = O.create_odoo_student({"first_name": "Ali", "email": "a@b.com"})
        self.assertFalse(result["success"])

    def test_no_internal_defaults_in_source(self):
        with open(os.path.join(ROOT, "odoo_service.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("sys.fajr.com", source)
        self.assertNotIn("develop_register", source)


class ClassifyStudyModeTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(A.classify_study_mode("Online"), "online")
        self.assertEqual(A.classify_study_mode("On-site"), "onsite")
        self.assertEqual(A.classify_study_mode("حضوري"), "onsite")
        self.assertIsNone(A.classify_study_mode(""))
        self.assertIsNone(A.classify_study_mode(None))
        self.assertIsNone(A.classify_study_mode("both"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
