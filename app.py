import os
import io
import time
import uuid
import base64
import json
import threading
import subprocess
import requests
import psutil
import zipfile
import mimetypes
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file, abort
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

app.secret_key = os.urandom(32)

# ================= KONFIGURATION =================
ADMIN_PASSWORD_HASH = "scrypt:32768:8:1$x2s5iN4SeE7BBcFa$1cb3f962e8b85692cfae9b2f1b12f9dbbec53e5a8980ac5074e981ff7c4ebbd2784bcddba9b1731f4215de19708cc069c0685d6282685a858aa7e562726fe722"
QUICK_CONNECT_PASSWORD_HASH = "scrypt:32768:8:1$Q8YJGxZrMFVkJmkf$b1e6267187f71ab8117a620ee8ebcf304d9c43f8cde1e53b2c2c398663db4344f64c2de988124fda25e8ae838c702006fdb5515eab40022b0e18cee12e9fff86"

VPN_SERVICE_NAME = "xray"
API_SERVICE_NAME = "website-backend.service"
XRAY_INTERNAL_PORT = 443

# Konfigurationen aus den Umgebungsvariablen abrufen
VLESS_LINK = os.getenv("VLESS_LINK", "https://greenstone51.de/404")
QR_CODE_NOTE = os.getenv("QR_CODE_NOTE", "Environment Variablen konnten nicht geladen werden. Kontaktiere den Systemadministrator via Email unter admin@greenstone51.de.")

# VAPID Keys fuer Web Push
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "private_key.pem")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@greenstone51.de")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, 'uploads'))
DOWNLOAD_FOLDER = UPLOAD_FOLDER
CLEANUP_STATE_FILE = os.path.abspath(os.path.join(BASE_DIR, '.cleanup_state.json'))
PROTECTED_FILES_LIST = os.path.abspath(os.path.join(UPLOAD_FOLDER, '.protected_files.json'))
PUSH_SUBSCRIPTIONS_FILE = os.path.abspath(os.path.join(BASE_DIR, 'push_subscriptions.json'))

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SERVER_IP4 = ""
SERVER_IP6 = ""
try:
    SERVER_IP4 = requests.get("https://api.ipify.org?format=json", timeout=3).json()["ip"]
except: pass
try:
    SERVER_IP6 = requests.get("https://api64.ipify.org?format=json", timeout=3).json()["ip"]
except: pass

# ================= RATE LIMITING & CLEANUP =================
upload_attempts = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # Zeitfenster in Sekunden
RATE_LIMIT_MAX_REQUESTS = 10  # Max. Uploads pro Zeitfenster

def get_upload_client_id():
    if 'upload_client_id' not in session:
        session['upload_client_id'] = uuid.uuid4().hex
    return session['upload_client_id']

def is_rate_limited(client_id):
    now = time.time()
    upload_attempts[client_id] = [t for t in upload_attempts[client_id] if now - t < RATE_LIMIT_WINDOW]
    if len(upload_attempts[client_id]) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    upload_attempts[client_id].append(now)
    return False

def cleanup_uploads_folder():
    folder = app.config['UPLOAD_FOLDER']
    if not os.path.exists(folder):
        return
    files = [
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
    ]
    if len(files) > 50:
        files.sort(key=os.path.getmtime)
        files_to_delete = files[:len(files) - 50]
        for fpath in files_to_delete:
            fname = os.path.basename(fpath)
            if not is_file_protected(fname, fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

# ================= AUTOMATISCHE 14-TAGE LÖSCHUNG =================

def is_file_protected(filename, filepath):
    if filename.startswith('.'):
        return True

    if os.path.exists(PROTECTED_FILES_LIST):
        try:
            with open(PROTECTED_FILES_LIST, 'r', encoding='utf-8') as f:
                protected_files = json.load(f)
                if isinstance(protected_files, list) and filename in protected_files:
                    return True
        except Exception:
            pass

    return False

def biweekly_sunday_cleanup():
    folder = app.config['UPLOAD_FOLDER']
    if not os.path.exists(folder):
        return

    now = datetime.now()
    if now.weekday() != 6:
        return

    today_str = now.strftime('%Y-%m-%d')
    last_cleanup_str = None

    if os.path.exists(CLEANUP_STATE_FILE):
        try:
            with open(CLEANUP_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_cleanup_str = data.get('last_cleanup_date')
        except Exception:
            pass

    if last_cleanup_str:
        try:
            last_cleanup_date = datetime.strptime(last_cleanup_str, '%Y-%m-%d')
            if (now - last_cleanup_date).days < 12:
                return
        except ValueError:
            pass

    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        if os.path.isfile(fpath):
            if not is_file_protected(fname, fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    try:
        with open(CLEANUP_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'last_cleanup_date': today_str}, f)
    except Exception:
        pass

def start_scheduled_cleanup():
    def loop():
        while True:
            try:
                biweekly_sunday_cleanup()
            except Exception:
                pass
            time.sleep(3600)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

start_scheduled_cleanup()

# ================= WEB PUSH HELFER =================

def load_push_subscriptions():
    if os.path.exists(PUSH_SUBSCRIPTIONS_FILE):
        try:
            with open(PUSH_SUBSCRIPTIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_push_subscriptions(subscriptions):
    try:
        with open(PUSH_SUBSCRIPTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(subscriptions, f, indent=2)
    except Exception:
        pass

def send_web_push_notifications(title, message, target_url="/download"):
    try:
        from pywebpush import webpush, WebPushException
    except ImportError as e:
        print(f"[Push Fehler] {e}", flush=True)
        return

    subscriptions = load_push_subscriptions()
    print(f"[Push] Geladene Abonnements: {len(subscriptions)}", flush=True)
    if not subscriptions:
        return

    priv_key = VAPID_PRIVATE_KEY
    if not os.path.isabs(priv_key):
        priv_key = os.path.join(BASE_DIR, priv_key)

    if not os.path.exists(priv_key):
        print(f"[Push Fehler] Private Key nicht gefunden unter: {priv_key}", flush=True)
        return

    payload = json.dumps({
        "title": title,
        "body": message,
        "url": target_url
    })

    remaining_subscriptions = []
    has_changes = False

    for sub in subscriptions:
        try:
            res = webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=priv_key,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL}
            )
            remaining_subscriptions.append(sub)
            print(f"[Push Erfolg] Status {res.status_code} fuer Endpunkt: {sub.get('endpoint', '')[:40]}...", flush=True)
        except WebPushException as ex:
            print(f"[Push WebPushException] {ex}", flush=True)
            if ex.response is not None and ex.response.status_code in (404, 410):
                has_changes = True
            else:
                remaining_subscriptions.append(sub)
        except Exception as ex:
            remaining_subscriptions.append(sub)
            print(f"[Push Allgemeiner Fehler] {ex}", flush=True)

    if has_changes:
        save_push_subscriptions(remaining_subscriptions)

# ================= ROUTES =================

@app.route('/')
def root_page():
    return render_template('index.html')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory(app.static_folder, 'sw.js')
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/robots.txt')
@app.route('/sitemap.xml')
def static_from_root():
    return send_from_directory(app.static_folder, request.path[1:])

@app.route('/vpn51')
def vpn51_page():
    return render_template('vpn51.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_page():
    client_id = get_upload_client_id()

    if request.method == 'POST':
        if is_rate_limited(client_id):
            abort(429)

        raw_files = []
        for key in request.files:
            raw_files.extend(request.files.getlist(key))

        files = [f for f in raw_files if f and f.filename != '']

        if not files:
            print("[Upload Warning] POST empfangen, aber keine Dateien im Formular gefunden.", flush=True)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Keine Dateien empfangen.'}), 400
            return render_template('upload.html', message="Keine Datei ausgewählt.", success=False)

        custom_name_raw = request.form.get('custom_name', '').strip()

        count = 0
        uploaded_names = []
        total_files = len(files)

        for idx, file in enumerate(files):
            orig_filename = secure_filename(file.filename)
            if not orig_filename:
                orig_filename = f"upload_{uuid.uuid4().hex[:8]}"
            
            _, ext = os.path.splitext(orig_filename)

            if custom_name_raw:
                clean_custom = secure_filename(custom_name_raw)
                if not clean_custom:
                    clean_custom = f"upload_{uuid.uuid4().hex[:8]}"
                
                custom_base, custom_ext = os.path.splitext(clean_custom)
                if not custom_ext and ext:
                    file_ext = ext
                    base_name = clean_custom
                else:
                    file_ext = custom_ext
                    base_name = custom_base

                if total_files > 1:
                    filename = f"{base_name}_{idx + 1}{file_ext}"
                else:
                    filename = f"{base_name}{file_ext}"
            else:
                filename = orig_filename

            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)
            os.utime(save_path, None)
            count += 1
            uploaded_names.append(filename)

        cleanup_uploads_folder()

        if count > 0:
            push_title = "Neuer Datei-Upload"
            if count == 1:
                push_body = f"Datei '{uploaded_names[0]}' wurde hochgeladen."
            else:
                push_body = f"{count} neue Dateien wurden hochgeladen."
            
            print(f"[Upload] {count} Datei(en) gespeichert. Starte Push-Benachrichtigung...", flush=True)
            send_web_push_notifications(push_title, push_body, "/download")

        msg = f"{count} Datei(en) erfolgreich hochgeladen."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': msg})
        
        return render_template('upload.html', message=msg, success=True)
            
    return render_template('upload.html')

@app.route('/download')
def download_page():
    return render_template('download.html')

# ==============================================================================
# TEMPORÄRE ROUTE: /religion (ANFANG) - Kann nach Nutzung komplett gelöscht werden
# ==============================================================================

RELIGION_JSON_FILE = os.path.join(BASE_DIR, 'religion_songs.json')

def load_religion_songs():
    if os.path.exists(RELIGION_JSON_FILE):
        try:
            with open(RELIGION_JSON_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_religion_songs(songs):
    try:
        with open(RELIGION_JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(songs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

@app.route('/religion', methods=['GET', 'POST'])
def religion_page():
    message = None
    success = False

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        author = request.form.get('author', '').strip()

        if title and author:
            songs = load_religion_songs()
            new_entry = {
                'id': uuid.uuid4().hex[:8],
                'title': title,
                'author': author,
                'timestamp': datetime.now().strftime('%d.%m.%Y %H:%M')
            }
            songs.insert(0, new_entry)
            save_religion_songs(songs)
            return redirect(url_for('religion_page'))
        else:
            message = "Bitte sowohl den Songtitel als auch den Autor/Künstler eintragen."
            success = False

    songs = load_religion_songs()
    return render_template('religion.html', songs=songs, message=message, success=success)

# ==============================================================================
# TEMPORÄRE ROUTE: /religion (ENDE)
# ==============================================================================

@app.route('/vpn51/admin', methods=['GET', 'POST'])
def admin_page():
    if request.method == 'POST':
        password = request.form.get('password', '')
        valid = check_password_hash(ADMIN_PASSWORD_HASH, password) if ADMIN_PASSWORD_HASH else False

        if valid:
            session['logged_in'] = True
            return redirect(url_for('admin_page'))
        return render_template('admin.html', login_mode=True, error="Zutritt verweigert! Falsches Passwort.")

    if not session.get('logged_in'):
        return render_template('admin.html', login_mode=True)
    return render_template('admin.html', login_mode=False)

@app.route('/vpn51/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('qr_access', None)
    return redirect(url_for('admin_page'))

# ================= PUSH SUBSCRIPTION API =================

@app.route('/api/push/public-key', methods=['GET'])
def get_push_public_key():
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    sub_data = request.get_json()
    if not sub_data or 'endpoint' not in sub_data:
        return jsonify({'success': False, 'message': 'Ungueltige Subscription-Daten.'}), 400

    subscriptions = load_push_subscriptions()
    if not any(s.get('endpoint') == sub_data.get('endpoint') for s in subscriptions):
        subscriptions.append(sub_data)
        save_push_subscriptions(subscriptions)
        print(f"[Push Subscribed] Neuer Endpunkt registriert: {sub_data.get('endpoint')[:40]}...", flush=True)

    return jsonify({'success': True})

@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    sub_data = request.get_json()
    if not sub_data or 'endpoint' not in sub_data:
        return jsonify({'success': False, 'message': 'Ungueltige Subscription-Daten.'}), 400

    subscriptions = load_push_subscriptions()
    new_subs = [s for s in subscriptions if s.get('endpoint') != sub_data.get('endpoint')]
    save_push_subscriptions(new_subs)
    print(f"[Push Unsubscribed] Endpunkt entfernt: {sub_data.get('endpoint')[:40]}...", flush=True)

    return jsonify({'success': True})

# ================= ERROR HANDLER =================

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(413)
def request_entity_too_large(e):
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'message': 'Datei(en) zu groß! Maximal 100 MB.'}), 413
    return render_template('upload.html', message="Datei ist zu groß! Maximal 100 MB.", success=False), 413

@app.errorhandler(429)
def ratelimit_handler(e):
    msg = "Zu viele Upload-Versuche! Bitte warte einen Moment."
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'message': msg}), 429
    return render_template('upload.html', message=msg, success=False), 429

# ================= DOWNLOAD API ENDPOINTS =================

@app.route('/download/api/files', methods=['GET'])
def api_list_files():
    folder = app.config['DOWNLOAD_FOLDER']
    file_list = []
    if os.path.exists(folder):
        for fname in os.listdir(folder):
            fpath = os.path.join(folder, fname)
            if os.path.isfile(fpath) and not fname.startswith('.'):
                stat = os.stat(fpath)
                file_list.append({
                    'name': fname,
                    'size': stat.st_size,
                    'mtime': stat.st_mtime
                })
    return jsonify(file_list)

@app.route('/download/file/<path:filename>', methods=['GET'])
def download_file(filename):
    try:
        return send_from_directory(
            app.config['DOWNLOAD_FOLDER'],
            filename,
            as_attachment=True
        )
    except FileNotFoundError:
        abort(404)

@app.route('/download/zip', methods=['POST'])
def download_zip():
    data = request.get_json() or {}
    filenames = data.get('files', [])
    if not filenames:
        return jsonify({'error': 'Keine Dateien ausgewählt'}), 400

    memory_file = io.BytesIO()
    base_dir = app.config['DOWNLOAD_FOLDER']

    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in filenames:
            target_path = os.path.abspath(os.path.join(base_dir, fname))
            if os.path.commonpath([base_dir, target_path]) == base_dir and os.path.isfile(target_path):
                arcname = os.path.basename(target_path)
                zf.write(target_path, arcname=arcname)

    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name='greenstone51_downloads.zip'
    )

# ================= PUBLIC API ENDPOINTS =================

@app.route('/vpn51/api/stats')
def api_stats():
    client_ip = request.headers.get('X-Forwarded-For', request.headers.get('CF-Connecting-IP', request.remote_addr))
    if client_ip and ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()

    is_protected = False
    if client_ip and client_ip not in ["127.0.0.1", "::1"]:
        if client_ip == SERVER_IP4 or client_ip == SERVER_IP6:
            is_protected = True

    ip_type = "IPv6" if client_ip and ":" in client_ip else "IPv4"

    try:
        res = subprocess.run(f"systemctl is-active {VPN_SERVICE_NAME}", shell=True, capture_output=True, text=True)
        vpn_status = res.stdout.strip()
        vpn_online = (vpn_status == "active")
    except:
        vpn_online = False

    try:
        cmd = f"ss -tn 'sport = :{XRAY_INTERNAL_PORT}' | grep -c ESTAB"
        active_users = int(subprocess.check_output(cmd, shell=True).decode().strip())
    except:
        active_users = 0

    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent

    return jsonify({
        "protected": is_protected, "client_ip": client_ip, "ip_type": ip_type,
        "vpn_online": vpn_online, "active_users": active_users, "cpu": cpu, "ram": ram
    })

@app.route('/vpn51/api/login', methods=['POST'])
def api_quick_login():
    data = request.get_json()
    if not data or 'password' not in data:
        return jsonify({"error": "Passwort fehlt"}), 400
    
    password = data.get('password')
    if QUICK_CONNECT_PASSWORD_HASH and check_password_hash(QUICK_CONNECT_PASSWORD_HASH, password):
        session['qr_access'] = True
        return jsonify({
            "success": True, 
            "vless": VLESS_LINK,
            "note": QR_CODE_NOTE
        }), 200
    
    return jsonify({"error": "Falsches Passwort"}), 401

@app.route('/vpn51/api/qrcode')
def api_qrcode():
    if not session.get('qr_access') and not session.get('logged_in'):
        return "Unauthorized", 401

    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(VLESS_LINK)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except: return ""

# ================= PROTECTED ADMIN API ENDPOINTS =================

@app.route('/vpn51/api/admin/sysinfo')
def admin_sysinfo():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        res_uptime = subprocess.run("uptime -p", shell=True, capture_output=True, text=True)
        uptime = res_uptime.stdout.strip().replace("up ", "")
        if not uptime: uptime = "Unbekannt"
    except:
        uptime = "Unbekannt"

    try:
        res_disk = subprocess.run("df -h / | awk 'NR==2 {print $5}'", shell=True, capture_output=True, text=True)
        disk = res_disk.stdout.strip()
        if not disk: disk = "0%"
    except:
        disk = "0%"

    try:
        res_xray = subprocess.run(f"systemctl is-active {VPN_SERVICE_NAME}", shell=True, capture_output=True, text=True)
        xray_state = res_xray.stdout.strip()
        if not xray_state: xray_state = "unknown"
    except:
        xray_state = "error"

    try:
        res_api = subprocess.run(f"systemctl is-active {API_SERVICE_NAME}", shell=True, capture_output=True, text=True)
        api_state = res_api.stdout.strip()
        if not api_state: api_state = "unknown"
    except:
        api_state = "error"

    return jsonify({
        "uptime": uptime,
        "disk_usage": disk,
        "xray_status": xray_state,
        "api_status": api_state
    })

@app.route('/vpn51/api/admin/execute', methods=['POST'])
def admin_execute():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    action = data.get("action")

    try:
        if action == "start_xray":
            subprocess.Popen(f"sudo systemctl start {VPN_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Xray-Core wird gestartet..."})
        elif action == "stop_xray":
            subprocess.Popen(f"sudo systemctl stop {VPN_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Xray-Core wurde gestoppt."})
        elif action == "restart_xray":
            subprocess.Popen(f"sudo systemctl restart {VPN_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Xray-Core wird neugestartet..."})

        elif action == "start_api":
            subprocess.Popen(f"sudo systemctl start {API_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Web-API wird gestartet..."})
        elif action == "stop_api":
            subprocess.Popen(f"sleep 1 && sudo systemctl stop {API_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Web-API wird heruntergefahren! Das Dashboard ist danach offline."})
        elif action == "restart_api":
            subprocess.Popen(f"sleep 1 && sudo systemctl restart {API_SERVICE_NAME}", shell=True)
            return jsonify({"success": True, "msg": "Web-API wird neugestartet..."})

        elif action == "reboot_server":
            subprocess.Popen("sleep 1 && sudo reboot", shell=True)
            return jsonify({"success": True, "msg": "Ubuntu-Server wird rebootet! Verbindung bricht gleich ab."})
        elif action == "shutdown_server":
            subprocess.Popen("sleep 1 && sudo poweroff", shell=True)
            return jsonify({"success": True, "msg": "Server wird heruntergefahren und ausgeschaltet."})

        return jsonify({"success": False, "msg": "Unbekannte Aktion"}), 400
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
