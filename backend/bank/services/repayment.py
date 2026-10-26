"""Recording a repayment against a loan.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  THIS IS YOUR TASK (back-end Feature Slice).                         │
    │                                                                     │
    │  A repayment arrives against a loan. Accept it, allocate it across   │
    │  the loan's balances in the order the domain requires, and record   │
    │  the result so the loan's accounting stays consistent.              │
    │                                                                     │
    │  What's already here for you:                                       │
    │    • the amounts owed, per bucket, via selectors + accrual          │
    │    • the waterfall allocation itself, in money.allocate_waterfall   │
    │    • the way this codebase writes balanced postings, in             │
    │      services.transactions.record_transaction                       │
    │                                                                     │
    │  You decide what "consistent" has to mean, and what happens at the  │
    │  edges. Read GLOSSARY.md first.                                      │
    │                                                                     │
    │  Scope: this service function is the task. You do NOT need to add   │
    │  an HTTP endpoint — exposing it over the API is out of scope.       │
    └─────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction as db_transaction

from ..models import Account, AccountKind, Loan, Posting, Transaction
from ..money import CENT, ZERO, allocate_waterfall
from ..selectors import get_balance
from .transactions import record_transaction


def record_repayment(*, loan: Loan, amount: Decimal, effective_on: date) -> Transaction:
    """Record a borrower's repayment against ``loan``.

    Returns the Transaction that was written.

    The payment is applied to fees, interest, then principal, as required by
    the repayment waterfall. Any excess after those buckets is also assigned
    to principal, so an overpayment leaves a negative principal receivable (a
    credit in the borrower's favour).

    Amounts must be positive and represent whole cents. Extra trailing zeroes
    are harmless (``Decimal("1.000")`` is one dollar); sub-cent amounts are
    rejected rather than silently rounded, which could misstate cash received.
    """
    # The ledger stores money in Decimal fields. Reject floats (and NaN/infinity)
    # rather than letting an imprecise or non-monetary value enter the ledger.
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise ValueError("amount must be a finite Decimal")
    if amount <= ZERO:
        raise ValueError("amount must be positive")
    # Compare values, not Decimal exponents: 1.000 equals 1.00 and is valid,
    # while 1.001 cannot be represented in whole cents without rounding.
    if amount != amount.quantize(CENT):
        raise ValueError("amount must be a whole-cent amount")

    # Serialize repayments on this loan so two concurrent payments cannot both
    # allocate against the same pre-payment balances.
    with db_transaction.atomic():
        locked_loan = Loan.objects.select_for_update().get(pk=loan.pk)
        accounts = {
            kind: Account.objects.get(loan=locked_loan, kind=kind)
            for kind in (
                AccountKind.FEES_RECEIVABLE,
                AccountKind.INTEREST_RECEIVABLE,
                AccountKind.PRINCIPAL_RECEIVABLE,
            )
        }
        # The global cash account is also initialized by disburse().
        cash = Account.objects.get(loan=None, kind=AccountKind.CASH)

        # Allocate the payment across the loan's balances in the order required by
        # the repayment waterfall. The allocation may exceed the amounts owed
        allocation = allocate_waterfall(
            amount,
            fees_due=get_balance(accounts[AccountKind.FEES_RECEIVABLE]),
            interest_due=get_balance(accounts[AccountKind.INTEREST_RECEIVABLE]),
            principal_due=get_balance(accounts[AccountKind.PRINCIPAL_RECEIVABLE]),
        )

        # Cash is debited by the received amount. 
        postings = [Posting(account=cash, amount=amount)]
        for kind, applied in (
            (AccountKind.FEES_RECEIVABLE, allocation.fees),
            (AccountKind.INTEREST_RECEIVABLE, allocation.interest),
            (AccountKind.PRINCIPAL_RECEIVABLE, allocation.principal),
        ):
            # Avoid zero-value ledger lines; an overpayment naturally makes
            # the principal credit larger than the amount currently owed.
            if applied:
                postings.append(Posting(account=accounts[kind], amount=-applied))

        return record_transaction(
            memo=f"Repayment of loan #{locked_loan.pk}",
            effective_on=effective_on,
            postings=postings,
            loan=locked_loan,
        )
