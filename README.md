# caddytail

Caddy web server with the [Tailscale plugin](https://github.com/tailscale/caddy-tailscale), packaged for pip installation. Run any Python web app on your tailnet with one command — Flask, FastAPI, Django, or any WSGI/ASGI callable.

## Installation

```bash
pip install caddytail
```

## Quick Start

Write a normal Flask app — no CaddyTail-specific setup needed:

```python
# app.py
from flask import Flask
from caddytail import get_user

app = Flask(__name__)

@app.get("/")
def index():
    user = get_user()
    return f"Hello, {user.name}!"
```

Or use any WSGI callable — no framework required:

```python
# app.py
from caddytail import get_user

def app(environ, start_response):
    user = get_user(environ)
    body = f"Hello, {user.name}!" if user else "Not authenticated"
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [body.encode()]
```

Run it on your tailnet:

```bash
caddytail run myapp app:app
```

That's it. Your app is now available at `https://myapp.<tailnet>.ts.net` with Tailscale authentication.

## CLI

Hostname is always the first positional argument:

```bash
# Development — foreground, Ctrl-C kills everything
caddytail run <hostname> <app_ref> [--funnel] [--tailnet] [--server dev|gunicorn] [--workers N] [--threads N] [--debug] [--env K=V]

# Production — install as systemd service + tail logs
caddytail install <hostname> <app_ref> [--funnel] [--tailnet] [--server dev|gunicorn] [--workers N] [--threads N] [--no-start] [--env K=V]

# Service management
caddytail status <hostname>
caddytail logs <hostname> [-n LINES] [-f]
caddytail restart <hostname>
caddytail uninstall <hostname>

# List all installed services
caddytail list

# Pre-provision Tailscale authentication
caddytail login <hostname> [--auth-key <key>]

# Raw Caddy pass-through
caddytail caddy [args...]
```

The `<app_ref>` format is `module:variable` (like uvicorn), defaulting the variable to `app`:
- `app:app` — import `app` from `app.py`
- `myproject.main:application` — import `application` from `myproject/main.py`
- `app` — shorthand for `app:app`

### Static File Server

A built-in WSGI file server is included. No code needed — just point it at a directory:

```bash
# Foreground
STATIC_PATH=./public caddytail run myfiles caddytail.fileserver:app

# Install as a systemd service
caddytail install myfiles caddytail.fileserver:app --env STATIC_PATH=/srv/files
```

`STATIC_PATH` defaults to `.` (the working directory). The server provides directory listings and serves `index.html` when present.

### Public exposure (Tailscale Funnel)

By default an app is private to your tailnet and every request is
Tailscale-authenticated. To expose it to the **public internet** via
[Tailscale Funnel], add `--funnel`:

```bash
# Public only — anyone on the internet, no Tailscale auth
caddytail run myapp app:app --funnel

# Public funnel AND a private authenticated tailnet listener
caddytail run myapp app:app --funnel --tailnet
```

- `--tailnet` is the implicit default. As soon as you pass `--funnel` it is no
  longer added automatically — pass `--tailnet` explicitly to get both.
- `--funnel` alone serves on `https://<hostname>.<tailnet>.ts.net` (port 443).
- `--funnel --tailnet` serves the **public** funnel on `:443` and a separate
  **authenticated** tailnet listener on `:8443`, both proxying the same app.

Funnel must be [enabled in your tailnet ACL] and on the node, and only supports
ports **443, 8443, 10000**.

> **Identity & security.** Funnel traffic is anonymous — it carries no
> Tailscale identity, so `get_user()` returns `None` on a funnel listener, and
> caddytail strips any inbound `Tailscale-User-*` headers so a public client
> cannot spoof a user. Gate anything sensitive behind the authenticated
> `--tailnet` listener, not the funnel one.

[Tailscale Funnel]: https://tailscale.com/kb/1223/funnel
[enabled in your tailnet ACL]: https://tailscale.com/kb/1223/funnel#allow-funnel-access-in-acl

### Production app server (gunicorn)

By default caddytail runs your app on its framework's built-in **development**
server (Werkzeug for Flask, `wsgiref`, or `uvicorn`). That's fine for the
tailnet, but not for public funnel traffic. Add `--server gunicorn` to run a
hardened [gunicorn] server instead:

```bash
pip install 'caddytail[gunicorn]'
caddytail run hsv survey.server.app:app --funnel --server gunicorn
```

caddytail spawns and supervises gunicorn as a subprocess bound to the internal
port, alongside Caddy. Defaults:

- **WSGI** (Flask etc.) → `gunicorn -w 1 -k gthread --threads 8`. This mirrors
  the threaded dev server — a single process with N threads — so apps with
  in-memory state keep working. Tune with `--workers N` / `--threads N`.
- **ASGI** (FastAPI etc.) → `gunicorn -k uvicorn.workers.UvicornWorker -w N`.

> Passing `--workers N > 1` runs multiple **processes**. Only do that if your
> app is stateless or uses shared storage (redis/db) — otherwise per-process
> in-memory state will diverge.

`ProxyFix` / Tailscale middleware are re-applied inside the gunicorn workers
(via the `caddytail.wsgi` entrypoint), so `url_for(_external=True)`, redirects,
and `get_user()` behave exactly as under the dev server.

[gunicorn]: https://gunicorn.org/

### Behavior

- **`run`** — starts Caddy + your app in the foreground. Ctrl-C kills everything. The framework is auto-detected: Flask and FastAPI get framework-specific middleware; generic WSGI apps are served with `wsgiref`; generic ASGI apps are served with `uvicorn`.
- **`install`** — writes a systemd unit file (ExecStart = `caddytail run ...`), enables, starts. If stdout is a tty, automatically tails logs. Ctrl-C stops tailing but leaves the service running.
- **`uninstall`** — stops, disables, and removes the unit file.
- **`login`** — authenticates a Tailscale node ahead of time. If already authenticated, returns immediately. Useful for headless provisioning with `--auth-key`.
- **`caddy`** — passes all remaining args to the bundled Caddy binary.

## Python API

### `get_user()`

Returns a `TailscaleUser` with `.name`, `.login`, `.profile_pic`:

```python
from caddytail import get_user

# Flask — no arguments needed (uses flask.request automatically)
user = get_user()

# FastAPI / Starlette — pass the Request object
user = get_user(request)

# WSGI — pass the environ dict
user = get_user(environ)

# Django — pass request.META
user = get_user(request.META)

if user:
    print(user.name)        # "John Doe"
    print(user.login)       # "john@example.com"
    print(user.profile_pic) # "https://..."
```

### `get_participant()`

`get_user()` covers staff on the tailnet. A participant is someone who comes in
over the public funnel: they have no Tailscale identity, so the app that admits
them (the *issuer*) signs a short-lived token saying who they are, and every
other app can recognise them from it.

```python
from caddytail import get_participant

@app.get("/task")
def task():
    p = get_participant()          # Flask; pass the Request (FastAPI) or environ (WSGI)
    if p is None:
        abort(403)
    return f"{p.id} in {p.study}, block {p.block}, test={p.test}"
```

Tell caddytail where the issuer publishes its public keys:

```bash
caddytail run myapp app:app --funnel --env CADDYTAIL_PARTICIPANT_KEYS=https://issuer.example/.well-known/participant-keys.json
```

caddytail then verifies the token on each request and hands the app
`Participant-Id`, `-Kind`, `-Study`, `-Cohort`, `-Block`, `-Test`, `-Issuer`,
`-Expires` and `-Claims` (all claims as JSON) request headers. Headers of those
names sent by a client are always removed, so an app can trust them.

| Setting | Meaning |
|---|---|
| `CADDYTAIL_PARTICIPANT_KEYS` | an https address serving `{"keys": ["k4.public...."]}`, and/or the keys themselves, comma-separated. Unset: nothing is verified |
| `CADDYTAIL_PARTICIPANT_ISSUER` | only accept tokens whose `iss` is exactly this |
| `CADDYTAIL_PARTICIPANT_AUDIENCE` | only accept tokens whose `aud` is exactly this |
| `CADDYTAIL_PARTICIPANT_HEADERS` | more request headers to read the token from |

The client sends the token as `Participant-Token: <token>` or
`Authorization: Bearer <token>`.

- **Format.** PASETO `v4.public`: an Ed25519 signature (libsodium, through
  PyNaCl; `cryptography` also works) over a JSON payload. Apps hold only public
  keys, so they can check a token but not make one. Install with
  `pip install 'caddytail[participants]'`.
- **Keys.** An address is fetched on first use, again every hour, and sooner
  when a token names a key not seen before, so a new key at the issuer is
  picked up without a restart. If a fetch fails the known keys stay in use.
  Whoever controls that address decides who is a participant, which is why it
  must be https.
- **Limits.** A token is valid until it expires and cannot be revoked; anything
  that needs counting ("one submission") stays with the issuer. Any app that
  receives a token could present it to another app; set an audience where that
  matters.
- **Where it runs.** In caddytail's Python layer around the app, not in the
  Caddy binary, so it applies to every app started with `caddytail run`.

An issuer uses the same module:

```python
from caddytail import participant

key = participant.generate_key()                 # "k4.secret....", keep private
token = participant.mint(key, {"sub": pid, "kind": "prolific", "study": "s1"}, ttl_s=4 * 3600)
participant.public_key(key)                      # "k4.public....", publish in {"keys": [...]}
```

### `login_required`

Works as both a Flask decorator and a FastAPI `Depends()` target:

```python
from caddytail import login_required

# Flask
@app.get("/secret")
@login_required
def secret():
    user = get_user()
    return f"Hello, {user.name}!"

# FastAPI
@app.get("/secret")
async def secret(user=Depends(login_required)):
    return {"message": f"Hello, {user.name}!"}
```

### `static()`

Register static file paths to be served directly by Caddy:

```python
from caddytail import static

static(app, "/assets/*", "./static")
static(app, "/uploads/*", "/var/www/uploads")
```

The runner picks these up automatically when starting Caddy.

### `CaddyTail` class

For programmatic use (most users should use the CLI runner instead):

```python
from caddytail import CaddyTail

caddy = CaddyTail(app, "myapp", debug=True)
caddy.run()
```

All ports are auto-allocated. No conflicts when running multiple apps.

The `funnel` / `tailnet_listener` keyword arguments mirror the `--funnel` /
`--tailnet` CLI flags:

```python
CaddyTail(app, "myapp")                                # tailnet only (default)
CaddyTail(app, "myapp", funnel=True)                   # public funnel only
CaddyTail(app, "myapp", funnel=True, tailnet_listener=True)  # public + authenticated
```

For full control, pass an explicit list of `Exposure` objects (or register them
on the app with `expose()`, which the runner picks up):

```python
from caddytail import CaddyTail, Exposure, expose

CaddyTail(app, "myapp", exposures=[
    Exposure("funnel-only", port=443),   # public, unauthenticated
    Exposure("tailnet", port=8443),      # private, authenticated
])

# …or declare it from app code, no CLI flags needed:
expose(app, "funnel-only", port=443)
expose(app, "tailnet", port=8443)
```

## Framework Examples

### FastAPI

```python
from fastapi import FastAPI, Request, Depends
from caddytail import get_user, login_required

app = FastAPI()

@app.get("/")
async def index(request: Request):
    user = get_user(request)
    return {"message": f"Hello, {user.name}!"}

@app.get("/protected")
async def protected(user=Depends(login_required)):
    return {"message": f"Hello, {user.name}!"}
```

### Django

```python
# views.py
from django.http import HttpResponse
from caddytail import get_user

def index(request):
    user = get_user(request.META)
    return HttpResponse(f"Hello, {user.name}!")
```

### Bare WSGI

```python
from caddytail import get_user

def app(environ, start_response):
    user = get_user(environ)
    body = f"Hello, {user.name}!" if user else "Not authenticated"
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [body.encode()]
```

### ASGI

```python
from caddytail import get_user

async def app(scope, receive, send):
    # For ASGI apps, extract headers from the scope manually
    ...
```

All examples are run the same way:

```bash
caddytail run myapp myproject:app
```

## Supported Platforms

Pre-built wheels are available for:

| Platform | Architecture |
|----------|--------------|
| Linux (glibc) | x86_64, aarch64 |
| macOS | x86_64 (Intel), arm64 (Apple Silicon) |
| Windows | x86_64 |

## Building from Source

```bash
git clone https://github.com/jpc/caddytail
cd caddytail

# Install Go and xcaddy
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest

# Build caddy with the tailscale plugin
xcaddy build --with github.com/tailscale/caddy-tailscale=github.com/jpc/caddy-tailscale@main --output src/caddytail/bin/caddy

# Build the wheel
pip install build
python -m build --wheel
```

## License

This project packages Caddy (Apache 2.0 License) with the Tailscale plugin (BSD 3-Clause License).

## Links

- [Caddy](https://caddyserver.com/)
- [Tailscale](https://tailscale.com/)
- [caddy-tailscale plugin](https://github.com/tailscale/caddy-tailscale)
- [xcaddy](https://github.com/caddyserver/xcaddy)
