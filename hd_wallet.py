# hd_wallet.py
import os
from eth_account import Account
from eth_account.hdaccount import ETHEREUM_DEFAULT_PATH
from cryptography.fernet import Fernet
from typing import Dict, Any
import logging
from dotenv import load_dotenv

load_dotenv()

Account.enable_unaudited_hdwallet_features()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HdWalletGenerator:
    """فئة لإدارة محافظ HD المشفرة"""

    def __init__(self):
        self.mnemonic = self._load_mnemonic()
        self.cipher = self._init_cipher()

    @staticmethod
    def _load_mnemonic() -> str:
        mnemonic = os.getenv("MASTER_MNEMONIC")
        if not mnemonic:
            logger.error("MASTER_MNEMONIC غير موجود في البيئة")
            raise ValueError("العبارة السرية مطلوبة")
        if len(mnemonic.split()) not in (12, 24):
            logger.error("العبارة يجب أن تحتوي على 12 أو 24 كلمة")
            raise ValueError("صيغة غير صالحة")
        return mnemonic

    @staticmethod
    def _init_cipher() -> Fernet:
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            logger.error("ENCRYPTION_KEY مطلوب")
            raise ValueError("مفتاح التشفير مفقود")
        try:
            return Fernet(key.encode())
        except Exception as fernet_error:
            logger.error("فشل تهيئة التشفير: %s", fernet_error)
            raise

    def _encrypt_private_key(self, private_key: str) -> bytes:
        return self.cipher.encrypt(private_key.encode())

    def generate_child_wallet(self, index: int) -> Dict[str, Any]:
        try:
            derivation_path = f"{ETHEREUM_DEFAULT_PATH}/{index}"
            account = Account.from_mnemonic(self.mnemonic, account_path=derivation_path)
            return {
                "address": account.address,
                "encrypted_private_key": self._encrypt_private_key(account.key.hex()),
                "derivation_path": derivation_path,
                "index": index
            }
        except ValueError as ve:
            logger.error("خطأ في الاشتقاق: %s", ve)
            raise
        except Exception as gen_error:
            logger.error("خطأ غير متوقع: %s", gen_error)
            raise


def get_child_wallet(index: int) -> Dict[str, Any]:
    return HdWalletGenerator().generate_child_wallet(index)


if __name__ == "__main__":
    try:
        wallet = get_child_wallet(0)
        print(f"العنوان: {wallet['address']}")
        print(f"المسار: {wallet['derivation_path']}")
        print(f"المفتاح المشفر: {wallet['encrypted_private_key'].decode()}")
    except Exception as main_error:
        logger.error("فشل التشغيل: %s", main_error)