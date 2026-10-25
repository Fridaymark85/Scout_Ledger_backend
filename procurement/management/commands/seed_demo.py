"""Create demo users (one per role) for local trials: `python manage.py seed_demo`."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from procurement.models import User

# (username, first name, role) – all share the password given on the command line.
DEMO_USERS = [
    ("requester", "Riya", User.Role.REQUESTOR),
    ("approver1", "Manoj", User.Role.SENIOR_APPROVER),
    ("approver2", "Farah", User.Role.SENIOR_APPROVER),
    ("approver3", "Dev", User.Role.SENIOR_APPROVER),
    ("admin", "Pooja", User.Role.ADMIN),
]


class Command(BaseCommand):
    help = "Create demo users for each role (refuses to run when DEBUG is off)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="Demo@12345")

    def handle(self, *args, **opts):
        # Shared known passwords must never reach a production database.
        if not (settings.DEBUG or getattr(settings, "USE_SQLITE", False)):
            raise CommandError("Demo users are for local trials only (set DJANGO_DEBUG=1 or DJANGO_USE_SQLITE=1).")
        for username, first, role in DEMO_USERS:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first, "role": role, "email": f"{username}@demo.com"},
            )
            user.set_password(opts["password"])
            user.save()
            self.stdout.write(f"{'created' if created else 'updated'} {username} ({role})")
