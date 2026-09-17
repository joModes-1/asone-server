"""Password rules beyond Django's four.

Django's `CommonPasswordValidator` carries a generic list of the twenty
thousand most-breached passwords. It is worth having, and it does not know
this is AsOne, in Uganda, running warehouses called Namayemba and Serere.

Probed on 16 September 2026 against the validators as configured:

    password      refused, too common
    qwerty123     refused, too common
    Grace123      refused, too similar to the first name
    asone123      ALLOWED
    AsOne2026     ALLOWED
    Uganda123     ALLOWED

Those last three are the passwords people here actually choose. A list of
site words closes it.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

#: Words that mean something to everyone using this system, so they are the
#: first thing anyone guessing would try. Sites, the organisation, the
#: product, and the obvious words around them.
#:
#: Kept here rather than in settings because it is a rule, not a
#: deployment knob — a second AsOne instance should refuse the same words.
SITE_WORDS = frozenset(
    {
        # the organisation and the people building it
        "asone",
        "asonelogistics",
        "asoneministries",
        "era92",
        "era",
        # the country and where the work happens
        "uganda",
        "ugandan",
        "kampala",
        "jinja",
        # warehouses and tailoring centres, from the 14 August pack
        "namayemba",
        "serere",
        "idudi",
        "rwanyabihuka",
        # what the system is about
        "logistics",
        "uniform",
        "uniforms",
        "warehouse",
        "inventory",
        "school",
        "schools",
        "tailoring",
        "garment",
        "garments",
    }
)

#: Everything that is not a letter. Stripped before comparing, so decorating
#: a site word with digits or punctuation does not get past this.
_DECORATION = re.compile(r"[^a-z]+")


class SiteWordValidator:
    """Refuse a password that is a site word wearing a disguise.

    The comparison strips digits and punctuation and lowercases what is
    left, so ``AsOne2026``, ``asone-123`` and ``A.S.O.N.E`` all reduce to
    ``asone`` and are refused.

    It deliberately does **not** refuse a password that merely *contains* a
    site word. ``correct-horse-namayemba-staple`` is a good passphrase and
    telling somebody it is not would push them towards a worse one. The
    rule is "this password is a site word with decoration", not "this
    password mentions us".
    """

    def validate(self, password, user=None):
        stripped = _DECORATION.sub("", password.lower())
        if stripped in SITE_WORDS:
            raise ValidationError(
                _(
                    "This password is built from a word everyone at AsOne "
                    "knows, so it is one of the first anybody would try. "
                    "Adding numbers to it does not help. Use several "
                    "unrelated words instead."
                ),
                code="password_site_word",
            )

    def get_help_text(self):
        return _(
            "Your password cannot be an AsOne, Uganda or warehouse word with "
            "numbers added."
        )
