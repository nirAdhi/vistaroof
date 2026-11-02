"""An in-memory stand-in for Microsoft's token endpoint + Microsoft Graph, served through httpx.MockTransport.

Covers exactly the calls ``app/microsoft.py`` makes: token exchange/refresh, /me, /shares, drive item
listing (paged), downloads (with a cross-origin 302 like the real service), delta, sharedWithMe,
folder creation and uploads (single PUT and upload session). Nothing touches the network.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app import microsoft
from conftest import jpeg_bytes


class FakeGraph:
    def __init__(self, email: str = "suraj@prasanit.org", granted_scope: str = "User.Read Files.Read.All Sites.Read.All Files.ReadWrite.All") -> None:
        self.email = email
        self.granted_scope = granted_scope
        self.token_calls: list[dict] = []
        self.download_headers: list[dict] = []
        self.created: list[dict] = []
        self.readonly = False  # 403 on writes (a "Can view" share)
        self.refresh_fails = False
        self.version = 0
        # drive -> item -> item dict; folders have "folder", files have "file"
        self.items: dict[str, dict[str, dict]] = {}
        self.children: dict[tuple[str, str], list[str]] = {}
        self.contents: dict[tuple[str, str], bytes] = {}
        self.shares: dict[str, tuple[str, str]] = {}  # sharing url -> (drive, item)
        self.drives: dict[str, dict] = {}
        self.shared_with_me: list[dict] = []
        self.changes: dict[str, list[tuple[int, dict]]] = {}  # drive -> [(version, item-or-deleted)]
        self.sessions: dict[str, dict] = {}

    # ------------------------------------------------------------ builders

    def add_drive(self, drive_id: str, web_url: str, root_id: str = "root") -> None:
        self.drives[drive_id] = {"id": drive_id, "name": "Documents", "webUrl": web_url, "driveType": "documentLibrary"}
        self.items.setdefault(drive_id, {})[root_id] = {"id": root_id, "name": "root", "folder": {"childCount": 0}, "parentReference": {"driveId": drive_id}}
        self.children.setdefault((drive_id, root_id), [])

    def add_folder(self, drive_id: str, parent_id: str, item_id: str, name: str, web_url: str = "") -> None:
        self.items[drive_id][item_id] = {"id": item_id, "name": name, "folder": {"childCount": 0}, "webUrl": web_url or f"https://x.sharepoint.com/{name}",
                                         "parentReference": {"driveId": drive_id, "id": parent_id}}
        self.children.setdefault((drive_id, parent_id), []).append(item_id)
        self.children.setdefault((drive_id, item_id), [])

    def add_file(self, drive_id: str, parent_id: str, item_id: str, name: str, mime: str, data: bytes, modified: str = "2026-03-01T10:00:00Z") -> dict:
        item = {"id": item_id, "name": name, "file": {"mimeType": mime}, "size": len(data), "lastModifiedDateTime": modified,
                "webUrl": f"https://x.sharepoint.com/{name}", "parentReference": {"driveId": drive_id, "id": parent_id}}
        self.items[drive_id][item_id] = item
        self.children.setdefault((drive_id, parent_id), []).append(item_id)
        self.contents[(drive_id, item_id)] = data
        self.version += 1
        self.changes.setdefault(drive_id, []).append((self.version, item))
        return item

    def remove_file(self, drive_id: str, item_id: str) -> None:
        item = self.items[drive_id].pop(item_id)
        parent = item["parentReference"]["id"]
        self.children[(drive_id, parent)].remove(item_id)
        self.version += 1
        self.changes.setdefault(drive_id, []).append((self.version, {"id": item_id, "name": item["name"], "deleted": {"state": "deleted"}}))

    def share(self, url: str, drive_id: str, item_id: str) -> None:
        self.shares[url] = (drive_id, item_id)

    # ------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        host = url.hostname or ""
        if host == "login.microsoftonline.com":
            return self._token(path, request)
        if host == "download.example.com":
            self.download_headers.append(dict(request.headers))
            drive_id, item_id = path.strip("/").split("/")
            return httpx.Response(200, content=self.contents[(drive_id, item_id)])
        if host == "upload.example.com":
            return self._upload_chunk(path.strip("/"), request)
        if host != "graph.microsoft.com":
            return httpx.Response(500, text=f"unexpected host {host}")
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return self._err(401, "InvalidAuthenticationToken", "no bearer token")
        if request.headers["Authorization"] == "Bearer expired":
            return self._err(401, "InvalidAuthenticationToken", "token expired")
        p = path.replace("/v1.0", "", 1)
        m = request.method
        if p == "/me":
            return httpx.Response(200, json={"mail": self.email, "userPrincipalName": self.email, "displayName": "Suraj"})
        if p == "/me/drive/root":
            return httpx.Response(200, json={"id": "root", "parentReference": {"driveId": "od1"}})
        if p == "/me/drive/sharedWithMe":
            return httpx.Response(200, json={"value": self.shared_with_me})
        if p.startswith("/shares/"):
            enc = p.split("/")[2]
            link = base64.urlsafe_b64decode(enc[2:] + "=" * (-len(enc[2:]) % 4)).decode()
            if link not in self.shares:
                return self._err(404, "itemNotFound", "The resource could not be found.")
            d, i = self.shares[link]
            return httpx.Response(200, json=self.items[d][i])
        mm = re.match(r"^/drives/([^/]+)(/.*)?$", p)
        if not mm:
            return httpx.Response(404, text=f"unknown path {p}")
        drive_id, rest = mm.group(1), mm.group(2) or ""
        if drive_id not in self.items and drive_id not in self.drives:
            return self._err(404, "itemNotFound", "drive not found")
        if rest == "":
            return httpx.Response(200, json=self.drives.get(drive_id, {"id": drive_id, "webUrl": ""}))
        if rest.startswith("/root:/"):
            name = rest[len("/root:/"):]
            for cid in self.children.get((drive_id, "root"), []):
                if self.items[drive_id][cid]["name"] == name:
                    return httpx.Response(200, json=self.items[drive_id][cid])
            return self._err(404, "itemNotFound", "no such path")
        im = re.match(r"^/items/([^/:]+)(.*)$", rest)
        if not im:
            return httpx.Response(404, text=f"unknown path {p}")
        item_id, tail = im.group(1), im.group(2)
        if item_id not in self.items[drive_id] and not tail.startswith(":/"):
            return self._err(404, "itemNotFound", "item not found")
        if tail == "" and m == "GET":
            return httpx.Response(200, json=self.items[drive_id][item_id])
        if tail == "/children" and m == "GET":
            ids = self.children.get((drive_id, item_id), [])
            page = int(q.get("page", "1"))
            size = 2
            chunk = [self.items[drive_id][i] for i in ids[(page - 1) * size: page * size]]
            body = {"value": chunk}
            if page * size < len(ids):
                body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/children?page={page + 1}"
            return httpx.Response(200, json=body)
        if tail == "/children" and m == "POST":
            if self.readonly:
                return self._err(403, "accessDenied", "Access denied")
            body = json.loads(request.content)
            new_id = f"folder-{len(self.created) + 1}"
            self.add_folder(drive_id, item_id, new_id, body["name"])
            self.created.append({"kind": "folder", "name": body["name"], "id": new_id})
            return httpx.Response(201, json=self.items[drive_id][new_id])
        if tail == "/content" and m == "GET":
            return httpx.Response(302, headers={"Location": f"https://download.example.com/{drive_id}/{item_id}"})
        if tail == "/delta" and m == "GET":
            since = int(q.get("token", "-1"))
            if since < 0:  # first call: everything under the root (files only is enough for the watcher)
                value = [it for it in self.items[drive_id].values() if "file" in it]
            else:
                value = [it for v, it in self.changes.get(drive_id, []) if v > since]
            return httpx.Response(200, json={"value": value, "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/delta?token={self.version}"})
        um = re.match(r"^:/([^:]+):/(content|createUploadSession)$", tail)
        if um:
            name, action = um.group(1), um.group(2)
            if self.readonly:
                return self._err(403, "accessDenied", "Access denied")
            if action == "content" and m == "PUT":
                assert q.get("@microsoft.graph.conflictBehavior") == "rename"
                return httpx.Response(201, json=self._store_upload(drive_id, item_id, name, request.content, request.headers.get("Content-Type", "")))
            if action == "createUploadSession" and m == "POST":
                sid = f"sess{len(self.sessions) + 1}"
                self.sessions[sid] = {"drive": drive_id, "folder": item_id, "name": name, "buf": bytearray(), "ranges": []}
                return httpx.Response(200, json={"uploadUrl": f"https://upload.example.com/{sid}"})
        return httpx.Response(404, text=f"unhandled {m} {p}")

    def _store_upload(self, drive_id: str, folder_id: str, name: str, data: bytes, mime: str) -> dict:
        existing = {self.items[drive_id][i]["name"] for i in self.children.get((drive_id, folder_id), [])}
        final = name
        n = 1
        while final in existing:
            stem, dot, ext = name.rpartition(".")
            final = f"{stem or ext} {n}.{ext}" if dot else f"{name} {n}"
            n += 1
        item = self.add_file(drive_id, folder_id, f"up-{len(self.created) + 1}", final, mime or "application/octet-stream", data)
        self.created.append({"kind": "file", "name": final, "data": bytes(data), "parent": folder_id, "drive": drive_id})
        return item

    def _upload_chunk(self, sid: str, request: httpx.Request) -> httpx.Response:
        s = self.sessions[sid]
        assert "Authorization" not in request.headers, "upload session URLs are pre-authenticated"
        rng = request.headers["Content-Range"]  # bytes a-b/total
        a, b_total = rng.split(" ")[1].split("-")
        b, total = b_total.split("/")
        s["ranges"].append((int(a), int(b)))
        s["buf"] += request.content
        if int(b) + 1 == int(total):
            return httpx.Response(201, json=self._store_upload(s["drive"], s["folder"], s["name"], bytes(s["buf"]), "application/pdf"))
        return httpx.Response(202, json={"nextExpectedRanges": [f"{int(b) + 1}-"]})

    def _token(self, path: str, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        form["tenant"] = path.split("/")[1]
        self.token_calls.append(form)
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "good-code" or not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70000: bad code"})
            return httpx.Response(200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600, "scope": self.granted_scope})
        if form.get("grant_type") == "refresh_token":
            if self.refresh_fails or form.get("refresh_token") == "bad":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS700082: refresh token expired"})
            return httpx.Response(200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600, "scope": self.granted_scope})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    @staticmethod
    def _err(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message}})


def install_fake_graph(monkeypatch, fake: FakeGraph) -> None:
    """Route every httpx client microsoft.py creates through the fake."""
    monkeypatch.setattr(microsoft, "build_http_client", lambda: httpx.Client(transport=fake.transport(), follow_redirects=True, timeout=10))


def sharepoint_fixture() -> FakeGraph:
    """A SharePoint library 'HRAccounts/Shared Documents' with an Invoices folder, a 2026 sub-folder and a few files."""
    g = FakeGraph()
    g.add_drive("drv1", "https://vistaroofing.sharepoint.com/sites/HRAccounts/Shared Documents")
    g.add_folder("drv1", "root", "inv", "Invoices", "https://vistaroofing.sharepoint.com/sites/HRAccounts/Shared%20Documents/Invoices")
    g.add_folder("drv1", "inv", "y2026", "2026")
    g.add_file("drv1", "inv", "f1", "INV-1001 Murphy.pdf", "application/pdf", b"%PDF-1.4 inv 1001")
    g.add_file("drv1", "y2026", "f2", "Chadwicks bill.pdf", "application/pdf", b"%PDF-1.4 bill")
    g.add_file("drv1", "y2026", "f3", "till receipt.jpg", "image/jpeg", jpeg_bytes())
    g.add_file("drv1", "inv", "f4", "notes.one", "application/msonenote", b"onenote")
    g.add_file("drv1", "inv", "f5", "AIB statement.txt", "text/plain", "AIB statement March".encode())
    g.share("https://vistaroofing.sharepoint.com/:f:/s/HRAccounts/EabcInvoicesLink", "drv1", "inv")
    return g
