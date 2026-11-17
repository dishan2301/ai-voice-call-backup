"""Explicit retrieval migration command; never loads mailbox credentials or syncs mail."""
import argparse
import os
from sqlalchemy import create_engine
from app.retrieval.migrate import migrate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--migrate',action='store_true',required=True)
    parser.add_argument('--vectors',action='store_true')
    args=parser.parse_args()
    dsn=os.environ.get('M8_MIGRATION_DATABASE_URL','')
    if not dsn.startswith('postgresql+psycopg://'):
        parser.error('Set a dedicated M8_MIGRATION_DATABASE_URL for the index')
    engine=create_engine(dsn,hide_parameters=True,connect_args={'connect_timeout':3})
    try:
        print({'applied':migrate(engine,vectors=args.vectors)})
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
