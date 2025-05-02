import requests

BOT_TOKEN = "7942179576:AAF77Tfhb8pjevtf9Qd4bsju0h0gncZjSyI"

def handle_callback_queries():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    response = requests.get(url).json()
    for update in response.get('result', []):
        if 'callback_query' in update:
            user = update['callback_query']['from']
            print(f"User ID: {user['id']}, Username: @{user.get('username', 'None')}")