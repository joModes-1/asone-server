"""Load AsOne's product catalogue from a spreadsheet — F05, F06, F08.

    .venv/bin/python manage.py import_catalog catalogue.xlsx --dry-run
    .venv/bin/python manage.py import_catalog catalogue.xlsx

A thin wrapper. Everything about what a catalogue file may contain, and what
makes one unacceptable, lives in `catalog/importing.py` — shared with
`POST /api/catalog/import/`, which the Garments tab uses. The rules must not
depend on which door a file came through, so neither door owns them.

What is left here is the part that is genuinely about a terminal: reading a
path off the command line, and printing a tally somebody can read.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog import importing


class Command(BaseCommand):
    help = "Import garments, sizes, SKUs and prices from a spreadsheet."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The .xlsx or .csv file to read.")
        parser.add_argument(
            "--sheet",
            help="Which worksheet to read. Defaults to the first one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported, then roll it back.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        try:
            tally = importing.import_catalogue(
                path,
                filename=path.name,
                sheet=options.get("sheet"),
                dry_run=options["dry_run"],
            )
        except importing.CatalogueFileError as exc:
            raise CommandError(str(exc)) from None
        except importing.RowsRejected as exc:
            self.stdout.write(
                self.style.ERROR(f"{len(exc.errors)} rows could not be read:")
            )
            for error in exc.errors:
                self.stdout.write(
                    self.style.ERROR(f"  line {error['line']}: {error['message']}")
                )
            raise CommandError(
                "Nothing was imported. Fix the file and run it again."
            ) from None

        self._report(tally, dry_run=options["dry_run"])

    def _report(self, tally, *, dry_run):
        verb = "would create" if dry_run else "created"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Catalogue import"))
        self.stdout.write(f"  {verb}  {tally['garments']:4} garments")
        self.stdout.write(f"  {verb}  {tally['sizes']:4} sizes")
        self.stdout.write(f"  {verb}  {tally['skus']:4} SKUs")
        self.stdout.write(f"  {verb}  {tally['prices']:4} prices")
        if tally["unchanged"]:
            self.stdout.write(f"  already there: {tally['unchanged']} garments")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Dry run — nothing was written. Drop --dry-run to import."
                )
            )
