"""Shared limits for Fletchery's JSON POST handlers."""
import json
from http.server import BaseHTTPRequestHandler


MAX_BODY = 8 * 1024


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class JsonRequestHandler(BaseHTTPRequestHandler):
    timeout = 30  # socket inactivity limit, including header and body reads

    def read_json_body(self, limit=MAX_BODY):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) > 1:
            raise RequestError(400, "ambiguous or unsupported request framing")
        value = lengths[0].strip(" \t") if lengths else "0"
        if not value.isascii() or not value.isdecimal():
            raise RequestError(400, "invalid Content-Length")
        digits = value.lstrip("0") or "0"
        if len(digits) > len(str(limit)) or int(digits) > limit:
            raise RequestError(413, "request too large")
        length = int(digits)
        if not length:
            return {}
        try:
            body = self.rfile.read(length)
        except TimeoutError as exc:
            raise RequestError(408, "request timed out") from exc
        if len(body) != length:
            raise RequestError(400, "incomplete request")
        try:
            data = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise RequestError(400, "bad JSON") from exc
        if not isinstance(data, dict):
            raise RequestError(400, "bad JSON")
        return data
