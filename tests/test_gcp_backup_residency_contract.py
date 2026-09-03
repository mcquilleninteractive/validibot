"""Contract tests for regional Cloud SQL backup provisioning.

These checks prevent a fresh stage from silently accepting Cloud SQL's
provider-selected multi-region backup location instead of the configured GCP
region.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GCP_RECIPES = (REPO_ROOT / "just/gcp/mod.just").read_text(encoding="utf-8")


def test_cloud_sql_creation_pins_backup_location_to_the_stage_region() -> None:
    """A new instance must not recreate backups outside its selected region."""
    creation_recipe = GCP_RECIPES.split(
        'gcloud sql instances create "$DB_INSTANCE"',
        maxsplit=1,
    )[1].split(
        'echo "   Created"',
        maxsplit=1,
    )[0]

    assert "--region={{gcp_region}}" in creation_recipe
    assert "--backup-location={{gcp_region}}" in creation_recipe
