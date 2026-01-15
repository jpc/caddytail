# caddytail

Caddy web server with the [Tailscale plugin](https://github.com/tailscale/caddy-tailscale), packaged for pip installation. Includes a Python API for easy integration with Flask and FastAPI applications.

## Installation

```bash
# Installation
pip install caddytail
```

## Python API

caddytail provides a `CaddyTail` class that wraps your Flask or FastAPI application with a Caddy reverse proxy that handles Tailscale authentication automatically.

### Flask Example

```python
from flask import Flask, g
from caddytail import CaddyTail, flask_user_required

app = Flask(__name__)

caddy = CaddyTail(
    app,
    hostname="myapp",           # Your Tailscale hostname
    tailnet="your-tailnet",     # Your tailnet name (without .ts.net)
    static_paths={
        "/static/*": "./static",
    },
)

@app.get("/")
def index():
    user = caddy.get_user()
    if not user:
        return "Not authenticated", 401
    return f"Hello, {user['name']}!"

@app.get("/protected")
@flask_user_required(caddy)
def protected():
    # User is automatically available in g.tailscale_user
    return f"Hello, {g.tailscale_user['name']}!"

if __name__ == "__main__":
    caddy.run()  # Starts both Caddy and Flask
```

### FastAPI Example

```python
from fastapi import FastAPI, Request, Depends
from caddytail import CaddyTail, fastapi_user_dependency

app = FastAPI()

caddy = CaddyTail(
    app,
    hostname="myapp",
    tailnet="your-tailnet",
    static_paths={
        "/static/*": "./static",
    },
)

# Create a dependency for protected routes
get_user = fastapi_user_dependency(caddy)

@app.get("/")
async def index(request: Request):
    user = caddy.get_user(request)
    if not user:
        return {"error": "Not authenticated"}
    return {"message": f"Hello, {user['name']}!"}

@app.get("/protected")
async def protected(user: dict = Depends(get_user)):
    # Automatically returns 401 if not authenticated
    return {"message": f"Hello, {user['name']}!"}

if __name__ == "__main__":
    caddy.run()  # Starts both Caddy and FastAPI
```

### CaddyTail Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `app` | (required) | Flask or FastAPI application instance |
| `hostname` | (required) | Tailscale hostname (e.g., "myapp" -> myapp.tailnet.ts.net) |
| `tailnet` | (required) | Tailscale tailnet name (without .ts.net suffix) |
| `caddy_path` | bundled binary | Path to caddy binary |
| `app_port` | 10800 | Port for the Python app to listen on |
| `caddy_http_port` | 10102 | Port for Caddy's HTTP listener |
| `caddy_admin_port` | 2019 | Port for Caddy's admin API |
| `static_paths` | None | Dict mapping URL patterns to local paths |
| `state_dir` | "./tailscale-state" | Directory for Tailscale state |
| `debug` | False | Enable Caddy debug mode |

### User Information

The `get_user()` method returns a dict with Tailscale user information:

```python
{
    "name": "John Doe",           # Display name
    "login": "john@example.com",  # Login/email
    "profile_pic": "https://..."  # Profile picture URL
}
```

### Static File Serving

Caddy can serve static files directly, bypassing your Python application for better performance:

```python
caddy = CaddyTail(
    app,
    hostname="myapp",
    tailnet="your-tailnet",
    static_paths={
        "/static/*": "./static",
        "/assets/*": "./public/assets",
        "/uploads/*": "/var/www/uploads",
    },
)

# Add paths dynamically
caddy.add_static_path("/media/*", "./media")

# Remove paths
caddy.remove_static_path("/uploads/*")
```

### Running in Background

For advanced use cases, you can start Caddy and your app in background threads:

```python
caddy_proc, app_thread = caddy.run_async()

# Do other things...

# When done
caddy.stop_caddy()
```

## CLI Usage

Once installed, the `caddytail` command is also available on your PATH:

```bash
# Show version
caddytail version

# List modules (should include tailscale)
caddytail list-modules

# Run with a Caddyfile
caddytail run --config /path/to/Caddyfile

# Start in the background
caddytail start

# Stop the background server
caddytail stop
```

## Tailscale Integration

This build of Caddy includes the Tailscale plugin, which allows you to:

- Serve sites directly on your Tailscale network
- Use Tailscale for automatic HTTPS certificates
- Authenticate users via Tailscale identity

### Manual Caddyfile Example

```caddyfile
{
    tailscale
}

myapp.your-tailnet.ts.net {
    tailscale_auth
    reverse_proxy localhost:8000
}
```

See the [caddy-tailscale documentation](https://github.com/tailscale/caddy-tailscale) for more details.

## Supported Platforms

Pre-built wheels are available for:

| Platform | Architecture |
|----------|--------------|
| Linux (glibc) | x86_64, aarch64 |
| macOS | x86_64 (Intel), arm64 (Apple Silicon) |
| Windows | x86_64 |

## Building from Source

If you need to build for a platform not listed above:

```bash
# Clone the repository
git clone https://github.com/yourusername/caddytail
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
