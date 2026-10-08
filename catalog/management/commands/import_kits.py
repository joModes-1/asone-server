"""Load AsOne's uniform kits and their components from a spreadsheet.

    .venv/bin/python manage.py import_kits "Kit BOM.csv" --dry-run
    .venv/bin/python manage.py import_kits "Kit BOM.csv"

The sibling of `import_catalog`, and a thin wrapper for the same reason:
everything about what a kit sheet may contain lives in
`catalog/kit_importing.py`, shared with `POST /api/catalog/kits/import/`.

**Run the catalogue import first.** A kit's components are matched to SKUs
that already exist — nothing is created here — so a kit sheet read against
an empty catalogue reports every line as an unknown SKU.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog import kit_importing


class Command(BaseCommand):
    help = "Import uniform kits and their components from a spreadsheet."

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
            tally = kit_importing.import_kits(
                path,
                filename=path.name,
                sheet=options.get("sheet"),
                dry_run=options["dry_run"],
            )
        except kit_importing.CatalogueFileError as exc:
            raise CommandError(str(exc)) from None
        except kit_importing.RowsRejected as exc:
            self.stdout.write(
                self.style.ERROR(f"{len(exc.errors)} rows could not be read:")
            )
            # Capped, because a kit sheet read before its catalogue reports
            # all 828 lines and a terminal full of the same message tells
            # nobody more than twenty of them would.
            for error in exc.errors[:20]:
                self.stdout.write(
                    self.style.ERROR(f"  line {error['line']}: {error['message']}")
                )
            if len(exc.errors) > 20:
                self.stdout.write(
                    self.style.ERROR(f"  … and {len(exc.errors) - 20} more.")
                )
            raise CommandError(
                "Nothing was imported. Fix the file and run it again."
            ) from None

        self._report(tally, dry_run=options["dry_run"])

    def _report(self, tally, *, dry_run):
        verb = "would create" if dry_run else "created"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Kit import"))
        self.stdout.write(f"  {verb}  {tally['kits']:4} kits")
        self.stdout.write(f"  {verb}  {tally['components']:4} components")
        if tally["components_changed"]:
            self.stdout.write(
                f"  quantity changed on {tally['components_changed']} components"
            )
        if tally["components_removed"]:
            self.stdout.write(
                self.style.WARNING(
                    f"  removed {tally['components_removed']} components no "
                    "longer in the file"
                )
            )
        if tally["unchanged"]:
            self.stdout.write(f"  already there: {tally['unchanged']} kits")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Dry run — nothing was written. Drop --dry-run to import."
                )
            )
