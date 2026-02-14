"""
Example minimal Django WSGI application using caddytail.

Run with: caddytail run myapp examples.django_app:app

Requirements:
    pip install django
"""

import os
import django
from django.conf import settings

# Minimal Django settings — no database, no installed apps beyond the essentials
if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="caddytail-example-not-for-production",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
    )
    django.setup()

from django.http import JsonResponse, HttpResponse
from django.urls import path
from caddytail import get_user


def index(request):
    """Main page showing user info."""
    user = get_user(request.META)
    if user:
        return HttpResponse(
            f"Hello, {user.name}!\nLogin: {user.login}\n",
            content_type="text/plain",
        )
    return HttpResponse("Not authenticated\n", status=401, content_type="text/plain")


def api_me(request):
    """API endpoint returning user info as JSON."""
    user = get_user(request.META)
    if user:
        return JsonResponse(user.to_dict())
    return JsonResponse({"error": "Not authenticated"}, status=401)


urlpatterns = [
    path("", index),
    path("api/me", api_me),
]

# Django's WSGI application
from django.core.wsgi import get_wsgi_application
app = get_wsgi_application()
