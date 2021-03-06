import base64
import csv
import dateutil.parser
import httplib2
import simplejson
import time
import urllib
from cStringIO import StringIO


class TimetricClient(object):
    """
    Timetric client. You'll need a config dict; the authenticated token will be
    written back to this dictionary. Obviously you should make this persistant
    in some way to avoid needing to authenticate each time.
    """
    request_token_url = 'http://timetric.com/oauth/request_token/'
    authorization_url = 'http://timetric.com/oauth/authorize/'
    access_token_url = 'http://timetric.com/oauth/access_token/'
    
    def __init__(self, config, user_agent="python-timetric"):
        self.http = httplib2.Http()
        self.http.follow_redirects = False
        self.config = config
        self.user_agent = user_agent
        self.authtype = config.get('authtype', 'oauth')
        if self.authtype == 'oauth':
            self.setup_oauth()
        elif self.authtype == 'apitoken':
            self.setup_apitokens()
        else:
            raise ValueError("Invalid Timetric auth type: '%s' "
                             "(should be 'oauth' or 'apitoken')" % self.authtype)

    def setup_oauth(self):
        from oauth import oauth # if this fails you need to `easy_install oauth`.
        self.oauth_module = oauth
        self.SIGNATURE = oauth.OAuthSignatureMethod_HMAC_SHA1()
        self.authtype = 'oauth'
        self.make_request = self.oauth_request
        try:
            self.consumer = oauth.OAuthConsumer(self.config['consumer_key'], self.config['consumer_secret'])
        except KeyError:
            raise KeyError('Config missing consumer key/secret.')
        try:
            self.access_token = oauth.OAuthToken(self.config['oauth_token'], self.config['oauth_secret'])
        except KeyError:
            self.access_token = None

    def setup_apitokens(self):
        self.authtype = 'apitoken'
        self.make_request = self.apitoken_request
        self.apitoken_key = self.config['apitoken_key']
        self.apitoken_secret = self.config['apitoken_secret']

    def series(self, id):
        """
        Get an existing data series. Fails if the client isn't successfully
        authorized.
        """
        if self.authtype == 'oauth' and not self.access_token:
            raise ValueError("Client isn't yet authorized.")
        return Series(self, id)
        
    def create_series(self, data=None, **params):
        """
        Create a new series.
    
        If given, the data may a list of values or a CSV file; see
        `Series.update` for details.
        
        The parameters are as described at http://timetric.com/help/httpapi/#series-metadata.
        """
        if 'caption' not in params or 'title' not in params:
            raise TypeError('Missing "caption" or "title" parameter')
        
        if data:
            if _is_file(data):
                files = {'csv': data}
            else:
                files = {'csv': _iterable_to_stream(data)}
        else:
            files = {}
            
        resp, body = self.post('https://timetric.com/create/', params=params, files=files)
        return Series(self, resp['location'].split('/')[-2])
        
    def get_request_token(self):
        """
        Step 0: request an OAuth token. 
        
        Called automatically by get_authorize_url() if needed.
        """
        req = self.oauth_module.OAuthRequest.from_consumer_and_token(
            self.consumer, http_url=self.request_token_url)
        req.sign_request(self.SIGNATURE, self.consumer, None)
        resp, body = self.http.request(req.to_url(), 'GET')
        return self.oauth_module.OAuthToken.from_string(body)

    def get_authorize_url(self, token=None, callback=None):
        """
        Step 1: direct the user to the authentication URL.
        """
        if not token:
            token = self.get_request_token()
        req = self.oauth_module.OAuthRequest.from_token_and_callback(
            token=token, http_url=self.authorization_url, callback=callback)
        return req.to_url()
        
    def get_access_token(self, token):
        """
        Step 2: once authorized, convert the app token to an access token.
        
        Stores the access token in the config dict for later use.
        """
        req = self.oauth_module.OAuthRequest.from_consumer_and_token(
            self.consumer, token=token, http_url=self.access_token_url)
        req.sign_request(self.SIGNATURE, self.consumer, token)
        resp, body = self.http.request(req.to_url(), 'GET')
        self.access_token = self.oauth_module.OAuthToken.from_string(body)
        self.config['oauth_token'] = self.access_token.key
        self.config['oauth_secret'] = self.access_token.secret
        return self.access_token

    def build_oauth_request(self, method, url, params):
        """
        Build a signed OAuthRequest.
        """
        if not self.access_token:
            raise ValueError("Client isn't yet authorized.")        
        req = self.oauth_module.OAuthRequest.from_consumer_and_token(
            self.consumer,
            self.access_token,
            http_method = method,
            http_url = url,
            parameters = params
        )
        req.sign_request(self.SIGNATURE, self.consumer, self.access_token)
        return req

    def get(self, url, params=None):
        """
        Make an authorized HTTP GET request. 
        
        Returns `(response_headers, body)`.
        """
        if not params:
            params = {}
        return self.make_request('GET', url, params=params)
        
    def delete(self, url, params=None):
        """
        Make an authorized HTTP DELETE request. 
        
        Returns `(response_headers, body)`.
        """
        return self.make_request('DELETE', url, params=params)
        
    def post(self, url, params=None, files=None):
        """
        Make an authorized HTTP POST request.

        Returns `(response_headers, body)`
        """
        if not params:
            params = {}
        if not files:
            files = {}
        if files:
            body = _encode_multipart(params, files)
            headers = {'Content-Type':MULTIPART_CONTENT}
        else:
            body = urllib.urlencode(params)
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        return self.make_request('POST', url, params=params, body=body, headers=headers)
        
    def put(self, url, body, content_type):
        """
        Make an authorized HTTP PUT request. 
        
        Returns `(response_headers, body)`
        """
        headers = {'Content-Type':content_type}
        return self.make_request('PUT', url, body=body, headers=headers)

    def oauth_request(self, method, url, params=None, body="", headers=None):
        if not params:
            params = {}
        if not headers:
            headers = {}
        req = self.build_oauth_request(method, url, params)
        headers.update(req.to_header())
        headers['User-Agent'] = self.user_agent
        return self.http.request(req.get_normalized_http_url(),
                                 method, body=body, headers=headers)

    def apitoken_request(self, method, url, params=None, body="", headers=None):
        if not headers:
            headers = {}
        if params:
            url += "?%s" % urllib.urlencode(params)
        auth_header = "Basic %s" % \
            base64.b64encode("%(apitoken_key)s:%(apitoken_secret)s" %
                             self.__dict__)
        headers['Authorization'] = auth_header
        headers['User-Agent'] = self.user_agent
        return self.http.request(url, method, body=body, headers=headers)


class Series(object):
    """
    A Timetric data series.
    
    Don't create directly; use TimetricClient.series().
    """
    
    def __init__(self, client, id):
        self.client = client
        self.id = id
        self.url = 'http://timetric.com/series/%s/' % self.id
    
    def __repr__(self):
        return "<timetric.Series('%s')>" % self.id
    
    def latest(self):
        """
        Get the latest value in this series. Returns a tuple `(timestamp,
        value)`.
        """
        resp, body = self.client.get(self.url + "value/json/")
        if resp.status != 200:
            raise TimetricClientError("Failed to fetch latest value: HTTP %s" % resp.status)
        data = simplejson.loads(body)
        return (data['timestamp'], data['value'])
        
    def csv(self):
        """
        Get the raw CSV data (as a string) of this series.
        """
        resp, body = self.client.get(self.url + "csv/")
        if resp.status != 200:
            raise TimetricClientError("Failed to fetch CSV: HTTP %s" % resp.status)
        return body
        
    def __iter__(self):
        return (
            (float(ts), _valueish(val))
            for (ts, val) in csv.reader(StringIO(self.csv()))
        )
    
    def __float__(self):
        return float(self.latest()[1])
        
    def __int__(self):
        return int(self.latest()[1])
        
    def update(self, value):
        """
        Update the series. 
        
        The argument can be:
        
            * A single number: update the series with the given value and a
              timestamp of "now".
            
            * An iterator yielding ``(datetime, value)`` pairs: update the
              series with the given lines of data. Datetime values can be
              datetime objects, Unix timestamps, or strings capable of being
              parsed into a datetime using ``dateutil.parser`` (which
              understands most common formats).
              
            * A file-like object containing a stream of CSV data; this data
              will be fed directly to Timetric.
        
        """
        try:
            iter(value)
        except TypeError:
            self._update_single(value)
        else:
            if not _is_file(value):
                value = _iterable_to_stream(value)
            self._update_from_file(value)
                
    def increment(self, amount):
        """
        Increment the current value by the given amount, which may be negative
        to perform a decrement.
        """
        resp, _ = self.client.post(self.url, {'increment': str(amount)})
        if resp.status != 204:
            raise TimetricClientError("Failed to incremement/decrement: HTTP %s" % resp.status)
        
    # Syntactic sugar for increment/decrement
    def __iadd__(self, amount):
        self.increment(amount)
        return self
        
    def __isub__(self, amount): 
        self.increment(-amount)
        return self

    def rewrite(self, data):
        """
        Rewrite (i.e. replace) all the data in the series with the given data.
        The data can be an iterator or a file as for `update`.
        """
        if not _is_file(data):
            data = _iterable_to_stream(data)
        resp, _ = self.client.put(self.url, data.read(), 'text/csv')
        if resp.status != 204:
            raise TimetricClientError("Failed to rewrite data: HTTP %s" % resp.status)
                                
    def delete(self):
        """
        Delete this series.
        """
        resp, _ = self.client.delete(self.url)
        if resp.status != 204:
            raise TimetricClientError("Failed to delete series: HTTP %s" % resp.status)
        
    def _update_single(self, value):
        """
        Update the series with a single value and a timestamp of now.
        """
        resp, _ = self.client.post(self.url, {'value': str(value)})
        if resp.status != 204:
            raise TimetricClientError("Failed to update: HTTP %s" % resp.status)
        
    def _update_from_file(self, file):
        """
        Update from a file-like object of CSV data.
        """
        resp, _ = self.client.post(self.url, files={'csv': file})
        if resp.status != 204:
            raise TimetricClientError("Failed to update: HTTP %s" % resp.status)

class TimetricClientError(Exception):
    pass

def _iterable_to_stream(values):
    """
    Convert an iterable of 2-tuples into a file-like object for the dataset.
    """
    io = StringIO()
    writer = csv.writer(io)
    for timestamp, value in values:
        writer.writerow([_parse_timestamp(timestamp), value])
    io.seek(0)
    return io
    
def _parse_timestamp(timestamp):
    """
    Parse a timestamp into a format that Timetric understands.
    """
    if hasattr(timestamp, 'timetuple'):
        return time.mktime(timestamp.utctimetuple())
    try:
        return float(timestamp)
    except (TypeError, ValueError):
        return time.mktime(dateutil.parser.parse(timestamp).utctimetuple())

def _valueish(val):
    """
    Try to convert something Timetric sent back to a Python value.
    """
    literals = {"null":None, "true":True, "false":False}
    v = val.lower()
    return v in literals and literals[v] or float(v)

#
# The following code is adapted from Django (django.test.client)
#
BOUNDARY = 'tHiSiStHeBoUnDaRyStRiNg'
MULTIPART_CONTENT = 'multipart/form-data; boundary=%s' % BOUNDARY

# Kind ghetto, but works.
def _is_file(thing): 
    return hasattr(thing, "read") and callable(thing.read)

def _encode_multipart(data, files):
    """
    Encodes multipart POST data from a dictionary of form values.
    """
    lines = []

    for (key, value) in data.items():
        lines.extend([
            '--' + BOUNDARY,
            'Content-Disposition: form-data; name="%s"' % str(key),
            '',
            str(value)
        ])
    for (key, value) in files.items():
        lines.extend([
            '--' + BOUNDARY,
            'Content-Disposition: form-data; name="%s"; filename="%s"' \
                % (str(key), str(key)),
            'Content-Type: application/octet-stream',
            '',
            value.read()
        ])

    lines.extend([
        '--' + BOUNDARY + '--',
        '',
    ])
    return '\r\n'.join(lines)
