"""Collection of classes related to filtering

"""

# Any commits made to this module between 2015-05-01 and 2017-03-01
# by Gerrit Holl are developed for the EC project “Fidelity and
# Uncertainty in Climate Data Records from Earth Observations (FIDUCEO)”.
# Grant agreement: 638822
# 
# All those contributions are dual-licensed under the MIT license for use
# in typhon, and the GNU General Public License version 3.

import sys
import abc
import dbm
import logging
import tempfile
import pathlib
import shutil
import datetime

import numpy
try:
    import progressbar
except ImportError:
    progressbar = None

from . import dataset

class OutlierFilter(metaclass=abc.ABCMeta):
    
    @abc.abstractmethod
    def filter_outliers(self, C):
        ...

class MEDMAD(OutlierFilter):
    """Outlier filter based on Median Absolute Deviation

    """

    def __init__(self, cutoff):
        self.cutoff = cutoff
    
    def filter_outliers(self, C):
        cutoff = self.cutoff
        if C.ndim == 3:
            med = numpy.ma.median(
                C.reshape(C.shape[0]*C.shape[1], C.shape[2]),
                0)
            mad = numpy.ma.median(
                abs(C - med).reshape(C.shape[0]*C.shape[1], C.shape[2]),
                0)
        elif C.ndim < 3:
            med = numpy.ma.median(C.reshape((-1,)))
            mad = numpy.ma.median(abs(C - med).reshape((-1,)))
        else:
            raise ValueError("Cannot filter outliers on "
                "input with {ndim:d}>3 dimensions".format(ndim=C.ndim))
        fracdev = ((C - med)/mad)
        return abs(fracdev) > cutoff


class OverlapFilter(metaclass=abc.ABCMeta):
    """Implementations to feed into firstline filtering

    This is used in tovs HIRS reading routine.
    """

    @abc.abstractmethod
    def filter_overlap(self, ds, path, header, scanlines):
        ...


class FirstlineDBFilter(OverlapFilter):
    def __init__(self, ds, granules_firstline_file):
        self.ds = ds
        self.granules_firstline_file = granules_firstline_file

    _tmpdir = None
    _firstline_db = None
    def filter_overlap(self, path, header, scanlines):
        """Filter out any scanlines that existed in the previous granule.

        Only works on datasets implementing get_dataname from the header.
        """
        dataname = self.ds.get_dataname(header, robust=True)
        if self._firstline_db is None:
            try:
                self._firstline_db = dbm.open(
                    str(self.granules_firstline_file), "r")
            except dbm.error as e: # presumably a lock
                tmpdir = tempfile.TemporaryDirectory()
                self._tmpdir = tmpdir # should be deleted only when object is
                tmp_gfl = str(pathlib.Path(tmpdir.name,
                    self.granules_firstline_file.name))
                logging.warning("Cannot read GFL DB at {!s}: {!s}, "
                    "presumably in use, copying to {!s}".format(
                        self.granules_firstline_file, e.args, tmp_gfl))
                shutil.copyfile(str(self.granules_firstline_file),
                    tmp_gfl)
                self.granules_firstline_file = tmp_gfl
                self._firstline_db = dbm.open(tmp_gfl)
        firstline = int(self._firstline_db[dataname])
        if firstline > scanlines.shape[0]:
            logging.warning("Full granule {:s} appears contained in previous one. "
                "Refusing to return any lines.".format(dataname))
            return scanlines[0:0]
        return scanlines[scanlines["hrs_scnlin"] > firstline]    

    def update_firstline_db(self, satname=None, start_date=None, end_date=None,
            overwrite=False):
        """Create / update the firstline database

        Create or update the database describing for each granule what the
        first scanline is that doesn't occur in the preceding granule.

        If a granule is entirely contained within the previous one,
        firstline is set to L+1 where L is the number of lines.
        """
        prev_head = prev_line = None
        satname = satname or self.ds.satname
        start_date = start_date or self.ds.start_date
        end_date = end_date or self.ds.end_date
        if end_date > datetime.datetime.now():
            end_date = datetime.datetime.now()
        logging.info("Updating firstline-db {:s} for "
            "{:%Y-%m-%d}--{:%Y-%m-%d}".format(satname, start_date, end_date))
        count_updated = count_all = 0
        with dbm.open(str(self.granules_firstline_file), "c") as gfd:
            try:
                bar = progressbar.ProgressBar(max_value=1,
                    widgets=[progressbar.Bar("=", "[", "]"), " ",
                        progressbar.Percentage(), ' (',
                        progressbar.AdaptiveETA(), " -> ",
                        progressbar.AbsoluteETA(), ') '])
            except AttributeError:
                dobar = False
                bar = None
                logging.info("If you had the "
                    "progressbar2 module, you would have gotten a "
                    "nice progressbar.")
            else:
                dobar = sys.stdout.isatty()
                if dobar:
                    bar.start()
                    bar.update(0)
            for (g_start, gran) in self.ds.find_granules_sorted(start_date, end_date,
                            return_time=True, satname=satname):
                try:
                    (cur_head, cur_line) = self.ds.read(gran,
                        return_header=True, filter_firstline=False,
                        apply_scale_factors=False, calibrate=False,
                        apply_flags=False)
                    cur_time = self.ds._get_time(cur_line)
                except (dataset.InvalidFileError,
                        dataset.InvalidDataError) as exc:
                    logging.error("Could not read {!s}: {!s}".format(gran, exc))
                    continue
                lab = self.ds.get_dataname(cur_head, robust=True)
                if lab in gfd and not overwrite:
                    logging.debug("Already present: {:s}".format(lab))
                elif prev_line is not None:
                    # what if prev_line is None?  We don't want to define any
                    # value for the very first granule we process, as we might
                    # be starting to process in the middle...
                    if cur_time.max() > prev_time.max():
                        # Bugfix 2017-01-16: do not get confused between
                        # the index and the hrs_scnlin field.  So far, I'm using
                        # the index to set firstline but the hrs_scnlin
                        # field to apply it.
                        #first = (cur_time > prev_time[-1]).nonzero()[0][0]
                        # Bugfix 2017-08-21: instead of taking the last
                        # time from the previous granule, take the
                        # maximum; this allows for time sequence errors.
                        # See #139
                        first = cur_line["hrs_scnlin"][cur_time > prev_time.max()].min()
                        logging.debug("{:s}: {:d}".format(lab, first))
                    else:
                        first = cur_line["hrs_scnlin"].max()+1
                        logging.info("{:s}: Fully contained in {:s}!".format(
                            lab, self.ds.get_dataname(prev_head, robust=True)))
                    gfd[lab] = str(first)
                    count_updated += 1
                prev_line = cur_line.copy()
                prev_head = cur_head.copy()
                prev_time = cur_time.copy()
                if dobar:
                    bar.update((g_start-start_date)/(end_date-start_date))
                count_all += 1
            if dobar:
                bar.update(1)
                bar.finish()
            logging.info("Updated {:d}/{:d} granules".format(count_updated, count_all))

class NullLineFilter(OverlapFilter):
    """Do not filter firstlines at all
    """

    def filter_overlap(self, path, header, scanlines):
        return scanlines

class BestLineFilter(OverlapFilter):
    """Choose best between overlaps
    """
    def __init__(self, ds):
        self.ds = ds

    def filter_overlap(self, path, header, scanlines):
        """Choose best lines in overlap between last/current/next granule
        """

        # self.read should be using caching already, so no need to keep
        # track of what I've already read here.  Except that caching only
        # works if the arguments are identical, which they aren't.
        # Consider applying caching on a lower level?  But then I need to
        # store more…
        prevnext = [
            self.ds.read(
                self.ds.find_most_recent_granule_before(
                    scanlines["time"][idx].astype(datetime.datetime) +
                        datetime.timedelta(minutes=Δmin)),
                fields=["hrs_qualind", "hrs_scnlin", "time"],
                return_header=False,
                apply_scale_factors=False, calibrate=False, apply_flags=False,
                filter_firstline=False, apply_filter=False, max_flagged=1.0)
                        for (idx, Δmin) in [(0, -1), (-1, 1)]]

        #
        raise NotImplementedError("Not implemented yet beyond this point")

