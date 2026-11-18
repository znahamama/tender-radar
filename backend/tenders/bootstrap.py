"""Create or update the admin superuser from a password the caller never sees.

Used by the worker's {"task": "create_admin", "username": ...} (tenders.worker), which reads
the password from a one-time SSM SecureString. Nothing here logs, prints or raises the
password: Django's validator messages describe the problem ("too short", "too common"),
never the value.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import transaction


class BootstrapError(Exception):
    """The username or password was refused; the message is safe to log."""


def bootstrap_admin(username: str, password: str) -> str:
    """Create ``username`` as an active superuser, or make an existing user one, with
    ``password``. Returns "created" or "updated". Raises BootstrapError if Django's
    username or password validators refuse it (nothing is written then)."""
    User = get_user_model()
    if not isinstance(username, str) or not username.strip():
        raise BootstrapError("username is missing")
    username = username.strip()
    try:
        UnicodeUsernameValidator()(username)
    except ValidationError as e:
        raise BootstrapError(f"username {username!r} is not valid: {' '.join(e.messages)}") from None
    if len(username) > User._meta.get_field("username").max_length:
        raise BootstrapError("username is too long")
    if not password:
        raise BootstrapError("the bootstrap password is empty")

    with transaction.atomic():
        user = User.objects.select_for_update().filter(username=username).first()
        created = user is None
        if created:
            user = User(username=username)
        try:
            # With the user, so UserAttributeSimilarityValidator compares against the name.
            validate_password(password, user=user)
        except ValidationError as e:
            # `from None`: the original exception's args are messages only, but keep the
            # traceback chain out of the logs anyway.
            raise BootstrapError("the bootstrap password was refused by Django's password "
                                 f"validators: {' '.join(e.messages)}") from None
        user.is_active = user.is_staff = user.is_superuser = True
        user.set_password(password)
        user.save()

    # A password reset is also the way back in after a lockout: clear this name's failures.
    from axes.utils import reset

    reset(username=username)
    return "created" if created else "updated"
