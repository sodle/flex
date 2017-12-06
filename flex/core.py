import os
import yaml
import sys
import inspect
import collections
from StringIO import StringIO
from flask import current_app, json, _app_ctx_stack, request as flask_request
from werkzeug.local import LocalProxy
from jinja2 import BaseLoader, ChoiceLoader, TemplateNotFound
from datetime import datetime
import aniso8601
from functools import wraps, partial
from . import logger
from .convert import to_date, to_time, to_timedelta

_converters = {'date': to_date, 'time': to_time, 'timedelta': to_timedelta}


def find_flex():
    if hasattr(current_app, 'flex'):
        return getattr(current_app, 'flex')
    else:
        if hasattr(current_app, 'blueprints'):
            blueprints = getattr(current_app, 'blueprints')
            for blueprint_name in blueprints:
                if hasattr(blueprints[blueprint_name], 'flex'):
                    return getattr(blueprints[blueprint_name], 'flex')


def dbgdump(obj):
    if current_app.config.get('FLEX_PRETTY_DEBUG_LOGS', False):
        indent = 2
    else:
        indent = None
    msg = json.dumps(obj, indent=indent)
    logger.debug(msg)


request = LocalProxy(lambda: find_flex().request)
session = LocalProxy(lambda: find_flex().session)
version = LocalProxy(lambda: find_flex().version)

from . import models


class Flex(object):
    """The Flex object provides the central interface for interacting with the Lex service.

    Flex object maps Lex Requests to flask view functions and handles Lex sessions.
    The constructor is passed a Flask App instance, and URL endpoint.
    The Flask instance allows the convienient API of endpoints and their view functions,
    so that Alexa requests may be mapped with syntax similar to a typical Flask server.
    Route provides the entry point for the skill, and must be provided if an app is given.

    Keyword Arguments:
        app {Flask object} -- App instance - created with Flask(__name__) (default: {None})
        route {str} -- entry point to which initial Lex Requests are forwarded (default: {None})
        blueprint {Flask blueprint} -- Flask Blueprint instance to use instead of Flask App (default: {None})
        path {str} -- path to templates yaml file for VUI dialog (default: {'templates.yaml'})
    """

    def __init__(self, app=None, route=None, blueprint=None, path='templates.yaml'):
        self.app = app
        self._route = route
        self._intent_view_funcs = {}
        self._intent_converts = {}
        self._intent_defaults = {}
        self._intent_mappings = {}
        self._session_ended_view_func = None
        self._default_intent_view_func = None
        if app is not None:
            self.init_app(app, path)
        elif blueprint is not None:
            self.init_blueprint(blueprint, path)

    def init_app(self, app, path='templates.yaml'):
        """Initializes Flex app by setting configuration variables, loading templates, and maps Flex route to a flask view.

        The Flex instance is given the following configuration variables by calling on Flask's configuration:

        'FLEX_PRETTY_DEBUG_LOGS':

            Adds tabs and linebreaks to the Lex request and response printed to the debug log.
            This improves readability when printing to the console, but breaks formatting when logging to CloudWatch.
            Default: False
        """

        if self._route is None:
            raise TypeError("route is a required argument when app is not None")

        app.flex = self

        app.add_url_rule(self._route, view_func=self._flask_view_func, methods=['POST'])
        app.jinja_loader = ChoiceLoader([app.jinja_loader, YamlLoader(app, path)])

    def init_blueprint(self, blueprint, path='templates.yaml'):
        """Initialize a Flask Blueprint, similar to init_app, but without the access
        to the application config.

        Keyword Arguments:
            blueprint {Flask Blueprint} -- Flask Blueprint instance to initialize (Default: {None})
            path {str} -- path to templates yaml file, relative to Blueprint (Default: {'templates.yaml'})
        """
        if self._route is not None:
            raise TypeError("route cannot be set when using blueprints!")

        # we need to tuck our reference to this Flex instance into the blueprint object and find it later!
        blueprint.flex = self

        # BlueprintSetupState.add_url_rule gets called underneath the covers and
        # concats the rule string, so we should set to an empty string to allow
        # Blueprint('blueprint_api', __name__, url_prefix="/flex") to result in
        # exposing the rule at "/flex" and not "/flex/".
        blueprint.add_url_rule("", view_func=self._flask_view_func, methods=['POST'])
        blueprint.jinja_loader = ChoiceLoader([YamlLoader(blueprint, path)])

    def on_session_started(self, f):
        """Decorator to call wrapped function upon starting a session.

        @flex.on_session_started
        def new_session():
            log.info('new session started')

        Because both launch and intent requests may begin a session, this decorator is used call
        a function regardless of how the session began.

        Arguments:
            f {function} -- function to be called when session is started.
        """
        self._on_session_started_callback = f

    def session_ended(self, f):
        """Decorator routes Lex Close request to the wrapped view function to end the skill.

        @flex.session_ended
        def session_ended():
            return "{}", 200
        The wrapped function is registered as the session_ended view function
        and renders the response for requests to the end of the session.

        Arguments:
            f {function} -- session_ended view function
        """
        self._session_ended_view_func = f

        @wraps(f)
        def wrapper(*args, **kw):
            self._flask_view_func(*args, **kw)

        return f

    def intent(self, intent_name, mapping={}, convert={}, default={}):
        """Decorator routes a Lex IntentRequest and provides the slot parameters to the wrapped function.

        Functions decorated as an intent are registered as the view function for the Intent's URL,
        and provide the backend responses to give your Skill its functionality.

        @flex.intent('WeatherIntent', mapping={'city': 'City'})
        def weather(city):
            return statement('I predict great weather for {}'.format(city))

        Arguments:
            intent_name {str} -- Name of the intent request to be mapped to the decorated function

        Keyword Arguments:
            mapping {dict} -- Maps parameters to intent slots of a different name
                default: {}
            convert {dict} -- Converts slot values to data types before assignment to parameters
                default: {}
            default {dict} --  Provides default values for Intent slots if Lex request
                returns no corresponding slot, or a slot with an empty value
                default: {}
        """

        def decorator(f):
            self._intent_view_funcs[intent_name] = f
            self._intent_mappings[intent_name] = mapping
            self._intent_converts[intent_name] = convert
            self._intent_defaults[intent_name] = default

            @wraps(f)
            def wrapper(*args, **kw):
                self._flask_view_func(*args, **kw)

            return f

        return decorator

    def default_intent(self, f):
        """Decorator routes any Lex IntentRequest that is not matched by any existing @flex.intent routing."""
        self._default_intent_view_func = f

        @wraps(f)
        def wrapper(*args, **kw):
            self._flask_view_func(*args, **kw)

        return f

    @property
    def current_intent(self):
        return getattr(_app_ctx_stack.top, '_flex_intent', None)

    @current_intent.setter
    def current_intent(self, value):
        _app_ctx_stack.top._flex_intent = value

    @property
    def bot(self):
        return getattr(_app_ctx_stack.top, '_flex_bot', {})

    @bot.setter
    def bot(self, value):
        _app_ctx_stack.top._flex_bot = value

    @property
    def version(self):
        return getattr(_app_ctx_stack.top, '_flex_version', None)

    @version.setter
    def version(self, value):
        _app_ctx_stack.top._flex_version = value

    @property
    def user(self):
        return getattr(_app_ctx_stack.top, '_flex_user', None)

    @user.setter
    def user(self, value):
        _app_ctx_stack.top._flex_user = value

    @property
    def transcript(self):
        return getattr(_app_ctx_stack.top, '_flex_transcript', None)

    @transcript.setter
    def transcript(self, value):
        _app_ctx_stack.top._flex_transcript = value

    @property
    def source(self):
        return getattr(_app_ctx_stack.top, '_flex_source', None)

    @source.setter
    def source(self, value):
        _app_ctx_stack.top._flex_source = value

    @property
    def output_mode(self):
        return getattr(_app_ctx_stack.top, '_flex_output_mode', None)

    @output_mode.setter
    def output_mode(self, value):
        _app_ctx_stack.top._flex_output_mode = value

    @property
    def session(self):
        return getattr(_app_ctx_stack.top, '_flex_session', {})

    @session.setter
    def session(self, value):
        _app_ctx_stack.top._flex_session = value

    @property
    def request(self):
        return getattr(_app_ctx_stack.top, '_flex_request', {})

    @request.setter
    def request(self, value):
        _app_ctx_stack.top._flex_request = value

    def run_aws_lambda(self, event):
        """Invoke the Flex application from an AWS Lamnda function handler.

        Use this method to service AWS Lambda requests from a custom Lex
        bot. This method will invoke your Flask application providing a
        WSGI-compatible environment that wraps the original Lex event
        provided to the AWS Lambda handler. Returns the output generated by
        a Flex application, which should be used as the return value
        to the AWS Lambda handler function.

        Example usage:

            from flask import Flask
            from flex import Flex, statement

            app = Flask(__name__)
            flex = Flex(app, '/')

            # This function name is what you defined when you create an
            # AWS Lambda function. By default, AWS calls this function
            # lambda_handler.
            def lambda_handler(event, _context):
                return flex.run_aws_lambda(event)

            @flex.intent('HelloIntent')
            def hello(firstname):
                speech_text = "Hello %s" % firstname
                return statement(speech_text).simple_card('Hello', speech_text)
        """

        # Convert an environment variable to a WSGI "bytes-as-unicode" string
        enc, esc = sys.getfilesystemencoding(), 'surrogateescape'

        def unicode_to_wsgi(u):
            return u.encode(enc, esc).decode('iso-8859-1')

        # Create a WSGI-compatible environ that can be passed to the
        # application. It is loaded with the OS environment variables,
        # mandatory CGI-like variables, as well as the mandatory WSGI
        # variables.
        environ = {k: unicode_to_wsgi(v) for k, v in os.environ.items()}
        environ['REQUEST_METHOD'] = 'POST'
        environ['PATH_INFO'] = '/'
        environ['SERVER_NAME'] = 'AWS-Lambda'
        environ['SERVER_PORT'] = '80'
        environ['SERVER_PROTOCOL'] = 'HTTP/1.0'
        environ['wsgi.version'] = (1, 0)
        environ['wsgi.url_scheme'] = 'http'
        environ['wsgi.errors'] = sys.stderr
        environ['wsgi.multithread'] = False
        environ['wsgi.multiprocess'] = False
        environ['wsgi.run_once'] = True

        # Convert the event provided by the AWS Lambda handler to a JSON
        # string that can be read as the body of a HTTP POST request.
        body = json.dumps(event)
        environ['CONTENT_TYPE'] = 'application/json'
        environ['CONTENT_LENGTH'] = len(body)
        environ['wsgi.input'] = StringIO(body)

        # Start response is a required callback that must be passed when
        # the application is invoked. It is used to set HTTP status and
        # headers. Read the WSGI spec for details (PEP3333).
        headers = []

        def start_response(status, response_headers, _exc_info=None):
            headers[:] = [status, response_headers]

        # Invoke the actual Flask application providing our environment,
        # with our Lex event as the body of the HTTP request, as well
        # as the callback function above. The result will be an iterator
        # that provides a serialized JSON string for our Alexa response.
        result = self.app(environ, start_response)
        try:
            if not headers:
                raise AssertionError("start_response() not called by WSGI app")

            output = b"".join(result)
            if not headers[0].startswith("2"):
                raise AssertionError("Non-2xx from app: hdrs={}, body={}".format(headers, output))

            # The Lambda handler expects a Python object that can be
            # serialized as JSON, so we need to take the already serialized
            # JSON and deserialize it.
            return json.loads(output)

        finally:
            # Per the WSGI spec, we need to invoke the close method if it
            # is implemented on the result object.
            if hasattr(result, 'close'):
                result.close()

    def _get_user(self):
        return self.user

    @staticmethod
    def _lex_request():
        raw_body = flask_request.data
        lex_request_payload = json.loads(raw_body)

        return lex_request_payload

    @staticmethod
    def _parse_timestamp(timestamp):
        """
        Parse a given timestamp value, raising ValueError if None or Falsey
        """
        if timestamp:
            try:
                return aniso8601.parse_datetime(timestamp)
            except AttributeError:
                # raised by aniso8601 if raw_timestamp is not valid string
                # in ISO8601 format
                try:
                    return datetime.utcfromtimestamp(timestamp)
                except:
                    # relax the timestamp a bit in case it was sent in millis
                    return datetime.utcfromtimestamp(timestamp / 1000)

        raise ValueError('Invalid timestamp value! Cannot parse from either ISO8601 string or UTC timestamp.')

    def _flask_view_func(self, *args, **kwargs):
        flex_payload = self._lex_request()
        dbgdump(flex_payload)
        request_body = models._Field(flex_payload)

        self.current_intent = request_body.currentIntent
        self.bot = request_body.bot
        self.user = request_body.userId
        self.transcript = request_body.inputTranscript
        self.source = request_body.invocationSource
        self.output_mode = request_body.outputDialogMode
        self.version = request_body.messageVersion
        self.session = request_body.sessionAttributes
        self.request = request_body.requestAttributes

        result = None

        if self._intent_view_funcs:
            result = self._map_intent_to_view_func(self.current_intent)()

        if result is not None:
            if isinstance(result, models._Response):
                return result.render_response()
            return result
        return '', 400

    def _map_intent_to_view_func(self, intent):
        """Provides appropriate parameters to the intent functions."""
        if intent.name in self._intent_view_funcs:
            view_func = self._intent_view_funcs[intent.name]
        elif self._default_intent_view_func is not None:
            view_func = self._default_intent_view_func
        else:
            raise NotImplementedError('Intent "{}" not found and no default intent specified.'.format(intent.name))

        argspec = inspect.getargspec(view_func)
        arg_names = argspec.args
        arg_values = self._map_params_to_view_args(intent.name, arg_names)

        return partial(view_func, *arg_values)

    def _map_params_to_view_args(self, view_name, arg_names):
        arg_values = []
        convert = self._intent_converts.get(view_name)
        default = self._intent_defaults.get(view_name)
        mapping = self._intent_mappings.get(view_name)

        convert_errors = {}

        request_data = {}
        intent = self.current_intent
        if intent.slots is not None:
            for slot_key in intent.slots.keys():
                slot_object = getattr(intent.slots, slot_key)
                request_data[slot_key] = slot_object

        for arg_name in arg_names:
            param_or_slot = mapping.get(arg_name, arg_name)
            arg_value = request_data.get(param_or_slot)
            if arg_value is None or arg_value == '':
                if arg_name in default:
                    default_value = default[arg_name]
                    if isinstance(default_value, collections.Callable):
                        default_value = default_value()
                    arg_value = default_value
            elif arg_name in convert:
                shorthand_or_function = convert[arg_name]
                if shorthand_or_function in _converters:
                    shorthand = shorthand_or_function
                    convert_func = _converters[shorthand]
                else:
                    convert_func = shorthand_or_function
                try:
                    arg_value = convert_func(arg_value)
                except Exception as e:
                    convert_errors[arg_name] = e
            arg_values.append(arg_value)
        self.convert_errors = convert_errors
        return arg_values


class YamlLoader(BaseLoader):

    def __init__(self, app, path):
        self.path = app.root_path + os.path.sep + path
        self.mapping = {}
        self._reload_mapping()

    def _reload_mapping(self):
        if os.path.isfile(self.path):
            self.last_mtime = os.path.getmtime(self.path)
            with open(self.path) as f:
                self.mapping = yaml.safe_load(f.read())

    def get_source(self, environment, template):
        if not os.path.isfile(self.path):
            return None, None, None
        if self.last_mtime != os.path.getmtime(self.path):
            self._reload_mapping()
        if template in self.mapping:
            source = self.mapping[template]
            return source, None, lambda: source == self.mapping.get(template)
        return TemplateNotFound(template)
