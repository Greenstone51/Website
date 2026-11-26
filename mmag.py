"""Temporary, password-protected MMAG upload endpoint.

Uploaded files are stored outside the static directory. The blueprint exposes
no file listing, preview, or download endpoint.
"""
import os
import shutil
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from config import BASE_DIR

mmag_bp = Blueprint("mmag", __name__)

# Keep only the scrypt hash here, never the plaintext password.
MMAG_PASSWORD_HASH = "scrypt:32768:8:1$492vAuClA6HIQava$1825b07a42333eaab67359337cdd7612491d8b5c374d0bc50d3e88023716d470e0d9952e6c6868528bd10904036026f22d711ed86365d9fc82ded5ac81885572"

MMAG_UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, "MMAG_uploads"))
os.makedirs(MMAG_UPLOAD_FOLDER, mode=0o700, exist_ok=True)
try:
    os.chmod(MMAG_UPLOAD_FOLDER, 0o700)
except OSError:
    current_app.logger.warning("Could not enforce private permissions on MMAG_uploads.")


def _is_xhr_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _upload_error(message, status_code):
    """Return upload errors as JSON for the progress-bar client, otherwise HTML."""
    if _is_xhr_request():
        return jsonify({"success": False, "message": message}), status_code
    return render_template(
        "mmag.html",
        authenticated=True,
        error=message,
    ), status_code


def _save_without_overwriting(file_storage, original_name, name_prefix):
    """Save a prefixed file with private permissions without overwriting another."""
    safe_filename = secure_filename(original_name) or "upload"
    safe_prefix = secure_filename(name_prefix).strip("._-")[:64].rstrip("._-")
    if not safe_prefix:
        raise ValueError("A valid name prefix is required.")

    stem, extension = os.path.splitext(safe_filename)
    prefixed_name = f"{safe_prefix}_{safe_filename}"

    # O_EXCL makes filename allocation atomic, including concurrent uploads.
    for attempt in range(100):
        if attempt == 0:
            candidate = prefixed_name
        else:
            candidate = f"{safe_prefix}_{stem}_{uuid.uuid4().hex[:10]}{extension}"

        path = os.path.join(MMAG_UPLOAD_FOLDER, candidate)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise OSError("Could not allocate a unique filename.")

    try:
        with os.fdopen(fd, "wb") as destination:
            file_storage.stream.seek(0)
            shutil.copyfileobj(file_storage.stream, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    return candidate


# Flask routes are case-sensitive, so register both spellings explicitly.
@mmag_bp.route("/MMAG", methods=["GET", "POST"])
@mmag_bp.route("/mmag", methods=["GET", "POST"])
def mmag_upload():
    if request.method == "POST" and not session.get("mmag_authenticated"):
        password = request.form.get("password", "")
        if not MMAG_PASSWORD_HASH or "REPLACE_WITH_" in MMAG_PASSWORD_HASH:
            current_app.logger.error("MMAG_PASSWORD_HASH has not been configured.")
            abort(503)

        if check_password_hash(MMAG_PASSWORD_HASH, password):
            session["mmag_authenticated"] = True
            # Preserve the spelling the visitor used.
            return redirect(request.path, code=303)

        return render_template(
            "mmag.html",
            authenticated=False,
            error="Falsches Passwort.",
        ), 401

    if not session.get("mmag_authenticated"):
        return render_template("mmag.html", authenticated=False)

    if request.method == "POST":
        raw_files = []
        for key in request.files:
            raw_files.extend(request.files.getlist(key))
        files = [item for item in raw_files if item and item.filename]

        if not files:
            return _upload_error("Keine Dateien ausgewählt.", 400)

        submitted_name = request.form.get("name", "").strip()
        name_prefix = secure_filename(submitted_name).strip("._-")[:64].rstrip("._-")
        if not name_prefix:
            return _upload_error(
                "Bitte gib einen gültigen Namen ein. Der Name muss Buchstaben oder Zahlen enthalten.",
                400,
            )

        try:
            for item in files:
                _save_without_overwriting(item, item.filename, name_prefix)
        except (OSError, ValueError):
            current_app.logger.exception("MMAG upload failed while saving files.")
            return _upload_error(
                "Mindestens eine Datei konnte nicht gespeichert werden.",
                500,
            )

        current_app.logger.info("MMAG upload stored %d file(s).", len(files))
        message = f"{len(files)} Datei(en) erfolgreich hochgeladen."

        if _is_xhr_request():
            # Do not return stored filenames or directory contents to the visitor.
            return jsonify({"success": True, "message": message})

        # Post/Redirect/Get prevents a page refresh from submitting the files again.
        flash(message, "success")
        return redirect(request.path, code=303)

    return render_template("mmag.html", authenticated=True)
