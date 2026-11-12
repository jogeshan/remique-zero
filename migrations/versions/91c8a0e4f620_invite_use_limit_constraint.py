"""keep invite redemption counts within their configured limit"""
from typing import Sequence, Union

from alembic import op

revision: str = "91c8a0e4f620"
down_revision: Union[str, Sequence[str], None] = "d22aaa61a3f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_invite_codes_uses_within_limit", "invite_codes", "use_count <= max_uses"
    )


def downgrade() -> None:
    op.drop_constraint("ck_invite_codes_uses_within_limit", "invite_codes", type_="check")
