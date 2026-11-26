"""Temporary, password-protected MMAG upload endpoint.

Uploaded batches are stored as uncompressed ZIP archives outside the static
directory. The blueprint exposes no file listing, preview, or download route.
"""
import logging
import os
import shutil
import uuid
import zipfile

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
    logging.getLogger(__name__).warning("Could not enforce private permissions on MMAG_uploads.")


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


def _unique_member_name(original_name, name_prefix, used_names):
    """Build a safe prefixed ZIP member name and avoid duplicate member paths."""
    safe_filename = secure_filename(original_name) or "upload"
    stem, extension = os.path.splitext(safe_filename)
    base_name = f"{name_prefix}_{safe_filename}"
    candidate = base_name
    suffix = 2

    while candidate.casefold() in used_names:
        candidate = f"{name_prefix}_{stem}_{suffix}{extension}"
        suffix += 1

    used_names.add(candidate.casefold())
    return candidate


def _create_uncompressed_archive(file_storages, name_prefix):
    """Store one upload batch in a private ZIP archive without recompressing data."""
    safe_prefix = secure_filename(name_prefix).strip("._-")[:64].rstrip("._-")
    if not safe_prefix:
        raise ValueError("A valid name prefix is required.")

    archive_stem = f"{safe_prefix}_upload"
    for attempt in range(100):
        if attempt == 0:
            archive_name = f"{archive_stem}.zip"
        else:
            archive_name = f"{archive_stem}_{uuid.uuid4().hex[:10]}.zip"

        archive_path = os.path.join(MMAG_UPLOAD_FOLDER, archive_name)
        try:
            archive_fd = os.open(
                archive_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            break
        except FileExistsError:
            continue
    else:
        raise OSError("Could not allocate a unique archive filename.")

    try:
        with os.fdopen(archive_fd, "w+b") as archive_file:
            with zipfile.ZipFile(
                archive_file,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                used_names = set()
                for item in file_storages:
                    member_name = _unique_member_name(
                        item.filename,
                        safe_prefix,
                        used_names,
                    )
                    member_info = zipfile.ZipInfo(member_name)
                    member_info.compress_type = zipfile.ZIP_STORED
                    member_info.external_attr = 0o600 << 16

                    # Stream data in chunks; do not load large videos/photos into RAM.
                    with archive.open(member_info, mode="w", force_zip64=True) as destination:
                        item.stream.seek(0)
                        shutil.copyfileobj(item.stream, destination, length=1024 * 1024)

            archive_file.flush()
            os.fsync(archive_file.fileno())

        os.chmod(archive_path, 0o600)
    except Exception:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        raise

    return archive_name


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
            archive_name = _create_uncompressed_archive(files, name_prefix)
        except (OSError, ValueError, zipfile.BadZipFile):
            current_app.logger.exception("MMAG upload failed while creating ZIP archive.")
            return _upload_error(
                "Das ZIP-Archiv konnte nicht erstellt werden. Bitte prüfe die Dateien und versuche es erneut.",
                500,
            )

        current_app.logger.info(
            "MMAG upload stored %d file(s) in an uncompressed ZIP archive.",
            len(files),
        )
        message = (
            f"{len(files)} Datei(en) erfolgreich hochgeladen und "
            "ohne Komprimierung in einem ZIP-Archiv gespeichert."
        )

        if _is_xhr_request():
            # Do not return stored filenames or directory contents to the visitor.
            return jsonify({"success": True, "message": message})

        # Post/Redirect/Get prevents a page refresh from submitting the files again.
        flash(message, "success")
        return redirect(request.path, code=303)

    return render_template("mmag.html", authenticated=True)
