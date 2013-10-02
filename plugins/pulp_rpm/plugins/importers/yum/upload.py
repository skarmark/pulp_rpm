# -*- coding: utf-8 -*-
#
# Copyright © 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

import hashlib
import logging
import os
import shutil

import rpm
from xml.etree import cElementTree as ET

from pulp.plugins.model import SyncReport
from pulp.plugins.util import verification
from pulp.server.db.model.criteria import UnitAssociationCriteria

from pulp_rpm.common import models
from pulp_rpm.plugins.importers.yum import utils
from pulp_rpm.plugins.importers.yum.parse import rpm as rpm_parse
from pulp_rpm.plugins.importers.yum.repomd import primary


# this is required because some of the pre-migration XML tags use the "rpm"
# namespace, which causes a parse error if that namespace isn't declared.
FAKE_XML = '<?xml version="1.0" encoding="%(encoding)s"?><faketag xmlns:rpm="http://pulpproject.org">%(xml)s</faketag>'

# Used when extracting metadata from an RPM
RPMTAG_NOSOURCE = 1051
CHECKSUM_READ_BUFFER_SIZE = 65536

# Configuration option specified to not take the steps of linking a newly
# uploaded erratum with RPMs in the destination repository.
CONFIG_SKIP_LINK_ERRATUM = 'skip_erratum_link'

_LOGGER = logging.getLogger(__name__)


# -- exceptions ---------------------------------------------------------------

# These are used by the _handle_* methods for each type so that the main driver
# method can consistently format/word the failure report. These should not be
# raised outside of this module.

class ModelInstantiationError(Exception): pass
class StoreFileError(Exception): pass
class PackageMetadataError(Exception) : pass


def upload(repo, type_id, unit_key, metadata, file_path, conduit, config):
    """
    :param repo: metadata describing the repository
    :type  repo: pulp.plugins.model.Repository

    :param type_id: type of unit being uploaded
    :type  type_id: str

    :param unit_key: identifier for the unit, specified by the user; will likely be None
                     for RPM uploads as the data is extracted server-side
    :type  unit_key: dict or None

    :param metadata: any user-specified metadata for the unit; will likely be None
                     for RPM uploads as the data is extracted server-side
    :type  metadata: dict or None

    :param file_path: path on the Pulp server's filesystem to the temporary
           location of the uploaded file; may be None in the event that a
           unit is comprised entirely of metadata and has no bits associated
    :type  file_path: str

    :param conduit: provides access to relevant Pulp functionality
    :type  conduit: pulp.plugins.conduits.upload.UploadConduit

    :param config: plugin configuration for the repository
    :type  config: pulp.plugins.config.PluginCallConfiguration

    :return: report of the details of the sync
    :rtype:  pulp.plugins.model.SyncReport
    """

    # Dispatch to process the upload by type
    handlers = {
        models.RPM.TYPE : _handle_package,
        models.SRPM.TYPE : _handle_package,
        models.PackageGroup.TYPE : _handle_group_category,
        models.PackageCategory.TYPE : _handle_group_category,
        models.Errata.TYPE : _handle_erratum,
    }

    if type_id not in handlers:
        return _fail_report('%s is not a supported type for upload' % type_id)

    try:
        handlers[type_id](type_id, unit_key, metadata, file_path, conduit, config)
    except ModelInstantiationError:
        msg = 'metadata for the uploaded file was invalid'
        _LOGGER.exception(msg)
        return _fail_report(msg)
    except StoreFileError:
        msg = 'file could not be deployed into Pulp\'s storage'
        _LOGGER.exception(msg)
        return _fail_report(msg)
    except PackageMetadataError:
        msg = 'metadata for the given package could not be extracted'
        _LOGGER.exception(msg)
        return _fail_report(msg)
    except:
        msg = 'unexpected error occurred importing uploaded file'
        _LOGGER.exception(msg)
        return _fail_report(msg)

    # TODO: add more info to this report?
    report = SyncReport(True, 1, 0, 0, '', {})
    return report

# -- erratum upload -----------------------------------------------------------

def _handle_erratum(type_id, unit_key, metadata, file_path, conduit, config):
    """
    Handles the upload for an erratum. There is no file uploaded so the only
    steps are to save the metadata and optionally link the erratum to RPMs
    in the repository.
    """

    # Validate the user specified data by instantiating the model
    try:
        model_class = models.TYPE_MAP[type_id]
        model = model_class(metadata=metadata, **unit_key)
    except TypeError:
        raise ModelInstantiationError()

    unit = conduit.init_unit(model.TYPE, model.unit_key, model.metadata, None)

    if not config.get_boolean(CONFIG_SKIP_LINK_ERRATUM):
        _link_errata_to_rpms(conduit, model, unit)

    conduit.save_unit(unit)


def _link_errata_to_rpms(conduit, errata_model, errata_unit):
    """
    :param conduit: provides access to relevant Pulp functionality
    :type  conduit: pulp.plugins.conduits.unit_add.UnitAddConduit
    :param errata_model:    model object representing an errata
    :type  errata_model:    pulp_rpm.common.models.Errata
    :param errata_unit:     unit object representing an errata
    :type  errata_unit:     pulp.plugins.model.Unit
    """
    fields = list(models.RPM.UNIT_KEY_NAMES)
    fields.append('_storage_path')
    filters = {'$or': errata_model.rpm_search_dicts}
    for model_type in (models.RPM.TYPE, models.SRPM.TYPE):
        criteria = UnitAssociationCriteria(type_ids=[model_type], unit_fields=fields,
                                           unit_filters=filters)
        for unit in conduit.get_units(criteria):
            conduit.link_unit(errata_unit, unit, bidirectional=True)

# -- yum metadata file upload -------------------------------------------------

def _handle_yum_metadata_file(type_id, unit_key, metadata, file_path, conduit, config):
    """
    Handles the upload for a yum repository metadata file.
    """

    # Validate the user specified data by instantiating the model
    try:
        model_class = models.TYPE_MAP[type_id]
        model = model_class(metadata=metadata, **unit_key)
    except TypeError:
        raise ModelInstantiationError()

    # TODO: Determine unique location for the file
    relative_path = None

    # Move the file to its final storage location in Pulp
    try:
        unit = conduit.init_unit(model.TYPE, model.unit_key, model.metadata, relative_path)
        shutil.move(file_path, unit.storage_path)
        conduit.save_unit(unit)
    except IOError:
        raise StoreFileError()

# -- package group/category upload --------------------------------------------

def _handle_group_category(type_id, unit_key, metadata, file_path, conduit, config):
    """
    Handles the creation of a package group or category. There is no file uploaded,
    so the process is simply to create the unit in Pulp.
    """

    # Validate the user specified data by instantiating the model
    try:
        model_class = models.TYPE_MAP[type_id]
        model = model_class(metadata=metadata, **unit_key)
    except TypeError:
        raise ModelInstantiationError()

    unit = conduit.init_unit(model.TYPE, model.unit_key, model.metadata, None)
    conduit.save_unit(unit)

# -- package upload -----------------------------------------------------------

def _handle_package(type_id, unit_key, metadata, file_path, conduit, config):
    """
    Handles the upload for an RPM or SRPM. For these types, the unit_key
    and metadata will only contain additions the user wishes to add. The
    typical use case is that the file is uploaded and all of the necessary
    data, both unit key and metadata, are extracted in this method.
    """

    # Extract the RPM key and metadata
    try:
        new_unit_key, new_unit_metadata = _generate_rpm_data(file_path)
    except:
        _LOGGER.exception('Error extracting RPM metadata for [%s]' % file_path)
        raise PackageMetadataError()

    # Update the RPM-extracted data with anything additional the user specified.
    # Allow the user-specified values to override the extracted ones.
    new_unit_key.update(unit_key or {})
    new_unit_metadata.update(metadata or {})

    # Validate the user specified data by instantiating the model
    try:
        model_class = models.TYPE_MAP[type_id]
        model = model_class(metadata=new_unit_metadata, **new_unit_key)
    except TypeError:
        raise ModelInstantiationError()

    # Move the file to its final storage location in Pulp
    try:
        unit = conduit.init_unit(model.TYPE, model.unit_key,
                                 model.metadata, model.relative_path)
        shutil.move(file_path, unit.storage_path)
    except IOError:
        raise StoreFileError()

    # Extract the repodata snippets
    unit.metadata['repodata'] = rpm_parse.get_package_xml(unit.storage_path)
    _update_provides_requires(unit)

    # Save the unit in Pulp
    conduit.save_unit(unit)


def _update_provides_requires(unit):
    """
    Determines the provides and requires fields based on the RPM's XML snippet and updates
    the model instance.

    :param unit: the unit being added to Pulp; the metadata attribute must already have
                 a key called 'repodata'
    :type  unit: pulp.plugins.model.Unit
    """

    try:
        # make a guess at the encoding
        codec = 'UTF-8'
        unit.metadata['repodata']['primary'].encode(codec)
    except UnicodeEncodeError:
        # best second guess we have, and it will never fail due to the nature
        # of the encoding.
        codec = 'ISO-8859-1'
        unit.metadata['repodata']['primary'].encode(codec)
    fake_xml = FAKE_XML % {'encoding': codec, 'xml': unit.metadata['repodata']['primary']}
    fake_element = ET.fromstring(fake_xml.encode(codec))
    utils.strip_ns(fake_element)
    primary_element = fake_element.find('package')
    format_element = primary_element.find('format')
    provides_element = format_element.find('provides')
    requires_element = format_element.find('requires')
    unit.metadata['provides'] = map(primary._process_rpm_entry_element,
                                     provides_element.findall('entry')) if provides_element else []
    unit.metadata['requires'] = map(primary._process_rpm_entry_element,
                                     requires_element.findall('entry')) if requires_element else []


def _generate_rpm_data(rpm_filename):
    """
    For the given RPM, analyzes its metadata to generate the appropriate unit
    key and metadata fields, returning both to the caller.

    :param rpm_filename: full path to the RPM to analyze
    :type  rpm_filename: str

    :return: tuple of unit key and unit metadata for the RPM
    :rtype:  tuple
    """

    # Expected metadata fields:
    # "vendor", "description", "buildhost", "license", "vendor", "requires", "provides", "relativepath", "filename"
    #
    # Expected unit key fields:
    # "name", "epoch", "version", "release", "arch", "checksumtype", "checksum"

    unit_key = dict()
    metadata = dict()

    # Read the RPM header attributes for use later
    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
    fd = os.open(rpm_filename, os.O_RDONLY)
    try:
        headers = ts.hdrFromFdno(fd)
        os.close(fd)
    except rpm.error:
        # Raised if the headers cannot be read
        os.close(fd)
        raise

    # -- Unit Key -----------------------

    # Checksum
    unit_key['checksumtype'] = verification.TYPE_SHA256
    unit_key['checksum'] = _calculate_checksum(unit_key['checksumtype'], rpm_filename)

    # Name, Version, Release, Epoch
    for k in ['name', 'version', 'release', 'epoch']:
        unit_key[k] = headers[k]

    #   Epoch munging
    if unit_key['epoch'] is None:
        unit_key['epoch'] = str(0)
    else:
        unit_key['epoch'] = str(unit_key['epoch'])

    # Arch
    if headers['sourcepackage']:
        if RPMTAG_NOSOURCE in headers.keys():
            unit_key['arch'] = 'nosrc'
        else:
            unit_key['arch'] = 'src'
    else:
        unit_key['arch'] = headers['arch']

    # -- Unit Metadata ------------------

    metadata['relativepath'] = os.path.basename(rpm_filename)
    metadata['filename'] = os.path.basename(rpm_filename)

    # This format is, and has always been, incorrect. As of the new yum importer, the
    # plugin will generate these from the XML snippet because the API into RPM headers
    # is atrocious. This is the end game for this functionality anyway, moving all of
    # that metadata derivation into the plugin, so this is just a first step.
    # I'm leaving these in and commented to show how not to do it.
    # metadata['requires'] = [(r,) for r in headers['requires']]
    # metadata['provides'] = [(p,) for p in headers['provides']]

    metadata['buildhost'] = headers['buildhost']
    metadata['license'] = headers['license']
    metadata['vendor'] = headers['vendor']
    metadata['description'] = headers['description']

    return unit_key, metadata


def _calculate_checksum(checksum_type, filename):
    m = hashlib.new(checksum_type)
    f = open(filename, 'r')
    while 1:
        file_buffer = f.read(CHECKSUM_READ_BUFFER_SIZE)
        if not file_buffer:
            break
        m.update(file_buffer)
    f.close()
    return m.hexdigest()

# -- generic utilities ---------------------------------------------------------

def _fail_report(message):
    # this is the format returned by the original importer. I'm not sure if
    # anything is actually parsing it
    details = {'errors': [message]}
    return SyncReport(False, 0, 0, 0, '', details)
