<?php
/**
 * WPCode Snippet: إرسال بيانات فورم القبول (Fluent Forms) إلى Flask Webhook بتاع Fajr-autom
 * (Odoo + Moodle + Email)
 *
 * التسجيل التلقائي لقسم الأون لاين فقط: السيرفر بيتجاهل On-site (study_mode)،
 * فالسنيبت بيبعت كل الطلبات والسيرفر هو اللي بيقرر.
 *
 * نوع الـ Snippet في WPCode: PHP Snippet
 * استبدل السنيبت القديم بالكامل بهذا (ماتسيبش نسختين شغالين مع بعض).
 */

// ===== إعدادات أساسية - عدّل هنا بس =====
if (!defined('FAJR_WEBHOOK_URL')) {
    define('FAJR_WEBHOOK_URL', 'https://fajr-autom.onrender.com/webhook');
}
// إلزامي: نفس قيمة WEBHOOK_SECRET اللي في Render بالظبط
if (!defined('FAJR_WEBHOOK_SECRET')) {
    define('FAJR_WEBHOOK_SECRET', '');
}
// Form ID بتاع فورم القبول (Fluent Forms > All Forms > الـ Shortcode: [fluentform id="X"])
if (!defined('FAJR_LEVEL_TEST_FORM_ID')) {
    define('FAJR_LEVEL_TEST_FORM_ID', 3);
}

add_action('fluentform_submission_inserted', 'fajr_send_to_webhook', 20, 3);

function fajr_log($message) {
    error_log('[Fajr Webhook] ' . $message);
}

function fajr_mask_email($email) {
    $parts = explode('@', (string) $email);
    return count($parts) === 2 ? substr($parts[0], 0, 2) . '***@' . $parts[1] : '***';
}

/**
 * Mapping: اسم الحقل اللي السيرفر محتاجه => أسماء ممكنة للحقل في الفورم (Fluent Forms)
 * الاسم الأول = الأساسي. الاسم بنقطة (names.first_name) معناه حقل جوه مجموعة.
 * لو اسم أي حقل في الفورم مختلف، ضيفه في القائمة بتاعته.
 */
function fajr_field_map() {
    return array(
        'first_name'     => array('first_name', 'names.first_name'),
        'last_name'      => array('last_name', 'names.last_name'),
        'arabic_name'    => array('arabic_name'),
        'mobile'         => array('mobile', 'phone'),
        'email'          => array('email'),
        'birth_date'     => array('birth_date', 'date_of_birth'),
        'gender'         => array('gender'),
        'study_mode'     => array('study_mode', 'studyMode', 'study_type'),
        // موعد اختبار تحديد المستوى اللي الطالب اختاره من الكالندر (اختياري)
        'placement_slot' => array('placement_slot', 'placement_date', 'exam_date'),
    );
}

/** يجيب قيمة حقل من بيانات الفورم، ويجرب كل الأسماء الممكنة بالترتيب */
function fajr_get_field($data, $candidates) {
    foreach ($candidates as $path) {
        $value = $data;
        foreach (explode('.', $path) as $part) {
            if (is_array($value) && array_key_exists($part, $value)) {
                $value = $value[$part];
            } else {
                $value = null;
                break;
            }
        }
        if (is_array($value)) {
            $value = implode(' ', array_filter($value, 'is_scalar'));
        }
        if (is_scalar($value) && trim((string) $value) !== '') {
            return sanitize_text_field((string) $value);
        }
    }
    return '';
}

/**
 * احتياطي: لو حقل نوع الدراسة اسمه مش في القائمة، ندور على أي حقل قيمته بالظبط Online أو On-site.
 * بيرجع array(اسم الحقل، القيمة) أو array(null, '').
 */
function fajr_detect_study_mode($data) {
    foreach ($data as $key => $value) {
        if (!is_string($value)) {
            continue;
        }
        $normalized = strtolower(preg_replace('/[\s\-_]+/', '', trim($value)));
        if ($normalized === 'online' || $normalized === 'onsite') {
            return array($key, $value);
        }
    }
    return array(null, '');
}

function fajr_send_to_webhook($entryId, $formData, $form) {

    // نشتغل بس على فورم القبول
    if ((int) $form->id !== (int) FAJR_LEVEL_TEST_FORM_ID) {
        return;
    }

    if (FAJR_WEBHOOK_SECRET === '') {
        fajr_log('FAJR_WEBHOOK_SECRET is empty - request NOT sent (the server would reject it with 403)');
        return;
    }

    // ===== بناء الـ payload من الفورم =====
    $payload = array();
    foreach (fajr_field_map() as $target => $candidates) {
        $payload[$target] = fajr_get_field($formData, $candidates);
    }
    $payload['email'] = sanitize_email($payload['email']);

    // نوع الدراسة: لازم يوصل للسيرفر (Online / On-site)
    if ($payload['study_mode'] === '') {
        list($detected_key, $detected_value) = fajr_detect_study_mode($formData);
        if ($detected_key !== null) {
            $payload['study_mode'] = sanitize_text_field($detected_value);
            fajr_log("study_mode found in field '{$detected_key}' (not in the mapping). Add '{$detected_key}' to fajr_field_map() to silence this notice.");
        } else {
            fajr_log('study_mode NOT FOUND - request NOT sent. Fields received: ' . implode(', ', array_keys($formData)));
            return;
        }
    }

    // لوج بدون بيانات شخصية: أسماء الحقول اللي اتبعتت + إيميل مخفي جزئيًا
    fajr_log('Sending: fields=' . implode(',', array_keys(array_filter($payload))) . ' email=' . fajr_mask_email($payload['email']));

    // ===== الإرسال =====
    $response = wp_remote_post(FAJR_WEBHOOK_URL, array(
        'timeout'     => 25, // ثواني - السيرفر بيكلم Odoo و Moodle و Brevo
        'headers'     => array(
            'Content-Type'    => 'application/json',
            'X-Webhook-Token' => FAJR_WEBHOOK_SECRET,
        ),
        'body'        => wp_json_encode($payload),
        'data_format' => 'body',
    ));

    // ===== نتيجة الطلب =====
    if (is_wp_error($response)) {
        fajr_log('ERROR: ' . $response->get_error_message());
        return;
    }

    $status_code = wp_remote_retrieve_response_code($response);
    $body        = wp_remote_retrieve_body($response);

    if ($status_code >= 200 && $status_code < 300) {
        // status: success / exists / skipped (On-site) / partial (شوف warnings)
        fajr_log("OK ({$status_code}): {$body}");
    } else {
        fajr_log("FAILED ({$status_code}): {$body}");
    }
}
