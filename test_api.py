import os
import pytest
import asyncpg
from quart.testing import QuartClient
import pytest_asyncio


os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "6379"
os.environ["WEBHOOK_SECRET"] = "testsecret"

from app import app


@pytest_asyncio.fixture(scope="module")
async def db_pool():
    pool = await asyncpg.create_pool(**DATABASE_CONFIG)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def test_client():
    app.config["TESTING"] = True
    async with app.test_client() as client:
        yield client


@pytest.mark.asyncio
async def test_confirm_payment(test_client: QuartClient):
    request_data = {
        "webhookSecret": os.getenv("WEBHOOK_SECRET"),
        "userWalletAddress": "0xTestWallet",
        "planId": "1",
        "telegramId": "123456",
        "telegramUsername": "testuser",
        "fullName": "Test User",
        "orderId": "order_123",
        "amount": "100"
    }
    response = await test_client.post("/api/confirm_payment", json=request_data)
    assert response.status_code == 200
    data = await response.get_json()
    assert "payment_token" in data
    assert "order_id" in data


@pytest.mark.asyncio
async def test_subscribe(test_client: QuartClient, db_pool: asyncpg.pool.Pool):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments 
            (payment_token, telegram_id, status)
            VALUES ($1, $2, $3)
            """,
                           "test_token", 123456, "pending"
                           )

    request_data = {
        "telegram_id": 123456,
        "subscription_plan_id": 1,
        "payment_token": "test_token"
    }
    headers = {"Authorization": f"Bearer {os.getenv('WEBHOOK_SECRET')}"}

    response = await test_client.post("/api/subscribe", json=request_data, headers=headers)
    assert response.status_code == 200
    data = await response.get_json()
    assert "invite_link" in data


@pytest.mark.asyncio
async def test_sse_stream(test_client: QuartClient):
    headers = {"X-Telegram-Id": "123456"}
    async with test_client.get("/api/sse?payment_token=test", headers=headers) as response:
        assert response.status_code == 200
        assert response.content_type == "text/event-stream"