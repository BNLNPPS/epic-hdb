"""
Django settings for hdb_project project.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-w_f-4jto%ot**s#x%z7&t9=ocv=fm_bmqiq)*=cwc(oe5*+g7m'

DEBUG = True

ALLOWED_HOSTS = ['*']
# ALLOWED_HOSTS = ['wondering-association-thumb-pieces.trycloudflare.com', 'localhost', '127.0.0.1']
CSRF_TRUSTED_ORIGINS = [
    "https://*.trycloudflare.com",
]

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

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


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


# Media (user-uploaded files: log attachments, property documents/images)

MEDIA_URL = '/media/'
MEDIA_ROOT = '/var/data/hdb/media'
