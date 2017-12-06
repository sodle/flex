import aniso8601
from xml.etree import ElementTree
from flask import json

from .core import session, dbgdump


class _Field(dict):
    """Container to represent Lex Request Data.

    Initialized with request_json and creates a dict object with attributes
    to be accessed via dot notation or as a dict key-value.

    Parameters within the request_json that contain their data as a json object
    are also represented as a _Field object.

    Example:

    payload_object = _Field(lex_json_payload)

    request_type_from_keys = payload_object['request']['type']
    request_type_from_attrs = payload_object.request.type

    assert request_type_from_keys == request_type_from_attrs
    """

    def __init__(self, request_json={}):
        super(_Field, self).__init__(request_json)
        for key, value in request_json.items():
            if isinstance(value, dict):
                value = _Field(value)
            self[key] = value

    def __getattr__(self, attr):
        # converts timestamp str to datetime.datetime object
        if 'timestamp' in attr:
            return aniso8601.parse_datetime(self.get(attr))
        return self.get(attr)

    def __setattr__(self, key, value):
        self.__setitem__(key, value)


class _Response(object):

    def __init__(self, action):
        self._json_default = None
        self._response = {
            'dialogAction': {
                'type': action
            }
        }

    def message(self, message):
        self._response['dialogAction']['message'] = _message(message)
        return self

    def response_card(self, title=None, subtitle=None, image_url=None, attachment_url=None, buttons=None):
        if 'responseCard' not in self._response:
            self._response['responseCard'] = {
                'version': 1,
                'contentType': 'application/vnd.amazonaws.card.generic',
                'genericAttachments': []
            }

        attachment = {}

        if title is not None:
            attachment['title'] = title

        if subtitle is not None:
            attachment['subTitle'] = subtitle

        if image_url is not None:
            attachment['imageUrl'] = image_url

        if attachment_url is not None:
            attachment['attachmentLinkUrl'] = attachment_url

        if buttons is not None:
            attachment['buttons'] = buttons

        self._response['responseCard']['genericAttachments'].append(attachment)
        return self

    def render_response(self):
        response_wrapper = self._response
        response_wrapper['sessionAttributes'] = dict(session)

        dbgdump(response_wrapper)

        return json.dumps(response_wrapper)


class close(_Response):

    def __init__(self, fulfilled):
        super(close, self).__init__('Close')
        self._response['dialogAction']['fulfillmentState'] = 'Fulfilled' if fulfilled else 'Failed'


class confirm_intent(_Response):

    def __init__(self, intent_name, slots):
        super(confirm_intent, self).__init__('ConfirmIntent')
        self._response['dialogAction']['intentName'] = intent_name
        self._response['dialogAction']['slots'] = slots


class delegate(_Response):

    def __init__(self, slots):
        super(delegate, self).__init__('Delegate')
        self._response['dialogAction']['slots'] = slots


class elicit_intent(_Response):

    def __init__(self):
        super(elicit_intent, self).__init__('ElicitIntent')


class elicit_slot(_Response):

    def __init__(self, intent_name, slot_to_elicit, slots):
        super(elicit_slot, self).__init__('ElicitSlot')
        self._response['dialogAction']['intentName'] = intent_name
        self._response['dialogAction']['slotToElicit'] = slot_to_elicit
        self._response['dialogAction']['slots'] = slots


def _message(message):
    try:
        xmldoc = ElementTree.fromstring(message)
        if xmldoc.tag == 'speak':
            return {'contentType': 'SSML', 'content': message}
    except (UnicodeEncodeError, ElementTree.ParseError) as e:
        pass
    return {'contentType': 'PlainText', 'content': message}
