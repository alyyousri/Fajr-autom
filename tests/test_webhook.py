"""
اختبارات الـ webhook - كلها بمحاكاة كاملة (مفيش اتصال حقيقي بـ Moodle أو Odoo أو Brevo).

التشغيل من مجلد المشروع:
    python -m unittest discover -s tests -v
"""
import os
import re
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
    reject_bcc = False  # يحاكي رفض Brevo للإيميل لو فيه BCC

    def __init__(self, client):
        pass

    def send_transac_email(self, message, **kwargs):
        if FakeMailAPI.error:
            raise FakeMailAPI.error
        if FakeMailAPI.reject_bcc and message.kw.get("bcc"):
            raise RuntimeError("bcc rejected")
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
        FakeMailAPI.reject_bcc = False
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

    def test_empty_gender_is_listed_as_empty_in_the_log(self):
        with self.assertLogs("fajr", level="INFO") as logs:
            self.send(gender="")
        received = [line for line in logs.output if "Webhook received" in line][0]
        self.assertIn("empty: ['gender']", received)

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
        self.assertIn("already registered", FakeMailAPI.sent[0]["subject"])

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
        self.assertIn("as selected in the registration form", html_content)


class TeamCopyTests(WebhookTestCase):
    TEAM = "team@example.com"

    def test_no_copy_when_not_configured(self):
        with mock.patch.object(A, "TEAM_COPY_EMAIL", ""):
            self.send()
        self.assertNotIn("bcc", FakeMailAPI.sent[0])

    def test_welcome_email_is_copied_to_the_team(self):
        with mock.patch.object(A, "TEAM_COPY_EMAIL", self.TEAM):
            self.send()
        sent = FakeMailAPI.sent[0]
        self.assertEqual(sent["to"][0]["email"], "ali@example.com")
        self.assertEqual(sent["bcc"], [{"email": self.TEAM}])
        self.assertIn("Welcome", sent["subject"])

    def test_already_registered_email_is_copied_too(self):
        self.moodle.users = [{"id": 5, "email": "ali@example.com"}]
        with mock.patch.object(A, "TEAM_COPY_EMAIL", self.TEAM):
            body = self.send().get_json()
        self.assertEqual(body["status"], "exists")
        self.assertEqual(FakeMailAPI.sent[0]["bcc"], [{"email": self.TEAM}])
        self.assertIn("already registered", FakeMailAPI.sent[0]["subject"])

    def test_invalid_team_address_is_ignored(self):
        for bad in ("not-an-email", "a@b", "x@y.com, z@w.com"):
            with self.subTest(bad=bad), mock.patch.object(A, "TEAM_COPY_EMAIL", bad):
                FakeMailAPI.sent = []
                self.send()
                self.assertNotIn("bcc", FakeMailAPI.sent[0])

    def test_no_bcc_when_the_student_is_the_team_address(self):
        with mock.patch.object(A, "TEAM_COPY_EMAIL", "ali@example.com"):
            self.send()
        self.assertNotIn("bcc", FakeMailAPI.sent[0])

    def test_student_email_still_goes_out_if_the_copy_is_rejected(self):
        FakeMailAPI.reject_bcc = True
        with mock.patch.object(A, "TEAM_COPY_EMAIL", self.TEAM), self.assertLogs("fajr", level="WARNING") as logs:
            body = self.send().get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(len(FakeMailAPI.sent), 1)
        self.assertNotIn("bcc", FakeMailAPI.sent[0])
        self.assertIn("without the team copy", "\n".join(logs.output))

    def test_log_never_shows_the_team_address(self):
        with mock.patch.object(A, "TEAM_COPY_EMAIL", self.TEAM), self.assertLogs("fajr", level="INFO") as logs:
            self.send()
        text = "\n".join(logs.output)
        self.assertIn("+team copy", text)
        self.assertNotIn(self.TEAM, text)


# ---------------------------------------------------------------- إعادة تعيين كلمة المرور
class ResetTokenTests(unittest.TestCase):
    SECRET = "s3cret"

    def test_roundtrip_normalizes_the_email(self):
        token = A.create_token(self.SECRET, "  Ali@Example.COM ", 3600)
        payload = A.verify_token(self.SECRET, token)
        self.assertEqual(payload["e"], "ali@example.com")

    def test_expired_token_is_rejected(self):
        token = A.create_token(self.SECRET, "ali@example.com", 60, now=1_000_000)
        self.assertIsNotNone(A.verify_token(self.SECRET, token, now=1_000_030))
        self.assertIsNone(A.verify_token(self.SECRET, token, now=1_000_061))

    def test_tampering_is_rejected(self):
        token = A.create_token(self.SECRET, "ali@example.com", 3600)
        body, signature = token.split(".")
        forged = A.create_token(self.SECRET, "victim@example.com", 3600).split(".")[0]
        self.assertIsNone(A.verify_token(self.SECRET, f"{forged}.{signature}"))
        self.assertIsNone(A.verify_token(self.SECRET, f"{body}.{signature[:-2]}AA"))
        self.assertIsNone(A.verify_token("another-secret", token))

    def test_garbage_is_rejected_not_a_crash(self):
        for garbage in ("", "abc", "a.b.c", "a.b", "é.é", None, 123, "." * 5, "e30.e30"):
            with self.subTest(garbage=garbage):
                self.assertIsNone(A.verify_token(self.SECRET, garbage))

    def test_tokens_are_unique_and_do_not_contain_the_secret(self):
        first = A.create_token(self.SECRET, "ali@example.com", 3600)
        second = A.create_token(self.SECRET, "ali@example.com", 3600)
        self.assertNotEqual(first, second)
        self.assertNotIn(self.SECRET, first)


class ExistsEmailLinkTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(A, "PUBLIC_BASE_URL", "https://fajr.test")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.moodle.users = [{"id": 5, "email": "ali@example.com"}]

    def test_exists_email_carries_a_valid_reset_link(self):
        self.assertEqual(self.send().get_json()["status"], "exists")
        html_content = FakeMailAPI.sent[0]["html_content"]
        match = re.search(r'href="https://fajr\.test/reset-password\?t=([^"]+)"', html_content)
        self.assertIsNotNone(match)
        self.assertEqual(A.verify_token("s3cret", match.group(1))["e"], "ali@example.com")
        self.assertIn("valid for 60 minutes", html_content)
        self.assertNotIn("forgot_password.php", html_content)

    def test_without_a_public_address_the_moodle_link_is_used(self):
        with mock.patch.object(A, "PUBLIC_BASE_URL", ""):
            self.send()
        html_content = FakeMailAPI.sent[0]["html_content"]
        self.assertNotIn("reset-password", html_content)
        self.assertIn("https://learn.fajr.com/login/forgot_password.php", html_content)

    def test_no_link_can_be_made_without_the_secret(self):
        with mock.patch.object(A, "WEBHOOK_SECRET", ""):
            self.assertEqual(A._reset_link("ali@example.com"), "")

    def test_new_students_do_not_get_a_reset_link(self):
        self.moodle.users = []
        self.send()
        self.assertNotIn("reset-password", FakeMailAPI.sent[0]["html_content"])


class ResetPasswordTests(WebhookTestCase):
    USER = {
        "id": 5, "username": "ali@example.com", "firstname": "Ali", "email": "ali@example.com",
        "auth": "manual", "suspended": False,
    }

    def setUp(self):
        super().setUp()
        A._used_reset_nonces.clear()
        A._last_reset.clear()
        self.addCleanup(A._used_reset_nonces.clear)
        self.addCleanup(A._last_reset.clear)
        self.moodle.users = [dict(self.USER)]

    def token(self, email="ali@example.com", ttl=3600, now=None):
        return A.create_token("s3cret", email, ttl, now=now)

    def confirm(self, token=None):
        return self.client.post("/reset-password", data={"t": token or self.token()})

    def updates(self):
        return [call for call in self.moodle.calls if call["wsfunction"] == "core_user_update_users"]

    # ---- الصفحة (GET)
    def test_get_shows_a_confirmation_page_and_changes_nothing(self):
        token = self.token()
        response = self.client.get("/reset-password", query_string={"t": token})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('method="post"', text)
        self.assertIn(token, text)
        self.assertEqual(self.moodle.calls, [])
        self.assertEqual(FakeMailAPI.sent, [])

    def test_page_has_hardening_headers(self):
        response = self.client.get("/reset-password", query_string={"t": self.token()})
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("script", response.get_data(as_text=True).lower())

    def test_invalid_expired_or_missing_tokens_are_refused_without_side_effects(self):
        expired = self.token(ttl=60, now=1_000_000)
        good_body = self.token().split(".")[0]
        for label, token in (("none", None), ("junk", "junk"), ("expired", expired), ("unsigned", good_body + ".x")):
            with self.subTest(token=label):
                query = {} if token is None else {"t": token}
                self.assertEqual(self.client.get("/reset-password", query_string=query).status_code, 400)
                self.assertEqual(self.client.post("/reset-password", data=query).status_code, 400)
        self.assertEqual(self.moodle.calls, [])
        self.assertEqual(FakeMailAPI.sent, [])

    def test_post_does_not_accept_the_token_from_the_query_string(self):
        response = self.client.post("/reset-password", query_string={"t": self.token()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.moodle.calls, [])

    def test_other_methods_are_not_allowed(self):
        self.assertEqual(self.client.put("/reset-password").status_code, 405)

    def test_not_configured_secret_is_503(self):
        with mock.patch.object(A, "WEBHOOK_SECRET", ""):
            self.assertEqual(self.client.get("/reset-password", query_string={"t": "x"}).status_code, 503)

    # ---- التنفيذ (POST)
    def test_reset_changes_the_password_and_emails_it_to_the_student(self):
        response = self.confirm()
        self.assertEqual(response.status_code, 200)
        update = self.updates()[0]
        self.assertEqual(update["users[0][id]"], 5)
        password = update["users[0][password]"]
        self.assertGreaterEqual(len(password), 12)

        self.assertEqual(self.moodle.call("core_user_get_users")["criteria[0][value]"], "ali@example.com")
        sent = FakeMailAPI.sent[0]
        self.assertEqual(sent["to"][0]["email"], "ali@example.com")
        self.assertIn("reset", sent["subject"])
        self.assertIn(password, sent["html_content"])
        self.assertIn("ali@example.com", sent["html_content"])  # اسم المستخدم
        self.assertIn("Hello Ali", sent["html_content"])

    def test_password_is_never_shown_on_the_page(self):
        response = self.confirm()
        password = self.updates()[0]["users[0][password]"]
        self.assertNotIn(password, response.get_data(as_text=True))
        self.assertIn("Check your email", response.get_data(as_text=True))

    def test_reset_email_is_not_copied_to_the_team(self):
        with mock.patch.object(A, "TEAM_COPY_EMAIL", "team@example.com"):
            self.confirm()
        self.assertNotIn("bcc", FakeMailAPI.sent[0])

    def test_username_that_differs_from_the_email_is_shown(self):
        self.moodle.users = [dict(self.USER, username="student1634")]
        self.confirm()
        self.assertIn("student1634", FakeMailAPI.sent[0]["html_content"])

    def test_password_change_is_forced_at_next_login(self):
        self.confirm()
        update = self.updates()[0]
        self.assertEqual(update["users[0][preferences][0][type]"], "auth_forcepasswordchange")
        self.assertEqual(update["users[0][preferences][0][value]"], "1")
        self.assertIn("change your password", FakeMailAPI.sent[0]["html_content"])

    def test_force_change_can_be_disabled(self):
        with mock.patch.object(A, "FORCE_PASSWORD_CHANGE", False):
            self.confirm()
        self.assertNotIn("users[0][preferences][0][type]", self.updates()[0])
        self.assertNotIn("change your password", FakeMailAPI.sent[0]["html_content"])

    def test_link_works_only_once(self):
        token = self.token()
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertEqual(self.confirm(token).status_code, 400)
        self.assertEqual(self.client.get("/reset-password", query_string={"t": token}).status_code, 400)
        self.assertEqual(len(self.updates()), 1)
        self.assertEqual(len(FakeMailAPI.sent), 1)

    def test_a_second_request_for_the_same_email_waits_for_the_cooldown(self):
        self.assertEqual(self.confirm().status_code, 200)
        response = self.confirm()  # رابط جديد لنفس الإيميل
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.updates()), 1)
        with mock.patch.object(A, "RESET_COOLDOWN_MINUTES", 0):
            self.assertEqual(self.confirm().status_code, 200)
        self.assertEqual(len(self.updates()), 2)

    def test_cooldown_is_per_email(self):
        self.confirm()
        other = dict(self.USER, id=6, email="sara@example.com", username="sara@example.com")
        self.moodle.users = [other]
        self.assertEqual(self.confirm(self.token("sara@example.com")).status_code, 200)

    # ---- حالات الرفض
    def test_accounts_that_cannot_be_reset_automatically_are_not_touched(self):
        cases = {
            "no account": [],
            "two accounts": [dict(self.USER), dict(self.USER, id=6)],
            "suspended": [dict(self.USER, suspended=True)],
            "not manual auth": [dict(self.USER, auth="oauth2")],
            "only a similar email": [dict(self.USER, email="ali_@example.com")],
        }
        for label, users in cases.items():
            with self.subTest(case=label):
                A._used_reset_nonces.clear()
                A._last_reset.clear()
                self.moodle.users = users
                self.moodle.calls.clear()
                FakeMailAPI.sent = []
                response = self.confirm()
                self.assertEqual(response.status_code, 409)
                self.assertIn("info@fajr.com", response.get_data(as_text=True))
                self.assertEqual(self.updates(), [])
                self.assertEqual(FakeMailAPI.sent, [])

    def test_moodle_lookup_failure_can_be_retried_with_the_same_link(self):
        token = self.token()
        self.moodle.overrides["core_user_get_users"] = requests.ConnectionError("down")
        self.assertEqual(self.confirm(token).status_code, 503)
        self.assertEqual(self.updates(), [])
        del self.moodle.overrides["core_user_get_users"]
        self.assertEqual(self.confirm(token).status_code, 200)

    def test_moodle_update_failure_sends_no_email_and_can_be_retried(self):
        token = self.token()
        self.moodle.overrides["core_user_update_users"] = MOODLE_EXCEPTION
        self.assertEqual(self.confirm(token).status_code, 503)
        self.assertEqual(FakeMailAPI.sent, [])
        del self.moodle.overrides["core_user_update_users"]
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertEqual(len(FakeMailAPI.sent), 1)

    def test_email_failure_is_tried_twice_logged_and_can_be_retried(self):
        token = self.token()
        FakeMailAPI.error = RuntimeError("brevo down")
        with mock.patch.object(A, "send_email", wraps=A.send_email) as spy, \
                self.assertLogs("fajr", level="ERROR") as logs:
            response = self.confirm(token)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(spy.call_count, 2)
        self.assertIn("WAS changed", "\n".join(logs.output))
        self.assertNotIn(self.updates()[0]["users[0][password]"], response.get_data(as_text=True))
        FakeMailAPI.error = None
        self.assertEqual(self.confirm(token).status_code, 200)

    def test_expired_state_is_purged(self):
        A._used_reset_nonces["old"] = 1
        A._last_reset["old@example.com"] = 1
        self.confirm()
        self.assertNotIn("old", A._used_reset_nonces)
        self.assertNotIn("old@example.com", A._last_reset)

    # ---- الخصوصية
    def test_logs_never_contain_the_email_token_or_password(self):
        token = self.token()
        with self.assertLogs("fajr", level="INFO") as logs:
            self.confirm(token)
            self.confirm(token)  # رابط مستخدم
            self.confirm(self.token())  # cooldown
        text = "\n".join(logs.output)
        password = self.updates()[0]["users[0][password]"]
        self.assertNotIn("ali@example.com", text)
        self.assertNotIn(token, text)
        self.assertNotIn(token.split(".")[0], text)
        self.assertNotIn(password, text)
        self.assertIn("user id=5", text)

    def test_moodle_failure_logs_do_not_leak_the_token_or_email(self):
        self.moodle.overrides["core_user_update_users"] = requests.ConnectionError(
            "https://moodle.test/?wstoken=TOK-123&email=ali@example.com"
        )
        with self.assertLogs("fajr", level="INFO") as logs:
            self.confirm()
        text = "\n".join(logs.output)
        self.assertNotIn("TOK-123", text)
        self.assertNotIn("ali@example.com", text)


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


# ---------------------------------------------------------------- إعادة محاولة Odoo
class FakeTimer:
    """بديل threading.Timer: يسجل الجدولة وننفذ الدالة بإيدينا بدل ما نستنى 20 دقيقة"""
    created = []

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, tuple(args)
        self.daemon = False
        self.started = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def run_now(self):
        self.function(*self.args)


TRANSIENT = {"success": False, "error": "timed out", "retryable": True}


class OdooRetryTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        FakeTimer.created = []
        A._pending_odoo_retries.clear()
        for patcher in (
            mock.patch.object(A.threading, "Timer", FakeTimer),
            mock.patch.object(A, "ODOO_RETRY_DELAY_MINUTES", 20),
            mock.patch.object(A, "ODOO_RETRY_MAX_ATTEMPTS", 3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(A._pending_odoo_retries.clear)

    def test_transient_failure_schedules_a_retry_after_20_minutes(self):
        self.odoo.return_value = TRANSIENT
        body = self.send().get_json()
        self.assertEqual(body["status"], "partial")            # Moodle والإيميل اتعملوا
        self.assertIn("odoo_failed", body["warnings"])
        self.assertIn("core_user_create_users", self.moodle.functions())
        self.assertEqual(len(FakeTimer.created), 1)
        timer = FakeTimer.created[0]
        self.assertEqual(timer.interval, 20 * 60)
        self.assertTrue(timer.started and timer.daemon)
        self.assertEqual(timer.args[0], "ali@example.com")
        self.assertEqual(timer.args[2], 1)

    def test_non_transient_failure_is_not_retried(self):
        self.odoo.return_value = {"success": False, "error": "gender missing or unrecognized"}
        self.send()
        self.assertEqual(FakeTimer.created, [])

    def test_success_is_not_retried(self):
        self.send()
        self.assertEqual(FakeTimer.created, [])

    def test_retry_can_be_disabled(self):
        self.odoo.return_value = TRANSIENT
        with mock.patch.object(A, "ODOO_RETRY_MAX_ATTEMPTS", 0):
            self.send()
        self.assertEqual(FakeTimer.created, [])

    def test_same_student_waits_in_one_retry_only(self):
        self.odoo.return_value = TRANSIENT
        self.send()
        self.send()
        self.assertEqual(len(FakeTimer.created), 1)

    def test_retry_sends_the_same_student_data_to_odoo(self):
        self.odoo.return_value = TRANSIENT
        self.send()
        first_call = self.odoo.call_args[0][0]
        self.odoo.reset_mock()
        self.odoo.return_value = {"success": True, "odoo_student_id": 9, "already_existed": False}
        FakeTimer.created[0].run_now()
        self.odoo.assert_called_once_with(first_call)

    def test_successful_retry_stops_and_logs(self):
        self.odoo.return_value = TRANSIENT
        self.send()
        self.odoo.return_value = {"success": True, "odoo_student_id": 9, "already_existed": False}
        with self.assertLogs("fajr", level="INFO") as logs:
            FakeTimer.created[0].run_now()
        self.assertEqual(len(FakeTimer.created), 1)             # مفيش جدولة تانية
        self.assertIn("Odoo retry 1/3 succeeded", "\n".join(logs.output))

    def test_failed_retries_repeat_then_give_up(self):
        self.odoo.return_value = TRANSIENT
        self.send()
        for expected_attempt in (1, 2):
            FakeTimer.created[-1].run_now()
            self.assertEqual(FakeTimer.created[-1].args[2], expected_attempt + 1)
        with self.assertLogs("fajr", level="ERROR") as logs:
            FakeTimer.created[-1].run_now()                      # المحاولة الثالثة والأخيرة
        self.assertEqual(len(FakeTimer.created), 3)
        self.assertIn("GAVE UP", "\n".join(logs.output))
        self.assertIn("manually", "\n".join(logs.output))

    def test_non_transient_retry_failure_gives_up_at_once(self):
        self.odoo.return_value = TRANSIENT
        self.send()
        self.odoo.return_value = {"success": False, "error": "some data error"}
        with self.assertLogs("fajr", level="ERROR") as logs:
            FakeTimer.created[0].run_now()
        self.assertEqual(len(FakeTimer.created), 1)
        self.assertIn("GAVE UP", "\n".join(logs.output))

    def test_retry_logs_do_not_contain_student_data(self):
        self.odoo.return_value = TRANSIENT
        with self.assertLogs("fajr", level="INFO") as logs:
            self.send()
            FakeTimer.created[0].run_now()
            FakeTimer.created[-1].run_now()
        text = "\n".join(logs.output)
        self.assertNotIn("ali@example.com", text)
        self.assertNotIn("Hassan", text)
        self.assertNotIn("+201000000000", text)


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

    def test_missing_gender_does_not_attempt_creation(self):
        # الجنس إلزامي في Odoo: من غيره مانبعتش الطلب خالص
        with mock.patch.object(O, "find_student_by_email", return_value=None), \
                mock.patch.object(O, "_get_connection", side_effect=AssertionError("must not connect")), \
                self.assertLogs("odoo_service", level="ERROR") as logs:
            result = O.create_odoo_student({"first_name": "Ali", "email": "a@b.com", "gender": ""})
        self.assertFalse(result["success"])
        self.assertIn("gender", result["error"])
        self.assertIn("gender is missing", "\n".join(logs.output))

    def test_existing_student_does_not_need_gender(self):
        with mock.patch.object(O, "find_student_by_email", return_value={"id": 7, O.STUDENT_CODE_FIELD: "C1"}):
            result = O.create_odoo_student({"first_name": "Ali", "email": "a@b.com", "gender": ""})
        self.assertTrue(result["success"])
        self.assertTrue(result["already_existed"])

    def test_fault_summary_drops_the_student_row(self):
        import xmlrpc.client
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/sql_db.py", line 250, in execute\n'
            "    res = self._obj.execute(query, params)\n"
            'psycopg2.IntegrityError: null value in column "gender" of relation "op_student" '
            "violates not-null constraint\n"
            "DETAIL:  Failing row contains (65445, null, Secret, null, Student, null, 2026-11-15).\n\n"
            "During handling of the above exception, another exception occurred:\n\n"
            "odoo.exceptions.ValidationError: ('Model: Student (op.student), Field: Gender (gender)', None)\n"
        )
        summary = O._fault_summary(xmlrpc.client.Fault(1, traceback_text))
        self.assertIn("IntegrityError", summary)
        self.assertIn("gender", summary)
        self.assertNotIn("Secret", summary)
        self.assertNotIn("65445", summary)
        self.assertNotIn("DETAIL", summary)

    def test_fault_summary_keeps_builtin_exceptions(self):
        # أخطاء بايثون العادية (من كود Odoo المخصص) مالهاش اسم موديول قبلها
        import xmlrpc.client
        text = (
            "Traceback (most recent call last):\n"
            '  File "/mnt/extra-addons/fajr/student_customization/models/op_student.py", line 120, in create\n'
            "    year = datetime.strptime(vals['birth_date'], '%Y-%m-%d').year\n"
            "TypeError: strptime() argument 1 must be str, not bool\n"
        )
        summary = O._fault_summary(xmlrpc.client.Fault(1, text))
        self.assertIn("TypeError: strptime() argument 1 must be str, not bool", summary)
        self.assertNotIn("File", summary)

    def test_fault_summary_with_unknown_shape_withholds_details(self):
        import xmlrpc.client
        summary = O._fault_summary(xmlrpc.client.Fault(2, "weird text with Ali Hassan inside"))
        self.assertNotIn("Ali", summary)

    def test_transient_errors_are_classified(self):
        import socket
        import xmlrpc.client
        transient = [
            TimeoutError("timed out"), socket.timeout("timed out"), ConnectionRefusedError(),
            ConnectionResetError(), socket.gaierror("dns"), xmlrpc.client.ProtocolError("u", 502, "bad gateway", {}),
            xmlrpc.client.ProtocolError("u", 503, "unavailable", {}),
        ]
        permanent = [
            O.OdooConnectionError("auth failed"), xmlrpc.client.Fault(1, "data error"), ValueError("x"),
            xmlrpc.client.ProtocolError("u", 404, "not found", {}),
        ]
        for exc in transient:
            with self.subTest(exc=repr(exc)):
                self.assertTrue(O._is_transient_error(exc))
        for exc in permanent:
            with self.subTest(exc=repr(exc)):
                self.assertFalse(O._is_transient_error(exc))

    def test_timeout_result_is_marked_retryable(self):
        with mock.patch.object(O, "find_student_by_email", side_effect=TimeoutError("timed out")):
            result = O.create_odoo_student({"first_name": "Ali", "email": "a@b.com", "gender": "Male"})
        self.assertFalse(result["success"])
        self.assertTrue(result["retryable"])

    def test_configuration_error_is_not_retryable(self):
        with mock.patch.object(O, "find_student_by_email", side_effect=O.OdooConnectionError("auth failed")):
            result = O.create_odoo_student({"first_name": "Ali", "email": "a@b.com", "gender": "Male"})
        self.assertFalse(result["success"])
        self.assertFalse(result.get("retryable"))

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
