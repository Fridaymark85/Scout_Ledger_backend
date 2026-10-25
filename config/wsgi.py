import os
from django.core.wsgi import get_wsgi_application

# Change 'config' to whatever folder name contains this file
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()