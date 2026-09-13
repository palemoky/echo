"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-09-13 17:15:00.140115
"""

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('track_all', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'external_id')
    )
    op.create_table('topics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('severity', sa.Integer(), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_topics_account_id'), 'topics', ['account_id'], unique=False)
    op.create_index('ix_topics_embedding', 'topics', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_table('works',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('comments_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('latest_comment_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('danmaku_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'external_id')
    )
    op.create_index(op.f('ix_works_account_id'), 'works', ['account_id'], unique=False)
    op.create_table('feedback',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('work_id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('parent_external_id', sa.String(length=128), nullable=True),
    sa.Column('author_hash', sa.String(length=32), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('rating', sa.Float(), nullable=True),
    sa.Column('likes', sa.Integer(), nullable=True),
    sa.Column('app_version', sa.String(length=64), nullable=True),
    sa.Column('progress_ms', sa.Integer(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.Column('sentiment', sa.String(length=16), nullable=True),
    sa.Column('labeled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('label_model', sa.String(length=128), nullable=True),
    sa.ForeignKeyConstraint(['work_id'], ['works.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'kind', 'external_id')
    )
    op.create_index('ix_feedback_created', 'feedback', ['created_at'], unique=False)
    op.create_index('ix_feedback_embedding', 'feedback', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('ix_feedback_work_created', 'feedback', ['work_id', 'created_at'], unique=False)
    op.create_table('snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('work_id', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(length=64), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['work_id'], ['works.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'work_id', 'label', postgresql_nulls_not_distinct=True)
    )
    op.create_index(op.f('ix_snapshots_account_id'), 'snapshots', ['account_id'], unique=False)
    op.create_table('sync_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('work_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='running', nullable=False),
    sa.Column('new_items', sa.Integer(), server_default='0', nullable=False),
    sa.Column('seen_items', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['work_id'], ['works.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('feedback_topics',
    sa.Column('feedback_id', sa.BigInteger(), nullable=False),
    sa.Column('topic_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['feedback_id'], ['feedback.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('feedback_id', 'topic_id')
    )
    op.create_index(op.f('ix_feedback_topics_topic_id'), 'feedback_topics', ['topic_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_feedback_topics_topic_id'), table_name='feedback_topics')
    op.drop_table('feedback_topics')
    op.drop_table('sync_runs')
    op.drop_index(op.f('ix_snapshots_account_id'), table_name='snapshots')
    op.drop_table('snapshots')
    op.drop_index('ix_feedback_work_created', table_name='feedback')
    op.drop_index('ix_feedback_embedding', table_name='feedback', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_index('ix_feedback_created', table_name='feedback')
    op.drop_table('feedback')
    op.drop_index(op.f('ix_works_account_id'), table_name='works')
    op.drop_table('works')
    op.drop_index('ix_topics_embedding', table_name='topics', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_index(op.f('ix_topics_account_id'), table_name='topics')
    op.drop_table('topics')
    op.drop_table('accounts')
