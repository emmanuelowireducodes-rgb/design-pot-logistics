from getpass import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from operations.models import DomainEvent, Organization, User


class Command(BaseCommand):
    help = "Create an account with an explicit role; password is entered privately."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--role", required=True, choices=User.Role.values)
        parser.add_argument("--organization-id", type=int)
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")

    @transaction.atomic
    def handle(self, *args, **options):
        if User.objects.filter(username=options["username"]).exists():
            raise CommandError("This username already exists.")
        organization = None
        if options["role"] == "client":
            if not options["organization_id"]:
                raise CommandError("Clients require --organization-id.")
            try:
                organization = Organization.objects.get(pk=options["organization_id"])
            except Organization.DoesNotExist as exc:
                raise CommandError("Organization does not exist.") from exc
        elif options["organization_id"]:
            raise CommandError("Only client accounts may belong to a client organization.")
        password = getpass("Password (minimum 12 characters): ")
        if password != getpass("Confirm password: "):
            raise CommandError("Passwords do not match.")
        user = User(
            username=options["username"],
            role=options["role"],
            organization=organization,
            first_name=options["first_name"],
            last_name=options["last_name"],
        )
        validate_password(password, user)
        user.full_clean(exclude=["password"])
        user.set_password(password)
        user.save()
        DomainEvent.objects.create(
            actor=user,
            kind="account.provisioned",
            payload={"source": "trusted_management_command", "role": user.role},
        )
        self.stdout.write(
            self.style.SUCCESS(f"Account {user.username} created with role {user.role}.")
        )
