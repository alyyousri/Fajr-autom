from flask import Flask, request, jsonify
import requests
import random
import string
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from dotenv import load_dotenv
import os

# ===== Odoo Integration =====
from odoo_service import create_odoo_student

load_dotenv()

app = Flask(__name__)

MOODLE_URL = os.getenv("MOODLE_URL")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")

def generate_password():
    chars = string.ascii_letters
    random_part = ''.join(random.choices(chars, k=4))
    random_digits = ''.join(random.choices(string.digits, k=2))
    return f"Fajr@{random_part}{random_digits}"

def send_email(to_email, to_name, subject, body):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": to_email, "name": to_name}],
        sender={"email": "info@fajr.com", "name": "Fajr Online"},
        subject=subject,
        html_content=body
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        print(f"✅ Email sent to {to_email}")
    except ApiException as e:
        print(f"❌ Email error: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or request.form.to_dict()
    print(f"📥 Received: {data}")

    first_name = data.get('first_name', '')
    last_name  = data.get('last_name', '')
    email      = data.get('email', '')
    gender     = data.get('gender', '')

    # ===== Odoo Integration: حقول إضافية مطلوبة لـ op.student =====
    arabic_name = data.get('arabic_name', '')
    mobile      = data.get('mobile', '')
    birth_date  = data.get('birth_date') or data.get('date_of_birth') or ''

    # ===== Odoo Integration: إنشاء/تأكيد الطالب في Odoo =====
    # ده بيحصل بغض النظر عن حالة الطالب في Moodle (موجود أو جديد)
    # لأن وجوده في Moodle مش معناه إنه موجود في Odoo بالضرورة
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
        if odoo_result.get("success"):
            if odoo_result.get("already_existed"):
                print(f"ℹ️ Odoo: الطالب موجود بالفعل | id={odoo_result['odoo_student_id']}")
            else:
                print(f"✅ Odoo: تم إنشاء الطالب | id={odoo_result['odoo_student_id']}")
        else:
            print(f"❌ Odoo Error: {odoo_result.get('error')}")
    except Exception as e:
        # خطأ Odoo منوقفش عملية تسجيل الطالب في Moodle - بس بنسجله في الـ Log
        print(f"❌ Odoo Exception: {e}")
        odoo_result = {"success": False, "error": str(e)}

    # ابحث عن الطالب
    search_response = requests.get(
        f"{MOODLE_URL}/webservice/rest/server.php",
        params={
            "wstoken": MOODLE_TOKEN,
            "wsfunction": "core_user_get_users",
            "moodlewsrestformat": "json",
            "criteria[0][key]": "email",
            "criteria[0][value]": email,
        }
    )

    search_result = search_response.json()
    users = search_result.get("users", [])

    # كود الطالب بتاع الأودوو (gr_no) - هنضيفه في الإيميلات لو موجود
    student_code = (odoo_result or {}).get("gr_no") or ""
    student_code_html = (
        f"<p><b>كود الطالب:</b> {student_code}</p>" if student_code else ""
    )

    if users:
        user_id = users[0]["id"]
        print(f"⚠️ User already exists! ID: {user_id}")

        send_email(
            to_email=email,
            to_name=first_name,
            subject="أنت مسجل بالفعل في Fajr Online",
            body=f"""
            <div style="font-family: Arial; direction: rtl; text-align: right;">
                <h2>مرحباً {first_name}،</h2>
                <p>أنت مسجل بالفعل في <b>Fajr Online</b>.</p>
                {student_code_html}
                <p>يمكنك الدخول من هنا:
                <a href="https://learn.fajr.com">learn.fajr.com</a></p>
                <p>لو نسيت الباسورد:
                <a href="https://learn.fajr.com/login/forgot_password.php">اضغط هنا</a></p>
                <p>لو في مشكلة تواصل معنا على info@fajr.com</p>
            </div>
            """
        )
        return jsonify({
            "status": "exists",
            "user_id": user_id,
            "odoo": odoo_result,
        })

    # اعمل User جديد
    password = generate_password()

    user_data = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": "core_user_create_users",
        "moodlewsrestformat": "json",
        "users[0][firstname]": first_name,
        "users[0][lastname]":  last_name,
        "users[0][email]":     email,
        "users[0][username]":  email,
        "users[0][password]":  password,
    }

    response = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data=user_data
    )

    result = response.json()

    if not isinstance(result, list) or len(result) == 0:
        print(f"❌ Error creating user: {result}")
        return jsonify({"status": "error", "message": result}), 400

    user_id = result[0]['id']
    print(f"✅ New User created! ID: {user_id}")

    # Enrollment في تحديد المستوى
    enroll_data = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": "enrol_manual_enrol_users",
        "moodlewsrestformat": "json",
        "enrolments[0][roleid]": 5,
        "enrolments[0][userid]": user_id,
        "enrolments[0][courseid]": 20,
    }

    enroll_response = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data=enroll_data
    )

    if enroll_response.text == "null":
        print(f"✅ Enrolled in تحديد المستوى!")
    else:
        print(f"❌ Enrollment error: {enroll_response.text}")

    # إضافة للـ Group حسب الـ Gender
    group_id = 2 if gender == 'Male' else 1

    group_data = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": "core_group_add_group_members",
        "moodlewsrestformat": "json",
        "members[0][groupid]": group_id,
        "members[0][userid]": user_id,
    }

    group_response = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data=group_data
    )

    if group_response.text == "null":
        print(f"✅ Added to {'Male' if gender == 'Male' else 'Female'} group!")
    else:
        print(f"❌ Group error: {group_response.text}")

    # ابعت إيميل للطالب الجديد
    send_email(
        to_email=email,
        to_name=first_name,
        subject="مرحباً بك في Fajr Online 🎉",
        body=f"""
        <div style="font-family: Arial; direction: rtl; text-align: right;">
            <h2>مرحباً {first_name}،</h2>
            <p>تم تسجيلك بنجاح في <b>Fajr Online</b>!</p>
            <hr>
            {student_code_html}
            <h3>بيانات الدخول:</h3>
            <p><b>Username:</b> {email}</p>
            <p><b>Password:</b> {password}</p>
            <p><b>رابط الدخول:</b>
            <a href="https://learn.fajr.com">learn.fajr.com</a></p>
            <hr>
            <p>سيتم التواصل معك قريباً لتحديد موعد تحديد المستوى.</p>
            <p>لو في أي استفسار تواصل معنا على info@fajr.com</p>
        </div>
        """
    )

    return jsonify({
        "status": "success",
        "user_id": user_id,
        "odoo": odoo_result,
    })

if __name__ == '__main__':
    app.run(debug=True, port=5003)