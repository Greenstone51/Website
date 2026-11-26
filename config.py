import os
import threading
from dotenv import load_dotenv

load_dotenv()

ADMIN_PASSWORD_HASH = "scrypt:32768:8:1$x2s5iN4SeE7BBcFa$1cb3f962e8b85692cfae9b2f1b12f9dbbec53e5a8980ac5074e981ff7c4ebbd2784bcddba9b1731f4215de19708cc069c0685d6282685a858aa7e562726fe722"
QUICK_CONNECT_PASSWORD_HASH = "scrypt:32768:8:1$Q8YJGxZrMFVkJmkf$b1e6267187f71ab8117a620ee8ebcf304d9c43f8cde1e53b2c2c398663db4344f64c2de988124fda25e8ae838c702006fdb5515eab40022b0e18cee12e9fff86"

VPN_SERVICE_NAME = "xray"
API_SERVICE_NAME = "website-backend.service"
XRAY_INTERNAL_PORT = 443

# Cloudflare Turnstile Konfiguration
TURNSTILE_SITEKEY = os.getenv("TURNSTILE_SITEKEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")

# Konfigurationen aus den Umgebungsvariablen abrufen
VLESS_LINK = os.getenv("VLESS_LINK", "https://greenstone51.de/404")
QR_CODE_NOTE = os.getenv("QR_CODE_NOTE", "Environment Variablen konnten nicht geladen werden. Kontaktiere den Systemadministrator via Email unter admin@greenstone51.de.")

# VAPID Keys fuer Web Push
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "private_key.pem")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@greenstone51.de")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_PASSWORD_HASH = "scrypt:32768:8:1$492vAuClA6HIQava$1825b07a42333eaab67359337cdd7612491d8b5c374d0bc50d3e88023716d470e0d9952e6c6868528bd10904036026f22d711ed86365d9fc82ded5ac81885572"
