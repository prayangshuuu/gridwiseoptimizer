from functools import wraps
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required

def role_required(allowed_roles):
    """
    Decorator for views that checks whether a user has a particular role,
    raising PermissionDenied if not.
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            if request.user.role in allowed_roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied
        return _wrapped_view
    return decorator

def admin_required(view_func):
    """
    Decorator for views that checks whether a user is an admin.
    """
    return role_required(['admin'])(view_func)
