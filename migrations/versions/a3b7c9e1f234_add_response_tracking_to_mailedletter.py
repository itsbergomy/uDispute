"""Add response tracking fields to MailedLetter

Revision ID: a3b7c9e1f234
Revises: 1020f8cd16e1
Create Date: 2026-03-19 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3b7c9e1f234'
down_revision = '1020f8cd16e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('mailed_letter', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_number', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('outcome', sa.String(length=20), server_default='pending', nullable=True))
        batch_op.add_column(sa.Column('response_file_url', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('response_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('response_received_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('legal_research_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('previous_letter_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_mailed_letter_previous', 'mailed_letter', ['previous_letter_id'], ['id'])


def downgrade():
    with op.batch_alter_table('mailed_letter', schema=None) as batch_op:
        batch_op.drop_constraint('fk_mailed_letter_previous', type_='foreignkey')
        batch_op.drop_column('previous_letter_id')
        batch_op.drop_column('legal_research_json')
        batch_op.drop_column('response_received_at')
        batch_op.drop_column('response_text')
        batch_op.drop_column('response_file_url')
        batch_op.drop_column('outcome')
        batch_op.drop_column('account_number')
