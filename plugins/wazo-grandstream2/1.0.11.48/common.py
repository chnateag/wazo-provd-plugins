# -*- coding: utf-8 -*-

# Copyright 2010-2016 The Wazo Authors  (see the AUTHORS file)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

from curses import raw
import errno
import logging
import re
import os.path
from operator import itemgetter
from provd import tzinform
from provd import synchronize
from provd.devices.config import RawConfigError
from provd.plugins import StandardPlugin, FetchfwPluginHelper, \
    TemplatePluginHelper
from provd.devices.pgasso import IMPROBABLE_SUPPORT, COMPLETE_SUPPORT, \
    FULL_SUPPORT, BasePgAssociator, UNKNOWN_SUPPORT
from provd.servers.http import HTTPNoListingFileService
from provd.util import norm_mac, format_mac
from twisted.internet import defer, threads

logger = logging.getLogger('plugin.wazo-grandstream2')

TZ_NAME = {'Europe/Paris': 'CET-1CEST-2,M3.5.0/02:00:00,M10.5.0/03:00:00'}
LOCALE = {
    u'de_DE': 'de',
    u'es_ES': 'es',
    u'fr_FR': 'fr',
    u'fr_CA': 'fr',
    u'it_IT': 'it',
    u'nl_NL': 'nl',
    u'en_US': 'en'
}

FUNCKEY_TYPES = {
    u'speeddial': 0,
    u'blf': 1,
    u'park': 9,
    u'default': 31,
    u'disabled': -1,
}

class BaseGrandstreamHTTPDeviceInfoExtractor(object):
    # Grandstream Model HW GXP1405 SW 1.0.4.23 DevId 000b8240d55c
    # Grandstream Model HW GXP2200 V2.2A SW 1.0.1.33 DevId 000b82462d97
    # Grandstream Model HW GXV3240 V1.6B SW 1.0.1.27 DevId 000b82632815
    # Grandstream GXP2000 (gxp2000e.bin:1.2.5.3/boot55e.bin:1.1.6.9) DevId 000b822726c8

    _UA_REGEX_LIST = [
        re.compile(
            r'^Grandstream Model HW (\w+)(?: V[^ ]+)? SW ([^ ]+) DevId ([^ ]+)'),
        re.compile(r'^Grandstream (GXP2000) .*:([^ ]+)\) DevId ([^ ]+)'),
    ]

    def extract(self, request, request_type):
        return defer.succeed(self._do_extract(request))

    def _do_extract(self, request):
        ua = request.getHeader('User-Agent')
        if ua:
            return self._extract_from_ua(ua)
        return None

    def _extract_from_ua(self, ua):
        for UA_REGEX in self._UA_REGEX_LIST:
            m = UA_REGEX.match(ua)
            if m:
                raw_model, raw_version, raw_mac = m.groups()
                try:
                    mac = norm_mac(raw_mac.decode('ascii'))
                except ValueError as e:
                    logger.warning(
                        'Could not normalize MAC address "%s": %s', raw_mac, e)
                else:
                    return {u'vendor': u'Grandstream',
                            u'model': raw_model.decode('ascii'),
                            u'version': raw_version.decode('ascii'),
                            u'mac': mac}
        return None


class BaseGrandstreamPgAssociator(BasePgAssociator):
    def __init__(self, models, version):
        BasePgAssociator.__init__(self)
        logger.info(models)
        self._models = models
        self._version = version

    def _do_associate(self, vendor, model, version):
        if vendor == u'Grandstream':
            if model in self._models:
                if version.startswith(self._version):
                    return FULL_SUPPORT
                return COMPLETE_SUPPORT
            return UNKNOWN_SUPPORT
        return IMPROBABLE_SUPPORT


class BaseGrandstreamPlugin(StandardPlugin):
    _ENCODING = 'UTF-8'

    # VPKs are the virtual phone keys on the main display
    # MPKs are the physical programmable keys on some models
    MODEL_FKEYS = {
        u'GXP2130': {
            u'vpk': 3,
            u'mpk': 8,
        },
        u'GXP2140': {
            u'vpk': 4,
            u'mpk': 160,
        },
        u'GXP2160': {
            u'vpk': 5,
            u'mpk': 24,
        },
        u'GXP2170': {
            u'vpk': 6,
            u'mpk': 160,
        },
        u'GXP2135': {
            u'vpk': 6,
            u'mpk': 24,
        }
    }

    DTMF_MODES = {
        # mode: (in audio, in RTP, in SIP)
        u'RTP-in-band': ('Yes', 'Yes', 'No'),
        u'RTP-out-of-band': ('No', 'Yes', 'No'),
        u'SIP-INFO': ('No', 'No', 'Yes'),
    }

    SIP_TRANSPORTS = {
        u'udp': u'UDP',
        u'tcp': u'TCP',
        u'tls': u'TlsOrTcp',
    }

    # This function init the wazo plugin
    def __init__(self, app, plugin_dir, gen_cfg, spec_cfg):
        StandardPlugin.__init__(self, app, plugin_dir, gen_cfg, spec_cfg)
        # update to use the non-standard tftpboot directory
        self._base_tftpboot_dir = self._tftpboot_dir
        self._tftpboot_dir = os.path.join(self._tftpboot_dir, 'Grandstream')

        self._tpl_helper = TemplatePluginHelper(plugin_dir)

        downloaders = FetchfwPluginHelper.new_downloaders(
            gen_cfg.get('proxies'))
        fetchfw_helper = FetchfwPluginHelper(plugin_dir, downloaders)
        # update to use the non-standard tftpboot directory
        fetchfw_helper.root_dir = self._tftpboot_dir

        self.services = fetchfw_helper.services()
        self.http_service = HTTPNoListingFileService(self._base_tftpboot_dir)

    http_dev_info_extractor = BaseGrandstreamHTTPDeviceInfoExtractor()

    # Return the path of the config file for the given device
    def _dev_specific_filename(self, device):
        # Return the device specific filename (not pathname) of device
        fmted_mac = format_mac(device[u'mac'], separator='', uppercase=False)
        return 'cfg' + fmted_mac + '.xml'

    # Return the mac of device formatted to be used in a directory name
    def _dev_fmted_mac(self, device):
        # Return the device mac adress formated for xml <mac></mac> tag
        fmted_mac = format_mac(device[u'mac'], separator='', uppercase=False)
        return fmted_mac

    # Check if the device is supportedss
    def _check_config(self, raw_config):
        if u'http_port' not in raw_config:
            raise RawConfigError('only support configuration via HTTP')

    # Check is the device properly show it's mac address
    def _check_device(self, device):
        if u'mac' not in device:
            raise Exception('MAC address needed for device configuration')

    # Create the configuration for the device
    def configure(self, device, raw_config):
        self._check_config(raw_config)
        self._check_device(device)
        self._check_lines_password(raw_config)
        self._add_sip_transport(raw_config)
        self._add_timezone(raw_config)
        self._add_locale(raw_config)
        self._add_dtmf_mode(raw_config)
        self._add_fkeys(raw_config)
        self._add_mpk(raw_config, device.get(u'model'))
        self._add_v2_fkeys(raw_config, device.get(u'model'))
        self._add_dns(raw_config)
        filename = self._dev_specific_filename(device)
        tpl = self._tpl_helper.get_dev_template(filename, device)
        logger.info(tpl)
        path = os.path.join(self._tftpboot_dir, filename)
        logger.info(raw_config)
        self._tpl_helper.dump(tpl, raw_config, path, self._ENCODING)

    # Reset the device to an auto prov state
    def deconfigure(self, device):
        self._remove_configuration_file(device)

    # Remove the configuration file from the fs
    def _remove_configuration_file(self, device):
        path = os.path.join(self._tftpboot_dir,
                            self._dev_specific_filename(device))
        try:
            os.remove(path)
        except OSError as e:
            logger.info('error while removing configuration file: %s', e)

    if hasattr(synchronize, 'standard_sip_synchronize'):
        def synchronize(self, device, raw_config):
            return synchronize.standard_sip_synchronize(device)

    else:
        # backward compatibility with older xivo-provd server
        def synchronize(self, device, raw_config):
            try:
                ip = device[u'ip'].encode('ascii')
            except KeyError:
                return defer.fail(Exception('IP address needed for device synchronization'))
            else:
                sync_service = synchronize.get_sync_service()
                if sync_service is None or sync_service.TYPE != 'AsteriskAMI':
                    return defer.fail(Exception('Incompatible sync service: %s' % sync_service))
                else:
                    return threads.deferToThread(sync_service.sip_notify, ip, 'check-sync')

    # ???
    def get_remote_state_trigger_filename(self, device):
        if u'mac' not in device:
            return None

        return self._dev_specific_filename(device)

    def get_remote_state_trigger_filename(self, device):
        if u'mac' in device:
            return self._dev_specific_filename(device)

    # Allow to use an empty password on an auto prov device
    def _check_lines_password(self, raw_config):
        for line in raw_config[u'sip_lines'].itervalues():
            if line[u'password'] == u'autoprov':
                line[u'password'] = u''

    # Setup the timezone using the one choosen in the wazo settings
    def _add_timezone(self, raw_config):
        if u'timezone' in raw_config and raw_config[u'timezone'] in TZ_NAME:
            raw_config[u'XX_timezone'] = TZ_NAME[raw_config[u'timezone']]
        else:
            raw_config['timezone'] = TZ_NAME['Europe/Paris']

    # Change the locale according to the wazo locale
    def _add_locale(self, raw_config):
        locale = raw_config.get(u'locale')
        if locale in LOCALE:
            raw_config[u'XX_locale'] = LOCALE[locale]


    def _add_fkeys(self, raw_config):
        lines = []
        for funckey_no, funckey_dict in raw_config[u'funckeys'].iteritems():
            '''
            i_funckey_no = int(funckey_no)
            funckey_type = funckey_dict[u'type']
            if funckey_type not in FUNCKEY_TYPES:
                logger.info('Unsupported funckey type: %s', funckey_type)
                continue
            type_code = u'P32%s' % (i_funckey_no + 2)
            lines.append((type_code, FUNCKEY_TYPES[funckey_type]))
            line_code = self._format_code(3 * i_funckey_no - 2)
            lines.append((line_code, int(funckey_dict[u'line']) - 1))
            if u'label' in funckey_dict:
                label_code = self._format_code(3 * i_funckey_no - 1)
                lines.append((label_code, funckey_dict[u'label']))
            value_code = self._format_code(3 * i_funckey_no)
            lines.append((value_code, funckey_dict[u'value']))
            '''
        raw_config[u'XX_fkeys'] = lines

    # Manage the MPK (right keys) on the device
    def _add_mpk(self, raw_config, model):
        lines = []
        logger.info(model)

        for funckey_no, funckey_dict in raw_config[u'funckeys'].iteritems():
            i_funckey_no = int(funckey_no)  # starts at 1

            # Step for the keys
            if model == u'GXP2160' or model == u'GXP2130':
                
                if(model == u'GXP2160' and i_funckey_no > 24):
                    break
                
                if(model == u'GXP2130' and i_funckey_no > 8):
                    break

                if i_funckey_no < 8:
                    step = (i_funckey_no - 1) 

                    key_mode_id = 323 + step
                    account_id = 301 + step
                    name_id = 302 + step
                    value_id = 303 + step

                elif i_funckey_no >= 8 and i_funckey_no <= 18: 
                    step = (i_funckey_no - 8) * 4

                    key_mode_id = 353 + step
                    account_id = 354 + step
                    name_id = 355 + step
                    value_id = 356 + step

                elif i_funckey_no >= 19:
                    step = (i_funckey_no - 19) * 4

                    key_mode_id = 1440 + step
                    account_id = 1441 + step
                    name_id = 1442 + step 
                    value_id = 1443 + step
            elif model == u'GXP2170' or model == u'GXP2140':
                step = (5 * i_funckey_no) - 5

                key_mode_id = 23000 + step
                account_id = 23001 + step
                name_id = 23002 + step
                value_id = 23003 + step
                
            elif model == u'GXP2135':
                continue
            else:
                logger.error('Unable to generate BLF for model "%s"', model)
         
            funckey_type = funckey_dict[u'type']
            if funckey_type not in FUNCKEY_TYPES:
                logger.info('Unsupported funckey type: %s', funckey_type)
                continue

            # Key mode (BLF, etc)
            key_mode_code = u'P{}'.format(key_mode_id)

            lines.append((key_mode_code, FUNCKEY_TYPES[funckey_type]))

            #Account used 
            line_code = u'P{}'.format(account_id)

            lines.append((line_code, int(funckey_dict[u'line']) - 1))

            # Label or display value
            if u'label' in funckey_dict:
                label_code = u'P{}'.format(name_id)
                lines.append((label_code, funckey_dict[u'label']))
           
            #Value for the key
            value_code = u'P{}'.format(value_id)
            lines.append((value_code, funckey_dict[u'value']))
       
        raw_config[u'XX_mpk'] = lines

    def _add_v2_fkeys(self, raw_config, model):
        lines = []
        '''
        model_fkeys = self.MODEL_FKEYS.get(model)
        if not model_fkeys:
            logger.info('Model Unknown model: "%s"', model)
            return
        for funckey_no in range(1, model_fkeys[u'vpk'] + 1):
            funckey = raw_config[u'funckeys'].get(str(funckey_no), {})
            funckey_type = funckey.get(u'type', 'disabled')
            if funckey_type not in FUNCKEY_TYPES:
                logger.info('Unsupported funckey type: %s', funckey_type)
                continue
            if str(funckey_no) in raw_config[u'sip_lines']:
                logger.info(
                    'Function key %s would conflict with an existing line', funckey_no
                )
                continue
            lines.append(
                (
                    funckey_no,
                    {
                        u'section': u'vpk',
                        u'type': FUNCKEY_TYPES[funckey_type],
                        u'label': funckey.get(u'label') or u'',
                        u'value': funckey.get(u'value') or u'',
                    },
                )
            )
        for funckey_no in range(1, model_fkeys[u'mpk'] + 1):
            funckey = raw_config[u'funckeys'].get(
                str(funckey_no + model_fkeys[u'vpk']), {}
            )
            funckey_type = funckey.get(u'type', 'disabled')
            if funckey_type not in FUNCKEY_TYPES:
                logger.info('Unsupported funckey type: %s', funckey_type)
            lines.append(
                (
                    funckey_no,
                    {
                        u'section': u'mpk',
                        u'type': FUNCKEY_TYPES[funckey_type],
                        u'label': funckey.get(u'label') or u'',
                        u'value': funckey.get(u'value') or u'',
                    },
                )
            )
            '''
        raw_config[u'XX_v2_fkeys'] = lines

    def _format_code(self, code):
        if code >= 10:
            str_code = str(code)
        else:
            str_code = u'0%s' % code
        return u'P3%s' % str_code

    def _add_dns(self, raw_config):
        if raw_config.get(u'dns_enabled'):
            dns_parts = raw_config[u'dns_ip'].split('.')
            for part_nb, part in enumerate(dns_parts, start=1):
                raw_config[u'XX_dns_%s' % part_nb] = part

    def _add_dtmf_mode(self, raw_config):
        if raw_config.get(u'sip_dtmf_mode'):
            dtmf_info = self.DTMF_MODES[raw_config[u'sip_dtmf_mode']]
            raw_config['XX_dtmf_in_audio'] = dtmf_info[0]
            raw_config['XX_dtmf_in_rtp'] = dtmf_info[1]
            raw_config['XX_dtmf_in_sip'] = dtmf_info[2]

    def _add_sip_transport(self, raw_config):
        sip_transport = raw_config.get(u'sip_transport')
        if sip_transport in self.SIP_TRANSPORTS:
            raw_config[u'XX_sip_transport'] = self.SIP_TRANSPORTS[sip_transport]
