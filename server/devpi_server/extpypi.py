"""

Implementation of the database layer for PyPI Package serving and
toxresult storage.

"""

from __future__ import unicode_literals

import asyncio
import time

import re
from devpi_common.vendor._pip import HTMLPage
from devpi_common.url import URL
from devpi_common.metadata import BasenameMeta
from devpi_common.metadata import is_archive_of_project
from devpi_common.validation import normalize_name
from functools import partial
from html.parser import HTMLParser
from .config import hookimpl
from .exceptions import lazy_format_exception
from .filestore import key_from_link
from .model import BaseStageCustomizer
from .model import BaseStage, SimplelinkMeta
from .model import ensure_boolean
from .model import join_links_data
from .readonly import ensure_deeply_readonly
from .log import threadlog
from .views import make_uuid_headers


class Link(URL):
    def __init__(self, url="", *args, **kwargs):
        self.requires_python = kwargs.pop('requires_python', None)
        self.yanked = kwargs.pop('yanked', None)
        URL.__init__(self, url, *args, **kwargs)


class ProjectParser(HTMLParser):
    def __init__(self, url):
        HTMLParser.__init__(self)
        self.projects = set()
        self.baseurl = URL(url)
        self.basehost = self.baseurl.replace(path='')
        self.project = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.project = None
            attrs = dict(attrs)
            if 'href' not in attrs:
                return
            href = attrs['href']
            if '://' not in href:
                project = href.rstrip('/').rsplit('/', 1)[-1]
            else:
                newurl = self.baseurl.joinpath(href)
                # remove trailing slashes, so basename works correctly
                newurl = newurl.asfile()
                if not newurl.is_valid_http_url():
                    return
                if not newurl.path.startswith(self.baseurl.path):
                    return
                if self.basehost != newurl.replace(path=''):
                    return
                project = newurl.basename
            self.project = project

    def handle_endtag(self, tag):
        if tag == 'a' and self.project:
            self.projects.add(self.project)
            self.project = None


class IndexParser:

    def __init__(self, project):
        self.project = normalize_name(project)
        self.basename2link = {}

    def _mergelink_ifbetter(self, newlink):
        """
        Stores a link given it's better fit than an existing one (if any).

        A link with hash_spec replaces one w/o it, even if the latter got other
        non-empty attributes (like requires_python), unlike the former.
        As soon as the first link with hash_spec is encountered, those that
        appear later are ignored.
        """
        entry = self.basename2link.get(newlink.basename)
        if entry is None or not entry.hash_spec and (newlink.hash_spec or (
            not entry.requires_python and newlink.requires_python
        )):
            self.basename2link[newlink.basename] = newlink
            threadlog.debug("indexparser: adding link %s", newlink)
        else:
            threadlog.debug("indexparser: ignoring candidate link %s", newlink)

    @property
    def releaselinks(self):
        # the BasenameMeta wrapping essentially does link validation
        return [BasenameMeta(x).obj for x in self.basename2link.values()]

    def parse_index(self, disturl, html):
        p = HTMLPage(html, disturl.url)
        seen = set()
        for link in p.links:
            newurl = Link(link.url, requires_python=link.requires_python, yanked=link.yanked)
            if not newurl.is_valid_http_url():
                continue
            if is_archive_of_project(newurl, self.project):
                if not newurl.is_valid_http_url():
                    threadlog.warn("unparsable/unsupported url: %r", newurl)
                else:
                    seen.add(newurl.url)
                    self._mergelink_ifbetter(newurl)
                    continue


def parse_index(disturl, html):
    if not isinstance(disturl, URL):
        disturl = URL(disturl)
    project = disturl.basename or disturl.parentbasename
    parser = IndexParser(project)
    parser.parse_index(disturl, html)
    return parser


class PyPIStage(BaseStage):
    def __init__(self, xom, username, index, ixconfig, customizer_cls):
        super(PyPIStage, self).__init__(
            xom, username, index, ixconfig, customizer_cls)
        self.xom = xom
        self.offline = self.xom.config.offline_mode
        self.timeout = xom.config.request_timeout
        # list of locally mirrored projects
        self.key_projects = self.keyfs.PROJNAMES(user=username, index=index)
        # used to log about stale projects only once
        self._offline_logging = set()

    def _get_extra_headers(self, extra_headers):
        if self.xom.is_replica():
            if extra_headers is None:
                extra_headers = {}
            (uuid, master_uuid) = make_uuid_headers(self.xom.config.nodeinfo)
            rt = self.xom.replica_thread
            token = rt.auth_serializer.dumps(uuid)
            extra_headers[rt.H_REPLICA_UUID] = uuid
            extra_headers[str('Authorization')] = 'Bearer %s' % token
        return extra_headers

    async def async_httpget(self, url, allow_redirects, timeout=None, extra_headers=None):
        extra_headers = self._get_extra_headers(extra_headers)
        return await self.xom.async_httpget(
            url=URL(url).url, allow_redirects=allow_redirects, timeout=timeout,
            extra_headers=extra_headers)

    def httpget(self, url, allow_redirects, timeout=None, extra_headers=None):
        extra_headers = self._get_extra_headers(extra_headers)
        return self.xom.httpget(
            url=URL(url).url, allow_redirects=allow_redirects, timeout=timeout,
            extra_headers=extra_headers)

    @property
    def cache_expiry(self):
        return self.ixconfig.get(
            'mirror_cache_expiry', self.xom.config.mirror_cache_expiry)

    @property
    def mirror_url(self):
        if self.xom.is_replica():
            url = self.xom.config.master_url.joinpath(self.name, "+simple")
        else:
            url = URL(self.ixconfig['mirror_url'])
        return url.asdir()

    @property
    def mirror_url_auth(self):
        url = self.mirror_url
        return dict(username=url.username, password=url.password)

    @property
    def use_external_url(self):
        return self.ixconfig.get('mirror_use_external_urls', False)

    def get_possible_indexconfig_keys(self):
        return tuple(dict(self.get_default_config_items())) + (
            "custom_data", "description",
            "mirror_url", "mirror_cache_expiry",
            "mirror_use_external_urls",
            "mirror_web_url_fmt", "title")

    def get_default_config_items(self):
        return [("volatile", True)]

    def normalize_indexconfig_value(self, key, value):
        if key == "volatile":
            return ensure_boolean(value)
        if key == "mirror_url":
            if not value.startswith(("http://", "https://")):
                raise self.InvalidIndexconfig([
                    "'mirror_url' option must be a URL."])
            return value
        if key == "mirror_cache_expiry":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise self.InvalidIndexconfig([
                    "'mirror_cache_expiry' option must be an integer"])
            return value
        if key == "mirror_use_external_urls":
            return ensure_boolean(value)
        if key in ("custom_data", "description", "mirror_web_url_fmt", "title"):
            return value

    def delete(self):
        # delete all projects on this index
        for name in self.key_projects.get():
            self.del_project(name)
        self.key_projects.delete()
        BaseStage.delete(self)

    def add_project_name(self, project):
        project = normalize_name(project)
        projects = self.key_projects.get(readonly=False)
        if project not in projects:
            projects.add(project)
            self.key_projects.set(projects)

    def del_project(self, project):
        if not self.is_project_cached(project):
            raise KeyError("project not found")
        (is_expired, links, cache_serial) = self._load_cache_links(project)
        if links is not None:
            entries = (self._entry_from_href(x[1]) for x in links)
            entries = (x for x in entries if x.file_exists())
            for entry in entries:
                entry.delete()
        self.key_projsimplelinks(project).delete()
        projects = self.key_projects.get(readonly=False)
        if project in projects:
            projects.remove(project)
            self.cache_retrieve_times.expire(project)
            self.key_projects.set(projects)

    def del_versiondata(self, project, version, cleanup=True):
        project = normalize_name(project)
        if not self.has_project_perstage(project):
            raise self.NotFound("project %r not found on stage %r" %
                                (project, self.name))
        # since this is a mirror, we only have the simple links and no
        # metadata, so only delete the files and keep the simple links
        # for the possibility to re-download a release
        (is_expired, links, cache_serial) = self._load_cache_links(project)
        if links is not None:
            entries = list(self._entry_from_href(x[1]) for x in links)
            entries = list(x for x in entries if x.version == version and x.file_exists())
            for entry in entries:
                entry.file_delete()

    def del_entry(self, entry, cleanup=True):
        project = entry.project
        if project is None:
            raise self.NotFound("no project set on entry %r" % entry)
        if not entry.file_exists():
            raise self.NotFound("entry has no file data %r" % entry)
        entry.delete()
        (is_expired, links, cache_serial) = self._load_cache_links(project)
        if links is not None:
            links = ensure_deeply_readonly(
                list(filter(self._is_file_cached, links)))
        if not links and cleanup:
            projects = self.key_projects.get(readonly=False)
            if project in projects:
                projects.remove(project)
                self.key_projects.set(projects)

    @property
    def cache_projectnames(self):
        """ cache for full list of projectnames. """
        # we could keep this info inside keyfs but pypi.org
        # produces a several MB list of names and it changes often which
        # would spam the database.
        try:
            return self.xom.get_singleton(self.name, "projectnames")
        except KeyError:
            cache_projectnames = ProjectNamesCache()
            self.xom.set_singleton(self.name, "projectnames", cache_projectnames)
            return cache_projectnames

    @property
    def cache_retrieve_times(self):
        """ per-xom RAM cache for keeping track when we last updated simplelinks. """
        # we could keep this info in keyfs but it would lead to a write
        # for each remote check.
        try:
            return self.xom.get_singleton(self.name, "project_retrieve_times")
        except KeyError:
            c = ProjectUpdateCache()
            self.xom.set_singleton(self.name, "project_retrieve_times", c)
            return c

    def _get_remote_projects(self):
        headers = {"Accept": "text/html"}
        # use a minimum of 30 seconds as timeout for remote server and
        # 60s when running as replica, because the list can be quite large
        # and the master might take a while to process it
        if self.xom.is_replica():
            timeout = max(self.timeout, 60)
        else:
            timeout = max(self.timeout, 30)
        response = self.httpget(
            self.mirror_url, allow_redirects=True, extra_headers=headers,
            timeout=timeout)
        if response.status_code != 200:
            raise self.UpstreamError(
                "URL %r returned %s %s",
                self.mirror_url, response.status_code, response.reason)
        parser = ProjectParser(response.url)
        parser.feed(response.text)
        return parser.projects

    def list_projects_perstage(self):
        """ return set of all projects served through the mirror. """
        if self.offline:
            threadlog.warn("offline mode: using stale projects list")
            return self.key_projects.get()
        if not self.cache_projectnames.is_expired(self.cache_expiry):
            projects = self.cache_projectnames.get()
        else:
            # no fresh projects or None at all, let's go remote
            try:
                projects = self._get_remote_projects()
            except self.UpstreamError as e:
                threadlog.warn(
                    "upstream error (%s): using stale projects list" % e)
                return self.key_projects.get()
            else:
                old = self.cache_projectnames.get()
                if not self.cache_projectnames.exists() or old != projects:
                    self.cache_projectnames.set(projects)

                    # trigger an initial-load event on master
                    if not self.xom.is_replica():
                        # make sure we are at the current serial
                        # this avoids setting the value again when
                        # called from the notification thread
                        self.keyfs.restart_read_transaction()
                        k = self.keyfs.MIRRORNAMESINIT(user=self.username, index=self.index)
                        if k.get() == 0:
                            self.keyfs.restart_as_write_transaction()
                            k.set(1)
                else:
                    # mark current without updating contents
                    self.cache_projectnames.mark_current()

        return projects

    def is_project_cached(self, project):
        """ return True if we have some cached simpelinks information. """
        return self.key_projsimplelinks(project).exists()

    def _save_cache_links(self, project, links, requires_python, yanked, serial):
        assert links != ()  # we don't store the old "Not Found" marker anymore
        assert isinstance(serial, int)
        assert project == normalize_name(project), project
        data = {
            "serial": serial, "links": links,
            "requires_python": requires_python,
            "yanked": yanked}
        key = self.key_projsimplelinks(project)
        old = key.get()
        if old != data:
            threadlog.debug("saving changed simplelinks for %s: %s", project, data)
            key.set(data)
            # maintain list of currently cached project names to enable
            # deletion and offline mode
            self.add_project_name(project)

        def on_commit():
            threadlog.debug("setting projects cache for %r", project)
            self.cache_retrieve_times.refresh(project)
            # make project appear in projects list even
            # before we next check up the full list with remote
            self.cache_projectnames.add(project)

        self.keyfs.tx.on_commit_success(on_commit)

    def _load_cache_links(self, project):
        is_expired, links_with_data, serial = True, None, -1

        cache = self.key_projsimplelinks(project).get()
        if cache:
            is_expired = self.cache_retrieve_times.is_expired(project, self.cache_expiry)
            serial = cache["serial"]
            links_with_data = join_links_data(
                cache["links"],
                cache.get("requires_python", []),
                cache.get("yanked", []))
            if self.offline and links_with_data:
                links_with_data = ensure_deeply_readonly(list(
                    filter(self._is_file_cached, links_with_data)))

        return is_expired, links_with_data, serial

    def _entry_from_href(self, href):
        # extract relpath from href by cutting of the hash
        relpath = re.sub(r"#.*$", "", href)
        return self.filestore.get_file_entry(relpath)

    def _is_file_cached(self, link):
        entry = self._entry_from_href(link[1])
        return entry is not None and entry.file_exists()

    def clear_simplelinks_cache(self, project):
        # we have to set to an empty dict instead of removing the key, so
        # replicas behave correctly
        self.cache_retrieve_times.expire(project)
        self.key_projsimplelinks(project).set({})
        threadlog.debug("cleared cache for %s", project)

    async def _async_fetch_releaselinks(self, newlinks_future, project, cache_serial, _key_from_link):
        # get the simple page for the project
        url = self.mirror_url.joinpath(project).asdir()
        threadlog.debug("reading index %r", url)
        (response, text) = await self.async_httpget(url, allow_redirects=True)
        if response.status != 200:
            if response.status == 404:
                self.cache_retrieve_times.refresh(project)
                raise self.UpstreamNotFoundError(
                    "not found on GET %r" % url)

            # we don't have an old result and got a non-404 code.
            raise self.UpstreamError("%s status on GET %r" % (
                response.status, url))

        # pypi.org provides X-PYPI-LAST-SERIAL header in case of 200 returns.
        # devpi-master may provide a 200 but not supply the header
        # (it's really a 404 in disguise and we should change
        # devpi-server behaviour since pypi.org serves 404
        # on non-existing projects for a longer time now).
        # Returning a 200 with "no such project" was originally meant to
        # provide earlier versions of easy_install/pip to request the full
        # simple page.
        try:
            serial = int(response.headers.get(str("X-PYPI-LAST-SERIAL")))
        except (TypeError, ValueError):
            # handle missing or invalid X-PYPI-LAST-SERIAL header
            serial = -1

        if serial < cache_serial:
            raise self.UpstreamError(
                "serial mismatch on GET %r, "
                "cache_serial %s is newer than returned serial %s" % (
                    url, cache_serial, serial))

        threadlog.debug("%s: got response with serial %s", project, serial)

        # check returned url has the same normalized name
        assert project == normalize_name(url.asfile().basename)

        # make sure we don't store credential in the database
        response_url = URL(response.url).replace(username=None, password=None)
        # parse simple index's link
        releaselinks = parse_index(response_url, text).releaselinks
        num_releaselinks = len(releaselinks)
        key_hrefs = [None] * num_releaselinks
        requires_python = [None] * num_releaselinks
        yanked = [None] * num_releaselinks
        for index, releaselink in enumerate(releaselinks):
            key = _key_from_link(releaselink)
            href = key.relpath
            if releaselink.hash_spec:
                href = f"{href}#{releaselink.hash_spec}"
            key_hrefs[index] = (releaselink.basename, href)
            requires_python[index] = releaselink.requires_python
            yanked[index] = releaselink.yanked
        newlinks_future.set_result(dict(
            serial=serial,
            releaselinks=releaselinks,
            key_hrefs=key_hrefs,
            requires_python=requires_python,
            yanked=yanked,
            devpi_serial=response.headers.get("X-DEVPI-SERIAL")))

    def _update_simplelinks(self, project, info, newlinks):
        if self.xom.is_replica():
            # on the replica we wait for the changes to arrive (changes were
            # triggered by our http request above) because we have no direct
            # writeaccess to the db other than through the replication thread
            # and we need the current data of the new entries
            devpi_serial = int(info["devpi_serial"])
            threadlog.debug("get_simplelinks pypi: waiting for devpi_serial %r",
                            devpi_serial)
            links = None
            if self.keyfs.wait_tx_serial(devpi_serial, timeout=self.timeout):
                threadlog.debug("get_simplelinks pypi: finished waiting for devpi_serial %r",
                                devpi_serial)
                # XXX raise TransactionRestart to get a consistent clean view
                self.keyfs.restart_read_transaction()
                is_expired, links, cache_serial = self._load_cache_links(project)
            if links is not None:
                self.cache_retrieve_times.refresh(project)
                return links
            raise self.UpstreamError("no cache links from master for %s" %
                                     project)
        else:
            # on the master we need to write the updated links.
            if not self.keyfs.tx.write:
                # we are in a read-only transaction so we need to start a
                # write transaction.
                self.keyfs.restart_as_write_transaction()

            maplink = partial(
                self.filestore.maplink,
                user=self.user.name, index=self.index, project=project)
            # calling maplink on the links creates the entries in the database
            for x in info["releaselinks"]:
                maplink(x)
            # this stores the simple links info
            self._save_cache_links(
                project,
                info["key_hrefs"], info["requires_python"], info["yanked"],
                info["serial"])
            return newlinks

    async def _update_simplelinks_in_future(self, newlinks_future, project):
        threadlog.debug("Awaiting simple links for %r", project)
        info = await newlinks_future
        threadlog.debug("Got simple links for %r", project)

        newlinks = join_links_data(
            info["key_hrefs"], info["requires_python"], info["yanked"])
        with self.keyfs.transaction(write=True):
            # fetch current links
            (is_expired, links, cache_serial) = self._load_cache_links(project)
            if links is None or set(links) != set(newlinks):
                # we got changes, so store them
                self._update_simplelinks(project, info, newlinks)
                threadlog.debug(
                    "Updated simplelinks for %r in background", project)
            else:
                threadlog.debug("Unchanged simplelinks for %r", project)

    def get_simplelinks_perstage(self, project):
        """ return all releaselinks from the index, returning cached entries
        if we have a recent enough request stored locally.

        Raise UpstreamError if the pypi server cannot be reached or
        does not return a fresh enough page although we know it must
        exist.
        """
        project = normalize_name(project)
        is_expired, links, cache_serial = self._load_cache_links(project)
        if self.offline and links is None:
            raise self.UpstreamError("offline mode")
        if self.offline or not is_expired:
            if self.offline and project not in self._offline_logging:
                threadlog.debug(
                    "using stale links for %r due to offline mode", project)
                self._offline_logging.add(project)
            return links

        is_retrieval_expired = self.cache_retrieve_times.is_expired(
            project, self.cache_expiry)

        if links is None and not is_retrieval_expired:
            raise self.UpstreamNotFoundError(
                "cached not found for project %s" % project)

        newlinks_future = self.xom.create_future()
        # we need to set this up here, as these access the database and
        # the async loop has no transaction
        _key_from_link = partial(
            key_from_link, self.keyfs, user=self.user.name, index=self.index)
        try:
            self.xom.run_coroutine_threadsafe(
                self._async_fetch_releaselinks(
                    newlinks_future, project, cache_serial, _key_from_link),
                timeout=self.timeout)
        except asyncio.TimeoutError:
            if not self.xom.is_replica():
                # we process the future in the background
                # but only on master, the replica will get the update
                # via the replication thread
                self.xom.create_task(
                    self._update_simplelinks_in_future(newlinks_future, project))
            # to prevent a buildup of updates we mark the project as fresh
            self.cache_retrieve_times.refresh(project)
            if links is not None:
                threadlog.warn(
                    "serving stale links for %r, getting data timed out after %s seconds",
                    project, self.timeout)
                return links
            raise self.UpstreamError(
                f"timeout after {self.timeout} seconds while getting data for {project!r}")
        except (self.UpstreamNotFoundError, self.UpstreamError) as e:
            # if we have and old result, return it. While this will
            # miss the rare event of actual project deletions it allows
            # to stay resilient against server misconfigurations.
            if links is not None:
                threadlog.warn(
                    "serving stale links, because of exception %s",
                    lazy_format_exception(e))
                return links
            raise

        info = newlinks_future.result()

        newlinks = join_links_data(
            info["key_hrefs"], info["requires_python"], info["yanked"])
        if links is not None and set(links) == set(newlinks):
            # no changes
            self.cache_retrieve_times.refresh(project)
            return links

        return self._update_simplelinks(project, info, newlinks)

    def has_project_perstage(self, project):
        if self.is_project_cached(project):
            return True
        try:
            self.get_simplelinks_perstage(project)
        except self.UpstreamNotFoundError:
            return False
        return True

    def list_versions_perstage(self, project):
        try:
            return set(x.version
                       for x in map(SimplelinkMeta, self.get_simplelinks_perstage(project)))
        except self.UpstreamNotFoundError:
            return []

    def get_last_project_change_serial_perstage(self, project, at_serial=None):
        tx = self.keyfs.tx
        if at_serial is None:
            at_serial = tx.at_serial
        info = tx.get_last_serial_and_value_at(
            self.key_projsimplelinks(project),
            at_serial, raise_on_error=False)
        if info is None:
            # never existed
            return -1
        (last_serial, links) = info
        return last_serial

    def get_versiondata_perstage(self, project, version, readonly=True):
        project = normalize_name(project)
        verdata = {}
        for sm in map(SimplelinkMeta, self.get_simplelinks_perstage(project)):
            link_version = sm.version
            if version == link_version:
                if not verdata:
                    verdata['name'] = project
                    verdata['version'] = version
                if sm.require_python is not None:
                    verdata['requires_python'] = sm.require_python
                if sm.yanked:
                    verdata['yanked'] = sm.yanked
                elinks = verdata.setdefault("+elinks", [])
                entrypath = sm._url.path
                elinks.append({"rel": "releasefile", "entrypath": entrypath})
        if readonly:
            return ensure_deeply_readonly(verdata)
        return verdata


class PyPICustomizer(BaseStageCustomizer):
    pass


@hookimpl
def devpiserver_get_stage_customizer_classes():
    # prevent plugins from installing their own under the reserved names
    return [
        ("mirror", PyPICustomizer)]


class ProjectNamesCache:
    """ Helper class for maintaining project names from a mirror. """
    def __init__(self):
        self._timestamp = -1
        self._data = frozenset()

    def exists(self):
        return self._timestamp != -1

    def is_expired(self, expiry_time):
        return (time.time() - self._timestamp) >= expiry_time

    def get(self):
        """ Get a copy of the cached data. """
        return self._data

    def add(self, project):
        """ Add project to cache. """
        self._data = self._data.union({project})

    def discard(self, project):
        """ Remove project from cache. """
        self._data = self._data.difference({project})

    def set(self, data):
        """ Set data and update timestamp. """
        if data is not self._data:
            self._data = frozenset(data)
        self.mark_current()

    def mark_current(self):
        self._timestamp = time.time()


class ProjectUpdateCache:
    """ Helper class to manage when we last updated something project specific. """
    def __init__(self):
        self._project2time = {}

    def is_expired(self, project, expiry_time):
        t = self._project2time.get(project)
        if t is not None:
            return (time.time() - t) >= expiry_time
        return True

    def get_timestamp(self, project):
        return self._project2time.get(project, 0)

    def refresh(self, project):
        self._project2time[project] = time.time()

    def expire(self, project):
        self._project2time.pop(project, None)
