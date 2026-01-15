# caddytail

Caddy web server with the [Tailscale plugin](https://github.com/tailscale/caddy-tailscale), packaged for pip installation.

## Installation

```bash
pip install caddytail
```

## Usage

Once installed, the `caddytail` command is available on your PATH:

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

### Example Caddyfile

```caddyfile
{
    # Use Tailscale for serving
    tailscale
}

# Serve on your Tailscale network
my-server.tail-scale.ts.net {
    respond "Hello from Tailscale!"
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

If you need to build for a platform not listed above, you can build locally:

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
