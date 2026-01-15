"""
Example FastAPI application using caddytail.

Run with: python fastapi_app.py

Requirements:
    pip install caddytail[fastapi]
"""

from fastapi import FastAPI, Request, Depends
from caddytail import CaddyTail, fastapi_user_dependency

app = FastAPI(title="Example FastAPI App with Tailscale Auth")

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

# Create a dependency for routes that need user auth
get_user = fastapi_user_dependency(caddy)


@app.get("/")
async def index(request: Request):
    """Main page showing user info."""
    user = caddy.get_user(request)
    if not user:
        return {"error": "Not authenticated"}
    
    return {
        "message": f"Hello, {user['name']}!",
        "user": user,
    }


@app.get("/api/me")
async def api_me(request: Request):
    """API endpoint returning user info as JSON."""
    user = caddy.get_user(request)
    if not user:
        return {"error": "Not authenticated"}
    return user


@app.get("/protected")
async def protected(user: dict = Depends(get_user)):
    """Example using dependency injection - automatically 401s if not authenticated."""
    return {"message": f"Hello, {user['name']}! This is a protected route.", "user": user}


@app.get("/via-state")
async def via_state(request: Request):
    """Access user via request.state (set by middleware)."""
    user = request.state.tailscale_user
    if not user:
        return {"error": "Not authenticated"}
    return {"message": f"Hello from state, {user.name}!", "login": user.login}


if __name__ == "__main__":
    # This starts both Caddy and FastAPI (via uvicorn)
    caddy.run()
