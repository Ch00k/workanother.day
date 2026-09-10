"""The two accounts a Polish sole trader pays its own obligations to.

Both are NRB, the domestic twenty-six digit form: two check digits, an eight-digit numer
rozliczeniowy naming the bank, then sixteen digits the bank assigns. Prefixing PL makes the
same number an IBAN, which is what a transfer from outside Poland is addressed to. Neither the
tax office nor ZUS writes the prefix on the number it gives its own payer, so neither does this
application - the country is in the check digits either way, ISO 13616 computing them over the
account followed by the code letters.

**The mikrorachunek podatkowy is computed rather than stored.** MF generates it from the
taxpayer's identifier and e-Urząd Skarbowy says as much on the page it shows it on, so the NIP
already on the seller is the whole of the input. A stored copy could outlive a correction to
that NIP, and a tax transfer addressed to a mikrorachunek belonging to somebody else is the
worst thing this application could arrange.

**The numer rachunku składkowego cannot be computed.** Twenty-one of its digits are ZUS's own
constant and the payer's NIP, and the three between them are allocated by ZUS: `001` on every
number seen, the specimen in ZUS's own mass-mailing template included, but ZUS publishes no rule
for them and tells payers to read the number off its records rather than generate it. So it is
entered by hand, and what can be checked about it is checked.
"""

from __future__ import annotations

# Two check digits and twenty-four the bank and its account occupy.
NRB_LENGTH = 26
CHECK_DIGITS_LENGTH = 2

# ISO 13616 moves the country code to the end as the values of its letters, P being 25 and L
# 21, and a correct number leaves 1 on division by 97. The check digits stand as zeros while
# they are being worked out, and the remainder is taken from 98.
POLAND_DIGITS = "2521"
MODULUS = 97
REMAINDER = 1
COMPLEMENT = 98

# What every mikrorachunek podatkowy carries: NBP's numer rozliczeniowy, the number completing
# it, and the digit naming which identifier the account was generated from - 2 for a NIP, which
# is the identifier of anyone carrying on działalność. The identifier itself follows, and the
# zeros that pad the account to length follow that.
MIKRORACHUNEK_PREFIX = "101000712222"
MIKRORACHUNEK_PADDING = "00"

# What ZUS states every numer rachunku składkowego carries at digits 3 to 13, and publishes as
# the way to tell one of its own numbers from one a letter has invented. The payer's NIP is the
# last ten digits, and the three between are ZUS's to allocate.
ZUS_PREFIX = "60000002026"
ZUS_PREFIX_AT = 2

NIP_LENGTH = 10

# Written in fours after the check digits, which is how both ZUS and e-Urząd Skarbowy write the
# number they give a payer, and how a bank's transfer form breaks it up.
GROUP = 4


def digits(value: str) -> str:
    """The digits of an account number, however it was written.

    Spaces because that is how a number is written down and copied, and the PL prefix because
    it is the same account either way and a payer who takes the number off a bank statement
    has it.
    """
    return "".join(value.split()).upper().removeprefix("PL")


def formatted(account: str) -> str:
    """An account number written out, or empty for one this cannot be sure of.

    Anything that is not twenty-six digits is handed back untouched: a number stored before it
    was checked is still worth showing to whoever has to correct it.
    """
    account = digits(account)
    if not _is_nrb_shaped(account):
        return account

    rest = account[CHECK_DIGITS_LENGTH:]
    groups = [rest[at : at + GROUP] for at in range(0, len(rest), GROUP)]

    return " ".join([account[:CHECK_DIGITS_LENGTH], *groups])


def check_digits(account: str) -> str:
    """The two digits that make `account` - the other twenty-four - a Polish account number."""
    remainder = int(account + POLAND_DIGITS + "0" * CHECK_DIGITS_LENGTH) % MODULUS

    return f"{COMPLEMENT - remainder:0{CHECK_DIGITS_LENGTH}d}"


def valid(value: str) -> bool:
    """Whether value is twenty-six digits passing the check ISO 13616 defines.

    The check digits are computed over every other digit, so a single mistyped or transposed
    one fails it. What it cannot catch is a number that is somebody else's and correct, which
    is why the account it is asked about is also checked for what it should contain.
    """
    account = digits(value)
    if not _is_nrb_shaped(account):
        return False

    return int(account[CHECK_DIGITS_LENGTH:] + POLAND_DIGITS + account[:CHECK_DIGITS_LENGTH]) % MODULUS == REMAINDER


def mikrorachunek(nip: str) -> str:
    """The mikrorachunek podatkowy a NIP pays PIT, CIT and VAT to. Empty for anything but a NIP.

    Verified against both places MF publishes it: the generator it links from
    podatki.gov.pl and the number e-Urząd Skarbowy shows a signed-in taxpayer.
    """
    if len(nip) != NIP_LENGTH or not nip.isdigit():
        return ""

    account = MIKRORACHUNEK_PREFIX + nip + MIKRORACHUNEK_PADDING

    return check_digits(account) + account


def valid_zus_account(value: str, *, nip: str) -> bool:
    """Whether value is the numer rachunku składkowego of the payer holding this NIP.

    The three checks ZUS publishes, and they are worth all three: the check digits catch a
    typo, the constant catches a number that is not a ZUS account at all - the thing a letter
    demanding contributions to somewhere else would carry - and the NIP catches another
    payer's, which is otherwise a perfectly correct account number.

    A NIP that is not there is not checked against, an account being enterable before the NIP
    beside it is.
    """
    account = digits(value)
    if not valid(account):
        return False

    if account[ZUS_PREFIX_AT : ZUS_PREFIX_AT + len(ZUS_PREFIX)] != ZUS_PREFIX:
        return False

    return not nip or account.endswith(nip)


def _is_nrb_shaped(account: str) -> bool:
    return len(account) == NRB_LENGTH and account.isdigit()
