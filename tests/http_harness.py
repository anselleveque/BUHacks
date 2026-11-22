"""Raw-socket HTTP exchange against ``foodapp.server.Handler`` for tests.

The server answers and closes the connection without reading the body of a
request it rejects early (bad Origin or Host, bad or oversized Content-Length).
A client that is still sending then gets BrokenPipeError or ConnectionResetError
(a timing race, so it only fails now and then). ``http.client`` cannot cope with
that, because it raises from ``request()`` before the response is read. This
module sends the request itself, tolerates the server closing early, and then
reads whatever response the server already wrote.
"""
import http.client
import json
import socket
import threading

from foodapp.server import Handler

TIMEOUT = 5
# Errors that only mean "the server closed before we finished sending".
EARLY_CLOSE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def build_request(method, path, body=None, headers=None, host="localhost"):
    """Serialize a request. Content-Length is added unless the caller sets it,
    and Host defaults to ``host``, like http.client does."""
    headers = dict(headers or {})
    if isinstance(body, str):
        body = body.encode("iso-8859-1")  # what http.client does
    if body is None and method in ("POST", "PUT", "PATCH") and "content-length" not in {n.lower() for n in headers}:
        headers["Content-Length"] = "0"
    names = {name.lower() for name in headers}
    lines = [f"{method} {path} HTTP/1.1"]
    if "host" not in names:
        lines.append(f"Host: {host}")
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    if body is not None and "content-length" not in names:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")


def exchange(server, data, *, timeout=TIMEOUT, client_address=("localhost", 0)):
    """Send raw bytes to a Handler over a socketpair and read one response.

    Returns ``(response, raw_body, early_close)``. ``response`` is None when the
    server closed without writing a response, and ``early_close`` says whether the
    server hung up while we were still sending. The worker thread must finish
    (so the connection really closed), or the test fails.
    """
    client, peer = socket.socketpair()

    def handle():
        with peer:
            Handler(peer, client_address, server)

    worker = threading.Thread(target=handle, daemon=True)
    worker.start()
    early_close = False
    response = raw = None
    with client:
        client.settimeout(timeout)
        try:
            client.sendall(data)
        except EARLY_CLOSE:
            early_close = True
        candidate = http.client.HTTPResponse(client)
        try:
            candidate.begin()
            response, raw = candidate, candidate.read()
        except (http.client.RemoteDisconnected, *EARLY_CLOSE):
            pass  # no readable response: the server closed the connection
        finally:
            candidate.close()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError("the server handler did not close the connection")
    return response, raw, early_close


def request(server, method, path, body=None, headers=None, *, timeout=TIMEOUT):
    """Return ``(status, headers, data)`` where data is parsed JSON for JSON
    responses, else the raw bytes. Raises AssertionError if the server wrote no
    response at all (it only closed the connection)."""
    response, raw, early_close = exchange(server, build_request(method, path, body, headers), timeout=timeout)
    if response is None:
        raise AssertionError(f"{method} {path}: connection closed without a response"
                             + (" while the request was being sent" if early_close else ""))
    data = json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw
    return response.status, response.headers, data
