"""
Example Flask application using caddytail.

Run with: caddytail run myapp examples.flask_app:app

Requirements:
    pip install caddytail[flask]
"""

from flask import Flask
from caddytail import get_user, login_required, static

app = Flask(__name__)

# Serve static files via Caddy (picked up by the runner automatically)
static(app, "/static/*", "./static")


@app.get("/")
def index():
    """Main page showing user info."""
    user = get_user()
    if not user:
        return "Not authenticated", 401

    return f"""
    <html>
    <body>
        <div style="position: absolute; top: 5px; left: 5px; width: 250px; height: 50px; background: #eee;">
            <img style="border-radius: 5px; margin: 5px; float: left;" width="40" height="40" src="{user.profile_pic}" />
            Hello, {user.name}!
        </div>
        <h1>Welcome to the Example App</h1>
        <p>Your login: {user.login}</p>
    </body>
    </html>
    """


@app.get("/api/me")
def api_me():
    """API endpoint returning user info as JSON."""
    user = get_user()
    if not user:
        return {"error": "Not authenticated"}, 401
    return user.to_dict()


@app.get("/protected")
@login_required
def protected():
    """Example using the login_required decorator."""
    user = get_user()
    return f"Hello, {user.name}! This is a protected route."
