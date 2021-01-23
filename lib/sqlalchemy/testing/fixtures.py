# testing/fixtures.py
# Copyright (C) 2005-2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

import contextlib
import re
import sys

import sqlalchemy as sa
from . import assertions
from . import config
from . import schema
from .entities import BasicEntity
from .entities import ComparableEntity
from .entities import ComparableMixin  # noqa
from .util import adict
from .util import drop_all_tables_from_metadata
from .. import event
from .. import util
from ..orm import declarative_base
from ..orm import registry
from ..orm.decl_api import DeclarativeMeta
from ..schema import sort_tables_and_constraints


@config.mark_base_test_class()
class TestBase(object):
    # A sequence of database names to always run, regardless of the
    # constraints below.
    __allowlist__ = ()

    # A sequence of requirement names matching testing.requires decorators
    __requires__ = ()

    # A sequence of dialect names to exclude from the test class.
    __unsupported_on__ = ()

    # If present, test class is only runnable for the *single* specified
    # dialect.  If you need multiple, use __unsupported_on__ and invert.
    __only_on__ = None

    # A sequence of no-arg callables. If any are True, the entire testcase is
    # skipped.
    __skip_if__ = None

    # if True, the testing reaper will not attempt to touch connection
    # state after a test is completed and before the outer teardown
    # starts
    __leave_connections_for_teardown__ = False

    def assert_(self, val, msg=None):
        assert val, msg

    @config.fixture()
    def connection(self):
        global _connection_fixture_connection

        eng = getattr(self, "bind", None) or config.db

        conn = eng.connect()
        trans = conn.begin()

        _connection_fixture_connection = conn
        yield conn

        _connection_fixture_connection = None

        if trans.is_active:
            trans.rollback()
        # trans would not be active here if the test is using
        # the legacy @provide_metadata decorator still, as it will
        # run a close all connections.
        conn.close()

    @config.fixture()
    def future_connection(self, future_engine, connection):
        # integrate the future_engine and connection fixtures so
        # that users of the "connection" fixture will get at the
        # "future" connection
        yield connection

    @config.fixture()
    def future_engine(self):
        eng = getattr(self, "bind", None) or config.db
        with _push_future_engine(eng):
            yield

    @config.fixture()
    def testing_engine(self):
        from . import engines

        def gen_testing_engine(
            url=None, options=None, future=None, asyncio=False
        ):
            if options is None:
                options = {}
            options["scope"] = "fixture"
            return engines.testing_engine(
                url=url, options=options, future=future, asyncio=asyncio
            )

        yield gen_testing_engine

        engines.testing_reaper._drop_testing_engines("fixture")

    @config.fixture()
    def async_testing_engine(self, testing_engine):
        def go(**kw):
            kw["asyncio"] = True
            return testing_engine(**kw)

        return go

    @config.fixture()
    def metadata(self, request):
        """Provide bound MetaData for a single test, dropping afterwards."""

        from ..sql import schema

        metadata = schema.MetaData()
        request.instance.metadata = metadata
        yield metadata
        del request.instance.metadata

        if (
            _connection_fixture_connection
            and _connection_fixture_connection.in_transaction()
        ):
            trans = _connection_fixture_connection.get_transaction()
            trans.rollback()
            with _connection_fixture_connection.begin():
                drop_all_tables_from_metadata(
                    metadata, _connection_fixture_connection
                )
        else:
            drop_all_tables_from_metadata(metadata, config.db)


_connection_fixture_connection = None


@contextlib.contextmanager
def _push_future_engine(engine):

    from ..future.engine import Engine
    from sqlalchemy import testing

    facade = Engine._future_facade(engine)
    config._current.push_engine(facade, testing)

    yield facade

    config._current.pop(testing)


class FutureEngineMixin(object):
    @config.fixture(autouse=True, scope="class")
    def _push_future_engine(self):
        eng = getattr(self, "bind", None) or config.db
        with _push_future_engine(eng):
            yield


class TablesTest(TestBase):

    # 'once', None
    run_setup_bind = "once"

    # 'once', 'each', None
    run_define_tables = "once"

    # 'once', 'each', None
    run_create_tables = "once"

    # 'once', 'each', None
    run_inserts = "each"

    # 'each', None
    run_deletes = "each"

    # 'once', None
    run_dispose_bind = None

    bind = None
    _tables_metadata = None
    tables = None
    other = None
    sequences = None

    @config.fixture(autouse=True, scope="class")
    def _setup_tables_test_class(self):
        cls = self.__class__
        cls._init_class()

        cls._setup_once_tables()

        cls._setup_once_inserts()

        yield

        cls._teardown_once_metadata_bind()

    @config.fixture(autouse=True, scope="function")
    def _setup_tables_test_instance(self):
        self._setup_each_tables()
        self._setup_each_inserts()

        yield

        self._teardown_each_tables()

    @property
    def tables_test_metadata(self):
        return self._tables_metadata

    @classmethod
    def _init_class(cls):
        if cls.run_define_tables == "each":
            if cls.run_create_tables == "once":
                cls.run_create_tables = "each"
            assert cls.run_inserts in ("each", None)

        cls.other = adict()
        cls.tables = adict()
        cls.sequences = adict()

        cls.bind = cls.setup_bind()
        cls._tables_metadata = sa.MetaData()

    @classmethod
    def _setup_once_inserts(cls):
        if cls.run_inserts == "once":
            cls._load_fixtures()
            with cls.bind.begin() as conn:
                cls.insert_data(conn)

    @classmethod
    def _setup_once_tables(cls):
        if cls.run_define_tables == "once":
            cls.define_tables(cls._tables_metadata)
            if cls.run_create_tables == "once":
                cls._tables_metadata.create_all(cls.bind)
            cls.tables.update(cls._tables_metadata.tables)
            cls.sequences.update(cls._tables_metadata._sequences)

    def _setup_each_tables(self):
        if self.run_define_tables == "each":
            self.define_tables(self._tables_metadata)
            if self.run_create_tables == "each":
                self._tables_metadata.create_all(self.bind)
            self.tables.update(self._tables_metadata.tables)
            self.sequences.update(self._tables_metadata._sequences)
        elif self.run_create_tables == "each":
            self._tables_metadata.create_all(self.bind)

    def _setup_each_inserts(self):
        if self.run_inserts == "each":
            self._load_fixtures()
            with self.bind.begin() as conn:
                self.insert_data(conn)

    def _teardown_each_tables(self):
        if self.run_define_tables == "each":
            self.tables.clear()
            if self.run_create_tables == "each":
                drop_all_tables_from_metadata(self._tables_metadata, self.bind)
            self._tables_metadata.clear()
        elif self.run_create_tables == "each":
            drop_all_tables_from_metadata(self._tables_metadata, self.bind)

        # no need to run deletes if tables are recreated on setup
        if (
            self.run_define_tables != "each"
            and self.run_create_tables != "each"
            and self.run_deletes == "each"
        ):
            with self.bind.begin() as conn:
                for table in reversed(
                    [
                        t
                        for (t, fks) in sort_tables_and_constraints(
                            self._tables_metadata.tables.values()
                        )
                        if t is not None
                    ]
                ):
                    try:
                        conn.execute(table.delete())
                    except sa.exc.DBAPIError as ex:
                        util.print_(
                            ("Error emptying table %s: %r" % (table, ex)),
                            file=sys.stderr,
                        )

    @classmethod
    def _teardown_once_metadata_bind(cls):
        if cls.run_create_tables:
            drop_all_tables_from_metadata(cls._tables_metadata, cls.bind)

        if cls.run_dispose_bind == "once":
            cls.dispose_bind(cls.bind)

        cls._tables_metadata.bind = None

        if cls.run_setup_bind is not None:
            cls.bind = None

    @classmethod
    def setup_bind(cls):
        return config.db

    @classmethod
    def dispose_bind(cls, bind):
        if hasattr(bind, "dispose"):
            bind.dispose()
        elif hasattr(bind, "close"):
            bind.close()

    @classmethod
    def define_tables(cls, metadata):
        pass

    @classmethod
    def fixtures(cls):
        return {}

    @classmethod
    def insert_data(cls, connection):
        pass

    def sql_count_(self, count, fn):
        self.assert_sql_count(self.bind, fn, count)

    def sql_eq_(self, callable_, statements):
        self.assert_sql(self.bind, callable_, statements)

    @classmethod
    def _load_fixtures(cls):
        """Insert rows as represented by the fixtures() method."""
        headers, rows = {}, {}
        for table, data in cls.fixtures().items():
            if len(data) < 2:
                continue
            if isinstance(table, util.string_types):
                table = cls.tables[table]
            headers[table] = data[0]
            rows[table] = data[1:]
        for table, fks in sort_tables_and_constraints(
            cls._tables_metadata.tables.values()
        ):
            if table is None:
                continue
            if table not in headers:
                continue
            with cls.bind.begin() as conn:
                conn.execute(
                    table.insert(),
                    [
                        dict(zip(headers[table], column_values))
                        for column_values in rows[table]
                    ],
                )


class RemovesEvents(object):
    @util.memoized_property
    def _event_fns(self):
        return set()

    def event_listen(self, target, name, fn, **kw):
        self._event_fns.add((target, name, fn))
        event.listen(target, name, fn, **kw)

    @config.fixture(autouse=True, scope="function")
    def _remove_events(self):
        yield
        for key in self._event_fns:
            event.remove(*key)


_fixture_sessions = set()


def fixture_session(**kw):
    kw.setdefault("autoflush", True)
    kw.setdefault("expire_on_commit", True)
    sess = sa.orm.Session(config.db, **kw)
    _fixture_sessions.add(sess)
    return sess


def _close_all_sessions():
    # will close all still-referenced sessions
    sa.orm.session.close_all_sessions()
    _fixture_sessions.clear()


def stop_test_class_inside_fixtures(cls):
    _close_all_sessions()
    sa.orm.clear_mappers()


def after_test():
    if _fixture_sessions:
        _close_all_sessions()


class ORMTest(TestBase):
    pass


class MappedTest(TablesTest, assertions.AssertsExecutionResults):
    # 'once', 'each', None
    run_setup_classes = "once"

    # 'once', 'each', None
    run_setup_mappers = "each"

    classes = None

    @config.fixture(autouse=True, scope="class")
    def _setup_tables_test_class(self):
        cls = self.__class__
        cls._init_class()

        if cls.classes is None:
            cls.classes = adict()

        cls._setup_once_tables()
        cls._setup_once_classes()
        cls._setup_once_mappers()
        cls._setup_once_inserts()

        yield

        cls._teardown_once_class()
        cls._teardown_once_metadata_bind()

    @config.fixture(autouse=True, scope="function")
    def _setup_tables_test_instance(self):
        self._setup_each_tables()
        self._setup_each_classes()
        self._setup_each_mappers()
        self._setup_each_inserts()

        yield

        sa.orm.session.close_all_sessions()
        self._teardown_each_mappers()
        self._teardown_each_classes()
        self._teardown_each_tables()

    @classmethod
    def _teardown_once_class(cls):
        cls.classes.clear()

    @classmethod
    def _setup_once_classes(cls):
        if cls.run_setup_classes == "once":
            cls._with_register_classes(cls.setup_classes)

    @classmethod
    def _setup_once_mappers(cls):
        if cls.run_setup_mappers == "once":
            cls.mapper = cls._generate_mapper()
            cls._with_register_classes(cls.setup_mappers)

    def _setup_each_mappers(self):
        if self.run_setup_mappers == "each":
            self.mapper = self._generate_mapper()
            self._with_register_classes(self.setup_mappers)

    def _setup_each_classes(self):
        if self.run_setup_classes == "each":
            self._with_register_classes(self.setup_classes)

    @classmethod
    def _generate_mapper(cls):
        decl = registry()
        return decl.map_imperatively

    @classmethod
    def _with_register_classes(cls, fn):
        """Run a setup method, framing the operation with a Base class
        that will catch new subclasses to be established within
        the "classes" registry.

        """
        cls_registry = cls.classes

        assert cls_registry is not None

        class FindFixture(type):
            def __init__(cls, classname, bases, dict_):
                cls_registry[classname] = cls
                type.__init__(cls, classname, bases, dict_)

        class _Base(util.with_metaclass(FindFixture, object)):
            pass

        class Basic(BasicEntity, _Base):
            pass

        class Comparable(ComparableEntity, _Base):
            pass

        cls.Basic = Basic
        cls.Comparable = Comparable
        fn()

    def _teardown_each_mappers(self):
        # some tests create mappers in the test bodies
        # and will define setup_mappers as None -
        # clear mappers in any case
        if self.run_setup_mappers != "once":
            sa.orm.clear_mappers()

    def _teardown_each_classes(self):
        if self.run_setup_classes != "once":
            self.classes.clear()

    @classmethod
    def setup_classes(cls):
        pass

    @classmethod
    def setup_mappers(cls):
        pass


class DeclarativeMappedTest(MappedTest):
    run_setup_classes = "once"
    run_setup_mappers = "once"

    @classmethod
    def _setup_once_tables(cls):
        pass

    @classmethod
    def _with_register_classes(cls, fn):
        cls_registry = cls.classes

        class FindFixtureDeclarative(DeclarativeMeta):
            def __init__(cls, classname, bases, dict_):
                cls_registry[classname] = cls
                DeclarativeMeta.__init__(cls, classname, bases, dict_)

        class DeclarativeBasic(object):
            __table_cls__ = schema.Table

        _DeclBase = declarative_base(
            metadata=cls._tables_metadata,
            metaclass=FindFixtureDeclarative,
            cls=DeclarativeBasic,
        )
        cls.DeclarativeBasic = _DeclBase

        # sets up cls.Basic which is helpful for things like composite
        # classes
        super(DeclarativeMappedTest, cls)._with_register_classes(fn)

        if cls._tables_metadata.tables and cls.run_create_tables:
            cls._tables_metadata.create_all(config.db)


class ComputedReflectionFixtureTest(TablesTest):
    run_inserts = run_deletes = None

    __backend__ = True
    __requires__ = ("computed_columns", "table_reflection")

    regexp = re.compile(r"[\[\]\(\)\s`'\"]*")

    def normalize(self, text):
        return self.regexp.sub("", text).lower()

    @classmethod
    def define_tables(cls, metadata):
        from .. import Integer
        from .. import testing
        from ..schema import Column
        from ..schema import Computed
        from ..schema import Table

        Table(
            "computed_default_table",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("normal", Integer),
            Column("computed_col", Integer, Computed("normal + 42")),
            Column("with_default", Integer, server_default="42"),
        )

        t = Table(
            "computed_column_table",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("normal", Integer),
            Column("computed_no_flag", Integer, Computed("normal + 42")),
        )

        if testing.requires.schemas.enabled:
            t2 = Table(
                "computed_column_table",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("normal", Integer),
                Column("computed_no_flag", Integer, Computed("normal / 42")),
                schema=config.test_schema,
            )

        if testing.requires.computed_columns_virtual.enabled:
            t.append_column(
                Column(
                    "computed_virtual",
                    Integer,
                    Computed("normal + 2", persisted=False),
                )
            )
            if testing.requires.schemas.enabled:
                t2.append_column(
                    Column(
                        "computed_virtual",
                        Integer,
                        Computed("normal / 2", persisted=False),
                    )
                )
        if testing.requires.computed_columns_stored.enabled:
            t.append_column(
                Column(
                    "computed_stored",
                    Integer,
                    Computed("normal - 42", persisted=True),
                )
            )
            if testing.requires.schemas.enabled:
                t2.append_column(
                    Column(
                        "computed_stored",
                        Integer,
                        Computed("normal * 42", persisted=True),
                    )
                )
