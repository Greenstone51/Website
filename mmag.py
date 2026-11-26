"""Temporary, password-protected MMAG upload endpoint.

Large uploads are sent in resumable chunks. The server then assembles all files
into a private, uncompressed ZIP64 archive in a background thread.
"""
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zipfile

from flask import (
    Blueprint,
    abort,
    current_app,
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
MMAG_STAGING_FOLDER = os.path.join(MMAG_UPLOAD_FOLDER, ".staging")
MMAG_CHUNK_SIZE = 32 * 1024 * 1024
MMAG_COPY_BUFFER_SIZE = 1024 * 1024
MMAG_ARCHIVE_PROGRESS_INTERVAL = 64 * 1024 * 1024

os.makedirs(MMAG_UPLOAD_FOLDER, mode=0o700, exist_ok=True)
os.makedirs(MMAG_STAGING_FOLDER, mode=0o700, exist_ok=True)
for _private_directory in (MMAG_UPLOAD_FOLDER, MMAG_STAGING_FOLDER):
    try:
        os.chmod(_private_directory, 0o700)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not enforce private permissions on %s.",
            _private_directory,
        )


def _is_xhr_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _json_error(message, status_code):
    return jsonify({"success": False, "message": message}), status_code


def _is_valid_upload_id(upload_id):
    return bool(re.fullmatch(r"[a-f0-9]{32}", upload_id or ""))


def _staging_path(upload_id):
    if not _is_valid_upload_id(upload_id):
        abort(400)
    return os.path.join(MMAG_STAGING_FOLDER, upload_id)


def _metadata_path(upload_id):
    return os.path.join(_staging_path(upload_id), "metadata.json")


def _status_path(upload_id):
    return os.path.join(_staging_path(upload_id), "status.json")


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return None


def _write_json_atomic(path, payload):
    """Atomically replace a private JSON state file."""
    directory = os.path.dirname(path)
    temporary_path = os.path.join(directory, f".state-{uuid.uuid4().hex}.tmp")
    fd = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, ensure_ascii=True, separators=(",", ":"))
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise


def _load_upload_metadata(upload_id):
    staging_dir = _staging_path(upload_id)
    metadata = _load_json_file(os.path.join(staging_dir, "metadata.json"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), list):
        abort(404)
    return metadata


def _chunk_count(file_size):
    return max(1, (file_size + MMAG_CHUNK_SIZE - 1) // MMAG_CHUNK_SIZE)


def _expected_chunk_size(file_size, chunk_index):
    if file_size == 0:
        return 0
    start = chunk_index * MMAG_CHUNK_SIZE
    return min(MMAG_CHUNK_SIZE, file_size - start)


def _chunk_path(upload_id, file_index, chunk_index):
    staging_dir = _staging_path(upload_id)
    return os.path.join(
        staging_dir,
        f"file_{file_index:08d}",
        f"chunk_{chunk_index:08d}.part",
    )


def _unique_member_name(original_name, name_prefix, used_names):
    """Build a safe prefixed ZIP member name and avoid duplicate member paths."""
    safe_filename = secure_filename(original_name) or "upload"
    stem, extension = os.path.splitext(safe_filename)
    candidate = f"{name_prefix}_{safe_filename}"
    suffix = 2

    while candidate.casefold() in used_names:
        candidate = f"{name_prefix}_{stem}_{suffix}{extension}"
        suffix += 1

    used_names.add(candidate.casefold())
    return candidate


def _write_upload_status(upload_id, state, processed_bytes, total_bytes, file_count, message):
    _write_json_atomic(
        _status_path(upload_id),
        {
            "state": state,
            "processed_bytes": int(processed_bytes),
            "total_bytes": int(total_bytes),
            "file_count": int(file_count),
            "message": message,
        },
    )


def _archive_path_for_prefix(name_prefix, upload_id, staging_dir):
    """Create the archive in staging, then atomically publish it in MMAG_uploads."""
    # The upload ID makes the final name unique even when the same person starts
    # several uploads with the same name at the same time.
    archive_name = f"{name_prefix}_upload_{upload_id[:12]}.zip"
    final_path = os.path.join(MMAG_UPLOAD_FOLDER, archive_name)
    temporary_path = os.path.join(staging_dir, "archive.building")

    if os.path.exists(final_path):
        # The previous worker may have completed the atomic rename but stopped
        # before it could write the completed status.
        return archive_name, final_path, None, None

    # A process may have been terminated while it was writing the previous
    # temporary archive. Only this upload's finalizer can own this staging file.
    try:
        os.remove(temporary_path)
    except FileNotFoundError:
        pass

    fd = os.open(
        temporary_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    return archive_name, final_path, temporary_path, fd


def _finalize_upload_worker(upload_id):
    """Create the final ZIP in the background, updating progress on disk."""
    staging_dir = _staging_path(upload_id)
    temporary_archive_path = None
    final_archive_path = None

    try:
        metadata = _load_upload_metadata(upload_id)
        files = metadata["files"]
        name_prefix = metadata["name_prefix"]
        total_bytes = metadata["total_bytes"]
        file_count = len(files)

        archive_name, final_archive_path, temporary_archive_path, archive_fd = (
            _archive_path_for_prefix(name_prefix, upload_id, staging_dir)
        )

        if archive_fd is None:
            # An atomic rename from an earlier worker already published the ZIP.
            _write_upload_status(
                upload_id,
                "completed",
                total_bytes,
                total_bytes,
                file_count,
                f"{file_count} Datei(en) erfolgreich in einem unkomprimierten ZIP-Archiv gespeichert.",
            )
            for file_index in range(file_count):
                shutil.rmtree(
                    os.path.join(staging_dir, f"file_{file_index:08d}"),
                    ignore_errors=True,
                )
            try:
                os.remove(os.path.join(staging_dir, "metadata.json"))
            except OSError:
                pass
            return

        processed_bytes = 0
        next_status_update = MMAG_ARCHIVE_PROGRESS_INTERVAL

        _write_upload_status(
            upload_id, "processing", 0, total_bytes, file_count,
            "ZIP-Archiv wird erstellt.",
        )

        with os.fdopen(archive_fd, "w+b") as archive_file:
            with zipfile.ZipFile(
                archive_file,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                used_names = set()

                for file_index, file_info in enumerate(files):
                    member_name = _unique_member_name(
                        file_info["filename"],
                        name_prefix,
                        used_names,
                    )
                    member_info = zipfile.ZipInfo(member_name)
                    member_info.compress_type = zipfile.ZIP_STORED
                    member_info.external_attr = 0o600 << 16

                    with archive.open(
                        member_info,
                        mode="w",
                        force_zip64=True,
                    ) as destination:
                        for chunk_index in range(file_info["chunk_count"]):
                            part_path = _chunk_path(
                                upload_id,
                                file_index,
                                chunk_index,
                            )
                            with open(part_path, "rb") as part:
                                while True:
                                    block = part.read(MMAG_COPY_BUFFER_SIZE)
                                    if not block:
                                        break
                                    destination.write(block)
                                    processed_bytes += len(block)

                                    if processed_bytes >= next_status_update:
                                        _write_upload_status(
                                            upload_id,
                                            "processing",
                                            processed_bytes,
                                            total_bytes,
                                            file_count,
                                            "ZIP-Archiv wird erstellt.",
                                        )
                                        next_status_update = (
                                            processed_bytes
                                            + MMAG_ARCHIVE_PROGRESS_INTERVAL
                                        )

                    _write_upload_status(
                        upload_id,
                        "processing",
                        processed_bytes,
                        total_bytes,
                        file_count,
                        f"ZIP-Archiv wird erstellt ({file_index + 1}/{file_count} Dateien).",
                    )

            archive_file.flush()
            os.fsync(archive_file.fileno())

        os.chmod(temporary_archive_path, 0o600)
        os.replace(temporary_archive_path, final_archive_path)
        temporary_archive_path = None

        # Publish the completed state before removing source chunks. If cleanup
        # later fails, the finished archive is still available and the client
        # can safely observe a completed upload.
        _write_upload_status(
            upload_id,
            "completed",
            total_bytes,
            total_bytes,
            file_count,
            f"{file_count} Datei(en) erfolgreich in einem unkomprimierten ZIP-Archiv gespeichert.",
        )

        # Source chunks are no longer required once the complete archive exists.
        for file_index in range(file_count):
            shutil.rmtree(
                os.path.join(staging_dir, f"file_{file_index:08d}"),
                ignore_errors=True,
            )
        try:
            os.remove(os.path.join(staging_dir, "metadata.json"))
        except OSError:
            pass

        logging.getLogger(__name__).info(
            "MMAG upload completed: %d file(s), uncompressed ZIP archive created.",
            file_count,
        )

    except Exception:
        logging.getLogger(__name__).exception(
            "MMAG background archive creation failed for upload %s.",
            upload_id,
        )
        if temporary_archive_path:
            try:
                os.remove(temporary_archive_path)
            except OSError:
                pass
        if final_archive_path:
            try:
                os.remove(final_archive_path)
            except OSError:
                pass
        try:
            metadata = _load_json_file(os.path.join(staging_dir, "metadata.json")) or {}
            _write_upload_status(
                upload_id,
                "failed",
                0,
                int(metadata.get("total_bytes", 0)),
                len(metadata.get("files", [])),
                "ZIP-Erstellung fehlgeschlagen. Du kannst die Archivierung erneut starten.",
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not persist MMAG failure status for upload %s.",
                upload_id,
            )
    finally:
        try:
            os.remove(os.path.join(staging_dir, ".finalizing"))
        except OSError:
            pass


def _process_is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _start_archive_finalization(upload_id):
    """Use an exclusive marker so only one worker finalizes an upload."""
    staging_dir = _staging_path(upload_id)
    marker_path = os.path.join(staging_dir, ".finalizing")

    for _ in range(2):
        try:
            marker_fd = os.open(
                marker_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(marker_fd, "w", encoding="ascii") as marker:
                marker.write(str(os.getpid()))
                marker.flush()
                os.fsync(marker.fileno())
            break
        except FileExistsError:
            try:
                with open(marker_path, "r", encoding="ascii") as marker:
                    worker_pid = int(marker.read().strip())
            except (OSError, ValueError):
                # A just-created marker may not have been fully written yet.
                return False

            if _process_is_alive(worker_pid):
                return False

            try:
                os.remove(marker_path)
            except OSError:
                return False
    else:
        return False

    try:
        thread = threading.Thread(
            target=_finalize_upload_worker,
            args=(upload_id,),
            name=f"mmag-zip-{upload_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        try:
            os.remove(marker_path)
        except OSError:
            pass
        raise


def _require_mmag_auth():
    if not session.get("mmag_authenticated"):
        return _json_error("Nicht autorisiert. Bitte MMAG erneut freischalten.", 401)
    return None


def _validate_upload_chunks(upload_id, metadata):
    for file_index, file_info in enumerate(metadata["files"]):
        for chunk_index in range(file_info["chunk_count"]):
            part_path = _chunk_path(upload_id, file_index, chunk_index)
            expected_size = _expected_chunk_size(file_info["size"], chunk_index)
            try:
                actual_size = os.path.getsize(part_path)
            except OSError:
                return False
            if actual_size != expected_size:
                return False
    return True


@mmag_bp.route("/MMAG/chunk/start", methods=["POST"])
@mmag_bp.route("/mmag/chunk/start", methods=["POST"])
def mmag_chunk_start():
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _json_error("Ungültige Upload-Metadaten.", 400)

    submitted_name = payload.get("name", "")
    if not isinstance(submitted_name, str):
        return _json_error("Bitte gib einen gültigen Namen ein.", 400)
    name_prefix = secure_filename(submitted_name.strip()).strip("._-")[:64].rstrip("._-")
    if not name_prefix:
        return _json_error("Bitte gib einen gültigen Namen ein.", 400)

    submitted_files = payload.get("files")
    if not isinstance(submitted_files, list) or not submitted_files:
        return _json_error("Bitte wähle mindestens eine Datei aus.", 400)

    files = []
    total_bytes = 0
    for item in submitted_files:
        if not isinstance(item, dict):
            return _json_error("Ungültige Dateiinformationen.", 400)
        filename = item.get("name")
        size = item.get("size")
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 4096
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            return _json_error("Ungültige Dateiinformationen.", 400)

        chunks = _chunk_count(size)
        files.append(
            {
                "filename": filename,
                "size": size,
                "chunk_count": chunks,
            }
        )
        total_bytes += size

    upload_id = uuid.uuid4().hex
    staging_dir = _staging_path(upload_id)
    os.mkdir(staging_dir, 0o700)

    metadata = {
        "name_prefix": name_prefix,
        "files": files,
        "total_bytes": total_bytes,
    }

    try:
        _write_json_atomic(os.path.join(staging_dir, "metadata.json"), metadata)
        _write_upload_status(
            upload_id,
            "uploading",
            0,
            total_bytes,
            len(files),
            "Dateien werden übertragen.",
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        logging.getLogger(__name__).exception("Could not initialize MMAG upload session.")
        return _json_error("Der Upload konnte auf dem Server nicht vorbereitet werden.", 500)

    return jsonify(
        {
            "success": True,
            "upload_id": upload_id,
            "chunk_size": MMAG_CHUNK_SIZE,
            "total_bytes": total_bytes,
            "file_count": len(files),
        }
    )


@mmag_bp.route(
    "/MMAG/chunk/<upload_id>/<int:file_index>/<int:chunk_index>",
    methods=["POST"],
)
@mmag_bp.route(
    "/mmag/chunk/<upload_id>/<int:file_index>/<int:chunk_index>",
    methods=["POST"],
)
def mmag_chunk_receive(upload_id, file_index, chunk_index):
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    metadata = _load_upload_metadata(upload_id)
    status_data = _load_json_file(_status_path(upload_id)) or {}
    if status_data.get("state") != "uploading":
        return _json_error("Dieser Upload nimmt keine weiteren Daten an.", 409)

    files = metadata["files"]
    if file_index < 0 or file_index >= len(files):
        return _json_error("Ungültiger Dateiindex.", 400)

    file_info = files[file_index]
    if chunk_index < 0 or chunk_index >= file_info["chunk_count"]:
        return _json_error("Ungültiger Chunk-Index.", 400)

    chunk_file = request.files.get("chunk")
    if chunk_file is None:
        return _json_error("Upload-Chunk fehlt.", 400)

    expected_size = _expected_chunk_size(file_info["size"], chunk_index)
    part_path = _chunk_path(upload_id, file_index, chunk_index)
    part_directory = os.path.dirname(part_path)
    os.makedirs(part_directory, mode=0o700, exist_ok=True)

    # A retry after a lost response is idempotent: an already stored complete
    # chunk is accepted instead of being written a second time.
    try:
        existing_size = os.path.getsize(part_path)
        if existing_size == expected_size:
            return jsonify({"success": True, "already_received": True})
        # Recover from a partial chunk left by a process interruption.
        os.remove(part_path)
    except FileNotFoundError:
        pass
    except OSError:
        return _json_error("Ein vorhandener Datenblock kann nicht ersetzt werden.", 500)

    try:
        fd = os.open(
            part_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _json_error("Dieser Chunk wird bereits verarbeitet. Bitte erneut versuchen.", 409)

    received = 0
    try:
        with os.fdopen(fd, "wb") as destination:
            while True:
                read_size = min(
                    MMAG_COPY_BUFFER_SIZE,
                    max(1, expected_size - received + 1),
                )
                block = chunk_file.stream.read(read_size)
                if not block:
                    break
                received += len(block)
                if received > expected_size:
                    raise ValueError("Chunk exceeds expected size.")
                destination.write(block)

            if received != expected_size:
                raise ValueError("Chunk size does not match expected size.")

            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        try:
            os.remove(part_path)
        except OSError:
            pass
        return _json_error("Ein Datenblock wurde unvollständig übertragen. Bitte erneut versuchen.", 400)

    return jsonify({"success": True, "received_bytes": received})


@mmag_bp.route("/MMAG/chunk/<upload_id>/finish", methods=["POST"])
@mmag_bp.route("/mmag/chunk/<upload_id>/finish", methods=["POST"])
def mmag_chunk_finish(upload_id):
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    status_path = _status_path(upload_id)
    status_data = _load_json_file(status_path) or {}

    # The completed worker removes upload metadata and temporary chunks. Check
    # completion first so a repeated finish request remains idempotent.
    if status_data.get("state") == "completed":
        return jsonify(
            {
                "success": True,
                "state": "completed",
                "message": status_data.get("message", "Upload erfolgreich abgeschlossen."),
            }
        )

    metadata = _load_upload_metadata(upload_id)
    if not _validate_upload_chunks(upload_id, metadata):
        return _json_error(
            "Die Übertragung ist noch nicht vollständig. Fehlende Datenblöcke werden erneut benötigt.",
            409,
        )

    try:
        _start_archive_finalization(upload_id)
    except Exception:
        current_app.logger.exception("Could not start MMAG ZIP finalization.")
        return _json_error("Die ZIP-Erstellung konnte nicht gestartet werden.", 500)

    return jsonify({"success": True, "state": "processing"}), 202


@mmag_bp.route("/MMAG/chunk/<upload_id>/status", methods=["GET"])
@mmag_bp.route("/mmag/chunk/<upload_id>/status", methods=["GET"])
def mmag_chunk_status(upload_id):
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    status_data = _load_json_file(_status_path(upload_id))
    if not isinstance(status_data, dict):
        abort(404)

    # If the web process was restarted during ZIP creation, recover the
    # background task when the browser next polls its status.
    if status_data.get("state") == "processing":
        try:
            _start_archive_finalization(upload_id)
        except Exception:
            current_app.logger.exception("Could not recover MMAG ZIP finalization.")
        status_data = _load_json_file(_status_path(upload_id)) or status_data

    total_bytes = int(status_data.get("total_bytes", 0))
    processed_bytes = int(status_data.get("processed_bytes", 0))
    response = {
        "success": True,
        "state": status_data.get("state", "unknown"),
        "processed_bytes": processed_bytes,
        "total_bytes": total_bytes,
        "file_count": int(status_data.get("file_count", 0)),
        "message": status_data.get("message", ""),
        "chunk_size": MMAG_CHUNK_SIZE,
    }

    # A browser reload loses its File objects. When the same files are selected
    # again, report the first missing chunk so the current upload can resume.
    if request.args.get("resume") == "1" and response["state"] in {
        "uploading", "processing", "failed"
    }:
        metadata = _load_upload_metadata(upload_id)
        received_bytes = 0
        next_file_index = len(metadata["files"])
        next_chunk_index = 0

        for file_index, file_info in enumerate(metadata["files"]):
            found_missing_chunk = False
            for chunk_index in range(file_info["chunk_count"]):
                part_path = _chunk_path(upload_id, file_index, chunk_index)
                expected_size = _expected_chunk_size(file_info["size"], chunk_index)
                try:
                    actual_size = os.path.getsize(part_path)
                except OSError:
                    actual_size = -1

                if actual_size != expected_size:
                    next_file_index = file_index
                    next_chunk_index = chunk_index
                    found_missing_chunk = True
                    break

                received_bytes += expected_size

            if found_missing_chunk:
                break

        response["next_file_index"] = next_file_index
        response["next_chunk_index"] = next_chunk_index
        response["received_bytes"] = received_bytes

    return jsonify(response)


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
            return redirect(request.path, code=303)

        return render_template(
            "mmag.html",
            authenticated=False,
            error="Falsches Passwort.",
        ), 401

    if not session.get("mmag_authenticated"):
        return render_template("mmag.html", authenticated=False)

    if request.method == "POST":
        # File transfers must use the chunk API; do not accept a single huge
        # multipart request from a stale page or a non-updated client.
        message = "Bitte lade die MMAG-Seite neu. Große Uploads werden blockweise übertragen."
        if _is_xhr_request():
            return _json_error(message, 400)
        return render_template(
            "mmag.html",
            authenticated=True,
            error=message,
        ), 400

    return render_template("mmag.html", authenticated=True)
