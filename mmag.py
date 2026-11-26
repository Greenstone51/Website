"""Temporary, password-protected MMAG upload endpoint.

This module is intentionally isolated so the feature can be removed by deleting
this file and its template, then removing the blueprint registration in app.py.
Uploaded files are stored outside the static directory and no download/list route
is exposed.
"""
import os
import uuid
import shutil

from flask import Blueprint, current_app, render_template, request, session, redirect, url_for, abort
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from config import BASE_DIR

mmag_bp = Blueprint("mmag", __name__)

# Replace this value with the scrypt hash generated for the MMAG password.
# Keep the hash here (not the plaintext password) so the temporary feature is
# self-contained and can be removed by deleting mmag.py and mmag.html.
MMAG_PASSWORD_HASH = "scrypt:32768:8:1$492vAuClA6HIQava$1825b07a42333eaab67359337cdd7612491d8b5c374d0bc50d3e88023716d470e0d9952e6c6868528bd10904036026f22d711ed86365d9fc82ded5ac81885572"

MMAG_UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, "MMAG_uploads"))
os.makedirs(MMAG_UPLOAD_FOLDER, mode=0o700, exist_ok=True)
try:
    os.chmod(MMAG_UPLOAD_FOLDER, 0o700)
except OSError:
    pass


def _save_without_overwriting(file_storage, original_name):
    """Save one upload with private permissions and never overwrite a file."""
    filename = secure_filename(original_name) or "upload"
    stem, extension = os.path.splitext(filename)

    # O_EXCL makes name allocation atomic, including concurrent uploads.
    for attempt in range(100):
        candidate = filename if attempt == 0 else f"{stem}_{uuid.uuid4().hex[:10]}{extension}"
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


@mmag_bp.route("/MMAG", methods=["GET", "POST"])
def mmag_upload():
    if request.method == "POST" and not session.get("mmag_authenticated"):
        password = request.form.get("password", "")
        if not MMAG_PASSWORD_HASH or "REPLACE_WITH_" in MMAG_PASSWORD_HASH:
            current_app.logger.error("MMAG_PASSWORD_HASH has not been configured.")
            abort(503)
        if check_password_hash(MMAG_PASSWORD_HASH, password):
            session["mmag_authenticated"] = True
            return redirect(url_for("mmag.mmag_upload"))
        return render_template("mmag.html", authenticated=False, error="Falsches Passwort."), 401

    if not session.get("mmag_authenticated"):
        return render_template("mmag.html", authenticated=False)

    if request.method == "POST":
        raw_files = []
        for key in request.files:
            raw_files.extend(request.files.getlist(key))
        files = [item for item in raw_files if item and item.filename]

        if not files:
            return render_template(
                "mmag.html", authenticated=True, error="Keine Dateien ausgewählt."
            ), 400

        saved_names = []
        try:
            for item in files:
                saved_names.append(_save_without_overwriting(item, item.filename))
        except OSError:
            current_app.logger.exception("MMAG upload failed while saving files.")
            return render_template(
                "mmag.html",
                authenticated=True,
                error="Mindestens eine Datei konnte nicht gespeichert werden.",
            ), 500

        current_app.logger.info("MMAG upload stored %d file(s).", len(saved_names))
        return render_template(
            "mmag.html",
            authenticated=True,
            success=f"{len(saved_names)} Datei(en) erfolgreich hochgeladen.",
            uploaded_names=saved_names,
        )

    return render_template("mmag.html", authenticated=True)
