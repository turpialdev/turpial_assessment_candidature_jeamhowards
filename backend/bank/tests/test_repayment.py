from datetime import date
from decimal import Decimal

import pytest

from bank.models import Account, AccountKind, Loan, Posting
from bank.selectors import get_balance
from bank.services.disbursement import disburse
from bank.services.repayment import record_repayment
from bank.services.transactions import record_transaction

pytestmark = pytest.mark.django_db


def _loan():
    """Build a loan with balances designed to exercise all waterfall tiers.

    Starting balances are $5.00 fees, $12.50 interest, and $100.00 principal.
    The interest event is posted through the normal transaction service so the
    fixture itself also obeys double-entry bookkeeping.
    """
    loan = Loan.objects.create(
        borrower="Test Borrower",
        principal=Decimal("100.00"),
        annual_rate=Decimal("0.1200"),
        fees=Decimal("5.00"),
        limit=Decimal("150.00"),
        opened_on=date(2026, 1, 5),
    )
    disburse(loan)
    interest = Account.objects.get(loan=loan, kind=AccountKind.INTEREST_RECEIVABLE)
    cash = Account.objects.get(loan=None, kind=AccountKind.CASH)
    record_transaction(
        memo="Test interest accrual",
        effective_on=date(2026, 1, 6),
        loan=loan,
        postings=[
            Posting(account=interest, amount=Decimal("12.50")),
            Posting(account=cash, amount=Decimal("-12.50")),
        ],
    )
    return loan


def test_repayment_applies_fees_then_interest_then_principal():
    """A $20 payment clears $5 fees and $12.50 interest, then pays $2.50 principal."""
    loan = _loan()

    txn = record_repayment(
        loan=loan, amount=Decimal("20.00"), effective_on=date(2026, 1, 7)
    )

    # Debiting cash by $20 is offset by $5 + $12.50 + $2.50 in receivable credits.
    assert sum(posting.amount for posting in txn.postings.all()) == Decimal("0.00")
    # Fees and interest are fully settled before any amount reaches principal.
    assert get_balance(Account.objects.get(loan=loan, kind=AccountKind.FEES_RECEIVABLE)) == Decimal("0.00")
    assert get_balance(Account.objects.get(loan=loan, kind=AccountKind.INTEREST_RECEIVABLE)) == Decimal("0.00")
    # The remaining $97.50 principal is the original $100 less $2.50 paid.
    assert get_balance(Account.objects.get(loan=loan, kind=AccountKind.PRINCIPAL_RECEIVABLE)) == Decimal("97.50")
    # Cash started at -$117.50 after disbursement and interest; the payment adds $20.
    assert get_balance(Account.objects.get(loan=None, kind=AccountKind.CASH)) == Decimal("-97.50")


def test_overpayment_is_recorded_as_borrower_credit_on_principal():
    """A $120 payment exceeds $117.50 owed, leaving a $2.50 principal credit."""
    loan = _loan()

    txn = record_repayment(
        loan=loan, amount=Decimal("120.00"), effective_on=date(2026, 1, 7)
    )

    principal = Account.objects.get(loan=loan, kind=AccountKind.PRINCIPAL_RECEIVABLE)
    # $120 - $5 fees - $12.50 interest = $102.50 applied to $100 principal.
    # The negative receivable is the $2.50 credit owed back to the borrower.
    assert get_balance(principal) == Decimal("-2.50")
    # Even though the payment exceeds the debt, the receipt and allocation still balance.
    assert sum(posting.amount for posting in txn.postings.all()) == Decimal("0.00")


def test_amount_with_only_trailing_zero_beyond_cents_is_accepted():
    """Extra Decimal scale is okay when the numeric value is still whole cents."""
    loan = _loan()

    txn = record_repayment(
        loan=loan, amount=Decimal("1.000"), effective_on=date(2026, 1, 7)
    )

    cash_posting = txn.postings.get(account__kind=AccountKind.CASH)
    # Decimal('1.000') represents exactly $1.00, so no rounding is needed.
    assert cash_posting.amount == Decimal("1.00")


@pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-1.00"), Decimal("1.001")])
def test_invalid_amount_is_rejected_without_recording(amount):
    """Zero, negative, and sub-cent receipts cannot create ledger transactions."""
    loan = _loan()
    before = loan.transactions.count()

    with pytest.raises(ValueError):
        record_repayment(loan=loan, amount=amount, effective_on=date(2026, 1, 7))

    assert loan.transactions.count() == before
