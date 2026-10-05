#!/usr/bin/env python3
"""Exercise managed users and saved prompt threads on an explicitly designated test server."""

import argparse
import concurrent.futures
import json
import os
import secrets
import statistics
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-test-data", action="store_true", required=True,
                        help="authorize creating test accounts and chat history")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--bootstrap-password", type=Path,
                        default=Path("/workspace/studio/auth/.bootstrap_password"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    timings = []

    def save_session(session):
        descriptor = os.open(args.session, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(session, output)

    def request(path, token=None, payload=None, method=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(args.base + path, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, b""
        elapsed = time.monotonic() - started
        timings.append(elapsed)
        if status != expected:
            raise AssertionError(f"{method or ('POST' if data else 'GET')} {path}: HTTP {status}, expected {expected}")
        return json.loads(body) if body else None

    def login(credentials):
        return request("/api/auth/login", payload=credentials)["access_token"]

    def set_password(credentials, token):
        new_password = secrets.token_urlsafe(32)
        reply = request("/api/auth/change-password", token,
                        {"current_password": credentials["password"], "new_password": new_password})
        credentials["password"] = new_password
        return reply["access_token"]

    if args.session.exists():
        session = json.loads(args.session.read_text())
    else:
        if args.verify_only:
            parser.error("verification requires an existing session file")
        owner = {"username": "unsloth", "password": args.bootstrap_password.read_text().strip()}
        owner_token = set_password(owner, login(owner))
        session = {"users": [owner], "threads": []}
        for _ in range(3):
            username = "probe-" + uuid.uuid4().hex[:12]
            setup = request("/api/accounts", owner_token, {"username": username}, expected=201)
            user = {"username": username, "password": setup["setup_code"]}
            set_password(user, login(user))
            session["users"].append(user)
        save_session(session)
        print("Created owner session and three managed test accounts", flush=True)

    tokens = [login(user) for user in session["users"]]

    def prompt_thread(item):
        user, number = item
        token = tokens[user]
        thread = uuid.uuid4().hex
        created = int(time.time() * 1000)
        request("/api/chat/threads", token,
                {"id": thread, "title": "SQLite concurrency probe", "modelType": "base",
                 "modelId": "", "createdAt": created})
        messages = []
        for index in range(8):
            message = uuid.uuid4().hex
            payload = {"id": message, "threadId": thread, "role": "user", "createdAt": created + index,
                       "content": [{"type": "text", "text": f"Database probe {number}/{index}; no generation"}]}
            request(f"/api/chat/threads/{thread}/messages/{message}", token, payload, method="PUT")
            request("/api/liveness")
            messages.append(message)
        return {"user": user, "id": thread, "messages": messages}

    if not args.verify_only:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            session["threads"] = list(pool.map(prompt_thread, [(user, number) for user in range(4) for number in range(4)]))
        save_session(session)
        print("Saved 16 parallel prompt threads and 128 messages across four accounts", flush=True)

    for thread in session["threads"]:
        token = tokens[thread["user"]]
        reply = request(f"/api/chat/threads/{thread['id']}/messages", token)
        actual = {message["id"] for message in reply["messages"]}
        assert actual == set(thread["messages"]), "Saved messages changed or were lost"
        other = tokens[(thread["user"] + 1) % len(tokens)]
        request(f"/api/chat/threads/{thread['id']}", other, expected=404)
    print("Verified all saved messages and cross-account isolation", flush=True)
    request("/api/models/gguf-variants?repo_id=unsloth%2FQwen3-0.6B-GGUF", tokens[0])
    print(f"Quantization metadata: {timings[-1]:.3f}s", flush=True)
    print(f"HTTP requests={len(timings)} median={statistics.median(timings):.3f}s max={max(timings):.3f}s", flush=True)
    print("PASS: login, account setup, parallel saved threads, message persistence, isolation and quantization metadata", flush=True)


if __name__ == "__main__":
    main()
