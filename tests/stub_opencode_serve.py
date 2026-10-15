#!/usr/bin/env python3
"""stub_opencode_serve.py — deterministic stand-in for `opencode serve`.

Implements just enough of the v1 server API for the brain to run offline:

  GET  /global/health                     {"healthy": true}
  POST /session                           -> {"id": "ses_...", "title": ...}
  GET  /session                           -> [session, ...]
  POST /session/{id}/prompt_async         -> 204, then the fake turn runs
  GET  /session/{id}/message              -> [{info, parts}, ...]
  POST /session/{id}/abort                -> true
  GET  /session/status                    -> {sid: {type: busy}} while running
  POST /permission/{id}/reply             -> true
  GET  /event                             SSE stream (session.status,
                                          session.idle, message.updated)

The fake model answers from STUB_ANSWER (default `SAY <last-channel-mentioned>
stub-ok`): it scans the prompt for a channel and echoes the marker there, so
the mock IRC server can assert on a known string through the REAL brain
pipeline: tail -> prompt_async -> SSE -> idle -> messages -> FIFO.

Usage: stub_opencode_serve.py [--port N]. Tests import StubServe directly.
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "stub-ok"


class StubState:
    def __init__(self, answer_env=None):
        self.lock = threading.Lock()
        self.sessions = {}          # sid -> {"id","title","messages","busy"}
        self.seq = 0
        self.events = []            # SSE backlog for late subscribers
        self.event_cond = threading.Condition()
        self.permission_replies = []  # (requestID, body)
        self.aborted = set()
        self.answer_env = answer_env if answer_env is not None else {}

    def broadcast(self, etype, props):
        ev = {"id": f"evt_{time.time_ns()}", "type": etype,
              "properties": props}
        with self.event_cond:
            self.events.append(ev)
            self.event_cond.notify_all()

    def new_sid(self):
        with self.lock:
            self.seq += 1
            return f"ses_stub{self.seq:06d}"


def default_answer(prompt, state):
    if "Do not send anything to IRC right now" in prompt:
        return ""                      # rehydrate prompts expect silence
    # Route to the LAST event line aimed at a channel: `[HH:MM] net|#chan ...`
    # (connect-time events with no target — numerics, modes — are skipped).
    route = "irc|#test"
    for m in re.finditer(r"^\[\d{2}:\d{2}\] (\S+)", prompt, re.M):
        head = m.group(1)
        tgt = head.partition("|")[2]
        if tgt.startswith(("#", "&")):
            route = head
    return f"SAY {route} {state.answer_env.get('STUB_MARKER', MARKER)}"


class StubServe:
    """Run in-thread for unit tests; serve_forever() for compose smoke."""

    def __init__(self, host="127.0.0.1", port=0, answer=None,
                 turn_delay=0.2, answer_env=None):
        self.state = StubState(answer_env=answer_env)
        self.answer = answer or default_answer
        self.turn_delay = turn_delay

        serve = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self):
                n = int(self.headers.get("content-length") or 0)
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n))
                except ValueError:
                    return {}

            def do_GET(self):
                st = serve.state
                if self.path == "/global/health":
                    self._json(200, {"healthy": True, "version": "stub"})
                elif self.path == "/session":
                    with st.lock:
                        sessions = list(st.sessions.values())
                    self._json(200, sessions)
                elif self.path == "/session/status":
                    with st.lock:
                        busy = {sid: {"type": "busy"}
                                for sid, s in st.sessions.items()
                                if s.get("busy")}
                    self._json(200, busy)
                elif self.path == "/event":
                    self._sse()
                elif re.match(r"^/session/[^/]+/message$", self.path):
                    sid = self.path.split("/")[2]
                    with st.lock:
                        sess = st.sessions.get(sid)
                        msgs = list(sess["messages"]) if sess else []
                    self._json(200, msgs)
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                st = serve.state
                body = self._read_body()
                if self.path == "/session":
                    sid = st.new_sid()
                    with st.lock:
                        st.sessions[sid] = {"id": sid,
                                            "title": body.get("title", ""),
                                            "messages": [], "busy": False}
                    st.broadcast("session.created",
                                 {"sessionID": sid, "info": {"id": sid}})
                    self._json(200, {"id": sid, "title": body.get("title", "")})
                elif re.match(r"^/session/[^/]+/prompt_async$", self.path):
                    sid = self.path.split("/")[2]
                    text = "".join(p.get("text", "")
                                   for p in body.get("parts", [])
                                   if p.get("type") == "text")
                    with st.lock:
                        sess = st.sessions.get(sid)
                        if sess is None:
                            self._json(404, {"error": "no session"})
                            return
                        sess["busy"] = True
                        msg_id = f"msg_u{time.time_ns()}"
                        sess["messages"].append(
                            {"info": {"id": msg_id, "role": "user"},
                             "parts": [{"type": "text", "text": text}]})
                    st.broadcast("session.status",
                                 {"sessionID": sid,
                                  "status": {"type": "busy"}})
                    threading.Thread(target=serve._run_turn,
                                     args=(sid, text), daemon=True).start()
                    self.send_response(204)
                    self.end_headers()
                elif re.match(r"^/session/[^/]+/abort$", self.path):
                    sid = self.path.split("/")[2]
                    with st.lock:
                        st.aborted.add(sid)
                        sess = st.sessions.get(sid)
                        if sess:
                            sess["busy"] = False
                    self._json(200, True)
                elif re.match(r"^/permission/[^/]+/reply$", self.path):
                    req_id = self.path.split("/")[2]
                    with st.lock:
                        st.permission_replies.append((req_id, body))
                    self._json(200, True)
                else:
                    self._json(404, {"error": "not found"})

            def _sse(self):
                st = serve.state
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                self.wfile.write(b"data: "
                                 b'{"type":"server.connected",'
                                 b'"properties":{}}\n\n')
                self.wfile.flush()
                sent = 1
                while True:
                    with st.event_cond:
                        while (len(st.events) <= sent
                               and not getattr(self.server, "stop", False)):
                            st.event_cond.wait(timeout=0.5)
                        if len(st.events) <= sent:
                            return
                        ev = st.events[sent]
                        sent += 1
                    try:
                        self.wfile.write(
                            f"data: {json.dumps(ev)}\n\n".encode())
                        self.wfile.flush()
                    except (OSError, ValueError):
                        return

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.stop = False
        self.port = self.httpd.server_address[1]
        self.url = f"http://{host}:{self.port}"

    def _run_turn(self, sid, prompt):
        time.sleep(self.turn_delay)
        st = self.state
        if sid in st.aborted:
            return
        answer = self.answer(prompt, st)
        amsg = {"info": {"id": f"msg_a{time.time_ns()}",
                         "role": "assistant"},
                "parts": [{"type": "text", "text": answer}]}
        with st.lock:
            sess = st.sessions.get(sid)
            if sess is None:
                return
            sess["messages"].append(amsg)
            sess["busy"] = False
        st.broadcast("session.status",
                     {"sessionID": sid, "status": {"type": "idle"}})
        st.broadcast("message.updated",
                     {"sessionID": sid, "info": amsg["info"]})
        st.broadcast("session.idle", {"sessionID": sid})

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def serve_forever(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.stop = True
        with self.state.event_cond:
            self.state.event_cond.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()

    def permission_denied_count(self):
        return len(self.state.permission_replies)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=46099)
    a = ap.parse_args()
    serve = StubServe(host=a.host, port=a.port)
    print(f"stub opencode serve on {serve.url}", flush=True)
    serve.serve_forever()


if __name__ == "__main__":
    main()
