from . import basehttp
from http.server import HTTPServer
from http.client import NOT_FOUND, OK
from io import BytesIO, TextIOWrapper
import subprocess
import random
from .utils import format_addr
from .utils import header_list, header_split, header_partition
import email.message
from misc import joinpath
import time
from .utils import SelectableServer
from .utils import RollbackReader
from socketserver import UDPServer, BaseRequestHandler
from struct import Struct
import urllib.parse
import json

_SESSION_DIGITS = 25

class Server(SelectableServer, HTTPServer):
    def __init__(self, address, *, ffmpeg2=True):
        """ffmpeg2: Assume FF MPEG 2.1 rather than libav 0.8.6"""
        self._ffmpeg2 = ffmpeg2
        self._sessions = dict()
        self._last_media = None
        super().__init__(address, Handler)
    
    def _get_media(self, path):
        """Returns OS media path from URL path"""
        return joinpath(path, ".")
    
    def _get_sdp(self, media):
        options = (
            "-show_entries", "format=duration : format_tags=title",
            "-print_format", "json",
            media,
        )
        ffprobe = _ffmpeg_command("ffprobe", options, stdout=subprocess.PIPE)
        with ffprobe, TextIOWrapper(ffprobe.stdout, "ascii") as metadata:
            metadata = json.load(metadata)
        if ffprobe.returncode:
            msg = "ffprobe returned exit status {}"
            raise EnvironmentError(msg.format(ffprobe.returncode))
        notitle = "title" not in metadata["format"].get("tags", dict())
        
        options = ("-t", "0")  # Stop before processing any video
        streams = ((type, None) for type in _streamtypes)
        ffmpeg = _ffmpeg(media, options, streams,
            loglevel="error",  # Avoid empty output warning caused by "-t 0"
            stdout=subprocess.PIPE,
            ffmpeg2=self._ffmpeg2,
        )
        with ffmpeg:
            sdp = BytesIO()
            line = ffmpeg.stdout.readline()
            
            # FF MPEG unhelpfully adds this prefix to its output
            if line.strip() == b"SDP:":
                line = ffmpeg.stdout.readline()
            
            streams = 0
            rtcp_bandwidth = False  # True if b=RR:0 written
            while line:
                type = line.strip()[:1]  # Empty string => EOF
                if not streams and not rtcp_bandwidth and (
                        type in b"trzkam" or line.startswith(b"b=RR:")):
                    sdp.write(b"b=RR:0\r\n")
                    rtcp_bandwidth = True
                if line.startswith(b"b=RR:"):
                    line = b""
                if type in b"m":
                    if streams:  # End of a media section
                        control = "a=control:{}\r\n".format(streams - 1)
                        sdp.write(control.encode("ascii"))
                    else:  # End of the top session-level section
                        range = "a=range:npt=0-{}\r\n"
                        range = range.format(metadata["format"]["duration"])
                        sdp.write(range.encode("ascii"))
                    rtcp_bandwidth = False
                if not type:
                    break
                
                if type == b"m":
                    fields = line.split(maxsplit=2)
                    PORT = 1
                    fields[PORT] = b"0"  # VLC hangs or times out otherwise
                    line = b" ".join(fields)
                    streams += 1
                if notitle and type == b"s":
                    # SDP specification says the session name field must be
                    # present and non-empty, recommending a single space
                    # where there is no name, but players tend to handle
                    # omitting it better
                    line = b""
                
                if not line.startswith(b"a=control:"):
                    sdp.write(line)
                
                line = ffmpeg.stdout.readline()
            else:
                with ffmpeg:
                    pass  # Close and wait for process
                msg = "FF MPEG failed generating SDP data; exit status: {}"
                raise EnvironmentError(msg.format(ffmpeg.returncode))
        return (media, sdp.getvalue(), streams)
    
    def server_close(self, *pos, **kw):
        while self._sessions:
            (_, session) = self._sessions.popitem()
            session.end()
        return super().server_close(*pos, **kw)

_streamtypes = ("video", "audio")

def _ffmpeg(file, options, streams, ffmpeg2=True, **kw):
    """Spawn an FF MPEG child process
    
    * options: CLI arguments to include before the input (-i) parameter
    * streams: Output an RTP stream for each of these
    """
    options = list(options) + ["-i", file]
    
    for (i, (type, addresses)) in enumerate(streams):
        t = type[0]
        if ffmpeg2:
            options.extend(("-map", "0:" + t))
        options.extend(("-{}codec".format(t), "copy"))
        options.extend("-{}n".format(other[0]) for
            other in _streamtypes if other != type)
        
        options.extend(("-f", "rtp", "-rtpflags", "send_bye"))
        if not addresses:
            # Avoid null or zero port because FF MPEG emits an error,
            # although only after outputting the SDP data,
            # and "libav" does not emit the error.
            rtp = ("localhost", 6970 + i * 2)
            query = ""
        else:
            [rtp, rtcp] = addresses
            query = "?" + urllib.parse.urlencode((("rtcpport", rtcp),))
        options.append("rtp://" + format_addr(rtp) + query)
        
        if not ffmpeg2 and i:
            options += ("-new" + type,)
        first = False
    
    return _ffmpeg_command("ffmpeg", options, **kw)

def _ffmpeg_command(command, options, loglevel="warning", **popenargs):
    command = [command, "-loglevel", loglevel]
    command.extend(options)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, **popenargs)

class Handler(basehttp.RequestHandler):
    server_version = "RTSP-server " + basehttp.RequestHandler.server_version
    protocol_version = "RTSP/1.0"
    
    def setup(self):
        basehttp.RequestHandler.setup(self)
        self.rfile = RollbackReader(self.rfile)
        self.channels = dict()
    
    def finish(self):
        while self.channels:
            next(iter(self.channels.values())).close()
        return basehttp.RequestHandler.finish(self)
    
    def handle_one_request(self):
        self.rfile.start_capture()
        c = self.rfile.read(1)
        if c == b"$":
            self.rfile.drop_capture()
            self.handle_interleaved()
            self.close_connection = False
            return
        self.rfile.roll_back()
        return basehttp.RequestHandler.handle_one_request(self)
    
    def get_encoding(self, protocol):
        if protocol in {b"RTSP", None}:
            return "utf-8"
        return basehttp.RequestHandler.get_encoding(self, protocol)
    
    def handle_method(self):
        self.media = None  # Indicates path not parsed
        self.streams = None  # Indicates media not parsed
        self.sessionparsed = False
        basehttp.RequestHandler.handle_method(self)
    
    def send_response(self, *pos, **kw):
        basehttp.RequestHandler.send_response(self, *pos, **kw)
        for cseq in self.headers.get_all("CSeq", ()):
            self.send_header("CSeq", cseq)
    
    def do_OPTIONS(self):
        """
        OPTIONS bad-path -> 404 + Public
        OPTIONS + Session: bad -> 454 + Allow + Public
        OPTIONS * [+ Session] -> 200 + [Session +] Allow + Public
        OPTIONS path [+ Session] -> 200 + [Session +] Allow + Public
        """
        try:
            if self.plainpath:
                self.parse_path()
                self.parse_media()
            try:
                self.parse_session()
                self.send_response(OK)
            except basehttp.ErrorResponse as err:
                self.send_response(err.code, err.message)
            self.send_allow()
        except basehttp.ErrorResponse as err:
            self.send_response(err.code, err.message)
        self.send_public()
        self.end_headers()
    
    def do_DESCRIBE(self):
        """
        DESCRIBE * -> 405 + [Session +] Allow
        DESCRIBE bad-path -> 404
        DESCRIBE stream -> 460 + [Session +] Allow
        DESCRIBE path -> 200 + entity
        """
        self.parse_path()
        sdp = self.parse_media()
        if self.stream is not None:
            raise basehttp.ErrorResponse(ONLY_AGGREGATE_OPERATION_ALLOWED)
        self.send_entity("application/sdp", sdp)
    
    def do_SETUP(self):
        """
        SETUP + Session: bad -> 454
        SETUP new-path + Session -> 455 + Session + Allow
        SETUP bad-path -> 404
        SETUP + Session: streaming -> 455 + Session + Allow
        SETUP * (no Session) -> 455 + Allow
        SETUP aggregate [+ Session] -> 459 + [Session +] Allow
        SETUP stream [+ Session] + Transport: bad -> 461
        SETUP stream [+ Session] + Transport -> 200 + Session + Transport
        """
        self.parse_session()
        self.parse_session_path()
        if self.session is None:
            if not self.plainpath:
                msg = "No media or session specified"
                raise basehttp.ErrorResponse(METHOD_NOT_VALID_IN_THIS_STATE,
                    msg)
            session = Session(self.media, self.ospath, self.streams)
        else:
            session = self.session
        
        if self.stream is None:
            if self.streams > 1:
                msg = "{} streams available".format(self.streams)
                raise basehttp.ErrorResponse(AGGREGATE_OPERATION_NOT_ALLOWED,
                    msg)
            self.stream = 0
        if session.ffmpeg:
            msg = "SETUP not supported while streaming"
            raise basehttp.ErrorResponse(METHOD_NOT_VALID_IN_THIS_STATE, msg)
        
        error = None
        single_error = False
        for transport in header_list(self.headers, "Transport"):
            try:
                header = email.message.Message()
                # Default get_params() header is Content-Type
                header["Content-Type"] = transport
                [transport, _] = header.get_params()[0]
                transport = iter(header_split(transport, "/"))
                if (next(transport, "RTP").upper() != "RTP" or
                next(transport, "AVP").upper() != "AVP"):
                    raise ValueError("Only RTP/AVP supported")
                
                mode = header_split(header.get_param("mode", "PLAY"), ",")
                if frozenset(map(str.upper, mode)) != {"PLAY"}:
                    raise ValueError('Only mode="PLAY" supported')
                
                channel = header.get_param("interleaved")
                if channel is not None:
                    transport = InterleavedTransport(self, channel)
                    break
                
                udp = next(transport, "UDP").upper() == "UDP"
                if udp and header.get_param("unicast") is not None:
                    transport = UdpTransport(self, header)
                    break
                
                msg = ("Only unicast UDP and interleaved transports "
                    "supported")
                raise ValueError(msg)
            except ValueError as exc:
                single_error = error is None
                error = format(exc)
        else:  # No suitable transport found
            if not single_error:
                error = ("No supported unicast UDP or interleaved transport "
                    "given")
            raise basehttp.ErrorResponse(UNSUPPORTED_TRANSPORT, error)
        
        session.transports[self.stream] = transport
        if self.session is None:
            self.sessionkey = random.getrandbits(_SESSION_DIGITS * 4)
            self.server._sessions[self.sessionkey] = session
            msg = "Session created"
        else:
            msg = None
        
        self.send_response(OK, msg)
        self.send_session()
        self.send_header("Transport", transport.header())
        self.end_headers()
    
    def do_TEARDOWN(self):
        """
        TEARDOWN new-path + Session -> 455 + Session + Allow
        TEARDOWN bad-path -> 404
        TEARDOWN + Session: bad -> 200 "Session not found"
        TEARDOWN (no Session) -> 454 + Allow
        TEARDOWN path/new-stream -> 200 "Not set up"
        TEARDOWN lone-stream -> 200 "Session invalidated"
        TEARDOWN stream + Session: streaming -> 455 + Session + Allow
        TEARDOWN stream + Session: stopped -> 200 + Session
        """
        try:
            self.parse_session()
        except basehttp.ErrorResponse as err:
            msg = err.message
            if msg is None:
                msg = self.responses.get(err.code)[0]
        self.parse_session_path()
        if self.invalidsession:
            raise basehttp.ErrorResponse(OK, msg)
        if not self.session:
            self.send_response(SESSION_NOT_FOUND, "No session given")
            self.send_allow()
            self.end_headers()
            return
        
        if self.stream is None:
            del self.server._sessions[self.sessionkey]
            self.session.end()
            msg = "Session invalidated"
        else:
            if self.session.ffmpeg:
                if self.session.other_transports(stream):
                    msg = "Partial TEARDOWN not supported while streaming"
                    raise basehttp.ErrorResponse(
                        METHOD_NOT_VALID_IN_THIS_STATE, msg)
                self.session.end()
            
            if self.session.transports[self.stream]:
                self.session.transports[self.stream] = None
                msg = "Stream {} closed".format(self.stream)
            else:
                msg = "Stream {} not set up".format(self.stream)
        self.send_response(OK, msg)
        if self.sessionkey in self.server._sessions:
            self.send_session()
        self.end_headers()
    
    def do_PLAY(self):
        """
        PLAY + Session: bad -> 454
        PLAY new-path -> 455 + Session + Allow
        PLAY bad-path -> 404
        PLAY (no Session) -> 454 + Allow
        PLAY new-stream -> 455 + Session + Allow
        PLAY lone-stream -> 200 + Session
        PLAY stream -> 460 + Session + Allow
        """
        self.parse_session()
        self.parse_session_path()
        if not self.session:
            self.send_response(SESSION_NOT_FOUND, "No session given")
            self.send_allow()
            self.end_headers()
            return
        if (self.stream is not None and
        self.session.other_transports(self.stream)):
            raise basehttp.ErrorResponse(ONLY_AGGREGATE_OPERATION_ALLOWED)
        if self.session.ffmpeg:
            self.send_response(OK, "Already playing")
            self.send_session()
            self.end_headers()
            return
        
        try:
            if "Range" in self.headers:
                time = self.headers.get_param(header="Range", param="time")
                if time is not None:
                    raise ValueError("Start time parameter not supported")
                npt = self.headers.get_param(header="Range", param="npt")
                if npt is None:
                    msg = "Only NPT range supported"
                    self.send_response(NOT_IMPLEMENTED, msg)
                    self.send_header("Accept-Ranges", "npt")
                    self.end_headers()
                    return
                [npt, end] = header_partition(npt, "-")
                if end:
                    raise ValueError("End point not supported")
                self.session.pause_point = float(npt)
        except ValueError as err:
            raise basehttp.ErrorResponse(
                HEADER_FIELD_NOT_VALID_FOR_RESOURCE, err)
        
        self.session.start(ffmpeg2=self.server._ffmpeg2)
        self.send_response(OK)
        self.send_session()
        range = "npt={:f}-".format(self.session.pause_point)
        self.send_header("Range", range)
        self.end_headers()
    
    def do_PAUSE(self):
        self.parse_session()
        self.parse_session_path()
        if not self.session:
            self.send_response(SESSION_NOT_FOUND, "No session given")
            self.send_allow()
            self.end_headers()
            return
        if (self.stream is not None and
        self.session.other_transports(self.stream)):
            raise basehttp.ErrorResponse(ONLY_AGGREGATE_OPERATION_ALLOWED)
        
        if "Range" in self.headers:
            msg = "Deferred pausing not supported"
            raise basehttp.ErrorResponse(HEADER_FIELD_NOT_VALID_FOR_RESOURCE,
                msg)
        
        msg = None
        if self.session.ffmpeg:
            stopped = time.monotonic()
            self.session.end()
            self.session.ffmpeg = None
            self.session.pause_point += stopped - self.session.started
        else:
            msg = "Already paused"
        self.send_response(OK, msg)
        self.send_session()
        self.send_header("Range", "npt={:f}".format(self.session.pause_point))
        self.end_headers()
    
    def parse_path(self):
        """Parse path into media path and possible stream number"""
        basehttp.RequestHandler.parse_path(self)
        self.media = self.parsedpath[:-1]
        stream = self.parsedpath[-1]
        if stream:
            try:
                self.stream = int(stream)
            except ValueError as err:
                raise basehttp.ErrorResponse(NOT_FOUND, err)
        else:
            self.stream = None
    
    def parse_media(self):
        try:
            media = self.server._get_media(self.media)
            if media != self.server._last_media:
                self.server._last_description = self.server._get_sdp(media)
                self.server._last_media = media
            (self.ospath, sdp, self.streams) = self.server._last_description
        except (ValueError, EnvironmentError,
        subprocess.CalledProcessError) as err:
            raise basehttp.ErrorResponse(NOT_FOUND, err)
        self.validate_stream()
        return sdp
    
    def parse_session_path(self):
        if not self.plainpath:
            self.stream = None
            return
        self.parse_path()
        if self.session:
            if self.media != self.session.media:
                msg = "Session already set up with different media file"
                raise basehttp.ErrorResponse(METHOD_NOT_VALID_IN_THIS_STATE,
                    msg)
            self.streams = len(self.session.transports)
            self.validate_stream()
        else:
            self.parse_media()
    
    def validate_stream(self):
        if (self.stream is not None and
        self.stream not in range(self.streams)):
            msg = "Stream number out of range 0-{}".format(self.streams - 1)
            raise basehttp.ErrorResponse(NOT_FOUND, msg)
    
    def parse_session(self):
        self.sessionparsed = True
        self.invalidsession = True
        self.session = None  # Indicate no session by default
        key = self.headers.get_params(header="Session")
        if not key:
            self.invalidsession = False
            return
        try:
            [key, _] = key[0]
            self.sessionkey = int(key, 16)
        except ValueError as err:
            raise basehttp.ErrorResponse(SESSION_NOT_FOUND, err)
        self.session = self.server._sessions.get(self.sessionkey)
        if self.session is None:
            raise basehttp.ErrorResponse(SESSION_NOT_FOUND)
        self.invalidsession = False
    
    def send_allow(self):
        if self.plainpath:
            try:
                if self.media is None:
                    self.parse_path()
                if self.streams is None:
                    self.parse_media()
            except basehttp.ErrorResponse:
                return
        if not self.sessionparsed:
            try:
                self.parse_session()
            except basehttp.ErrorResponse:
                pass
        
        mediamatch = (not self.session or not self.plainpath or
            self.session.media == self.media)
        streaming = self.session and self.session.ffmpeg
        allstreams = self.session and (
            not self.plainpath or self.stream is None or
            self.session.transports[self.stream] and
            not self.session.other_transports(self.stream)
        )
        
        allow = ["OPTIONS"]
        
        if self.plainpath:
            if self.stream is None:
                allow.append("DESCRIBE")
            
            singlestream = self.stream is not None or self.streams <= 1
        else:
            singlestream = self.session and len(self.session.transports) <= 1
        if (mediamatch and singlestream and not self.invalidsession and
        not streaming):
            allow.append("SETUP")
        
        if (self.invalidsession or
        self.session and mediamatch and (allstreams or not streaming)):
            allow.append("TEARDOWN")
        if mediamatch and allstreams:
            allow.extend(("PLAY", "PAUSE"))
        
        if self.session:
            self.send_session()
        self.send_header("Allow", ", ".join(allow))
    
    def send_session(self):
        key = "{:0{}X};timeout=86400"
        key = key.format(self.sessionkey, _SESSION_DIGITS)
        self.send_header("Session", key)
    
    def handle_interleaved(self):
        # Drop RT(C)P traffic sent from client; FF MPEG CLI doesn't seem to
        # have a way to get the local RT(C)P ports
        header = self.rfile.read(self.interleaved_header.size)
        [channel, length] = self.interleaved_header.unpack(header)
        while length:
            length -= len(self.rfile.read(min(length, 0x10000)))
    
    interleaved_header = Struct("!BH")

Handler.responses = dict(Handler.responses)  # Copy from base class
for (code, message) in (
    (454, "Session Not Found"),
    (455, "Method Not Valid In This State"),
    (456, "Header Field Not Valid for Resource"),
    (459, "Aggregate Operation Not Allowed"),
    (460, "Only Aggregate Operation Allowed"),
    (461, "Unsupported Transport"),
):
    symbol = "_".join(message.split()).upper()
    globals()[symbol] = code
    Handler.responses[code] = (message,)

Handler.allow_codes = Handler.allow_codes | {
    METHOD_NOT_VALID_IN_THIS_STATE,  # Recommended by specification
    
    # Other statuses not suggested by specification
    AGGREGATE_OPERATION_NOT_ALLOWED,
    ONLY_AGGREGATE_OPERATION_ALLOWED,
}

class Session:
    def __init__(self, media, ospath, streams):
        self.media = media
        self.ospath = ospath
        self.transports = [None] * streams
        self.ffmpeg = None
        self.pause_point = 0
    
    def start(self, ffmpeg2=True):
        options = ("-re", "-ss", format(self.pause_point, "f"))
        transports = zip(_streamtypes, self.transports)
        streams = list()
        for [type, transport] in transports:
            if transport:
                streams.append((type, transport.setup()))
        self.ffmpeg = _ffmpeg(self.ospath, options, streams, ffmpeg2=ffmpeg2,
            stdout=subprocess.DEVNULL)
        self.started = time.monotonic()
    
    def end(self):
        if self.ffmpeg:
            self.close_transports()
            self.ffmpeg.kill()  # Avoid FF MPEG sending RTCP BYE messages
            self.ffmpeg.wait()
    
    def close_transports(self):
        for transport in self.transports:
            if transport:
                transport.close()
    
    def other_transports(self, stream):
        return (any(self.transports[:stream]) or
            any(self.transports[stream + 1:]))

class Transport:
    def close(self):
        pass

class UdpTransport(Transport):
    def __init__(self, handler, header):
        port = header.get_param("client_port")
        if port is None:
            raise ValueError('UDP transport missing "client_port" parameter')
        [port, end] = header_partition(port, "-")
        self.port = int(port)
        if end and int(end) < self.port + 1:
            raise ValueError("Pair of ports required for RTP and RTCP")
        
        [self.dest, _] = handler.client_address
    
    def header(self):
        header = "RTP/AVP/UDP;unicast;destination={};client_port={}-{}"
        return header.format(self.dest, self.port, self.port + 1)
    
    def setup(self):
        return ((self.dest, self.port), self.port + 1)

class InterleavedTransport(Transport):
    def __init__(self, handler, channel):
        [channel, end] = header_partition(channel, "-")
        self.channel = int(channel)
        if end and int(end) < self.channel + 1:
            raise ValueError("Pair of channels required for RTP and RTCP")
        
        self.handler = handler
    
    def header(self):
        header = "RTP/AVP/TCP;interleaved={}-{}"
        return header.format(self.channel, self.channel + 1)
    
    def setup(self):
        self.rtp = UdpListener(self.handler, self.channel)
        self.rtp.register(self.handler.server.selector)
        self.rtcp = UdpListener(self.handler, self.channel + 1)
        self.rtcp.register(self.handler.server.selector)
        [_, rtcp] = self.rtcp.server_address
        return (self.rtp.server_address, rtcp)
    
    def close(self):
        self.rtcp.server_close()
        self.rtp.server_close()

class UdpListener(SelectableServer, UDPServer):
    def __init__(self, connection, channel):
        self.connection = connection
        self.channel = channel
        SelectableServer.__init__(self, ("", 0), InterleavedHandler)
        self.connection.channels[self.channel] = self
    
    def server_close(self):
        if self.connection.channels.pop(self.channel, None):
            SelectableServer.server_close(self)

class InterleavedHandler(BaseRequestHandler):
    header = Struct("!cBH")
    def handle(self):
        [packet, _] = self.request
        header = self.header.pack(b"$", self.server.channel, len(packet))
        self.server.connection.wfile.write(header)
        self.server.connection.wfile.write(packet)
        self.server.connection.wfile.flush()
