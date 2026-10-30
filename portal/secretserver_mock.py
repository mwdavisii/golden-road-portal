"""The mock Secret Server (spec 2026-10-29 Delinea secrets, "The mock"): an httpx.MockTransport that answers exactly the requests in
the client's table (portal.secretserver), in Secret Server's response shapes, over the dummy data in mock_secret_server.json.

- A token is minted per mock user (`mock-<username>`, the password is not checked: it is a mock) and the platform account
  (`golden-road-eso`) is one of the users.
- A user sees the secrets in their folders and the secrets shared with them: a person sees their folders, the platform account
  sees what is shared with it.
- Anything outside the table answers 404, every value endpoint among them (GET /api/v1/secrets/{id}, /fields/...), so a client
  call outside the table fails its tests. The mock holds no values: every value would be MOCK_VALUE, and none is ever served.
- The person's mock user is the one whose github_login matches a GitHub link the person verified through OAuth (source
  github-oauth, with a github_user_id): user_for. An admin-made link never maps, even with an id from the collector. The real version gets the person's own Secret Server token at sign-in instead (spec D7)."""

import dataclasses as dc
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from .secretserver import Client

MOCK = Path(__file__).resolve().parent / "mock_secret_server.json"
BASE_URL = "https://secretserver.mock/SecretServer"
PREFIX = "/SecretServer"
MOCK_VALUE = "<mock>"  # what any value would be; the mock serves none
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_NAME = 200
SECRET_PATH = re.compile(r"/api/v1/secrets/([1-9][0-9]{0,9})/summary")
TEMPLATE_PATH = re.compile(r"/api/v1/secret-templates/([1-9][0-9]{0,9})")
MAX_TAKE = 100
FOLDER_ID = re.compile(r"[0-9]{1,10}")  # ASCII digits only: str.isdigit takes other scripts' digits and superscripts


class MockSecretServerError(ValueError):
    pass


@dc.dataclass(frozen=True)
class Folder:
    id: int
    name: str
    path: str


@dc.dataclass(frozen=True)
class Template:
    id: int
    name: str
    fields: tuple[dict, ...]  # Secret Server's fields[] records: fieldSlugName, displayName, isPassword, isFile


@dc.dataclass(frozen=True)
class Secret:
    id: int
    name: str
    folder_id: int
    template_id: int
    shared_with: frozenset


@dc.dataclass(frozen=True)
class User:
    username: str
    github_login: str | None
    folders: frozenset


def _keys(entry, keys, what):
    if not isinstance(entry, dict) or set(entry) != keys:
        raise MockSecretServerError(f"the mock Secret Server's {what} has missing or unknown keys")


def _id(value, what):
    if type(value) is not int or value < 1:
        raise MockSecretServerError(f"the mock Secret Server's {what} must be a whole number from 1")
    return value


def _name(value, what):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME or any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise MockSecretServerError(f"the mock Secret Server's {what} must be 1 to {MAX_NAME} characters with no control or format "
                                    "characters")
    return value


def _text(value, pattern, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MockSecretServerError(f"the mock Secret Server's {what} is not valid")
    return value


def _list(value, what):
    if not isinstance(value, list):
        raise MockSecretServerError(f"the mock Secret Server's {what} must be a list")
    return value


def _unique(values, what):
    values = list(values)
    if len(set(values)) != len(values):
        raise MockSecretServerError(f"the mock Secret Server lists a {what} twice")


def parse(doc) -> "MockSecretServer":
    _keys(doc, {"schema", "platform_user", "folders", "templates", "secrets", "users"}, "document")
    if doc["schema"] != "mock-secret-server/v1":
        raise MockSecretServerError("the mock Secret Server is not a mock-secret-server/v1")
    folders = []
    for f in _list(doc["folders"], "folders"):
        _keys(f, {"id", "folderName", "folderPath"}, "folder")
        folders.append(Folder(_id(f["id"], "folder id"), _name(f["folderName"], "folder name"), _name(f["folderPath"], "folder path")))
    _unique((f.id for f in folders), "folder id")
    templates = []
    for t in _list(doc["templates"], "templates"):
        _keys(t, {"id", "name", "fields"}, "template")
        fields = []
        for field in _list(t["fields"], "template fields"):
            _keys(field, {"fieldSlugName", "displayName", "isPassword", "isFile"}, "template field")
            if type(field["isPassword"]) is not bool or type(field["isFile"]) is not bool:
                raise MockSecretServerError("the mock Secret Server's isPassword and isFile must be true or false")
            fields.append({"fieldSlugName": _text(field["fieldSlugName"], SLUG, "field slug"),
                           "displayName": _name(field["displayName"], "field name"),
                           "isPassword": field["isPassword"], "isFile": field["isFile"]})
        _unique((f["fieldSlugName"] for f in fields), "field slug in one template")
        templates.append(Template(_id(t["id"], "template id"), _name(t["name"], "template name"), tuple(fields)))
    _unique((t.id for t in templates), "template id")
    users = []
    for u in _list(doc["users"], "users"):
        _keys(u, {"username", "github_login", "folders"}, "user")
        login = None if u["github_login"] is None else _text(u["github_login"], LOGIN, "user's github_login")
        users.append(User(_text(u["username"], USERNAME, "username"), login,
                          frozenset(_id(f, "user's folder") for f in _list(u["folders"], "user's folders"))))
    _unique((u.username for u in users), "username")
    _unique((u.github_login.lower() for u in users if u.github_login), "github_login")
    folder_ids, template_ids, usernames = {f.id for f in folders}, {t.id for t in templates}, {u.username for u in users}
    if any(not u.folders <= folder_ids for u in users):
        raise MockSecretServerError("a mock Secret Server user names a folder the mock does not list")
    secrets = []
    for s in _list(doc["secrets"], "secrets"):
        _keys(s, {"id", "name", "folderId", "secretTemplateId", "shared_with"}, "secret")
        shared = _list(s["shared_with"], "secret's shared_with")
        if not all(isinstance(n, str) for n in shared) or not set(shared) <= usernames:
            raise MockSecretServerError("a mock secret is shared with a user the mock does not list")
        secret = Secret(_id(s["id"], "secret id"), _name(s["name"], "secret name"), _id(s["folderId"], "secret's folderId"),
                        _id(s["secretTemplateId"], "secret's secretTemplateId"), frozenset(shared))
        if secret.folder_id not in folder_ids or secret.template_id not in template_ids:
            raise MockSecretServerError("a mock secret names a folder or template the mock does not list")
        secrets.append(secret)
    _unique((s.id for s in secrets), "secret id")
    platform = doc["platform_user"]
    if platform not in usernames:
        raise MockSecretServerError("the mock Secret Server's platform_user is not one of its users")
    if next(u for u in users if u.username == platform).github_login is not None:
        raise MockSecretServerError("the mock Secret Server's platform_user must have no github_login: it is never a person")
    return MockSecretServer(platform, tuple(folders), tuple(templates), tuple(secrets), tuple(users))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise MockSecretServerError("the mock Secret Server names a key twice in one object")
    return dict(pairs)


def load_mock() -> "MockSecretServer":
    try:
        return parse(json.loads(MOCK.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys))
    except MockSecretServerError:
        raise
    except (OSError, ValueError) as exc:
        raise MockSecretServerError(f"the mock Secret Server can't be loaded: {type(exc).__name__}") from None


def _answer(status, body):
    return httpx.Response(status, json=body)


NOT_IN_MOCK = {"message": "not part of the mock"}


@dc.dataclass(frozen=True)
class MockSecretServer:
    name = "mock-secret-server"  # what binding/v2 records as a secret's verified_by
    platform_user: str
    folders: tuple[Folder, ...]
    templates: tuple[Template, ...]
    secrets: tuple[Secret, ...]
    users: tuple[User, ...]

    def _user(self, username) -> User | None:
        return next((u for u in self.users if u.username == username), None)

    def visible(self, username) -> list[Secret]:
        """The secrets a user can see: those in their folders and those shared with them."""
        user = self._user(username)
        if user is None:
            return []
        return [s for s in self.secrets if s.folder_id in user.folders or username in s.shared_with]

    def user_for(self, links) -> str | None:
        """The mock user of a person with these GitHub links: the first GitHub link the person verified through OAuth (source
        github-oauth and a github_user_id) whose login is a mock user's github_login. An admin-made link never maps, even with an
        id: only the person's own OAuth proves the login is theirs. None without one."""
        for link in links:
            if getattr(link, "source", None) != "github-oauth" or link.github_user_id is None \
                    or not isinstance(link.github_login, str):
                continue
            for user in self.users:
                if user.github_login is not None and user.github_login.lower() == link.github_login.lower():
                    return user.username
        return None

    def client(self) -> Client:
        return Client(BASE_URL, http=httpx.Client(transport=self.transport()))

    def _record(self, s: Secret) -> dict:
        template = next(t for t in self.templates if t.id == s.template_id)
        return {"id": s.id, "name": s.name, "folderId": s.folder_id, "secretTemplateId": s.template_id,
                "secretTemplateName": template.name, "active": True}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host != "secretserver.mock" or not path.startswith(PREFIX + "/"):
            return _answer(404, NOT_IN_MOCK)
        path = path[len(PREFIX):]
        if request.method == "POST" and path == "/oauth2/token":
            return self._token(request)
        if request.method != "GET":
            return _answer(404, NOT_IN_MOCK)
        if path == "/api/v1/secrets":
            route = self._search
        elif path == "/api/v1/folders":
            route = self._folders
        elif m := TEMPLATE_PATH.fullmatch(path):
            route = lambda user, request: self._template(int(m.group(1)))  # noqa: E731
        elif m := SECRET_PATH.fullmatch(path):
            route = lambda user, request: self._summary(user, int(m.group(1)))  # noqa: E731
        else:  # GET /api/v1/secrets/{id} (items[].itemValue), /fields/..., and everything else
            return _answer(404, NOT_IN_MOCK)
        auth = request.headers.get("authorization", "")
        username = auth[len("Bearer mock-"):] if auth.startswith("Bearer mock-") else None
        if username is None or self._user(username) is None:
            return _answer(401, {"message": "Authentication failed or was not provided."})
        return route(username, request)

    def _token(self, request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return _answer(400, {"error": "invalid_request"})
        form = {k: v[-1] for k, v in parse_qs(request.content.decode("utf-8", "replace")).items()}
        if form.get("grant_type") != "password" or self._user(form.get("username")) is None:
            return _answer(400, {"error": "invalid_grant"})
        return _answer(200, {"access_token": f"mock-{form['username']}", "token_type": "bearer", "expires_in": 1200})

    @staticmethod
    def _paged(records, request):
        try:
            skip, take = int(request.url.params.get("skip", "0")), int(request.url.params.get("take", "10"))
        except ValueError:
            return _answer(400, {"message": "skip and take must be whole numbers"})
        if skip < 0 or not 1 <= take <= MAX_TAKE:
            return _answer(400, {"message": f"skip must be 0 or more and take from 1 to {MAX_TAKE}"})
        return _answer(200, {"total": len(records), "skip": skip, "take": take, "hasNext": skip + take < len(records),
                             "records": records[skip:skip + take]})

    def _search(self, username, request):
        text = request.url.params.get("filter.searchText", "").lower()
        folder = request.url.params.get("filter.folderId")
        if folder is not None and not FOLDER_ID.fullmatch(folder):
            return _answer(400, {"message": "filter.folderId must be a whole number"})
        found = [s for s in self.visible(username) if text in s.name.lower() and (folder is None or s.folder_id == int(folder))]
        return self._paged([self._record(s) for s in found], request)

    def _folders(self, username, request):
        text = request.url.params.get("filter.searchText", "").lower()
        mine = self._user(username).folders
        found = [f for f in self.folders if f.id in mine and (text in f.name.lower() or text in f.path.lower())]
        return self._paged([{"id": f.id, "folderName": f.name, "folderPath": f.path} for f in found], request)

    def _template(self, template_id):
        template = next((t for t in self.templates if t.id == template_id), None)
        if template is None:
            return _answer(404, {"message": "Secret template not found."})
        return _answer(200, {"id": template.id, "name": template.name, "fields": [dict(f) for f in template.fields]})

    def _summary(self, username, secret_id):
        secret = next((s for s in self.visible(username) if s.id == secret_id), None)
        if secret is None:  # unknown or not visible alike: the mock never tells which
            return _answer(403, {"message": "Access Denied"})
        return _answer(200, self._record(secret))
