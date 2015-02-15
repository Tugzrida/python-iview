import http.server
from urllib.parse import urlparse
from http.client import (
    REQUEST_URI_TOO_LONG, REQUEST_HEADER_FIELDS_TOO_LARGE,
    METHOD_NOT_ALLOWED, BAD_REQUEST, FORBIDDEN,
)
from http.client import NOT_IMPLEMENTED, INTERNAL_SERVER_ERROR
from http.client import OK
import email.parser
import urllib.parse
from .utils import SelectableHandler

class RequestHandler(SelectableHandler, http.server.BaseHTTPRequestHandler):
    def handle_one_request(self):
        self.close_connection = True  # Required by base class
        self.response_started = False
        self.requestline = "-"  # Required by base class
        self.request_version = None
        try:
            try:
                # RFC 7230 recommends a minimum limit of 8000 octets
                request = self.rfile.readline(8000 + 1)
                if not request:
                    return
                if len(request) > 8000:
                    msg = "Request line too long"
                    raise ErrorResponse(REQUEST_URI_TOO_LONG, msg)
                
                words = request.split(maxsplit=1)
                if not words:
                    self.close_connection = False
                    return
                
                self.command = words[0]
                if len(words) < 2:
                    words = (b"",)
                else:
                    words = words[1].rsplit(maxsplit=1) # TODO: 301 if space, to help downstream proxies
                self.path = words[0]
                if len(words) < 2:
                    protocol = None
                else:
                    version = words[1]
                    protocol = version.rsplit(b"/", 1)[0]
                encoding = self.get_encoding(protocol)
                try:
                    self.requestline = request.strip().decode(encoding)
                    self.command = self.command.decode(encoding)
                    self.path = self.path.decode(encoding)
                    if protocol is not None:
                        self.request_version = version.decode(encoding)
                except ValueError as err:
                    raise ErrorResponse(BAD_REQUEST, err)
                
                self.plainpath = urlparse(self.path).path
                if self.plainpath == "*":
                    self.plainpath = None
                
                parser = email.parser.BytesFeedParser(
                    _factory=self.MessageClass)
                for _ in range(200):
                    line = self.rfile.readline(1000 + 1)
                    if len(line) > 1000:
                        code = REQUEST_HEADER_FIELDS_TOO_LARGE
                        msg = "Request header line too long"
                        raise ErrorResponse(code, msg)
                    parser.feed(line)
                    if not line.rstrip(b"\r\n"):
                        break
                else:
                    msg = "Request header too long"
                    raise ErrorResponse(REQUEST_HEADER_FIELDS_TOO_LARGE, msg)
                self.headers = parser.close()
                
                self.close_connection = False
                self.handle_method()
            
            except ErrorResponse as resp:
                self.send_error(resp.code, resp.message)
        except Exception as err:
            self.server.handle_error(self.request, self.client_address)
            if not self.response_started:
                self.send_error(INTERNAL_SERVER_ERROR, err)
        if self.response_started:
            self.close_connection = True
    
    def get_encoding(self, protocol):
        return "latin-1"
    
    def handle_method(self):
        handler = getattr(self, "do_" + self.command, self.handle_request)
        handler()
    
    allow_codes = {METHOD_NOT_ALLOWED}  # Required by specification
    
    def send_error(self, code, message=None):
        self.send_response(code, message)
        if self.close_connection:
            self.send_header("Connection", "close")
        if code in self.allow_codes:
            self.send_allow()
        self.end_headers()
    
    def send_response(self, *pos, **kw):
        self.response_started = True
        http.server.BaseHTTPRequestHandler.send_response(self, *pos, **kw)
    
    def end_headers(self, *pos, **kw):
        http.server.BaseHTTPRequestHandler.end_headers(self, *pos, **kw)
        self.response_started = False
    
    def handle_request(self):
        msg = 'Request method "{}" not implemented'.format(self.command)
        self.send_response(NOT_IMPLEMENTED, msg)
        self.send_public()
        self.end_headers()
    
    def do_HEAD(self):
        raise ErrorResponse(FORBIDDEN)
    do_GET = do_HEAD
    
    def send_entity(self, type, data):
        self.send_response(OK)
        self.send_header("Content-Type", type)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)
    
    def parse_path(self):
        path = self.plainpath
        if not path:
            msg = "Method {} does not accept null path".format(self.command)
            raise ErrorResponse(METHOD_NOT_ALLOWED, msg)
        
        if path.startswith("/"):
            path = path[1:]
        self.parsedpath = list()
        emptyfile = ("",)  # Remember if normal path ends with a slash
        for elem in path.split("/"):
            emptyfile = ("",)  # Default unless special value not found
            if elem == "..":
                if self.parsedpath:
                    self.parsedpath.pop()
            elif elem not in {"", "."}:
                elem = urllib.parse.unquote(elem,
                    "ascii", "surrogateescape")
                self.parsedpath.append(elem)
                emptyfile = ()
        self.parsedpath.extend(emptyfile)
    
    def send_public(self):
        methods = list()
        for method in dir(self):
            if method.startswith("do_"):
                methods.append(method[3:])
        self.send_header("Public", ", ".join(methods))

class ErrorResponse(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message
        Exception.__init__(self, self.code)