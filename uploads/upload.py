from __future__ import print_function, unicode_literals, division, absolute_import
import sys
import logging
from io import StringIO

try:
    from bs4 import BeautifulSoup
except ImportError:
    logging.critical('You must install "beautifulsoup4" for this script to work.  Try "pip install beautifulsoup4".')
    sys.exit(1)

try:
    import requests
except ImportError:
    logging.critical('You must install "requests" for this script to work.  Try "pip install requests".')
    sys.exit(1)

import config
import files
import trackers
import metadata
from .utils import normalize_title, strings_match, years_match

if sys.version_info[0] >= 3:
    raw_input = input


class Upload(object):
    """
    Represents the preparation and execution of an upload to a Tracker.
    """

    def __init__(
            self,
            path,
            tracker,
            imdb_link=None,
            take_screenshots=True,
            num_screenshots=0,
            delete_unwanted_files=False
    ):

        assert issubclass(tracker, trackers.BaseTracker)

        try:
            self.tracker = tracker()
            self.release = files.Release(path)
        except trackers.TrackerError as e:
            raise UploadInterruptedError(e)
        except files.ReleaseError as e:
            raise UploadInterruptedError(e)

        self.delete_unwanted_files = delete_unwanted_files
        self.take_screens = take_screenshots
        self.num_screens = num_screenshots
        self.use_nfo = True
        self.metadata_is_verified = False
        self.technical_is_verified = False

        self.title = None
        self.year = None
        self.source = self.release.source
        self.resolution = None
        self.codec = None
        self.container = None
        self.nfo = None
        self.tmdb = None
        self.film_description = None
        self.mediainfo = None
        self.screenshots = None
        self.torrent = None
        self.torrent_description = None

        if imdb_link is None:
            self.imdb = None
        else:
            self.imdb = metadata.IMDb(imdb_link)

    def __repr__(self):
        text = 'Upload to {tracker} in progress: "{release}"'
        return text.format(tracker=self.tracker, release=self.release)

    def start(self, dry_run=False):

        # Find the NFO file, if it exists
        self.nfo = self.release.get_nfo()

        # Fetch metadata from various sources, and cross-check them
        self.get_metadata()
        self.metadata_is_verified = self.verify_metadata()

        # Check the tracker for an existing torrent group
        try:
            self.release.torrent_group_id = self.tracker.get_torrent_group(self.imdb)
        except trackers.TrackerError as e:
            raise UploadInterruptedError(e)

        # Check the tracker for dupes
        if self.release.torrent_group_id is not None:
            possible_dupes = self.tracker.dupe_check(self.release)
            for dupe in possible_dupes:
                msg = 'Possible dupe : {release_name} : {url}'
                logging.warning(msg.format(release_name=dupe['name'], url=dupe['url']))
                print('\nDo you want to continue? (Y/N)')
                answer = raw_input()
                print()
                if answer.lower() != 'y':
                    raise UploadInterruptedError('User aborted upload')

        try:

            # Extract RAR archives, get rid of unwanted files
            self.release.clean_up(
                delete_unwanted_files=self.delete_unwanted_files,
                extension_whitelist=self.tracker.FILE_EXTENSION_WHITELIST
            )

            # Find the video file so we can run mediainfo on it
            self.release.find_video_file()

        except files.ReleaseError as e:

            raise UploadInterruptedError(e)

        # Get mediainfo and cross-check technical details with the release name
        self.get_mediainfo()
        self.technical_is_verified = self.verify_technical()

        # Take screenshots
        if self.take_screens and self.num_screens > 0:
            try:
                self.screenshots = files.Screenshots(self.release.video_file)
                self.screenshots.take(self.num_screens)
                self.screenshots.upload()
            except files.ScreenshotsError as e:
                raise UploadInterruptedError(e)
        else:
            logging.info('Skipping screenshots')

        # Make the .torrent file
        try:
            self.torrent = files.Torrent(self.release, self.tracker)
        except files.TorrentError as e:
            raise UploadInterruptedError(e)

        # Pull the trigger
        try:
            self.tracker.take_upload(self, dry_run=dry_run)
        except trackers.TrackerError as e:
            raise UploadInterruptedError(e)

        # Move the .torrent file to the watch folder
        self.torrent.move_to(config.WATCH_DIR)

    def get_imdb_id_from_nfo(self):

        # Try to get IMDb ID from the NFO
        if self.use_nfo and self.nfo is not None:

            imdb_id = None
            for line in StringIO(self.nfo.text):
                if 'imdb.com/title/tt' in line:
                    imdb_id = metadata.IMDb.get_valid_id(line)

            if imdb_id is not None:
                msg = 'Found IMDb ID in NFO: {id}'
                logging.debug(msg.format(id=imdb_id))
                return imdb_id
            else:
                logging.warning('Could not find IMDb ID in NFO!')
                return None

    def get_imdb_id(self):

        if isinstance(self.imdb, metadata.IMDb):
            return

        imdb_id = self.get_imdb_id_from_nfo()

        if imdb_id is not None:
            self.imdb = metadata.IMDb(imdb_id)
            return

        logging.debug('Searching IMDb, TMDb, and Google.')

        # Search using TMDb API
        try:
            tmdb = metadata.TMDB(title=self.release.title, year=self.release.year)
            tmdb.get_metadata()
            tmdb_search = metadata.IMDb(tmdb.imdb_id)
        except (metadata.TMDBError, metadata.IMDbError):
            logging.debug('TMDb search failed to produce an IMDb link.')
            tmdb_search = None

        if self.release.year is not None:
            query = '{title} ({year})'.format(title=self.release.title, year=self.release.year)
        else:
            query = self.release.title

        # Search using IMDb web site
        params = {

            # Title, and year if we have it
            'q': query,

            # Search by title
            's': 'tt',

            # Search for films
            'ttype': 'ft'

        }
        response = requests.get('http://www.imdb.com/find', params=params)
        response.raise_for_status()
        dom = BeautifulSoup(response.text, "html.parser")
        first_result = dom.find('tr', attrs={'class': 'findResult'})
        if first_result is not None:
            imdb_link = first_result.a.get('href').strip()
            try:
                imdb_search = metadata.IMDb(imdb_link)
            except metadata.IMDbError:
                logging.debug('IMDb search failed to produce an IMDb link.')
                imdb_search = None
        else:
            imdb_search = None

        # Search using Google
        query += ' IMDb'
        params = {

            # Title, and year if we have it
            'q': query,

            # We're feeling lucky.  ;)
            'btnI': ''

        }
        response = requests.get('http://www.google.com/search', params=params)
        response.raise_for_status()

        try:
            google_search = metadata.IMDb(response.url)
        except metadata.IMDbError:
            logging.debug('Google search failed to produce an IMDb link.')
            google_search = None

        if (imdb_search, google_search, tmdb_search).count(None) > 1:
            raise UploadInterruptedError('More than one search failed.')

        if imdb_search == google_search or imdb_search == tmdb_search:
            self.imdb = imdb_search
        elif google_search == tmdb_search:
            self.imdb = tmdb_search
        else:
            raise UploadInterruptedError('Google, IMDb, and TMDb searches all produced different IMDb IDs.')

    def get_metadata(self):

        self.get_imdb_id()

        # Get IMDb and TMDB data
        assert self.imdb is not None
        try:
            self.imdb.get_metadata()
            self.tmdb = metadata.TMDB(imdb_id=self.imdb.id)
            self.tmdb.get_metadata()
        except (metadata.IMDbError, metadata.TMDBError) as e:
            raise UploadInterruptedError(e)

        # Make sure we have a title
        titles = [
            self.tmdb.title,
            self.imdb.title,
        ]
        self.title = next(t for t in titles if t is not None)

        # Make sure we have a year
        years = [
            self.imdb.year,
            self.tmdb.year,
        ]
        self.year = next(y for y in years if y is not None)

        # Make sure we have a film description
        descriptions = [
            self.tmdb.description,
            self.imdb.description,
            'Could not find film description on TMDb or IMDb.'
        ]
        self.film_description = next(d for d in descriptions if d is not None and d.strip())

    def get_mediainfo(self):

        if self.release.path is not None:
            # Get mediainfo, and parse it for codec, container, and resolution
            try:
                self.mediainfo = metadata.Mediainfo(
                    path=self.release.video_file,
                    base_path=self.release.base_path
                )
                self.mediainfo.parse()
                self.mediainfo.get_info()
            except metadata.MediainfoError as e:
                raise UploadInterruptedError(e)

    def verify_metadata(self):

        if self.use_nfo is False:
            logging.info('Attempting to fetch metadata without using IMDb ID from NFO.')

        # Normalize titles for fuzzy comparison
        assert self.release.title
        assert self.imdb.title
        assert self.tmdb.title
        assert self.tmdb.original_title
        release_title = normalize_title(self.release.title)
        imdb_title = normalize_title(self.imdb.title)
        imdb_aka_list = [normalize_title(aka_title) for aka_title in self.imdb.aka_list]
        tmdb_title = normalize_title(self.tmdb.title)
        tmdb_original_title = normalize_title(self.tmdb.original_title)

        # Verify release title against IMDb/TMDb
        if strings_match(imdb_title, release_title):
            logging.debug('Release title and IMDb title match.')
        elif strings_match(release_title, imdb_aka_list):
            logging.debug('Release title matches an AKA title on IMDb.')
        elif strings_match(tmdb_title, release_title) and strings_match(imdb_title, tmdb_original_title):
            logging.debug('Release title matches TMDb title, and IMDb title matches TMDb original_title.')
        else:
            msg = 'Release title "{r}" does not match IMDb title "{i}".'
            logging.warning(msg.format(r=self.release.title, i=self.imdb.title))
            prompt = '\nIs this the correct IMDb link for the release? (Y/N)  {link}'
            print(prompt.format(link=self.imdb.link))
            answer = raw_input()
            print()
            if answer.lower() == 'y':
                logging.debug('Assuming IMDb title is correct')
            elif self.use_nfo is True and self.nfo is not None:
                # This could mean the wrong IMDb ID was provided in the NFO file
                self.use_nfo = False
                self.get_metadata()
                return self.verify_metadata()
            else:
                raise UploadInterruptedError('User aborted upload')

        # Check to make sure the release year and the year from IMDb match
        if self.release.year is None:
            logging.debug('Year not parsed from release name, so we cannot match it with IMDb year.')
        elif years_match(self.release.year, self.imdb.year):
            logging.debug('Release year and IMDb year match.')
        else:
            # This could mean the wrong IMDb ID was provided in the NFO file
            if self.use_nfo is True and self.nfo is not None:
                self.use_nfo = False
                self.get_metadata()
                return self.verify_metadata()
            else:
                msg = 'Release year "{r}" does not match IMDb year "{i}".'
                raise UploadInterruptedError(msg.format(r=self.release.year, i=self.imdb.year))

        # All checks passed
        return True

    def verify_technical(self):

        # Check to make sure the release resolution and the width/height match
        w = self.mediainfo.width
        h = self.mediainfo.height
        resolution = self.release.resolution
        if (
            (0 < h <= 576 and 0 < w <= 1024 and resolution not in ('Standard Def', '480p', '576p'))
            or (576 < h <= 720 and 1024 < w <= 1280 and resolution != '720p')
            or (720 < h <= 1080 and 1280 < w <= 1920 and resolution != '1080p')
            or (h > 1080 or w > 1920)
        ):
            msg = 'Release resolution "{resolution}" does not match width and height ({w} x {h}).'
            raise UploadInterruptedError(msg.format(resolution=resolution, w=w, h=h))
        else:
            logging.debug('Resolution matches width and height.')
            self.resolution = self.release.resolution

        # Check to make sure the codec from release name and mediainfo match
        if self.release.codec != self.mediainfo.codec:
            release_codec = self.tracker.CODEC_STRING[self.release.codec]
            mediainfo_codec = self.tracker.CODEC_STRING[self.mediainfo.codec]
            msg = 'Release codec "{r}" does not match mediainfo codec "{m}".'
            raise UploadInterruptedError(msg.format(r=release_codec, m=mediainfo_codec))
        else:
            logging.debug('Release name matches codec found in mediainfo.')
            self.codec = self.mediainfo.codec

        # Check to make sure the container from the file name and mediainfo match
        if self.release.container != self.mediainfo.container:
            msg = 'Release container "{r}" does not match mediainfo container "{m}".'
            raise UploadInterruptedError(msg.format(r=self.release.container, m=self.mediainfo.container))
        else:
            logging.debug('File name matches container found in mediainfo.')
            self.container = self.mediainfo.container

        # All checks passed
        return True


class UploadInterruptedError(Exception):
    pass
