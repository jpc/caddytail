"""
Example Flask application using caddytail.

Run with: python flask_app.py

Requirements:
    pip install caddytail[flask]
"""

from flask import Flask, g
from caddytail import CaddyTail, flask_user_required

app = Flask(__name__)

# Configure CaddyTail wrapper
caddy = CaddyTail(
    app,
    hostname="myapp",           # Your Tailscale hostname
    tailnet="your-tailnet",     # Your tailnet name (without .ts.net)
    app_port=10800,
    static_paths={
        "/static/*": "./static",  # Serve static files from ./static at /static/*
    },
    debug=True,
)


@app.get("/")
def index():
    """Main page showing user info."""
    user = caddy.get_user()
    if not user:
        return "Not authenticated", 401
    
    return f"""
    <html>
    <body>
        <div style="position: absolute; top: 5px; left: 5px; width: 250px; height: 50px; background: #eee;">
            <img style="border-radius: 5px; margin: 5px; float: left;" width="40" height="40" src="{user['profile_pic']}" />
            Hello, {user['name']}!
        </div>
        <h1>Welcome to the Example App</h1>
        <p>Your login: {user['login']}</p>
    </body>
    </html>
    """


@app.get("/api/me")
def api_me():
    """API endpoint returning user info as JSON."""
    user = caddy.get_user()
    if not user:
        return {"error": "Not authenticated"}, 401
    return user


@app.get("/protected")
@flask_user_required(caddy)
def protected():
    """Example using the decorator - user is in g.tailscale_user."""
    return f"Hello, {g.tailscale_user['name']}! This is a protected route."


if __name__ == "__main__":
    # This starts both Caddy and Flask
    caddy.run(debug=True)
