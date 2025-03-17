import unittest
from unittest.mock import patch
from app import app

class TestVerifyPayment(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    async def test_verify_payment(self):
        # بيانات وهمية لمحاكاة رد API لمعالجة دفعة بقيمة 25 دولار (25 USDT)
        dummy_bscscan_response = {
            "result": [{
                "tokenSymbol": "USDT",
                "to": "0xtestdepositaddress",
                "value": "25000000",  # 25 USDT عند تحويل القيمة
                "hash": "0xdummyhash123456789abcdef123456789abcdef123456789abcdef123456789abcdef"
            }]
        }

        # محاكاة دالة fetch_bscscan_data لتعيد البيانات الوهمية
        with patch('utils.retry.fetch_bscscan_data', return_value=dummy_bscscan_response):
            # محاكاة دالة is_transaction_confirmed لتعيد True مباشرةً
            with patch('services.confirmation_checker.is_transaction_confirmed', return_value=True):
                async with self.client as client:
                    response = await client.post('/api/verify-payment', json={
                        "webhookSecret": "61c6a9be0b7ab1688c0f619b59a6cd1260136492bfb168035077710902dc2963",  # تأكد من مطابقة القيمة مع البيئة
                        "telegramId": "7382197778",
                        "deposit_address": "0xtestdepositaddress"
                    })
                    self.assertEqual(response.status_code, 200)
                    data = await response.get_json()
                    self.assertTrue(data.get("success"))

if __name__ == "__main__":
    unittest.main()
