import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"


def _required(name: str, development_default: str) -> str:
    """Read a secret that only development is allowed to guess.

    Sessions are the whole of this app's authentication, so a deployment that reaches
    production without its own keys must refuse to start rather than fall back to
    something an attacker can guess in one try. Development keeps a fixed value so
    restarting the server does not invalidate everything issued before it.
    """
    value = os.environ.get(name, "")
    if value:
        return value

    if DEBUG:
        return development_default

    message = f"{name} must be set when DJANGO_DEBUG is not 1."
    raise ImproperlyConfigured(message)


SECRET_KEY = _required("DJANGO_SECRET_KEY", "insecure-development-secret-key")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if not DEBUG else []

CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS if host]

CANONICAL_HOST = os.environ.get("DJANGO_CANONICAL_HOST", "")

# Both deployments terminate TLS at a proxy (Fly, Caddy) that sets this header, so it is
# what tells Django a request arrived over HTTPS. Without it the redirect below would
# loop, because every proxied request looks plaintext from here.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# A session cookie is a bearer token for the account. Sent once over plaintext it is
# captured, and the redirect to HTTPS comes too late to help, so the browser is told
# never to send it that way. Off in development, which is served over http.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "wad",
]

MIDDLEWARE = [
    "wad.middleware.WwwRedirectMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wad.middleware.HtmxRedirectMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "wad.context_processors.feature_flags",
                "wad.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3")),
        "OPTIONS": {
            # WAL lets the calendar feed be read while days off are being booked. Writes
            # take the lock up front so two of them queue instead of one discovering
            # halfway through that it has to roll back, and a writer waits rather than
            # failing the request outright.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashes the contents into each filename, so a stylesheet change reaches browsers
    # that already hold the old one instead of waiting for their cache to expire.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Errors are the only signal a single-machine deployment gives that something is wrong.
# Django's own default routes them to mail_admins, which goes nowhere without ADMINS, so
# they are written to stderr where Fly collects them.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"stderr": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "django": {"handlers": ["stderr"], "level": "INFO"},
        "django.request": {"handlers": ["stderr"], "level": "ERROR", "propagate": False},
        "wad": {"handlers": ["stderr"], "level": "INFO"},
    },
}

# KSeF
# Who invoices are issued by, and the credential they are issued with, are set per
# contract. What stays here is which KSeF a deployment talks to, because a token only
# works against the environment it was created in. The verification host is derived from
# the same choice so the two cannot disagree. It defaults to the sandbox: a
# half-configured instance should not be able to issue anything with legal effect.
KSEF_ENVIRONMENT = os.environ.get("KSEF_ENVIRONMENT", "TEST")

# Encrypts the stored KSeF tokens. Held apart from SECRET_KEY because rotating a signing
# key should not make every seller's credential unreadable. Generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
KSEF_TOKEN_KEY = _required("DJANGO_KSEF_TOKEN_KEY", "rXevUkBPASjAGdFZDd0mIj-SukHMTv-SxRKx1uGmbCY=")
KSEF_QR_BASE_URL = {
    "TEST": "https://qr-test.ksef.mf.gov.pl",
    "DEMO": "https://qr-demo.ksef.mf.gov.pl",
    "PRODUCTION": "https://qr.ksef.mf.gov.pl",
}[KSEF_ENVIRONMENT]

# Auth
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
