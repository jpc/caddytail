"""
Example FastAPI application using caddytail.

Run with: caddytail run myapp examples.fastapi_app:app

Requirements:
    pip install caddytail[fastapi]
"""

from fastapi import FastAPI, Request, Depends
from caddytail import get_user, login_required

app = FastAPI(title="Example FastAPI App with Tailscale Auth")


@app.get("/")
async def index(request: Request):
    """Main page showing user info."""
    user = get_user(request)
    if not user:
        return {"error": "Not authenticated"}

    return {
        "message": f"Hello, {user.name}!",
        "user": user.to_dict(),
    }


@app.get("/api/me")
async def api_me(request: Request):
    """API endpoint returning user info as JSON."""
    user = get_user(request)
    if not user:
        return {"error": "Not authenticated"}
    return user.to_dict()


@app.get("/protected")
async def protected(user=Depends(login_required)):
    """Example using dependency injection — automatically 401s if not authenticated."""
    return {"message": f"Hello, {user.name}! This is a protected route."}
