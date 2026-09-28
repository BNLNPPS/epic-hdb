"""
Django settings for hdb_project project.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# DJANGO_DEBUG / DJANGO_ALLOWED_HOSTS / DJANGO_SECRET_KEY are read from the
# environment (see /etc/hdb/env on RHEL) so production can be locked down
# without editing this file. Unlike DJANGO_DB_TYPE below, these default to
# the *safe* choice when unset -- forgetting to set DJANGO_DEBUG in prod
# must not silently reopen debug pages the way an unset DJANGO_DB_TYPE
# silently fell back to sqlite once. Local dev sets DJANGO_DEBUG=true (e.g.
# in the venv's activate script) to get debug pages and runserver's
# built-in static-file serving back.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-w_f-4jto%ot**s#x%z7&t9=ocv=fm_bmqiq)*=cwc(oe5*+g7m',
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes', 'on')

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if h.strip()
]
CSRF_TRUSTED_ORIGINS = [
    "https://*.trycloudflare.com",
    "https://epic-hwdb.sdcc.bnl.gov",
]


SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Apache forwards the original Host header as X-Forwarded-Host (see
# deploy/httpd/hdb.conf's RequestHeader line) because ProxyPass sends
# requests to gunicorn as http://127.0.0.1:8002/, so without this
# Django would otherwise build absolute URIs (e.g. the DRF browsable
# API's hyperlinks) using that internal address instead of the public
# hostname, leaking the backend's loopback port to anyone hitting /api/.

### Attention!

# USE_X_FORWARDED_HOST = True

# fields.W342: DesignElementInstance.instance is a ForeignKey(unique=True)
# rather than a OneToOneField. Deliberate -- the DB constraint is identical
# either way, but a real OneToOneField's reverse accessor returns a single
# object (raising DoesNotExist if unset) instead of the manager-style
# accessor (.all(), .exists(), prefetch_related) that hdb/views_web.py and
# inventory_detail.html rely on via ComponentInstance.design_installations.
# Switching would mean reworking those call sites for no functional gain,
# so this specific warning is silenced rather than "fixed".
SILENCED_SYSTEM_CHECKS = ["fields.W342"]

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'hdb',
]

# djangorestframework is optional — install with: pip install djangorestframework
try:
    import rest_framework  # noqa: F401
    INSTALLED_APPS.append('rest_framework')
    REST_FRAMEWORK = {
        'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
        'PAGE_SIZE': 50,
        'DEFAULT_FILTER_BACKENDS': [
            'rest_framework.filters.SearchFilter',
            'rest_framework.filters.OrderingFilter',
        ],
        # The API requires the same Django-auth login as the rest of the
        # site. SessionAuthentication covers browser/AJAX callers that are
        # already logged in; BasicAuthentication covers script/CLI callers
        # that pass a username:password directly.
        'DEFAULT_AUTHENTICATION_CLASSES': [
            'rest_framework.authentication.SessionAuthentication',
            'rest_framework.authentication.BasicAuthentication',
        ],
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.IsAuthenticated',
        ],
    }
except ImportError:
    pass


MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'hdb_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'hdb_project.wsgi.application'


# Database
#
# DJANGO_DB_TYPE selects the backend: "sqlite" for local development,
# "postgres" for the RHEL deployment server. Defaults to "sqlite" so an
# unset variable behaves like the previous hardcoded default instead of
# silently trying to reach a Postgres server.

DJANGO_DB_TYPE = os.environ.get('DJANGO_DB_TYPE', 'sqlite').lower()

if DJANGO_DB_TYPE == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
elif DJANGO_DB_TYPE == 'postgres':
    try:
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': os.environ['DJANGO_DB_NAME'],
                'USER': os.environ['DJANGO_DB_USER'],
                'PASSWORD': os.environ['DJANGO_DB_PASSWORD'],
                'HOST': os.environ.get('DJANGO_DB_HOST', '127.0.0.1'),
                'PORT': os.environ.get('DJANGO_DB_PORT', '5432'),
            }
        }
    except KeyError as exc:
        raise ImproperlyConfigured(
            f"DJANGO_DB_TYPE=postgres requires {exc} to be set in the environment"
        ) from exc
else:
    raise ImproperlyConfigured(
        f"DJANGO_DB_TYPE={DJANGO_DB_TYPE!r} is not valid; expected 'sqlite' or 'postgres'"
    )


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalisation

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Authentication
# Only users that exist in Django's auth Users table may access the site.
# Anonymous visitors are sent to the login page (the site's landing page);
# after a successful login they land on the Dashboard.
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'


# Static files

STATIC_URL = 'static/'
STATIC_ROOT = '/var/data/hdb/static'


# Media (user-uploaded files: log attachments, property documents/images)

MEDIA_URL = '/media/'
MEDIA_ROOT = '/var/data/hdb/media'
