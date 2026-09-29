from allauth.socialaccount.forms import SignupForm
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Organization


class DesignPotSocialSignupForm(SignupForm):
    role = forms.ChoiceField(
        label="Account type",
        widget=forms.RadioSelect(attrs={"class": "role-radio-control"}),
        choices=[
            ("client", "Client"),
            ("rider", "Rider"),
            ("driver", "Driver"),
        ],
    )
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    phone = forms.CharField(max_length=24)
    organization_name = forms.CharField(max_length=160, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["aria-describedby"] = f"id_{name}_errors"

    def clean_email(self):
        email = super().clean_email()
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                "An account already uses this email. Sign in to that account or contact Design-Pot Admin."
            )
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") == "client" and not cleaned.get("organization_name", "").strip():
            self.add_error("organization_name", "Enter your business or organization name.")
        return cleaned

    def save(self, request):
        user = super().save(request)
        user.first_name = self.cleaned_data["first_name"].strip()
        user.last_name = self.cleaned_data["last_name"].strip()
        user.phone = self.cleaned_data["phone"].strip()
        user.role = self.cleaned_data["role"]
        user.operational_access_approved = user.role == "client"
        update_fields = [
            "first_name", "last_name", "phone", "role", "operational_access_approved"
        ]
        if user.role == "client":
            user.organization = Organization.objects.create(
                name=self.cleaned_data["organization_name"].strip()
            )
            update_fields.append("organization")
        user.save(update_fields=update_fields)
        return user
