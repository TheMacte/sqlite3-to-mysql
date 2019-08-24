import logging
import sqlite3
import sys
import re
from math import ceil
from os.path import realpath, isfile

import mysql.connector
from mysql.connector import errorcode
from tqdm import tqdm


class SQLite3toMySQL:
    """ Use this class to transfer an SQLite 3 database to MySQL.
    """

    COLUMN_PATTERN = re.compile(r"^[^(]+")

    def __init__(self, **kwargs):
        self._sqlite_file = kwargs.get("sqlite_file") or None
        if not self._sqlite_file:
            raise ValueError("Please provide an SQLite file")
        elif not isfile(self._sqlite_file):
            raise FileNotFoundError("SQLite file does not exist")

        self._mysql_user = kwargs.get("mysql_user") or None
        if not self._mysql_user:
            raise ValueError("Please provide a MySQL user")
        self._mysql_user = str(self._mysql_user)

        self._mysql_password = kwargs.get("mysql_password") or None
        if self._mysql_password:
            self._mysql_password = str(self._mysql_password)

        self._mysql_host = kwargs.get("mysql_host") or "localhost"
        if self._mysql_host:
            self._mysql_host = str(self._mysql_host)

        self._mysql_port = kwargs.get("mysql_port") or 3306
        if self._mysql_port:
            self._mysql_port = int(self._mysql_port)

        self._chunk_size = kwargs.get("chunk") or None
        if self._chunk_size:
            self._chunk_size = int(self._chunk_size)

        self._logger = self._setup_logger(log_file=kwargs.get("log_file") or None)

        self._mysql_database = kwargs.get("mysql_database", "transfer")

        self._mysql_integer_type = kwargs.get("mysql_integer_type", "int(11)")

        self._mysql_string_type = kwargs.get("mysql_string_type", "varchar(300)")

        self._sqlite = sqlite3.connect(kwargs.get("sqlite_file") or None)
        self._sqlite.row_factory = sqlite3.Row

        self._sqlite_cur = self._sqlite.cursor()

        try:
            self._mysql = mysql.connector.connect(
                user=self._mysql_user,
                password=self._mysql_password,
                host=self._mysql_host,
                port=self._mysql_port,
                use_pure=True,
            )
            if not self._mysql.is_connected():
                raise ConnectionError("Unable to connect to MySQL")

            self._mysql_cur = self._mysql.cursor(prepared=True)
            try:
                self._mysql.database = self._mysql_database
            except mysql.connector.Error as err:
                if err.errno == errorcode.ER_BAD_DB_ERROR:
                    self._create_database()
                else:
                    self._logger.error(err)
                    raise
        except mysql.connector.Error as err:
            self._logger.error(err)
            raise

    @classmethod
    def _setup_logger(cls, log_file=None):
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        screen_handler = logging.StreamHandler(stream=sys.stdout)
        screen_handler.setFormatter(formatter)
        logger = logging.getLogger(cls.__name__)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(screen_handler)

        if log_file:
            file_handler = logging.FileHandler(realpath(log_file), mode="w")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger

    def _create_database(self):
        try:
            self._mysql_cur.execute(
                "CREATE DATABASE IF NOT EXISTS `{}` DEFAULT CHARACTER SET 'utf8'".format(
                    self._mysql_database
                )
            )
            self._mysql_cur.close()
            self._mysql.commit()
            self._mysql.database = self._mysql_database
            self._mysql_cur = self._mysql.cursor(prepared=True)
        except mysql.connector.Error as err:
            self._logger.error(
                "_create_database failed creating databse {}: {}".format(
                    self._mysql_database, err
                )
            )
            raise

    @classmethod
    def _valid_column_type(cls, column_type):
        return cls.COLUMN_PATTERN.match(column_type.strip())

    @classmethod
    def _translate_type_from_sqlite_to_mysql(cls, column_type):
        """ This method could be optimized even further, however at the time of writing it
            seemed adequate enough.
        """
        full_column_type = column_type.upper()
        match = cls._valid_column_type(column_type)
        if not match:
            raise ValueError("Invalid column_type!")

        data_type = match.group(0).upper()
        if data_type in {"TEXT", "CLOB"}:
            return "TEXT"
        elif data_type in {"CHARACTER", "NCHAR", "NATIVE CHARACTER"}:
            return "CHAR" + cls._column_type_length(column_type)
        elif data_type in {"VARYING CHARACTER", "NVARCHAR", "VARCHAR"}:
            return "VARCHAR" + cls._column_type_length(column_type, 255)
        elif data_type == "DOUBLE PRECISION":
            return "DOUBLE"
        elif data_type == "UNSIGNED BIG INT":
            return "BIGINT" + cls._column_type_length(column_type) + " UNSIGNED"
        elif data_type in {"INT1", "INT2"}:
            return "INT"
        else:
            return full_column_type

    @staticmethod
    def _column_type_length(column_type, default=None):
        suffix = re.search(r"\(\d+\)$", column_type)
        if suffix:
            return suffix.group(0)
        elif default:
            return "({})".format(default)
        return ""

    def _create_table(self, table_name):
        primary_key = ""

        sql = "CREATE TABLE IF NOT EXISTS `{}` ( ".format(table_name)

        self._sqlite_cur.execute('PRAGMA table_info("{}")'.format(table_name))

        for row in self._sqlite_cur.fetchall():
            column = dict(row)
            sql += " `{name}` {type} {notnull} {auto_increment}, ".format(
                name=column["name"],
                type=self._translate_type_from_sqlite_to_mysql(column["type"]),
                notnull="NOT NULL" if column["notnull"] else "NULL",
                auto_increment="AUTO_INCREMENT"
                if column["pk"]
                   and self._translate_type_from_sqlite_to_mysql(column["type"])
                   in {"INT", "BIGINT"}
                else "",
            )
            if column["pk"]:
                primary_key = column["name"]

        sql = sql.rstrip(", ")
        if primary_key:
            sql += ", PRIMARY KEY (`{}`)".format(primary_key)
        sql += " ) ENGINE = InnoDB CHARACTER SET utf8"
        sql = " ".join(sql.split())

        try:
            self._mysql_cur.execute(sql)
            self._mysql.commit()
        except mysql.connector.Error as err:
            self._logger.error(
                "_create_table failed creating table {}: {}".format(table_name, err)
            )
            raise

    def _transfer_table_data(self, sql, total_records=0):
        if self._chunk_size is not None and self._chunk_size > 0:
            for _ in tqdm(range(0, ceil(total_records / self._chunk_size))):
                self._mysql_cur.executemany(
                    sql,
                    (
                        tuple(row)
                        for row in self._sqlite_cur.fetchmany(self._chunk_size)
                    ),
                )
                self._mysql.commit()
        else:
            self._mysql_cur.executemany(
                sql, (tuple(row) for row in self._sqlite_cur.fetchall())
            )
            self._mysql.commit()

    def transfer(self):
        """ The primary and only method with which we transfer the data
        """
        self._sqlite_cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        for row in self._sqlite_cur.fetchall():
            table = dict(row)

            # create the table
            self._create_table(table["name"])

            # get the size of the data
            self._sqlite_cur.execute(
                'SELECT COUNT(*) AS total_records FROM "{}"'.format(table["name"])
            )
            total_records = int(dict(self._sqlite_cur.fetchone())["total_records"])

            # only continue if there is anything to transfer
            if total_records > 0:
                # populate it
                self._logger.info("Transferring table {}".format(table["name"]))
                self._sqlite_cur.execute('SELECT * FROM "{}"'.format(table["name"]))
                columns = [column[0] for column in self._sqlite_cur.description]
                sql = "INSERT IGNORE INTO `{table}` ({fields}) VALUES ({placeholders})".format(
                    table=table["name"],
                    fields=("`{}`, " * len(columns)).rstrip(" ,").format(*columns),
                    placeholders=("%s, " * len(columns)).rstrip(" ,"),
                )
                try:
                    self._transfer_table_data(sql=sql, total_records=total_records)
                except mysql.connector.Error as err:
                    self._logger.error(
                        "transfer failed inserting data into table {}: {}".format(
                            table["name"], err
                        )
                    )
                    raise
        self._logger.info("Done!")
