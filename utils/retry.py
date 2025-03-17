from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
import os
import requests
from requests.exceptions import RequestException

@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(RequestException)
)
def fetch_bscscan_data(address: str) -> dict:
    api_key = os.getenv("BSCSCAN_API_KEY")
    if not api_key:
        raise ValueError("BSCSCAN_API_KEY must be set in environment variables.")
    api_url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&apikey={api_key}"
    response = requests.get(api_url, timeout=10)
    response.raise_for_status()
    return response.json()
