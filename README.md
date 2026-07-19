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
caddytail run <hostname> <app_ref> [--funnel] [--tailnet] [--debug] [--env K=V]

# Production — install as systemd service + tail logs
caddytail install <hostname> <app_ref> [--funnel] [--tailnet] [--no-start] [--env K=V]

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
