"""Temporary, password-protected MMAG upload endpoint.

Large uploads are sent in resumable chunks. The server then assembles all files
into a private, uncompressed ZIP64 archive in a background thread.
"""
import fcntl
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zipfile
from contextlib import contextmanager

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from config import BASE_DIR

mmag_bp = Blueprint("mmag", __name__)

# Keep only the scrypt hash here, never the plaintext password.
MMAG_PASSWORD_HASH = "scrypt:32768:8:1$UCiUfz7IaGSpiodp$2b9efe421a542daae88ad220d16dedd25f0f20dcc9d63f5f69fd849d38cc779dccf93366ccf8cb0b214a71994dc6b802b57f8c444e889b379023406f9cf5fecc"

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


@contextmanager
def _upload_lock(upload_id):
    """Serialize state-changing operations for one upload across processes."""
    staging_dir = _staging_path(upload_id)
    if not os.path.isdir(staging_dir):
        abort(404)

    lock_path = os.path.join(staging_dir, ".upload.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield staging_dir
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _upload_cancel_requested(upload_id):
    return os.path.exists(os.path.join(_staging_path(upload_id), ".cancel_requested"))


def _remove_upload_payload(staging_dir):
    """Remove all upload data while retaining only the tiny status and lock files."""
    for entry in os.listdir(staging_dir):
        if entry in {"status.json", ".upload.lock"}:
            continue
        path = os.path.join(staging_dir, entry)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except FileNotFoundError:
            pass


def _mark_upload_cancelled(upload_id):
    """Delete the complete private staging directory for an abandoned upload."""
    staging_dir = _staging_path(upload_id)
    shutil.rmtree(staging_dir)


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


class _UploadCancelled(Exception):
    """Raised when the owner explicitly abandons an unfinished upload."""


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
    archive_name = f"{name_prefix}_upload_{upload_id}.zip"
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
    archive_is_published = False

    try:
        metadata = _load_upload_metadata(upload_id)
        files = metadata["files"]
        name_prefix = metadata["name_prefix"]
        total_bytes = metadata["total_bytes"]
        file_count = len(files)

        if _upload_cancel_requested(upload_id):
            raise _UploadCancelled()

        processed_bytes = 0
        next_status_update = MMAG_ARCHIVE_PROGRESS_INTERVAL
        _write_upload_status(
            upload_id, "processing", 0, total_bytes, file_count,
            "ZIP-Archiv wird erstellt.",
        )

        archive_name, final_archive_path, temporary_archive_path, archive_fd = (
            _archive_path_for_prefix(name_prefix, upload_id, staging_dir)
        )

        if archive_fd is None:
            # An atomic rename from an earlier worker already published the ZIP.
            # Coordinate completion with cancellation before reporting success.
            archive_is_published = True
            with _upload_lock(upload_id):
                try:
                    os.remove(os.path.join(staging_dir, ".cancel_requested"))
                except OSError:
                    pass
                _write_upload_status(
                    upload_id,
                    "completed",
                    total_bytes,
                    total_bytes,
                    file_count,
                    "Dateien erfolgreich hochgeladen.",
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

        archive_file_context = os.fdopen(archive_fd, "w+b")
        archive_fd = None
        with archive_file_context as archive_file:
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
                                    if _upload_cancel_requested(upload_id):
                                        raise _UploadCancelled()
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

        # Coordinate the final rename with cancellation so an archive cannot
        # appear after the user has successfully cancelled the upload.
        with _upload_lock(upload_id):
            if _upload_cancel_requested(upload_id):
                raise _UploadCancelled()
            os.replace(temporary_archive_path, final_archive_path)
            temporary_archive_path = None
            archive_is_published = True

            _write_upload_status(
                upload_id,
                "completed",
                total_bytes,
                total_bytes,
                file_count,
                "Dateien erfolgreich hochgeladen.",
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

    except Exception as error:
        if isinstance(error, _UploadCancelled):
            logging.getLogger(__name__).info(
                "MMAG upload %s was cancelled; removing temporary data.",
                upload_id,
            )
            if temporary_archive_path:
                try:
                    os.remove(temporary_archive_path)
                except OSError:
                    pass
            try:
                with _upload_lock(upload_id):
                    _mark_upload_cancelled(upload_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not clean up cancelled MMAG upload %s.",
                    upload_id,
                )
            return

        logging.getLogger(__name__).exception(
            "MMAG background archive creation failed for upload %s.",
            upload_id,
        )
        if "archive_fd" in locals() and archive_fd is not None:
            try:
                os.close(archive_fd)
            except OSError:
                pass
        if temporary_archive_path:
            try:
                os.remove(temporary_archive_path)
            except OSError:
                pass
        if final_archive_path and not archive_is_published:
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
    """Start at most one archive worker for an upload, across threads/processes."""
    try:
        with _upload_lock(upload_id) as staging_dir:
            status_data = _load_json_file(_status_path(upload_id)) or {}
            state = status_data.get("state")
            if state in {"completed", "cancelled", "failed", "uploading"}:
                return False
            if state not in {"processing", "cancelling"}:
                return False

            marker_path = os.path.join(staging_dir, ".finalizing")
            try:
                marker_fd = os.open(
                    marker_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    with open(marker_path, "r", encoding="ascii") as marker:
                        worker_pid = int(marker.read().strip())
                except (OSError, ValueError):
                    return False

                if _process_is_alive(worker_pid):
                    return False

                os.remove(marker_path)
                marker_fd = os.open(
                    marker_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )

            try:
                with os.fdopen(marker_fd, "w", encoding="ascii") as marker:
                    marker.write(str(os.getpid()))
                    marker.flush()
                    os.fsync(marker.fileno())
            except Exception:
                try:
                    os.remove(marker_path)
                except OSError:
                    pass
                raise
    except FileNotFoundError:
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
            os.remove(os.path.join(_staging_path(upload_id), ".finalizing"))
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

    # Each request carries one 32-MiB chunk plus multipart framing.
    if request.content_length is None:
        return _json_error("Der Request benötigt einen Content-Length-Header.", 411)
    if request.content_length > MMAG_CHUNK_SIZE + 1024 * 1024:
        return _json_error("Der Datenblock überschreitet die erlaubte Größe.", 413)

    chunk_file = request.files.get("chunk")
    if chunk_file is None:
        return _json_error("Upload-Chunk fehlt.", 400)

    try:
        with _upload_lock(upload_id):
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

            expected_size = _expected_chunk_size(file_info["size"], chunk_index)
            part_path = _chunk_path(upload_id, file_index, chunk_index)
            part_directory = os.path.dirname(part_path)
            os.makedirs(part_directory, mode=0o700, exist_ok=True)

            # A retry after a lost response is idempotent.
            try:
                existing_size = os.path.getsize(part_path)
                if existing_size == expected_size:
                    return jsonify({"success": True, "already_received": True})
                os.remove(part_path)
            except FileNotFoundError:
                pass

            fd = os.open(
                part_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
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
                return _json_error(
                    "Ein Datenblock wurde unvollständig übertragen. Bitte erneut versuchen.",
                    400,
                )

            return jsonify({"success": True, "received_bytes": received})
    except FileNotFoundError:
        return _json_error("Dieser Upload existiert nicht mehr.", 404)


@mmag_bp.route("/MMAG/chunk/<upload_id>/finish", methods=["POST"])
@mmag_bp.route("/mmag/chunk/<upload_id>/finish", methods=["POST"])
def mmag_chunk_finish(upload_id):
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    try:
        with _upload_lock(upload_id):
            status_data = _load_json_file(_status_path(upload_id)) or {}
            state = status_data.get("state")

            if state == "completed":
                return jsonify(
                    {
                        "success": True,
                        "state": "completed",
                        "message": status_data.get("message", "Upload erfolgreich abgeschlossen."),
                    }
                )
            if state in {"cancelled", "cancelling"}:
                return _json_error("Dieser Upload wurde abgebrochen.", 409)

            metadata = _load_upload_metadata(upload_id)
            if not _validate_upload_chunks(upload_id, metadata):
                return _json_error(
                    "Die Übertragung ist noch nicht vollständig. Fehlende Datenblöcke werden erneut benötigt.",
                    409,
                )

            if state == "uploading" or state == "failed":
                _write_upload_status(
                    upload_id,
                    "processing",
                    int(status_data.get("total_bytes", metadata["total_bytes"])),
                    int(status_data.get("total_bytes", metadata["total_bytes"])),
                    len(metadata["files"]),
                    "ZIP-Archiv wird erstellt.",
                )
    except FileNotFoundError:
        return _json_error("Dieser Upload existiert nicht mehr.", 404)

    try:
        _start_archive_finalization(upload_id)
    except Exception:
        current_app.logger.exception("Could not start MMAG ZIP finalization.")
        return _json_error("Die ZIP-Erstellung konnte nicht gestartet werden.", 500)

    return jsonify({"success": True, "state": "processing"}), 202


@mmag_bp.route("/MMAG/chunk/<upload_id>/cancel", methods=["POST"])
@mmag_bp.route("/mmag/chunk/<upload_id>/cancel", methods=["POST"])
def mmag_chunk_cancel(upload_id):
    auth_error = _require_mmag_auth()
    if auth_error:
        return auth_error

    if not os.path.isdir(_staging_path(upload_id)):
        return jsonify(
            {
                "success": True,
                "state": "cancelled",
                "message": "Der vorherige Upload wurde bereits entfernt.",
            }
        )

    try:
        with _upload_lock(upload_id):
            status_data = _load_json_file(_status_path(upload_id))
            if not isinstance(status_data, dict):
                return _json_error("Dieser Upload existiert nicht mehr.", 404)

            state = status_data.get("state")
            if state == "completed":
                return _json_error("Der Upload ist bereits abgeschlossen.", 409)
            if state == "cancelled":
                return jsonify({"success": True, "state": "cancelled"})

            if state in {"processing", "cancelling"}:
                marker_path = os.path.join(_staging_path(upload_id), ".cancel_requested")
                fd = os.open(
                    marker_path,
                    os.O_WRONLY | os.O_CREAT,
                    0o600,
                )
                os.close(fd)
                _write_upload_status(
                    upload_id,
                    "cancelling",
                    int(status_data.get("processed_bytes", 0)),
                    int(status_data.get("total_bytes", 0)),
                    int(status_data.get("file_count", 0)),
                    "Upload wird abgebrochen und temporäre Daten werden gelöscht.",
                )
                return jsonify(
                    {
                        "success": True,
                        "state": "cancelling",
                        "message": "Upload wird abgebrochen.",
                    }
                ), 202

            if state not in {"uploading", "failed"}:
                return _json_error("Dieser Upload kann nicht mehr abgebrochen werden.", 409)

            _mark_upload_cancelled(upload_id)
            return jsonify(
                {
                    "success": True,
                    "state": "cancelled",
                    "message": "Der abgebrochene Upload und seine temporären Dateien wurden gelöscht.",
                }
            )
    except FileNotFoundError:
        return _json_error("Dieser Upload existiert nicht mehr.", 404)
    except Exception:
        current_app.logger.exception("Could not cancel MMAG upload %s.", upload_id)
        return _json_error("Der Upload konnte nicht vollständig abgebrochen werden.", 500)


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
    if status_data.get("state") in {"processing", "cancelling"}:
        try:
            # A cancellation marker makes a recovered worker clean up instead
            # of finishing the archive after a web-process restart.
            _start_archive_finalization(upload_id)
        except Exception:
            current_app.logger.exception("Could not recover MMAG ZIP finalization.")
        status_data = _load_json_file(_status_path(upload_id)) or status_data

    if _upload_cancel_requested(upload_id):
        status_data["state"] = "cancelling"

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
        if response["state"] == "uploading":
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
        else:
            # Finalization only starts after every chunk has been verified.
            received_bytes = total_bytes
            next_file_index = response["file_count"]
            next_chunk_index = 0

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

MMAG_DOWNLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, "mmag_downloads"))
os.makedirs(MMAG_DOWNLOAD_FOLDER, mode=0o700, exist_ok=True)

@mmag_bp.route("/mmag-download", methods=["GET", "POST"])
def mmag_download():
    if request.method == "POST" and not session.get("mmag_authenticated"):
        password = request.form.get("password", "")
        if not MMAG_PASSWORD_HASH or "REPLACE_WITH_" in MMAG_PASSWORD_HASH:
            current_app.logger.error("MMAG_PASSWORD_HASH has not been configured.")
            abort(503)

        if check_password_hash(MMAG_PASSWORD_HASH, password):
            session["mmag_authenticated"] = True
            return redirect(request.path, code=303)

        return render_template(
            "mmag_download.html",
            authenticated=False,
            error="Falsches Passwort.",
        ), 401

    if not session.get("mmag_authenticated"):
        return render_template("mmag_download.html", authenticated=False)

    return render_template("mmag_download.html", authenticated=True)

@mmag_bp.route('/mmag-download/api/files', methods=['GET'])
def api_list_files():
    if not session.get("mmag_authenticated"):
        return redirect(url_for("mmag_download"))
    folder = MMAG_DOWNLOAD_FOLDER
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

@mmag_bp.route('/mmag-download/file/<path:filename>', methods=['GET'])
def download_file(filename):
    if not session.get("mmag_authenticated"):
        return redirect("mmag_download")
    try:
        return send_from_directory(
            MMAG_DOWNLOAD_FOLDER,
            filename,
            as_attachment=True
        )
    except FileNotFoundError:
        abort(404)

@mmag_bp.route('/mmag-download/zip', methods=['POST'])
def download_zip():
    if not session.get("mmag_authenticated"):
        return redirect(url_for("mmag_download"))
    data = request.get_json() or {}
    filenames = data.get('files', [])
    if not filenames:
        return jsonify({'error': 'Keine Dateien ausgewählt'}), 400

    memory_file = io.BytesIO()
    base_dir = MMAG_DOWNLOAD_FOLDER

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

@mmag_bp.route("/mmag-logout")
def mmag_logout():
    session.pop("mmag_authenticated", None)
    return redirect("/mmag-download", code=303)
