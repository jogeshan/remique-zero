"""add api_tokens table

Revision ID: b7e2c9d41a05
Revises: 4c7ab313eca4
Create Date: 2026-11-15 14:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c9d41a05'
down_revision: Union[str, Sequence[str], None] = '4c7ab313eca4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('api_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash'))
    )
    op.create_index('ix_api_tokens_user', 'api_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_api_tokens_user', table_name='api_tokens')
    op.drop_table('api_tokens')
