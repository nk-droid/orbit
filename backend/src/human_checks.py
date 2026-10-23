"""Pages that ask whether the user is a person: a CAPTCHA, a tick box, a puzzle, or a
site's security check.

Orbit never does such a check itself, and never tries to get around one. It hands the
page to the user, who does it in the browser they can see, and goes on once they have.
This module only tells such a page from any other.
"""

import re

# What such a page says, anywhere on it. A page that merely has a CAPTCHA's badge,
# saying it is protected by one, does not ask anything.
_ASKS = re.compile(
    r"i[\u2019']?m not a robot|are you a robot|"
    r"verify (?:that )?you(?: are|[\u2019']re) (?:a )?human|"
    r"confirm (?:that )?you(?: are|[\u2019']re) (?:a )?(?:human|person)|"
    r"security (?:check|verification)|unusual (?:traffic|activity)|"
    r"press (?:and|&) hold|captcha checkbox|checking (?:your browser|if the site)",
    re.IGNORECASE,
)
# What says it at the top of a page, where its title is.
_TOP = re.compile(r"captcha", re.IGNORECASE)
TOP_CHARS = 1200


def asks(text: str) -> bool:
    """Whether a page, as the browser reads it, asks the user to show they are a
    person."""
    return bool(_ASKS.search(text) or _TOP.search(text[:TOP_CHARS]))
