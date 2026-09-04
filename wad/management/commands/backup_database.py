from pathlib import Path
from typing import cast

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection


class Command(BaseCommand):
    help = (
        "Write a consistent copy of the database to a path. Safe to run while the application is "
        "serving: VACUUM INTO reads under a single transaction, so the copy carries the "
        "transactions still in the write-ahead log and cannot catch a half-written page. What it "
        "writes is one self-contained file with no sidecars that have to travel with it."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", help="Where to write the copy.")

    def handle(self, *args: str, **options: str | None) -> None:  # noqa: ARG002
        path = Path(cast("str", options["path"]))

        # VACUUM INTO refuses to write over a file that exists, so a run interrupted between the
        # copy and its removal would leave every run after it failing on the leftover.
        path.unlink(missing_ok=True)

        with connection.cursor() as cursor:
            cursor.execute("VACUUM INTO %s", [str(path)])

        self.stdout.write(f"Wrote {path}, {path.stat().st_size} bytes.")
