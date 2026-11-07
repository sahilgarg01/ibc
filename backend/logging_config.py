"""Keep polling quiet while retaining failed requests and import progress."""
import logging


class PollingAccessFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _, method, path, _, status = args
            route = str(path).split('?', 1)[0]
            polling = route in {'/api/import-files', '/api/import-history', '/api/scheduler', '/api/imports'} or route.startswith('/api/imports/')
            if method == 'GET' and polling and 200 <= int(status) < 300:
                return False
        return True


def configure_logging():
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(item, PollingAccessFilter) for item in access.filters):
        access.addFilter(PollingAccessFilter())
    logger = logging.getLogger('ibc.import')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
    logger.propagate = False
