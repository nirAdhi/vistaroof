"""Document sources: Google Drive accounts (OAuth, one token per source), local folders, and (in
``microsoft.py``) SharePoint / OneDrive through Microsoft 365. ``open_source()`` is the factory.

Google Drive
------------
* Scopes: ``drive.readonly`` (read everything the account can see) + ``drive.file`` (create files
  through the app = the Upload feature). Tokens granted before ``drive.file`` was added still work for
  reading; ``can_upload()`` is False for them and the UI asks for a reconnect.
* ``DriveSource.list_files()`` walks either one folder tree (``drive_folder_id``) or the whole Drive.
* ``DriveSource.changes_since()`` uses the Drive *changes* feed so the watcher (sync.py) can notice
  new/changed/deleted files every couple of minutes without re-listing everything.
* ``DriveSource.upload()`` creates a file in the source's folder (or a "Ledger Uploads" folder at the
  root for whole-Drive sources) and returns the FileMeta so it can be read immediately.
* Tokens are stored encrypted (crypto.py) via ``save_token()`` / read via ``credentials_for()``.

Local folders
-------------
``LocalSource`` walks a directory. ``LOCAL_ROOTS`` (config) can restrict which directories an admin
may register. Uploads are written into ``<folder>/Ledger Uploads``.

Debugging tips
--------------
* "needs to be reconnected": the refresh token was revoked or expired (Google expires tokens of apps
  in *testing* mode after 7 days). Press Reconnect on the source card.
* ``build_drive_service`` is the single seam tests use to inject a fake Drive.
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config, crypto, db, settings

log = logging.getLogger(__name__)

# oauthlib insists on https unless told otherwise; the redirect may be the local loopback address.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT = "application/vnd.google-apps.shortcut"

# Windows' mimetypes table misses several common types; register the ones we care about.
for _ext, _mime in (
    (".heic", "image/heic"), (".heif", "image/heif"), (".webp", "image/webp"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
    (".png", "image/png"), (".tif", "image/tiff"), (".tiff", "image/tiff"), (".bmp", "image/bmp"), (".md", "text/markdown"),
    (".csv", "text/csv"), (".txt", "text/plain"), (".pdf", "application/pdf"),
    (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
):
    mimetypes.add_type(_mime, _ext)

SUPPORTED_MIMES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "image/heif", "image/tiff", "image/bmp", "image/x-ms-bmp",
    "text/plain", "text/csv", "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    GOOGLE_DOC, GOOGLE_SHEET, GOOGLE_SLIDES,
}
UPLOADABLE_MIMES = SUPPORTED_MIMES - {GOOGLE_DOC, GOOGLE_SHEET, GOOGLE_SLIDES}

SCOPE_READ = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPES = [SCOPE_READ, SCOPE_FILE]
UPLOAD_FOLDER_NAME = "Ledger Uploads"


@dataclass
class FileMeta:
    """Everything the ledger stores about a file before it is read."""
    source: str
    source_ref: int | None
    source_id: str
    name: str
    mime: str
    folder: str
    web_link: str
    size: int | None
    modified_at: str
    uploaded_by: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class FileContent:
    """Bytes (pdf/images/office) or text (csv/txt/sheets)."""
    mime: str
    data: bytes | None = None
    text: str | None = None


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ------------------------------------------------------------------ Google OAuth helpers


def build_drive_service(creds: Any):
    """Build the Drive v3 client. Kept as a separate function so tests can substitute a fake."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _flow(redirect_uri: str):
    from google_auth_oauthlib.flow import Flow

    cfg = settings.google_client_config()
    if not cfg:
        raise RuntimeError("No Google OAuth client configured. Upload the client JSON in Admin → Google.")
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=redirect_uri)


def authorization_url(redirect_uri: str, state: str) -> str:
    """Google consent URL. ``prompt=consent`` + ``access_type=offline`` guarantee a refresh token."""
    flow = _flow(redirect_uri)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    return url


def exchange_code(redirect_uri: str, code: str) -> tuple[str, str]:
    """Swap the OAuth code for tokens. Returns (token_json, account_email)."""
    flow = _flow(redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials
    service = build_drive_service(creds)
    about = service.about().get(fields="user").execute()
    email = (about.get("user") or {}).get("emailAddress", "")
    return creds.to_json(), email


def save_token(source_id: int, token_json: str | None) -> None:
    """Store (encrypted) or clear a source's Google token."""
    db.update_source(source_id, token_json=crypto.encrypt(token_json) if token_json else None)


def token_info(source: dict[str, Any]) -> dict[str, Any]:
    """Decrypted token dict, or {} when the source is not connected."""
    raw = source.get("token_json")
    if not raw:
        return {}
    try:
        return json.loads(crypto.decrypt(raw) or "{}")
    except ValueError:
        return {}


def can_upload(source: dict[str, Any]) -> bool:
    """Local folders always accept uploads; Drives need the drive.file scope; Microsoft 365 the write scope."""
    if source["kind"] == "local":
        return True
    if source["kind"] == "m365":
        from . import microsoft

        return microsoft.can_upload(source)
    return SCOPE_FILE in (token_info(source).get("scopes") or [])


def upload_block_reason(source: dict[str, Any]) -> str:
    """Why uploads are not possible for a connected source (shown to the user)."""
    if source["kind"] == "m365":
        from . import microsoft

        return microsoft.upload_block_reason(source)
    return "this Google Drive was connected without upload permission; reconnect it in Admin → Sources"


def credentials_for(source: dict[str, Any]):
    """google.oauth2 Credentials for a Drive source, refreshing (and re-saving) when expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    info = token_info(source)
    if not info:
        raise RuntimeError(f"Google Drive '{source['name']}' is not connected yet. Open Admin → Sources and press Connect.")
    creds = Credentials.from_authorized_user_info(info, info.get("scopes") or SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_token(source["id"], creds.to_json())
        else:
            raise RuntimeError(f"Google Drive '{source['name']}' needs to be reconnected (token expired or revoked).")
    return creds


# ------------------------------------------------------------------ Google Drive source


class DriveSource:
    kind = "drive"

    def __init__(self, source: dict[str, Any]) -> None:
        self.row = source
        self.id = int(source["id"])
        self.name = source["name"]
        self.folder_id = (source.get("drive_folder_id") or "").strip()
        self.service = build_drive_service(credentials_for(source))

    # -- listing ---------------------------------------------------------

    def _list(self, q: str, fields: str) -> Iterator[dict]:
        """Paginate files.list for a query."""
        token = None
        while True:
            resp = self.service.files().list(
                q=q, fields=f"nextPageToken, files({fields})", pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                break

    def list_files(self) -> Iterator[FileMeta]:
        fields = "id, name, mimeType, modifiedTime, size, webViewLink, parents, shortcutDetails"
        if self.folder_id:
            # Breadth-first walk of the chosen folder so every file gets a readable path.
            root_name = self.service.files().get(fileId=self.folder_id, fields="name", supportsAllDrives=True).execute()["name"]
            queue = [(self.folder_id, root_name)]
            while queue:
                folder_id, path = queue.pop(0)
                for f in self._list(f"'{folder_id}' in parents and trashed = false", fields):
                    if f["mimeType"] == GOOGLE_FOLDER:
                        queue.append((f["id"], f"{path}/{f['name']}"))
                    else:
                        yield self._to_meta(f, path)
        else:
            # Whole Drive: fetch all folders once to resolve paths, then all non-folder files.
            folders = {
                f["id"]: (f["name"], (f.get("parents") or [None])[0])
                for f in self._list(f"mimeType = '{GOOGLE_FOLDER}' and trashed = false", "id, name, parents")
            }

            def path_of(parent_id: str | None) -> str:
                parts, seen = [], set()
                while parent_id and parent_id in folders and parent_id not in seen:
                    seen.add(parent_id)
                    name, parent_id = folders[parent_id]
                    parts.append(name)
                return "/".join(reversed(parts)) or "My Drive"

            for f in self._list(f"mimeType != '{GOOGLE_FOLDER}' and trashed = false", fields):
                yield self._to_meta(f, path_of((f.get("parents") or [None])[0]))

    def _to_meta(self, f: dict, folder: str, uploaded_by: str | None = None) -> FileMeta:
        mime, file_id = f["mimeType"], f["id"]
        if mime == GOOGLE_SHORTCUT:  # follow shortcuts to their target
            details = f.get("shortcutDetails") or {}
            mime = details.get("targetMimeType", "")
            file_id = details.get("targetId", file_id)
        return FileMeta(
            source="drive", source_ref=self.id, source_id=file_id, name=f["name"], mime=mime, folder=f"{self.name}/{folder}",
            web_link=f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
            size=int(f["size"]) if f.get("size") else None, modified_at=f.get("modifiedTime", ""), uploaded_by=uploaded_by,
        )

    # -- content ---------------------------------------------------------

    def fetch(self, meta: FileMeta) -> FileContent:
        """Download a file; Google-native docs are exported (Docs/Slides -> PDF, Sheets -> CSV)."""
        if meta.mime in (GOOGLE_DOC, GOOGLE_SLIDES):
            req = self.service.files().export_media(fileId=meta.source_id, mimeType="application/pdf")
            return FileContent(mime="application/pdf", data=_download(req))
        if meta.mime == GOOGLE_SHEET:
            req = self.service.files().export_media(fileId=meta.source_id, mimeType="text/csv")
            return FileContent(mime="text/csv", text=_download(req).decode("utf-8", "replace"))
        req = self.service.files().get_media(fileId=meta.source_id, supportsAllDrives=True)
        data = _download(req)
        if meta.mime.startswith("text/"):
            return FileContent(mime=meta.mime, text=data.decode("utf-8", "replace"))
        return FileContent(mime=meta.mime, data=data)

    # -- change feed (watcher) -------------------------------------------

    def changes_since(self, page_token: str | None) -> tuple[bool, str]:
        """Return (anything_changed, new_page_token). With no token yet, just obtain the start cursor."""
        if not page_token:
            resp = self.service.changes().getStartPageToken(supportsAllDrives=True).execute()
            return False, resp["startPageToken"]
        changed = False
        token = page_token
        while token:
            resp = self.service.changes().list(
                pageToken=token, pageSize=1000, includeRemoved=True, supportsAllDrives=True, includeItemsFromAllDrives=True,
                fields="nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, trashed))",
            ).execute()
            for ch in resp.get("changes", []):
                f = ch.get("file") or {}
                if ch.get("removed") or f.get("trashed") or f.get("mimeType") != GOOGLE_FOLDER:
                    changed = True  # any non-folder change (or a deletion) is enough to trigger a sync
            token = resp.get("nextPageToken")
            if not token:
                return changed, resp.get("newStartPageToken", page_token)
        return changed, page_token

    # -- upload ----------------------------------------------------------

    def _upload_folder_id(self) -> str:
        """Target folder for uploads: the source's folder, else "Ledger Uploads" at the Drive root (created once)."""
        if self.folder_id:
            return self.folder_id
        existing = list(self._list(
            f"name = '{UPLOAD_FOLDER_NAME}' and mimeType = '{GOOGLE_FOLDER}' and 'root' in parents and trashed = false", "id, name"))
        if existing:
            return existing[0]["id"]
        created = self.service.files().create(
            body={"name": UPLOAD_FOLDER_NAME, "mimeType": GOOGLE_FOLDER, "parents": ["root"]}, fields="id", supportsAllDrives=True
        ).execute()
        return created["id"]

    def upload(self, filename: str, data: bytes, mime: str) -> FileMeta:
        """Create the file in Drive and return its metadata (so it can be read straight away)."""
        from googleapiclient.http import MediaIoBaseUpload

        folder_id = self._upload_folder_id()
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=len(data) > 5 * 1024 * 1024)
        created = self.service.files().create(
            body={"name": filename, "parents": [folder_id]}, media_body=media,
            fields="id, name, mimeType, modifiedTime, size, webViewLink, parents", supportsAllDrives=True,
        ).execute()
        folder = UPLOAD_FOLDER_NAME if not self.folder_id else "(uploads)"
        log.info("uploaded %s to Drive source %s (file id %s)", filename, self.name, created.get("id"))
        return self._to_meta(created, folder, uploaded_by="app")


def _download(request) -> bytes:
    """Read a Drive media request fully into memory (chunked)."""
    if hasattr(request, "execute") and not hasattr(request, "uri"):
        return request.execute()  # fake request objects in tests return bytes directly
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ------------------------------------------------------------------ Local folder source


def check_local_folder(folder: str) -> Path:
    """Validate a folder an admin wants to register: must exist and, if LOCAL_ROOTS is set, lie inside one of them."""
    p = Path(folder).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"folder not found: {folder}")
    if config.LOCAL_ROOTS and not any(p == root or root in p.parents for root in config.LOCAL_ROOTS):
        raise ValueError("that folder is outside the allowed LOCAL_ROOTS for this server")
    return p


class LocalSource:
    kind = "local"

    def __init__(self, source: dict[str, Any]) -> None:
        self.row = source
        self.id = int(source["id"])
        self.name = source["name"]
        folder = (source.get("local_folder") or "").strip()
        if not folder:
            raise RuntimeError(f"Source '{self.name}' has no folder set.")
        self.root = Path(folder)
        if not self.root.is_dir():
            raise RuntimeError(f"Folder does not exist: {self.root}")

    def list_files(self) -> Iterator[FileMeta]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            folder = f"{self.name}/{rel_dir}" if rel_dir != "." else self.name
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("~$"):
                    continue
                yield self._meta_for(Path(dirpath) / fn, folder)

    def _meta_for(self, p: Path, folder: str, uploaded_by: str | None = None) -> FileMeta:
        st = p.stat()
        return FileMeta(
            source="local", source_ref=self.id, source_id=str(p.resolve()), name=p.name, mime=guess_mime(p.name), folder=folder,
            web_link=p.resolve().as_uri(), size=st.st_size,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"), uploaded_by=uploaded_by,
        )

    def fetch(self, meta: FileMeta) -> FileContent:
        p = Path(meta.source_id)
        if meta.mime.startswith("text/"):
            return FileContent(mime=meta.mime, text=p.read_text("utf-8", errors="replace"))
        return FileContent(mime=meta.mime, data=p.read_bytes())

    def upload(self, filename: str, data: bytes, mime: str) -> FileMeta:
        """Write into <folder>/Ledger Uploads, never overwriting an existing file."""
        target_dir = self.root / UPLOAD_FOLDER_NAME
        target_dir.mkdir(exist_ok=True)
        safe = Path(filename).name  # strip any directory component from the client-supplied name
        target = target_dir / safe
        n = 1
        while target.exists():
            target = target_dir / f"{Path(safe).stem} ({n}){Path(safe).suffix}"
            n += 1
        target.write_bytes(data)
        return self._meta_for(target, f"{self.name}/{UPLOAD_FOLDER_NAME}", uploaded_by="app")


def open_source(source: dict[str, Any]):
    """Factory: the right source class for a sources-table row."""
    if source["kind"] == "local":
        return LocalSource(source)
    if source["kind"] == "drive":
        return DriveSource(source)
    if source["kind"] == "m365":
        from . import microsoft  # imported lazily: microsoft.py imports FileMeta from this module

        return microsoft.M365Source(source)
    raise RuntimeError(f"unknown source kind {source['kind']}")


CLOUD_KINDS = ("drive", "m365")  # kinds that need an OAuth connection before they can be used
KIND_LABELS = {"drive": "Google Drive", "m365": "Microsoft 365", "local": "Local folder"}
