import os
import json
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException

load_dotenv()

SUBSCRIPTIONS_FILE = os.path.abspath('push_subscriptions.json')
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "private_key.pem")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@greenstone51.de")

def test_push():
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        print("[FEHLER] 'push_subscriptions.json' existiert noch nicht. Es hat sich noch kein Browser registriert.")
        return

    with open(SUBSCRIPTIONS_FILE, 'r', encoding='utf-8') as f:
        subscriptions = json.load(f)

    if not subscriptions:
        print("[FEHLER] 'push_subscriptions.json' ist leer. Klicke auf der Webseite zuerst auf 'Push-Benachrichtigungen aktivieren'.")
        return

    print(f"[INFO] Gefundene Registrierungen: {len(subscriptions)}")

    priv_key = VAPID_PRIVATE_KEY
    if os.path.exists(priv_key):
        priv_key = os.path.abspath(priv_key)

    payload = json.dumps({
        "title": "Test Benachrichtigung",
        "body": "Dies ist ein direkter Test vom Server.",
        "url": "/download"
    })

    for idx, sub in enumerate(subscriptions):
        print(f"\n--- Sende Push an Client #{idx + 1} ---")
        try:
            response = webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=priv_key,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL}
            )
            print(f"[ERFOLG] Status-Code: {response.status_code}")
        except WebPushException as ex:
            print(f"[PUSH FEHLER] WebPushException: {ex}")
            if ex.response is not None:
                print(f"[PUSH FEHLER] Server Antworte mit: {ex.response.status_code} - {ex.response.text}")
        except Exception as ex:
            print(f"[FEHLER] Allgemeiner Ausfuehrungsfehler: {ex}")

if __name__ == '__main__':
    test_push()
