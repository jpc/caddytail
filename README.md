# caddytail

Caddy web server with the [Tailscale plugin](https://github.com/tailscale/caddy-tailscale), packaged for pip installation. Run any Flask or FastAPI app on your tailnet with one command.

## Installation

```bash
pip install caddytail[flask]    # or caddytail[fastapi]
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

Run it on your tailnet:

```bash
caddytail run myapp app:app
```

That's it. Your app is now available at `https://myapp.<tailnet>.ts.net` with Tailscale authentication.

## CLI

Hostname is always the first positional argument:

```bash
# Development — foreground, Ctrl-C kills everything
caddytail run <hostname> <app_ref> [--debug]

# Production — install as systemd service + tail logs
caddytail install <hostname> <app_ref> [--no-start] [--env K=V]

# Service management
caddytail status <hostname>
caddytail logs <hostname> [-n LINES] [-f]
caddytail restart <hostname>
caddytail uninstall <hostname>

# List all installed services
caddytail list

# Raw Caddy pass-through
caddytail caddy [args...]
```

The `<app_ref>` format is `module:variable` (like uvicorn), defaulting the variable to `app`:
- `app:app` — import `app` from `app.py`
- `myproject.main:application` — import `application` from `myproject/main.py`
- `app` — shorthand for `app:app`

### Behavior

- **`run`** — starts Caddy + your app in the foreground. Ctrl-C kills everything.
- **`install`** — writes a systemd unit file (ExecStart = `caddytail run ...`), enables, starts. If stdout is a tty, automatically tails logs. Ctrl-C stops tailing but leaves the service running.
- **`uninstall`** — stops, disables, and removes the unit file.
- **`caddy`** — passes all remaining args to the bundled Caddy binary.

## Python API

### `get_user()`

Returns a `TailscaleUser` with `.name`, `.login`, `.profile_pic`:

```python
from caddytail import get_user

# Flask — no arguments needed
user = get_user()

# FastAPI — pass the Request object
user = get_user(request)

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

## FastAPI Example

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
xcaddy build --with github.com/tailscale/caddy-tailscale --output src/caddytail/bin/caddy

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
