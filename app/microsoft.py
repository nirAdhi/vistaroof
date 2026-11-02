"""Microsoft 365 source: SharePoint document libraries and OneDrive, through Microsoft Graph.

Why a separate module
---------------------
Cormac's documents live in SharePoint (HRAccounts and VistaFiles sites), not Google Drive. This
module gives the app the same abilities for Microsoft 365 that ``sources.DriveSource`` gives for
Google Drive: sign in once per account, list a folder tree, download files, notice changes quickly
(delta feed) and upload files from the dashboard.

How a Microsoft source is set up
--------------------------------
1. Admin → Microsoft setup: register an app in Microsoft Entra (client id, optional secret, tenant).
2. Admin → Sources → "Add a Microsoft 365 folder": a name and, optionally, a folder link copied from
   SharePoint/OneDrive ("Share → Copy link"). Blank = the whole OneDrive of the signed-in account;
   the word ``shared`` = everything that has been shared with the signed-in account (the guest case:
   Cormac shares the folders with Suraj's address, the app signs in as Suraj).
3. "Connect with Microsoft" → sign in → back to ``/admin/oauth/ms-callback`` → the link is resolved
   to a (drive id, item id) pair stored in ``sources.ms_location`` and the first sync starts.

Guest access (someone outside the organisation)
-----------------------------------------------
A guest's token must be issued *by the organisation that owns the files*. Put that organisation's
domain or tenant id (e.g. ``vistaroofing.ie``) in the source's "Organisation (tenant)" field; the
sign-in and token refresh then use ``login.microsoftonline.com/<tenant>`` instead of ``common``.

Permissions requested (delegated, so never more than the signed-in person can do themselves)
--------------------------------------------------------------------------------------------
``User.Read`` (who signed in), ``Files.Read.All`` + ``Sites.Read.All`` (read files the account can
see, resolve links), ``offline_access`` (refresh token) and, when uploads are enabled for the source,
``Files.ReadWrite.All`` (create files). Granted scopes are stored with the token; ``can_upload()``
checks for the write scope so a read-only connection is reported honestly in the UI.

Graph calls used
----------------
* ``GET /me`` – account email.
* ``GET /shares/u!<base64url(link)>/driveItem`` – turn a pasted link into drive id + item id.
* ``GET /drives/{d}/items/{i}/children`` – listing (paged with ``@odata.nextLink``).
* ``GET /me/drive/sharedWithMe`` – the guest case.
* ``GET /drives/{d}/items/{i}/content`` – download (302 to a pre-authenticated URL; httpx follows it
  and drops the Authorization header on the cross-origin hop).
* ``GET /drives/{d}/items/{i}/delta`` – change feed; the cursor is the ``@odata.deltaLink`` and is kept
  in ``sources.drive_page_token`` as JSON (one link per watched root).
* ``PUT …:/{name}:/content`` (≤ 4 MB) or an upload session (larger) – uploads.

Debugging tips
--------------
* 401 ``InvalidAuthenticationToken`` → token expired and refresh failed: press Reconnect.
* 403 on upload → the account only has "Can view" on that folder (ask for "Can edit").
* 404 from ``/shares`` → the link is not visible to the signed-in account (shared with someone else)
  or it is not a sharing link; use Share → Copy link in SharePoint.
* ``build_http_client`` is the seam tests use to plug in an ``httpx.MockTransport``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

from . import crypto, db, settings
from .sources import SUPPORTED_MIMES, UPLOAD_FOLDER_NAME, FileContent, FileMeta, guess_mime

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPES_READ = ["User.Read", "Files.Read.All", "Sites.Read.All", "offline_access"]
SCOPE_WRITE = "Files.ReadWrite.All"
SHARED_KEYWORD = "shared"  # ms_link value meaning "everything shared with the signed-in account"
ITEM_FIELDS = "id,name,file,folder,size,lastModifiedDateTime,webUrl,parentReference,remoteItem,deleted"
SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024  # Graph's limit for a single PUT; above it an upload session is used
CHUNK = 5 * 1024 * 1024  # upload-session chunk (must be a multiple of 320 KiB)


def build_http_client() -> httpx.Client:
    """One place to create the HTTP client, so tests can substitute a mock transport."""
    return httpx.Client(timeout=60, follow_redirects=True)


# ------------------------------------------------------------------ app registration (settings)


def client_config() -> dict[str, str] | None:
    """{"client_id", "client_secret", "tenant"} saved in Admin → Microsoft setup, or None."""
    raw = settings.get("ms_client_config")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(cfg, dict) or not cfg.get("client_id"):
        return None
    cfg.setdefault("tenant", "common")
    cfg.setdefault("client_secret", "")
    return cfg


def _tenant_for(source: dict[str, Any], cfg: dict[str, str]) -> str:
    return (source.get("ms_tenant") or cfg.get("tenant") or "common").strip()


def scopes_for(source: dict[str, Any]) -> list[str]:
    """Read scopes, plus the write scope when the admin enabled uploads for this source."""
    scopes = list(SCOPES_READ)
    if source.get("ms_upload", 1):
        scopes.append(SCOPE_WRITE)
    return scopes


# ------------------------------------------------------------------ OAuth (authorization code + PKCE)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(source: dict[str, Any], redirect_uri: str, state: str, code_challenge: str) -> str:
    cfg = client_config()
    if not cfg:
        raise RuntimeError("No Microsoft app registration configured. Fill in Admin → Microsoft setup first.")
    params = {
        "client_id": cfg["client_id"], "response_type": "code", "redirect_uri": redirect_uri, "response_mode": "query",
        "scope": " ".join(scopes_for(source)), "state": state, "code_challenge": code_challenge,
        "code_challenge_method": "S256", "prompt": "select_account",
    }
    return f"{LOGIN}/{_tenant_for(source, cfg)}/oauth2/v2.0/authorize?{urlencode(params)}"


def _token_request(tenant: str, data: dict[str, str]) -> dict[str, Any]:
    cfg = client_config()
    if not cfg:
        raise RuntimeError("Microsoft app registration has been removed; reconnect after adding it again.")
    body = {"client_id": cfg["client_id"], **data}
    if cfg.get("client_secret"):
        body["client_secret"] = cfg["client_secret"]
    with build_http_client() as http:
        resp = http.post(f"{LOGIN}/{tenant}/oauth2/v2.0/token", data=body)
    if resp.status_code != 200:
        try:
            err = resp.json()
            desc = err.get("error_description") or err.get("error") or resp.text
        except ValueError:
            desc = resp.text
        raise RuntimeError(f"Microsoft sign-in failed: {str(desc)[:300]}")
    tok = resp.json()
    return {
        "provider": "microsoft", "tenant": tenant, "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", data.get("refresh_token", "")),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", ""),
    }


def exchange_code(source: dict[str, Any], redirect_uri: str, code: str, code_verifier: str) -> tuple[dict[str, Any], str]:
    """Swap the auth code for tokens. Returns (token_dict, account_email)."""
    tenant = _tenant_for(source, client_config() or {})
    token = _token_request(tenant, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier})
    me = Graph(source, token).get("/me", params={"$select": "mail,userPrincipalName,displayName"})
    email = me.get("mail") or me.get("userPrincipalName") or me.get("displayName") or ""
    return token, email


def save_token(source_id: int, token: dict[str, Any] | None) -> None:
    db.update_source(source_id, token_json=crypto.encrypt(json.dumps(token)) if token else None)


def token_info(source: dict[str, Any]) -> dict[str, Any]:
    raw = source.get("token_json")
    if not raw:
        return {}
    try:
        return json.loads(crypto.decrypt(raw) or "{}")
    except ValueError:
        return {}


def can_upload(source: dict[str, Any]) -> bool:
    """True when the token carries the write scope and the source has a folder to upload into."""
    if (source.get("ms_link") or "").strip().lower() == SHARED_KEYWORD:
        return False  # "shared with me" has no single target folder
    return SCOPE_WRITE in (token_info(source).get("scope") or "").split()


def upload_block_reason(source: dict[str, Any]) -> str:
    if (source.get("ms_link") or "").strip().lower() == SHARED_KEYWORD:
        return "this source reads everything shared with the account; add a source for a specific folder to upload into it"
    return "this Microsoft 365 source was connected without the create-files permission; enable uploads and reconnect it in Admin → Sources"


# ------------------------------------------------------------------ Graph client


class Graph:
    """Thin Microsoft Graph client bound to one source's token; refreshes and re-saves it when needed."""

    def __init__(self, source: dict[str, Any], token: dict[str, Any] | None = None) -> None:
        self.source = source
        self.token = token or token_info(source)
        if not self.token:
            raise RuntimeError(f"Microsoft 365 '{source['name']}' is not connected yet. Open Admin → Sources and press Connect.")
        self.http = build_http_client()

    # -- auth -------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self.token.get("expires_at", 0) - 60 > time.time():
            return self.token["access_token"]
        if not self.token.get("refresh_token"):
            raise RuntimeError(f"Microsoft 365 '{self.source['name']}' needs to be reconnected (no refresh token).")
        try:
            new = _token_request(self.token.get("tenant") or "common", {"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"]})
        except RuntimeError as exc:
            raise RuntimeError(f"Microsoft 365 '{self.source['name']}' needs to be reconnected ({exc}).") from exc
        new["scope"] = new.get("scope") or self.token.get("scope", "")
        self.token = new
        if self.source.get("id"):
            save_token(int(self.source["id"]), new)
        return new["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # -- requests ---------------------------------------------------------

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """Send a request with auth, retrying on throttling (429/503) up to 3 times."""
        if url.startswith("/"):
            url = GRAPH + url
        for attempt in range(4):
            headers = {**self._headers(), **kw.pop("headers", {})}
            resp = self.http.request(method, url, headers=headers, **kw)
            if resp.status_code in (429, 503) and attempt < 3:
                time.sleep(min(float(resp.headers.get("Retry-After", "2")), 30))
                continue
            if resp.status_code >= 400:
                raise GraphError(resp)
            return resp
        raise RuntimeError("Microsoft Graph kept throttling the request")  # pragma: no cover

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", url, params=params).json()

    def paged(self, url: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Iterate a collection, following @odata.nextLink."""
        page = self.get(url, params)
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self.get(nxt)


class GraphError(RuntimeError):
    def __init__(self, resp: httpx.Response) -> None:
        self.status = resp.status_code
        try:
            err = resp.json().get("error", {})
            msg = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        except ValueError:
            msg = resp.text[:200]
        super().__init__(f"Microsoft Graph {resp.status_code} {msg}")


# ------------------------------------------------------------------ link resolution


def encode_share_url(url: str) -> str:
    """Graph's 'u!' encoding of a sharing URL."""
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def resolve_link(graph: Graph, link: str) -> dict[str, Any]:
    """Turn a pasted link (or ``drive_id:item_id``) into {"drive_id", "item_id", "label", "web_url"}."""
    link = link.strip()
    if link.lower() == SHARED_KEYWORD:
        return {"mode": "shared", "label": "Shared with me"}
    if "://" not in link and ":" in link:
        drive_id, item_id = link.split(":", 1)
        item = graph.get(f"/drives/{drive_id}/items/{item_id}", params={"$select": "id,name,folder,webUrl,parentReference"})
    else:
        try:
            item = graph.get(f"/shares/{encode_share_url(link)}/driveItem", params={"$select": "id,name,folder,webUrl,parentReference"})
        except GraphError as exc:
            if exc.status in (403, 404):
                raise RuntimeError("That link is not visible to the signed-in account. In SharePoint use Share → Copy link, "
                                   "shared with this person, and make sure the account has at least view access.") from exc
            raise
    if "folder" not in item:
        raise RuntimeError("The link points to a file; paste a link to a folder (or a document library).")
    drive_id = item["parentReference"]["driveId"]
    label = item["name"]
    try:
        drive = graph.get(f"/drives/{drive_id}", params={"$select": "name,webUrl,driveType"})
        site = (drive.get("webUrl") or "").split("/sites/")
        if len(site) > 1:
            label = f"{site[1].split('/')[0]}/{label}"
    except Exception:  # label is cosmetic
        pass
    return {"mode": "folder", "drive_id": drive_id, "item_id": item["id"], "label": label, "web_url": item.get("webUrl", "")}


def location_of(source: dict[str, Any]) -> dict[str, Any]:
    raw = source.get("ms_location")
    if not raw:
        return {"mode": "onedrive", "label": "OneDrive"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"mode": "onedrive", "label": "OneDrive"}


# ------------------------------------------------------------------ the source


class M365Source:
    kind = "m365"

    def __init__(self, source: dict[str, Any]) -> None:
        self.row = source
        self.id = int(source["id"])
        self.name = source["name"]
        self.graph = Graph(source)
        self.location = location_of(source)

    # -- roots ------------------------------------------------------------

    def _roots(self) -> list[tuple[str, str, str]]:
        """(drive_id, item_id, path label) for every folder tree this source covers."""
        mode = self.location.get("mode", "onedrive")
        if mode == "folder":
            return [(self.location["drive_id"], self.location["item_id"], self.location.get("label") or "Folder")]
        if mode == "shared":
            roots = []
            for it in self.graph.paged("/me/drive/sharedWithMe", params={"allowexternal": "true"}):
                remote = it.get("remoteItem") or {}
                if "folder" in remote and remote.get("parentReference", {}).get("driveId"):
                    roots.append((remote["parentReference"]["driveId"], remote["id"], f"Shared/{remote.get('name') or it.get('name')}"))
            return roots
        root = self.graph.get("/me/drive/root", params={"$select": "id,parentReference"})
        return [(root["parentReference"]["driveId"], root["id"], "OneDrive")]

    # -- listing ----------------------------------------------------------

    def list_files(self) -> Iterator[FileMeta]:
        for drive_id, item_id, label in self._roots():
            yield from self._walk(drive_id, item_id, label)
        if self.location.get("mode") == "shared":
            # files (not folders) shared directly with the account
            for it in self.graph.paged("/me/drive/sharedWithMe", params={"allowexternal": "true"}):
                remote = it.get("remoteItem") or {}
                if "file" in remote and remote.get("parentReference", {}).get("driveId"):
                    yield self._to_meta({**remote, "name": remote.get("name") or it.get("name")}, "Shared")

    def _walk(self, drive_id: str, folder_id: str, path: str) -> Iterator[FileMeta]:
        queue = [(folder_id, path)]
        while queue:
            fid, p = queue.pop(0)
            for it in self.graph.paged(f"/drives/{drive_id}/items/{fid}/children", params={"$select": ITEM_FIELDS, "$top": 200}):
                if "folder" in it:
                    queue.append((it["id"], f"{p}/{it['name']}"))
                elif "file" in it:
                    yield self._to_meta({**it, "parentReference": {"driveId": drive_id, **(it.get("parentReference") or {})}}, p)

    def _to_meta(self, it: dict[str, Any], folder: str, uploaded_by: str | None = None) -> FileMeta:
        drive_id = (it.get("parentReference") or {}).get("driveId", "")
        mime = (it.get("file") or {}).get("mimeType") or guess_mime(it.get("name", ""))
        if mime == "application/octet-stream":
            mime = guess_mime(it.get("name", ""))
        return FileMeta(
            source="m365", source_ref=self.id, source_id=f"{drive_id}:{it['id']}", name=it.get("name", ""), mime=mime,
            folder=f"{self.name}/{folder}", web_link=it.get("webUrl", ""), size=it.get("size"),
            modified_at=it.get("lastModifiedDateTime", ""), uploaded_by=uploaded_by,
        )

    # -- content ----------------------------------------------------------

    def fetch(self, meta: FileMeta) -> FileContent:
        drive_id, item_id = meta.source_id.split(":", 1)
        data = self.graph.request("GET", f"/drives/{drive_id}/items/{item_id}/content").content
        if meta.mime.startswith("text/"):
            return FileContent(mime=meta.mime, text=data.decode("utf-8", "replace"))
        return FileContent(mime=meta.mime, data=data)

    # -- change feed ------------------------------------------------------

    def changes_since(self, cursor: str | None) -> tuple[bool, str]:
        """Delta per watched root. Cursor = JSON {"<drive>:<item>": deltaLink}. First call only seeds."""
        try:
            links: dict[str, str] = json.loads(cursor) if cursor else {}
        except ValueError:
            links = {}
        roots = {f"{d}:{i}": (d, i) for d, i, _ in self._roots()}
        changed = set(links) != set(roots) and bool(links)  # a shared folder appeared or vanished
        new_links: dict[str, str] = {}
        for key, (drive_id, item_id) in roots.items():
            url = links.get(key) or f"{GRAPH}/drives/{drive_id}/items/{item_id}/delta?$select={ITEM_FIELDS}&$top=500"
            seeding = key not in links
            while True:
                page = self.graph.get(url)
                if not seeding:
                    for it in page.get("value", []):
                        if "file" in it or it.get("deleted"):
                            changed = True
                nxt = page.get("@odata.nextLink")
                if nxt:
                    url = nxt
                    continue
                new_links[key] = page.get("@odata.deltaLink", url)
                break
        return changed, json.dumps(new_links)

    # -- upload -----------------------------------------------------------

    def _upload_target(self) -> tuple[str, str, str]:
        """(drive_id, folder_item_id, folder label): the source folder, or OneDrive/'Ledger Uploads' (created once)."""
        mode = self.location.get("mode", "onedrive")
        if mode == "folder":
            return self.location["drive_id"], self.location["item_id"], "(uploads)"
        if mode == "shared":
            raise RuntimeError(upload_block_reason(self.row))
        root = self.graph.get("/me/drive/root", params={"$select": "id,parentReference"})
        drive_id = root["parentReference"]["driveId"]
        try:
            folder = self.graph.get(f"/drives/{drive_id}/root:/{UPLOAD_FOLDER_NAME}", params={"$select": "id"})
        except GraphError as exc:
            if exc.status != 404:
                raise
            folder = self.graph.request("POST", f"/drives/{drive_id}/items/{root['id']}/children",
                                        json={"name": UPLOAD_FOLDER_NAME, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}).json()
        return drive_id, folder["id"], UPLOAD_FOLDER_NAME

    def upload(self, filename: str, data: bytes, mime: str) -> FileMeta:
        drive_id, folder_id, label = self._upload_target()
        safe = filename.replace("/", "_").replace("\\", "_").replace(":", "_")
        if len(data) <= SMALL_UPLOAD_LIMIT:
            item = self.graph.request(
                "PUT", f"/drives/{drive_id}/items/{folder_id}:/{safe}:/content",
                params={"@microsoft.graph.conflictBehavior": "rename"}, content=data, headers={"Content-Type": mime},
            ).json()
        else:
            session = self.graph.request(
                "POST", f"/drives/{drive_id}/items/{folder_id}:/{safe}:/createUploadSession",
                json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": safe}},
            ).json()
            upload_url, item = session["uploadUrl"], None
            with build_http_client() as http:  # the session URL is pre-authenticated: no bearer header
                for start in range(0, len(data), CHUNK):
                    chunk = data[start:start + CHUNK]
                    resp = http.put(upload_url, content=chunk, headers={
                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"})
                    if resp.status_code >= 400:
                        raise GraphError(resp)
                    if resp.status_code in (200, 201):
                        item = resp.json()
            if item is None:
                raise RuntimeError("upload session finished without returning the file")
        item.setdefault("parentReference", {})["driveId"] = drive_id
        log.info("uploaded %s to Microsoft 365 source %s (item %s)", filename, self.name, item.get("id"))
        return self._to_meta(item, label, uploaded_by="app")


def is_supported(mime: str) -> bool:
    return mime in SUPPORTED_MIMES
