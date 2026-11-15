"""add finance tables

Revision ID: 500bc7c3f050
Revises: 7a6c81d662aa
Create Date: 2026-11-15 11:52:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '500bc7c3f050'
down_revision: Union[str, Sequence[str], None] = '7a6c81d662aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('transactions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('type', sa.String(length=10), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("type IN ('income', 'expense')", name='valid_transaction_type'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_transactions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transactions'))
    )
    op.create_index('ix_transactions_user_occurred', 'transactions', ['user_id', 'occurred_at'], unique=False)

    op.create_table('debts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('counterparty', sa.String(length=120), nullable=False),
    sa.Column('principal', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('remaining', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("direction IN ('borrowed', 'lent')", name='valid_debt_direction'),
    sa.CheckConstraint("kind IN ('debt', 'loan')", name='valid_debt_kind'),
    sa.CheckConstraint("status IN ('open', 'settled')", name='valid_debt_status'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_debts_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_debts'))
    )
    op.create_index('ix_debts_user_status', 'debts', ['user_id', 'status'], unique=False)

    op.create_table('debt_payments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('debt_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['debt_id'], ['debts.id'], name=op.f('fk_debt_payments_debt_id_debts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_debt_payments_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_debt_payments'))
    )
    op.create_index('ix_debt_payments_user_paid', 'debt_payments', ['user_id', 'paid_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_debt_payments_user_paid', table_name='debt_payments')
    op.drop_table('debt_payments')
    op.drop_index('ix_debts_user_status', table_name='debts')
    op.drop_table('debts')
    op.drop_index('ix_transactions_user_occurred', table_name='transactions')
    op.drop_table('transactions')
