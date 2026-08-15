from __future__ import annotations

import functools

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


@functools.cache
def _cipher() -> Fernet:
    return Fernet(settings.KSEF_TOKEN_KEY.encode())


class EncryptedTextField(models.TextField):
    """Text that is unreadable in the database file itself.

    For values whose loss is worse than the loss of the row holding them: a KSeF token is
    the standing power to issue invoices under a NIP until it is revoked, so a copy of the
    volume should not be enough to use one.

    Encryption happens here rather than at the call sites so that no query, fixture or
    admin path can write one in the clear by forgetting to.
    """

    def get_prep_value(self, value: str | None) -> str | None:
        if value is None or value == "":
            return value

        return _cipher().encrypt(value.encode()).decode()

    def from_db_value(self, value: str | None, expression: object, connection: object) -> str | None:
        del expression, connection

        if value is None or value == "":
            return value

        try:
            return _cipher().decrypt(value.encode()).decode()
        except InvalidToken:
            # Written under a key this deployment no longer holds. Reporting it as absent
            # keeps the rest of the seller readable and sends its owner to the form to
            # enter the token again, which is the only way back.
            return ""
