import logging
import logging.handlers
import pkg_resources
import socket


class ContextFilter(logging.Filter):
    hostname = socket.gethostname()
    version = pkg_resources.get_distribution('rpi_metar').version

    def filter(self, record):
        record.hostname = ContextFilter.hostname
        record.version = ContextFilter.version
        return True


def init_logger():

    log = logging.getLogger(__name__)

    ctx_filter = ContextFilter()

    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(version)s - %(message)s')
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    handler.setFormatter(formatter)
    handler.setFilter(ctx_filter)
    log.addHandler(handler)

    papertrail = logging.handlers.SysLogHandler(address=('logs2.papertrailapp.com', 43558))
    formatter = logging.Formatter(
        '%(asctime)s %(hostname)s rpi_metar: %(levelname)s %(version)s %(message)s',
        datefmt='%b %d %H:%M:%S'
    )

    papertrail.setFormatter(formatter)
    papertrail.setLevel(logging.INFO)
    papertrail.setFilter(ctx_filter)
    log.addHandler(papertrail)


init_logger()
