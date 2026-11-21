from django.db import migrations

_FIELDS = ("waiting", "prior_waiting")


def _rewrite(apps, change):
    rows = apps.get_model("django_spec_agents_store", "StoredThreadRun").objects
    for field in _FIELDS:
        for row in rows.filter(**{f"{field}__isnull": False}).only("pk", field):
            setattr(row, field, change(getattr(row, field)))
            row.save(update_fields=[field])


def _to_lists(apps, schema_editor):
    _rewrite(apps, lambda call: [call])


def _to_first_call(apps, schema_editor):
    _rewrite(apps, lambda calls: calls[0])


class Migration(migrations.Migration):
    dependencies = [
        ("django_spec_agents_store", "0007_thread_run_prior"),
    ]

    operations = [
        migrations.RunPython(_to_lists, _to_first_call, elidable=True),
    ]
