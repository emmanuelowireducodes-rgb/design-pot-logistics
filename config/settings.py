import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
PRODUCTION = os.getenv("DPOT_ENV", "development") == "production"
DEBUG = not PRODUCTION
DEMO_MODE = os.getenv("DPOT_DEMO", "0") == "1"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-only-not-for-production")
if PRODUCTION and (len(SECRET_KEY) < 50 or DEMO_MODE):
    raise ImproperlyConfigured("Production requires a strong secret and demo mode disabled.")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = list(filter(None, os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")))
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "operations",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "operations.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "operations.context.shell",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
if PRODUCTION or os.getenv("DPOT_POSTGRES") == "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["PGDATABASE"],
            "USER": os.environ["PGUSER"],
            "PASSWORD": os.environ["PGPASSWORD"],
            "HOST": os.getenv("PGHOST", "localhost"),
            "PORT": os.getenv("PGPORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": os.getenv("PGSSLMODE", "require")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / ("demo.sqlite3" if DEMO_MODE else "db.sqlite3"),
        }
    }
AUTH_USER_MODEL = "operations.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 28800
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SECURE_SSL_REDIRECT = PRODUCTION
ENABLE_HSTS = PRODUCTION and os.getenv("DPOT_ENABLE_HSTS", "0") == "1"
SECURE_HSTS_SECONDS = int(os.getenv("DPOT_HSTS_SECONDS", "31536000")) if ENABLE_HSTS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = ENABLE_HSTS and os.getenv("DPOT_HSTS_SUBDOMAINS", "0") == "1"
SECURE_HSTS_PRELOAD = ENABLE_HSTS and os.getenv("DPOT_HSTS_PRELOAD", "0") == "1"
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if PRODUCTION and os.getenv("DPOT_TRUST_PROXY_SSL_HEADER") == "1"
    else None
)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Accra"
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Optional provider adapters may use this short cache window when introduced.
ROAD_ROUTE_CACHE_SECONDS = max(1, min(int(os.getenv("ROAD_ROUTE_CACHE_SECONDS", "300")), 900))
VAPID_PUBLIC_KEY = os.getenv("DPOT_VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("DPOT_VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("DPOT_VAPID_SUBJECT", "")
WEB_PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT)
# A shared cache is required for multi-worker rate limiting and routing cost control.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "dpot_cache"}
}
if "test" in sys.argv:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

PRIVATE_MEDIA_ROOT = Path(os.getenv("DPOT_PRIVATE_MEDIA_ROOT", str(BASE_DIR / "private_media")))
POD_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_HANDLERS = [
    "operations.uploads.BoundedImageUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 1
POD_POLICIES = {
    "standard": {"recipient_name": True, "photo": True},
    "premium": {"recipient_name": True, "photo": True},
}
# Development prices are opt-in in production and never described as approved rates.
ALLOW_DEVELOPMENT_PRICING = not PRODUCTION or os.getenv("DPOT_ALLOW_DEVELOPMENT_PRICING") == "1"
DEVELOPMENT_PRICING = {
    "bike": {"base": "20.00", "minimum": "20.00", "per_km": "2.00"},
    "car": {"base": "40.00", "minimum": "40.00", "per_km": "3.00"},
    "premium_surcharge": "10.00",
    "rounding": "1.00",
    "quote_minutes": 30,
}
