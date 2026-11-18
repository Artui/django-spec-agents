from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_spec_agents_store", "0006_inbox_claim"),
    ]

    operations = [
        migrations.AddField(
            model_name="storedthreadrun",
            name="prior_status",
            field=models.CharField(blank=True, default=None, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="storedthreadrun",
            name="prior_reason",
            field=models.CharField(blank=True, default=None, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="storedthreadrun",
            name="prior_waiting",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="storedthreadrun",
            name="prior_last_run_id",
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
    ]
