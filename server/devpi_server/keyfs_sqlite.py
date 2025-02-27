from devpi_common.types import cached_property
from .config import hookimpl
from .fileutil import dumps, loads
from .interfaces import IStorageConnection2
from .keyfs import RelpathInfo
from .keyfs import get_relpath_at
from .log import threadlog, thread_push_log, thread_pop_log
from .mythread import current_thread
from .readonly import ReadonlyView
from .readonly import ensure_deeply_readonly, get_mutable_deepcopy
from .sizeof import gettotalsizeof
from repoze.lru import LRUCache
from zope.interface import implementer
import contextlib
import os
import py
import sqlite3
import time


absent = object()


class BaseConnection:
    _get_relpath_at = get_relpath_at

    def __init__(self, sqlconn, basedir, storage):
        self._sqlconn = sqlconn
        self._basedir = basedir
        self.dirty_files = {}
        self.storage = storage
        self._changelog_cache = storage._changelog_cache
        self._relpath_cache = storage._relpath_cache

    def _explain(self, query, *args):
        # for debugging
        c = self._sqlconn.cursor()
        r = c.execute("EXPLAIN " + query, *args)
        result = r.fetchall()
        c.close()
        return result

    def _explain_query_plan(self, query, *args):
        # for debugging
        c = self._sqlconn.cursor()
        r = c.execute("EXPLAIN QUERY PLAN " + query, *args)
        result = r.fetchall()
        c.close()
        return result

    def _print_rows(self, rows):
        # for debugging
        for row in rows:
            print(row)

    def fetchall(self, query, *args):
        c = self._sqlconn.cursor()
        # print(query)
        # self._print_rows(self._explain(query, *args))
        # self._print_rows(self._explain_query_plan(query, *args))
        r = c.execute(query, *args)
        result = r.fetchall()
        c.close()
        return result

    def fetchone(self, query, *args):
        c = self._sqlconn.cursor()
        # print(query)
        # self._print_rows(self._explain(query, *args))
        # self._print_rows(self._explain_query_plan(query, *args))
        r = c.execute(query, *args)
        result = r.fetchone()
        c.close()
        return result

    def iterall(self, query, *args):
        c = self._sqlconn.cursor()
        # print(query)
        # self._print_rows(self._explain(query, *args))
        # self._print_rows(self._explain_query_plan(query, *args))
        yield from c.execute(query, *args)
        c.close()

    def close(self):
        self._sqlconn.close()

    def commit(self):
        self._sqlconn.commit()

    def rollback(self):
        self._sqlconn.rollback()

    @cached_property
    def last_changelog_serial(self):
        return self.db_read_last_changelog_serial()

    def db_read_last_changelog_serial(self):
        q = 'SELECT MAX(_ROWID_) FROM "changelog" LIMIT 1'
        res = self.fetchone(q)[0]
        return -1 if res is None else res

    def db_read_typedkey(self, relpath):
        q = "SELECT keyname, serial FROM kv WHERE key = ?"
        row = self.fetchone(q, (relpath,))
        if row is None:
            raise KeyError(relpath)
        return tuple(row[:2])

    def db_write_typedkey(self, relpath, name, next_serial):
        q = "INSERT OR REPLACE INTO kv (key, keyname, serial) VALUES (?, ?, ?)"
        self.fetchone(q, (relpath, name, next_serial))

    def write_changelog_entry(self, serial, entry):
        threadlog.debug("writing changelog for serial %s", serial)
        data = dumps(entry)
        self.fetchone(
            "INSERT INTO changelog (serial, data) VALUES (?, ?)",
            (serial, sqlite3.Binary(data)))

    def get_raw_changelog_entry(self, serial):
        q = "SELECT data FROM changelog WHERE serial = ?"
        row = self.fetchone(q, (serial,))
        if row is not None:
            return bytes(row[0])
        return None

    def get_changes(self, serial):
        changes = self._changelog_cache.get(serial, absent)
        if changes is absent:
            data = self.get_raw_changelog_entry(serial)
            changes, rel_renames = loads(data)
            # make values in changes read only so no calling site accidentally
            # modifies data
            changes = ensure_deeply_readonly(changes)
            assert isinstance(changes, ReadonlyView)
            self._changelog_cache.put(serial, changes)
        return changes

    def get_relpath_at(self, relpath, serial):
        result = self._relpath_cache.get((serial, relpath), absent)
        if result is absent:
            result = self._changelog_cache.get((serial, relpath), absent)
        if result is absent:
            changes = self._changelog_cache.get(serial, absent)
            if changes is not absent and relpath in changes:
                (keyname, back_serial, value) = changes[relpath]
                result = (serial, back_serial, value)
        if result is absent:
            result = self._get_relpath_at(relpath, serial)
        if gettotalsizeof(result, maxlen=100000) is None:
            # result is big, put it in the changelog cache,
            # which has fewer entries to preserve memory
            self._changelog_cache.put((serial, relpath), result)
        else:
            # result is small
            self._relpath_cache.put((serial, relpath), result)
        return result

    def iter_relpaths_at(self, typedkeys, at_serial):
        keynames = frozenset(k.name for k in typedkeys)
        keyname_id_values = {"keynameid%i" % i: k for i, k in enumerate(keynames)}
        q = """
            SELECT key, keyname, serial
            FROM kv
            WHERE serial=:serial AND keyname IN (:keynames)
        """
        q = q.replace(':keynames', ", ".join(':' + x for x in keyname_id_values))
        for serial in range(at_serial, -1, -1):
            rows = self.fetchall(q, dict(
                serial=serial,
                **keyname_id_values))
            if not rows:
                continue
            changes = self.get_changes(serial)
            for relpath, keyname, serial in rows:
                (keyname, back_serial, val) = changes[relpath]
                yield RelpathInfo(
                    relpath=relpath, keyname=keyname,
                    serial=serial, back_serial=back_serial,
                    value=val)


@implementer(IStorageConnection2)
class Connection(BaseConnection):
    def io_file_os_path(self, path):
        return None

    def io_file_exists(self, path):
        assert not os.path.isabs(path)
        q = "SELECT path FROM files WHERE path = ?"
        result = self.fetchone(q, (path,))
        return result is not None

    def io_file_set(self, path, content):
        assert not os.path.isabs(path)
        assert not path.endswith("-tmp")
        q = "INSERT OR REPLACE INTO files (path, size, data) VALUES (?, ?, ?)"
        self.fetchone(q, (path, len(content), sqlite3.Binary(content)))
        self.dirty_files[path] = True

    def io_file_open(self, path):
        return py.io.BytesIO(self.io_file_get(path))

    def io_file_get(self, path):
        assert not os.path.isabs(path)
        q = "SELECT data FROM files WHERE path = ?"
        content = self.fetchone(q, (path,))
        if content is None:
            raise IOError()
        return bytes(content[0])

    def io_file_size(self, path):
        assert not os.path.isabs(path)
        q = "SELECT size FROM files WHERE path = ?"
        result = self.fetchone(q, (path,))
        if result is not None:
            return result[0]

    def io_file_delete(self, path):
        assert not os.path.isabs(path)
        q = "DELETE FROM files WHERE path = ?"
        self.fetchone(q, (path,))
        self.dirty_files[path] = None

    def write_transaction(self):
        return Writer(self.storage, self)

    def commit_files_without_increasing_serial(self):
        self.commit()


class BaseStorage(object):
    def __init__(self, basedir, notify_on_commit, cache_size):
        self.basedir = basedir
        self.sqlpath = self.basedir.join(self.db_filename)
        self._notify_on_commit = notify_on_commit
        changelog_cache_size = max(1, cache_size // 20)
        relpath_cache_size = max(1, cache_size - changelog_cache_size)
        self._changelog_cache = LRUCache(changelog_cache_size)  # is thread safe
        self._relpath_cache = LRUCache(relpath_cache_size)  # is thread safe
        self.last_commit_timestamp = time.time()
        self.ensure_tables_exist()

    def _get_sqlconn_uri_kw(self, uri):
        return sqlite3.connect(
            uri, timeout=60, isolation_level=None, uri=True)

    def _get_sqlconn_uri(self, uri):
        return sqlite3.connect(
            uri, timeout=60, isolation_level=None)

    def _get_sqlconn_path(self, uri):
        return sqlite3.connect(
            self.sqlpath.strpath, timeout=60, isolation_level=None)

    def _get_sqlconn(self, uri):
        # we will try different connection methods and overwrite _get_sqlconn
        # with the first successful one
        try:
            # the uri keyword is only supported from Python 3.4 onwards and
            # possibly other Python implementations
            conn = self._get_sqlconn_uri_kw(uri)
            # remember for next time
            self._get_sqlconn = self._get_sqlconn_uri_kw
            return conn
        except TypeError as e:
            if e.args and 'uri' in e.args[0] and 'keyword argument' in e.args[0]:
                threadlog.warn(
                    "The uri keyword for 'sqlite3.connect' isn't supported by "
                    "this Python version.")
            else:
                raise
        except sqlite3.OperationalError as e:
            threadlog.warn("%s" % e)
            threadlog.warn(
                "The installed version of sqlite3 doesn't seem to support "
                "the uri keyword for 'sqlite3.connect'.")
        except sqlite3.NotSupportedError:
            threadlog.warn(
                "The installed version of sqlite3 doesn't support the uri "
                "keyword for 'sqlite3.connect'.")
        try:
            # sqlite3 might be compiled with default URI support
            conn = self._get_sqlconn_uri(uri)
            # remember for next time
            self._get_sqlconn = self._get_sqlconn_uri
            return conn
        except sqlite3.OperationalError as e:
            # log the error and switch to using the path
            threadlog.warn("%s" % e)
            threadlog.warn(
                "Opening the sqlite3 db without options in URI. There is a "
                "higher possibility of read/write conflicts between "
                "threads, causing slowdowns due to retries.")
            conn = self._get_sqlconn_path(uri)
            # remember for next time
            self._get_sqlconn = self._get_sqlconn_path
            return conn

    def get_connection(self, closing=True, write=False):
        # we let the database serialize all writers at connection time
        # to play it very safe (we don't have massive amounts of writes).
        mode = "ro"
        if write:
            mode = "rw"
        if not self.sqlpath.exists():
            mode = "rwc"
        uri = "file:%s?mode=%s" % (self.sqlpath, mode)
        sqlconn = self._get_sqlconn(uri)
        if write:
            start_time = time.monotonic()
            thread = current_thread()
            while 1:
                try:
                    sqlconn.execute("begin immediate")
                    break
                except sqlite3.OperationalError:
                    # another thread may be writing, give it a chance to finish
                    time.sleep(0.01)
                    if hasattr(thread, "exit_if_shutdown"):
                        thread.exit_if_shutdown()
                    if time.monotonic() - start_time > 30:
                        # if it takes this long, something is wrong
                        raise
        conn = self.Connection(sqlconn, self.basedir, self)
        if closing:
            return contextlib.closing(conn)
        return conn

    def _reflect_schema(self):
        result = {}
        with self.get_connection(write=False) as conn:
            c = conn._sqlconn.cursor()
            rows = c.execute("""
                SELECT type, name, sql FROM sqlite_master""")
            for row in rows:
                result.setdefault(row[0], {})[row[1]] = row[2]
        return result

    def ensure_tables_exist(self):
        schema = self._reflect_schema()
        missing = dict()
        for kind, objs in self.expected_schema.items():
            for name, q in objs.items():
                if name not in schema.get(kind, set()):
                    missing.setdefault(kind, dict())[name] = q
        if not missing:
            return
        with self.get_connection(write=True) as conn:
            if not schema:
                threadlog.info("DB: Creating schema")
            else:
                threadlog.info("DB: Updating schema")
            c = conn._sqlconn.cursor()
            for kind in ('table', 'index'):
                objs = missing.pop(kind, {})
                for name in list(objs):
                    q = objs.pop(name)
                    c.execute(q)
                assert not objs
            c.close()
            conn.commit()
        assert not missing


class Storage(BaseStorage):
    Connection = Connection
    db_filename = ".sqlite_db"
    expected_schema = dict(
        index=dict(
            kv_serial_idx="""
                CREATE INDEX kv_serial_idx ON kv (serial);
            """),
        table=dict(
            changelog="""
                CREATE TABLE changelog (
                    serial INTEGER PRIMARY KEY,
                    data BLOB NOT NULL
                )
            """,
            kv="""
                CREATE TABLE kv (
                    key TEXT NOT NULL PRIMARY KEY,
                    keyname TEXT,
                    serial INTEGER
                )
            """,
            files="""
                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    data BLOB NOT NULL
                )
            """))

    def perform_crash_recovery(self):
        pass


@hookimpl
def devpiserver_storage_backend(settings):
    return dict(
        storage=Storage,
        name="sqlite_db_files",
        description="SQLite backend with files in DB for testing only")


@hookimpl
def devpiserver_metrics(request):
    result = []
    xom = request.registry["xom"]
    storage = xom.keyfs._storage
    if not isinstance(storage, BaseStorage):
        return result
    changelog_cache = getattr(storage, '_changelog_cache', None)
    relpath_cache = getattr(storage, '_relpath_cache', None)
    if changelog_cache is None and relpath_cache is None:
        return result
    # get sizes for changelog_cache
    evictions = changelog_cache.evictions if changelog_cache else 0
    hits = changelog_cache.hits if changelog_cache else 0
    lookups = changelog_cache.lookups if changelog_cache else 0
    misses = changelog_cache.misses if changelog_cache else 0
    size = changelog_cache.size if changelog_cache else 0
    # add sizes for relpath_cache
    evictions += relpath_cache.evictions if relpath_cache else 0
    hits += relpath_cache.hits if relpath_cache else 0
    lookups += relpath_cache.lookups if relpath_cache else 0
    misses += relpath_cache.misses if relpath_cache else 0
    size += relpath_cache.size if relpath_cache else 0
    result.extend([
        ('devpi_server_storage_cache_evictions', 'counter', evictions),
        ('devpi_server_storage_cache_hits', 'counter', hits),
        ('devpi_server_storage_cache_lookups', 'counter', lookups),
        ('devpi_server_storage_cache_misses', 'counter', misses),
        ('devpi_server_storage_cache_size', 'gauge', size)])
    if changelog_cache:
        result.extend([
            ('devpi_server_changelog_cache_evictions', 'counter', changelog_cache.evictions),
            ('devpi_server_changelog_cache_hits', 'counter', changelog_cache.hits),
            ('devpi_server_changelog_cache_lookups', 'counter', changelog_cache.lookups),
            ('devpi_server_changelog_cache_misses', 'counter', changelog_cache.misses),
            ('devpi_server_changelog_cache_size', 'gauge', changelog_cache.size),
            ('devpi_server_changelog_cache_items', 'gauge', len(changelog_cache.data) if changelog_cache.data else 0)])
    if relpath_cache:
        result.extend([
            ('devpi_server_relpath_cache_evictions', 'counter', relpath_cache.evictions),
            ('devpi_server_relpath_cache_hits', 'counter', relpath_cache.hits),
            ('devpi_server_relpath_cache_lookups', 'counter', relpath_cache.lookups),
            ('devpi_server_relpath_cache_misses', 'counter', relpath_cache.misses),
            ('devpi_server_relpath_cache_size', 'gauge', relpath_cache.size),
            ('devpi_server_relpath_cache_items', 'gauge', len(relpath_cache.data) if relpath_cache.data else 0)])
    return result


class LazyChangesFormatter:
    __slots__ = ('keys',)

    def __init__(self, changes):
        self.keys = changes.keys()

    def __str__(self):
        return f"keys: {','.join(repr(c) for c in self.keys)}"


class Writer:
    def __init__(self, storage, conn):
        self.conn = conn
        self.storage = storage
        self.changes = {}
        self.commit_serial = conn.last_changelog_serial + 1

    def record_set(self, typedkey, value=None, back_serial=None):
        """ record setting typedkey to value (None means it's deleted) """
        assert not isinstance(value, ReadonlyView), value
        if back_serial is None:
            try:
                _, back_serial = self.conn.db_read_typedkey(typedkey.relpath)
            except KeyError:
                back_serial = -1
        self.conn.db_write_typedkey(typedkey.relpath, typedkey.name, self.commit_serial)
        # at __exit__ time we write out changes to the _changelog_cache
        # so we protect here against the caller modifying the value later
        value = get_mutable_deepcopy(value)
        self.changes[typedkey.relpath] = (typedkey.name, back_serial, value)

    def __enter__(self):
        self.log = thread_push_log("fswriter%s:" % self.commit_serial)
        return self

    def __exit__(self, cls, val, tb):
        commit_serial = self.commit_serial
        thread_pop_log("fswriter%s:" % commit_serial)
        if cls is None:
            entry = self.changes, []
            self.conn.write_changelog_entry(commit_serial, entry)
            self.conn.commit()
            self.log.info("committed at %s", commit_serial)
            self.log.debug(
                "committed: %s", LazyChangesFormatter(self.changes))

            self.storage._notify_on_commit(commit_serial)
        else:
            self.conn.rollback()
            self.log.info("roll back at %s", commit_serial)
