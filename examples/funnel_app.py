"""
Example app exposed both publicly (Tailscale Funnel) and privately (tailnet).

Run with both listeners:
    caddytail run myapp examples.funnel_app:app --funnel --tailnet

This serves:
    - a PUBLIC funnel on https://myapp.<tailnet>.ts.net        (port 443)
    - a PRIVATE authenticated listener on ...ts.net:8443       (port 8443)

Both proxy this same app. Public funnel traffic is anonymous: get_user()
returns None, so gate anything sensitive on the authenticated listener.

Alternatively, declare the exposures from code (no CLI flags needed):
    caddytail run myapp examples.funnel_app:app

Requirements:
    pip install caddytail[flask]
"""

from flask import Flask
from caddytail import get_user, expose

app = Flask(__name__)

# Declare exposures here so `caddytail run` needs no --funnel/--tailnet flags.
# (Flags, if passed, take precedence over these registrations.)
expose(app, "funnel-only", port=443)  # public internet
expose(app, "tailnet", port=8443)     # authenticated tailnet


@app.get("/")
def index():
    """Public landing page — reachable by anyone on the internet."""
    user = get_user()  # None over Funnel; a TailscaleUser on the tailnet listener
    if user:
        return f"Hello, {user.name}! You're authenticated on the tailnet."
    return "Hello, internet! This is the public funnel (no Tailscale identity)."


@app.get("/whoami")
def whoami():
    """Show whether the request arrived authenticated."""
    user = get_user()
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, **user.to_dict()}
