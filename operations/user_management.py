from __future__ import annotations

from typing import cast

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import SESSION_KEY, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.sessions.models import Session
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .access import roles_required
from .models import DomainEvent, User


def _record_user_event(*, actor: User, target: User, kind: str) -> None:
    DomainEvent.objects.create(
        actor=actor,
        kind=kind,
        payload={"target_user_id": target.pk, "target_username": target.username},
    )


def _invalidate_user_sessions(user: User, *, preserve_session_key: str | None = None) -> None:
    """Delete only live database sessions authenticated as this user."""
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        return

    session_keys = []
    sessions = Session.objects.filter(expire_date__gt=timezone.now()).only(
        "session_key", "session_data"
    )
    for session in sessions.iterator():
        data = session.get_decoded()
        if (
            data.get(SESSION_KEY) == str(user.pk)
            and session.session_key != preserve_session_key
        ):
            session_keys.append(session.session_key)
    if session_keys:
        Session.objects.filter(session_key__in=session_keys).delete()


@roles_required(User.Role.ADMIN)
def user_list(request: HttpRequest) -> HttpResponse:
    users = (
        User.objects.defer("password")
        .annotate(
            has_local_password=~Q(password__startswith="!"),
            google_linked=Exists(
                SocialAccount.objects.filter(user_id=OuterRef("pk"), provider="google")
            ),
        )
        .order_by("username")
    )
    page = Paginator(users, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "operations/user_list.html",
        {"title": "User Management", "section": "users", "page_obj": page},
    )


@roles_required(User.Role.ADMIN)
@require_http_methods(["GET", "POST"])
def reset_user_password(request: HttpRequest, user_id: int) -> HttpResponse:
    target = get_object_or_404(User, pk=user_id)
    if target.pk == request.user.pk:
        messages.error(request, "Use Change My Password to update your own password.")
        return redirect("user-management")

    if not target.has_usable_password():
        return render(
            request,
            "operations/user_password_reset.html",
            {
                "title": "Reset Password",
                "target": target,
                "form": None,
                "google_only": SocialAccount.objects.filter(user=target, provider="google").exists(),
            },
        )

    form = SetPasswordForm(target, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            target = get_object_or_404(User.objects.select_for_update(), pk=user_id)
            form = SetPasswordForm(target, request.POST)
            if form.is_valid():
                form.save()
                _invalidate_user_sessions(target)
                _record_user_event(
                    actor=cast(User, request.user), target=target, kind="USER_PASSWORD_RESET"
                )
            else:
                return render(
                    request,
                    "operations/user_password_reset.html",
                    {"title": "Reset Password", "target": target, "form": form},
                )
        messages.success(request, f"Password updated for {target.username}.")
        return redirect("user-management")

    return render(
        request,
        "operations/user_password_reset.html",
        {"title": "Reset Password", "target": target, "form": form},
    )


@roles_required(User.Role.ADMIN)
@require_POST
def toggle_user_status(request: HttpRequest, user_id: int) -> HttpResponse:
    if user_id == request.user.pk:
        messages.error(request, "You cannot disable your own account.")
        return redirect("user-management")

    with transaction.atomic():
        target = get_object_or_404(User.objects.select_for_update(), pk=user_id)
        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        if not target.is_active:
            _invalidate_user_sessions(target)
        kind = "USER_ENABLED" if target.is_active else "USER_DISABLED"
        _record_user_event(actor=cast(User, request.user), target=target, kind=kind)

    state = "enabled" if target.is_active else "disabled"
    messages.success(request, f"{target.username} has been {state}.")
    return redirect("user-management")


@roles_required(User.Role.ADMIN)
@require_http_methods(["GET", "POST"])
def change_own_password(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    if not user.has_usable_password():
        return render(
            request,
            "operations/user_password_change.html",
            {"title": "Change My Password", "form": None, "google_only": True},
        )

    form = PasswordChangeForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            update_session_auth_hash(request, user)
            _record_user_event(actor=user, target=user, kind="USER_PASSWORD_CHANGED")
        messages.success(request, "Your password has been changed.")
        return redirect("admin-password-change")

    return render(
        request,
        "operations/user_password_change.html",
        {"title": "Change My Password", "form": form, "google_only": False},
    )
