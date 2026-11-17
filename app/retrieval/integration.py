"""Lazy opt-in adapter; no migrations, indexing or provider calls at startup."""
from functools import lru_cache
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from app.core.auth import validate_mailbox_context
from app.retrieval.postgres import PostgresIndex


@lru_cache(maxsize=2)
def index_engine(dsn):
    if not dsn.startswith('postgresql+psycopg://'):
        raise ValueError('Use a dedicated PostgreSQL psycopg retrieval database')
    url=make_url(dsn)
    if url.host not in {'127.0.0.1','localhost','::1'} and url.query.get('sslmode')!='verify-full':
        raise ValueError('Remote retrieval databases require sslmode=verify-full')
    return create_engine(dsn,hide_parameters=True,pool_pre_ping=True,pool_size=3,max_overflow=0,
                         connect_args={'connect_timeout':3})


def for_agent(agent):
    if not agent.mailbox_context:
        raise PermissionError('A server-authenticated mailbox context is required')
    def authorize():
        with agent.session_factory() as db:
            validate_mailbox_context(db,agent.mailbox_context)
    model=None
    if agent.settings.retrieval_semantic_enabled and agent.settings.retrieval_local_model_path:
        try:
            from app.retrieval.embedding import LocalE5
            model=local_model(agent.settings.retrieval_local_model_path)
        except (ImportError,OSError,ValueError):
            pass  # optional offline model is absent; lexical path remains usable
    key=(Path(agent.settings.retrieval_content_key_file).read_bytes()
         if agent.settings.retrieval_content_enabled and agent.settings.retrieval_content_key_file else None)
    return PostgresIndex(index_engine(agent.settings.retrieval_database_url),agent.mailbox_context,authorize,
        content_key=key,semantic=agent.settings.retrieval_semantic_enabled,embedder=model,
        stale_seconds=agent.settings.freshness_sla_minutes*60)


@lru_cache(maxsize=1)
def local_model(path):
    from app.retrieval.embedding import LocalE5
    return LocalE5(path)


def provision_for_agent(agent):
    """Explicit owner-scoped provisioning; never called implicitly by a search."""
    index=for_agent(agent)
    index.register(content_enabled=agent.settings.retrieval_content_enabled)
    return index


def sync_for_agent(agent):
    from app.retrieval.sync import IndexSynchronizer, GmailIndexSource
    index=agent.retrieval_index or for_agent(agent)
    return IndexSynchronizer(index,GmailIndexSource(agent._gmail())).run(max_pages=2,page_size=50)
