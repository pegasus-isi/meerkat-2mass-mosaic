#!/usr/bin/env python3
"""Minimal reader/writer for the IPAC table format Montage's mImgtbl emits.

Used on the submit host only, by bin/prefetch.py (to sort raw image tables
deterministically) and by workflow_generator.py (to split a sorted table into
per-chunk tables at DAG-generation time). No workflow job needs it: the chunked
design in SPEC.md re-runs mImgtbl inside the job that assembles a directory,
so tables are never merged by hand.

Why sorting matters: mImgtbl emits rows in directory-listing order, and
mBgModel's iterative global background fit is order-sensitive. Two runs over
the same images can otherwise produce slightly different corrections. Sorting
by filename makes the input to every downstream stage deterministic.

Format handled (see the Montage docs for the full specification):

    \\ optional comment / keyword lines
    |  cntr   |      ra   |     dec   |    fname    |
    |  int    |   double  |  double   |    char     |
    |         |    deg    |   deg     |             |
    |         |   null    |   null    |             |
       1        266.4       -29.2       image.fits

Header lines begin with '|' or '\\'; everything after them is data. Column
extents come from the '|' positions of the first '|' line, which is also how
Montage itself parses these files.
"""

import logging
import os

logger = logging.getLogger(__name__)


class MontageTable:
    """An IPAC table: a block of header lines plus fixed-width data rows."""

    def __init__(self, header_lines, data_rows, columns):
        self.header_lines = header_lines
        self.data_rows = data_rows
        self.columns = columns          # ordered [(name, start, end)]

    # ---------------------------------------------------------------- loading

    @classmethod
    def read(cls, path):
        with open(path) as handle:
            lines = handle.read().splitlines()

        header_lines, data_rows, columns = [], [], None
        for line in lines:
            if line.startswith("\\"):
                header_lines.append(line)
            elif line.startswith("|"):
                header_lines.append(line)
                if columns is None:
                    columns = cls._parse_columns(line)
            elif line.strip():
                data_rows.append(line)

        if columns is None:
            raise ValueError("%s has no '|' header line; not an IPAC table" % path)

        return cls(header_lines, data_rows, columns)

    @staticmethod
    def _parse_columns(line):
        """Column (name, start, end) triples from a '|'-delimited header line."""
        bars = [i for i, char in enumerate(line) if char == "|"]
        columns = []
        for left, right in zip(bars, bars[1:]):
            name = line[left + 1:right].strip()
            columns.append((name, left + 1, right))
        return columns

    # ---------------------------------------------------------------- access

    def column_names(self):
        return [name for name, _, _ in self.columns]

    def value(self, row, name):
        """Field `name` of `row`, stripped.

        Data rows are sometimes shorter than the header (trailing columns
        blank) and sometimes longer (a long filename running past its column),
        so the last column is read to end-of-line.
        """
        for index, (column_name, start, end) in enumerate(self.columns):
            if column_name != name:
                continue
            if index == len(self.columns) - 1:
                return row[start:].strip()
            return row[start:end].strip()
        raise KeyError("no column %r in table (have %s)" % (name, self.column_names()))

    def filename_column(self):
        """The column holding the image filename.

        mImgtbl calls it 'fname'; some Montage versions use 'file'.
        """
        names = self.column_names()
        for candidate in ("fname", "file", "filename"):
            if candidate in names:
                return candidate
        raise KeyError("no filename column in table (have %s)" % names)

    def filenames(self):
        column = self.filename_column()
        return [self.value(row, column) for row in self.data_rows]

    def __len__(self):
        return len(self.data_rows)

    # ---------------------------------------------------------------- writing

    def sorted_by_filename(self):
        """A copy with data rows in filename order -- see the module docstring."""
        column = self.filename_column()
        rows = sorted(self.data_rows, key=lambda row: self.value(row, column))
        return MontageTable(self.header_lines, rows, self.columns)

    def subset(self, rows):
        return MontageTable(self.header_lines, list(rows), self.columns)

    def chunks(self, chunk_size):
        """Split into tables of at most `chunk_size` data rows, order preserved."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1, got %r" % chunk_size)
        return [self.subset(self.data_rows[i:i + chunk_size])
                for i in range(0, len(self.data_rows), chunk_size)]

    def write(self, path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as handle:
            for line in self.header_lines:
                handle.write(line + "\n")
            for row in self.data_rows:
                handle.write(row + "\n")
        return path


def read(path):
    return MontageTable.read(path)


def sort_in_place(path):
    """Rewrite `path` with its rows sorted by filename. Returns the row count."""
    table = MontageTable.read(path).sorted_by_filename()
    table.write(path)
    return len(table)
