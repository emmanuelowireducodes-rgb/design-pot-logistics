from django import forms
from django.contrib.auth.forms import AuthenticationForm


class SignInForm(AuthenticationForm):
    username = forms.CharField(
        label="Username or email",
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "autofocus": True,
                "placeholder": "Username or email",
                "aria-describedby": "id_username_errors",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "current-password",
                "placeholder": "Your password",
                "aria-describedby": "id_password_errors",
            }
        ),
    )


class DeliveryFilterForm(forms.Form):
    q = forms.CharField(max_length=100, required=False)
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All statuses"),
            ("unassigned", "Unassigned"),
            ("requested", "Requested"),
            ("confirmed", "Confirmed"),
            ("assigned", "Assigned"),
            ("in_transit", "In transit"),
            ("delivered", "Delivered"),
            ("failed", "Failed delivery"),
            ("issues", "Delivery exceptions"),
        ],
    )
