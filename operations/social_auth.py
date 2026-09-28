from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpResponseForbidden


def google_oauth_configured():
    """Return whether Google OAuth credentials are configured in Django settings."""
    from django.conf import settings

    return bool(settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APPS"))


class DisabledPasswordSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False


class DesignPotSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return google_oauth_configured()

    def pre_social_login(self, request, sociallogin):
        user: AbstractBaseUser = sociallogin.user
        if sociallogin.is_existing and not user.is_active:
            raise ImmediateHttpResponse(
                HttpResponseForbidden("This Design-Pot account is inactive. Contact an administrator.")
            )
