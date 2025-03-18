from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import HexBytes
import os
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

bsc_rpc_url = os.getenv("BSC_RPC_URL")
if not bsc_rpc_url:
    raise ValueError("BSC_RPC_URL must be set in environment variables.")

w3 = Web3(Web3.HTTPProvider(bsc_rpc_url))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1))
def is_transaction_confirmed(tx_hash: str, required_confirmations: int = 3) -> bool:
    try:
        # تحقق من صيغة الـ hash أولاً
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            raise ValueError("تنسيق hash غير صالح")

        hex_bytes = HexBytes(tx_hash)
        receipt = w3.eth.get_transaction_receipt(hex_bytes)

        if receipt is None:
            logging.warning("المعاملة لم يتم تأكيدها بعد")
            return False

        current_block = w3.eth.block_number
        confirmations = current_block - receipt.blockNumber

        logging.info(f"🔍 عدد التأكيدات الحالية: {confirmations} (مطلوب: {required_confirmations})")

        return confirmations >= required_confirmations

    except TransactionNotFound:
        logging.warning("المعاملة غير موجودة في الشبكة")
        return False
    except Exception as err:
        logging.error(f"خطأ فني: {str(err)}")
        return False
