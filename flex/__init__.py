import logging

logger = logging.getLogger('flex')
logger.addHandler(logging.StreamHandler())
if logger.level == logging.NOTSET:
    logger.setLevel(logging.WARN)

from .core import (
    Flex,
    request,
    session,
    version
)

from .models import close, confirm_intent, delegate, elicit_intent, elicit_slot
