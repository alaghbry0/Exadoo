import os
import logging
import aiohttp
from datetime import datetime, UTC
from web3 import Web3
from asyncpg.pool import Pool
from config import BSC_NODE_URL, USDT_CONTRACT_ADDRESS, BSCSCAN_API_KEY, ENCRYPTION_KEY
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class PaymentManager:
    def __init__(self, db_pool: Pool):
        self.db_pool = db_pool
        self.w3 = Web3(Web3.HTTPProvider(BSC_NODE_URL))
        self.usdt_contract = self.w3.eth.contract(
            address=USDT_CONTRACT_ADDRESS,
            abi=self._load_abi()
        )
        # إعداد التشفير للمفاتيح الخاصة
        self.cipher = Fernet(ENCRYPTION_KEY)

    @staticmethod
    def _load_abi():
        """ABI للتعامل مع عقد USDT"""
        return [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]

    async def create_payment_session(self, user_id: int, amount: float) -> dict:
        try:
            account = self.w3.eth.account.create()
            expires_at = datetime.now(UTC) + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

            # تشفير المفتاح الخاص قبل التخزين
            encrypted_key = self.cipher.encrypt(account.key)

            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO payment_addresses 
                    (user_id, address, private_key, amount, expires_at, confirmed)
                    VALUES ($1, $2, $3, $4, $5, FALSE)
                ''', user_id, account.address, encrypted_key.decode(), amount, expires_at)

            return {
                'address': account.address,
                'expires_at': expires_at.isoformat(),
                'amount': amount,
                'currency': 'USDT'
            }
        except Exception as e:
            logger.error(f"Payment session creation failed: {str(e)}")
            raise

    async def verify_payment(self, address: str) -> bool:
        try:
            async with self.db_pool.acquire() as conn:
                payment_data = await conn.fetchrow('''
                    SELECT * FROM payment_addresses
                    WHERE address = $1 AND expires_at > NOW() AND NOT confirmed
                ''', address)

            if not payment_data:
                return False

            # التحقق من المعاملة باستخدام BscScan API
            # تأكد من استخدام endpoint مناسب (Mainnet أو Testnet)
            api_url = f"https://api-testnet.bscscan.com/api?module=account&action=tokentx&address={address}&apikey={BSCSCAN_API_KEY}"

            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as resp:
                    if resp.status != 200:
                        logger.error(f"BscScan API returned status: {resp.status}")
                        return False
                    data = await resp.json()

            for tx in data.get('result', []):
                if self._validate_transaction(tx, payment_data):
                    await self._update_user_subscription(payment_data['user_id'])
                    await self._mark_as_paid(address)
                    return True

            return False
        except Exception as e:
            logger.error(f"Payment verification error: {str(e)}")
            return False

    @staticmethod
    def _validate_transaction(tx: dict, payment_data: dict) -> bool:
        required_confirmations = 3
        try:
            return all([
                tx['to'].lower() == payment_data['address'].lower(),
                tx['contractAddress'].lower() == USDT_CONTRACT_ADDRESS.lower(),
                float(tx['value']) / 1e18 == payment_data['amount'],
                int(tx['confirmations']) >= required_confirmations
            ])
        except Exception as e:
            logger.error(f"Transaction validation error: {str(e)}")
            return False

    async def _update_user_subscription(self, user_id: int):
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE users 
                SET subscription_end = COALESCE(subscription_end, NOW()) + INTERVAL '1 month'
                WHERE id = $1
            ''', user_id)

    async def _mark_as_paid(self, address: str):
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE payment_addresses 
                SET confirmed = TRUE 
                WHERE address = $1
            ''', address)
