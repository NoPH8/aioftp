import asyncio
import re
import collections
import pathlib

from . import errors
from . import pathio
from .common import *  # noqa
from .common import logger


__all__ = (
    "Client",
    "Code",
)


def add_prefix(message):

    return str.format("aioftp client: {}", message)


@asyncio.coroutine
def open_connection(host, port, loop, create_connection):

    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = yield from create_connection(lambda: protocol, host, port)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


class Code(str):
    """
    Representation of server status code.
    """

    def matches(self, mask):
        """
        :param mask: Template for comparision. If mask symbol is not digit
            then it passes.
        :type mask: :py:class:`str`

        ::

            >>> Code("123").matches("1")
            True
            >>> Code("123").matches("1x3")
            True
        """
        return all(map(lambda m, c: not str.isdigit(m) or m == c, mask, self))


class DataConnectionThrottleStreamIO(ThrottleStreamIO):
    """
    Add `finish` method to :py:class:`aioftp.ThrottleStreamIO`, which is
        specific for data connection. This requires `client`.

    :param client: client class, which have :py:meth:`aioftp.Client.command`
    :type client: :py:class:`aioftp.BaseClient`

    :param *args: positional arguments passed to :py:class:`aioftp.StreamIO`

    :param **kwargs: keyword arguments passed to :py:class:`aioftp.StreamIO`
    """

    def __init__(self, client, *args, **kwargs):

        super().__init__(*args, **kwargs)
        self.client = client

    @asyncio.coroutine
    def finish(self, expected_codes="2xx", wait_codes="1xx"):
        """
        :py:func:`asyncio.coroutine`

        Close connection and wait for `expected_codes` response from server
        passing `wait_codes`.

        :param expected_codes: tuple of expected codes or expected code
        :type expected_codes: :py:class:`tuple` of :py:class:`str` or
            :py:class:`str`

        :param wait_codes: tuple of wait codes or wait code
        :type wait_codes: :py:class:`tuple` of :py:class:`str` or
            :py:class:`str`
        """
        self.close()
        yield from self.client.command(None, expected_codes, wait_codes)


class BaseClient:

    def __init__(self,
                 *,
                 loop=None,
                 create_connection=None,
                 socket_timeout=None,
                 server_to_client_speed_limit=None,
                 client_to_server_speed_limit=None,
                 path_timeout=None,
                 path_io_factory=pathio.AsyncPathIO):

        self.loop = loop or asyncio.get_event_loop()
        self.create_connection = create_connection or \
            self.loop.create_connection
        self.socket_timeout = socket_timeout

        self.server_to_client_throttle = Throttle(
            loop=self.loop,
            limit=server_to_client_speed_limit
        )
        self.client_to_server_throttle = Throttle(
            loop=self.loop,
            limit=client_to_server_speed_limit
        )

        self.path_timeout = path_timeout
        self.path_io = path_io_factory(timeout=path_timeout, loop=loop)

    @asyncio.coroutine
    def connect(self, host, port=21):

        self.server_host = host
        reader, writer = yield from open_connection(
            host,
            port,
            self.loop,
            self.create_connection
        )
        self.stream = ThrottleStreamIO(
            reader,
            writer,
            throttles={
                "main": StreamThrottle(
                    read=self.server_to_client_throttle,
                    write=self.client_to_server_throttle
                )
            },
            timeout=self.socket_timeout,
            loop=self.loop
        )

    def close(self):
        """
        Close connection.
        """
        self.stream.close()

    @asyncio.coroutine
    def parse_line(self):
        """
        :py:func:`asyncio.coroutine`

        Parsing server response line.

        :return: (code, line)
        :rtype: (:py:class:`aioftp.Code`, :py:class:`str`)

        :raises ConnectionResetError: if received data is empty (this
            means, that connection is closed)
        :raises asyncio.TimeoutError: if there where no data for `timeout`
            period
        """
        line = yield from self.stream.readline()
        if not line:

            self.stream.close()
            raise ConnectionResetError

        s = str.rstrip(bytes.decode(line, encoding="utf-8"))
        logger.info(add_prefix(s))
        return Code(s[:3]), s[3:]

    @asyncio.coroutine
    def parse_response(self):
        """
        :py:func:`asyncio.coroutine`

        Parsing full server response (all lines).

        :return: (code, lines)
        :rtype: (:py:class:`aioftp.Code`, :py:class:`list` of :py:class:`str`)

        :raises aioftp.StatusCodeError: if received code does not matches all
            already received codes
        """
        code, rest = yield from self.parse_line()
        info = [rest]
        curr_code = code
        while str.startswith(rest, "-") or not str.isdigit(curr_code):

            curr_code, rest = yield from self.parse_line()
            if str.isdigit(curr_code):

                info.append(rest)
                if curr_code != code:

                    raise errors.StatusCodeError(code, curr_code, info)

            else:

                info.append(curr_code + rest)

        return code, info

    def check_codes(self, expected_codes, received_code, info):
        """
        Checks if any of expected matches received.

        :param expected_codes: tuple of expected codes
        :type expected_codes: :py:class:`tuple`

        :param received_code: received code for matching
        :type received_code: :py:class:`aioftp.Code`

        :param info: list of response lines from server
        :type info: :py:class:`list`

        :raises aioftp.StatusCodeError: if received code does not matches any
            expected code
        """
        if not any(map(received_code.matches, expected_codes)):

            raise errors.StatusCodeError(expected_codes, received_code, info)

    @asyncio.coroutine
    def command(self, command=None, expected_codes=(), wait_codes=()):
        """
        :py:func:`asyncio.coroutine`

        Basic command logic.

        1. Send command if not omitted.
        2. Yield response until no wait code matches.
        3. Check code for expected.

        :param command: command line
        :type command: :py:class:`str`

        :param expected_codes: tuple of expected codes or expected code
        :type expected_codes: :py:class:`tuple` of :py:class:`str` or
            :py:class:`str`

        :param wait_codes: tuple of wait codes or wait code
        :type wait_codes: :py:class:`tuple` of :py:class:`str` or
            :py:class:`str`
        """
        expected_codes = wrap_with_container(expected_codes)
        wait_codes = wrap_with_container(wait_codes)

        if command:

            logger.info(add_prefix(command))
            message = command + END_OF_LINE
            yield from self.stream.write(str.encode(message, encoding="utf-8"))

        if expected_codes or wait_codes:

            code, info = yield from self.parse_response()
            while any(map(code.matches, wait_codes)):

                code, info = yield from self.parse_response()

            if expected_codes:

                self.check_codes(expected_codes, code, info)

            return code, info

    def parse_address_response(self, s):
        """
        Parsing ip:port server response.

        :param s: response line
        :type s: :py:class:`str`

        :return: (ip, port)
        :rtype: (:py:class:`str`, :py:class:`int`)
        """
        sub, *_ = re.findall(r"[^(]*\(([^)]*)", s)
        nums = tuple(map(int, str.split(sub, ",")))
        ip = str.join(".", map(str, nums[:4]))
        port = (nums[4] << 8) | nums[5]
        return ip, port

    def parse_directory_response(self, s):
        """
        Parsing directory server response.

        :param s: response line
        :type s: :py:class:`str`

        :rtype: :py:class:`pathlib.Path`
        """
        seq_quotes = 0
        start = False
        directory = ""
        for ch in s:

            if not start:

                if ch == '"':

                    start = True

            else:

                if ch == '"':

                    seq_quotes += 1

                else:

                    if seq_quotes == 1:

                        break

                    elif seq_quotes == 2:

                        seq_quotes = 0
                        directory += '"'

                    directory += ch

        return pathlib.Path(directory)

    def parse_mlsx_line(self, b):
        """
        Parsing MLS(T|D) response.

        :param b: response line
        :type b: :py:class:`bytes` or :py:class:`str`

        :return: (path, info)
        :rtype: (:py:class:`pathlib.Path`, :py:class:`dict`)
        """
        if isinstance(b, bytes):

            s = str.rstrip(bytes.decode(b, encoding="utf-8"))

        else:

            s = b

        line = str.rstrip(s)
        facts_found, _, name = str.partition(line, " ")
        entry = {}
        for fact in str.split(facts_found[:-1], ";"):

            key, _, value = str.partition(fact, "=")
            entry[key.lower()] = value

        return pathlib.Path(name), entry


class Client(BaseClient):
    """
    FTP client.

    :param loop: loop to use for creating connection and binding with streams
    :type loop: :py:class:`asyncio.BaseEventLoop`

    :param create_connection: factory for creating
        connection.
        Using default :py:meth:`asyncio.BaseEventLoop.create_connection`
        if omitted.
    :type create_connection: :py:func:`callable`

    :param socket_timeout: timeout for read operations
    :type socket_timeout: :py:class:`float`, :py:class:`int` or `None`

    :param server_to_client_speed_limit: download speed limit in bytes per
        second
    :type server_to_client_speed_limit: :py:class:`int` or `None`

    :param client_to_server_speed_limit: upload speed limit in bytes per second
    :type client_to_server_speed_limit: :py:class:`int` or `None`

    :param path_timeout: timeout for path-related operations (make directory,
        unlink file, etc.)
    :type path_timeout: :py:class:`float`, :py:class:`int` or
        :py:class:`None`

    :param path_io_factory: factory of «path abstract layer»
    :type path_io_factory: :py:class:`aioftp.AbstractPathIO`
    """

    @asyncio.coroutine
    def connect(self, host, port=21):
        """
        :py:func:`asyncio.coroutine`

        Connect to server.

        :param host: host name for connection
        :type host: :py:class:`str`

        :param port: port number for connection
        :type port: :py:class:`int`
        """
        yield from super().connect(host, port)
        code, info = yield from self.command(None, "220", "120")
        return info

    @asyncio.coroutine
    def login(self, user="anonymous", password="anon@", account=""):
        """
        :py:func:`asyncio.coroutine`

        Server authentication.

        :param user: username
        :type user: :py:class:`str`

        :param password: password
        :type password: :py:class:`str`

        :param account: account (almost always blank)
        :type account: :py:class:`str`

        :raises aioftp.StatusCodeError: if unknown code received
        """
        code, info = yield from self.command("USER " + user, ("230", "33x"))
        while code.matches("33x"):

            if code == "331":

                cmd = "PASS " + password

            elif code == "332":

                cmd = "ACCT " + account

            else:

                raise errors.StatusCodeError("33x", code, info)

            code, info = yield from self.command(cmd, ("230", "33x"))

    @asyncio.coroutine
    def get_current_directory(self):
        """
        :py:func:`asyncio.coroutine`

        Getting current working directory.

        :rtype: :py:class:`pathlib.Path`
        """
        code, info = yield from self.command("PWD", "257")
        directory = self.parse_directory_response(info[-1])
        return directory

    @asyncio.coroutine
    def change_directory(self, path=".."):
        """
        :py:func:`asyncio.coroutine`

        Change current directory. Goes «up» if no parameters passed.

        :param pathlib.Path path: new directory, goes «up» if omitted
        :type path: :py:class:`str` or :py:class:`pathlib.Path`
        """
        if path in ("..", pathlib.Path("..")):

            cmd = "CDUP"

        else:

            cmd = "CWD " + str(path)

        yield from self.command(cmd, "2xx")

    @asyncio.coroutine
    def make_directory(self, path, *, parents=True):
        """
        :py:func:`asyncio.coroutine`

        Make directory.

        :param path: path to directory to create
        :type path: :py:class:`str` or :py:class:`pathlib.Path`

        :param parents: create parents if does not exists
        :type parents: :py:class:`bool`
        """
        path = pathlib.Path(path)
        need_create = []
        while path.name and not (yield from self.exists(path)):

            need_create.append(path)
            path = path.parent
            if not parents:

                break

        need_create.reverse()
        for path in need_create:

            yield from self.command("MKD " + str(path), "257")

    @asyncio.coroutine
    def remove_directory(self, path):
        """
        :py:func:`asyncio.coroutine`

        Low level remove method for removing empty directory.

        :param path: empty directory to remove
        :type path: :py:class:`str` or :py:class:`pathlib.Path`
        """
        yield from self.command("RMD " + str(path), "250")

    @asyncio.coroutine
    def list(self, path="", *, recursive=False):
        """
        :py:func:`asyncio.coroutine`

        List all files and directories in "path".

        :param path: directory or file path
        :type path: :py:class:`str` or :py:class:`pathlib.Path`

        :param recursive: list recursively
        :type recursive: :py:class:`bool`

        :rtype: :py:class:`list` or :py:class:`None`
        """
        result = []
        directories = []
        command = str.strip("MLSD " + str(path))
        stream = yield from self.get_stream(command, "1xx")
        while True:

            line = yield from stream.readline()
            if not line:

                yield from stream.finish()
                break

            name, info = self.parse_mlsx_line(line)
            if info["type"] in ("file", "dir"):

                stat = path / name, info
                if info["type"] == "dir":

                    directories.append(stat)

                result.append(stat)

        if recursive:

            for name, info in directories:

                result += yield from self.list(name, recursive=recursive)

        return result

    @asyncio.coroutine
    def stat(self, path):
        """
        :py:func:`asyncio.coroutine`

        Getting path stats.

        :param path: path for getting info
        :type path: :py:class:`str` or :py:class:`pathlib.Path`

        :return: path info
        :rtype: :py:class:`dict`
        """
        code, info = yield from self.command("MLST " + str(path), "2xx")
        name, info = self.parse_mlsx_line(str.lstrip(info[1]))
        return info

    @asyncio.coroutine
    def is_file(self, path):
        """
        :py:func:`asyncio.coroutine`

        Checks if path is file.

        :rtype: :py:class:`bool`
        """
        info = yield from self.stat(path)
        return info["type"] == "file"

    @asyncio.coroutine
    def is_dir(self, path):
        """
        :py:func:`asyncio.coroutine`

        Checks if path is dir.

        :rtype: :py:class:`bool`
        """
        info = yield from self.stat(path)
        return info["type"] == "dir"

    @asyncio.coroutine
    def exists(self, path):
        """
        :py:func:`asyncio.coroutine`

        Check path for existence.

        :param path: path for checking on server side
        :type path: :py:class:`str` or :py:class:`pathlib.Path`

        :rtype: :py:class:`bool`
        """
        code, info = yield from self.command(
            "MLST " + str(path),
            ("2xx", "550")
        )
        exists = code.matches("2xx")
        return exists

    @asyncio.coroutine
    def rename(self, source, destination):
        """
        :py:func:`asyncio.coroutine`

        Rename (move) file or directory.

        :param source: path to rename
        :type source: :py:class:`str` or :py:class:`pathlib.Path`

        :param destination: path new name
        :type destination: :py:class:`str` or :py:class:`pathlib.Path`
        """
        yield from self.command("RNFR " + str(source), "350")
        yield from self.command("RNTO " + str(destination), "2xx")

    @asyncio.coroutine
    def remove_file(self, path):
        """
        :py:func:`asyncio.coroutine`

        Low level remove method for removing file.

        :param path: file to remove
        :type path: :py:class:`str` or :py:class:`pathlib.Path`
        """
        yield from self.command("DELE " + str(path), "2xx")

    @asyncio.coroutine
    def remove(self, path):
        """
        :py:func:`asyncio.coroutine`

        High level remove method for removing path recursively (file or
        directory).

        :param path: path to remove
        :type path: :py:class:`str` or :py:class:`pathlib.Path`
        """
        if (yield from self.exists(path)):

            info = yield from self.stat(path)
            if info["type"] == "file":

                yield from self.remove_file(path)

            elif info["type"] == "dir":

                for name, info in (yield from self.list(path)):

                    if info["type"] in ("dir", "file"):

                        yield from self.remove(name)

                yield from self.remove_directory(path)

    @asyncio.coroutine
    def upload_stream(self, destination):
        """
        :py:func:`asyncio.coroutine`

        Create stream for write data to `destination` file.

        :param destination: destination path of file on server side
        :type destination: :py:class:`str` or :py:class:`pathlib.Path`

        :rtype: :py:class:`aioftp.ThrottleStreamIO`
        """
        return (yield from self.get_stream("STOR " + str(destination), "1xx"))

    @asyncio.coroutine
    def append_stream(self, destination):
        """
        :py:func:`asyncio.coroutine`

        Create stream for append (write) data to `destination` file.

        :param destination: destination path of file on server side
        :type destination: :py:class:`str` or :py:class:`pathlib.Path`

        :rtype: :py:class:`aioftp.ThrottleStreamIO`
        """
        return (yield from self.get_stream("APPE " + str(destination), "1xx"))

    @asyncio.coroutine
    def upload(self, source, destination="", *, write_into=False,
               block_size=DEFAULT_BLOCK_SIZE):
        """
        :py:func:`asyncio.coroutine`

        High level upload method for uploading files and directories
        recursively from file system.

        :param source: source path of file or directory on client side
        :type source: :py:class:`str` or :py:class:`pathlib.Path`

        :param destination: destination path of file or directory on server
            side
        :type destination: :py:class:`str` or :py:class:`pathlib.Path`

        :param write_into: write source into destination (if you want upload
            file and change it name, as well with directories)
        :type write_into: :py:class:`bool`

        :param block_size: block size for transaction
        :type block_size: :py:class:`int`
        """
        source = pathlib.Path(source)
        destination = pathlib.Path(destination)
        if not write_into:

            destination = destination / source.name

        if (yield from self.path_io.is_file(source)):

            yield from self.make_directory(destination.parent)
            try:

                fin = yield from self.path_io.open(source, mode="rb")
                stream = yield from self.upload_stream(destination)
                while True:

                    block = yield from self.path_io.read(fin, block_size)
                    if not block:

                        yield from stream.finish()
                        break

                    yield from stream.write(block)

            finally:

                yield from self.path_io.close(fin)

        elif (yield from self.path_io.is_dir(source)):

            yield from self.make_directory(destination)
            sources = collections.deque([source])
            while sources:

                src = sources.popleft()
                for path in (yield from self.path_io.list(src)):

                    if write_into:

                        relative = destination.name / path.relative_to(source)

                    else:

                        relative = path.relative_to(source.parent)

                    if (yield from self.path_io.is_dir(path)):

                        yield from self.make_directory(relative)
                        sources.append(path)

                    else:

                        yield from self.upload(
                            path,
                            relative,
                            write_into=True,
                            block_size=block_size
                        )

    @asyncio.coroutine
    def download_stream(self, source):
        """
        :py:func:`asyncio.coroutine`

        Create stream for read data from `source` file.

        :param source: source path of file on server side
        :type source: :py:class:`str` or :py:class:`pathlib.Path`

        :rtype: :py:class:`aioftp.ThrottleStreamIO`
        """
        return (yield from self.get_stream("RETR " + str(source), "1xx"))

    @asyncio.coroutine
    def download(self, source, destination="", *, write_into=False,
                 block_size=DEFAULT_BLOCK_SIZE):
        """
        :py:func:`asyncio.coroutine`

        High level download method for downloading files and directories
        recursively and save them to the file system.

        :param source: source path of file or directory on server side
        :type source: :py:class:`str` or :py:class:`pathlib.Path`

        :param destination: destination path of file or directory on client
            side
        :type destination: :py:class:`str` or :py:class:`pathlib.Path`

        :param write_into: write source into destination (if you want download
            file and change it name, as well with directories)
        :type write_into: :py:class:`bool`

        :param block_size: block size for transaction
        :type block_size: :py:class:`int`
        """
        source = pathlib.Path(source)
        destination = pathlib.Path(destination)
        if not write_into:

            destination = destination / source.name

        if (yield from self.is_file(source)):

            if not (yield from self.path_io.exists(destination.parent)):

                yield from self.path_io.mkdir(destination.parent)

            fout = yield from self.path_io.open(destination, mode="wb")
            try:

                stream = yield from self.download_stream(source)
                while True:

                    block = yield from stream.read(block_size)
                    if not block:

                        yield from stream.finish()
                        break

                    yield from self.path_io.write(fout, block)

            finally:

                yield from self.path_io.close(fout)

        elif (yield from self.is_dir(source)):

            if not (yield from self.path_io.exists(destination)):

                yield from self.path_io.mkdir(destination, parents=True)

            for name, info in (yield from self.list(source, recursive=True)):

                full = destination / name.relative_to(source)
                if info["type"] == "dir":

                    if not (yield from self.path_io.exists(full)):

                        yield from self.path_io.mkdir(full, parents=True)

                elif info["type"] == "file":

                    yield from self.download(
                        name,
                        full,
                        write_into=True,
                        block_size=block_size
                    )

    @asyncio.coroutine
    def quit(self):
        """
        :py:func:`asyncio.coroutine`

        Send "QUIT" and close connection.
        """
        yield from self.command("QUIT", "2xx")
        self.close()

    @asyncio.coroutine
    def get_passive_connection(self, conn_type="I"):
        """
        :py:func:`asyncio.coroutine`

        Getting pair of reader, writer for passive connection with server.

        :param conn_type: connection type ("I", "A", "E", "L")
        :type conn_type: :py:class:`str`

        :rtype: (:py:class:`asyncio.StreamReader`,
            :py:class:`asyncio.StreamWriter`)
        """
        yield from self.command("TYPE " + conn_type, "200")
        code, info = yield from self.command("PASV", "227")
        ip, port = self.parse_address_response(info[-1])
        if ip == "0.0.0.0":

            ip = self.server_host

        reader, writer = yield from open_connection(
            ip,
            port,
            self.loop,
            self.create_connection,
        )
        return reader, writer

    @asyncio.coroutine
    def get_stream(self, *command_args, conn_type="I"):
        """
        :py:func:`asyncio.coroutine`

        Create :py:class:`aioftp.StreamIO` for straight read/write io.

        :param command_args: arguments for :py:meth:`aioftp.Client.command`

        :param conn_type: connection type ("I", "A", "E", "L")
        :type conn_type: :py:class:`str`

        :rtype: :py:class:`aioftp.StreamIO`
        """
        reader, writer = yield from self.get_passive_connection(conn_type)
        yield from self.command(*command_args)
        stream = DataConnectionThrottleStreamIO(
            self,
            reader,
            writer,
            throttles={
                "main": StreamThrottle(
                    read=self.server_to_client_throttle,
                    write=self.client_to_server_throttle
                )
            },
            timeout=self.socket_timeout,
            loop=self.loop
        )
        return stream

    @asyncio.coroutine
    def abort(self):
        """
        :py:func:`asyncio.coroutine`

        Request data transfer abort.
        """
        yield from self.command("ABOR")
