import unicodedata
from collections import defaultdict

from django.db import migrations, models
import django.db.models.deletion


def populate_names(apps, schema_editor):
    Zone = apps.get_model('operations', 'OperationalZone')
    groups = defaultdict(list)
    for zone in Zone.objects.using(schema_editor.connection.alias).all():
        key = ' '.join(unicodedata.normalize('NFKC', zone.name).casefold().split())
        groups[key].append(zone)
    conflicts = [f'{key!r}: ' + ', '.join(f'{z.name} ({z.pk})' for z in zones)
                 for key, zones in groups.items() if not key or len(zones) > 1]
    if conflicts:
        raise RuntimeError('Area normalization collisions; no areas were merged or renamed. Resolve explicitly before migrating: ' + '; '.join(conflicts))
    for key, zones in groups.items():
        Zone.objects.using(schema_editor.connection.alias).filter(pk=zones[0].pk).update(normalized_name=key)


class Migration(migrations.Migration):
    dependencies = [('operations', '0014_bulk_quote_notice_state')]
    operations = [
        migrations.AddField(model_name='operationalzone', name='region', field=models.CharField(max_length=100, blank=True)),
        migrations.AddField(model_name='operationalzone', name='district', field=models.CharField(max_length=100, blank=True)),
        migrations.AddField(model_name='operationalzone', name='normalized_name', field=models.CharField(max_length=200, null=True, editable=False)),
        migrations.RunPython(populate_names, migrations.RunPython.noop),
        migrations.AlterField(model_name='operationalzone', name='normalized_name', field=models.CharField(max_length=200, unique=True, editable=False)),
        migrations.CreateModel(name='AreaAlias', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('name', models.CharField(max_length=100)),
            ('normalized_name', models.CharField(max_length=200, unique=True, editable=False)),
            ('zone', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='aliases', to='operations.operationalzone')),
        ]),
    ]
