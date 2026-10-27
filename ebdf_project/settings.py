"""
Django settings for ebdf_project project.

PostgreSQL-backed civic emergency blood dispatch framework.
"""

import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
env_file = BASE_DIR / '.env'
if env_file.is_file():
    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-58_slsmudf+onc%8mmyw9uj61#_c5pienwew86p%_*%p%%m1pt')
DONOR_ID_PEPPER = os.environ.get('DONOR_ID_PEPPER', 'ebdf-production-donor-pepper-value-2026')

DEBUG = os.environ.get('DEBUG', 'True').lower() in ('true', '1', 'yes')

if not DEBUG:
    if not os.environ.get('SECRET_KEY') or SECRET_KEY.startswith('django-insecure'):
        raise ValueError("Insecure SECRET_KEY: A secure SECRET_KEY environment variable must be set in production (DEBUG=False).")
    if not os.environ.get('DONOR_ID_PEPPER'):
        raise ValueError("DONOR_ID_PEPPER environment variable must be configured in production (DEBUG=False).")

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'blood_ledger.apps.BloodLedgerConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
]
try:
    import whitenoise  # noqa: F401
    MIDDLEWARE.append('whitenoise.middleware.WhiteNoiseMiddleware')
except ImportError:
    pass

MIDDLEWARE.extend([
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
])

CSRF_TRUSTED_ORIGINS = [
    'https://*.onrender.com',
    'https://*.koyeb.app',
    'https://*.railway.app',
    'https://*.hf.space',
    'https://*.vercel.app',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]
extra_csrf = os.environ.get('CSRF_TRUSTED_ORIGINS')
if extra_csrf:
    CSRF_TRUSTED_ORIGINS.extend([o.strip() for o in extra_csrf.split(',') if o.strip()])

ROOT_URLCONF = 'ebdf_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'ebdf_project.wsgi.application'

# Database Configuration (Neon PostgreSQL / Render / Local)
if os.environ.get('DATABASE_URL'):
    DATABASES = {
        'default': dj_database_url.config(
            default=os.environ['DATABASE_URL'],
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'EBDF'),
            'USER': os.environ.get('DB_USER', os.environ.get('DB_USERNAME', 'postgres')),
            'PASSWORD': os.environ.get('DB_PASSWORD', '250730'),
            'HOST': os.environ.get('DB_HOST', 'localhost'),
            'PORT': os.environ.get('DB_PORT', '5432'),
        }
    }

# Database Cache Configuration for custom rate-limiting
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'ebdf_cache_table',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'public_search'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Application specific settings
DONOR_SEARCH_RADIUS_KM = int(os.environ.get('DONOR_SEARCH_RADIUS_KM', 50))
LOW_STOCK_THRESHOLD = int(os.environ.get('LOW_STOCK_THRESHOLD', 4))
