import hashlib
import secrets
import string
import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

TOKEN_LENGTH = 20
TOKEN_ALPHABET = string.ascii_letters + string.digits


def generate_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


CALENDAR_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def generate_calendar_token() -> str:
    return "".join(secrets.choice(CALENDAR_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Guest(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guest")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Guest: {self.user.username}"


class AccountToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_token")
    token_hash = models.CharField(max_length=64, unique=True)

    def __str__(self) -> str:
        return f"AccountToken: {self.user.username}"


class CalendarToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_token")
    token = models.CharField(max_length=TOKEN_LENGTH, unique=True)

    def __str__(self) -> str:
        return f"CalendarToken: {self.user.username}"


class Contract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contracts")
    name = models.CharField(max_length=200)
    home_country = models.CharField(max_length=2)
    client_country = models.CharField(max_length=2)
    max_working_days = models.PositiveIntegerField()
    working_hours_per_day = models.PositiveIntegerField(default=8)
    start_date = models.DateField()
    end_date = models.DateField()

    def __str__(self) -> str:
        return self.name


class TimeOff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="time_off")
    date = models.DateField()
    hours = models.PositiveIntegerField()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["contract", "date"], name="unique_contract_date"),
        ]

    def __str__(self) -> str:
        return f"{self.contract.name} - {self.date}"


class Holiday(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_code = models.CharField(max_length=2)
    year = models.PositiveIntegerField()
    date = models.DateField()
    name = models.CharField(max_length=200)
    fetched_at = models.DateTimeField()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["country_code", "year", "date"],
                name="unique_country_year_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.country_code} {self.date})"
