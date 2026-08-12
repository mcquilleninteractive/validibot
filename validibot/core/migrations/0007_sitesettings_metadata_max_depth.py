"""Bound the nesting depth of untrusted submission metadata."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_credentialverificationkey"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="metadata_max_depth",
            field=models.PositiveIntegerField(
                default=8,
                help_text=(
                    "Maximum nesting depth for submission metadata, counting the "
                    "top-level object as depth 1. Set to 0 to disable the limit."
                ),
            ),
        ),
    ]
