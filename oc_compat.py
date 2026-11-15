#!/usr/bin/env python3
"""Shared stdlib helpers for the OpenCode v1/v2 HTTP surfaces."""
import base64
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class AuthError(OSError):
    """The v2 API needs OPENCODE_SERVER_PASSWORD; never include the value."""


def detect_version(binary="opencode", override=""):
    if override:
        return override.strip()
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or out.stderr).strip()


def is_v2(version):
    m = re.search(r"(\d+)\.", version or "")
    return bool(m and int(m.group(1)) >= 2)


def auth_headers(password, username="opencode"):
    if not password:
        return {}
    token = base64.b64encode(
        f"{username}:{password}".encode()).decode()
    return {"authorization": "Basic " + token}


def request(url, method="GET", body=None, timeout=15, password="",
            username="opencode", version="1.19.5"):
    if is_v2(version) and not password:
        raise AuthError(
            "OPENCODE_SERVER_PASSWORD is required for opencode 2.x /api")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    headers.update(auth_headers(password, username))
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError(
                "opencode API rejected credentials (401); check "
                "OPENCODE_SERVER_PASSWORD") from e
        raise
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def url_with(base, path, params=None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url
