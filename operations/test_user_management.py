from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import SESSION_KEY
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from .models import DomainEvent, User
from .social_auth import DesignPotSocialAccountAdapter


class UserManagementTests(TestCase):
    admin: User
    target: User

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "users-admin", password="Admin-current-pass-493!", role=User.Role.ADMIN
        )
        cls.target = User.objects.create_user(
            "managed-client",
            password="Target-current-pass-493!",
            role=User.Role.CLIENT,
            email="client@example.test",
            first_name="Ama",
            last_name="Mensah",
        )

    def admin_login(self):
        self.client.force_login(self.admin)

    def test_admin_can_view_user_management_without_password_data(self):
        self.admin_login()
        response = self.client.get(reverse("user-management"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "User Management")
        self.assertContains(response, "Ama Mensah")
        self.assertContains(response, "client@example.test")
        self.assertContains(response, "Disable User")
        self.assertContains(response, "Disable this user?")
        self.assertContains(response, 'class="table-scroll mobile-card-table user-management-table"')
        self.assertContains(response, "Reset Password")
        self.assertContains(response, reverse("admin-password-change"))
        self.assertNotContains(response, "pbkdf2_sha256$")
        self.assertNotContains(response, "Target-current-pass-493!")

    def test_all_non_admin_roles_and_anonymous_are_denied(self):
        pending_rider = User.objects.create_user(
            "pending-rider-user-admin", role=User.Role.RIDER, operational_access_approved=False
        )
        pending_driver = User.objects.create_user(
            "pending-driver-user-admin", role=User.Role.DRIVER, operational_access_approved=False
        )
        users = [
            User.objects.create_user("user-client-admin", role=User.Role.CLIENT),
            User.objects.create_user("user-rider-admin", role=User.Role.RIDER),
            User.objects.create_user("user-driver-admin", role=User.Role.DRIVER),
            pending_rider,
            pending_driver,
        ]
        urls = [
            ("get", reverse("user-management")),
            ("get", reverse("user-password-reset", args=[self.target.pk])),
            ("post", reverse("user-status-toggle", args=[self.target.pk])),
            ("get", reverse("admin-password-change")),
        ]
        for user in [None, *users]:
            if user is None:
                self.client.logout()
            else:
                self.client.force_login(user)
            for method, url in urls:
                with self.subTest(role=getattr(user, "role", "anonymous"), method=method, url=url):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 302 if user is None else 403)

    def test_admin_can_disable_and_enable_user_with_audit_and_no_role_change(self):
        history = DomainEvent.objects.create(actor=self.target, kind="fixture", payload={"kept": True})
        target_session = Client()
        target_session.force_login(self.target)
        self.admin_login()
        status_url = reverse("user-status-toggle", args=[self.target.pk])

        response = self.client.post(status_url)
        self.assertRedirects(response, reverse("user-management"))
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertEqual(self.target.role, User.Role.CLIENT)
        self.assertTrue(DomainEvent.objects.filter(pk=history.pk).exists())
        disabled_event = DomainEvent.objects.get(kind="USER_DISABLED")
        self.assertEqual(disabled_event.actor, self.admin)
        self.assertEqual(disabled_event.payload["target_user_id"], self.target.pk)
        self.assertEqual(disabled_event.payload["target_username"], self.target.username)
        self.assertEqual(target_session.get("/client/settings/").status_code, 302)
        self.assertNotIn(SESSION_KEY, target_session.session)

        self.client.post(status_url)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertEqual(self.target.role, User.Role.CLIENT)
        enabled_event = DomainEvent.objects.get(kind="USER_ENABLED")
        self.assertEqual(enabled_event.actor, self.admin)
        self.assertEqual(enabled_event.payload["target_user_id"], self.target.pk)

    def test_status_change_is_post_only_and_self_disable_is_blocked_server_side(self):
        self.admin_login()
        status_url = reverse("user-status-toggle", args=[self.target.pk])
        self.assertEqual(self.client.get(status_url).status_code, 405)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertFalse(DomainEvent.objects.filter(kind__in=["USER_ENABLED", "USER_DISABLED"]).exists())

        response = self.client.post(reverse("user-status-toggle", args=[self.admin.pk]))
        self.assertRedirects(response, reverse("user-management"))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertFalse(DomainEvent.objects.filter(kind="USER_DISABLED").exists())

    def test_csrf_protection_rejects_status_post_without_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(reverse("user-status-toggle", args=[self.target.pk]))
        self.assertEqual(response.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_disabled_user_password_login_is_blocked_and_google_login_is_blocked(self):
        self.target.is_active = False
        self.target.save(update_fields=["is_active"])
        login_client = Client()
        response = login_client.post(
            reverse("login"),
            {"username": self.target.username, "password": "Target-current-pass-493!"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, login_client.session)

        SocialAccount.objects.create(user=self.target, provider="google", uid="managed-client-google")
        sociallogin = SocialLogin(
            user=self.target, account=SocialAccount(provider="google", uid="managed-client-google")
        )
        with self.assertRaises(ImmediateHttpResponse) as raised:
            DesignPotSocialAccountAdapter().pre_social_login(
                RequestFactory().get("/accounts/google/login/callback/"), sociallogin
            )
        self.assertEqual(raised.exception.response.status_code, 403)

    def test_admin_can_reset_another_local_password_and_target_sessions_expire(self):
        target_session = Client()
        target_session.force_login(self.target)
        self.admin_login()
        url = reverse("user-password-reset", args=[self.target.pk])
        password_form = self.client.get(url)
        self.assertEqual(password_form.status_code, 200)
        self.assertContains(password_form, 'type="password"')
        new_password = "New-temporary-password-927!"
        response = self.client.post(
            url,
            {"new_password1": new_password, "new_password2": new_password},
        )
        self.assertRedirects(response, reverse("user-management"))

        self.target.refresh_from_db()
        self.assertEqual(self.target.role, User.Role.CLIENT)
        self.assertFalse(self.target.check_password("Target-current-pass-493!"))
        self.assertTrue(self.target.check_password(new_password))
        password_login = Client()
        login_response = password_login.post(
            reverse("login"),
            {"username": self.target.username, "password": new_password},
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertIn(SESSION_KEY, password_login.session)
        self.assertNotIn(SESSION_KEY, target_session.session)
        target_response = target_session.get("/client/settings/")
        self.assertEqual(target_response.status_code, 302)
        self.assertNotIn(SESSION_KEY, target_session.session)

        event = DomainEvent.objects.get(kind="USER_PASSWORD_RESET")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.payload["target_user_id"], self.target.pk)
        self.assertNotIn(new_password, str(event.payload))
        self.assertNotIn(new_password, str(event))

    def test_password_validation_is_enforced_and_no_audit_written_on_failure(self):
        self.admin_login()
        response = self.client.post(
            reverse("user-password-reset", args=[self.target.pk]),
            {"new_password1": "password", "new_password2": "password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "password")
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Target-current-pass-493!"))
        self.assertFalse(DomainEvent.objects.filter(kind="USER_PASSWORD_RESET").exists())

    def test_admin_cannot_use_other_user_reset_route_for_self(self):
        self.admin_login()
        response = self.client.post(
            reverse("user-password-reset", args=[self.admin.pk]),
            {"new_password1": "New-temporary-password-927!", "new_password2": "New-temporary-password-927!"},
        )
        self.assertRedirects(response, reverse("user-management"))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("Admin-current-pass-493!"))

    def test_google_only_account_password_is_managed_by_google(self):
        account = User.objects.create_user("google-password-user", password=None, role=User.Role.CLIENT)
        SocialAccount.objects.create(user=account, provider="google", uid="google-password-user")
        self.admin_login()
        reset = self.client.get(reverse("user-password-reset", args=[account.pk]))
        self.assertContains(reset, "password is managed by Google")
        self.assertNotContains(reset, 'name="new_password1"')
        own = User.objects.create_user("google-admin-user", password=None, role=User.Role.ADMIN)
        SocialAccount.objects.create(user=own, provider="google", uid="google-admin-user")
        self.client.force_login(own)
        own_page = self.client.get(reverse("admin-password-change"))
        self.assertContains(own_page, "password changes for this account are managed by Google")
        self.assertNotContains(own_page, 'name="old_password"')

    def test_admin_can_change_own_password_and_keep_current_session(self):
        self.admin_login()
        url = reverse("admin-password-change")
        new_password = "Updated-admin-password-819!"
        response = self.client.post(
            url,
            {
                "old_password": "Admin-current-pass-493!",
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(response, url)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(new_password))
        self.assertEqual(self.client.get(reverse("user-management")).status_code, 200)
        self.assertIn(SESSION_KEY, self.client.session)
        event = DomainEvent.objects.get(kind="USER_PASSWORD_CHANGED")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.payload["target_user_id"], self.admin.pk)
        self.assertNotIn(new_password, str(event.payload))

    def test_wrong_current_password_is_rejected(self):
        self.admin_login()
        response = self.client.post(
            reverse("admin-password-change"),
            {
                "old_password": "wrong-current-password",
                "new_password1": "Updated-admin-password-819!",
                "new_password2": "Updated-admin-password-819!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your old password was entered incorrectly")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("Admin-current-pass-493!"))
