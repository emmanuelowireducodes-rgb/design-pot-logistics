import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.template import loader
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .models import Organization, Resource
from .social_auth import DesignPotSocialAccountAdapter, DisabledPasswordSignupAdapter
from .social_forms import DesignPotSocialSignupForm

User = get_user_model()


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class SocialOnboardingTests(TestCase):
    def make_form(self, email, role, *, organization="", data=None):
        sociallogin = SocialLogin(user=User(email=email), account=SocialAccount(provider="google"), provider=SimpleNamespace(name="Google"))
        values = {
            "email": email,
            "role": role,
            "first_name": "Ama",
            "last_name": "Mensah",
            "phone": "+233200000001",
            "organization_name": organization,
        }
        values.update(data or {})
        return DesignPotSocialSignupForm(data=values, sociallogin=sociallogin)

    def test_client_google_onboarding_creates_active_scoped_client(self):
        form = self.make_form("client@example.test", "client", organization="Ama Foods")
        self.assertTrue(form.is_valid(), form.errors)
        account = User.objects.create_user("google-client", email="client@example.test")
        with patch("operations.social_forms.SignupForm.save", return_value=account):
            saved = form.save(RequestFactory().post("/accounts/google/signup/"))
        self.assertEqual(saved.role, User.Role.CLIENT)
        self.assertTrue(saved.is_active)
        self.assertTrue(saved.operational_access_approved)
        self.assertEqual(saved.organization.name, "Ama Foods")
        self.assertEqual(saved.phone, "+233200000001")

    def test_rider_driver_google_onboarding_is_pending_and_cannot_choose_admin(self):
        for role in (User.Role.RIDER, User.Role.DRIVER):
            with self.subTest(role=role):
                form = self.make_form(f"{role}@example.test", role)
                self.assertTrue(form.is_valid(), form.errors)
                account = User.objects.create_user(f"google-{role}", email=f"{role}@example.test")
                with patch("operations.social_forms.SignupForm.save", return_value=account):
                    saved = form.save(RequestFactory().post("/accounts/google/signup/"))
                self.assertFalse(saved.operational_access_approved)
                self.assertFalse(Resource.objects.filter(user=saved).exists())
        self.assertNotIn("admin", dict(DesignPotSocialSignupForm.base_fields["role"].choices))
        invalid = self.make_form("admin@example.test", "admin")
        self.assertFalse(invalid.is_valid())

    def test_onboarding_role_cards_are_labeled_accessible_and_never_offer_admin(self):
        sociallogin = SocialLogin(
            user=User(email="new@example.test"),
            account=SocialAccount(provider="google"),
            provider=SimpleNamespace(name="Google"),
        )
        form = DesignPotSocialSignupForm(sociallogin=sociallogin)
        html = loader.get_template("socialaccount/signup.html").render(
            {"form": form, "redirect_field_value": "", "redirect_field_name": "next"}
        )

        for role in ("Client", "Rider", "Driver"):
            self.assertIn(role, html)
        self.assertNotIn('value="admin"', html)
        self.assertIn('type="radio"', html)
        self.assertIn('aria-describedby="id_role_errors"', html)
        self.assertIn("Delivery access starts after Design-Pot Admin approves", html)
        self.assertIn("Send and manage deliveries.", html)
        self.assertIn("Deliver packages by motorbike.", html)
        self.assertIn("Handle deliveries using a vehicle.", html)
        signup_template = (
            Path(__file__).resolve().parents[1] / "templates" / "socialaccount" / "signup.html"
        )
        self.assertIn("{% csrf_token %}", signup_template.read_text(encoding="utf-8"))

    def test_duplicate_email_is_not_automatically_linked(self):
        User.objects.create_user("existing", email="same@example.test")
        form = self.make_form("same@example.test", "client", organization="Example")
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)
        self.assertEqual(User.objects.filter(email__iexact="same@example.test").count(), 1)

    def test_password_signup_closed_and_social_auth_requires_configured_credentials(self):
        self.assertFalse(DisabledPasswordSignupAdapter().is_open_for_signup(None))
        adapter = DesignPotSocialAccountAdapter()
        with override_settings(SOCIALACCOUNT_PROVIDERS={"google": {}}):
            self.assertFalse(adapter.is_open_for_signup(None, None))

    def test_existing_inactive_google_account_is_rejected(self):
        inactive = User.objects.create_user("inactive-google", is_active=False)
        sociallogin = SocialLogin(user=inactive, account=SocialAccount(provider="google"))
        with self.assertRaises(ImmediateHttpResponse) as raised:
            request = RequestFactory().get("/accounts/google/login/callback/")
            request.user = AnonymousUser()
            DesignPotSocialAccountAdapter().pre_social_login(
                request, sociallogin
            )
        response = raised.exception.response
        self.assertEqual(response.status_code, 403)
        self.assertIn("We couldn't sign you in.", response.content.decode())
        self.assertIn("Contact Design-Pot Admin", response.content.decode())

    def test_google_authentication_does_not_elevate_existing_client(self):
        existing = User.objects.create_user(
            "existing-google-client", role=User.Role.CLIENT, email="existing@example.test"
        )
        sociallogin = SocialLogin(
            user=existing, account=SocialAccount(provider="google")
        )

        DesignPotSocialAccountAdapter().pre_social_login(
            RequestFactory().get("/accounts/google/login/callback/"), sociallogin
        )

        existing.refresh_from_db()
        self.assertEqual(existing.role, User.Role.CLIENT)

    def test_admin_approval_links_vehicle_and_unblocks_rider(self):
        admin = User.objects.create_user("approval-admin", role=User.Role.ADMIN)
        rider = User.objects.create_user(
            "pending-rider", role=User.Role.RIDER, operational_access_approved=False,
            first_name="Akua", email="rider@example.test",
        )
        self.client.force_login(rider)
        self.assertRedirects(self.client.get(reverse("home")), reverse("field-pending"))
        self.assertContains(self.client.get(reverse("field-pending")), "before you can access delivery operations")
        self.assertEqual(self.client.get("/field/dashboard/").status_code, 403)
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/admin/fleet/").status_code, 200)
        driver = User.objects.create_user(
            "pending-driver-approved", role=User.Role.DRIVER,
            operational_access_approved=False, first_name="Kojo",
        )
        response = self.client.post(
            reverse("approve-field-account", args=[rider.pk]),
            {"vehicle_name": "Honda bike", "registration": "DP-001"},
        )
        self.assertEqual(response.status_code, 302)
        rider.refresh_from_db()
        self.assertTrue(rider.operational_access_approved)
        self.assertEqual(rider.resource.vehicle_type, "bike")
        self.assertEqual(rider.resource.registration, "DP-001")
        response = self.client.post(
            reverse("approve-field-account", args=[driver.pk]),
            {"vehicle_name": "Delivery van", "registration": "DP-003"},
        )
        self.assertEqual(response.status_code, 302)
        driver.refresh_from_db()
        self.assertTrue(driver.operational_access_approved)
        self.assertEqual(driver.resource.vehicle_type, "car")
        self.client.force_login(rider)
        self.assertEqual(self.client.get(reverse("home")).status_code, 302)
        self.assertEqual(self.client.get("/field/dashboard/").status_code, 200)

    def test_admin_only_approval_and_role_reused_after_login(self):
        client_user = User.objects.create_user("approval-client", role=User.Role.CLIENT)
        rider_user = User.objects.create_user("approval-rider", role=User.Role.RIDER)
        driver_user = User.objects.create_user("approval-driver", role=User.Role.DRIVER)
        pending_driver = User.objects.create_user(
            "pending-driver", role=User.Role.DRIVER, operational_access_approved=False
        )
        for non_admin in (client_user, rider_user, driver_user):
            with self.subTest(role=non_admin.role):
                self.client.force_login(non_admin)
                self.assertEqual(
                    self.client.post(
                        reverse("approve-field-account", args=[pending_driver.pk]),
                        {"vehicle_name": "Van", "registration": "DP-002"},
                    ).status_code,
                    403,
                )
        approved_rider = User.objects.create_user(
            "stored-rider", role=User.Role.RIDER, operational_access_approved=True
        )
        self.client.force_login(approved_rider)
        response = self.client.get(reverse("home"))
        self.assertEqual(getattr(response, "url"), "/field/dashboard/")
        self.assertFalse(Organization.objects.filter(name="Unexpected").exists())


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class GoogleLoginButtonTests(TestCase):
    @override_settings(
        SOCIALACCOUNT_PROVIDERS={
            "google": {
                "APPS": [{
                    "client_id": "safe-test-client-id",
                    "secret": "safe-test-client-secret",
                    "key": "",
                }]
            }
        }
    )
    def test_login_displays_google_button_when_provider_is_configured(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["google_oauth_enabled"])
        self.assertContains(response, "Continue with Google")
        self.assertContains(response, reverse("google_login"))
        self.assertContains(response, "google-g-logo.png")
        self.assertContains(response, 'name="username"')
        self.assertContains(response, "Username or email")
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'aria-describedby="id_username_errors"')
        self.assertNotContains(response, "safe-test-client-secret")
        self.assertNotContains(response, "safe-test-client-id")
        self.assert_google_form_policy(response)

    def test_login_uses_branded_secure_google_and_local_auth_actions(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in to Design-Pot Logistics")
        self.assertContains(response, 'class="local-login-form"')
        self.assertContains(response, 'type="password"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, "Contact your Design-Pot administrator")

    @override_settings(
        SOCIALACCOUNT_PROVIDERS={
            "google": {
                "APPS": [{"client_id": "safe-test-client-id", "secret": "safe-test-client-secret", "key": ""}]
            }
        }
    )
    def test_google_action_remains_visible_on_rate_limit_response(self):
        for _ in range(11):
            response = self.client.post(
                reverse("login"), {"username": "no-such-user", "password": "invalid"}
            )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Continue with Google", status_code=429)

    def assert_google_form_policy(self, response):
        directives = {
            directive.split()[0]: directive.split()[1:]
            for directive in response["Content-Security-Policy"].split(";")
        }
        self.assertEqual(
            directives["form-action"], ["'self'", "https://accounts.google.com"]
        )
        self.assertEqual(directives["default-src"], ["'self'"])
        self.assertEqual(directives["script-src"], ["'self'"])
        self.assertNotIn("*", directives["form-action"])

    @override_settings(
        SOCIALACCOUNT_PROVIDERS={
            "google": {
                "APPS": [{
                    "client_id": "safe-test-client-id",
                    "secret": "safe-test-client-secret",
                    "key": "",
                }]
            }
        }
    )
    def test_google_oauth_post_redirect_and_csp_allow_google_only(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        login = client.get(reverse("login"), HTTP_HOST="127.0.0.1:8010")
        self.assertEqual(login.status_code, 200)
        self.assert_google_form_policy(login)
        self.assertContains(login, '<form method="post"')

        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("google_login"),
            {"csrfmiddlewaretoken": csrf_token},
            HTTP_HOST="127.0.0.1:8010",
            HTTP_REFERER="http://127.0.0.1:8010/login/",
        )

        self.assertEqual(response.status_code, 302)
        redirect = urlsplit(response["Location"])
        self.assertEqual(redirect.scheme, "https")
        self.assertEqual(redirect.hostname, "accounts.google.com")
        self.assert_google_form_policy(response)

    @override_settings(SOCIALACCOUNT_PROVIDERS={"google": {}})
    def test_login_hides_google_button_when_provider_is_not_configured(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["google_oauth_enabled"])
        self.assertNotContains(response, "Continue with Google")
        self.assertContains(response, "Sign in")


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class BrandingManifestTests(TestCase):
    def test_manifest_has_separate_any_and_safe_maskable_icons(self):
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        icons = {icon["purpose"]: icon for icon in manifest["icons"]}
        self.assertEqual(icons["any"]["sizes"], "512x512")
        self.assertEqual(icons["maskable"]["src"], "/static/icon-maskable-512.png")
        self.assertEqual(icons["maskable"]["sizes"], "512x512")
        self.assertEqual(manifest["theme_color"], "#00634b")

    def test_brand_assets_exist_at_required_dimensions_and_public_login_references_them(self):
        required = {
            "favicon.png": (48, 48),
            "icon-192.png": (192, 192),
            "icon-512.png": (512, 512),
            "icon-maskable-512.png": (512, 512),
            "apple-touch-icon.png": (180, 180),
            "design-pot-logo.png": (1254, 1254),
            "google-g-logo.png": (200, 204),
        }
        for asset, dimensions in required.items():
            path = finders.find(asset)
            assert path is not None
            with open(path, "rb") as image:
                self.assertEqual(image.read(8), b"\x89PNG\r\n\x1a\n")
                image.read(8)  # IHDR chunk header
                width, height = struct.unpack(">II", image.read(8))
            self.assertEqual((width, height), dimensions)
        page = self.client.get(reverse("login"))
        self.assertContains(page, "apple-touch-icon.png")
        self.assertContains(page, "favicon.png")
        self.assertContains(page, "design-pot-logo.png")

    def test_google_state_and_pkce_security_defaults(self):
        from django.conf import settings

        self.assertFalse(settings.SOCIALACCOUNT_LOGIN_ON_GET)
        self.assertFalse(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION)
        self.assertFalse(settings.SOCIALACCOUNT_STORE_TOKENS)
        self.assertTrue(settings.SOCIALACCOUNT_PROVIDERS["google"]["OAUTH_PKCE_ENABLED"])
