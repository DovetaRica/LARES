"""Explicit entry point. Importing this module does not open a database."""
import argparse


def create_app():
    from .runtime import app
    return app


def main():
    parser = argparse.ArgumentParser(description='LARES local application preview')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--demo', action='store_true', help='Temporary synthetic data; networking disabled')
    args = parser.parse_args()
    import uvicorn
    if args.demo:
        import os
        import tempfile
        import logging
        from pathlib import Path
        from .database import Database
        from .demo import seed
        with tempfile.TemporaryDirectory(prefix='lares-preview-') as directory:
            path = Path(directory)
            os.environ.update(DATABASE_PATH=str(path/'demo.db'), LOG_PATH=str(path/'demo.log'),
                              BACKUP_DIR=str(path/'backups'), LARES_ENABLE_NETWORK='false',
                              LARES_ENABLE_SCHEDULE='false', THIRD_PARTY_ENABLED='false',
                              HA_TOKEN='', LEARNING_ONLY='true')
            seed(Database(path/'demo.db'))
            try:
                uvicorn.run('app.main:create_app', factory=True, host='127.0.0.1', port=args.port)
            finally:
                logging.shutdown()
    else:
        uvicorn.run('app.main:create_app', factory=True, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
