"""Create the built-in developer account (support/recovery) if it does not exist yet.

Username/password default to developer/developer and can be overridden with the
DEVELOPER_USERNAME / DEVELOPER_PASSWORD environment variables at deploy time.
The password is meant to be changed right after the first login (Account page).
"""
import os

from django.contrib.auth.hashers import make_password
from django.db import migrations


def create_developer(apps, schema_editor):
    User = apps.get_model("procurement", "User")
    username = os.environ.get("DEVELOPER_USERNAME", "developer")
    if User.objects.filter(username=username).exists() or User.objects.filter(role="DEVELOPER").exists():
        return  # never overwrite an existing account or reset a changed password
    User.objects.create(
        username=username,
        email="developer@localhost",
        first_name="Developer",
        role="DEVELOPER",
        is_active=True,
        password=make_password(os.environ.get("DEVELOPER_PASSWORD", "developer")),
    )


class Migration(migrations.Migration):
    dependencies = [("procurement", "0002_developer_role")]
    operations = [migrations.RunPython(create_developer, migrations.RunPython.noop)]
