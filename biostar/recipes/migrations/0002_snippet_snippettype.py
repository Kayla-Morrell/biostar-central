# Generated by Django 2.2 on 2019-10-16 23:17

import biostar.recipes.models
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('recipes', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SnippetType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(blank=True, default=None, max_length=1024, upload_to=biostar.recipes.models.snippet_images)),
                ('uid', models.CharField(max_length=10000, unique=True)),
                ('name', models.CharField(max_length=256)),
                ('default', models.BooleanField(default=False)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='Snippet',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('help_text', models.CharField(max_length=10000)),
                ('uid', models.CharField(max_length=10000, unique=True)),
                ('command', models.CharField(max_length=10000, null=True)),
                ('default', models.BooleanField(default=False)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='recipes.SnippetType')),
            ],
        ),
    ]
