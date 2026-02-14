"""
Example bare WSGI application using caddytail.

Run with: caddytail run myapp examples.wsgi_app:app

No extra dependencies required beyond caddytail.
"""

from caddytail import get_user, static


def app(environ, start_response):
    """Minimal WSGI application."""
    path = environ.get("PATH_INFO", "/")

    if path == "/":
        user = get_user(environ)
        if user:
            body = (
                f"Hello, {user.name}!\n"
                f"Login: {user.login}\n"
                f"Profile pic: {user.profile_pic}\n"
            )
        else:
            body = "Not authenticated\n"
        status = "200 OK"

    elif path == "/api/me":
        user = get_user(environ)
        if user:
            import json
            body = json.dumps(user.to_dict())
        else:
            body = '{"error": "Not authenticated"}'
            status = "401 Unauthorized"
            start_response(status, [("Content-Type", "application/json")])
            return [body.encode()]
        status = "200 OK"
        start_response(status, [("Content-Type", "application/json")])
        return [body.encode()]

    else:
        status = "404 Not Found"
        body = "Not found\n"

    start_response(status, [("Content-Type", "text/plain")])
    return [body.encode()]


# Serve static files via Caddy (picked up by the runner automatically)
static(app, "/static/*", "./static")
