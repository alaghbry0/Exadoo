import base64
from pytoniq import Cell  # نفترض أنّ الدوال موجودة كما في الوثائق


def test_parse_payload(payload_b64: str):
    # فك تشفير Base64 للحصول على بايتات الـ BOC
    boc_bytes = base64.b64decode(payload_b64)

    # إنشاء خلية من الـ BOC
    cell = Cell.one_from_boc(boc_bytes)

    # بدء تحليل الخلية
    parser = cell.begin_parse()

    # قراءة op_code الرئيسي (32 بت)
    op_code = parser.load_uint(32)
    print("Op code:", hex(op_code))

    # التأكد أن الـ op_code هو تحويل Jetton (0x7362D09C)
    if op_code != 0x7362D09C:
        print("هذا ليس payload تحويل Jetton")
        return

    # تخطي query_id (64 بت)
    parser.load_bits(64)

    # قراءة مبلغ الـ Jetton (يتم تحويله من وحدات coins إلى TON)
    jetton_amount = parser.load_coins() / 1e9
    print("Jetton amount:", jetton_amount)

    # قراءة عنوان المرسل
    jetton_sender = parser.load_address().to_str(1, 1, 1)
    print("Jetton sender:", jetton_sender)

    # التحقق من وجود forward payload
    if parser.load_bit():
        forward_parser = parser.load_ref().begin_parse()
    else:
        forward_parser = parser

    # التحقق مما إذا كان هناك 32 بت متبقية قبل قراءة op_code
    if len(forward_parser.bits) < 32:
        print("لا يوجد forward payload (تعليق)")
    else:
        # قراءة op_code داخل forward payload
        forward_op_code = forward_parser.load_uint(32)
        print("Forward payload op code:", forward_op_code)
        # إذا كان op_code = 0 فهذا يعني أن هناك تعليق نصي
        if forward_op_code == 0:
            comment = forward_parser.load_snake_string()
            print("Extracted comment:", comment)
        else:
            print("Unknown forward payload op code:", forward_op_code)


# payload التجريبي كما قدمته
test_payload = "te6cckEBAgEAZQABYHNi0JwAAAAAAAAAACJxCAAs3xj6UriBzrC/pXVzEikcSiNrvB+o8YmJcoIXc3/FlwEAYAAAAABvcmRlcklkOjUwZDA2N2JiLTg2YmYtNDg2MS1iNzRiLWI5YTVlYzBhNjA0MRcuC1k="

# تشغيل الاختبار
test_parse_payload(test_payload)
