"""
Wikimedia articles
------------------

All articles for a given Wikimedia project, specified by language and version.

Records include the following key fields (plus a few others):

    - ``text``: Plain text content of the wiki page -- no wiki markup!
    - ``title``: Title of the wiki page.
    - ``wiki_links``: A list of other wiki pages linked to from this page.
    - ``ext_links``: A list of external URLs linked to from this page.
    - ``categories``: A list of categories to which this wiki page belongs.
    - ``dt_created``: Date on which the wiki page was first created.
    - ``page_id``: Unique identifier of the wiki page, usable in Wikimedia APIs.

Datasets are generated by the Wikimedia Foundation for a variety of projects,
such as Wikipedia and Wikinews. The source files are meant for search indexes,
so they're dumped in Elasticsearch bulk insert format -- basically, a compressed
JSON file with one record per line. For more information, refer to
https://meta.wikimedia.org/wiki/Data_dumps.
"""
from __future__ import annotations

import datetime
import itertools
import logging
import os
import re
import urllib.parse
from typing import Iterable, Optional, Set

import requests
from cytoolz import itertoolz

from .. import constants, types, utils
from .. import io as tio
from .base import Dataset

LOGGER = logging.getLogger(__name__)

METAS = {
    "wikipedia": {
        "site_url": "https://en.wikipedia.org/wiki/Main_Page",
        "description": (
            "All pages for a given language- and version-specific "
            "Wikipedia site snapshot."
        ),
    },
    "wikinews": {
        "site_url": "https://en.wikinews.org/wiki/Main_Page",
        "description": (
            "All pages for a given language- and version-specific "
            "Wikinews site snapshot."
        ),
    },
}
# NOTE: let's use a mirror rather than the official wikimedia host
# see: https://meta.wikimedia.org/wiki/Mirroring_Wikimedia_project_XML_dumps
# DOWNLOAD_ROOT = "https://dumps.wikimedia.org/other/cirrussearch/"
DOWNLOAD_ROOT = "https://dumps.wikimedia.your.org/other/cirrussearch/"


def _is_bad_category_en(cat: str) -> bool:
    return (
        cat == "All stub articles"
        or cat.startswith("Disambiguation pages")
        or re.search(
            r"^(?:All )?(?:Wikipedia )?(?:[Aa]rticles?|[Pp]ages)", cat, flags=re.UNICODE
        )
        is not None
    )


is_bad_category_funcs = {
    "wiki": {
        "de": lambda cat: cat.startswith("Wikipedia:"),
        "en": _is_bad_category_en,
        "nl": lambda cat: cat.startswith("Wikipedia:"),
    },
    "wikinews": {
        "de": lambda cat: cat in {"Artikelstatus: Fertig", "Veröffentlicht"},
        "en": lambda cat: cat in {"Archived", "Published", "AutoArchived", "No publish"},
        "es": lambda cat: cat in {"Archivado", "Artículos publicados"},
        "fr": lambda cat: cat in {"Article archivé", "Article publié"},
        "it": lambda cat: cat in {"Pubblicati"},
        "nl": lambda cat: cat in {"Gepubliceerd"},
        "pt": lambda cat: cat in {"Arquivado", "Publicado"},
    },
}

_bad_wiki_link_starts = {
    "wiki": {
        "de": ("Wikipedia:", "Hilfe:"),
        "el": ("Βοήθεια:",),
        "en": ("Wikipedia:", "Help:"),
        "es": ("Wikipedia:", "Ayuda:"),
        "fr": ("Wikipédia:", "Aide:"),
        "it": ("Wikipedia:", "Aiuto:"),
        "nl": ("Wikipedia:",),
        "pt": ("Wikipédia:", "Ajuda:"),
    },
    "wikinews": {
        "de": ("Wikinews:",),
        "el": ("Βικινέα",),
        "en": ("Wikinews:", "Template:", "User:"),
        "es": ("Wikinoticias:",),
        "fr": ("Wikinews:",),
        "it": ("Wikinotizie:",),
        "nl": ("Wikinieuws:",),
        "pt": ("Wikinotícias:",),
    },
}


class Wikimedia(Dataset):
    """
    Base class for project-specific Wikimedia datasets. See:

    * :class:`Wikipedia`
    * :class:`Wikinews`
    """

    def __init__(
        self,
        name,
        meta,
        project,
        data_dir,
        lang="en",
        version="current",
        namespace=0,
    ):
        super().__init__(name, meta=meta)
        self.lang = lang
        self.version = version
        self.project = project
        self.namespace = int(namespace)
        self._filestub = os.path.join(
            f"{self.lang}{self.project}",
            f"{self.version}",
            f"{self.lang}{self.project}-{self.version}-cirrussearch-content.json.gz",
        )
        self.data_dir = utils.to_path(data_dir).resolve()
        self._filepath = self.data_dir.joinpath(self._filestub)

    @property
    def filepath(self) -> Optional[str]:
        """
        str: Full path on disk for the Wikimedia CirrusSearch db dump
        corresponding to the ``project``, ``lang``, and ``version``.
        """
        if self._filepath.is_file():
            return str(self._filepath)
        else:
            return None

    def download(self, *, force: bool = False) -> None:
        """
        Download the Wikimedia CirrusSearch db dump corresponding to the given
        ``project``, ``lang``, and ``version`` as a compressed JSON file,
        and save it to disk under the ``data_dir`` directory.

        Args:
            force: If True, download the dataset, even if it already exists
                on disk under ``data_dir``.

        Note:
            Some datasets are quite large (e.g. English Wikipedia is ~28GB)
            and can take hours to fully download.
        """
        file_url = self._get_file_url()
        tio.download_file(
            file_url, filename=self._filestub, dirpath=self.data_dir, force=force
        )

    def _get_file_url(self):
        # get dates for the previous two mondays
        # in case it's too soon for the previous week's dump
        if self.version == "current":
            today = datetime.date.today()
            version_dts = (
                today - datetime.timedelta(days=today.weekday()),
                today - datetime.timedelta(days=today.weekday() + 7),
            )
        # otherwise, version should be a date string like YYYYMMDD
        else:
            try:
                version_dts = (
                    datetime.datetime.strptime(self.version, "%Y%m%d").date(),
                )
            except ValueError:
                LOGGER.exception(
                    "version = %s is invalid; must be 'current' "
                    "or a date string like YYYYMMDD",
                    self.version,
                )
                raise
        for version_dt in version_dts:
            file_url = urllib.parse.urljoin(
                DOWNLOAD_ROOT,
                "{version}/{lang}{project}-{version_dt}-cirrussearch-content.json.gz".format(
                    version=self.version,
                    lang=self.lang,
                    project=self.project,
                    version_dt=version_dt.strftime("%Y%m%d"),
                ),
            )
            response = requests.head(file_url)
            if response.status_code == 200:
                return file_url
        # check that the version actually exists...
        response = requests.head(urllib.parse.urljoin(DOWNLOAD_ROOT, self.version))
        if response.status_code != 200:
            raise ValueError(
                f"no Wikimedia CirrusSearch data found for version='{self.version}'; "
                f"check out '{DOWNLOAD_ROOT}' for available data"
            )
        else:
            raise ValueError(
                f"no Wikimedia CirrusSearch data found for version = '{self.version}', "
                f"lang = '{self.lang}', project = '{self.project}'; "
                f"check out '{response.url}' for available data"
            )

    def __iter__(self):
        if not self.filepath:
            raise OSError(
                f"{self.project} database dump file '{self.filepath}' not found; "
                "has the dataset been downloaded yet?"
            )

        is_bad_category = is_bad_category_funcs.get(self.project, {}).get(self.lang)
        bad_wl_starts = _bad_wiki_link_starts.get(self.project, {}).get(
            self.lang, tuple()
        )

        lines = tio.read_json(self.filepath, mode="rb", lines=True)
        for index, source in itertoolz.partition(2, lines):
            if source.get("namespace") != self.namespace:
                continue
            # split opening text from main body text, if available
            opening_text = source.get("opening_text")
            text = source.get("text")
            if opening_text and text and text.startswith(opening_text):
                text = opening_text + "\n\n" + text[len(opening_text) :].strip()
            # do minimal cleaning of categories and wiki links, if available
            if is_bad_category:
                categories = tuple(
                    cat for cat in source.get("category", []) if not is_bad_category(cat)
                )
            else:
                categories = tuple(source.get("category", []))
            wiki_links = tuple(
                wl
                for wl in source.get("outgoing_link", [])
                if not any(wl.startswith(bwls) for bwls in bad_wl_starts)
            )
            yield {
                "page_id": index["index"]["_id"],
                "title": source["title"],
                "text": text,
                "headings": tuple(source.get("heading", [])),
                "wiki_links": wiki_links,
                "ext_links": tuple(
                    urllib.parse.unquote_plus(el)
                    for el in source.get("external_link", [])
                ),
                "categories": categories,
                "dt_created": source.get("create_timestamp"),
                "n_incoming_links": source.get("incoming_links"),
                "popularity_score": source.get("popularity_score"),
            }

    def _get_filters(self, category, wiki_link, min_len):
        filters = []
        if min_len is not None:
            if min_len < 1:
                raise ValueError("`min_len` must be at least 1")
            filters.append(lambda record: len(record.get("text", "")) >= min_len)
        if category is not None:
            category = utils.validate_set_members(
                category, (str, bytes), valid_vals=None
            )
            filters.append(
                lambda record: (
                    record.get("categories")
                    and any(ctgry in record["categories"] for ctgry in category)
                )
            )
        if wiki_link is not None:
            wiki_link = utils.validate_set_members(
                wiki_link, (str, bytes), valid_vals=None
            )
            filters.append(
                lambda record: (
                    record.get("wiki_links")
                    and any(wl in record["wiki_links"] for wl in wiki_link)
                )
            )
        return filters

    def _filtered_iter(self, filters):
        if filters:
            for record in self:
                if all(filter_(record) for filter_ in filters):
                    yield record
        else:
            for record in self:
                yield record

    def texts(
        self,
        *,
        category: Optional[str | Set[str]] = None,
        wiki_link: Optional[str | Set[str]] = None,
        min_len: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Iterable[str]:
        """
        Iterate over wiki pages in this dataset, optionally filtering by a variety
        of metadata and/or text length, and yield texts only,
        in order of appearance in the db dump file.

        Args:
            category: Filter wiki pages by the categories to which they've been assigned.
                For multiple values (Set[str]), ANY rather than ALL of the values
                must be found among a given page's categories.
            wiki_link: Filter wiki pages by the other wiki pages to which they've been linked.
                For multiple values (Set[str]), ANY rather than ALL of the values
                must be found among a given page's wiki links.
            min_len: Filter wiki pages by the length (# characters) of their text content.
            limit: Yield no more than ``limit`` wiki pages that match all specified filters.

        Yields:
            Text of the next wiki page in dataset passing all filters.

        Raises:
            ValueError: If any filtering options are invalid.
        """
        filters = self._get_filters(category, wiki_link, min_len)
        for record in itertools.islice(self._filtered_iter(filters), limit):
            yield record["text"]

    def records(
        self,
        *,
        category: Optional[str | Set[str]] = None,
        wiki_link: Optional[str | Set[str]] = None,
        min_len: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Iterable[types.Record]:
        """
        Iterate over wiki pages in this dataset, optionally filtering by a variety
        of metadata and/or text length, and yield text + metadata pairs,
        in order of appearance in the db dump file.

        Args:
            category: Filter wiki pages by the categories to which they've been assigned.
                For multiple values (Set[str]), ANY rather than ALL of the values
                must be found among a given page's categories.
            wiki_link: Filter wiki pages by the other wiki pages to which they've been linked.
                For multiple values (Set[str]), ANY rather than ALL of the values
                must be found among a given page's wiki links.
            min_len: Filter wiki pages by the length (# characters) of their text content.
            limit: Yield no more than ``limit`` wiki pages that match all specified filters.

        Yields:
            Text of the next wiki page in dataset passing all filters,
            and its corresponding metadata.

        Raises:
            ValueError: If any filtering options are invalid.
        """
        filters = self._get_filters(category, wiki_link, min_len)
        for record in itertools.islice(self._filtered_iter(filters), limit):
            yield types.Record(text=record.pop("text"), meta=record)


class Wikipedia(Wikimedia):
    """
    Stream a collection of Wikipedia pages from a version- and language-specific
    database dump, either as texts or text + metadata pairs.

    Download a database dump (one time only!) and save its contents to disk::

        >>> import textacy.datasets
        >>> ds = textacy.datasets.Wikipedia(lang="en", version="current")
        >>> ds.download()
        >>> ds.info
        {'name': 'wikipedia',
         'site_url': 'https://en.wikipedia.org/wiki/Main_Page',
         'description': 'All pages for a given language- and version-specific Wikipedia site snapshot.'}

    Iterate over wiki pages as texts or records with both text and metadata::

        >>> for text in ds.texts(limit=5):
        ...     print(text[:500])
        >>> for text, meta in ds.records(limit=5):
        ...     print(meta["page_id"], meta["title"])

    Filter wiki pages by a variety of metadata fields and text length::

        >>> for text, meta in ds.records(category="Living people", limit=5):
        ...     print(meta["title"], meta["categories"])
        >>> for text, meta in ds.records(wiki_link="United_States", limit=5):
        ...     print(meta["title"], meta["wiki_links"])
        >>> for text in ds.texts(min_len=10000, limit=5):
        ...     print(len(text))

    Stream wiki pages into a :class:`textacy.Corpus <textacy.corpus.Corpus>`::

        >>> textacy.Corpus("en", data=ds.records(min_len=2000, limit=50))
        Corpus(50 docs; 72368 tokens)

    Args:
        data_dir: Path to directory on disk under which database dump files are stored.
            Each file is expected as
            ``{lang}{project}/{version}/{lang}{project}-{version}-cirrussearch-content.json.gz``
            immediately under this directory.
        lang: Standard two-letter language code, e.g. "en" => "English", "de" => "German".
            https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
        version: Database dump version to use. Either "current" for the most recently
            available version or a date formatted as "YYYYMMDD".
            Dumps are produced weekly; check for available versions at
            https://dumps.wikimedia.org/other/cirrussearch/.
        namespace: Namespace of the wiki pages to include. Typical, public-
            facing content is in the 0 (default) namespace.
    """

    def __init__(
        self,
        data_dir: types.PathLike = constants.DEFAULT_DATA_DIR.joinpath("wikipedia"),
        lang: str = "en",
        version: str = "current",
        namespace: int = 0,
    ):
        super().__init__(
            "wikipedia",
            METAS["wikipedia"],
            "wiki",
            data_dir,
            lang=lang,
            version=version,
            namespace=namespace,
        )


class Wikinews(Wikimedia):
    """
    Stream a collection of Wikinews pages from a version- and language-specific
    database dump, either as texts or text + metadata pairs.

    Download a database dump (one time only!) and save its contents to disk::

        >>> import textacy.datasets
        >>> ds = textacy.datasets.Wikinews(lang="en", version="current")
        >>> ds.download()
        >>> ds.info
        {'name': 'wikinews',
         'site_url': 'https://en.wikinews.org/wiki/Main_Page',
         'description': 'All pages for a given language- and version-specific Wikinews site snapshot.'}

    Iterate over wiki pages as texts or records with both text and metadata::

        >>> for text in ds.texts(limit=5):
        ...     print(text[:500])
        >>> for text, meta in ds.records(limit=5):
        ...     print(meta["page_id"], meta["title"])

    Filter wiki pages by a variety of metadata fields and text length::

        >>> for text, meta in ds.records(category="Politics and conflicts", limit=5):
        ...     print(meta["title"], meta["categories"])
        >>> for text, meta in ds.records(wiki_link="Reuters", limit=5):
        ...     print(meta["title"], meta["wiki_links"])
        >>> for text in ds.texts(min_len=5000, limit=5):
        ...     print(len(text))

    Stream wiki pages into a :class:`textacy.Corpus <textacy.corpus.Corpus>`::

        >>> textacy.Corpus("en", data=ds.records(limit=100))
        Corpus(100 docs; 33092 tokens)

    Args:
        data_dir: Path to directory on disk under which database dump files are stored.
            Each file is expected as
            ``{lang}{project}/{version}/{lang}{project}-{version}-cirrussearch-content.json.gz``
            immediately under this directory.
        lang: Standard two-letter language code, e.g. "en" => "English", "de" => "German".
            https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
        version: Database dump version to use. Either "current" for the most recently
            available version or a date formatted as "YYYYMMDD".
            Dumps are produced weekly; check for available versions at
            https://dumps.wikimedia.org/other/cirrussearch/.
        namespace: Namespace of the wiki pages to include. Typical, public-
            facing content is in the 0 (default) namespace.
    """

    def __init__(
        self,
        data_dir: types.PathLike = constants.DEFAULT_DATA_DIR.joinpath("wikinews"),
        lang: str = "en",
        version: str = "current",
        namespace: int = 0,
    ):
        super().__init__(
            "wikinews",
            METAS["wikinews"],
            "wikinews",
            data_dir,
            lang=lang,
            version=version,
            namespace=namespace,
        )
